"""P5 backups: forge-backup saves the home folder to a USB or second drive with restic."""
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-backup")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
USER_UNITS = os.path.join(ROOT, "files/system/usr/lib/systemd/user")
RULE = os.path.join(ROOT, "files/system/usr/lib/udev/rules.d/90-forge-backup.rules")
PASSWORD = "fake-test-password-only"   # obvious test value, never a real one  # pragma: allowlist secret
LABEL = "FORGEBACKUP"

# A fake restic: records how it was called and whether the password shows up in its
# command line or environment (what the process list and other users could see).
FAKE_RESTIC = r'''#!@PY@
import json, os, sys, time
args = sys.argv[1:]
mode = os.environ.get("FAKE_RESTIC_MODE", "ok")
pwfile = os.environ.get("RESTIC_PASSWORD_FILE", "")
pw = open(pwfile).read().strip() if pwfile and os.path.exists(pwfile) else ""
cmdline = open("/proc/self/cmdline", "rb").read()
environ = open("/proc/self/environ", "rb").read()
rec = {"argv": args, "pwfile": pwfile,
       "env_restic": sorted(k for k in os.environ if k.startswith("RESTIC_")),
       "pw_in_cmdline": bool(pw) and pw.encode() in cmdline,
       "pw_in_environ": bool(pw) and pw.encode() in environ}
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(rec) + "\n")
repo = args[args.index("-r") + 1] if "-r" in args else ""
cmd = [a for a in args if not a.startswith("-") and a != repo][0] if args else ""
if mode == "wrongpw" and cmd != "version":
    sys.stderr.write("Fatal: wrong password or no key found\n"); sys.exit(1)
if cmd == "init":
    os.makedirs(repo, exist_ok=True); open(os.path.join(repo, "config"), "w").write("x"); sys.exit(0)
if cmd == "backup":
    if mode == "slow":
        time.sleep(3)
    if mode == "full":
        sys.stderr.write("Fatal: unable to save snapshot: write /x/data/ab: no space left on device\n"); sys.exit(1)
    if mode == "fail":
        sys.stderr.write("Fatal: something else broke\n"); sys.exit(1)
    with open(os.path.join(repo, "snaps"), "a") as f:
        f.write("s\n")
    print(json.dumps({"message_type": "summary", "snapshot_id": "ab12cd34" * 8, "data_added": 1234,
                      "total_bytes_processed": 5678, "files_new": 2}))
    sys.exit(3 if mode == "partial" else 0)
if cmd == "snapshots":
    plant = os.environ.get("FAKE_PLANT")
    if plant:   # another user makes the restore folder while forge-backup talks to restic
        os.makedirs(plant, exist_ok=True); os.chmod(plant, 0o777)
    n = len(open(os.path.join(repo, "snaps")).read().split()) if os.path.exists(os.path.join(repo, "snaps")) else 0
    print(json.dumps([{"short_id": "ab12cd34", "id": "ab12cd34" * 8, "time": "2026-11-19T10:00:00+07:00",
                       "hostname": "pc", "paths": ["/home/x"]}] * n)); sys.exit(0)
if cmd == "restore":
    t = args[args.index("--target") + 1]
    os.makedirs(t, exist_ok=True); open(os.path.join(t, "restored.txt"), "w").write("r"); sys.exit(0)
if cmd == "check":
    print("no errors were found"); sys.exit(0)
sys.exit(0)
'''

FAKE_UDISKSCTL = r'''#!/bin/sh
echo "udisksctl $*" >> "$FAKE_CALLS"
if [ "$1" = mount ]; then
  mkdir -p "$FAKE_MOUNT"
  echo "99 1 8:33 / $FAKE_MOUNT rw,nosuid - ext4 $3 rw" >> "$FAKE_MOUNTINFO"
  echo "Mounted $3 at $FAKE_MOUNT"
fi
exit 0
'''


