"""Backup health: `forge-backup restore-test` restores a few random files from the newest backup into a
private temporary folder, compares them, runs `restic check` on a small part of the data, cleans up and
records the result. A real restic on temp folders where it matters, a fake one for the odd cases."""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_backup import (LABEL, PASSWORD, ROOT, TOOL, Base, find_restic)  # noqa: E402

USER_UNITS = os.path.join(ROOT, "files/system/usr/lib/systemd/user")
DAY = 86400

# A fake restic for the cases a real repository can't easily show. It records how it was called,
# and whether the password shows in its command line or environment.
FAKE = r'''#!@PY@
import json, os, sys
args = sys.argv[1:]
pwfile = os.environ.get("RESTIC_PASSWORD_FILE", "")
pw = open(pwfile).read().strip() if pwfile and os.path.exists(pwfile) else ""
rec = {"argv": args, "pwfile": pwfile,
       "env_restic": sorted(k for k in os.environ if k.startswith("RESTIC_")),
       "pw_in_cmdline": bool(pw) and pw.encode() in open("/proc/self/cmdline", "rb").read(),
       "pw_in_environ": bool(pw) and pw.encode() in open("/proc/self/environ", "rb").read()}
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(rec) + "\n")
home = os.environ["FAKE_HOME"]
repo = args[args.index("-r") + 1]
cmd = [a for a in args if not a.startswith("-") and a != repo][0]
nodes = json.load(open(os.environ["FAKE_NODES"])) if os.environ.get("FAKE_NODES") else []
if cmd == "snapshots":
    print(json.dumps([{"id": "ab12cd34" * 8, "short_id": "ab12cd34", "time": "2026-11-19T10:00:00.123456789+07:00",
                       "paths": [home], "hostname": "pc"}] if nodes else [])); sys.exit(0)
if cmd == "ls":
    print(json.dumps({"struct_type": "snapshot", "id": "ab12cd34" * 8, "paths": [home]}))
    for n in nodes:
        print(json.dumps(dict(n, struct_type="node")))
    sys.exit(0)
if cmd == "restore":
    t = args[args.index("--target") + 1]
    wrong = os.environ.get("FAKE_WRONG", "")
    for a in args:
        if a.startswith("--include="):
            rel = a[len("--include="):]
            p = t + rel
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(home + rel, "rb") as src:
                data = src.read()
            open(p, "wb").write(b"X" * len(data) if rel == wrong else data)
    if os.environ.get("FAKE_RO"):   # like a Go module cache: folders restored as 0555
        for d, dirs, _ in os.walk(t, topdown=False):
            if d != t:
                os.chmod(d, 0o555)
    if os.environ.get("FAKE_UNPLUG"):
        os.remove(os.environ["FAKE_UNPLUG"])
    sys.exit(int(os.environ.get("FAKE_RESTORE_RC", "0")))
if cmd == "check":
    if os.environ.get("FAKE_CHECK_RC", "0") != "0":
        print("Pack ID does not match, want 1234, got 5678"); sys.exit(1)
    print("no errors were found"); sys.exit(0)
sys.exit(0)
'''


def node(home, rel, path_mtime=None):
    p = home + rel
    st = os.stat(p)
    mt = path_mtime if path_mtime is not None else st.st_mtime
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mt)) + ".123456789" + time.strftime("%z")
    stamp = stamp[:-2] + ":" + stamp[-2:]
    return {"name": os.path.basename(p), "type": "file", "path": p, "size": st.st_size, "mtime": stamp}


class RestoreTestBase(Base):
    def setUp(self):
        super().setUp()
        self.runtime = os.path.join(self.t, "run-user")
        os.makedirs(self.runtime, mode=0o700)
        for rel, text in {"Documents/a.txt": "alpha\n", "Documents/b.txt": "beta\n" * 50,
                          "Music/c.bin": "c" * 3000, "Pictures/d.txt": "delta"}.items():
            p = os.path.join(self.home, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)

    def env(self, **extra):
        env = super().env(**extra)
        env.setdefault("XDG_RUNTIME_DIR", self.runtime)
        return env

    def rt(self):
        with open(os.path.join(self.home, ".local/state/forge-backup/restore-test.json")) as f:
            return json.load(f)

    def write_rt(self, **vals):
        d = os.path.join(self.home, ".local/state/forge-backup")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "restore-test.json"), "w") as f:
            json.dump(vals, f)

    def leftovers(self):
        return [n for n in os.listdir(self.runtime)] + [n for n in os.listdir(self.t) if n.startswith("forge-restore")]


