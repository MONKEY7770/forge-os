"""Background section: Wallpaper Engine style backgrounds, per user, no root.

Covers the small framework extension it needs (text fields with "choices": "backend", and show_if
that hides a field whose controlling field is hidden), the backend
/usr/libexec/forge-appearance-background (fake dbus-send, qdbus, systemctl, systemd-run and
kwriteconfig6 on PATH that record their argv), the Steam workshop listing, the crash guard, the
preview hook and the pinned plugin downloads.
"""
import hashlib
import importlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
BACKEND = os.path.join(ROOT, "files/system/usr/libexec/forge-appearance-background")
SHIPPED = os.path.join(ROOT, "files/system/usr/share/forge/appearance.json")
PREVIEW = os.path.join(ROOT, "files/system/usr/share/forge/appearance-preview/background.js")
VIDEO_SCRIPT = os.path.join(ROOT, "files/scripts/install-video-wallpaper.sh")
WE_SCRIPT = os.path.join(ROOT, "files/scripts/install-wallpaper-engine.sh")
RECIPE = os.path.join(ROOT, "recipes/common-modules.yml")
FINALIZE = os.path.join(ROOT, "files/scripts/finalize.sh")
NODE = shutil.which("node") or ("/opt/node22/bin/node" if os.path.exists("/opt/node22/bin/node") else None)

sys.path.insert(0, LIB)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_appearance import png, SAMPLES  # noqa: E402


def text_field(key, **extra):
    f = {"key": key, "type": "text", "label": key, "help": "", "default": "", "max_length": 20}
    f.update(extra)
    return f


CHOICE_SCHEMA = {"version": 1, "sections": [
    {"id": "background", "label": "Background", "help": "", "fields": [
        {"key": "type", "type": "select", "label": "Type", "help": "", "default": "a",
         "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]},
        text_field("item", choices="backend", show_if={"key": "type", "equals": "b"}),
        {"key": "mode", "type": "select", "label": "Mode", "help": "", "default": "x",
         "options": [{"value": "x", "label": "X"}, {"value": "y", "label": "Y"}],
         "show_if": {"key": "type", "equals": "b"}},
        {"key": "level", "type": "number", "label": "Level", "help": "", "default": 1, "min": 0, "max": 9,
         "show_if": {"key": "mode", "equals": "y"}},
        text_field("plain"),
    ]},
    {"id": "dock", "label": "Dock", "help": "", "fields": []},
]}


class LibBase(unittest.TestCase):
    schema_raw = CHOICE_SCHEMA

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.schema_file = os.path.join(self.tmp, "schema.json")
        with open(self.schema_file, "w") as f:
            json.dump(self.schema_raw, f)
        self.env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
            "XDG_DATA_HOME": os.path.join(self.tmp, "data"),
            "FORGE_APPEARANCE_SCHEMA": self.schema_file,
            "FORGE_APPEARANCE_LIBEXEC": os.path.join(self.tmp, "libexec"),
        })
        self.env.start()
        import forgeappearance
        self.fa = importlib.reload(forgeappearance)
        self.schema = self.fa.load_schema()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backend(self, section, body):
        d = os.path.join(self.tmp, "libexec")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "forge-appearance-" + section)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
        return p


# 1. Framework: text fields whose choices come from the section's backend

class ChoicesSchemaTest(LibBase):
    def test_choices_is_allowed_only_on_text_fields_and_only_as_backend(self):
        self.assertEqual(self.fa.schema_problems(CHOICE_SCHEMA), [])
        item = {f["key"]: f for f in self.schema["sections"][0]["fields"]}["item"]
        self.assertEqual(item["choices"], "backend", "the key must be kept in the clean schema")
        bad = json.loads(json.dumps(CHOICE_SCHEMA))
        bad["sections"][0]["fields"] = [
            text_field("t1", choices="https://example.invalid/list"),
            text_field("t2", choices=True),
            {"key": "s", "type": "select", "label": "s", "help": "", "default": "a", "choices": "backend",
             "options": [{"value": "a", "label": "A"}]},
            text_field("ok"),
        ]
        problems = self.fa.schema_problems(bad)
        self.assertEqual(len(problems), 3, problems)
        self.assertEqual([f["key"] for f in self.fa.clean_schema(bad)["sections"][0]["fields"]], ["ok"])

    def test_values_of_a_choices_field_are_checked_like_text(self):
        _, errors = self.fa.check_values({"background": {"item": "12345"}}, self.schema)
        self.assertEqual(errors, [])
        _, errors = self.fa.check_values({"background": {"item": "x" * 21}}, self.schema)
        self.assertTrue(errors)


class CascadeTest(LibBase):
    def test_a_field_is_hidden_when_the_field_it_depends_on_is_hidden(self):
        fields = self.schema["sections"][0]["fields"]
        f = {x["key"]: x for x in fields}
        vals = {"type": "a", "mode": "y", "level": 1, "item": "", "plain": ""}
        # Old behaviour, without the section's fields: only the direct condition counts.
        self.assertTrue(self.fa.visible(f["level"], vals))
        # With the fields: "mode" is hidden (type is a), so "level" is hidden too.
        self.assertFalse(self.fa.visible(f["level"], vals, fields))
        self.assertTrue(self.fa.visible(f["level"], dict(vals, type="b"), fields))
        self.assertFalse(self.fa.visible(f["level"], dict(vals, type="b", mode="x"), fields))
        self.assertTrue(self.fa.visible(f["plain"], vals, fields))

    def test_a_show_if_loop_never_hangs(self):
        fields = [
            {"key": "a", "type": "toggle", "label": "a", "default": True, "show_if": {"key": "b", "equals": True}},
            {"key": "b", "type": "toggle", "label": "b", "default": True, "show_if": {"key": "a", "equals": True}},
        ]
        self.assertTrue(self.fa.visible(fields[0], {"a": True, "b": True}, fields))
        self.assertFalse(self.fa.visible(fields[0], {"a": True, "b": False}, fields))


OPTIONS_BACKEND = """#!%s
import json, sys
if sys.argv[1:] != ["options"]:
    print("wrong argv", sys.argv[1:]); sys.exit(9)
got = json.load(sys.stdin)
open(%r, "w").write(json.dumps(got))
print(json.dumps({"fields": {
  "item": {"choices": [{"value": "111", "label": "Rain (video)"},
                       {"value": "bad value with spaces and far too long", "label": "dropped"},
                       {"value": "222", "label": "Bad\\u0007label"},
                       {"value": "333", "label": "City " + "x" * 300},
                       {"value": 444, "label": "not text"},
                       {"value": "555"}],
           "note": "Found 1 wallpaper."},
  "plain": {"choices": [{"value": "nope", "label": "plain has no choices key"}]},
  "type": {"choices": [{"value": "a", "label": "A"}]},
  "unknown": {"choices": [{"value": "x", "label": "X"}]}
}}))
"""


class SectionChoicesTest(LibBase):
    def setUp(self):
        super().setUp()
        # The library refuses root (appearance is per user); the test container runs as root.
        euid = mock.patch.object(self.fa.os, "geteuid", return_value=1000)
        euid.start()
        self.addCleanup(euid.stop)

    def test_runs_the_backend_with_argv_options_and_keeps_only_good_choices(self):
        got = os.path.join(self.tmp, "got.json")
        self.backend("background", OPTIONS_BACKEND % (sys.executable, got))
        state, data, text = self.fa.section_choices("background", self.schema)
        self.assertEqual(state, "ok", text)
        self.assertEqual(list(data), ["item"])
        self.assertEqual(data["item"]["choices"][0], {"value": "111", "label": "Rain (video)"})
        values = [c["value"] for c in data["item"]["choices"]]
        self.assertEqual(values, ["111", "333"])          # bad value, control character, number, no label dropped
        self.assertLessEqual(len(data["item"]["choices"][1]["label"]), self.fa.CHOICE_LABEL_MAX)
        self.assertEqual(data["item"]["note"], "Found 1 wallpaper.")
        with open(got) as f:
            payload = json.load(f)
        self.assertEqual(payload["section"], "background")
        self.assertIn("values", payload)

    def test_no_choices_fields_or_no_backend_runs_nothing(self):
        self.assertEqual(self.fa.section_choices("dock", self.schema)[0], "none")
        self.assertEqual(self.fa.section_choices("background", self.schema)[0], "not-installed")
        with self.assertRaises(ValueError):
            self.fa.section_choices("../background", self.schema)

    def test_broken_slow_or_noisy_backend_gives_no_choices(self):
        for body in ("#!/bin/sh\necho not json\n", "#!/bin/sh\nexit 3\n", "#!/bin/sh\necho '[1,2]'\n",
                     "#!/bin/sh\necho '{\"fields\": 5}'\n"):
            self.backend("background", body)
            state, data, text = self.fa.section_choices("background", self.schema)
            self.assertEqual(data, {}, body)
        # Text on stderr does not spoil the JSON on stdout.
        self.backend("background", "#!/bin/sh\necho warning >&2\n"
                                   "echo '{\"fields\": {\"item\": {\"choices\": [{\"value\": \"7\", \"label\": \"Seven\"}]}}}'\n")
        state, data, text = self.fa.section_choices("background", self.schema)
        self.assertEqual(data, {"item": {"choices": [{"value": "7", "label": "Seven"}], "note": ""}}, text)
        self.backend("background", "#!/bin/sh\nsleep 5\n")
        with mock.patch.object(self.fa, "CHOICES_TIMEOUT_S", 1):
            t0 = time.time()
            state, data, text = self.fa.section_choices("background", self.schema)
        self.assertEqual((state, data), ("failed", {}))
        self.assertLess(time.time() - t0, 4)
        self.backend("background", "#!/bin/sh\nhead -c 3000000 /dev/zero | tr '\\\\0' x\n")
        state, data, text = self.fa.section_choices("background", self.schema)
        self.assertEqual((state, data), ("failed", {}))


# 2. The backend: /usr/libexec/forge-appearance-background