def load_tool():
    if LIB not in sys.path:
        sys.path.insert(0, LIB)   # forgecfg from this repo, not /usr/lib/forge
    loader = importlib.machinery.SourceFileLoader("forge_backup", TOOL)
    spec = importlib.util.spec_from_loader("forge_backup", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.home = os.path.join(self.t, "home", "user")
        os.makedirs(os.path.join(self.home, "Documents"))
        with open(os.path.join(self.home, "Documents", "notes.txt"), "w") as f:
            f.write("hello\n")
        self.mount = os.path.join(self.t, "media", LABEL)
        self.dev = os.path.join(self.t, "dev", "sdz1")
        os.makedirs(os.path.dirname(self.dev))
        open(self.dev, "w").close()
        self.bylabel = os.path.join(self.t, "by-label")
        os.makedirs(self.bylabel)
        self.mountinfo = os.path.join(self.t, "mountinfo")
        open(self.mountinfo, "w").close()
        self.bin = os.path.join(self.t, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.t, "restic.log")
        self.calls_file = os.path.join(self.t, "calls")
        self.conf = os.path.join(self.t, "settings.json")
        self.settings()
        self.write_fakes()

    def write_fakes(self):
        fakes = {"restic": FAKE_RESTIC.replace("@PY@", sys.executable), "udisksctl": FAKE_UDISKSCTL,
                 "notify-send": '#!/bin/sh\necho "notify-send $*" >> "$FAKE_CALLS"\n'}
        for name, text in fakes.items():
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, 0o755)

    def settings(self, **vals):
        with open(self.conf, "w") as f:
            json.dump({k.replace("__", "."): v for k, v in vals.items()}, f)

    def plug(self, mounted=True):
        os.symlink(self.dev, os.path.join(self.bylabel, LABEL))
        if mounted:
            os.makedirs(self.mount, exist_ok=True)
            with open(self.mountinfo, "a") as f:
                f.write(f"98 1 8:33 / {self.mount} rw,nosuid - ext4 {self.dev} rw\n")

    def repo(self):
        return os.path.join(self.mount, "forge-backup", "pc")

    def set_up_done(self):
        data = os.path.join(self.home, ".local/share/forge-backup")
        os.makedirs(data, mode=0o700)
        fd = os.open(os.path.join(data, "password"), os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, (PASSWORD + "\n").encode())
        os.close(fd)
        with open(os.path.join(data, "config.json"), "w") as f:
            json.dump({"host": "pc"}, f)
        if os.path.isdir(self.mount):
            os.makedirs(self.repo())
            open(os.path.join(self.repo(), "config"), "w").close()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        env.update(HOME=self.home, FORGE_LIB=LIB, FORGE_SCHEMA=SCHEMA, FORGE_CONF=self.conf,
                   FORGE_BACKUP_BYLABEL=self.bylabel, FORGE_BACKUP_MOUNTINFO=self.mountinfo,
                   FAKE_LOG=self.log, FAKE_CALLS=self.calls_file, FAKE_MOUNT=self.mount,
                   FAKE_MOUNTINFO=self.mountinfo, PATH=self.bin + ":" + os.environ["PATH"])
        env.pop("XDG_STATE_HOME", None)
        env.pop("XDG_DATA_HOME", None)
        env.update(extra)
        return env

    def run_tool(self, *args, stdin=None, timeout=60, **extra):
        return subprocess.run([sys.executable, TOOL, *args], env=self.env(**extra), input=stdin,
                              capture_output=True, text=True, timeout=timeout, start_new_session=True)

    def restic_calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def commands(self):
        out = []
        for c in self.restic_calls():
            a = c["argv"]
            repo = a[a.index("-r") + 1] if "-r" in a else None
            out.append([x for x in a if not x.startswith("-") and x != repo][0])
        return out

    def other_calls(self):
        if not os.path.exists(self.calls_file):
            return []
        with open(self.calls_file) as f:
            return f.read().splitlines()

    def status(self):
        with open(os.path.join(self.home, ".local/state/forge-backup/status.json")) as f:
            return json.load(f)

    def write_status(self, **vals):
        d = os.path.join(self.home, ".local/state/forge-backup")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "status.json"), "w") as f:
            json.dump(vals, f)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        with open(SCHEMA) as f:
            self.schema = json.load(f)
        self.keys = {s["key"]: s for s in self.schema["settings"]}

    def test_backup_group_and_settings_exist_with_defaults(self):
        groups = {g["id"]: g["label"] for g in self.schema["groups"]}
        self.assertEqual(groups.get("backup"), "Backups")
        want = {"backup.enabled": False, "backup.drive_label": "FORGEBACKUP", "backup.every_hours": 24,
                "backup.keep_daily": 7, "backup.keep_weekly": 4, "backup.keep_monthly": 6,
                "backup.exclude_big": True}
        for k, v in want.items():
            s = self.keys[k]
            self.assertEqual(s["default"], v, k)
            self.assertEqual(s["group"], "backup", k)
            for field in ("label", "help", "tags"):
                self.assertTrue(s.get(field), f"{k} has no {field}")
        self.assertEqual(self.keys["backup.enabled"]["type"], "toggle")
        self.assertEqual(self.keys["backup.exclude_big"]["type"], "toggle")
        self.assertEqual((self.keys["backup.every_hours"]["min"], self.keys["backup.every_hours"]["max"]), (1, 168))
        for k in ("backup.keep_daily", "backup.keep_weekly", "backup.keep_monthly"):
            self.assertEqual(self.keys[k]["type"], "number", k)

    def test_drive_label_is_checked_like_a_filesystem_label(self):
        sys.path.insert(0, LIB)
        import forgecfg
        s = self.keys["backup.drive_label"]
        self.assertEqual(forgecfg.coerce(s, "FORGEBACKUP"), "FORGEBACKUP")
        self.assertEqual(forgecfg.coerce(s, "my_drive-2"), "my_drive-2")
        for bad in ("BAD LABEL", "../x", "a/b", "TWELVECHARSX", "x*", "x\ny", ""):
            with self.assertRaises(ValueError, msg=bad):
                forgecfg.coerce(s, bad)

    def test_whole_schema_still_validates_with_defaults(self):
        sys.path.insert(0, LIB)
        import forgecfg
        clean, errors = forgecfg.validate({}, self.schema)
        self.assertEqual(errors, [])
        self.assertEqual(clean["backup.drive_label"], "FORGEBACKUP")


