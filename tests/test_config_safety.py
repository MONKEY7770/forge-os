"""P0 configuration safety: snapshots before each apply, a history with reasons,
multi-step undo, and a clear note when a change only works after a restart."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPLY = os.path.join(ROOT, "files/system/usr/libexec/forge-apply")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")


class ConfigSafetyTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.conf = os.path.join(self.root, "etc/forge/settings.json")
        self.state = os.path.join(self.root, "var/lib/forge-apply")
        self.env = dict(os.environ, FORGE_ROOT=self.root, FORGE_DRYRUN="1", FORGE_CONF=self.conf,
                        PYTHONPATH=LIB, FORGE_SCHEMA=SCHEMA)
        self.env.pop("SUDO_USER", None)

    def apply(self, *args, **env):
        return subprocess.run([sys.executable, APPLY, *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def set(self, **values):
        f = os.path.join(self.root, "in.json")
        current = json.load(open(self.conf)) if os.path.exists(self.conf) else {}
        current.update({k.replace("__", "."): v for k, v in values.items()})
        with open(f, "w") as fh:
            json.dump(current, fh)
        r = self.apply("--import", f)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def saved(self, key):
        return json.load(open(self.conf)).get(key)

    def snapshots(self):
        d = os.path.join(self.state, "snapshots")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def history(self):
        p = os.path.join(self.state, "history.jsonl")
        return [json.loads(line) for line in open(p)] if os.path.exists(p) else []

    def test_each_change_is_snapshotted_and_logged_with_old_and_new(self):
        self.set(ai__keep_alive_min=7)
        self.set(ai__keep_alive_min=9)
        self.assertEqual(len(self.snapshots()), 2)
        last = self.history()[-1]
        self.assertEqual(last["changed"]["ai.keep_alive_min"], [7, 9])
        self.assertIn("ts", last)
        self.assertIn("by", last)

    def test_only_the_newest_ten_snapshots_are_kept(self):
        for i in range(12):
            self.set(ai__keep_alive_min=i + 1)
        self.assertEqual(len(self.snapshots()), 10)

    # (Values are not the default, which is not stored in the overrides file.)
    def test_undo_one_and_several_steps(self):
        # "undo N" restores the settings as they were N changes ago.
        self.set(ai__keep_alive_min=23)
        self.set(ai__keep_alive_min=24)
        self.set(ai__keep_alive_min=25)
        r = self.apply("--undo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.saved("ai.keep_alive_min"), 24)
        r = self.apply("--undo")              # an undo is a change too, so it can be undone
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.saved("ai.keep_alive_min"), 25)
        r = self.apply("--undo", "3")          # 25 -> (undo) 24 -> (undo) 25: three changes ago it was 24
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.saved("ai.keep_alive_min"), 24)
        self.assertEqual(self.history()[-1]["action"], "undo")

    def test_undo_further_than_kept_is_refused(self):
        self.set(ai__keep_alive_min=3)
        r = self.apply("--undo", "5")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.saved("ai.keep_alive_min"), 3)

    def test_history_command_lists_changes(self):
        self.set(ai__keep_alive_min=6)
        r = self.apply("--history")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ai.keep_alive_min", r.stdout)

    def test_changes_that_need_a_restart_say_so(self):
        r = self.set(security__usb_key_timeout_sec=20)
        self.assertIn("restart", r.stdout.lower())
        self.assertIn("security.usb_key_timeout_sec", r.stdout)
        self.assertIn("security.usb_key_timeout_sec", self.history()[-1]["restart"]["boot"])
        r = self.set(ai__keep_alive_min=8)
        self.assertNotIn("after a restart", r.stdout.lower())

    def test_snapshots_are_root_only(self):
        self.set(ai__keep_alive_min=6)
        d = os.path.join(self.state, "snapshots")
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        for f in self.snapshots():
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, f)).st_mode), 0o600)

    def test_a_broken_snapshot_is_refused_and_changes_nothing(self):
        self.set(ai__keep_alive_min=3)
        self.set(ai__keep_alive_min=4)
        newest = os.path.join(self.state, "snapshots", self.snapshots()[-1])
        with open(newest, "w") as f:
            json.dump({"ai.keep_alive_min": "not a number"}, f)
        r = self.apply("--undo")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.saved("ai.keep_alive_min"), 4)

    def test_forge_core_still_may_only_import(self):
        for args in (["--undo"], ["--history"]):
            r = self.apply(*args, SUDO_USER="forge")
            self.assertEqual(r.returncode, 2, args)


class ForgeConfigCommandsTest(unittest.TestCase):
    def test_undo_and_history_go_through_forge_apply(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "args")
        fake = os.path.join(tmp, "forge-apply")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\necho \"$@\" >> {log}\n")
        os.chmod(fake, 0o755)
        env = dict(os.environ, FORGE_APPLY=fake, FORGE_LIB=LIB, PYTHONPATH=LIB, FORGE_SCHEMA=SCHEMA,
                   FORGE_CONF=os.path.join(tmp, "settings.json"))
        tool = os.path.join(ROOT, "files/system/usr/bin/forge-config")
        for args in (["undo"], ["undo", "3"], ["history"]):
            r = subprocess.run([sys.executable, tool, *args], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(log).read().splitlines(), ["--undo 1", "--undo 3", "--history"])
        r = subprocess.run([sys.executable, tool, "undo", "x"], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


class ConfigSafetyRound2Test(ConfigSafetyTest):
    def test_undo_order_does_not_depend_on_the_clock(self):
        self.set(ai__keep_alive_min=12)
        # Pretend the clock was a day ahead earlier: snapshot names that sort "in the future".
        d = os.path.join(self.state, "snapshots")
        for f in os.listdir(d):
            os.rename(os.path.join(d, f), os.path.join(d, "9" * 20 + "-" + f))
        self.set(ai__keep_alive_min=13)
        self.set(ai__keep_alive_min=14)
        r = self.apply("--undo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.saved("ai.keep_alive_min"), 13)

    def test_odd_undo_and_import_arguments_fail_cleanly(self):
        for args in (["--undo", "\u00b2"], ["--undo", "-1"], ["--import"]):
            r = self.apply(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertNotIn("Traceback", r.stderr)

    def test_snapshot_that_is_a_fifo_or_link_is_refused_quickly(self):
        self.set(ai__keep_alive_min=12)
        d = os.path.join(self.state, "snapshots")
        newest = os.path.join(d, sorted(os.listdir(d))[-1])
        os.unlink(newest)
        os.mkfifo(newest)
        r = self.apply("--undo")
        self.assertEqual(r.returncode, 2)
        os.unlink(newest)
        secret = os.path.join(self.root, "secret.json")
        with open(secret, "w") as f:
            json.dump({"ai.keep_alive_min": 99}, f)
        os.symlink(secret, newest)
        r = self.apply("--undo")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.saved("ai.keep_alive_min"), 12)

    def test_huge_values_are_refused(self):
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"ai.models_dir": "/var/home/eve/" + "a" * 990_000}, fh)
        r = self.apply("--import", f)
        self.assertEqual(r.returncode, 2)

    def test_merge_changes_only_the_given_setting(self):
        self.set(ai__keep_alive_min=12)
        f = os.path.join(self.root, "part.json")
        with open(f, "w") as fh:
            json.dump({"monitor.interval_min": 30}, fh)
        r = self.apply("--import", f, "--merge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.saved("ai.keep_alive_min"), 12, "a merge must not reset other settings")
        self.assertEqual(self.saved("monitor.interval_min"), 30)


class ReviewRound4Test(ConfigSafetyTest):
    """Found by the independent reviewer in the merge and history changes."""

    def merge(self, **values):
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({k.replace("__", "."): v for k, v in values.items()}, fh)
        return self.apply("--import", f, "--merge")

    def write_conf(self, text):
        os.makedirs(os.path.dirname(self.conf), exist_ok=True)
        with open(self.conf, "w") as f:
            f.write(text)

    def test_merge_never_wipes_settings_when_one_value_is_bad(self):
        text = '{"ai.keep_alive_min": 7, "monitor.interval_min": 17, "agents.cpu_weight": 0}'
        self.write_conf(text)
        r = self.merge(devices__bluetooth=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("agents.cpu_weight", r.stderr)
        self.assertEqual(open(self.conf).read(), text)
        # Fixing the bad value itself with a merge works and keeps the others.
        r = self.merge(agents__cpu_weight=40)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.saved("ai.keep_alive_min"), 7)
        self.assertEqual(self.saved("monitor.interval_min"), 17)

    def test_merge_refuses_a_settings_file_it_cannot_read(self):
        for text in ('{"ai.keep_alive_min": 7,', '["not", "an", "object"]'):
            self.write_conf(text)
            r = self.merge(devices__bluetooth=False)
            self.assertEqual(r.returncode, 2, text)
            self.assertEqual(open(self.conf).read(), text)

    def test_snapshot_keeps_the_old_values_even_if_one_was_bad(self):
        self.write_conf('{"ai.keep_alive_min": 7, "agents.cpu_weight": 0}')
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"ai.keep_alive_min": 9}, fh)
        self.assertEqual(self.apply("--import", f).returncode, 0)
        snap = json.load(open(os.path.join(self.state, "snapshots", self.snapshots()[-1])))
        self.assertEqual(snap.get("ai.keep_alive_min"), 7)
        self.assertEqual(snap.get("agents.cpu_weight"), 0)

    def test_settings_file_stays_readable_under_a_strict_umask(self):
        # forge-apply also runs from Forge Core (UMask=0077). Desktop users must still read settings.
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"ai.keep_alive_min": 9}, fh)
        old = os.umask(0o077)
        try:
            self.assertEqual(self.apply("--import", f).returncode, 0)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.conf)).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(self.conf).st_mode), 0o644)
        # Config read by services that don't run as root (systemd-resolved, geoclue) or by users (KDE).
        for p in ("etc/systemd/resolved.conf.d/90-forge.conf", "etc/xdg/kded6rc"):
            full = os.path.join(self.root, p)
            self.assertEqual(stat.S_IMODE(os.stat(full).st_mode), 0o644, p)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(full)).st_mode), 0o755, p)
        os.chmod(self.conf, 0o600)
        self.assertEqual(self.apply("--import", f).returncode, 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.conf).st_mode), 0o644)

    def test_malformed_history_lines_do_not_crash_history(self):
        self.set(ai__keep_alive_min=7)
        with open(os.path.join(self.state, "history.jsonl"), "a") as f:
            f.write('[1, 2]\n{"changed": {"a": [1, 2, 3]}}\n{"changed": [1]}\n{"restart": [1]}\n'
                    '{"ts": "x", "action": 5}\nnot json\n')
        r = self.apply("--history")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_snapshot_numbers_cannot_overflow_the_name_format(self):
        d = os.path.join(self.state, "snapshots")
        os.makedirs(d, mode=0o700)
        with open(os.path.join(d, "9999999999.json"), "w") as f:
            f.write("{}")
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"ai.keep_alive_min": 9}, fh)
        r = self.apply("--import", f)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(all(len(n) == len("0000000000.json") for n in os.listdir(d)), os.listdir(d))


class ReviewRound5Test(unittest.TestCase):
    """Found by the independent reviewer after the round 4 fixes."""

    def setUp(self):
        ConfigSafetyTest.setUp(self)

    apply = ConfigSafetyTest.apply
    set = ConfigSafetyTest.set
    saved = ConfigSafetyTest.saved
    snapshots = ConfigSafetyTest.snapshots

    def test_the_lock_file_is_private_so_other_users_cannot_hold_it(self):
        lock = os.path.join(self.root, "run/forge-apply.lock")
        os.makedirs(os.path.dirname(lock))
        open(lock, "w").close()
        os.chmod(lock, 0o644)          # left behind by an older version
        self.set(ai__keep_alive_min=7)
        self.assertEqual(stat.S_IMODE(os.stat(lock).st_mode), 0o600)
        os.unlink(lock)
        self.set(ai__keep_alive_min=8)
        self.assertEqual(stat.S_IMODE(os.stat(lock).st_mode), 0o600)

    def test_config_files_made_private_by_an_older_version_become_readable(self):
        self.set(ai__keep_alive_min=7)
        for p in ("etc/xdg/kded6rc", "etc/systemd/user/forge-health.timer.d/90-forge.conf"):
            os.chmod(os.path.join(self.root, p), 0o600)
        self.set(ai__keep_alive_min=8)
        for p in ("etc/xdg/kded6rc", "etc/systemd/user/forge-health.timer.d/90-forge.conf"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, p)).st_mode), 0o644, p)

    def test_settings_file_is_replaced_in_one_step(self):
        # Readers (forge-virt, forge-health) must never see a half-written file.
        self.set(ai__keep_alive_min=7)
        ino = os.stat(self.conf).st_ino
        self.set(ai__keep_alive_min=8)
        self.assertNotEqual(os.stat(self.conf).st_ino, ino, "saved in place, not swapped in")
        self.assertEqual(stat.S_IMODE(os.stat(self.conf).st_mode), 0o644)
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.conf)) if f.startswith(".")], [])

    def test_unchanged_drop_ins_are_repaired_too(self):
        self.set(ai__keep_alive_min=7)
        p = os.path.join(self.root, "etc/systemd/system/forge-core.service.d/90-forge.conf")
        os.chmod(p, 0o600)
        self.set(ai__keep_alive_min=8)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o644)

    def test_leftover_temp_files_from_a_killed_save_are_cleaned_up(self):
        self.set(ai__keep_alive_min=7)
        folder = os.path.dirname(self.conf)
        stale = os.path.join(folder, ".settings.forge-0123456789ab")
        open(stale, "w").close()
        odd = os.path.join(folder, ".settings.forge-0123456789ab\n")
        open(odd, "w").close()            # not an exact match: left alone
        self.addCleanup(lambda: os.path.exists(odd) and os.unlink(odd))
        self.set(ai__keep_alive_min=8)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(odd))

    def test_a_full_disk_fails_cleanly_and_leaves_no_extra_undo_point(self):
        self.set(ai__keep_alive_min=7)
        before = self.snapshots()
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"ai.keep_alive_min": 9}, fh)
        # forge-apply's own test seam: fail the write as a full disk would.
        r = self.apply("--import", f, FORGE_TEST_SAVE_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("Not saved", r.stderr)
        self.assertEqual(self.saved("ai.keep_alive_min"), 7)
        self.assertEqual(self.snapshots(), before)

    def test_backup_copy_is_readable_like_the_settings(self):
        self.set(ai__keep_alive_min=7)
        os.chmod(self.conf, 0o600)
        self.set(ai__keep_alive_min=8)
        self.assertEqual(stat.S_IMODE(os.stat(self.conf + ".bak").st_mode), 0o644)

    def run_limited(self, fsize, *args):
        import resource

        def limit():
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        return subprocess.run([sys.executable, APPLY, *args], env=self.env, capture_output=True, text=True,
                              timeout=60, preexec_fn=limit)

    def test_a_completely_full_disk_fails_cleanly_and_undo_still_works(self):
        self.set(ai__keep_alive_min=7)
        self.set(ai__keep_alive_min=8)
        before = self.snapshots()
        bak = open(self.conf + ".bak").read()
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"ai.keep_alive_min": 9}, fh)
        r = self.run_limited(5, "--import", f)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("Not saved", r.stderr)
        self.assertEqual(self.saved("ai.keep_alive_min"), 8)
        self.assertEqual(self.snapshots(), before)
        self.assertEqual(open(self.conf + ".bak").read(), bak)     # the one-step undo point is kept
        leftovers = [n for n in os.listdir(os.path.join(self.state, "snapshots")) if not n.endswith(".json")]
        self.assertEqual(leftovers, [])
        self.assertEqual(self.apply("--undo").returncode, 0)
        self.assertEqual(self.saved("ai.keep_alive_min"), 7)

    def test_a_history_that_cannot_be_written_does_not_crash_the_save(self):
        self.set(ai__keep_alive_min=7)
        hist = os.path.join(self.state, "history.jsonl")
        os.unlink(hist)
        os.mkdir(hist)                    # writing the history now fails
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"ai.keep_alive_min": 9}, fh)
        r = self.apply("--import", f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.saved("ai.keep_alive_min"), 9)
        self.assertIn("history", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(hist + ".tmp"))

    def test_restart_note_is_shown_even_if_history_fails(self):
        self.set(ai__keep_alive_min=7)
        hist = os.path.join(self.state, "history.jsonl")
        os.unlink(hist)
        os.mkdir(hist)
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"monitor.health": False}, fh)
        r = self.apply("--import", f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("log out and in again: monitor.health", r.stdout)

    def test_leftover_snapshot_temp_files_are_cleaned_up(self):
        self.set(ai__keep_alive_min=7)
        d = os.path.join(self.state, "snapshots")
        stale = os.path.join(d, ".snap-aaaaaaaaaaaa")
        open(stale, "w").close()
        self.set(ai__keep_alive_min=8)
        self.assertFalse(os.path.exists(stale))

    def test_a_deeply_nested_history_line_does_not_crash_history(self):
        self.set(ai__keep_alive_min=7)
        with open(os.path.join(self.state, "history.jsonl"), "a") as f:
            f.write("[" * 200000 + "\n")
        r = self.apply("--history")
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertNotIn("Traceback", r.stderr)


class ForgeConfigMergeTest(unittest.TestCase):
    def test_set_sends_only_the_changed_setting(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        rec = os.path.join(tmp, "rec")
        fake = os.path.join(tmp, "forge-apply")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\necho \"$@\" >> {rec}.args\ncat \"$2\" >> {rec}.json\necho >> {rec}.json\n")
        os.chmod(fake, 0o755)
        env = dict(os.environ, FORGE_APPLY=fake, FORGE_LIB=LIB, PYTHONPATH=LIB, FORGE_SCHEMA=SCHEMA,
                   FORGE_CONF=os.path.join(tmp, "settings.json"))
        tool = os.path.join(ROOT, "files/system/usr/bin/forge-config")
        for args in (["set", "ai.keep_alive_min", "12"], ["reset", "ai.keep_alive_min"]):
            r = subprocess.run([sys.executable, tool, *args], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        sent = [json.loads(line) for line in open(rec + ".json").read().split("\n") if line.strip()]
        self.assertEqual(sent, [{"ai.keep_alive_min": 12}, {"ai.keep_alive_min": 5}])
        self.assertTrue(all(line.endswith("--merge") for line in open(rec + ".args").read().splitlines()))
        r = subprocess.run([sys.executable, tool, "undo", "\u00b2"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

if __name__ == "__main__":
    unittest.main()



