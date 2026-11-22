"""Gaming: the MangoHud overlay preset (per user) and automatic gaming mode while a game runs.

forge-game runs as you. It finds games by reading /proc (a fake /proc here), and switches modes the
way forge-mode does: AI models unloaded with ollama, the power profile with powerprofilesctl, and the
one root step (the agents' CPU and memory limits) through the forge-mode-switch socket: a root service
that takes one mode word, checks who asks (an administrator with the active local session on seat0), and
sets the limits. Here a fake socket server stands in for it; every command is a fake on PATH.
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-game")
HELPER = os.path.join(ROOT, "files/system/usr/libexec/forge-mode-apply")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
USER_UNITS = os.path.join(ROOT, "files/system/usr/lib/systemd/user")
SYSTEM_UNITS = os.path.join(ROOT, "files/system/usr/lib/systemd/system")
POLKIT_RULE = os.path.join(ROOT, "files/system/usr/share/polkit-1/rules.d/90-forge-game-mode.rules")
SOCKET_UNIT = os.path.join(SYSTEM_UNITS, "forge-mode-switch.socket")
SWITCH_UNIT = os.path.join(SYSTEM_UNITS, "forge-mode-switch.service")
MANGOHUD_SRC = os.environ.get("FORGE_TEST_MANGOHUD_SRC", "/tmp/ws/game/scratch/src/MangoHud")

FAKES = {
    "systemctl": r'''#!/bin/sh
echo "systemctl $*" >> "$FAKE_CALLS"
case "$*" in
  *is-active*) exit "${FAKE_OLLAMA_INACTIVE:-0}" ;;
esac
exit 0
''',
    "powerprofilesctl": r'''#!/bin/sh
echo "powerprofilesctl $*" >> "$FAKE_CALLS"
if [ "$1" = get ]; then cat "$FAKE_POWER"; exit 0; fi
if [ "$1" = set ]; then echo "$2" > "$FAKE_POWER"; exit 0; fi
exit 1
''',
    "ollama": r'''#!/bin/sh
echo "ollama $*" >> "$FAKE_CALLS"
if [ "$1" = ps ]; then printf 'NAME ID SIZE\nqwen3:8b abc 5GB\nbad;name x y\n'; fi
exit 0
''',
    "notify-send": '#!/bin/sh\necho "notify-send $*" >> "$FAKE_CALLS"\n',
}


def load_tool(path=TOOL, name="forge_game"):
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    # forgecfg reads its paths once, when it is first imported: point it at this test's files.
    # (Another test module may have imported it first with other paths, so set both every time.)
    mod.forgecfg.SCHEMA_PATH = os.environ.get("FORGE_SCHEMA", SCHEMA)
    if "FORGE_CONF" in os.environ:
        mod.forgecfg.OVERRIDES_PATH = os.environ["FORGE_CONF"]
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.home = os.path.join(self.t, "home")
        os.makedirs(self.home)
        self.proc = os.path.join(self.t, "proc")
        os.makedirs(self.proc)
        self.bin = os.path.join(self.t, "bin")
        os.makedirs(self.bin)
        for name, text in FAKES.items():
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, 0o755)
        self.calls_file = os.path.join(self.t, "calls")
        self.power = os.path.join(self.t, "power")
        with open(self.power, "w") as f:
            f.write("balanced\n")
        self.conf = os.path.join(self.t, "settings.json")
        self.settings()
        self.publish = os.path.join(self.t, "publish")
        with open(self.publish, "w") as f:
            f.write(f'#!/bin/sh\necho publish >> "{self.calls_file}"\n')
        os.chmod(self.publish, 0o755)
        self.sock_path = os.path.join(self.t, "mode.sock")
        self.start_switch_server()
        self.old_env = dict(os.environ)
        self.addCleanup(self.restore_env)
        os.environ.update(self.env())
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("XDG_STATE_HOME", None)

    def start_switch_server(self):
        """Stands in for the root forge-mode-switch service: records "mode-switch WORD" and answers ok, or an
        error while FAKE_SYSTEMCTL_RC is not 0 (the root step failing)."""
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        srv.listen(8)
        srv.settimeout(0.2)
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    continue
                with conn:
                    conn.settimeout(5)
                    data = conn.recv(64).decode().strip()
                    with open(self.calls_file, "a") as f:
                        f.write(f"mode-switch {data}\n")
                    bad = os.environ.get("FAKE_SYSTEMCTL_RC", "0") != "0"
                    conn.sendall(b"error: not allowed now\n" if bad else b"ok\n")
        th = threading.Thread(target=loop, daemon=True)
        th.start()
        self.addCleanup(lambda: (stop.set(), th.join(2), srv.close()))

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        env.update(HOME=self.home, FORGE_LIB=LIB, FORGE_SCHEMA=SCHEMA, FORGE_CONF=self.conf,
                   FORGE_GAME_PROC=self.proc, FAKE_CALLS=self.calls_file, FAKE_POWER=self.power,
                   FORGE_ACTIVITY_PUBLISH=self.publish, FORGE_MODE_SOCKET=self.sock_path, PATH=self.bin + ":" + self.old_env.get("PATH", ""))
        env.update(extra)
        return env

    def settings(self, **vals):
        with open(self.conf, "w") as f:
            json.dump({k.replace("__", "."): v for k, v in vals.items()}, f)

    def calls(self):
        try:
            with open(self.calls_file) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def clear_calls(self):
        if os.path.exists(self.calls_file):
            os.remove(self.calls_file)

    def add_proc(self, pid, comm, args, state="S", uid=None):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in args) + b"\0")
        with open(os.path.join(d, "stat"), "w") as f:
            f.write(f"{pid} ({comm}) {state} 1 1 1 0 -1\n")
        if uid is not None:
            os.chown(d, uid, uid)
        return d

    def steam_game(self, pid=4242, appid="1091500"):
        return self.add_proc(pid, "reaper", ["/home/u/.local/share/Steam/ubuntu12_32/reaper", "SteamLaunch",
                                             f"AppId={appid}", "--", "/home/u/game/bin/game"])

    def end_game(self, pid=4242):
        shutil.rmtree(os.path.join(self.proc, str(pid)))

    def state_dir(self):
        return os.path.join(self.home, ".local/state/forge")

    def mode(self):
        try:
            with open(os.path.join(self.state_dir(), "mode")) as f:
                return f.read().strip()
        except FileNotFoundError:
            return None

    def set_mode(self, mode):
        os.makedirs(self.state_dir(), exist_ok=True)
        with open(os.path.join(self.state_dir(), "mode"), "w") as f:
            f.write(mode + "\n")

    def run_tool(self, *args, timeout=30, **extra):
        return subprocess.run([sys.executable, TOOL, *args], env=self.env(**extra), capture_output=True,
                              text=True, timeout=timeout)


# ---------- MangoHud overlay ----------

class OverlayTest(Base):
    def conf_file(self, base=None):
        return os.path.join(base or os.path.join(self.home, ".config"), "MangoHud", "MangoHud.conf")

    def read(self, path=None):
        with open(path or self.conf_file()) as f:
            return f.read()

    def options(self, text):
        out = {}
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip() if _ else "1"
        return out

    def test_minimal_is_an_fps_counter_with_the_toggle_key(self):
        self.settings(gaming__overlay="minimal")
        r = self.run_tool("overlay", "apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.read()
        self.assertTrue(text.startswith("# forge-managed"), text[:80])
        opts = self.options(text)
        self.assertEqual(opts.get("fps_only"), "1")
        self.assertEqual(opts.get("toggle_hud"), "Shift_R+F12")
        self.assertNotIn("no_display", opts)
        self.assertNotIn("gpu_stats", opts)
        self.assertEqual(stat.S_IMODE(os.stat(self.conf_file()).st_mode), 0o644)

    def test_full_shows_every_stat(self):
        self.settings(gaming__overlay="full")
        self.assertEqual(self.run_tool("overlay", "apply").returncode, 0)
        opts = self.options(self.read())
        for k in ("fps", "frametime", "frame_timing", "gpu_stats", "gpu_temp", "cpu_stats", "cpu_temp", "vram",
                  "ram"):
            self.assertEqual(opts.get(k), "1", k)
        self.assertNotIn("fps_only", opts)
        self.assertEqual(opts.get("toggle_hud"), "Shift_R+F12")

    def test_off_hides_the_overlay_and_never_creates_a_file(self):
        self.settings(gaming__overlay="off")
        self.assertEqual(self.run_tool("overlay", "apply").returncode, 0)
        self.assertFalse(os.path.exists(self.conf_file()), "off made a MangoHud file nobody asked for")
        self.settings(gaming__overlay="full")
        self.run_tool("overlay", "apply")
        self.settings(gaming__overlay="off")
        self.assertEqual(self.run_tool("overlay", "apply").returncode, 0)
        opts = self.options(self.read())
        self.assertEqual(opts.get("no_display"), "1")
        self.assertNotIn("gpu_stats", opts)

    def test_every_option_name_exists_in_mangohud(self):
        src = os.path.join(MANGOHUD_SRC, "src/overlay_params.h")
        if not os.path.exists(src):
            self.skipTest("MangoHud source not here (set FORGE_TEST_MANGOHUD_SRC)")
        with open(src) as f:
            known = set(re.findall(r"OVERLAY_PARAM_(?:BOOL|CUSTOM)\((\w+)\)", f.read()))
        game = load_tool()
        for preset in ("off", "minimal", "full"):
            for k in self.options(game.overlay_text(preset)):
                self.assertIn(k, known, f"{preset}: {k} is not a MangoHud option")

    def test_follows_xdg_config_home_like_mangohud(self):
        xdg = os.path.join(self.t, "xdg")
        self.settings(gaming__overlay="minimal")
        r = self.run_tool("overlay", "apply", XDG_CONFIG_HOME=xdg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.conf_file(xdg)))
        self.assertFalse(os.path.exists(self.conf_file()))

    def test_your_own_mangohud_file_is_never_changed(self):
        os.makedirs(os.path.dirname(self.conf_file()))
        with open(self.conf_file(), "w") as f:
            f.write("# my own\nfps_limit=60\n")
        self.settings(gaming__overlay="full")
        r = self.run_tool("overlay", "apply")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read(), "# my own\nfps_limit=60\n")
        self.assertIn("your own", (r.stdout + r.stderr).lower())

    def test_a_link_is_never_followed(self):
        os.makedirs(os.path.dirname(self.conf_file()))
        target = os.path.join(self.t, "elsewhere")
        with open(target, "w") as f:
            f.write("# forge-managed: MangoHud overlay\nkeep\n")
        os.symlink(target, self.conf_file())
        self.settings(gaming__overlay="full")
        self.run_tool("overlay", "apply")
        with open(target) as f:
            self.assertEqual(f.read(), "# forge-managed: MangoHud overlay\nkeep\n")
        self.assertTrue(os.path.islink(self.conf_file()))

    def test_no_global_switch_that_forces_it_on_for_every_app(self):
        # MANGOHUD=1 in the session environment would load it into every Vulkan and OpenGL app.
        for d, _, files in os.walk(os.path.join(ROOT, "files")):
            for name in files:
                p = os.path.join(d, name)
                if os.path.islink(p):
                    continue
                with open(p, "rb") as f:
                    data = f.read()
                self.assertNotRegex(data, rb"MANGOHUD(_DLSYM)?=1", p)
                if "environment.d" in p or name == "environment":
                    self.assertNotIn(b"MANGOHUD", data, p)

    def test_path_unit_reapplies_when_settings_change(self):
        path = open(os.path.join(USER_UNITS, "forge-game-overlay.path")).read()
        svc = open(os.path.join(USER_UNITS, "forge-game-overlay.service")).read()
        self.assertIn("PathChanged=/etc/forge/settings.json", path)
        self.assertIn("Unit=forge-game-overlay.service", path)
        self.assertIn("ExecStart=/usr/bin/forge-game overlay apply", svc)
        self.assertIn("Type=oneshot", svc)


# ---------- Finding games ----------

class DetectTest(Base):
    def test_steam_game_through_reaper(self):
        self.steam_game()
        game = load_tool()
        found = game.find_games(self.proc)
        self.assertEqual([(g["kind"], g["appid"]) for g in found], [("steam", "1091500")])

    def test_steam_itself_and_other_reapers_are_not_games(self):
        self.add_proc(100, "steam", ["/home/u/.local/share/Steam/ubuntu12_32/steam", "-srt-logger-opened"])
        self.add_proc(101, "steamwebhelper", ["steamwebhelper", "AppId=5"])
        self.add_proc(102, "reaper", ["reaper", "--", "something"])
        self.add_proc(103, "reaper", ["reaper", "SteamLaunch", "AppId=abc", "--"])
        self.assertEqual(load_tool().find_games(self.proc), [])

    def test_roblox_through_sober(self):
        self.add_proc(200, "sober", ["/app/bin/sober"])
        self.assertEqual([g["kind"] for g in load_tool().find_games(self.proc)], ["roblox"])

    def test_roblox_studio_is_not_a_game(self):
        # Studio is creative work: agents may keep helping while it runs.
        self.add_proc(300, "RobloxStudioBet", ["C:\\users\\u\\RobloxStudioBeta.exe"])
        self.assertEqual(load_tool().find_games(self.proc), [])

    def test_other_users_and_ended_processes_do_not_count(self):
        self.steam_game(pid=4242)
        os.chown(os.path.join(self.proc, "4242"), 65534, 65534)
        self.add_proc(4243, "reaper", ["reaper", "SteamLaunch", "AppId=10", "--"], state="Z")
        self.assertEqual(load_tool().find_games(self.proc), [])

    def test_odd_proc_entries_are_ignored(self):
        os.makedirs(os.path.join(self.proc, "self"))
        os.makedirs(os.path.join(self.proc, "77"))   # vanished half way: no files
        d = self.add_proc(78, "reaper", ["x"])
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"reaper\0SteamLaunch\0AppId=1\0" + b"A" * 300000)
        found = load_tool().find_games(self.proc)
        self.assertEqual([g["appid"] for g in found], ["1"])


# ---------- Automatic gaming mode ----------

class AutoModeTest(Base):
    def watcher(self):
        game = load_tool()
        return game, game.Watcher(proc=self.proc)

    def apply_calls(self):
        return [c for c in self.calls() if c.startswith("mode-switch ")]

    def test_game_start_switches_to_gaming_and_end_switches_back(self):
        self.set_mode("agents")
        game, w = self.watcher()
        w.tick()
        self.assertEqual(self.apply_calls(), [])
        self.steam_game()
        w.tick()
        self.assertEqual(self.mode(), "gaming")
        self.assertIn("mode-switch gaming", self.calls())
        self.assertIn("ollama stop qwen3:8b", self.calls())
        self.assertFalse(any("bad;name" in c for c in self.calls()), "an odd model name was passed on")
        self.assertIn("powerprofilesctl set performance", self.calls())
        with open(os.path.join(self.state_dir(), "activity.log")) as f:
            self.assertRegex(f.read(), r"\S+ mode=gaming\n")
        self.clear_calls()
        w.tick()   # still running: nothing again
        self.assertEqual(self.apply_calls(), [])
        self.end_game()
        for _ in range(game.EXIT_POLLS - 1):
            w.tick()
            self.assertEqual(self.mode(), "gaming", "switched back before the game was gone for a while")
        w.tick()
        self.assertEqual(self.mode(), "agents")
        self.assertIn("mode-switch agents", self.calls())
        self.assertIn("powerprofilesctl set balanced", self.calls())   # what it was before, not the mode default
        self.assertFalse(os.path.exists(os.path.join(self.state_dir(), "game-mode.json")))
        with open(os.path.join(self.state_dir(), "activity.log")) as f:
            self.assertTrue(f.read().endswith("mode=agents\n"))

    def test_back_to_default_when_no_mode_was_chosen(self):
        game, w = self.watcher()
        self.steam_game()
        w.tick()
        self.end_game()
        for _ in range(game.EXIT_POLLS):
            w.tick()
        self.assertIn("mode-switch default", self.calls())
        self.assertEqual(self.mode(), "default")

    def test_a_short_gap_between_launcher_and_game_does_not_switch_back(self):
        self.set_mode("creative")
        game, w = self.watcher()
        self.steam_game(pid=1)
        w.tick()
        self.end_game(pid=1)
        w.tick()
        self.steam_game(pid=2)
        for _ in range(game.EXIT_POLLS + 1):
            w.tick()
        self.assertEqual(self.mode(), "gaming")
        self.assertEqual(len([c for c in self.apply_calls() if c.endswith(" gaming")]), 1)
        self.assertEqual([c for c in self.apply_calls() if c.endswith(" creative")], [])

    def test_turned_off_does_nothing(self):
        self.settings(gaming__auto_mode=False)
        self.set_mode("agents")
        _, w = self.watcher()
        self.steam_game()
        w.tick()
        self.assertEqual(self.mode(), "agents")
        self.assertEqual(self.calls(), [])

    def test_turning_it_off_during_a_game_switches_back(self):
        self.set_mode("quiet")
        _, w = self.watcher()
        self.steam_game()
        w.tick()
        self.settings(gaming__auto_mode=False)
        w.tick()
        self.assertEqual(self.mode(), "quiet")

    def test_already_in_gaming_mode_is_left_alone(self):
        self.set_mode("gaming")
        game, w = self.watcher()
        self.steam_game()
        w.tick()
        self.end_game()
        for _ in range(game.EXIT_POLLS + 1):
            w.tick()
        self.assertEqual(self.mode(), "gaming")
        self.assertEqual(self.apply_calls(), [])

    def test_a_mode_you_pick_during_the_game_is_kept(self):
        self.set_mode("agents")
        game, w = self.watcher()
        self.steam_game()
        w.tick()
        os.utime(os.path.join(self.state_dir(), "mode"), (1, 1))
        self.set_mode("creative")   # forge-mode creative while the game runs
        self.end_game()
        for _ in range(game.EXIT_POLLS + 1):
            w.tick()
        self.assertEqual(self.mode(), "creative")
        self.assertEqual([c for c in self.apply_calls() if c.endswith(" agents")], [])

    def test_models_stay_when_the_gaming_mode_keeps_them(self):
        self.settings(modes__gaming__unload_models=False)
        _, w = self.watcher()
        self.steam_game()
        w.tick()
        self.assertFalse(any(c.startswith("ollama") for c in self.calls()))

    def test_models_are_not_asked_when_ollama_is_not_running(self):
        os.environ["FAKE_OLLAMA_INACTIVE"] = "3"
        _, w = self.watcher()
        self.steam_game()
        w.tick()
        self.assertFalse(any(c.startswith("ollama") for c in self.calls()))

    def test_broken_settings_never_switch(self):
        with open(self.conf, "w") as f:
            f.write('{"gaming.auto_mode": "maybe"}')
        self.set_mode("agents")
        _, w = self.watcher()
        self.steam_game()
        w.tick()
        self.assertEqual(self.mode(), "agents")

    def test_root_step_failing_keeps_trying_and_tells_the_truth(self):
        # F2 (rvGame): the switch back must not be forgotten when the root step fails.
        self.set_mode("agents")
        game, w = self.watcher()
        self.steam_game()
        w.tick()
        os.environ["FAKE_SYSTEMCTL_RC"] = "1"
        self.end_game()
        for _ in range(game.EXIT_POLLS):
            w.tick()
        self.assertEqual(self.mode(), "gaming", "the agent limits are still the gaming ones")
        with open(os.path.join(self.state_dir(), "game-mode.json")) as f:
            st = json.load(f)
        self.assertTrue(st["pending"])
        r = self.run_tool("status", "--json")
        d = json.loads(r.stdout)
        self.assertTrue(d["switch_back_pending"])
        self.assertIn("still", self.run_tool("status").stdout)
        self.assertEqual([c for c in self.calls() if c.startswith("powerprofilesctl set balanced")], [])
        os.environ["FAKE_SYSTEMCTL_RC"] = "0"
        self.clear_calls()
        for _ in range(game.EXIT_POLLS):
            w.tick()
        self.assertIn("mode-switch agents", self.calls())
        self.assertEqual(self.mode(), "agents")
        self.assertIn("powerprofilesctl set balanced", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.state_dir(), "game-mode.json")))

    def test_refused_on_entry_never_waits_to_switch_back_and_says_why(self):
        # rvGame round 2: a user the mode service always refuses got stuck in gaming mode.
        os.environ["FAKE_SYSTEMCTL_RC"] = "1"
        self.set_mode("agents")
        game, w = self.watcher()
        self.steam_game()
        w.tick()
        self.assertEqual(self.mode(), "gaming")
        notes = [c for c in self.calls() if c.startswith("notify-send")]
        self.assertEqual(len(notes), 1, self.calls())
        self.assertIn("not allowed now", notes[0])
        d = json.loads(self.run_tool("status", "--json").stdout)
        self.assertFalse(d["limits_set"])
        self.assertIn("not allowed now", d["limits_error"])
        self.clear_calls()
        self.end_game()
        for _ in range(game.EXIT_POLLS):
            w.tick()
        self.assertEqual(self.mode(), "agents")
        self.assertIn("powerprofilesctl set balanced", self.calls())
        self.assertEqual([c for c in self.calls() if c.startswith("mode-switch")], [],
                         "the limits never changed, so there is nothing to switch back")
        self.assertFalse(os.path.exists(os.path.join(self.state_dir(), "game-mode.json")))
        self.steam_game(pid=4300)   # the next game: the reason is not shown again the same day
        w.tick()
        self.assertEqual([c for c in self.calls() if c.startswith("notify-send")], [])

    def test_pending_switch_back_is_retried_at_the_next_login(self):
        self.set_mode("agents")
        game, w = self.watcher()
        self.steam_game()
        w.tick()
        os.environ["FAKE_SYSTEMCTL_RC"] = "1"
        self.end_game()
        self.assertEqual(self.run_tool("restore", "--force").returncode, 0)   # logout: fails, stays pending
        self.assertEqual(self.mode(), "gaming")
        os.environ["FAKE_SYSTEMCTL_RC"] = "0"
        game.Watcher(proc=self.proc).start()                                   # next login
        self.assertEqual(self.mode(), "agents")

    def test_no_switch_service_is_a_pending_switch_back_too(self):
        self.set_mode("agents")
        game, w = self.watcher()
        self.steam_game()
        w.tick()
        self.end_game()
        os.environ["FORGE_MODE_SOCKET"] = os.path.join(self.t, "missing.sock")
        for _ in range(game.EXIT_POLLS):
            w.tick()
        self.assertEqual(self.mode(), "gaming")
        self.assertTrue(os.path.exists(os.path.join(self.state_dir(), "game-mode.json")))


class StuckGuardTest(Base):
    """However the watcher stops (crash, kill, logout, power loss), the PC never stays in gaming mode."""

    def start_game_mode(self):
        self.set_mode("agents")
        game = load_tool()
        w = game.Watcher(proc=self.proc)
        self.steam_game()
        w.tick()
        self.assertEqual(self.mode(), "gaming")
        self.clear_calls()
        return game

    def test_restore_after_the_watcher_died_and_the_game_ended(self):
        self.start_game_mode()
        self.end_game()
        r = self.run_tool("restore")   # ExecStopPost, and the first thing a new watcher does
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mode(), "agents")
        self.assertIn("mode-switch agents", self.calls())

    def test_restore_while_the_game_still_runs_waits(self):
        self.start_game_mode()
        r = self.run_tool("restore")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.mode(), "gaming")
        r = self.run_tool("restore", "--force")   # the service stops: logout or the watcher is turned off
        self.assertEqual(self.mode(), "agents")

    def test_new_watcher_cleans_up_after_a_power_loss(self):
        game = self.start_game_mode()
        self.end_game()
        w = game.Watcher(proc=self.proc)   # next login
        w.start()
        self.assertEqual(self.mode(), "agents")

    def test_watch_runs_restore_first_even_when_turned_off(self):
        self.start_game_mode()
        self.end_game()
        self.settings(gaming__auto_mode=False)
        r = self.run_tool("watch", "--once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mode(), "agents")

    def test_broken_state_file_is_dropped_safely(self):
        os.makedirs(self.state_dir())
        with open(os.path.join(self.state_dir(), "game-mode.json"), "w") as f:
            f.write('{"prev": "$(reboot)", "prev_power": "--help"}')
        self.set_mode("gaming")
        r = self.run_tool("restore")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("$(reboot)", " ".join(self.calls()))
        self.assertNotIn("--help", " ".join(self.calls()))
        self.assertIn("mode-switch default", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.state_dir(), "game-mode.json")))

    def test_state_file_link_is_refused(self):
        os.makedirs(self.state_dir())
        os.symlink("/etc/passwd", os.path.join(self.state_dir(), "game-mode.json"))
        r = self.run_tool("restore")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_service_restores_on_any_stop_and_restarts_after_a_crash(self):
        svc = open(os.path.join(USER_UNITS, "forge-game.service")).read()
        self.assertIn("ExecStart=/usr/bin/forge-game watch", svc)
        self.assertIn("ExecStopPost=/usr/bin/forge-game restore --force", svc)
        self.assertIn("Restart=on-failure", svc)
        self.assertIn("PartOf=graphical-session.target", svc)

    def test_status(self):
        self.start_game_mode()
        r = self.run_tool("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["auto_mode"], True)
        self.assertEqual(d["switched_by_game"], True)
        self.assertEqual(d["back_to"], "agents")
        self.assertEqual(d["games"][0]["appid"], "1091500")


# ---------- The root step ----------

class ModeApplyHelperTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.bin = os.path.join(self.t, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.t, "calls")
        p = os.path.join(self.bin, "systemctl")
        with open(p, "w") as f:
            f.write(f'#!/bin/sh\necho "systemctl $*" >> {self.log}\n')
        os.chmod(p, 0o755)
        self.conf = os.path.join(self.t, "settings.json")
        self.settings({})

    def settings(self, vals):
        with open(self.conf, "w") as f:
            json.dump(vals, f)

    def run_helper(self, *args):
        env = dict(os.environ, FORGE_LIB=LIB, FORGE_SCHEMA=SCHEMA, FORGE_CONF=self.conf,
                   PATH=self.bin + ":" + os.environ["PATH"])
        return subprocess.run([sys.executable, HELPER, *args], env=env, capture_output=True, text=True, timeout=30)

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_each_mode_sets_only_the_agent_limits(self):
        self.settings({"modes.gaming.cpu_weight": 15, "modes.gaming.memory_high_pct": 20})
        r = self.run_helper("gaming")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), ["systemctl set-property --runtime agents.slice CPUWeight=15 MemoryHigh=20%"])

    def test_default_uses_the_agent_settings(self):
        self.settings({"agents.cpu_weight": 70, "agents.memory_high_pct": 60})
        self.assertEqual(self.run_helper("default").returncode, 0)
        self.assertEqual(self.calls(), ["systemctl set-property --runtime agents.slice CPUWeight=70 MemoryHigh=60%"])

    def test_unknown_or_odd_names_are_refused(self):
        for bad in ("", "gaming;reboot", "GAMING", "../gaming", "x" * 300):
            r = self.run_helper(bad)
            self.assertNotEqual(r.returncode, 0, bad)
        r = self.run_helper("gaming", "extra")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_broken_settings_change_nothing(self):
        with open(self.conf, "w") as f:
            f.write('{"modes.gaming.cpu_weight": "lots"}')
        self.run_helper("gaming")
        self.assertEqual(self.calls(), [])

    def test_the_old_password_free_unit_and_polkit_rule_are_gone(self):
        # F1 (rvGame): polkit can't see the job mode, so "start --job-mode=flush" was allowed too.
        self.assertFalse(os.path.exists(POLKIT_RULE))
        self.assertFalse(os.path.exists(os.path.join(SYSTEM_UNITS, "forge-mode-apply@.service")))
        for d, _, files in os.walk(os.path.join(ROOT, "files")):
            for name in files:
                if name.endswith(".rules") and "polkit" in d:
                    with open(os.path.join(d, name)) as f:
                        self.assertNotIn("forge-mode-apply", f.read())

    def test_socket_and_service_are_locked_down(self):
        with open(SOCKET_UNIT) as f:
            sock = f.read()
        for line in ("ListenStream=/run/forge-mode.sock", "SocketUser=root", "SocketGroup=wheel", "SocketMode=0660",
                     "Accept=no", "WantedBy=sockets.target"):
            self.assertIn(line, sock)
        with open(SWITCH_UNIT) as f:
            svc = f.read()
        for line in ("ExecStart=/usr/libexec/forge-mode-apply --serve", "CapabilityBoundingSet=CAP_SYS_ADMIN",
                     "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                     "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX", "ProtectKernelTunables=yes",
                     "ProtectKernelModules=yes", "ProtectControlGroups=yes", "RestrictNamespaces=yes",
                     "LockPersonality=yes", "SystemCallArchitectures=native", "IPAddressDeny=any",
                     "ProtectProc=invisible"):
            self.assertIn(line, svc)
        self.assertNotIn("[Install]", svc)
        analyze = shutil.which("systemd-analyze")
        if analyze:     # the unit files parse (the program path is swapped for one that exists here)
            d = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, d, True)
            shutil.copy(SOCKET_UNIT, d)
            with open(os.path.join(d, "forge-mode-switch.service"), "w") as f:
                f.write(svc.replace("/usr/libexec/forge-mode-apply", "/usr/bin/true"))
            r = subprocess.run([analyze, "verify", os.path.join(d, "forge-mode-switch.socket"),
                                os.path.join(d, "forge-mode-switch.service")], capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Unknown", r.stderr)
        rec = open(os.path.join(ROOT, "recipes/common-modules.yml")).read()
        self.assertRegex(rec, r"system:\s*\n\s+enabled:\s*\n(\s+- .*\n)*\s+- forge-mode-switch\.socket")


SERVE_WRAPPER = r"""
import os, sys
fd = int(sys.argv[1])
os.dup2(fd, 3)
os.environ["LISTEN_FDS"] = "1"
os.environ["LISTEN_PID"] = str(os.getpid())
os.execv(sys.executable, [sys.executable, sys.argv[2], "--serve"])
"""

CLIENT = r"""
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(30)
s.connect(sys.argv[1])
try:
    s.sendall(sys.argv[2].encode("utf-8", "surrogateescape"))