class ImageFilesTest(unittest.TestCase):
    def test_restic_is_in_the_image(self):
        text = open(os.path.join(ROOT, "recipes/common-modules.yml")).read()
        self.assertRegex(text, r"\n\s+- restic\s+#")
        self.assertRegex(text, r"user:\s*\n\s+enabled:\s*\n(\s+- .*\n)*\s+- forge-backup\.timer")

    def test_tool_is_executable_in_the_image(self):
        self.assertIn("/usr/bin/forge-backup", open(os.path.join(ROOT, "files/scripts/finalize.sh")).read())

    def test_timer_and_service(self):
        svc = open(os.path.join(USER_UNITS, "forge-backup.service")).read()
        tim = open(os.path.join(USER_UNITS, "forge-backup.timer")).read()
        self.assertIn("ExecStart=/usr/bin/forge-backup run --auto", svc)
        self.assertIn("Type=oneshot", svc)
        self.assertIn("IOSchedulingClass=idle", svc)
        self.assertIn("OnUnitActiveSec=1h", tim)
        self.assertIn("WantedBy=timers.target", tim)

    def test_drive_plug_rule_starts_the_user_service_without_running_anything_as_root(self):
        rule = open(RULE).read()
        lines = [x for x in rule.splitlines() if x and not x.startswith("#")]
        self.assertEqual(len(lines), 1)
        self.assertIn('ACTION=="add"', lines[0])
        self.assertIn('SUBSYSTEM=="block"', lines[0])
        self.assertIn('ENV{SYSTEMD_USER_WANTS}+="forge-backup.service"', lines[0])
        self.assertNotIn("RUN", lines[0])