@unittest.skipUnless(find_restic(), "restic is not installed (set FORGE_TEST_RESTIC to its path)")
class RealResticRestoreTest(RestoreTestBase):
    def write_fakes(self):
        super().write_fakes()
        os.remove(os.path.join(self.bin, "restic"))
        os.symlink(os.path.abspath(find_restic()), os.path.join(self.bin, "restic"))

    def backed_up(self):
        self.plug()
        r = self.run_tool("init", stdin=f"{PASSWORD}\n{PASSWORD}\n", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_tool("run", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def tree(self):
        out = {}
        for d, _, files in os.walk(self.home):
            for f in files:
                p = os.path.join(d, f)
                rel = os.path.relpath(p, self.home)
                if rel.startswith((".local/state/forge-backup/", ".cache/")):
                    continue
                with open(p, "rb") as fh:
                    out[rel] = (fh.read(), os.stat(p).st_mtime_ns)
        return out

    def test_restores_a_sample_compares_checks_and_cleans_up(self):
        self.backed_up()
        before = self.tree()
        r = self.run_tool("restore-test", timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rt = self.rt()
        self.assertEqual(rt["result"], "ok", rt)
        self.assertGreaterEqual(rt["files_restored"], 4)
        self.assertGreaterEqual(rt["files_compared"], 4)   # nothing changed since the backup
        self.assertTrue(rt["check_ok"])
        self.assertIsInstance(rt["last_success"], int)
        self.assertEqual(self.tree(), before, "the restore test changed something in the home folder")
        self.assertEqual(self.leftovers(), [], "the temporary folder was not removed")
        st = json.loads(self.run_tool("status", "--json").stdout)
        self.assertEqual(st["restore_test"]["result"], "ok")
        self.assertFalse(st["restore_test"]["overdue"])

    def test_damaged_backup_fails_and_notifies(self):
        self.backed_up()
        top = os.path.join(self.mount, "forge-backup")
        data = os.path.join(top, os.listdir(top)[0], "data")
        packs = [os.path.join(d, f) for d, _, fs in os.walk(data) for f in fs]
        self.assertTrue(packs)
        for p in packs:
            os.chmod(p, 0o600)
            with open(p, "r+b") as f:
                f.seek(os.path.getsize(p) // 2)
                f.write(b"\0broken\0" * 4)
        r = self.run_tool("restore-test", "--auto", timeout=180)
        self.assertNotEqual(r.returncode, 0)
        rt = self.rt()
        self.assertEqual(rt["result"], "failed", rt)
        self.assertNotIn(PASSWORD, json.dumps(rt) + r.stdout + r.stderr)
        self.assertTrue(any("notify-send" in c and "restore test" in c.lower() for c in self.other_calls()),
                        self.other_calls())
        self.assertEqual(self.leftovers(), [])

    def test_wrong_password(self):
        self.backed_up()
        with open(os.path.join(self.home, ".local/share/forge-backup/password"), "w") as f:
            f.write("not-the-password\n")
        r = self.run_tool("restore-test", timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.rt()["result"], "wrong_password")
        self.assertEqual(self.leftovers(), [])


class FakeResticRestoreTest(RestoreTestBase):
    def write_fakes(self):
        super().write_fakes()
        p = os.path.join(self.bin, "restic")
        with open(p, "w") as f:
            f.write(FAKE.replace("@PY@", sys.executable))
        os.chmod(p, 0o755)

    def env(self, **extra):
        env = super().env(**extra)
        env.update(FAKE_HOME=self.home, FAKE_NODES=self.nodes_file)
        return env

    def setUp(self):
        self.nodes_file = None
        super().setUp()
        self.nodes_file = os.path.join(self.t, "nodes.json")
        self.nodes(["/Documents/a.txt", "/Documents/b.txt", "/Music/c.bin", "/Pictures/d.txt"])
        self.plug()
        self.set_up_done()
        self.write_status(last_success=int(time.time()) - 3600, set_up_at=int(time.time()) - 90 * DAY)

    def nodes(self, rels, mtime=None):
        items = [node(self.home, r, mtime) for r in rels]
        items.append({"name": "Documents", "type": "dir", "path": self.home + "/Documents", "size": 0})
        items.append({"name": "link", "type": "symlink", "path": self.home + "/link", "size": 0})
        with open(self.nodes_file, "w") as f:
            json.dump(items, f)

    def restic_argv(self, cmd):
        return [c["argv"] for c in self.restic_calls() if cmd in c["argv"]]

    def test_password_only_through_the_password_file(self):
        r = self.run_tool("restore-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.restic_calls()
        self.assertTrue(calls)
        for c in calls:
            self.assertEqual(c["env_restic"], ["RESTIC_PASSWORD_FILE"])
            self.assertFalse(c["pw_in_cmdline"])
            self.assertFalse(c["pw_in_environ"])
            self.assertNotIn(PASSWORD, " ".join(c["argv"]))
        self.assertNotIn(PASSWORD, r.stdout + r.stderr + json.dumps(self.rt()))

    def test_restores_only_into_a_new_private_temp_folder(self):
        r = self.run_tool("restore-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        argv = self.restic_argv("restore")[0]
        target = argv[argv.index("--target") + 1]
        self.assertTrue(target.startswith(self.runtime + "/forge-restore-test-"), target)
        self.assertNotIn(self.home, target)
        self.assertTrue(any(a.startswith("ab12cd34") and a.endswith(":" + self.home) for a in argv), argv)
        includes = [a for a in argv if a.startswith("--include=")]
        self.assertTrue(1 <= len(includes) <= 4, includes)
        self.assertEqual(self.leftovers(), [])

    def test_unsafe_runtime_folder_falls_back_to_a_private_temp_folder(self):
        os.chmod(self.runtime, 0o777)
        r = self.run_tool("restore-test", TMPDIR=self.t)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        argv = self.restic_argv("restore")[0]
        target = argv[argv.index("--target") + 1]
        self.assertFalse(target.startswith(self.runtime), target)
        self.assertTrue(os.path.basename(target).startswith("forge-restore-test-"), target)

    def test_check_reads_only_a_small_part_of_the_data(self):
        self.run_tool("restore-test")
        argv = self.restic_argv("check")[0]
        subset = [a for a in argv if a.startswith("--read-data-subset=")]
        self.assertEqual(len(subset), 1, argv)

    def test_a_restored_file_that_differs_from_an_unchanged_original_fails(self):
        r = self.run_tool("restore-test", FAKE_WRONG="/Music/c.bin")
        self.assertNotEqual(r.returncode, 0)
        rt = self.rt()
        self.assertEqual(rt["result"], "failed")
        self.assertIn("Music/c.bin", rt["message"])

    def test_files_changed_since_the_backup_are_not_compared(self):
        self.nodes(["/Documents/a.txt", "/Music/c.bin"], mtime=1000000000)
        r = self.run_tool("restore-test", FAKE_WRONG="/Music/c.bin")
        # c.bin changed after the backup (other mtime), so its content can't be compared; size still is.
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rt = self.rt()
        self.assertEqual(rt["result"], "ok")
        self.assertEqual(rt["files_compared"], 0)
        self.assertEqual(rt["files_restored"], 2)

    def test_check_failure_fails(self):
        r = self.run_tool("restore-test", FAKE_CHECK_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.rt()["result"], "failed")
        self.assertFalse(self.rt()["check_ok"])

    def test_sample_is_small_and_bounded(self):
        big = os.path.join(self.home, "big.bin")
        with open(big, "wb") as f:
            f.truncate(200 * 1024 * 1024)   # sparse: bigger than any file the test restores
        rels = [f"/many/{i}.txt" for i in range(300)]
        for rel in rels:
            os.makedirs(os.path.dirname(self.home + rel), exist_ok=True)
            with open(self.home + rel, "w") as f:
                f.write(rel)
        self.nodes(rels + ["/big.bin"])
        r = self.run_tool("restore-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        includes = [a for a in self.restic_argv("restore")[0] if a.startswith("--include=")]
        self.assertLessEqual(len(includes), 12)
        self.assertNotIn("--include=/big.bin", includes)
        first = sorted(includes)
        self.run_tool("restore-test")
        self.assertNotEqual(first, sorted([a for a in self.restic_argv("restore")[1] if a.startswith("--include=")]),
                            "the sample is not random")

    def test_odd_paths_from_restic_are_skipped(self):
        items = [node(self.home, "/Documents/a.txt"),
                 {"type": "file", "path": "/etc/shadow", "size": 10, "mtime": "2026-01-01T00:00:00Z"},
                 {"type": "file", "path": self.home + "/../../etc/x", "size": 10, "mtime": "2026-01-01T00:00:00Z"},
                 {"type": "file", "path": self.home + "/bad\nname", "size": 10, "mtime": "2026-01-01T00:00:00Z"},
                 {"type": "file", "path": self.home + "/a[b]*.txt", "size": "10", "mtime": None}]
        with open(self.nodes_file, "w") as f:
            json.dump(items, f)
        r = self.run_tool("restore-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        includes = [a for a in self.restic_argv("restore")[0] if a.startswith("--include=")]
        self.assertEqual(includes, ["--include=/Documents/a.txt"])

    def test_no_backup_yet(self):
        with open(self.nodes_file, "w") as f:
            json.dump([], f)
        r = self.run_tool("restore-test")
        self.assertEqual(self.rt()["result"], "no_backup_yet")
        self.assertEqual(self.restic_argv("restore"), [])
        self.assertNotEqual(r.returncode, 0)

    # The drive and the schedule

    def test_missing_drive_is_handled_quietly(self):
        os.remove(os.path.join(self.bylabel, LABEL))
        self.write_rt(last_success=int(time.time()) - 31 * DAY, result="ok")   # due, not overdue
        r = self.run_tool("restore-test", "--auto")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rt()["result"], "drive_missing")
        self.assertEqual(self.restic_calls(), [])
        self.assertFalse(any("notify-send" in c for c in self.other_calls()))
        r = self.run_tool("restore-test")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(LABEL, r.stderr)

    def test_unplugged_while_testing_is_not_a_failed_test(self):
        r = self.run_tool("restore-test", "--auto", FAKE_RESTORE_RC="1",
                          FAKE_UNPLUG=os.path.join(self.bylabel, LABEL))
        self.assertEqual(self.rt()["result"], "drive_missing")
        self.assertEqual(r.returncode, 0)

    def test_overdue_after_40_days_notifies_once_a_day(self):
        os.remove(os.path.join(self.bylabel, LABEL))
        self.write_rt(last_success=int(time.time()) - 41 * DAY, result="ok")
        self.run_tool("restore-test", "--auto")
        notes = [c for c in self.other_calls() if "notify-send" in c]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("41 days", notes[0])
        self.run_tool("restore-test", "--auto")
        self.assertEqual(len([c for c in self.other_calls() if "notify-send" in c]), 1)
        st = json.loads(self.run_tool("status", "--json").stdout)
        self.assertTrue(st["restore_test"]["overdue"])

    def test_never_tested_counts_from_set_up(self):
        os.remove(os.path.join(self.bylabel, LABEL))
        st = json.loads(self.run_tool("status", "--json").stdout)
        self.assertTrue(st["restore_test"]["overdue"])   # set up 90 days ago, never tested

    def test_auto_runs_monthly_only(self):
        self.write_rt(last_success=int(time.time()) - 5 * DAY, last_run=int(time.time()) - 5 * DAY, result="ok")
        self.assertEqual(self.run_tool("restore-test", "--auto").returncode, 0)
        self.assertEqual(self.restic_calls(), [])
        self.write_rt(last_success=int(time.time()) - 31 * DAY, last_run=int(time.time()) - 31 * DAY, result="ok")
        self.assertEqual(self.run_tool("restore-test", "--auto").returncode, 0)
        self.assertTrue(self.restic_argv("restore"))

    def test_auto_retries_a_failed_test_the_next_day(self):
        self.write_rt(last_success=int(time.time()) - 60 * DAY, last_run=int(time.time()) - 3600, result="failed")
        self.run_tool("restore-test", "--auto")
        self.assertEqual(self.restic_calls(), [])
        self.write_rt(last_success=int(time.time()) - 60 * DAY, last_run=int(time.time()) - DAY - 60, result="failed")
        self.run_tool("restore-test", "--auto")
        self.assertTrue(self.restic_argv("restore"))

    def test_auto_waits_while_gaming(self):
        os.makedirs(os.path.join(self.home, ".local/state/forge"))
        with open(os.path.join(self.home, ".local/state/forge/mode"), "w") as f:
            f.write("gaming\n")
        self.assertEqual(self.run_tool("restore-test", "--auto").returncode, 0)
        self.assertEqual(self.restic_calls(), [])

    def test_auto_does_nothing_when_backups_are_not_set_up(self):
        os.remove(os.path.join(self.home, ".local/share/forge-backup/password"))
        self.assertEqual(self.run_tool("restore-test", "--auto").returncode, 0)
        self.assertEqual(self.restic_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local/state/forge-backup/restore-test.json")))

    def test_never_runs_during_a_backup(self):
        import fcntl
        d = os.path.join(self.home, ".local/state/forge-backup")
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertEqual(self.run_tool("restore-test", "--auto").returncode, 0)
            r = self.run_tool("restore-test")
            self.assertEqual(r.returncode, 75)
        finally:
            os.close(fd)
        self.assertEqual(self.restic_calls(), [])

    def test_result_file_is_private(self):
        self.run_tool("restore-test")
        p = os.path.join(self.home, ".local/state/forge-backup/restore-test.json")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)


class UnitsTest(unittest.TestCase):
    def test_timer_and_service(self):
        svc = open(os.path.join(USER_UNITS, "forge-backup-restore-test.service")).read()
        tim = open(os.path.join(USER_UNITS, "forge-backup-restore-test.timer")).read()
        self.assertIn("ExecStart=/usr/bin/forge-backup restore-test --auto", svc)
        self.assertIn("Type=oneshot", svc)
        self.assertIn("IOSchedulingClass=idle", svc)
        self.assertIn("WantedBy=timers.target", tim)
        self.assertIn("Persistent=true", tim)
        self.assertRegex(svc, r"\nTimeoutStartSec=\d+h\n")   # F5: a hung restic is stopped by systemd too
        rec = open(os.path.join(ROOT, "recipes/common-modules.yml")).read()
        self.assertRegex(rec, r"user:\s*\n\s+enabled:\s*\n(\s+- .*\n)*\s+- forge-backup-restore-test\.timer")


if __name__ == "__main__":
    unittest.main()


HANG = """#!/bin/sh
echo '{"struct_type":"snapshot"}'
exec sleep 30
"""


class ReviewFixesTest(unittest.TestCase):
    """rvGame F4, F5, F7."""

    def setUp(self):
        import tempfile
        self.t = tempfile.mkdtemp()
        os.chmod(self.t, 0o755)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for d, dirs, _ in os.walk(self.t):
            for x in dirs:
                try:
                    os.chmod(os.path.join(d, x), 0o755, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
        shutil.rmtree(self.t, True)

    def tool(self):
        from test_backup import load_tool
        return load_tool()

    def test_hung_restic_ls_is_stopped_at_the_deadline(self):
        # F5: readline blocked forever when restic stopped printing.
        bin_ = os.path.join(self.t, "bin")
        os.makedirs(bin_)
        with open(os.path.join(bin_, "restic"), "w") as f:
            f.write(HANG)
        os.chmod(os.path.join(bin_, "restic"), 0o755)
        m = self.tool()
        m.RT_LS_TIMEOUT_S = 1
        m.password_file = lambda: "/nonexistent"
        old = os.environ["PATH"]
        os.environ["PATH"] = bin_ + ":" + old
        try:
            started = time.monotonic()
            sample, failure = m.rt_sample("/x", "a" * 64, "/home/u")
        finally:
            os.environ["PATH"] = old
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(sample)
        self.assertIn("too long", failure.message)

    def test_read_only_folders_are_removed_without_following_links(self):
        # F4: as a normal user (root ignores folder permissions), restored 0555 folders stayed behind.
        uid = 65534
        top = os.path.join(self.t, "rt")
        os.makedirs(os.path.join(top, "a/b/c"))
        outside = os.path.join(self.t, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep"), "w") as f:
            f.write("k")
        os.chmod(outside, 0o555)
        with open(os.path.join(top, "a/b/c/f.txt"), "w") as f:
            f.write("x")
        os.symlink(outside, os.path.join(top, "a/b/link"))
        for d, dirs, files in os.walk(top):
            for x in dirs + files:
                os.lchown(os.path.join(d, x), uid, uid)
        os.chown(top, uid, uid)
        os.chown(outside, uid, uid)
        for d in ("a/b/c", "a/b", "a", ""):
            os.chmod(os.path.join(top, d), 0o555)
        code = ("import sys; sys.path.insert(0, %r); sys.argv=['x']\n"
                "from test_backup import load_tool\nm = load_tool()\nprint(repr(m.remove_tree(%r)))") % (
                    os.path.dirname(os.path.abspath(__file__)), top)

        def drop():
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
        os.chmod(self.t, 0o777)
        r = subprocess.run([sys.executable, "-c", code], preexec_fn=drop, capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, HOME=self.t))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "None")
        self.assertFalse(os.path.lexists(top))
        self.assertEqual(stat.S_IMODE(os.stat(outside).st_mode), 0o555, "a link was followed")
        self.assertTrue(os.path.exists(os.path.join(outside, "keep")))

    def test_folders_you_cannot_even_open_are_removed_too(self):
        # rvGame round 2 probe: folders restored as 0100 and 0000, with links to an outside folder.
        uid = 65534
        top = os.path.join(self.t, "rt2")
        outside = os.path.join(self.t, "outside2")
        os.makedirs(outside)
        open(os.path.join(outside, "keep"), "w").close()
        os.makedirs(os.path.join(top, "a/b/c"))
        open(os.path.join(top, "a/b/c/f"), "w").close()
        os.symlink(outside, os.path.join(top, "a/lnk"))
        os.symlink(outside, os.path.join(top, "a/b/c/lnk2"))
        for d, dirs, files in os.walk(top):
            for x in dirs + files:
                os.lchown(os.path.join(d, x), uid, uid)
        os.chown(top, uid, uid)
        os.chown(outside, uid, uid)
        os.chmod(outside, 0o500)
        os.chmod(os.path.join(top, "a/b/c"), 0o000)
        os.chmod(os.path.join(top, "a/b"), 0o555)
        os.chmod(os.path.join(top, "a"), 0o100)
        code = ("import sys; sys.path.insert(0, %r); sys.argv=['x']\n"
                "from test_backup import load_tool\nm = load_tool()\nprint(repr(m.remove_tree(%r)))") % (
                    os.path.dirname(os.path.abspath(__file__)), top)

        def drop():
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
        os.chmod(self.t, 0o777)
        r = subprocess.run([sys.executable, "-c", code], preexec_fn=drop, capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, HOME=self.t))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "None")
        self.assertFalse(os.path.lexists(top))
        self.assertEqual(stat.S_IMODE(os.stat(outside).st_mode), 0o500, "a link was followed")
        self.assertEqual(os.listdir(outside), ["keep"])

    def test_failed_cleanup_is_reported(self):
        m = self.tool()
        self.assertIsNotNone(m.remove_tree("/proc/self"))   # can't be removed: an error comes back

    def test_mode_file_follows_xdg_state_home(self):
        # F7: forge-game and forge-mode honour XDG_STATE_HOME, forge-backup did not.
        m = self.tool()
        state = os.path.join(self.t, "state")
        os.makedirs(os.path.join(state, "forge"))
        with open(os.path.join(state, "forge", "mode"), "w") as f:
            f.write("gaming\n")
        old = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = state
        try:
            self.assertEqual(m.forge_mode(), "gaming")
        finally:
            if old is None:
                os.environ.pop("XDG_STATE_HOME")
            else:
                os.environ["XDG_STATE_HOME"] = old


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the tool as another uid")
class ReadOnlyRestoreAsUserTest(FakeResticRestoreTest):
    """F4 end to end: the restore test runs as a normal user and the backup has read-only folders."""

    def run_tool(self, *args, timeout=60, **extra):
        uid = 65534
        for d, dirs, files in os.walk(self.t):
            for x in dirs + files:
                os.lchown(os.path.join(d, x), uid, uid)
        os.chown(self.t, uid, uid)

        def drop():
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
        return subprocess.run([sys.executable, TOOL, *args], env=self.env(**extra), capture_output=True, text=True,
                              timeout=timeout, preexec_fn=drop)

    def test_read_only_restored_folders_leave_nothing_behind(self):
        for _ in range(2):
            r = self.run_tool("restore-test", FAKE_RO="1")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.leftovers(), [])