FAKE_TOOL = r'''#!%s
# Test stand-in: records its argv, answers from FAKE_CONF (rules matched on the joined argv).
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"tool": name, "argv": sys.argv[1:]}) + "\n")
conf = {}
if os.path.exists(os.environ.get("FAKE_CONF", "")):
    with open(os.environ["FAKE_CONF"]) as f:
        conf = json.load(f)
joined = " ".join(sys.argv[1:])
for i, rule in enumerate(conf.get(name, [])):
    if rule.get("match", "") in joined:
        counter = os.path.join(os.path.dirname(os.environ["FAKE_LOG"]), "count-%%s-%%d" %% (name, i))
        n = int(open(counter).read()) if os.path.exists(counter) else 0
        open(counter, "w").write(str(n + 1))
        outs = rule.get("outputs", [""])
        sys.stdout.write(outs[min(n, len(outs) - 1)])
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("rc", 0))
'''
TOOLS = ("dbus-send", "qdbus6", "systemctl", "systemd-run", "kwriteconfig6")
VIDEO_ID = "luisbocanegra.smart.video.wallpaper.reborn"
WE_ID = "com.github.captsilver.wallpaperEngineKde"
DBUS_OK = 'method return time=1.0 sender=:1.5 -> destination=:1.9 serial=7 reply_serial=2\n   string "forge-ok 2"\n'


def load_backend():
    loader = importlib.machinery.SourceFileLoader("forge_appearance_background", BACKEND)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class BackendBase(LibBase):
    schema_raw = None

    def setUp(self):
        self.tmp0 = tempfile.mkdtemp()
        with open(SHIPPED) as f:
            self.schema_raw = json.load(f)
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        self.log = os.path.join(self.tmp, "fake.log")
        self.conf_path = os.path.join(self.tmp, "fake.json")
        self.walls = os.path.join(self.tmp, "wallpapers")
        self.drm = os.path.join(self.tmp, "drm")
        os.makedirs(self.walls)
        os.makedirs(self.drm)
        for t in TOOLS:
            self.tool(t)
        self.env2 = mock.patch.dict(os.environ, {
            "HOME": self.home, "PATH": self.fakebin, "FAKE_LOG": self.log, "FAKE_CONF": self.conf_path,
            "XDG_STATE_HOME": os.path.join(self.tmp, "state"), "FORGE_PLASMA_WALLPAPERS": self.walls,
            "FORGE_SYS_DRM": self.drm, "FORGE_LIB": LIB,
        })
        self.env2.start()
        self.conf({"dbus-send": [{"outputs": [DBUS_OK]}],
                   "systemctl": [{"match": "NRestarts", "outputs": nrestarts(3)},
                                 {"match": "ActiveState", "outputs": ["active\n"]}]})
        self.bg = load_backend()
        self.bg.is_root = lambda: False      # these tests run as root; the real tool refuses root
        self.bg.WATCH_S, self.bg.POLL_S, self.bg.STOP_WAIT_S = 0.3, 0.02, 0.3

    def tearDown(self):
        self.env2.stop()
        super().tearDown()
        shutil.rmtree(self.tmp0, ignore_errors=True)

    def tool(self, name):
        p = os.path.join(self.fakebin, name)
        with open(p, "w") as f:
            f.write(FAKE_TOOL % sys.executable)
        os.chmod(p, 0o755)

    def conf(self, c):
        with open(self.conf_path, "w") as f:
            json.dump(c, f)
        for n in os.listdir(self.tmp):
            if n.startswith("count-"):
                os.unlink(os.path.join(self.tmp, n))

    def pend(self, plugin, restarts, gen="g1"):
        self.bg.write_state("current.json", {"gen": gen})
        self.bg.write_state("pending.json", {"plugin": plugin, "restarts": restarts, "at": time.time(), "gen": gen})

    def clear(self):
        if os.path.exists(self.log):
            os.unlink(self.log)

    def calls(self, tool=None):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            out = [json.loads(line) for line in f]
        return [c["argv"] for c in out if c["tool"] == tool] if tool else out

    def install_plugin(self, pid):
        d = os.path.join(self.walls, pid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "metadata.json"), "w") as f:
            json.dump({"KPlugin": {"Id": pid}}, f)

    def payload(self, **values):
        vals = self.fa.load(self.schema)
        vals["background"].update(values)
        clean, errors = self.fa.check_values({"background": vals["background"]}, self.schema)
        self.assertEqual(errors, [])
        self.fa.save(clean, self.schema)
        return self.fa._backend_payload("background", self.schema, self.fa.sections(self.schema))

    def run_cmd(self, cmd, payload=None):
        out = io.StringIO()
        stdin = io.StringIO(json.dumps(payload) if payload is not None else "")
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(out):
            rc = self.bg.main([cmd])
        return rc, out.getvalue()

    def apply(self, **values):
        rc, out = self.run_cmd("apply", self.payload(**values))
        self.assertEqual(self.calls("kwriteconfig6"), [], "apply must never write the applets file")
        return rc, out

    def sent_script(self):
        sent = self.calls("dbus-send")
        self.assertEqual(len(sent), 1, sent)
        argv = sent[0]
        self.assertEqual(argv[:-1], ["--session", "--print-reply", "--reply-timeout=30000",
                                     "--dest=org.kde.plasmashell", "/PlasmaShell",
                                     "org.kde.PlasmaShell.evaluateScript"])
        self.assertTrue(argv[-1].startswith("string:"))
        return argv[-1][len("string:"):]

    def expect_script(self, plugin, config, connector=None):
        params = {"plugin": plugin, "config": config, "connector": connector}
        want = self.bg.SCRIPT_TEMPLATE.replace("__FORGE_PARAMS__",
                                               json.dumps(params, ensure_ascii=True, sort_keys=True))
        self.assertEqual(self.sent_script(), want)
        return want

    def add(self, name, data):
        return self.fa.media_add(self.file(name, data))

    def file(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p


class BackendSchemaTest(BackendBase):
    def test_shipped_schema_is_valid_and_offers_only_what_the_backend_applies(self):
        self.assertEqual(self.fa.schema_problems(self.schema_raw), [])
        bg = {s["id"]: s for s in self.schema_raw["sections"]}["background"]
        keys = {f["key"] for f in bg["fields"]}
        self.assertEqual(keys, set(self.bg.USED_KEYS), "every field must be used by the backend, and only those")
        f = {x["key"]: x for x in bg["fields"]}
        self.assertEqual([o["value"] for o in f["type"]["options"]],
                         ["picture", "slideshow", "video", "wallpaper_engine", "color", "gradient"])
        for key, table in self.bg.SELECT_TABLES.items():
            self.assertEqual({o["value"] for o in f[key]["options"]}, set(table), key)
        self.assertEqual(f["we_item"].get("choices"), "backend")
        self.assertEqual(f["screen_connector"].get("choices"), "backend")
        self.assertNotIn("dim", keys, "no plugin can dim, so it must not be offered")
        # Defaults: pause when a window is maximized or fullscreen, scene wallpapers off.
        self.assertEqual(f["video_pause"]["default"], "busy")
        self.assertEqual(f["we_pause"]["default"], "busy")
        self.assertIs(f["we_scenes"]["default"], False)

    def test_tweaks_show_only_for_their_type(self):
        bg = {s["id"]: s for s in self.schema["sections"]}["background"]
        fields = bg["fields"]
        vals = dict(self.fa.defaults(self.schema)["background"])

        def shown(**v):
            d = dict(vals, **v)
            return {f["key"] for f in fields if self.fa.visible(f, d, fields)}
        self.assertEqual(shown(type="picture"), {"type", "picture", "fill", "screen"})
        self.assertEqual(shown(type="color"), {"type", "color", "screen"})
        self.assertNotIn("video_blur_radius", shown(type="picture", video_blur="always"))
        self.assertIn("video_blur_radius", shown(type="video", video_blur="always"))
        self.assertNotIn("night_video", shown(type="picture", day_night="time"))
        self.assertEqual(shown(type="video", day_night="time") - shown(type="video"),
                         {"night_video", "day_starts", "night_starts"})
        self.assertIn("screen_connector", shown(screen="one"))
        self.assertEqual(shown(type="wallpaper_engine") - shown(type="color"),
                         {"we_item", "we_scenes", "we_fill", "fps", "speed", "volume", "we_pause", "pause_on_battery"})


class BackendApplyTest(BackendBase):
    def test_picture_sends_the_exact_script(self):
        pic = self.add("a.png", png())
        path = self.fa.media_path(pic["id"])
        rc, out = self.apply(type="picture", picture=pic["id"], fill="fit_blur")
        self.assertEqual(rc, 0, out)
        self.assertIn("Background applied", out)
        script = self.expect_script("org.kde.image", {"Image": "file://" + path, "FillMode": 1, "Blur": True,
                                                      "Color": "#000000"})
        # The template itself, so a change to it is a visible change to this test.
        self.assertIn("d.wallpaperPlugin = p.plugin;", script)
        self.assertIn('d.currentConfigGroup = ["Wallpaper", p.plugin, "General"];', script)
        self.assertEqual(script.count("__FORGE"), 0)
        # A picture is a known good background now; no crash guard for a stock plugin.
        self.assertEqual(self.bg.read_last_good(), {"url": "file://" + path, "path": path})
        self.assertEqual(self.calls("systemd-run"), [])

    def test_nothing_chosen_changes_nothing(self):
        for values in ({"type": "picture", "picture": ""}, {"type": "video", "video": ""},
                       {"type": "slideshow"}, {"type": "wallpaper_engine", "we_item": ""}):
            self.install_plugin(VIDEO_ID)
            self.install_plugin(WE_ID)
            rc, out = self.apply(**values)
            self.assertEqual(rc, 0, out)
            self.assertIn("Nothing changed", out)
        self.assertEqual(self.calls(), [c for c in self.calls() if c["tool"] == "systemctl"])

    def test_slideshow_uses_every_picture_in_the_library(self):
        self.add("a.png", png())
        self.add("v.mp4", SAMPLES["mp4"][0])
        rc, out = self.apply(type="slideshow", slide_interval=30, slide_order="newest", fill="tile")
        self.assertEqual(rc, 0, out)
        self.expect_script("org.kde.slideshow", {"SlidePaths": [self.fa.media_dir()], "SlideInterval": 1800,
                                                 "SlideshowMode": 4, "FillMode": 3, "Blur": False,
                                                 "Color": "#000000"})

    def test_video(self):
        vid = self.add("v.mp4", SAMPLES["mp4"][0])
        path = self.fa.media_path(vid["id"])
        rc, out = self.apply(type="video", video=vid["id"])
        self.assertEqual(rc, 1)
        self.assertIn("video wallpaper plugin is not installed", out)
        self.assertEqual(self.calls("dbus-send"), [])
        self.install_plugin(VIDEO_ID)
        rc, out = self.apply(type="video", video=vid["id"], volume=40, speed=1.5, video_pause="active",
                             video_blur="paused", video_blur_radius=48, pause_on_battery=False)
        self.assertEqual(rc, 0, out)
        urls = [{"filename": "file://" + path, "enabled": True, "duration": 0, "customDuration": 0,
                 "playbackRate": 0.0, "alternativePlaybackRate": 0.0, "loop": False, "dayNightPhase": 4}]
        self.expect_script(VIDEO_ID, {
            "VideoUrls": json.dumps(urls), "FillMode": 2, "PauseMode": 1, "MuteMode": 1, "Volume": 0.4,
            "PlaybackRate": 1.5, "BatteryPausesVideo": False, "PauseBatteryLevel": 100, "BlurMode": 3,
            "BlurRadius": 48, "DayNightCycleMode": 0})

    def test_video_day_and_night(self):
        self.install_plugin(VIDEO_ID)
        day = self.add("d.mp4", SAMPLES["mp4"][0])
        night = self.add("n.webm", SAMPLES["webm"][0])
        rc, out = self.apply(type="video", video=day["id"], night_video=night["id"], day_night="time",
                             day_starts=6, night_starts=20)
        self.assertEqual(rc, 0, out)
        cfg = json.loads(re.search(r"const p = (.*);\n", self.sent_script()).group(1))["config"]
        urls = json.loads(cfg["VideoUrls"])
        self.assertEqual([(u["filename"], u["dayNightPhase"]) for u in urls],
                         [("file://" + self.fa.media_path(day["id"]), 2), ("file://" + self.fa.media_path(night["id"]), 0)])
        self.assertEqual((cfg["DayNightCycleMode"], cfg["DayNightCycleSunriseTime"], cfg["DayNightCycleSunsetTime"]),
                         (2, 360, 1200))
        self.assertEqual((cfg["MuteMode"], cfg["Volume"]), (5, 0.0), "volume 0 means always muted")

    def test_color_and_gradient(self):
        rc, out = self.apply(type="color", color="#12ab34")
        self.assertEqual(rc, 0, out)
        self.expect_script("org.kde.color", {"Color": "#12ab34"})
        self.clear()
        rc, out = self.apply(type="gradient", color="#000000", color2="#ffffff", gradient_direction="down")
        self.assertEqual(rc, 0, out)
        gen = os.path.join(self.fa.data_dir(), "generated")
        files = os.listdir(gen)
        self.assertEqual(len(files), 1)
        path = os.path.join(gen, files[0])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(gen).st_mode), 0o700)
        self.expect_script("org.kde.image", {"Image": "file://" + path, "FillMode": 0, "Blur": False,
                                             "Color": "#000000"})
        w, h, rows = decode_png(path)
        self.assertEqual((w, h), (self.bg.GRADIENT_W, self.bg.GRADIENT_H))
        self.assertEqual(rows[0][:3], b"\x00\x00\x00")
        self.assertEqual(rows[-1][:3], b"\xff\xff\xff")
        self.assertEqual(rows[0][:3], rows[0][-3:], "top to bottom: a row is one color")
        # Other colors make a new file; the old one is removed unless it is the last good background.
        self.clear()
        rc, out = self.apply(type="gradient", color="#ff0000", color2="#0000ff", gradient_direction="diagonal")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(os.listdir(gen)), 2, "the previous gradient is the last good one, so it is kept")
        rc, out = self.apply(type="gradient", color="#00ff00", color2="#0000ff", gradient_direction="right")
        self.assertEqual(len(os.listdir(gen)), 2)

    def test_one_screen_by_connector(self):
        rc, out = self.apply(type="color", screen="one", screen_connector="DP-2")
        self.assertEqual(rc, 0, out)
        self.expect_script("org.kde.color", {"Color": "#1d2733"}, connector="DP-2")
        for bad in ("", "DP 1", "DP-1;x", "../x", "x" * 33):
            self.clear()
            rc, out = self.run_cmd("apply", dict(self.payload(type="color", screen="one"),
                                                 values=dict(self.fa.load(self.schema)["background"], type="color",
                                                             screen="one", screen_connector=bad)))
            self.assertEqual(rc, 1, bad)
            self.assertEqual(self.calls("dbus-send"), [], bad)
        self.conf({"dbus-send": [{"outputs": ['method return\n   string "forge-error: no-screen"\n']}]})
        self.clear()
        rc, out = self.apply(type="color", screen="one", screen_connector="HDMI-A-9")
        self.assertEqual(rc, 1)
        self.assertIn("HDMI-A-9", out)

    def test_plasma_errors_are_explained(self):
        for err, want in (("Error org.freedesktop.DBus.Error.Failed: Widgets are locked\n", "locked"),
                          ("Error org.freedesktop.DBus.Error.ServiceUnknown: The name org.kde.plasmashell "
                           "was not provided by any .service files\n", "not running"),
                          ('Failed to open connection to "session" message bus: Unable to autolaunch a dbus-daemon\n',
                           "not running"),
                          ("Error org.freedesktop.DBus.Error.Failed: TypeError: boom\n", "did not accept")):
            self.conf({"dbus-send": [{"outputs": [""], "stderr": err, "rc": 1}]})
            rc, out = self.apply(type="color")
            self.assertEqual(rc, 1)
            self.assertIn(want, out)

    def test_qdbus_when_there_is_no_dbus_send(self):
        os.unlink(os.path.join(self.fakebin, "dbus-send"))
        self.conf({"qdbus6": [{"outputs": ["forge-ok 1\n"]}]})
        rc, out = self.apply(type="color", color="#000001")
        self.assertEqual(rc, 0, out)
        [argv] = self.calls("qdbus6")
        params = {"plugin": "org.kde.color", "config": {"Color": "#000001"}, "connector": None}
        self.assertEqual(argv, ["org.kde.plasmashell", "/PlasmaShell", "org.kde.PlasmaShell.evaluateScript",
                                self.bg.SCRIPT_TEMPLATE.replace("__FORGE_PARAMS__", json.dumps(
                                    params, ensure_ascii=True, sort_keys=True))])
        os.unlink(os.path.join(self.fakebin, "qdbus6"))
        rc, out = self.apply(type="color")
        self.assertEqual(rc, 1)
        self.assertIn("dbus-send", out)

    def test_media_must_still_be_the_librarys_own_file(self):
        pic = self.add("a.png", png())
        p = self.payload(type="picture", picture=pic["id"])
        # A changed payload path is refused: only the library's own file for that id is used.
        bad = json.loads(json.dumps(p))
        bad["media"][pic["id"]]["path"] = "/etc/passwd"
        rc, out = self.run_cmd("apply", bad)
        self.assertEqual(rc, 1)
        # The file swapped for a link after it was chosen: refused too.
        real = self.fa.media_path(pic["id"])
        os.unlink(real)
        os.symlink("/etc/passwd", real)
        rc, out = self.run_cmd("apply", p)
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls("dbus-send"), [])
        # A video id in the picture field (values not checked by forgeappearance) is refused.
        vid = self.add("v.mp4", SAMPLES["mp4"][0])
        p2 = self.payload(type="video", video=vid["id"])
        p2["values"] = dict(p2["values"], type="picture", picture=vid["id"])
        rc, out = self.run_cmd("apply", p2)
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls("dbus-send"), [])

    def test_bad_input_is_refused(self):
        for raw in ("", "nope", "[]", '{"values": 5}', json.dumps({"values": {"type": "evil"}, "media": {}})):
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(raw)), redirect_stdout(out):
                rc = self.bg.main(["apply"])
            self.assertEqual(rc, 1, raw)
        self.assertEqual(self.bg.main(["nope"]), 2)
        self.assertEqual(self.calls("dbus-send"), [])