class UnitTests(Base):
    def setUp(self):
        super().setUp()
        self.fb = load_tool()

    def test_restic_env_has_only_the_password_file(self):
        base = {"PATH": "/usr/bin", "RESTIC_PASSWORD": "leak",  # pragma: allowlist secret
                "RESTIC_PASSWORD_COMMAND": "cat x",  # pragma: allowlist secret
                "RESTIC_REPOSITORY": "/elsewhere", "HOME": "/home/u"}
        env = self.fb.restic_env("/home/u/.local/share/forge-backup/password", base)
        self.assertEqual([k for k in env if k.startswith("RESTIC_")], ["RESTIC_PASSWORD_FILE"])
        self.assertEqual(env["RESTIC_PASSWORD_FILE"], "/home/u/.local/share/forge-backup/password")

    def test_built_argv_never_contains_the_password(self):
        s = {"backup.exclude_big": True, "backup.keep_daily": 7, "backup.keep_weekly": 4,
             "backup.keep_monthly": 6, "ai.models_dir": "/var/lib/ollama/models"}
        argv = self.fb.backup_argv("/media/x/forge-backup/pc", self.home, s)
        argv += self.fb.forget_argv("/media/x/forge-backup/pc", s)
        self.assertNotIn(PASSWORD, " ".join(argv))
        self.assertFalse(any(a.startswith("--password") for a in argv))

    def test_exclude_patterns(self):
        h = self.home
        on = self.fb.exclude_patterns(h, {"backup.exclude_big": True, "ai.models_dir": h + "/models"})
        for p in (h + "/.cache", h + "/.local/share/Trash", h + "/.var/app/*/cache", "steamapps/common",
                  "steamapps/shadercache", h + "/.ollama/models", h + "/models", "*.qcow2", "*.vmdk",
                  h + "/.local/share/libvirt/images", h + "/.local/share/forge-backup"):
            self.assertIn(p, on)
        self.assertNotIn("steamapps/compatdata", on)   # game saves for Proton games live there
        off = self.fb.exclude_patterns(h, {"backup.exclude_big": False, "ai.models_dir": h + "/models"})
        self.assertEqual(off, [h + "/.local/share/forge-backup"])

    def test_models_dir_outside_home_is_not_an_exclude(self):
        on = self.fb.exclude_patterns(self.home, {"backup.exclude_big": True,
                                                  "ai.models_dir": "/var/lib/ollama/models"})
        self.assertNotIn("/var/lib/ollama/models", on)

    def test_home_with_glob_characters_is_escaped(self):
        on = self.fb.exclude_patterns("/home/a[1]*", {"backup.exclude_big": True, "ai.models_dir": "/x"})
        self.assertIn(r"/home/a\[1\]\*/.cache", on)

    def test_restore_target_rules(self):
        ok = os.path.join(self.t, "restored", "new")
        self.assertEqual(self.fb.check_restore_target(ok, self.home)[1], None)
        empty = os.path.join(self.t, "empty")
        os.makedirs(empty)
        full = os.path.join(self.t, "full")
        os.makedirs(full)
        open(os.path.join(full, "f"), "w").close()
        link = os.path.join(self.t, "link")
        os.symlink(empty, link)
        afile = os.path.join(self.t, "afile")
        open(afile, "w").close()
        for bad in (self.home, self.home + "/", full, link, afile, "/", os.path.dirname(self.home), empty):
            self.assertIsNotNone(self.fb.check_restore_target(bad, self.home)[1], bad)

    def test_restore_target_parent_must_be_private(self):
        # A folder other users can write to (sticky like /tmp, or not) is refused, and so is one
        # that belongs to another user. Your home folder (or a private folder in it) is fine.
        cases = {"sticky": (0o1777, None), "open": (0o777, None), "group": (0o775, None),
                 "theirs": (0o755, 65534)}
        for name, (mode, owner) in cases.items():
            d = os.path.join(self.t, name)
            os.makedirs(d)
            os.chmod(d, mode)
            if owner is not None:
                os.chown(d, owner, owner)
            err = self.fb.check_restore_target(os.path.join(d, "Restored"), self.home)[1]
            self.assertIsNotNone(err, name)
            self.assertIn("home", err, name)
            err = self.fb.check_restore_target(os.path.join(d, "a", "Restored"), self.home)[1]
            self.assertIsNotNone(err, name + "/a")
        self.assertIsNone(self.fb.check_restore_target(os.path.join(self.home, "Restored"), self.home)[1])

    def test_restore_path_is_kept_inside_the_snapshot(self):
        self.assertEqual(self.fb.include_path("Documents/a.txt", "/home/u"), "/Documents/a.txt")
        self.assertEqual(self.fb.include_path("/home/u/Documents", "/home/u"), "/Documents")
        for bad in ("../etc", "Documents/../../x", "/etc/passwd", "a\nb", ""):
            self.assertIsNone(self.fb.include_path(bad, "/home/u"), bad)

    def test_snapshot_id_is_validated(self):
        for good in ("latest", "ab12cd34", "ab12cd34" * 8):
            self.assertTrue(self.fb.valid_snapshot(good), good)
        for bad in ("--target=/", "ab12", "xyz12345", "latest;rm", ""):
            self.assertFalse(self.fb.valid_snapshot(bad), bad)