except BrokenPipeError:
    pass    # refused before reading: the answer is still there
print(s.recv(512).decode().strip())
"""

HOLD = r"""
import socket, sys, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
print("connected", flush=True)
time.sleep(float(sys.argv[2]))
"""

LOGINCTL = r'''#!/bin/sh
echo "loginctl $*" >> "$FAKE_CALLS"
case "$1" in
  show-seat) echo "${FAKE_SEAT_SESSION-c2}" ;;
  show-session) printf 'User=%s\nRemote=%s\nActive=%s\nType=%s\nClass=user\nState=%s\n' \
      "${FAKE_SESSION_USER:-0}" "${FAKE_SESSION_REMOTE:-no}" "${FAKE_SESSION_ACTIVE:-yes}" \
      "${FAKE_SESSION_TYPE:-wayland}" "${FAKE_SESSION_STATE:-active}" ;;
esac
'''


@unittest.skipUnless(os.geteuid() == 0, "needs root to connect as other uids")
class ModeSwitchServerTest(unittest.TestCase):
    """The root side: forge-mode-apply --serve on a socket, as systemd would start it."""

    def setUp(self):
        self.t = tempfile.mkdtemp()
        os.chmod(self.t, 0o755)
        self.addCleanup(shutil.rmtree, self.t, True)
        self.bin = os.path.join(self.t, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.t, "calls")
        for name, text in (("systemctl", f'#!/bin/sh\necho "systemctl $*" >> "{self.log}"\n'), ("loginctl", LOGINCTL)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, 0o755)
        self.conf = os.path.join(self.t, "settings.json")
        with open(self.conf, "w") as f:
            json.dump({"modes.gaming.cpu_weight": 15, "modes.gaming.memory_high_pct": 20}, f)
        self.path = os.path.join(self.t, "mode.sock")
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def serve(self, test_marker=True, **env):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        os.chmod(self.path, 0o666)
        srv.listen(16)
        e = {"PATH": self.bin + ":/usr/bin:/bin", "FORGE_LIB": LIB, "FORGE_SCHEMA": SCHEMA, "FORGE_CONF": self.conf,
             "FAKE_CALLS": self.log, "FORGE_MODE_SWITCH_GROUP": "root", "FORGE_MODE_SWITCH_IDLE_S": "20"}
        if test_marker:
            e["FORGE_MODE_SWITCH_TEST"] = "1"
        e.update(env)
        self.proc = subprocess.Popen([sys.executable, "-c", SERVE_WRAPPER, str(srv.fileno()), HELPER],
                                     pass_fds=[srv.fileno()], env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
        srv.close()

    @staticmethod
    def as_uid(uid):
        def drop():
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
        return drop

    def ask(self, word, uid=0):
        def as_uid():
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
        r = subprocess.run([sys.executable, "-I", "-c", CLIENT, self.path, word], preexec_fn=as_uid,
                           capture_output=True, text=True, timeout=40)
        return r.stdout.strip() or r.stderr.strip()[-200:]

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def set_property_calls(self):
        return [c for c in self.calls() if c.startswith("systemctl")]

    def test_an_admin_with_the_active_local_session_gets_the_mode(self):
        self.serve()
        self.assertEqual(self.ask("gaming\n"), "ok")
        self.assertEqual(self.set_property_calls(),
                         ["systemctl set-property --runtime agents.slice CPUWeight=15 MemoryHigh=20%"])
        self.assertIn("loginctl show-seat seat0 --property=ActiveSession --value", self.calls())

    def test_other_users_are_refused(self):
        self.serve()
        self.assertTrue(self.ask("gaming\n", uid=65534).startswith("error"))
        self.assertTrue(self.ask("gaming\n", uid=65533).startswith("error"))   # no account at all
        self.assertEqual(self.set_property_calls(), [])

    def test_without_the_test_marker_the_real_admin_group_is_used(self):
        self.serve(test_marker=False)
        import grp
        try:
            members = grp.getgrnam("wheel")
            in_wheel = members.gr_gid == 0 or "root" in members.gr_mem
        except KeyError:
            in_wheel = False
        reply = self.ask("gaming\n")
        if not in_wheel:
            self.assertTrue(reply.startswith("error"), reply)
            self.assertEqual(self.set_property_calls(), [])

    def test_the_session_must_be_active_local_graphical_and_yours(self):
        for env in ({"FAKE_SEAT_SESSION": ""}, {"FAKE_SEAT_SESSION": "c2;rm"}, {"FAKE_SESSION_USER": "1000"},
                    {"FAKE_SESSION_REMOTE": "yes"}, {"FAKE_SESSION_ACTIVE": "no"}, {"FAKE_SESSION_TYPE": "tty"},
                    {"FAKE_SESSION_STATE": "online"}):
            with self.subTest(env=env):
                self.serve(**env)
                reply = self.ask("gaming\n")
                self.assertTrue(reply.startswith("error"), reply)
                self.proc.kill()
                self.proc.wait()
                os.unlink(self.path)
        self.assertEqual(self.set_property_calls(), [])

    def test_only_one_known_word(self):
        self.serve()
        for word in ("", "\n", "GAMING\n", "gaming extra\n", "gaming;reboot\n", "../gaming\n", "x" * 5000,
                     "gaming\ngaming\n", "--job-mode=flush\n", "g\u00e4ming\n"):
            with self.subTest(word=word[:20]):
                self.assertTrue(self.ask(word).startswith("error"), word[:20])
        self.assertEqual(self.set_property_calls(), [])
        self.proc.kill()
        self.proc.wait()
        os.unlink(self.path)
        self.serve()   # a fresh rate limit: every connection counts, also the refused ones
        for word in ("agents\n", "creative\n", "quiet\n", "default\n"):
            self.assertEqual(self.ask(word), "ok")

    def test_requests_are_rate_limited(self):
        self.serve()
        replies = [self.ask("quiet\n") for _ in range(14)]
        self.assertIn("ok", replies)
        self.assertTrue(any(r.startswith("error: too many") for r in replies), replies)
        self.assertLessEqual(len(self.set_property_calls()), 10)

    def test_a_silent_client_of_another_user_does_not_block_the_next_one(self):
        self.serve()
        hold = subprocess.Popen([sys.executable, "-I", "-c", HOLD, self.path, "20"], preexec_fn=self.as_uid(65534),
                                stdout=subprocess.PIPE, text=True)
        self.assertEqual(hold.stdout.readline().strip(), "connected")
        started = time.monotonic()
        self.assertEqual(self.ask("gaming\n"), "ok")
        self.assertLess(time.monotonic() - started, 1.5)
        hold.kill()
        hold.wait()
        hold.stdout.close()

    def test_one_connection_at_a_time_per_user_and_silent_ones_count(self):
        # rvGame round 2 (N1): silent connections held the single-threaded service 2 s each.
        self.serve()
        held = []
        for _ in range(3):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(self.path)
            held.append(s)
        time.sleep(0.2)
        started = time.monotonic()
        replies = [s.recv(256).decode().strip() for s in held[1:]]   # refused at once, before any read
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(all(r.startswith("error: busy") for r in replies), replies)
        self.assertTrue(held[0].recv(256).decode().startswith("error"))   # dropped after 0.5 s of silence
        for s in held:
            s.close()
        # Only the connection that got the slot counts (round 4): 1 silent + 9 = 10 per minute.
        replies = [self.ask("quiet\n") for _ in range(10)]
        self.assertEqual(replies[:9], ["ok"] * 9)
        self.assertTrue(replies[9].startswith("error: too many"), replies)

    def test_a_burst_refused_as_busy_does_not_lock_you_out(self):
        self.serve()
        held = []
        for _ in range(12):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(self.path)
            held.append(s)
        time.sleep(0.8)     # the one that got the slot sent nothing and was dropped
        for s in held:
            s.close()
        self.assertEqual(self.ask("gaming\n"), "ok")

    def test_exits_when_idle_and_refuses_a_socket_not_meant_for_it(self):
        self.serve(FORGE_MODE_SWITCH_IDLE_S="1")
        self.assertEqual(self.proc.wait(10), 0)
        os.unlink(self.path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        srv.listen(1)
        env = {"PATH": "/usr/bin:/bin", "FORGE_LIB": LIB, "LISTEN_FDS": "1", "LISTEN_PID": "1"}
        r = subprocess.run([sys.executable, HELPER, "--serve"], env=env, pass_fds=[srv.fileno()],
                           capture_output=True, text=True, timeout=20)
        srv.close()
        self.assertNotEqual(r.returncode, 0)


class ImageTest(unittest.TestCase):
    def test_tools_are_executable_and_units_enabled(self):
        fin = open(os.path.join(ROOT, "files/scripts/finalize.sh")).read()
        self.assertIn("/usr/bin/forge-game", fin)
        self.assertIn("/usr/libexec/forge-mode-apply", fin)
        rec = open(os.path.join(ROOT, "recipes/common-modules.yml")).read()
        for unit in ("forge-game.service", "forge-game-overlay.path", "forge-game-overlay.service"):
            self.assertRegex(rec, r"user:\s*\n\s+enabled:\s*\n(\s+- .*\n)*\s+- " + re.escape(unit))

    def test_settings(self):
        schema = json.load(open(SCHEMA))
        keys = {s["key"]: s for s in schema["settings"]}
        self.assertIn("gaming", [g["id"] for g in schema["groups"]])
        ov = keys["gaming.overlay"]
        self.assertEqual(ov["type"], "select")
        self.assertEqual(ov["options"], ["off", "minimal", "full"])
        self.assertEqual(ov["default"], "off")
        self.assertIn("Right Shift+F12", ov["help"])
        am = keys["gaming.auto_mode"]
        self.assertEqual(am["type"], "toggle")
        self.assertIs(am["default"], True)
        for s in (ov, am):
            self.assertEqual(s["group"], "gaming")
            self.assertTrue(s["label"] and s["help"] and s["tags"])
            self.assertNotIn("\u2014", s["help"])
            self.assertNotIn("--", s["help"])


if __name__ == "__main__":
    unittest.main()