def decode_png(path):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    w = h = None
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        assert struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0] == zlib.crc32(kind + body)
        if kind == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
            assert body[8:13] == bytes([8, 2, 0, 0, 0])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = 1 + 3 * w
    rows = [raw[i * stride + 1:(i + 1) * stride] for i in range(h)]
    assert all(raw[i * stride] == 0 for i in range(h))
    return w, h, rows


@unittest.skipUnless(NODE, "node not installed")
class GeneratedScriptRunsTest(BackendBase):
    """The script is plain JavaScript: run it in node with stand-ins for Plasma's scripting API."""
    HARNESS = """
const forgeLog = [];
function print(s) { forgeLog.push(String(s)); }
function screenForConnector(c) { return c === "DP-2" ? 1 : -1; }
function desk(screen) {
  return {screen, wallpaperPlugin: "org.kde.image", currentConfigGroup: [], cfg: {},
          writeConfig(k, v) { this.cfg[this.currentConfigGroup.join("/") + "/" + k] = v; }};
}
const forgeDesks = [desk(0), desk(1), desk(0)];
function desktops() { return forgeDesks; }
{
%s
}
console.log(JSON.stringify({log: forgeLog, desks: forgeDesks}));
"""

    def run_js(self, script):
        p = os.path.join(self.tmp, "s.js")
        with open(p, "w") as f:
            f.write(self.HARNESS % script)
        r = subprocess.run([NODE, p], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_all_screens_one_screen_and_a_missing_screen(self):
        self.apply(type="color", color="#0a0b0c")
        got = self.run_js(self.sent_script())
        self.assertEqual(got["log"], ["forge-ok 3"])
        for d in got["desks"]:
            self.assertEqual(d["wallpaperPlugin"], "org.kde.color")
            self.assertEqual(d["cfg"], {"Wallpaper/org.kde.color/General/Color": "#0a0b0c"})
        self.clear()
        self.apply(type="color", color="#0a0b0c", screen="one", screen_connector="DP-2")
        got = self.run_js(self.sent_script())
        self.assertEqual(got["log"], ["forge-ok 1"])
        self.assertEqual([d["wallpaperPlugin"] for d in got["desks"]], ["org.kde.image", "org.kde.color", "org.kde.image"])
        self.clear()
        self.apply(type="color", screen="one", screen_connector="DP-7")
        got = self.run_js(self.sent_script())
        self.assertEqual(got["log"], ["forge-error: no-screen"])
        self.assertEqual({d["wallpaperPlugin"] for d in got["desks"]}, {"org.kde.image"})

    def test_hostile_text_stays_data(self):
        # Values never become code: even a crafted connector (refused anyway) or file name is a JSON string.
        script = self.bg.build_script("org.kde.image", {"Image": 'file:///x"; print("pwned"); "'}, None)
        got = self.run_js(script)
        self.assertEqual(got["log"], ["forge-ok 3"])
        self.assertEqual(got["desks"][0]["cfg"]["Wallpaper/org.kde.image/General/Image"], 'file:///x"; print("pwned"); "')
        r = subprocess.run([NODE, "--check", "-"], input=script, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


# 3. Wallpaper Engine: the workshop items in the Steam library (project.json is untrusted)

class WorkshopTest(BackendBase):
    def content(self, root=None):
        root = root or os.path.join(self.home, ".local/share/Steam")
        d = os.path.join(root, "steamapps/workshop/content/431960")
        os.makedirs(d, exist_ok=True)
        return d

    def item(self, d, wid, project, files=("main.mp4",)):
        p = os.path.join(d, wid)
        os.makedirs(p, exist_ok=True)
        if project is not None:
            with open(os.path.join(p, "project.json"), "wb" if isinstance(project, bytes) else "w") as f:
                f.write(project if isinstance(project, (str, bytes)) else json.dumps(project))
        for name in files:
            os.makedirs(os.path.dirname(os.path.join(p, name)), exist_ok=True)
            with open(os.path.join(p, name), "wb") as f:
                f.write(b"x")
        return p

    def hostile_library(self):
        d = self.content()
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "project.json"), "w") as f:
            json.dump({"title": "Outside", "type": "video", "file": "main.mp4"}, f)
        with open(os.path.join(outside, "main.mp4"), "wb") as f:
            f.write(b"x")
        self.item(d, "100", {"title": "Rain", "type": "Video", "file": "main.mp4"})
        self.item(d, "101", {"title": "Clock", "type": "web", "file": "web/index.html"}, files=("web/index.html",))
        self.item(d, "102", {"title": "Space", "type": "scene", "file": "scene.pkg"}, files=("scene.pkg",))
        self.item(d, "103", '{"title": "' + "x" * 300000 + '", "type": "video", "file": "main.mp4"}')
        self.item(d, "104", "{not json")
        self.item(d, "105", "[" * 100000 + "]" * 100000)
        self.item(d, "106", {"title": "Up", "type": "video", "file": "../../../../etc/passwd"})
        self.item(d, "107", {"title": "Abs", "type": "video", "file": "/etc/passwd"})
        p = self.item(d, "108", None)
        os.symlink(os.path.join(outside, "project.json"), os.path.join(p, "project.json"))
        os.symlink(outside, os.path.join(d, "109"))
        p = self.item(d, "110", {"title": "Link", "type": "video", "file": "evil.mp4"}, files=())
        os.symlink("/etc/passwd", os.path.join(p, "evil.mp4"))
        self.item(d, "abc", {"title": "Name", "type": "video", "file": "main.mp4"})
        self.item(d, "111", {"title": "App", "type": "application", "file": "main.mp4"})
        self.item(d, "112", {"title": "Bad\x07‮" + "y" * 500, "type": "video", "file": "main.mp4"})
        p = self.item(d, "113", None)
        os.mkfifo(os.path.join(p, "project.json"))
        self.item(d, "114", [1, 2, 3])
        self.item(d, "115", {"title": "Dir", "type": "video", "file": "sub"}, files=("sub/inner",))
        self.item(d, "116", {"title": 5, "type": "video", "file": "main.mp4"})
        p = self.item(d, "117", {"title": "Linked folder", "type": "web", "file": "w/index.html"}, files=())
        os.symlink(outside, os.path.join(p, "w"))
        self.item(d, "118", {"title": "Odd#name", "type": "video", "file": "a#b.mp4"}, files=("a#b.mp4",))
        return d

    def test_listing_skips_everything_hostile(self):
        d = self.hostile_library()
        t0 = time.time()
        items = self.bg.workshop_items()
        self.assertLess(time.time() - t0, 10, "a FIFO must not block the listing")
        self.assertEqual([i["id"] for i in items], ["100", "101", "102", "112", "116"])
        by = {i["id"]: i for i in items}
        self.assertEqual(by["100"], {"id": "100", "title": "Rain", "type": "video", "file": "main.mp4",
                                     "dir": os.path.join(d, "100"),
                                     "library": os.path.join(self.home, ".local/share/Steam")})
        self.assertEqual(by["101"]["file"], "web/index.html")
        self.assertEqual(by["116"]["title"], "Wallpaper 116")
        t = by["112"]["title"]
        self.assertTrue(t.startswith("Bady"))
        self.assertLessEqual(len(t), 80)

    def test_listing_caps(self):
        d = self.content()
        for i in range(30):
            self.item(d, str(1000 + i), {"title": f"W{i}", "type": "video", "file": "main.mp4"})
        with mock.patch.object(self.bg, "WORKSHOP_MAX", 10):
            self.assertEqual(len(self.bg.workshop_items()), 10)

    def test_more_steam_libraries_from_libraryfolders_vdf(self):
        steam = os.path.join(self.home, ".local/share/Steam")
        os.makedirs(os.path.join(steam, "steamapps"))
        lib2 = os.path.join(self.tmp, "games2")
        d2 = self.content(lib2)
        self.item(d2, "200", {"title": "Second drive", "type": "video", "file": "main.mp4"})
        with open(os.path.join(steam, "steamapps/libraryfolders.vdf"), "w") as f:
            f.write('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n'
                    '\t"2"\n\t{\n\t\t"path"\t\t"relative/nope"\n\t}\n}\n' % (steam, lib2))
        items = self.bg.workshop_items()
        self.assertEqual([(i["id"], i["library"]) for i in items], [("200", lib2)])
        with open(os.path.join(steam, "steamapps/libraryfolders.vdf"), "wb") as f:
            f.write(b"\x00\xff garbage" * 1000)
        self.assertEqual(self.bg.workshop_items(), [])

    def test_apply_wallpaper_engine(self):
        d = self.hostile_library()
        rc, out = self.apply(type="wallpaper_engine", we_item="100")
        self.assertEqual(rc, 1)
        self.assertIn("Wallpaper Engine support is not included in this build", out)
        self.install_plugin(WE_ID)
        steam = os.path.join(self.home, ".local/share/Steam")
        rc, out = self.apply(type="wallpaper_engine", we_item="101", fps=24, volume=0, speed=1.25,
                             we_pause="fullscreen", we_fill="fit", pause_on_battery=True)
        self.assertEqual(rc, 0, out)
        self.expect_script(WE_ID, {
            "SteamLibraryPath": "file://" + steam, "WallpaperWorkShopId": "101",
            "WallpaperSource": "file://" + os.path.join(d, "101") + "/web/index.html+web",
            "Fps": 24, "MuteAudio": True, "Volume": 0, "Speed": 1.25, "PauseMode": 5, "DisplayMode": 0,
            "PauseOnBatPower": True})
        # Scene wallpapers only with the experimental switch on.
        self.clear()
        rc, out = self.apply(type="wallpaper_engine", we_item="102")
        self.assertEqual(rc, 1)
        self.assertIn("scene", out.lower())
        self.assertEqual(self.calls("dbus-send"), [])
        rc, out = self.apply(type="wallpaper_engine", we_item="102", we_scenes=True)
        self.assertEqual(rc, 0, out)
        # Items that the listing refused can't be applied either.
        for wid in ("108", "109", "110", "999", "118"):
            self.clear()
            rc, out = self.apply(type="wallpaper_engine", we_item=wid)
            self.assertEqual(rc, 1, wid)
            self.assertEqual(self.calls("dbus-send"), [], wid)

    def test_options_lists_wallpapers_and_screens(self):
        self.hostile_library()
        for name, status in (("card1-DP-1", "connected"), ("card1-HDMI-A-1", "disconnected"),
                             ("card1-DP-2", "connected\n"), ("card0-eDP-1", "connected"), ("version", None),
                             ("card1-Bad Name", "connected")):
            os.makedirs(os.path.join(self.drm, name))
            if status is not None:
                with open(os.path.join(self.drm, name, "status"), "w") as f:
                    f.write(status)
        rc, out = self.run_cmd("options", self.payload())
        self.assertEqual(rc, 0)
        got = json.loads(out)["fields"]
        self.assertEqual(got["we_item"]["note"], "Wallpaper Engine support is not included in this build.")
        self.assertEqual(got["screen_connector"]["choices"], [{"value": "DP-1", "label": "DP-1"},
                                                              {"value": "DP-2", "label": "DP-2"},
                                                              {"value": "eDP-1", "label": "eDP-1"}])
        self.install_plugin(WE_ID)
        rc, out = self.run_cmd("options", self.payload())
        got = json.loads(out)["fields"]
        self.assertEqual([c["value"] for c in got["we_item"]["choices"]], ["100", "101", "102", "112", "116"])
        self.assertEqual(got["we_item"]["choices"][0]["label"], "Rain (video)")
        self.assertEqual(got["we_item"]["choices"][2]["label"], "Space (scene, experimental)")
        self.assertIn("5 wallpapers", got["we_item"]["note"])
        # Every choice passes the framework's own checks, so none is dropped on the way to the page.
        fields = {f["key"]: f for f in {s["id"]: s for s in self.schema["sections"]}["background"]["fields"]}
        for key in ("we_item", "screen_connector"):
            for c in got[key]["choices"]:
                self.assertEqual(self.fa.coerce(fields[key], c["value"]), c["value"])
                self.assertEqual(self.fa._clean_text(c["label"], self.fa.CHOICE_LABEL_MAX), c["label"])
        shutil.rmtree(os.path.join(self.home, ".local/share/Steam"))
        rc, out = self.run_cmd("options", self.payload())
        self.assertIn("No Wallpaper Engine wallpapers", json.loads(out)["fields"]["we_item"]["note"])