class RunLogicTest(Base):
    def test_automatic_run_does_nothing_when_turned_off(self):
        self.plug()
        self.set_up_done()
        r = self.run_tool("run", "--auto")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.restic_calls(), [])

    def test_automatic_run_waits_until_the_interval_has_passed(self):
        self.settings(backup__enabled=True, backup__every_hours=24)
        self.plug()
        self.set_up_done()
        self.write_status(last_success=int(time.time()) - 3600)
        r = self.run_tool("run", "--auto")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.restic_calls(), [])

    def test_run_backs_up_home_then_prunes_and_writes_status(self):
        self.settings(backup__enabled=True)
        self.plug()
        self.set_up_done()
        r = self.run_tool("run", "--auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cmds = self.commands()
        self.assertIn("backup", cmds)
        self.assertLess(cmds.index("backup"), cmds.index("forget"))
        backup = self.restic_calls()[cmds.index("backup")]["argv"]
        self.assertEqual(backup[-1], self.home)
        self.assertIn("--one-file-system", backup)
        forget = self.restic_calls()[cmds.index("forget")]["argv"]
        for a in ("--prune", "--keep-daily", "--keep-weekly", "--keep-monthly"):
            self.assertIn(a, forget)
        self.assertEqual(forget[forget.index("--keep-daily") + 1], "7")
        st = self.status()
        self.assertEqual(st["result"], "ok")
        self.assertEqual(st["snapshot_count"], 1)
        for k in ("version", "last_run", "last_success", "size_bytes", "drive_free_bytes", "message",
                  "every_hours", "last_snapshot", "drive_label"):
            self.assertIn(k, st)
        self.assertEqual(st["last_run"], st["last_success"])
        mode = os.stat(os.path.join(self.home, ".local/state/forge-backup/status.json")).st_mode & 0o777
        self.assertEqual(mode & 0o077, 0)
        self.assertFalse(any(c.startswith("notify-send") for c in self.other_calls()))

    def test_password_goes_only_through_the_password_file(self):
        self.settings(backup__enabled=True)
        self.plug()
        self.set_up_done()
        r = self.run_tool("run", "--auto", RESTIC_PASSWORD="inherited-fake")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.restic_calls()
        self.assertTrue(calls)
        pwfile = os.path.join(self.home, ".local/share/forge-backup/password")
        for c in calls:
            self.assertEqual(c["env_restic"], ["RESTIC_PASSWORD_FILE"], c)
            self.assertEqual(c["pwfile"], pwfile)
            self.assertFalse(c["pw_in_cmdline"], c["argv"])
            self.assertFalse(c["pw_in_environ"])
            self.assertNotIn(PASSWORD, " ".join(c["argv"]))
        for text in (r.stdout, r.stderr, json.dumps(self.status()), "\n".join(self.other_calls())):
            self.assertNotIn(PASSWORD, text)

    def test_failed_backup_never_prunes_and_notifies(self):
        self.settings(backup__enabled=True)
        self.plug()
        self.set_up_done()
        r = self.run_tool("run", "--auto", FAKE_RESTIC_MODE="fail")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("forget", self.commands())
        self.assertEqual(self.status()["result"], "failed")
        self.assertTrue(any(c.startswith("notify-send") for c in self.other_calls()))

    def test_unreadable_files_still_count_as_a_backup(self):
        self.settings(backup__enabled=True)
        self.plug()
        self.set_up_done()
        self.run_tool("run", "--auto", FAKE_RESTIC_MODE="partial")
        self.assertIn("forget", self.commands())
        self.assertEqual(self.status()["result"], "ok_with_warnings")

    def test_full_drive_gives_a_clear_message(self):
        self.plug()
        self.set_up_done()
        r = self.run_tool("run", FAKE_RESTIC_MODE="full")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("full", (r.stdout + r.stderr).lower())
        self.assertIn(LABEL, r.stdout + r.stderr)
        self.assertEqual(self.status()["result"], "drive_full")
        self.assertNotIn("forget", self.commands())

    def test_wrong_password_gives_a_clear_message(self):
        self.plug()
        self.set_up_done()
        r = self.run_tool("run", FAKE_RESTIC_MODE="wrongpw")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("password", (r.stdout + r.stderr).lower())
        self.assertIn("forge-backup init", r.stdout + r.stderr)
        self.assertEqual(self.status()["result"], "wrong_password")

    def test_missing_drive_by_hand(self):
        self.set_up_done()
        r = self.run_tool("run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(LABEL, r.stdout + r.stderr)
        self.assertEqual(self.restic_calls(), [])
        self.assertEqual(self.status()["result"], "drive_missing")

    def test_missing_drive_is_quiet_until_backups_are_overdue(self):
        self.settings(backup__enabled=True, backup__every_hours=24)
        self.set_up_done()
        when = int(time.time()) - 30 * 3600     # read the clock once (it can tick between two reads)
        self.write_status(last_success=when)
        self.run_tool("run", "--auto")
        self.assertFalse(any(c.startswith("notify-send") for c in self.other_calls()))
        self.assertEqual(self.status()["last_success"], when)

    def test_overdue_notifies_once_a_day(self):
        self.settings(backup__enabled=True, backup__every_hours=24)
        self.set_up_done()
        self.write_status(last_success=int(time.time()) - 4 * 86400)
        self.run_tool("run", "--auto")
        self.run_tool("run", "--auto")
        notes = [c for c in self.other_calls() if c.startswith("notify-send")]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("No backup", notes[0])

    def test_drive_not_mounted_is_mounted_with_udisksctl_and_unmounted_after(self):
        self.plug(mounted=False)
        self.set_up_done()
        os.makedirs(self.repo())
        open(os.path.join(self.repo(), "config"), "w").close()
        r = self.run_tool("run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.other_calls()
        self.assertIn(f"udisksctl mount -b {self.dev} --no-user-interaction", calls)
        self.assertIn(f"udisksctl unmount -b {self.dev} --no-user-interaction", calls)
        self.assertIn("backup", self.commands())

    def test_not_set_up(self):
        self.plug()
        r = self.run_tool("run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("forge-backup init", r.stdout + r.stderr)
        self.assertEqual(self.restic_calls(), [])

    def test_backup_folder_on_the_drive_that_is_a_link_is_refused(self):
        self.plug()
        self.set_up_done()
        shutil.rmtree(os.path.join(self.mount, "forge-backup"))
        elsewhere = os.path.join(self.t, "elsewhere", "pc")
        os.makedirs(elsewhere)
        open(os.path.join(elsewhere, "config"), "w").close()
        os.symlink(os.path.dirname(elsewhere), os.path.join(self.mount, "forge-backup"))
        r = self.run_tool("run")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("backup", self.commands())

    def test_two_runs_never_overlap(self):
        self.settings(backup__enabled=True)
        self.plug()
        self.set_up_done()
        first = subprocess.Popen([sys.executable, TOOL, "run"], env=self.env(FAKE_RESTIC_MODE="slow"),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 start_new_session=True)
        deadline = time.time() + 20
        while "backup" not in self.commands() and time.time() < deadline:
            time.sleep(0.1)
        second = self.run_tool("run", FAKE_RESTIC_MODE="slow")
        auto = self.run_tool("run", "--auto", FAKE_RESTIC_MODE="slow")
        out, err = first.communicate(timeout=60)
        self.assertEqual(first.returncode, 0, out + err)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already running", (second.stdout + second.stderr).lower())
        self.assertEqual(auto.returncode, 0)
        self.assertEqual(self.commands().count("backup"), 1)

    def test_status_command(self):
        self.write_status(version=1, result="ok", last_run=1700000000, last_success=1700000000,
                          snapshot_count=3, size_bytes=2048, message="Backup saved.")
        r = self.run_tool("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["snapshot_count"], 3)
        self.assertIn("set_up", data)
        self.assertIn("automatic", data)
        r = self.run_tool("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("3", r.stdout)


class RestoreLogicTest(Base):
    def setUp(self):
        super().setUp()
        self.plug()
        self.set_up_done()
        with open(os.path.join(self.repo(), "snaps"), "w") as f:
            f.write("s\n")   # one backup on the fake drive

    def test_restore_into_home_or_a_full_folder_is_refused_before_restic_runs(self):
        full = os.path.join(self.t, "full")
        os.makedirs(full)
        open(os.path.join(full, "keep.txt"), "w").close()
        for target in (self.home, full):
            r = self.run_tool("restore", "latest", "--target", target)
            self.assertNotEqual(r.returncode, 0, target)
        self.assertNotIn("restore", self.commands())
        self.assertEqual(os.listdir(full), ["keep.txt"])

    def test_restore_into_a_new_folder(self):
        target = os.path.join(self.t, "out", "restored")
        r = self.run_tool("restore", "latest", "Documents", "--target", target)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        argv = self.restic_calls()[self.commands().index("restore")]["argv"]
        self.assertIn("--include=/Documents", argv)
        self.assertTrue(os.path.exists(os.path.join(target, "restored.txt")))
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o700)

    def test_restore_through_a_fuse_mount_is_refused(self):
        # A FUSE filesystem can claim any owner and serve links, so the owner checks can't trust it.
        os.makedirs(os.path.join(self.t, "out"))
        for fstype in ("fuse.evilfs", "fuse", "fuseblk"):
            with open(self.mountinfo, "w") as f:
                f.write("99 30 0:77 / %s rw,nosuid,nodev - %s evil rw\n" % (os.path.join(self.t, "out"), fstype))
            target = os.path.join(self.t, "out", "restored-" + fstype.replace(".", "-"))
            r = self.run_tool("restore", "latest", "--target", target)
            self.assertNotEqual(r.returncode, 0, fstype)
            self.assertIn("FUSE", r.stdout + r.stderr, fstype)
            self.assertNotIn("restore", self.commands(), fstype)
            self.assertFalse(os.path.exists(target), fstype)

    def test_restore_into_an_existing_empty_folder_is_refused(self):
        target = os.path.join(self.t, "empty")
        os.makedirs(target)
        r = self.run_tool("restore", "latest", "--target", target)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("restore", self.commands())
        self.assertEqual(os.listdir(target), [])

    def test_folder_made_by_someone_else_after_the_check_is_refused(self):
        # The folder appears between the first check and forge-backup making it (here while
        # restic lists the backups). It must not be used.
        target = os.path.join(self.t, "out", "restored")
        r = self.run_tool("restore", "latest", "--target", target, FAKE_PLANT=target)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("restore", self.commands())
        self.assertEqual(os.listdir(target), [])

    def test_restore_needs_a_target(self):
        r = self.run_tool("restore", "latest")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("restore", self.commands())


class InitLogicTest(Base):
    def test_passwords_that_do_not_match_create_nothing(self):
        self.plug()
        r = self.run_tool("init", stdin="fake-test-password-1\nfake-test-password-2\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("init", self.commands())
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local/share/forge-backup/password")))

    def test_short_or_padded_passwords_are_refused(self):
        self.plug()
        for pw in ("short", " padded-fake-password "):
            r = self.run_tool("init", stdin=f"{pw}\n{pw}\n")
            self.assertNotEqual(r.returncode, 0, pw)
        self.assertNotIn("init", self.commands())

    def test_init_saves_the_password_privately_and_warns(self):
        self.plug()
        r = self.run_tool("init", stdin=f"{PASSWORD}\n{PASSWORD}\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pw = os.path.join(self.home, ".local/share/forge-backup/password")
        self.assertEqual(os.stat(pw).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(pw)).st_mode & 0o777, 0o700)
        self.assertIn("write", r.stdout.lower())
        self.assertIn("another pc", r.stdout.lower())
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)
        init = self.restic_calls()[self.commands().index("init")]
        self.assertTrue(init["argv"][init["argv"].index("-r") + 1].startswith(
            os.path.join(self.mount, "forge-backup") + "/"))
        self.assertFalse(init["pw_in_cmdline"] or init["pw_in_environ"])

    def test_init_without_the_drive(self):
        r = self.run_tool("init", stdin=f"{PASSWORD}\n{PASSWORD}\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(LABEL, r.stdout + r.stderr)
        self.assertEqual(self.restic_calls(), [])


def find_restic():
    return os.environ.get("FORGE_TEST_RESTIC") or shutil.which("restic")


@unittest.skipUnless(find_restic(), "restic is not installed (set FORGE_TEST_RESTIC to its path)")
class RealResticTest(Base):
    """The same tool with the real restic binary, on temp folders."""

    def write_fakes(self):
        super().write_fakes()
        os.remove(os.path.join(self.bin, "restic"))
        os.symlink(os.path.abspath(find_restic()), os.path.join(self.bin, "restic"))

    def init(self):
        self.plug()
        r = self.run_tool("init", stdin=f"{PASSWORD}\n{PASSWORD}\n", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def tree(self, top):
        out = {}
        for d, _, files in os.walk(top):
            for f in files:
                p = os.path.join(d, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, top)] = fh.read()
        return out

    def assert_kept(self, before):
        # restic adds its cache and Forge adds its status file, but nothing that was there may change.
        after = self.tree(self.home)
        for rel, data in before.items():
            if rel.startswith(".local/state/forge-backup/"):
                continue
            self.assertEqual(after.get(rel), data, f"the backup changed or removed {rel}")

    def test_init_run_list_restore_check(self):
        r = self.init()
        self.assertIn("write", r.stdout.lower())
        hosts = os.listdir(os.path.join(self.mount, "forge-backup"))
        self.assertEqual(len(hosts), 1)
        self.assertTrue(os.path.exists(os.path.join(self.mount, "forge-backup", hosts[0], "config")))
        before = self.tree(self.home)
        r = self.run_tool("run", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_kept(before)
        st = self.status()
        self.assertEqual(st["result"], "ok")
        self.assertEqual(st["snapshot_count"], 1)
        self.assertGreater(st["size_bytes"], 0)
        r = self.run_tool("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, re.escape(st["last_snapshot"]))
        target = os.path.join(self.t, "restored")
        r = self.run_tool("restore", st["last_snapshot"], "--target", target, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(target, "Documents", "notes.txt")) as f:
            self.assertEqual(f.read(), "hello\n")
        r = self.run_tool("restore", "latest", "--target", target)
        self.assertNotEqual(r.returncode, 0, "restored into a folder that is not empty")
        r = self.run_tool("restore", "latest", "--target", self.home)
        self.assertNotEqual(r.returncode, 0)
        self.assert_kept(before)
        r = self.run_tool("check", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # A second run. restic keeps the oldest snapshot while there are fewer days than keep_daily,
        # so both stay; forget --prune still ran without removing anything it should keep.
        r = self.run_tool("run", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.status()["result"], "ok")
        self.assertEqual(self.status()["snapshot_count"], 2)

    def test_excludes_with_real_restic(self):
        h = self.home
        files = {
            ".cache/thumbs/x": "c", ".local/share/Trash/files/old": "t",
            ".local/share/Steam/steamapps/common/Game/big.pak": "g",
            ".local/share/Steam/steamapps/compatdata/123/pfx/save.dat": "s",
            "Games/SteamLibrary/steamapps/common/Other/data": "g",
            ".ollama/models/blobs/sha": "m", ".var/app/org.x.App/cache/c": "c",
            ".var/app/org.x.App/config/settings.ini": "k", "VMs/win.qcow2": "v",
            ".local/share/libvirt/images/a.img": "v", "Projects/game.blend": "b",
        }
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(h, rel)), exist_ok=True)
            with open(os.path.join(h, rel), "w") as f:
                f.write(text)
        self.init()
        r = self.run_tool("run", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        target = os.path.join(self.t, "r")
        r = self.run_tool("restore", "latest", "--target", target, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = set(self.tree(target))
        for kept in ("Documents/notes.txt", "Projects/game.blend", ".var/app/org.x.App/config/settings.ini",
                     ".local/share/Steam/steamapps/compatdata/123/pfx/save.dat"):
            self.assertIn(kept, got)
        for skipped in (".cache/thumbs/x", ".local/share/Trash/files/old",
                        ".local/share/Steam/steamapps/common/Game/big.pak",
                        "Games/SteamLibrary/steamapps/common/Other/data", ".ollama/models/blobs/sha",
                        ".var/app/org.x.App/cache/c", "VMs/win.qcow2", ".local/share/libvirt/images/a.img",
                        ".local/share/forge-backup/password"):
            self.assertNotIn(skipped, got)

    def as_nobody(self, *cmd):
        return subprocess.run(["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", *cmd],
                              capture_output=True, text=True)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("setpriv"), "needs root and setpriv")
    def test_another_user_cannot_redirect_a_restore_into_home(self):
        # Review finding: another user makes the target folder in a shared folder, then plants a
        # link in it while restic starts. restic follows the link and writes into the owner's home.
        self.init()
        r = self.run_tool("run", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        notes = os.path.join(self.home, "Documents/notes.txt")
        with open(notes, "w") as f:
            f.write("NEWER WORK\n")
        os.chmod(self.t, 0o711)
        shared = os.path.join(self.t, "shared")
        os.makedirs(shared)
        os.chmod(shared, 0o1777)
        tgt = os.path.join(shared, "Restored")
        self.assertEqual(self.as_nobody("mkdir", "-m", "777", tgt).returncode, 0)
        real = os.path.realpath(os.path.join(self.bin, "restic"))
        wrapper = os.path.join(self.bin, "restic")
        os.remove(wrapper)
        with open(wrapper, "w") as f:
            f.write(f"""#!/bin/sh
case " $* " in *" restore "*) setpriv --reuid=65534 --regid=65534 --clear-groups \\
  ln -s {self.home}/Documents {tgt}/Documents 2>/dev/null;; esac
exec {real} "$@"
""")
        os.chmod(wrapper, 0o755)
        r = self.run_tool("restore", "latest", "--target", tgt, timeout=120)
        with open(notes) as f:
            self.assertEqual(f.read(), "NEWER WORK\n", "the restore wrote into the home folder")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        # A fresh folder in the same shared place is refused too.
        r = self.run_tool("restore", "latest", "--target", os.path.join(shared, "New"), timeout=120)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(os.path.join(shared, "New")))

    def test_wrong_password_with_real_restic(self):
        self.init()
        pw = os.path.join(self.home, ".local/share/forge-backup/password")
        with open(pw, "w") as f:
            f.write("another-fake-test-password\n")
        r = self.run_tool("run", timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("password", (r.stdout + r.stderr).lower())
        self.assertEqual(self.status()["result"], "wrong_password")
        self.assertNotIn("another-fake-test-password", r.stdout + r.stderr)

    def test_init_again_reconnects_to_the_existing_backup(self):
        self.init()
        os.remove(os.path.join(self.home, ".local/share/forge-backup/password"))
        r = self.run_tool("init", stdin="wrong-fake-password\n", timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local/share/forge-backup/password")))
        r = self.run_tool("init", stdin=f"{PASSWORD}\n", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_tool("run", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()


class BackupMachineFlagsTest(unittest.TestCase):
    def test_drive_settings_belong_to_one_pc(self):
        import json as _j
        schema = _j.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                           "files/system/usr/share/forge/settings.json")))
        defs = {s["key"]: s for s in schema["settings"]}
        for k in ("backup.enabled", "backup.drive_label"):
            self.assertIs(defs[k].get("machine"), True, k)
