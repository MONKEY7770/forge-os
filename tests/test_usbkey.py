"""forge-usbkey against a real LUKS2 encrypted disk image (needs cryptsetup)."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-usbkey")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
REAL_CRYPTSETUP = shutil.which("cryptsetup")
HAVE = REAL_CRYPTSETUP is not None
FAST = ["--pbkdf", "pbkdf2", "--pbkdf-force-iterations", "1000"]

# A stand-in cryptsetup put first on PATH. It passes every call to the real one,
# makes new key slots fast to create, and can fail chosen calls (FAKE_FAIL, a comma
# list like "token import,luksKillSlot") or press Ctrl-C on forge-usbkey right after
# a key slot was really added (FAKE_SIGINT_AFTER_ADD=1).
WRAPPER = r'''#!{python}
import os, signal, subprocess, sys, time
args = sys.argv[1:]
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(" ".join(args) + "\n")
for rule in os.environ.get("FAKE_FAIL", "").split(","):
    words = rule.split()
    if words and all(w in args for w in words):
        print("fake failure: " + rule, file=sys.stderr)
        sys.exit(int(os.environ.get("FAKE_RC", "1")))
# FAKE_FAIL_EPOCH_READ=N (with FAKE_STATE2=file): the Nth plain luksDump (the epoch read) fails, once.
if os.environ.get("FAKE_FAIL_EPOCH_READ") and "luksDump" in args and "--dump-json-metadata" not in args:
    st = os.environ["FAKE_STATE2"]
    n = int(open(st).read()) + 1 if os.path.exists(st) else 1
    open(st, "w").write(str(n))
    if n == int(os.environ["FAKE_FAIL_EPOCH_READ"]):
        print("fake failure: epoch read", file=sys.stderr)
        sys.exit(1)
# FAKE_FAIL_STICK_CHECK=1: checks made with the stick's key copy fail (rc FAKE_RC), other checks work.
if (os.environ.get("FAKE_FAIL_STICK_CHECK") and "--test-passphrase" in args
        and any("forge-usbkey-key-" in a for a in args)):
    print("fake failure: stick check", file=sys.stderr)
    sys.exit(int(os.environ.get("FAKE_RC", "1")))
# FAKE_ADMIN_ADDS_FIRST=keyfile: another admin adds a password into the same slot just before ours.
if "luksAddKey" in args and os.environ.get("FAKE_ADMIN_ADDS_FIRST"):
    slot = args[args.index("--key-slot") + 1]
    subprocess.call([{real!r}, "luksAddKey"] + {fast!r} + ["--key-slot", slot, "--key-file",
                    os.environ["FAKE_ADMIN_PW"], args[-2], os.environ["FAKE_ADMIN_ADDS_FIRST"]])
# FAKE_STEAL_TOKEN_ID=1: another tool takes the chosen record id just before our first import.
if "import" in args and "--token-id" in args and os.environ.get("FAKE_STEAL_TOKEN_ID"):
    state = os.environ["FAKE_STEAL_TOKEN_ID"]
    if not os.path.exists(state):
        open(state, "w").write("done")
        tid = args[args.index("--token-id") + 1]
        foreign = state + ".json"
        open(foreign, "w").write('{{"type": "systemd-recovery-like", "keyslots": []}}')
        subprocess.call([{real!r}, "token", "import", "--token-id", tid, "--json-file", foreign, args[-1]])
# FAKE_BAD_DUMP_AFTER_ADD=N (with FAKE_STATE=file): the Nth metadata read after a key
# slot was added returns garbage, once.
counter, state = os.environ.get("FAKE_BAD_DUMP_AFTER_ADD"), os.environ.get("FAKE_STATE")
if counter and "luksDump" in args and os.path.exists(state):
    n = int(open(state).read()) + 1
    open(state, "w").write(str(n))
    if n == int(counter):
        os.unlink(state)
        print("not json")
        sys.exit(0)
if "luksAddKey" in args:
    args = {fast!r} + args
rc = subprocess.call([{real!r}] + args)
if "luksAddKey" in args and counter:
    open(state, "w").write("0")
if "luksAddKey" in args and os.environ.get("FAKE_SIGINT_AFTER_ADD"):
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(0.5)
# FAKE_SIGNAL_AFTER_ADD=SIGKILL (or SIGQUIT...): that signal hits forge-usbkey right after the slot is added.
if "luksAddKey" in args and os.environ.get("FAKE_SIGNAL_AFTER_ADD"):
    os.kill(os.getppid(), getattr(signal, os.environ["FAKE_SIGNAL_AFTER_ADD"]))
    time.sleep(0.5)
sys.exit(rc)
'''


@unittest.skipUnless(HAVE, "cryptsetup not installed")
class UsbKeyTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.img = os.path.join(self.t, "disk.img")
        self.pw = os.path.join(self.t, "password")
        with open(self.pw, "w") as f:
            f.write("my-disk-password")
        subprocess.run(["truncate", "-s", "32M", self.img], check=True)
        subprocess.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--pbkdf", "pbkdf2",
                        "--pbkdf-force-iterations", "1000", self.img, self.pw], check=True)
        with open(os.path.join(self.t, "crypttab"), "w") as f:
            f.write(f"luks-test {self.img} none discard\n")
        self.applied = os.path.join(self.t, "applied.json")
        apply = os.path.join(self.t, "apply")
        with open(apply, "w") as f:
            f.write(f"#!/bin/sh\ncp \"$2\" {self.applied}\n")
        os.chmod(apply, 0o755)
        self.stick_a = os.path.join(self.t, "stickA")
        self.stick_b = os.path.join(self.t, "stickB")
        os.makedirs(self.stick_a)
        os.makedirs(self.stick_b)
        with open(os.path.join(self.stick_b, "forge_key"), "w") as f:
            f.write("B" * 40)
        self.env = dict(os.environ, FORGE_LIB=LIB, FORGE_CRYPTTAB=os.path.join(self.t, "crypttab"),
                        FORGE_TEST_EXISTING_KEYFILE=self.pw, FORGE_APPLY=apply,
                        FORGE_SCHEMA=os.path.join(ROOT, "files/system/usr/share/forge/settings.json"),
                        FORGE_CONF=os.path.join(self.t, "settings.json"))
        bindir = os.path.join(self.t, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "cryptsetup"), "w") as f:
            f.write(WRAPPER.format(python=sys.executable, fast=FAST, real=REAL_CRYPTSETUP))
        os.chmod(os.path.join(bindir, "cryptsetup"), 0o755)
        self.fast_path = bindir + os.pathsep + os.environ.get("PATH", "")

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def run_tool(self, *args, stick=None, fast=True, **extra):
        env = dict(self.env, FORGE_USB_DIR=stick or self.stick_a, **extra)
        if fast:
            env["PATH"] = self.fast_path
        return subprocess.run([sys.executable, TOOL, *args], env=env, capture_output=True, text=True)

    def opens(self, keyfile):
        return subprocess.run(["cryptsetup", "open", "--test-passphrase", "--key-file", keyfile, self.img],
                              capture_output=True).returncode == 0

    def opened_slot(self, keyfile):
        r = subprocess.run(["cryptsetup", "open", "--test-passphrase", "-v", "--key-file", keyfile, self.img],
                           capture_output=True, text=True)
        m = re.search(r"Key slot (\d+) unlocked", r.stdout + r.stderr)
        return int(m.group(1)) if r.returncode == 0 and m else None

    def meta(self):
        out = subprocess.run(["cryptsetup", "luksDump", "--dump-json-metadata", self.img],
                             capture_output=True, text=True).stdout
        return json.loads(out)

    def tokens(self):
        return [t for t in self.meta()["tokens"].values() if t["type"] == "forge-usbkey"]

    def assert_no_hidden_key(self, keyfile):
        """The key either does not open the disk, or its slot has a forge-usbkey record."""
        slot = self.opened_slot(keyfile)
        if slot is not None:
            tracked = {int(s) for t in self.tokens() for s in t["keyslots"]}
            # A record written before an add that was killed half way still names the slot.
            tracked |= {int(t["pending_slot"]) for t in self.tokens() if "pending_slot" in t}
            self.assertIn(slot, tracked, f"slot {slot} opens the disk but has no forge-usbkey record")

    def add_raw_slot(self, keyfile):
        subprocess.run(["cryptsetup", "luksAddKey", *FAST, "--key-file", self.pw, self.img, keyfile],
                       check=True, capture_output=True)

    def test_create_writes_random_private_key_and_adds_it(self):
        # Runs with the real cryptsetup defaults (no fast wrapper).
        r = self.run_tool("create", "home", fast=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        key = os.path.join(self.stick_a, "forge_key")
        self.assertEqual(len(open(key).read()), 64)
        self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
        self.assertTrue(self.opens(key))
        self.assertEqual([t["name"] for t in self.tokens()], ["home"])
        self.assertTrue(json.load(open(self.applied))["security.usb_unlock"])

    def test_create_refuses_to_overwrite_existing_key(self):
        r = self.run_tool("create", "x", stick=self.stick_b)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(open(os.path.join(self.stick_b, "forge_key")).read(), "B" * 40)

    def test_multiple_sticks_each_with_own_slot(self):
        self.run_tool("create", "home")
        r = self.run_tool("add", "backup", stick=self.stick_b)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(t["name"] for t in self.tokens()), ["backup", "home"])
        self.assertTrue(self.opens(os.path.join(self.stick_a, "forge_key")))
        self.assertTrue(self.opens(os.path.join(self.stick_b, "forge_key")))
        self.assertIn("opens as backup", self.run_tool("test", stick=self.stick_b).stdout)
        listing = self.run_tool("list").stdout
        self.assertIn("home", listing)
        self.assertIn("backup", listing)

    def test_same_stick_twice_adds_nothing(self):
        self.run_tool("add", "backup", stick=self.stick_b)
        self.run_tool("add", "backup2", stick=self.stick_b)
        self.assertEqual(len(self.tokens()), 1)

    def test_bad_and_duplicate_names_refused(self):
        self.run_tool("create", "home")
        self.assertNotEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        self.assertNotEqual(self.run_tool("add", "bad/name", stick=self.stick_b).returncode, 0)
        self.assertNotEqual(self.run_tool("add", stick=self.stick_b).returncode, 0)

    def test_revoke_one_lost_stick_keeps_the_others_and_password(self):
        self.run_tool("create", "home")
        self.run_tool("add", "travel", stick=self.stick_b)
        r = self.run_tool("revoke", "travel", stick=os.path.join(self.t, "no-stick"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.opens(os.path.join(self.stick_b, "forge_key")))
        self.assertTrue(self.opens(os.path.join(self.stick_a, "forge_key")))
        self.assertTrue(self.opens(self.pw))
        self.assertEqual([t["name"] for t in self.tokens()], ["home"])
        self.assertNotEqual(self.run_tool("revoke", "nope").returncode, 0)

    def test_remove_all_turns_off_and_password_still_works(self):
        self.run_tool("create", "home")
        self.run_tool("add", "backup", stick=self.stick_b)
        r = self.run_tool("remove")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tokens(), [])
        self.assertTrue(self.opens(self.pw))
        self.assertFalse(json.load(open(self.applied))["security.usb_unlock"])

    def test_password_slot_is_never_revoked(self):
        self.run_tool("create", "home")
        # Remove the password slot by other means: the last USB key must then be refused.
        subprocess.run(["cryptsetup", "luksKillSlot", "--key-file", os.path.join(self.stick_a, "forge_key"),
                        self.img, "0"], check=True)
        r = self.run_tool("revoke", "home")
        self.assertNotEqual(r.returncode, 0)

    # R1: a working key slot must never be left without a forge-usbkey record.

    def test_r1_failed_name_save_and_failed_cleanup_leave_no_hidden_key(self):
        # Saving the name fails, and the old clean-up (which needs the password) fails too.
        r = self.run_tool("add", "backup", stick=self.stick_b, FAKE_FAIL="token import,luksKillSlot")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.opens(os.path.join(self.stick_b, "forge_key")), r.stderr)
        self.assertEqual(self.tokens(), [])
        self.assertFalse(os.path.exists(self.applied))

    def test_r1_ctrl_c_right_after_key_added_leaves_no_hidden_key(self):
        r = self.run_tool("add", "backup", stick=self.stick_b, FAKE_SIGINT_AFTER_ADD="1")
        self.assert_no_hidden_key(os.path.join(self.stick_b, "forge_key"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([t["name"] for t in self.tokens()], ["backup"])

    def test_r1_cleanup_that_cannot_be_verified_is_loud_then_audit_and_adopt_fix_it(self):
        # The final record can't be written and the undo fails too.
        # (Failing every "token import" now stops the add before any key slot exists.)
        r = self.run_tool("add", "backup", stick=self.stick_b,
                          FAKE_FAIL="token import --token-replace,luksKillSlot,luksRemoveKey")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("slot 1", r.stderr)
        self.assertIn("STILL", r.stderr)
        self.assert_no_hidden_key(os.path.join(self.stick_b, "forge_key"))
        a = self.run_tool("audit", stick=self.stick_b)
        self.assertNotEqual(a.returncode, 0, a.stdout)
        self.assertRegex(a.stdout, r"slot 1: UNFINISHED")
        r = self.run_tool("adopt", "backup", stick=self.stick_b)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(t["name"], t["keyslots"]) for t in self.tokens()], [("backup", ["1"])])
        self.assertTrue(json.load(open(self.applied))["security.usb_unlock"])
        self.assertEqual(self.run_tool("audit", stick=self.stick_b).returncode, 0)

    def test_r1_untracked_key_is_not_reported_as_already_added(self):
        self.add_raw_slot(os.path.join(self.stick_b, "forge_key"))
        r = self.run_tool("add", "backup", stick=self.stick_b)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("already added", r.stdout)
        self.assertIn("adopt", r.stderr)
        self.assertFalse(os.path.exists(self.applied))
        self.assertEqual(self.tokens(), [])

    def test_r1_audit_names_every_slot(self):
        self.run_tool("create", "home")
        a = self.run_tool("audit")
        self.assertEqual(a.returncode, 0, a.stdout + a.stderr)
        self.assertIn("slot 0: other (password or unknown)", a.stdout)
        self.assertIn("slot 1: USB key 'home'", a.stdout)
        self.assertEqual(self.run_tool("audit", stick=os.path.join(self.t, "no-stick")).returncode, 0)

    def test_r1_list_warns_about_record_whose_slot_is_gone(self):
        self.run_tool("create", "home")
        subprocess.run(["cryptsetup", "luksKillSlot", "--key-file", self.pw, self.img, "1"], check=True)
        out = self.run_tool("list").stdout
        self.assertIn("home", out)
        self.assertIn("no longer exists", out)

    # R15: a failed record removal must be reported and be cleanable.

    def test_r15_failed_record_removal_is_reported_and_cleanup_removes_it(self):
        self.run_tool("create", "home")
        self.run_tool("add", "travel", stick=self.stick_b)
        r = self.run_tool("revoke", "travel", FAKE_FAIL="token remove")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cleanup", r.stderr)
        self.assertFalse(self.opens(os.path.join(self.stick_b, "forge_key")))
        self.assertIn("travel", self.run_tool("list").stdout)
        c = self.run_tool("cleanup")
        self.assertEqual(c.returncode, 0, c.stderr)
        self.assertEqual([t["name"] for t in self.tokens()], ["home"])
        self.assertNotIn("no longer exists", self.run_tool("list").stdout)

    # The last working unlock method can never be removed.

    def test_remove_refuses_when_only_usb_keys_are_left(self):
        key_a = os.path.join(self.stick_a, "forge_key")
        self.run_tool("create", "home")
        self.run_tool("add", "backup", stick=self.stick_b)
        subprocess.run(["cryptsetup", "luksKillSlot", "--key-file", key_a, self.img, "0"], check=True)
        r = self.run_tool("remove", FORGE_TEST_EXISTING_KEYFILE=key_a)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.opens(key_a))
        self.assertTrue(self.opens(os.path.join(self.stick_b, "forge_key")))
        self.assertEqual(sorted(t["name"] for t in self.tokens()), ["backup", "home"])

    def test_revoke_allowed_when_another_key_still_works(self):
        key_a = os.path.join(self.stick_a, "forge_key")
        self.run_tool("create", "home")
        self.run_tool("add", "backup", stick=self.stick_b)
        subprocess.run(["cryptsetup", "luksKillSlot", "--key-file", key_a, self.img, "0"], check=True)
        r = self.run_tool("revoke", "backup", FORGE_TEST_EXISTING_KEYFILE=key_a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.opens(key_a))
        self.assertNotEqual(self.run_tool("revoke", "home", FORGE_TEST_EXISTING_KEYFILE=key_a).returncode, 0)
        self.assertTrue(self.opens(key_a))

    # Wrong passwords change nothing.

    def test_wrong_password_on_add_changes_nothing(self):
        wrong = os.path.join(self.t, "wrong")
        with open(wrong, "w") as f:
            f.write("not-the-password")
        r = self.run_tool("add", "backup", stick=self.stick_b, FORGE_TEST_EXISTING_KEYFILE=wrong)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(list(self.meta()["keyslots"]), ["0"])
        self.assertEqual(self.tokens(), [])
        self.assertFalse(os.path.exists(self.applied))

    def test_wrong_password_on_revoke_changes_nothing(self):
        self.run_tool("add", "travel", stick=self.stick_b)
        wrong = os.path.join(self.t, "wrong")
        with open(wrong, "w") as f:
            f.write("not-the-password")
        r = self.run_tool("revoke", "travel", FORGE_TEST_EXISTING_KEYFILE=wrong)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.opens(os.path.join(self.stick_b, "forge_key")))
        self.assertEqual([t["name"] for t in self.tokens()], ["travel"])

    # Duplicates.

    def test_same_key_on_another_stick_is_already_added(self):
        self.run_tool("add", "backup", stick=self.stick_b)
        copy = os.path.join(self.t, "stickC")
        os.makedirs(copy)
        shutil.copy(os.path.join(self.stick_b, "forge_key"), copy)
        # A copy can't get a second name: revoking one name would silently disable both sticks.
        r = self.run_tool("add", "other", stick=copy)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("copy of the USB key 'backup'", r.stderr)
        self.assertEqual([t["name"] for t in self.tokens()], ["backup"])
        self.assertEqual(len(self.meta()["keyslots"]), 2)

    def test_same_name_twice_refused_even_with_another_stick(self):
        self.run_tool("add", "home", stick=self.stick_b)
        with open(os.path.join(self.stick_a, "forge_key"), "w") as f:
            f.write("A" * 40)
        self.assertNotEqual(self.run_tool("add", "HOME").returncode, 0)
        self.assertFalse(self.opens(os.path.join(self.stick_a, "forge_key")))

    # R6: the drive name from settings is validated before use.

    def test_r6_invalid_label_in_settings_is_refused(self):
        with open(self.env["FORGE_CONF"], "w") as f:
            json.dump({"security.usb_key_label": "BAD LABEL!"}, f)
        r = self.run_tool("test", stick=self.stick_b)
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_invalid_label_does_not_block_commands_that_need_no_stick(self):
        self.run_tool("create", "home")
        self.run_tool("add", "travel", stick=self.stick_b)
        with open(self.env["FORGE_CONF"], "w") as f:
            json.dump({"security.usb_key_label": "BAD LABEL!"}, f)
        no_stick = os.path.join(self.t, "no-stick")
        # Revoke a lost stick.
        r = self.run_tool("revoke", "travel", stick=no_stick)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.opens(os.path.join(self.stick_b, "forge_key")))
        self.assertEqual([t["name"] for t in self.tokens()], ["home"])
        for cmd in ("list", "status", "cleanup"):
            r = self.run_tool(cmd, stick=no_stick)
            self.assertEqual(r.returncode, 0, cmd + ": " + r.stderr)
        self.assertIn("home", self.run_tool("list", stick=no_stick).stdout)
        a = self.run_tool("audit", stick=no_stick)
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertIn("slot 1: USB key 'home'", a.stdout)
        # Commands that need the stick still refuse.
        self.assertNotEqual(self.run_tool("add", "other", stick=self.stick_b).returncode, 0)
        self.assertNotEqual(self.run_tool("adopt", "other", stick=self.stick_a).returncode, 0)
        # The last working unlock method is still protected.
        key_a = os.path.join(self.stick_a, "forge_key")
        subprocess.run(["cryptsetup", "luksKillSlot", "--key-file", key_a, self.img, "0"], check=True)
        self.assertNotEqual(self.run_tool("remove", stick=no_stick, FORGE_TEST_EXISTING_KEYFILE=key_a).returncode, 0)
        self.assertNotEqual(self.run_tool("revoke", "home", stick=no_stick,
                                          FORGE_TEST_EXISTING_KEYFILE=key_a).returncode, 0)
        self.assertTrue(self.opens(key_a))

    # Second review round (independent reviewer found these gaps in the first R1 fix).

    def nostick(self):
        return os.path.join(self.t, "no-stick-plugged-in")

    def hard_interrupted_add(self, sig):
        key = os.path.join(self.stick_b, "forge_key")
        self.run_tool("add", "home", stick=self.stick_b, FAKE_SIGNAL_AFTER_ADD=sig)
        return key

    def test_r1_sigkill_after_key_added_is_listed_and_revocable_without_the_stick(self):
        key = self.hard_interrupted_add("SIGKILL")
        self.assertTrue(self.opens(key), "setup: the kill must come after the slot was added")
        self.assert_no_hidden_key(key)
        r = self.run_tool("list", stick=self.nostick())
        self.assertIn("home", r.stdout)
        self.assertRegex(r.stdout, "(?i)unfinished")
        r = self.run_tool("audit", stick=self.nostick())
        self.assertNotEqual(r.returncode, 0, "audit must not report all clear: " + r.stdout)
        # The stick is lost: revoke must still find and disable it.
        r = self.run_tool("revoke", "home", stick=self.nostick())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.opens(key))
        self.assertTrue(self.opens(self.pw))
        self.assertEqual(self.tokens(), [])

    def test_r1_sigkill_then_adopt_with_the_stick_finishes_the_add(self):
        key = self.hard_interrupted_add("SIGKILL")
        r = self.run_tool("add", "other", stick=self.stick_b)
        self.assertNotEqual(r.returncode, 0, "an unfinished add must be resolved first")
        r = self.run_tool("adopt", "home", stick=self.stick_b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [tok] = self.tokens()
        self.assertEqual((tok["name"], tok["keyslots"]), ("home", [str(self.opened_slot(key))]))
        self.assertNotIn("pending_slot", tok)
        self.assertEqual(self.run_tool("audit", stick=self.stick_b).returncode, 0)

    def test_r1_sigkill_before_any_slot_was_added_leaves_a_harmless_leftover(self):
        # Simulate: the record was written, then the tool died before the key slot was added.
        rec = os.path.join(self.t, "pending.json")
        with open(rec, "w") as f:
            json.dump({"type": "forge-usbkey", "keyslots": [], "name": "home", "pending_slot": "1"}, f)
        subprocess.run(["cryptsetup", "token", "import", "--json-file", rec, self.img], check=True)
        r = self.run_tool("list", stick=self.nostick())
        self.assertIn("leftover", r.stdout)
        self.assertEqual(self.run_tool("cleanup", stick=self.nostick()).returncode, 0)
        self.assertEqual(self.tokens(), [])
        self.assertTrue(self.opens(self.pw))

    def test_r1_sigquit_cannot_stop_the_add_half_way(self):
        key = self.hard_interrupted_add("SIGQUIT")
        self.assert_no_hidden_key(key)
        [tok] = self.tokens()
        self.assertNotIn("pending_slot", tok, "Ctrl-\\ must not leave the add unfinished")

    def test_r1_failed_slot_check_is_not_reported_as_nothing_changed(self):
        key = os.path.join(self.stick_b, "forge_key")
        r = self.run_tool("add", "home", stick=self.stick_b, FAKE_FAIL="open --test-passphrase --key-slot")
        self.assertNotIn("Nothing was changed", r.stdout + r.stderr)
        self.assert_no_hidden_key(key)

    def test_r1_unexpected_error_while_recording_leaves_no_hidden_key_and_no_traceback(self):
        key = os.path.join(self.stick_b, "forge_key")
        for n in ("1", "2", "3"):
            with self.subTest(bad_read=n):
                r = self.run_tool("add", "home", stick=self.stick_b, FAKE_BAD_DUMP_AFTER_ADD=n,
                                  FAKE_STATE=os.path.join(self.t, "state"))
                self.assertNotIn("Traceback", r.stderr)
                self.assert_no_hidden_key(key)
                self.run_tool("revoke", "home", stick=self.nostick())
                self.run_tool("cleanup", stick=self.nostick())

    def test_stick_holding_the_disk_password_is_never_adopted(self):
        shutil.copy(self.pw, os.path.join(self.stick_b, "forge_key"))
        with open(os.path.join(self.stick_b, "forge_key"), "a") as f:
            f.write("")
        # forge_key needs 16+ characters: use a long password for this test.
        with open(self.pw, "w") as f:
            f.write("a-long-disk-password-1234")
        subprocess.run(["cryptsetup", "luksAddKey", *FAST, "--key-file",
                        os.path.join(self.stick_b, "forge_key"), self.img, self.pw], check=True)
        subprocess.run(["cryptsetup", "luksKillSlot", "--key-file", self.pw, self.img, "0"], check=True)
        shutil.copy(self.pw, os.path.join(self.stick_b, "forge_key"))
        for cmd in ("add", "adopt"):
            r = self.run_tool(cmd, "home", stick=self.stick_b)
            self.assertNotEqual(r.returncode, 0, cmd)
            self.assertIn("password", (r.stdout + r.stderr).lower())
        self.assertEqual(self.tokens(), [])
        self.assertTrue(self.opens(self.pw))

    def test_copy_of_a_registered_stick_under_a_new_name_is_an_error(self):
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        shutil.copy(os.path.join(self.stick_b, "forge_key"), os.path.join(self.stick_a, "forge_key"))
        r = self.run_tool("add", "backup", stick=self.stick_a)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("copy", r.stdout + r.stderr)
        self.assertEqual([t["name"] for t in self.tokens()], ["home"])

    def test_two_forge_usbkey_runs_cannot_overlap(self):
        import fcntl
        lock = os.path.join(self.t, "crypttab.lock")
        with open(lock, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            r = self.run_tool("add", "home", stick=self.stick_b, FORGE_USBKEY_LOCK_WAIT="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Another forge-usbkey", r.stdout + r.stderr)
        self.assertEqual(self.tokens(), [])

    def test_other_crypttab_lines_do_not_block_revoking_a_lost_stick(self):
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        with open(os.path.join(self.t, "crypttab"), "w") as f:
            f.write("luks-backup UUID=00000000-dead-beef-0000-000000000000 none nofail\n")
            f.write("swap /dev/null /dev/urandom swap\n")
            f.write(f"luks-test {self.img} none discard\n")
        r = self.run_tool("revoke", "home", stick=self.nostick())
        # The connected disk is revoked; the missing backup disk is named, and the run is not
        # reported as a full success (the stick may still open that disk).
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("luks-backup", r.stdout + r.stderr, "skipped volumes must be named")
        self.assertFalse(self.opens(os.path.join(self.stick_b, "forge_key")))

    # Third review round: findings in the pending-record design.

    def import_token(self, tok):
        path = os.path.join(self.t, "tok.json")
        with open(path, "w") as f:
            json.dump(tok, f)
        subprocess.run(["cryptsetup", "token", "import", "--json-file", path, self.img], check=True,
                       capture_output=True)

    def keyfile(self, text):
        path = os.path.join(self.t, "kf-" + str(abs(hash(text))))
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_stale_record_never_claims_a_password_added_later(self):
        # A pending record for slot 1 whose key was never added (the tool died first)...
        self.import_token({"type": "forge-usbkey", "keyslots": [], "name": "home", "pending_slot": "1",
                           "epoch_after_add": "999"})
        # ...then a second password is added the normal way, and lands in slot 1.
        pw2 = self.keyfile("second-password-xyz")
        self.add_raw_slot(pw2)
        self.assertEqual(self.opened_slot(pw2), 1)
        for cmd in (["revoke", "home"], ["remove"]):
            self.run_tool(*cmd, stick=self.nostick())
            self.assertEqual(self.opened_slot(pw2), 1, f"{cmd} removed a password")
            self.assertTrue(self.opens(self.pw))

    def test_stale_record_right_before_a_manual_add_is_still_not_revoked(self):
        # Worst case: the counter even matches, because the manual add was the very next write.
        meta_epoch = int(re.search(r"Epoch:\s*(\d+)", subprocess.run(
            ["cryptsetup", "luksDump", self.img], capture_output=True, text=True).stdout).group(1))
        self.import_token({"type": "forge-usbkey", "keyslots": [], "name": "home", "pending_slot": "1",
                           "epoch_after_add": str(meta_epoch + 2)})
        pw2 = self.keyfile("second-password-xyz")
        self.add_raw_slot(pw2)
        # Revoke is authorised with that second password: it opens slot 1, so slot 1 is a password.
        r = self.run_tool("revoke", "home", stick=self.nostick(), FORGE_TEST_EXISTING_KEYFILE=pw2)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.opened_slot(pw2), 1)

    def test_record_pointing_at_the_password_slot_is_never_revoked(self):
        self.assertEqual(self.run_tool("add", "travel", stick=self.stick_b).returncode, 0)
        self.import_token({"type": "forge-usbkey", "keyslots": ["0"], "name": "bogus"})
        r = self.run_tool("revoke", "bogus", stick=self.nostick())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("password", r.stderr)
        self.assertTrue(self.opens(self.pw))

    def test_password_stick_refused_even_when_other_slots_belong_to_other_tokens(self):
        tpm = self.keyfile("tpm-secret-blob-xxxxxxxx")
        self.add_raw_slot(tpm)
        self.import_token({"type": "other-tpm", "keyslots": ["1"]})
        shutil.copy(self.pw, os.path.join(self.stick_b, "forge_key"))
        with open(self.pw, "a"):
            pass
        # forge_key needs 16+ characters and the password is 16: fine.
        for cmd in ("add", "adopt"):
            r = self.run_tool(cmd, "home", stick=self.stick_b, FORGE_TEST_EXISTING_KEYFILE=tpm)
            self.assertNotEqual(r.returncode, 0, cmd)
        self.assertEqual(self.tokens(), [])
        self.assertTrue(self.opens(self.pw))

    def test_adopt_matches_unfinished_adds_by_stick_and_name(self):
        key = self.hard_interrupted_add("SIGKILL")          # unfinished 'home' on slot 1
        other = self.keyfile("C" * 40)
        self.add_raw_slot(other)                             # an unnamed slot 2 from some other stick
        os.makedirs(os.path.join(self.t, "stickC"))
        shutil.copy(other, os.path.join(self.t, "stickC", "forge_key"))
        r = self.run_tool("adopt", "home", stick=os.path.join(self.t, "stickC"))
        self.assertNotEqual(r.returncode, 0, "a different stick must not take the unfinished name")
        r = self.run_tool("adopt", "work", stick=self.stick_b)
        self.assertNotEqual(r.returncode, 0, "the unfinished stick keeps its own name")
        self.assertIn("adopt home", r.stderr)
        self.assertEqual(sorted(t["name"] for t in self.tokens()), ["home"])
        self.assertEqual(self.run_tool("adopt", "home", stick=self.stick_b).returncode, 0)
        self.assert_no_hidden_key(key)

    def test_adopting_a_copied_stick_under_a_new_name_is_an_error(self):
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        shutil.copy(os.path.join(self.stick_b, "forge_key"), os.path.join(self.stick_a, "forge_key"))
        r = self.run_tool("adopt", "copy", stick=self.stick_a)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Done", r.stdout)

    def test_odd_record_values_do_not_crash_list_or_block_revoke(self):
        self.assertEqual(self.run_tool("add", "travel", stick=self.stick_b).returncode, 0)
        self.import_token({"type": "forge-usbkey", "keyslots": [], "name": "odd", "pending_slot": "\u00b2"})
        for cmd in (["list"], ["revoke", "travel"]):
            r = self.run_tool(*cmd, stick=self.nostick())
            self.assertNotIn("Traceback", r.stderr)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.opens(os.path.join(self.stick_b, "forge_key")))

    def test_same_disk_listed_twice_gets_one_key(self):
        link = os.path.join(self.t, "disk-link.img")
        os.symlink(self.img, link)
        with open(os.path.join(self.t, "crypttab"), "a") as f:
            f.write(f"luks-again {link} none\n")
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        self.assertEqual(len(self.tokens()), 1)
        self.assertEqual(len(self.meta()["keyslots"]), 2)

    def test_symlink_at_the_lock_path_fails_cleanly(self):
        os.symlink("/etc/hostname", os.path.join(self.t, "crypttab.lock"))
        r = self.run_tool("list", stick=self.nostick())
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)

    def test_leftover_key_copy_from_a_killed_run_is_wiped(self):
        tmp = "/run" if os.geteuid() == 0 else tempfile.gettempdir()

        def dead_copies():
            # Copies whose run is gone. (Other runs, for example parallel tests, may have live ones.)
            out = []
            for d in glob.glob(os.path.join(tmp, "forge-usbkey-key-*")):
                m = re.match(r"forge-usbkey-key-(\d+)-", os.path.basename(d))
                try:
                    if m:
                        os.kill(int(m.group(1)), 0)
                        continue
                except ProcessLookupError:
                    pass
                out.append(d)
            return out

        self.hard_interrupted_add("SIGKILL")
        self.assertTrue(dead_copies(), "setup: the killed run should leave its key copy behind")
        self.run_tool("revoke", "home", stick=self.nostick())
        self.assertEqual(dead_copies(), [])

    # Fourth review round.

    def stick_with(self, folder, text):
        path = os.path.join(self.t, folder)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "forge_key"), "w") as f:
            f.write(text)
        return path

    def test_round4_b_own_writes_do_not_make_a_lost_stick_unrevocable(self):
        self.assertEqual(self.run_tool("add", "x", stick=self.stick_b).returncode, 0)
        home = self.stick_with("stickH", "H" * 40)
        self.run_tool("add", "home", stick=home, FAKE_SIGNAL_AFTER_ADD="SIGKILL")
        self.assertEqual(self.run_tool("revoke", "x", stick=self.nostick()).returncode, 0)
        r = self.run_tool("revoke", "home", stick=self.nostick())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.opens(os.path.join(home, "forge_key")))
        self.assertTrue(self.opens(self.pw))

    def test_round4_c_unfinished_slot_does_not_let_the_password_be_adopted(self):
        self.hard_interrupted_add("SIGKILL")
        pwstick = self.stick_with("stickP", open(self.pw).read())
        r = self.run_tool("adopt", "foo", stick=pwstick)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("foo", [t["name"] for t in self.tokens()])

    def test_round4_a_failed_add_never_leaves_a_record_on_someone_elses_slot(self):
        pw2 = self.keyfile("second-password-xyz")
        r = self.run_tool("add", "home", stick=self.stick_b, FAKE_ADMIN_ADDS_FIRST=pw2, FAKE_ADMIN_PW=self.pw)
        self.assertNotEqual(r.returncode, 0)
        slot2 = self.opened_slot(pw2)
        self.assertIsNotNone(slot2)
        self.run_tool("revoke", "home", stick=self.nostick())
        self.assertEqual(self.opened_slot(pw2), slot2, "the other admin's password was removed")

    def test_round4_d_a_failed_check_is_never_read_as_no(self):
        self.assertEqual(self.run_tool("add", "travel", stick=self.stick_b).returncode, 0)
        self.import_token({"type": "forge-usbkey", "keyslots": ["0"], "name": "bogus"})
        log = os.path.join(self.t, "calls.log")
        r = self.run_tool("revoke", "bogus", stick=self.nostick(), FAKE_FAIL="open --test-passphrase --key-slot",
                          FAKE_RC="3", FAKE_LOG=log)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("luksKillSlot", open(log).read(), "must stop before trying to remove anything")
        self.assertTrue(self.opens(self.pw))

    def test_round4_e_a_failed_check_does_not_add_the_same_stick_twice(self):
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        r = self.run_tool("add", "travel", stick=self.stick_b, FAKE_FAIL_STICK_CHECK="1", FAKE_RC="3")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([t["name"] for t in self.tokens()], ["home"])
        self.assertEqual(len(self.meta()["keyslots"]), 2)

    def test_round4_f_a_failed_record_write_never_deletes_another_tools_record(self):
        r = self.run_tool("add", "home", stick=self.stick_b, FAKE_STEAL_TOKEN_ID=os.path.join(self.t, "steal"))
        types = [t["type"] for t in self.meta()["tokens"].values()]
        self.assertIn("systemd-recovery-like", types)
        self.assert_no_hidden_key(os.path.join(self.stick_b, "forge_key"))

    def test_round4_g_sweep_never_wipes_a_running_tools_key_copy(self):
        tmp = "/run" if os.geteuid() == 0 else tempfile.gettempdir()
        live = tempfile.mkdtemp(prefix=f"forge-usbkey-key-{os.getpid()}-", dir=tmp)
        with open(os.path.join(live, "key"), "w") as f:
            f.write("still in use")
        try:
            self.run_tool("cleanup", stick=self.nostick())
            self.assertTrue(os.path.exists(os.path.join(live, "key")))
        finally:
            shutil.rmtree(live, ignore_errors=True)

    # Fifth review round.

    def test_round5_n1_failed_read_after_add_keeps_a_revocable_record(self):
        key = os.path.join(self.stick_b, "forge_key")
        self.run_tool("add", "home", stick=self.stick_b, FAKE_BAD_DUMP_AFTER_ADD="1",
                      FAKE_STATE=os.path.join(self.t, "state"))
        self.assertTrue(self.opens(key), "setup: the key was added")
        r = self.run_tool("revoke", "home", stick=self.nostick())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.opens(key))

    def test_round5_n2_error_before_the_add_leaves_nothing_that_can_claim_a_password(self):
        r = self.run_tool("add", "home", stick=self.stick_b, FAKE_FAIL_EPOCH_READ="2",
                          FAKE_STATE2=os.path.join(self.t, "state2"))
        self.assertNotEqual(r.returncode, 0)
        pw2 = self.keyfile("second-password-xyz")
        self.add_raw_slot(pw2)
        slot2 = self.opened_slot(pw2)
        self.run_tool("cleanup", stick=self.nostick())
        self.run_tool("revoke", "home", stick=self.nostick())
        self.assertEqual(self.opened_slot(pw2), slot2, "password #2 was removed")

    def test_round5_n3_contradictory_check_keeps_the_record(self):
        self.run_tool("add", "home", stick=self.stick_b, FAKE_FAIL_STICK_CHECK="1", FAKE_RC="2")
        self.assert_no_hidden_key(os.path.join(self.stick_b, "forge_key"))
        self.assertIn("home", self.run_tool("list", stick=self.nostick()).stdout)

    def test_round5_n4_one_name_never_covers_two_different_sticks(self):
        img2 = os.path.join(self.t, "data.img")
        subprocess.run(["truncate", "-s", "32M", img2], check=True)
        subprocess.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", *FAST, img2, self.pw],
                       check=True)
        home = self.stick_with("stickH", "H" * 40)
        self.assertEqual(self.run_tool("add", "home", stick=home).returncode, 0)   # root volume only
        with open(os.path.join(self.t, "crypttab"), "a") as f:
            f.write(f"luks-data {img2} none\n")
        subprocess.run(["cryptsetup", "luksAddKey", *FAST, "--key-file", self.pw, img2,
                        os.path.join(self.stick_b, "forge_key")], check=True, capture_output=True)
        r = self.run_tool("adopt", "home", stick=self.stick_b)
        self.assertNotEqual(r.returncode, 0, r.stdout)

    # Sixth review round: more than one encrypted disk.

    def second_disk(self, name="data"):
        img = os.path.join(self.t, name + ".img")
        subprocess.run(["truncate", "-s", "32M", img], check=True)
        subprocess.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", *FAST, img, self.pw],
                       check=True)
        return img

    def crypttab(self, *lines):
        with open(os.path.join(self.t, "crypttab"), "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def test_round6_new1_revoke_is_not_reported_done_while_a_disk_is_missing(self):
        data = self.second_disk()
        self.crypttab(f"luks-test {self.img} none", f"luks-data {data} none")
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        moved = data + ".unplugged"
        os.rename(data, moved)
        for cmd in (["revoke", "home"], ["remove"]):
            r = self.run_tool(*cmd, stick=self.nostick())
            self.assertNotEqual(r.returncode, 0, f"{cmd} claimed success with luks-data missing")
            self.assertIn("luks-data", r.stdout + r.stderr)
        self.assertNotEqual(json.load(open(self.applied)).get("security.usb_unlock"), False,
                            "USB unlock was turned off although a disk still has the key")

    def test_round6_encrypted_swap_line_does_not_block_anything(self):
        self.crypttab("swap /dev/null /dev/urandom swap", f"luks-test {self.img} none")
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        r = self.run_tool("revoke", "home", stick=self.nostick())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_round6_new2_no_add_or_adopt_while_a_disk_is_missing(self):
        data = self.second_disk()
        self.crypttab(f"luks-test {self.img} none", f"luks-data {data} none")
        os.rename(data, data + ".unplugged")
        r = self.run_tool("add", "home", stick=self.stick_b)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.tokens(), [])

    def test_round6_new3_adopt_checks_every_disk_before_writing(self):
        data = self.second_disk()
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)   # root only
        subprocess.run(["cryptsetup", "luksAddKey", *FAST, "--key-file", self.pw, data,
                        os.path.join(self.stick_b, "forge_key")], check=True, capture_output=True)
        self.crypttab(f"luks-data {data} none", f"luks-test {self.img} none")
        r = self.run_tool("adopt", "travel", stick=self.stick_b)
        self.assertNotEqual(r.returncode, 0)
        meta = json.loads(subprocess.run(["cryptsetup", "luksDump", "--dump-json-metadata", data],
                                         capture_output=True, text=True).stdout)
        self.assertEqual([t for t in meta["tokens"].values() if t["type"] == "forge-usbkey"], [],
                         "adopt wrote to luks-data before refusing on luks-test")

    def test_round6_new4_an_unclear_record_does_not_block_revoking_clear_ones(self):
        data = self.second_disk()
        self.crypttab(f"luks-data {data} none", f"luks-test {self.img} none")
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        # Make luks-data's record unclear: point it at a pending slot with a stale counter.
        meta = json.loads(subprocess.run(["cryptsetup", "luksDump", "--dump-json-metadata", data],
                                         capture_output=True, text=True).stdout)
        [(tid, tok)] = [(k, v) for k, v in meta["tokens"].items() if v["type"] == "forge-usbkey"]
        tok = {"type": "forge-usbkey", "name": "home", "keyslots": [], "pending_slot": tok["keyslots"][0],
               "epoch_after_add": "1"}
        path = os.path.join(self.t, "u.json")
        with open(path, "w") as f:
            json.dump(tok, f)
        subprocess.run(["cryptsetup", "token", "import", "--token-id", tid, "--token-replace", "--json-file",
                        path, data], check=True)
        r = self.run_tool("revoke", "home", stick=self.nostick())
        self.assertNotEqual(r.returncode, 0, "the unclear one must be reported")
        self.assertFalse(self.opens(os.path.join(self.stick_b, "forge_key")), "luks-test was not revoked")

    def test_round6_new6_neutralised_record_never_points_at_the_password(self):
        r = self.run_tool("add", "home", stick=self.stick_b, FAKE_FAIL_EPOCH_READ="2",
                          FAKE_STATE2=os.path.join(self.t, "s2"), FAKE_FAIL="token remove")
        self.assertNotEqual(r.returncode, 0)
        a = self.run_tool("audit", stick=self.nostick())
        self.assertNotRegex(a.stdout, r"slot 0: (UNFINISHED|UNCLEAR)")
        self.assertEqual(self.run_tool("cleanup", stick=self.nostick()).returncode, 0)
        self.assertEqual(self.tokens(), [])

    # Seventh review round.

    def test_round7_unreadable_luks_disk_is_never_treated_as_not_luks(self):
        # cryptsetup reports a read error (EIO) with the same exit code as "not LUKS2".
        data = self.second_disk()
        self.crypttab(f"luks-test {self.img} none", f"luks-data {data} none")
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        broken = f"luksDump {data},isLuks {data}"
        for cmd in (["revoke", "home"], ["remove"]):
            r = self.run_tool(*cmd, stick=self.nostick(), FAKE_FAIL=broken)
            self.assertNotEqual(r.returncode, 0, f"{cmd} claimed success with luks-data unreadable")
            self.assertIn("luks-data", r.stdout + r.stderr)
        self.assertNotEqual(json.load(open(self.applied)).get("security.usb_unlock"), False)
        r = self.run_tool("add", "other", stick=self.stick_a, FAKE_FAIL=broken)
        self.assertNotEqual(r.returncode, 0)

    def test_round7_present_but_empty_device_counts_as_missing(self):
        empty = os.path.join(self.t, "no-medium.img")
        open(empty, "w").close()
        self.crypttab(f"luks-test {self.img} none", f"luks-card {empty} none")
        r = self.run_tool("add", "home", stick=self.stick_b)
        self.assertNotEqual(r.returncode, 0)

    def test_round7_zero_filled_disk_is_ignored(self):
        zeros = os.path.join(self.t, "zeros.img")
        subprocess.run(["truncate", "-s", "1M", zeros], check=True)
        self.crypttab(f"luks-test {self.img} none", f"plain {zeros} /dev/urandom plain")
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)

    def test_round7_name_only_on_a_missing_disk_says_so(self):
        data = self.second_disk()
        self.crypttab(f"luks-data {data} none")
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        self.crypttab(f"luks-test {self.img} none", f"luks-data {data}.gone none")
        r = self.run_tool("revoke", "home", stick=self.nostick())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("NOT revoked on luks-data", r.stdout + r.stderr)

    # Eighth review round.

    def test_round8_a_luks1_line_does_not_block_anything(self):
        l1 = os.path.join(self.t, "old.img")
        subprocess.run(["truncate", "-s", "8M", l1], check=True)
        subprocess.run(["cryptsetup", "luksFormat", "--type", "luks1", "--batch-mode",
                        "--pbkdf-force-iterations", "1000", l1, self.pw], check=True, capture_output=True)
        self.crypttab(f"old {l1} none", f"luks-test {self.img} none")
        r = self.run_tool("add", "home", stick=self.stick_b)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LUKS1", r.stderr)
        self.assertEqual(self.run_tool("revoke", "home", stick=self.nostick()).returncode, 0)

    def test_round8_wiped_primary_header_with_unreadable_rest_is_not_ignored(self):
        data = self.second_disk()
        self.crypttab(f"luks-test {self.img} none", f"luks-data {data} none")
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 0)
        with open(data, "r+b") as f:          # the first 4 KiB are gone, the backup header is intact
            f.write(b"\0" * 4096)
        r = self.run_tool("revoke", "home", stick=self.nostick(), FAKE_FAIL=f"luksDump {data},isLuks {data}")
        self.assertNotEqual(r.returncode, 0, "the backup header still makes it a LUKS2 disk")
        self.assertIn("luks-data", r.stdout + r.stderr)

    def test_round8_detached_header_volume_is_never_silently_skipped(self):
        data = self.second_disk()
        self.crypttab(f"luks-test {self.img} none", f"luks-det /dev/null none header={data}")
        # Never skipped quietly: named on every run. (Round 9: it no longer blocks adds, because the
        # tool never puts keys there, but revoke can't promise the stick doesn't open it.)
        r = self.run_tool("add", "home", stick=self.stick_b)
        self.assertIn("luks-det", r.stderr)
        r = self.run_tool("revoke", "home", stick=self.nostick())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("luks-det", r.stdout + r.stderr)

    # Ninth round: round-8 Low findings.

    def test_round9_luks1_header_over_a_surviving_luks2_backup_is_not_skipped(self):
        data = os.path.join(self.t, "hybrid.img")
        subprocess.run(["truncate", "-s", "40M", data], check=True)
        subprocess.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", *FAST,
                        "--luks2-metadata-size", "4m", "--luks2-keyslots-size", "8m", data, self.pw],
                       check=True, capture_output=True)
        old = os.path.join(self.t, "old1.img")
        subprocess.run(["truncate", "-s", "8M", old], check=True)
        subprocess.run(["cryptsetup", "luksFormat", "--type", "luks1", "--batch-mode",
                        "--pbkdf-force-iterations", "1000", old, self.pw], check=True, capture_output=True)
        with open(old, "rb") as src, open(data, "r+b") as dst:   # LUKS1 header over the LUKS2 primary
            dst.write(src.read(1024 * 1024))
        self.assertEqual(subprocess.run(["cryptsetup", "isLuks", "--type", "luks1", data]).returncode, 0)
        with open(data, "rb") as f:
            f.seek(4 * 1024 * 1024)
            self.assertEqual(f.read(6), b"SKUL\xba\xbe", "setup: the LUKS2 backup header survived")
        self.crypttab(f"luks-test {self.img} none", f"luks-data {data} none")
        self.assertEqual(self.run_tool("add", "home", stick=self.stick_b).returncode, 1)

    def test_round9_detached_header_message_is_accurate_and_add_works(self):
        data = self.second_disk()
        self.crypttab(f"luks-test {self.img} none", f"luks-det /dev/null none header={data}")
        r = self.run_tool("add", "home", stick=self.stick_b)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_tool("revoke", "home", stick=self.nostick())
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Connect it", r.stdout + r.stderr)
        self.assertIn("header", r.stdout + r.stderr)
        self.assertFalse(self.opens(os.path.join(self.stick_b, "forge_key")), "luks-test was still revoked")

if __name__ == "__main__":
    unittest.main()