# 4. Crash guard

APPLETSRC = """[ActionPlugins][0]
RightButton;NoModifier=org.kde.contextmenu

[Containments][1]
activityId=abc
formfactor=0
plugin=org.kde.plasma.folder
wallpaperplugin=luisbocanegra.smart.video.wallpaper.reborn

[Containments][1][Wallpaper][luisbocanegra.smart.video.wallpaper.reborn][General]
VideoUrls=[{"filename":"file:///x.mp4"}]

[Containments][2]
wallpaperplugin=org.kde.image

[Containments][7]
plugin=org.kde.plasma.folder
wallpaperplugin=com.github.captsilver.wallpaperEngineKde

[Containments][9][Applets][3]
wallpaperplugin=luisbocanegra.smart.video.wallpaper.reborn
"""


class CrashGuardTest(BackendBase):
    def setUp(self):
        super().setUp()
        self.appletsrc = os.path.join(self.tmp, "config", "plasma-org.kde.plasma.desktop-appletsrc")
        os.makedirs(os.path.dirname(self.appletsrc), exist_ok=True)
        with open(self.appletsrc, "w") as f:
            f.write(APPLETSRC)
        self.install_plugin(VIDEO_ID)

    def crash_conf(self, restarts, active_after_stop="inactive\n", state="active\n"):
        self.conf({"dbus-send": [{"outputs": [DBUS_OK]}],
                   "systemctl": [{"match": "NRestarts", "outputs": nrestarts(*[r.strip() for r in restarts])},
                                 {"match": "ActiveState", "outputs": [state]},
                                 {"match": "is-active", "outputs": [active_after_stop], "rc": 3}]})

    def kw(self, *groups_key_value):
        return groups_key_value

    def test_video_apply_starts_one_guard(self):
        vid = self.add("v.mp4", SAMPLES["mp4"][0])
        rc, out = self.apply(type="video", video=vid["id"])
        self.assertEqual(rc, 0, out)
        sysctl = self.calls("systemctl")
        self.assertIn(["--user", "show", "-p", "LoadState", "-p", "ActiveState", "-p", "NRestarts",
                       "plasma-plasmashell.service"], sysctl)
        self.assertIn(["--user", "stop", "forge-background-guard.service"], sysctl)
        [run] = self.calls("systemd-run")
        self.assertEqual(run[:8], ["--user", "--quiet", "--collect", "--unit=forge-background-guard",
                                   "--property=RuntimeMaxSec=180", "--property=TimeoutStopSec=45",
                                   "--setenv=FORGE_LIB=" + LIB,
                                   "--setenv=XDG_CONFIG_HOME=" + os.path.join(self.tmp, "config")])
        self.assertEqual(run[-3:], ["--", os.path.realpath(BACKEND), "guard"])
        self.assertEqual(self.bg.read_pending()["restarts"], 3)
        self.assertEqual(self.bg.read_pending()["plugin"], VIDEO_ID)

    def test_guard_starts_before_the_change_so_a_crash_during_apply_is_caught(self):
        self.conf({"dbus-send": [{"outputs": [""], "rc": 1,
                                  "stderr": "Error org.freedesktop.DBus.Error.NoReply: Message recipient disconnected\n"}],
                   "systemctl": [{"match": "NRestarts", "outputs": nrestarts(3)}]})
        vid = self.add("v.mp4", SAMPLES["mp4"][0])
        rc, out = self.apply(type="video", video=vid["id"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.calls("systemd-run")), 1)
        tools = [c["tool"] for c in self.calls()]
        self.assertLess(tools.index("systemd-run"), tools.index("dbus-send"))
        self.assertEqual(self.bg.read_pending()["restarts"], 3)

    def test_no_systemd_service_means_no_guard_and_says_so(self):
        self.conf({"dbus-send": [{"outputs": [DBUS_OK]}], "systemctl": [{"match": "NRestarts", "outputs": ["\n"]}]})
        vid = self.add("v.mp4", SAMPLES["mp4"][0])
        rc, out = self.apply(type="video", video=vid["id"])
        self.assertEqual(rc, 0, out)
        self.assertIn("crash guard is off", out)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_picture_apply_stops_a_running_guard(self):
        self.pend(VIDEO_ID, 1)
        pic = self.add("a.png", png())
        rc, out = self.apply(type="picture", picture=pic["id"])
        self.assertEqual(rc, 0, out)
        self.assertIn(["--user", "stop", "forge-background-guard.service"], self.calls("systemctl"))
        self.assertIsNone(self.bg.read_pending())

    def test_stable_desktop_is_left_alone(self):
        self.pend(VIDEO_ID, 3)
        self.crash_conf(["3\n"])
        rc, out = self.run_cmd("guard")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls("kwriteconfig6"), [])
        self.assertNotIn(["--user", "stop", "plasma-plasmashell.service"], self.calls("systemctl"))
        self.assertIsNone(self.bg.read_pending())
        with open(self.appletsrc) as f:
            self.assertEqual(f.read(), APPLETSRC)

    def recovered_calls(self, image):
        f = ["--file", self.appletsrc]
        want = []
        for cid in ("1", "7"):
            want.append(f + ["--group", "Containments", "--group", cid, "--key", "wallpaperplugin", "org.kde.image"])
            img = f + ["--group", "Containments", "--group", cid, "--group", "Wallpaper", "--group", "org.kde.image",
                       "--group", "General", "--key", "Image"]
            want.append(img + ([image] if image else ["--delete"]))
        return want

    def test_crash_loop_goes_back_to_the_last_good_picture_once(self):
        pic = self.add("a.png", png())
        self.apply(type="picture", picture=pic["id"])
        url = "file://" + self.fa.media_path(pic["id"])
        self.pend(VIDEO_ID, 3)
        self.crash_conf(["3\n", "4\n", "5\n"])
        self.clear()
        rc, out = self.run_cmd("guard")
        self.assertEqual(rc, 0, out)
        sysctl = self.calls("systemctl")
        stop = sysctl.index(["--user", "stop", "plasma-plasmashell.service"])
        self.assertEqual(sysctl[-2:], [["--user", "reset-failed", "plasma-plasmashell.service"],
                                       ["--user", "start", "plasma-plasmashell.service"]])
        self.assertEqual(self.calls("kwriteconfig6"), self.recovered_calls(url))
        # Order: stopped and confirmed stopped before any write, started after.
        tools = [c["tool"] for c in self.calls()]
        first_kw = tools.index("kwriteconfig6")
        self.assertLess(stop, first_kw)
        self.assertIn(["--user", "is-active", "plasma-plasmashell.service"], sysctl[:sysctl.index(
            ["--user", "reset-failed", "plasma-plasmashell.service"])])
        # Never a loop: the pending record is gone, a second guard does nothing.
        self.clear()
        rc, out = self.run_cmd("guard")
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls("kwriteconfig6"), [])
        self.assertEqual([c for c in self.calls("systemctl") if "stop" in c or "start" in c], [])
        # The next apply says what happened, once.
        self.clear()
        self.crash_conf(["0\n"])
        rc, out = self.apply(type="color")
        self.assertIn("crashed", out)
        rc, out = self.apply(type="color")
        self.assertNotIn("crashed", out)

    def test_failed_shell_counts_as_a_crash_and_no_last_good_means_plasma_default(self):
        self.pend(VIDEO_ID, 3)
        self.crash_conf(["3\n"], state="failed\n")
        rc, out = self.run_cmd("guard")
        self.assertEqual(self.calls("kwriteconfig6"), self.recovered_calls(None))

    def test_last_good_file_gone_means_plasma_default(self):
        pic = self.add("a.png", png())
        self.apply(type="picture", picture=pic["id"])
        self.fa.media_remove(pic["id"])
        self.pend(WE_ID, 0)
        self.crash_conf(["2\n"])
        self.clear()
        self.run_cmd("guard")
        self.assertEqual(self.calls("kwriteconfig6"), self.recovered_calls(None))

    def test_never_writes_while_plasmashell_still_runs(self):
        self.pend(VIDEO_ID, 3)
        self.crash_conf(["3\n", "6\n"], active_after_stop="active\n")
        rc, out = self.run_cmd("guard")
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls("kwriteconfig6"), [])
        self.assertIn("not confirmed stopped", out)
        self.assertIn(["--user", "start", "plasma-plasmashell.service"], self.calls("systemctl"))

    def test_broken_pending_record_is_ignored(self):
        for raw in (b"", b"{", b"[]", b'{"plugin": "org.kde.image", "restarts": 1, "at": 1}',
                    b'{"plugin": "luisbocanegra.smart.video.wallpaper.reborn", "restarts": "x", "at": 1}'):
            d = self.bg.state_dir()
            with open(os.path.join(d, "pending.json"), "wb") as f:
                f.write(raw)
            self.crash_conf(["9\n"])
            rc, out = self.run_cmd("guard")
            self.assertEqual(rc, 0, raw)
        self.assertEqual(self.calls("kwriteconfig6"), [])

    def test_guard_is_detached_when_systemd_run_fails(self):
        self.conf({"dbus-send": [{"outputs": [DBUS_OK]}],
                   "systemd-run": [{"rc": 1, "stderr": "Failed to start transient service unit\n"}],
                   "systemctl": [{"match": "NRestarts", "outputs": nrestarts(3)},
                                 {"match": "ActiveState", "outputs": ["active\n"]}]})
        vid = self.add("v.mp4", SAMPLES["mp4"][0])
        real, seen = subprocess.Popen, []

        def popen(argv, *a, **kw):
            if argv == [os.path.realpath(BACKEND), "guard"]:
                seen.append((argv, kw))
                return mock.Mock()
            return real(argv, *a, **kw)
        with mock.patch.object(subprocess, "Popen", popen):
            t0 = time.time()
            rc, out = self.apply(type="video", video=vid["id"])
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(rc, 0, out)
        self.assertIn("runs in the background", out)
        [(argv, kw)] = seen
        args = (argv,)
        self.assertEqual(args[0], [os.path.realpath(BACKEND), "guard"])
        for k in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw[k], subprocess.DEVNULL, k)
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(self.bg.read_pending()["restarts"], 3, "the detached guard reads it")

    @unittest.skipUnless(os.geteuid() == 0, "needs root to run the backend as a normal test user")
    def test_apply_returns_quickly_and_leaves_nothing_holding_its_output(self):
        # The real program in its own process, as a normal user: a helper that stays behind (a fake
        # systemd-run that leaves a sleeper, or the detached guard) must not keep stdout open.
        vid = self.add("v.mp4", SAMPLES["mp4"][0])
        payload = json.dumps(self.payload(type="video", video=vid["id"]))
        for name in os.listdir(self.fakebin):
            os.chmod(os.path.join(self.fakebin, name), 0o755)
        for rule in ([{"outputs": [""]}], [{"outputs": [""], "rc": 1}]):
            sleeper = os.path.join(self.fakebin, "systemd-run")
            with open(sleeper, "w") as f:
                f.write("#!/bin/sh\n/bin/sleep 30 &\nexit %d\n" % rule[0].get("rc", 0))
            os.chmod(sleeper, 0o755)
            for d, dirs, files in os.walk(self.tmp):
                for n in dirs + files:
                    if not os.path.islink(os.path.join(d, n)):
                        os.chmod(os.path.join(d, n), 0o777)
            os.chmod(self.tmp, 0o777)
            env = dict(os.environ, FORGE_BG_WATCH_S="2")
            t0 = time.time()
            p = subprocess.Popen([sys.executable, BACKEND, "apply"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=env, preexec_fn=tap.as_uid(tap.OWNER))
            out, _ = p.communicate(payload.encode(), timeout=20)
            self.assertLess(time.time() - t0, 10, out)
            self.assertEqual(p.returncode, 0, out)
            self.assertIn(b"Background applied", out)

    def test_risky_containments_parser(self):
        self.assertEqual(self.bg.risky_containments(self.appletsrc), ["1", "7"])
        with open(self.appletsrc, "wb") as f:
            f.write(b"\xff\xfe\x00 garbage [Containments][x]\nwallpaperplugin=" + VIDEO_ID.encode())
        self.assertEqual(self.bg.risky_containments(self.appletsrc), [])
        os.unlink(self.appletsrc)
        os.symlink("/etc/passwd", self.appletsrc)
        self.assertEqual(self.bg.risky_containments(self.appletsrc), [])


# 5. Image build: the video plugin pinned by sha256, Wallpaper Engine optional and pinned

class VideoPluginDownloadTest(unittest.TestCase):
    def setUp(self):
        with open(VIDEO_SCRIPT) as f:
            self.text = f.read()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_verify(self, script, target, **env):
        return subprocess.run(["bash", script], capture_output=True, text=True,
                              env=dict(os.environ, VERIFY_ONLY=target, **env), timeout=30)

    def test_version_and_checksum_are_pinned(self):
        self.assertRegex(self.text, r'(?m)^SVWR_VERSION="\d+\.\d+\.\d+"$')
        self.assertRegex(self.text, r'(?m)^SVWR_SHA256="[0-9a-f]{64}"$')
        self.assertIn("sha256sum -c", self.text)
        self.assertIn("https://github.com/luisbocanegra/plasma-smart-video-wallpaper-reborn/releases/download/"
                      "v${SVWR_VERSION}/", self.text)
        self.assertEqual(self.text.count("curl "), 1)
        self.assertIn('SVWR_ID="luisbocanegra.smart.video.wallpaper.reborn"', self.text)
        self.assertIn("/usr/share/plasma/wallpapers/", self.text)

    def test_wrong_or_missing_file_fails_and_the_pin_cannot_be_swapped(self):
        p = os.path.join(self.tmp, "x.tar.gz")
        with open(p, "wb") as f:
            f.write(b"not the plugin")
        real = hashlib.sha256(b"not the plugin").hexdigest()
        self.assertNotEqual(self.run_verify(VIDEO_SCRIPT, p).returncode, 0)
        self.assertNotEqual(self.run_verify(VIDEO_SCRIPT, p, SVWR_SHA256=real).returncode, 0)
        self.assertNotEqual(self.run_verify(VIDEO_SCRIPT, os.path.join(self.tmp, "none")).returncode, 0)
        copy = os.path.join(self.tmp, "s.sh")
        with open(copy, "w") as f:
            f.write(re.sub(r'(?m)^SVWR_SHA256="[0-9a-f]{64}"$', f'SVWR_SHA256="{real}"', self.text))
        r = self.run_verify(copy, p)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class WallpaperEngineOptionalTest(unittest.TestCase):
    def setUp(self):
        with open(WE_SCRIPT) as f:
            self.text = f.read()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_copy(self, version, fpr):
        text = re.sub(r'(?m)^WE_VERSION=".*"$', f'WE_VERSION="{version}"', self.text)
        text = re.sub(r'(?m)^WE_KEY_FPR=".*"$', f'WE_KEY_FPR="{fpr}"', text)
        p = os.path.join(self.tmp, "we.sh")
        with open(p, "w") as f:
            f.write(text)
        fakebin = os.path.join(self.tmp, "bin")
        os.makedirs(fakebin, exist_ok=True)
        for name in ("curl", "dnf", "gpg"):
            with open(os.path.join(fakebin, name), "w") as f:
                f.write(f'#!/bin/sh\necho "{name} $*" >> "{self.tmp}/ran"\nexit 1\n')
            os.chmod(os.path.join(fakebin, name), 0o755)
        return subprocess.run(["bash", p], capture_output=True, text=True, timeout=30,
                              env=dict(os.environ, PATH=fakebin + ":/usr/bin:/bin"))

    def test_shipped_pins_are_empty_so_the_build_skips_it_with_a_clear_line(self):
        self.assertRegex(self.text, r'(?m)^WE_VERSION=""$')
        self.assertRegex(self.text, r'(?m)^WE_KEY_FPR=""$')
        r = self.run_copy("", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Wallpaper Engine support is not included in this build", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ran")), "nothing may be downloaded")

    def test_half_a_pin_stops_the_build(self):
        for v, k in (("1.5-1.fc45", ""), ("", "A" * 40), ("1.5-1.fc45", "nothex"), ("1.5;rm -rf /", "A" * 40)):
            r = self.run_copy(v, k)
            self.assertNotEqual(r.returncode, 0, (v, k))

    def test_install_is_exact_and_signature_checked(self):
        self.assertIn("--repofrompath", self.text)
        self.assertIn("gpgcheck=1", self.text)
        self.assertIn('"${WE_PACKAGE}-${WE_VERSION}"', self.text)
        self.assertNotIn("copr enable", self.text)
        self.assertNotIn("/etc/yum.repos.d", self.text, "no repo file may stay in the image")
        # A full pin with a key that does not match: the build stops (curl fails here: no network).
        r = self.run_copy("1.5-1.fc45", "A" * 40)
        self.assertNotEqual(r.returncode, 0)

    def test_recipe_and_finalize(self):
        with open(RECIPE) as f:
            recipe = f.read()
        self.assertIn("qt6-qtmultimedia", recipe)
        dnf = recipe.find("type: dnf")
        for s in ("install-video-wallpaper.sh", "install-wallpaper-engine.sh"):
            self.assertGreater(recipe.find(s), dnf, s)
            self.assertTrue(os.access(os.path.join(ROOT, "files/scripts", s), os.X_OK), s)
        with open(FINALIZE) as f:
            self.assertIn("/usr/libexec/forge-appearance-background", f.read())


# 6. Preview hook and the page

class PreviewHookTest(unittest.TestCase):
    def test_follows_the_page_rules(self):
        with open(PREVIEW) as f:
            text = f.read()
        self.assertIn('forgeAppearance.registerPreview("background"', text)
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", 'setAttribute("style"', "eval(", "new Function",
                    "http://", "https://", "document.write", "fetch("):
            self.assertNotIn(bad, text, bad)
        self.assertLess(len(text.encode()), 256 * 1024)

    @unittest.skipUnless(NODE, "node not installed")
    def test_parses(self):
        r = subprocess.run([NODE, "--check", PREVIEW], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


import test_appearance_page as tap  # noqa: E402
from test_settings_page import request, refused  # noqa: E402


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the server and clients as other uids")
class ChoicesEndpointTest(unittest.TestCase):
    # The page test's helpers, without running its tests a second time here.
    start = tap.AppearancePageTest.start
    tearDown = tap.AppearancePageTest.tearDown

    def setUp(self):
        tap.AppearancePageTest.setUp(self)
        schema = os.path.join(self.tmp, "schema.json")
        with open(schema, "w") as f:
            json.dump(CHOICE_SCHEMA, f)
        os.chmod(schema, 0o644)
        self.env["FORGE_APPEARANCE_SCHEMA"] = schema

    def test_choices_need_the_token_and_the_owner(self):
        p = os.path.join(self.libexec, "forge-appearance-background")
        with open(p, "w") as f:
            f.write("#!/bin/sh\ncat > /dev/null\necho \"$1\" > " + os.path.join(self.tmp, "argv") + "\n"
                    "echo '{\"fields\": {\"item\": {\"choices\": [{\"value\": \"42\", \"label\": \"Answer\"}], "
                    "\"note\": \"One found.\"}}}'\n")
        os.chmod(p, 0o755)
        os.chmod(self.tmp, 0o777)
        self.start()
        url = self.base + "/api/appearance/choices?section=background"
        self.assertTrue(refused(tap.OTHER, "GET", url, self.h))
        self.assertEqual(request(tap.OWNER, "GET", url, {})[0], 404)
        status, out = request(tap.OWNER, "GET", url, self.h)
        self.assertEqual(status, 200)
        got = json.loads(out)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["fields"], {"item": {"choices": [{"value": "42", "label": "Answer"}],
                                                  "note": "One found."}})
        with open(os.path.join(self.tmp, "argv")) as f:
            self.assertEqual(f.read().strip(), "options")
        for bad in ("../background", "nope", ""):
            status, out = request(tap.OWNER, "GET", self.base + "/api/appearance/choices?section=" + bad, self.h)
            self.assertFalse(json.loads(out)["ok"], bad)
        status, out = request(tap.OWNER, "GET", self.base + "/api/appearance/choices?section=dock", self.h)
        self.assertEqual(json.loads(out), {"ok": True, "fields": {}, "output": ""})
        self.assertIn("A.choices", self.page)


if __name__ == "__main__":
    unittest.main()


# 7. Review fixes (reviewer rvBG). Each was a passing proof of the problem; these assert the right behaviour.

def nrestarts(*values):
    """systemctl show -p LoadState -p NRestarts output for a loaded plasmashell unit."""
    return ["LoadState=loaded\nActiveState=active\nNRestarts=%s\n" % v for v in values]


class GradientPerScreenTest(BackendBase):
    def test_a_gradient_another_screen_still_shows_is_kept(self):
        rc, out = self.apply(type="gradient", color="#112233", color2="#445566", screen="one", screen_connector="DP-2")
        self.assertEqual(rc, 0, out)
        a_path = json.loads(re.search(r"const p = (.*);\n", self.sent_script()).group(1))["config"]["Image"][7:]
        pic = self.add("p.png", png())
        self.clear()
        self.assertEqual(self.apply(type="picture", picture=pic["id"], screen="one", screen_connector="DP-1")[0], 0)
        self.clear()
        self.assertEqual(self.apply(type="gradient", color="#aa0000", color2="#00aa00", screen="one",
                                    screen_connector="DP-3")[0], 0)
        self.assertTrue(os.path.exists(a_path), "DP-2 still shows it")
        # Once every screen gets something else, old gradients go.
        self.clear()
        self.assertEqual(self.apply(type="gradient", color="#000000", color2="#ffffff", screen="all")[0], 0)
        self.clear()
        self.assertEqual(self.apply(type="gradient", color="#010101", color2="#fefefe", screen="all")[0], 0)
        gen = os.path.join(self.fa.data_dir(), "generated")
        self.assertFalse(os.path.exists(a_path))
        self.assertLessEqual(len(os.listdir(gen)), 2)


class PackedSceneTest(BackendBase):
    content = WorkshopTest.content
    item = WorkshopTest.item

    def test_a_scene_packed_in_scene_pkg_is_listed_and_applies(self):
        # The plugin's scene renderer opens <file with .pkg extension> (SceneWallpaper.cpp), so a published scene
        # names scene.json in project.json and ships scene.pkg.
        self.install_plugin(WE_ID)
        d = self.content()
        self.item(d, "300", {"title": "Packed scene", "type": "scene", "file": "scene.json"},
                  files=("scene.pkg", "preview.jpg"))
        self.item(d, "301", {"title": "Video", "type": "video", "file": "v.mp4"}, files=("v.mp4",))
        self.item(d, "302", {"title": "No package", "type": "scene", "file": "scene.json"}, files=("other.pkg",))
        self.item(d, "303", {"title": "Packed video", "type": "video", "file": "v.mp4"}, files=("v.pkg",))
        p = self.item(d, "304", {"title": "Linked pkg", "type": "scene", "file": "scene.json"}, files=())
        os.symlink("/etc/passwd", os.path.join(p, "scene.pkg"))
        self.assertEqual([i["id"] for i in self.bg.workshop_items()], ["300", "301"])
        self.assertEqual(self.bg.find_item("300")["file"], "scene.json")
        rc, out = self.apply(type="wallpaper_engine", we_item="300", we_scenes=True)
        self.assertEqual(rc, 0, out)
        cfg = json.loads(re.search(r"const p = (.*);\n", self.sent_script()).group(1))["config"]
        self.assertEqual(cfg["WallpaperSource"], "file://" + os.path.join(d, "300") + "/scene.json+scene")


class GuardMisreadTest(BackendBase):
    def setUp(self):
        super().setUp()
        self.appletsrc = os.path.join(self.tmp, "config", "plasma-org.kde.plasma.desktop-appletsrc")
        os.makedirs(os.path.dirname(self.appletsrc), exist_ok=True)
        with open(self.appletsrc, "w") as f:
            f.write(APPLETSRC)

    def test_only_an_explicit_stopped_state_allows_writing(self):
        for out, rc in (("", 1), ("unknown\n", 4), ("deactivating\n", 3)):
            self.clear()
            self.bg.write_state("current.json", {"gen": "g1"})
            self.bg.write_state("pending.json", {"plugin": VIDEO_ID, "restarts": 3, "at": time.time(), "gen": "g1"})
            self.conf({"systemctl": [{"match": "NRestarts", "outputs": nrestarts(3, 6)},
                                     {"match": "ActiveState", "outputs": ["active\n"]},
                                     {"match": "stop plasma", "outputs": [""], "rc": 1, "stderr": "Failed to connect to bus\n"},
                                     {"match": "is-active", "outputs": [out], "rc": rc}]})
            rc2, text = self.run_cmd("guard")
            self.assertEqual(self.calls("kwriteconfig6"), [], out)
            self.assertIn(["--user", "start", "plasma-plasmashell.service"], self.calls("systemctl"))

    def test_a_missing_plasmashell_unit_means_no_guard(self):
        # Real systemd prints NRestarts=0 for a unit that does not exist; LoadState tells.
        self.conf({"dbus-send": [{"outputs": [DBUS_OK]}],
                   "systemctl": [{"match": "NRestarts", "outputs": ["LoadState=not-found\nNRestarts=0\n"]}]})
        self.install_plugin(VIDEO_ID)
        vid = self.add("v.mp4", SAMPLES["mp4"][0])
        rc, out = self.apply(type="video", video=vid["id"])
        self.assertEqual(rc, 0, out)
        self.assertIn("crash guard is off", out)
        self.assertEqual(self.calls("systemd-run"), [])


class GuardGenerationTest(BackendBase):
    def setUp(self):
        super().setUp()
        self.install_plugin(VIDEO_ID)

    def test_an_older_guard_stops_when_a_newer_apply_exists(self):
        self.bg.write_state("current.json", {"gen": "new"})
        self.bg.write_state("pending.json", {"plugin": VIDEO_ID, "restarts": 3, "at": time.time(), "gen": "old"})
        self.conf({"systemctl": [{"match": "NRestarts", "outputs": nrestarts(9)}]})
        rc, out = self.run_cmd("guard")
        self.assertEqual(rc, 0)
        self.assertIn("newer", out)
        self.assertNotIn(["--user", "stop", "plasma-plasmashell.service"], self.calls("systemctl"))

    def test_every_apply_makes_a_new_generation(self):
        vid = self.add("v.mp4", SAMPLES["mp4"][0])
        self.conf({"dbus-send": [{"outputs": [DBUS_OK]}], "systemctl": [{"match": "NRestarts", "outputs": nrestarts(3)}]})
        self.apply(type="video", video=vid["id"])
        first = self.bg.read_pending()["gen"]
        self.assertEqual(self.bg.read_state("current.json")["gen"], first)
        self.apply(type="color")
        self.assertNotEqual(self.bg.read_state("current.json")["gen"], first)

    def test_stop_guard_ends_a_detached_guard_by_pid_only_if_it_is_ours(self):
        sleeper = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(sleeper.kill)
        self.bg.write_state("guard.json", {"pid": sleeper.pid, "gen": "g"})
        self.bg.stop_guard()
        time.sleep(0.2)
        self.assertIsNone(sleeper.poll(), "not our guard program: left alone")
        guard = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", os.path.realpath(BACKEND), "guard"])
        self.addCleanup(guard.kill)
        with mock.patch.object(self.bg, "guard_cmdline_ok", lambda pid: pid == guard.pid):
            self.bg.write_state("guard.json", {"pid": guard.pid, "gen": "g"})
            self.bg.stop_guard()
        self.assertEqual(guard.wait(timeout=5), -signal.SIGTERM)
        self.assertIsNone(self.bg.read_state("guard.json"))

    def test_apply_waits_out_a_recovery_and_restarts_a_shell_left_stopped(self):
        # A recovery in progress (marker with a live pid): apply does not interfere.
        guard = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", os.path.realpath(BACKEND), "guard"])
        self.addCleanup(guard.wait)
        self.addCleanup(guard.kill)
        self.bg.write_state("recovering.json", {"pid": guard.pid, "at": time.time()})
        rc, out = self.apply(type="color")
        self.assertEqual(rc, 1)
        self.assertIn("restarting the desktop", out)
        self.assertEqual(self.calls("dbus-send"), [])
        self.assertNotIn(["--user", "stop", "forge-background-guard.service"], self.calls("systemctl"))
        # A recovery that died half way (its pid is gone): apply starts plasmashell again first.
        dead = subprocess.Popen(["/bin/true"])
        dead.wait()
        self.bg.write_state("recovering.json", {"pid": dead.pid, "at": time.time()})
        self.clear()
        self.conf({"dbus-send": [{"outputs": [DBUS_OK]}],
                   "systemctl": [{"match": "is-active", "outputs": ["inactive\n", "active\n"], "rc": 0}]})
        rc, out = self.apply(type="color")
        self.assertEqual(rc, 0, out)
        calls = self.calls()
        start = calls.index({"tool": "systemctl", "argv": ["--user", "start", "plasma-plasmashell.service"]})
        self.assertLess(start, [c["tool"] for c in calls].index("dbus-send"))
        self.assertIsNone(self.bg.read_state("recovering.json"))


class ChoicesCapTest(BackendBase):
    content = WorkshopTest.content
    item = WorkshopTest.item

    def test_the_list_never_holds_more_than_the_page_keeps(self):
        self.install_plugin(WE_ID)
        d = self.content()
        for i in range(1, 1011):
            self.item(d, str(i), {"title": "W%d" % i, "type": "video", "file": "main.mp4"})
        rc, out = self.run_cmd("options", self.payload())
        got = json.loads(out)["fields"]["we_item"]
        self.assertLessEqual(len(got["choices"]), self.fa.CHOICES_MAX)
        self.assertEqual(self.bg.WORKSHOP_MAX, self.fa.CHOICES_MAX)
        self.assertIn("first 1000", got["note"])


GUARD_FAKE = r'''#!/usr/bin/python3
import os, sys, time
d = os.environ["POC_DIR"]
name = os.path.basename(sys.argv[0])
args = " ".join(sys.argv[1:])
with open(os.path.join(d, "log"), "a") as f:
    f.write(name + " " + args + "\n")
if name == "systemd-run":
    sys.exit(1)
if name == "dbus-send":
    print('   string "forge-ok 1"')
if name == "systemctl":
    if "NRestarts" in args:
        print("LoadState=loaded\nActiveState=active\nNRestarts=" + open(os.path.join(d, "nrestarts")).read().strip())
    elif "ActiveState" in args:
        print("active")
    elif "is-active" in args:
        print("inactive"); sys.exit(3)
    elif args.startswith("--user stop plasma"):
        time.sleep(float(os.environ.get("POC_STOP_SLEEP", "0")))
'''


@unittest.skipUnless(os.geteuid() == 0, "needs root to switch to a test user")
class GuardProcessTest(unittest.TestCase):
    """The real program, as a normal user, with fake system tools (from the reviewer's proofs)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.bin = os.path.join(self.d, "bin")
        os.makedirs(self.bin)
        for n in ("systemd-run", "dbus-send", "systemctl", "kwriteconfig6"):
            with open(os.path.join(self.bin, n), "w") as f:
                f.write(GUARD_FAKE)
            os.chmod(os.path.join(self.bin, n), 0o755)
        self.home = os.path.join(self.d, "home")
        os.makedirs(self.home)
        with open(os.path.join(self.d, "nrestarts"), "w") as f:
            f.write("3\n")
        open(os.path.join(self.d, "log"), "w").close()
        walls = os.path.join(self.d, "walls", VIDEO_ID)
        os.makedirs(walls)
        open(os.path.join(walls, "metadata.json"), "w").close()
        for root, dirs, files in os.walk(self.d):
            for n in dirs + files:
                os.chown(os.path.join(root, n), tap.OWNER, tap.OWNER)
        os.chown(self.d, tap.OWNER, tap.OWNER)
        os.chmod(self.d, 0o755)
        self.env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "POC_DIR": self.d, "FORGE_LIB": LIB,
                    "FORGE_PLASMA_WALLPAPERS": os.path.join(self.d, "walls"),
                    "XDG_STATE_HOME": os.path.join(self.home, "state"),
                    "XDG_CONFIG_HOME": os.path.join(self.home, "config"),
                    "XDG_DATA_HOME": os.path.join(self.home, "data")}

    def tearDown(self):
        subprocess.run(["pkill", "-u", str(tap.OWNER), "-f", "forge-appearance-background"], capture_output=True)
        shutil.rmtree(self.d, ignore_errors=True)

    def run_bg(self, cmd, payload=None, env=None):
        p = subprocess.run([sys.executable, BACKEND, cmd], input=json.dumps(payload or {}).encode(),
                           capture_output=True, env=dict(self.env, **(env or {})),
                           preexec_fn=tap.as_uid(tap.OWNER), timeout=60)
        return p.returncode, p.stdout.decode()

    def log(self):
        with open(os.path.join(self.d, "log")) as f:
            return f.read()

    def write_state(self, name, obj):
        st = os.path.join(self.home, "state", "forge", "background")
        os.makedirs(st, mode=0o700, exist_ok=True)
        with open(os.path.join(st, name), "w") as f:
            json.dump(obj, f)
        for p in (os.path.join(self.home, "state"), os.path.join(self.home, "state", "forge"), st,
                  os.path.join(st, name)):
            os.chown(p, tap.OWNER, tap.OWNER)

    def wait_log(self, text, seconds=10):
        for _ in range(int(seconds * 20)):
            if text in self.log():
                return True
            time.sleep(0.05)
        return False

    def test_a_sigterm_during_recovery_still_starts_plasmashell(self):
        self.write_state("current.json", {"gen": "g"})
        self.write_state("pending.json", {"plugin": VIDEO_ID, "restarts": 3, "at": time.time(), "gen": "g"})
        with open(os.path.join(self.d, "nrestarts"), "w") as f:
            f.write("5\n")
        p = subprocess.Popen([sys.executable, BACKEND, "guard"], env=dict(self.env, POC_STOP_SLEEP="2"),
                             preexec_fn=tap.as_uid(tap.OWNER), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True)
        self.assertTrue(self.wait_log("stop plasma-plasmashell"))
        os.killpg(p.pid, signal.SIGTERM)       # what systemd does to the unit's control group
        p.wait(timeout=30)
        p.stdout.close()
        self.assertIn("--user start plasma-plasmashell.service", self.log(), "plasmashell must not stay stopped")

    def test_a_detached_guard_does_nothing_after_a_newer_apply(self):
        code = ("import sys, json; sys.path.insert(0, %r); import forgeappearance as fa;"
                "m = fa.media_add(%r); s = fa.load_schema(); v = fa.load(s);"
                "v['background'].update(type='video', video=m['id']); c, e = fa.check_values({'background': v['background']}, s);"
                "fa.save(c, s); print(json.dumps(fa._backend_payload('background', s, fa.sections(s))))")
        sample = os.path.join(self.d, "v.mp4")
        with open(sample, "wb") as f:
            f.write(SAMPLES["mp4"][0])
        os.chown(sample, tap.OWNER, tap.OWNER)
        r = subprocess.run([sys.executable, "-c", code % (LIB, sample)], capture_output=True,
                           env=dict(self.env, FORGE_APPEARANCE_SCHEMA=SHIPPED, PATH=self.bin),
                           preexec_fn=tap.as_uid(tap.OWNER))
        self.assertEqual(r.returncode, 0, r.stderr)
        video_payload = json.loads(r.stdout)
        rc, out = self.run_bg("apply", video_payload, {"FORGE_BG_WATCH_S": "8"})
        self.assertIn("runs in the background (without systemd-run)", out)
        color_payload = json.loads(json.dumps(video_payload))
        color_payload["values"]["type"] = "color"
        rc, out = self.run_bg("apply", color_payload)
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.d, "nrestarts"), "w") as f:
            f.write("5\n")
        time.sleep(4)
        self.assertNotIn("--user stop plasma-plasmashell.service", self.log(), "a stale guard acted")
        rc, out = self.run_bg("apply", color_payload)
        self.assertNotIn("crashed", out)


# 8. Second review round (rvBG): whole-unit SIGTERM, reused pids, a unit that is installed but not running.

KW_SLOW = r'''#!/usr/bin/python3
import os, sys, time
d = os.environ["POC_DIR"]
open(os.path.join(d, "log"), "a").write("kw start " + " ".join(sys.argv[1:]) + "\n")
time.sleep(1)
open(os.path.join(d, "log"), "a").write("kw done\n")
'''


@unittest.skipUnless(os.geteuid() == 0, "needs root to switch to a test user")
class GroupTermTest(unittest.TestCase):
    # GuardProcessTest's helpers, without running its tests twice.
    setUp, tearDown = GuardProcessTest.setUp, GuardProcessTest.tearDown
    write_state, wait_log, log = GuardProcessTest.write_state, GuardProcessTest.wait_log, GuardProcessTest.log

    def test_a_sigterm_to_the_whole_unit_does_not_lose_a_recovery_write(self):
        with open(os.path.join(self.bin, "kwriteconfig6"), "w") as f:
            f.write(KW_SLOW)
        cfg = os.path.join(self.home, "config")
        os.makedirs(cfg)
        applets = os.path.join(cfg, "plasma-org.kde.plasma.desktop-appletsrc")
        with open(applets, "w") as f:
            f.write("[Containments][1]\nwallpaperplugin=%s\n" % VIDEO_ID)
        for p in (cfg, applets):
            os.chown(p, tap.OWNER, tap.OWNER)
        self.write_state("current.json", {"gen": "g"})
        self.write_state("pending.json", {"plugin": VIDEO_ID, "restarts": 3, "at": time.time(), "gen": "g"})
        with open(os.path.join(self.d, "nrestarts"), "w") as f:
            f.write("5\n")
        p = subprocess.Popen([sys.executable, BACKEND, "guard"], env=self.env, preexec_fn=tap.as_uid(tap.OWNER),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self.assertTrue(self.wait_log("kw start"))
        os.killpg(p.pid, signal.SIGTERM)      # logout or RuntimeMaxSec: systemd signals the whole unit
        p.communicate(timeout=60)
        time.sleep(1.5)
        log = self.log()
        self.assertIn("--user start plasma-plasmashell.service", log)
        self.assertEqual(log.count("kw start"), 2)
        self.assertEqual(log.count("kw done"), 2, "every recovery write must finish")


class StaleMarkerTest(BackendBase):
    @unittest.skipUnless(os.geteuid() == 0, "needs root to run a process as another user")
    def test_a_reused_pid_of_another_program_does_not_block_apply(self):
        other = subprocess.Popen(["/bin/sleep", "30"], cwd="/", preexec_fn=tap.as_uid(tap.OWNER))
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.bg.write_state("recovering.json", {"pid": other.pid, "at": time.time() - 5})
        rc, out = self.apply(type="color")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("restarting the desktop", out)
        mine = subprocess.Popen(["/bin/sleep", "30"])          # ours, but not the guard program
        self.addCleanup(mine.wait)
        self.addCleanup(mine.kill)
        self.bg.write_state("recovering.json", {"pid": mine.pid, "at": time.time() - 5})
        self.assertFalse(self.bg.recovery_running())


class InstalledButNotRunningTest(BackendBase):
    def test_no_guard_when_the_unit_exists_but_plasmashell_was_not_started_by_it(self):
        self.install_plugin(VIDEO_ID)
        self.conf({"dbus-send": [{"outputs": [DBUS_OK]}],
                   "systemctl": [{"match": "NRestarts",
                                  "outputs": ["LoadState=loaded\nActiveState=inactive\nNRestarts=0\n"]}]})
        vid = self.add("v.mp4", SAMPLES["mp4"][0])
        rc, out = self.apply(type="video", video=vid["id"])
        self.assertEqual(rc, 0, out)
        self.assertIn("crash guard is off", out)
        self.assertEqual(self.calls("systemd-run"), [])
