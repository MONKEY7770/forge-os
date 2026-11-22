"""Regression tests for forge-apply security issues R2 and R7 (see SECURITY.md).

R2: the model folder step must never chown by path, so a user who owns the
parent folder cannot swap the new folder for a link and make root chown
any file.
R7: the settings import must not hang on special files, follow links, read
huge files, or wait forever for the apply lock.

Most tests need root (they chown files and switch to the "nobody" user to
act as the attacker), so they are skipped when not run as root.
"""
import fcntl
import importlib.machinery
import importlib.util
import json
import contextlib
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
sys.path.insert(0, LIB)
os.environ["FORGE_SCHEMA"] = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
APPLY = os.path.join(ROOT, "files/system/usr/libexec/forge-apply")

import forgecfg  # noqa: E402

IS_ROOT = os.geteuid() == 0
try:
    NOBODY = pwd.getpwnam("nobody")
except KeyError:
    NOBODY = None
NEED_ROOT = unittest.skipUnless(IS_ROOT and NOBODY, "needs root and a 'nobody' user")
TIMEOUT = 20   # seconds; a run that takes longer counts as hung


def load_apply(root):
    """Load forge-apply as a module, pointed at a fake root folder."""
    old = {k: os.environ.get(k) for k in ("FORGE_ROOT", "FORGE_DRYRUN", "FORGE_CONF")}
    os.environ.update(FORGE_ROOT=root, FORGE_DRYRUN="1",
                      FORGE_CONF=os.path.join(root, "etc/forge/settings.json"))
    try:
        loader = importlib.machinery.SourceFileLoader("forge_apply_under_test", APPLY)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


def as_nobody(fn):
    """Run fn() in a child process as the unprivileged 'nobody' user."""
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.setgroups([])
            os.setgid(NOBODY.pw_gid)
            os.setuid(NOBODY.pw_uid)
            fn()
            code = 0
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise AssertionError("attacker step failed")


class ModelsDirTest(unittest.TestCase):
    """R2: creating ai.models_dir and giving it to the ollama user."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.chmod(self.root, 0o755)
        # /var/home/eve belongs to the attacker.
        self.home = os.path.join(self.root, "var/home/eve")
        os.makedirs(self.home)
        if IS_ROOT and NOBODY:
            os.chown(self.home, NOBODY.pw_uid, NOBODY.pw_gid)
        # A root-owned file the attacker wants to own (stands in for /etc/sudoers).
        os.makedirs(os.path.join(self.root, "etc"))
        self.victim = os.path.join(self.root, "etc/sudoers")
        with open(self.victim, "w") as f:
            f.write("root ALL=(ALL) ALL\n")
        os.chmod(self.victim, 0o440)

    def apply_models_dir(self, models_dir):
        """Run apply() for real (not dry) with only chown simulated.

        Commands are recorded instead of run. A "chown" command is simulated
        with os.chown, which follows links just like chown(1) does. The ollama
        user is replaced by "nobody" because the sandbox has no ollama user.
        """
        mod = load_apply(self.root)
        mod.DRY = False
        mod.OLLAMA_USER = "nobody"
        ran = []

        def fake_run(*cmd):
            ran.append(cmd)
            if cmd[0] == "chown":
                os.chown(cmd[-1], NOBODY.pw_uid, NOBODY.pw_gid)
            return True

        mod.run = fake_run
        vals = forgecfg.values(overrides={})
        vals["ai.models_dir"] = models_dir
        clean, errors = forgecfg.validate(vals)
        self.assertEqual(errors, [])
        mod.apply(clean)
        return mod, ran

    @NEED_ROOT
    def test_models_dir_symlink_swap_cannot_chown_other_files(self):
        target = os.path.join(self.root, "var/home/eve/models")
        victim = self.victim
        real_mkdir = os.mkdir

        def swap():
            # The attacker owns the parent, so they may replace root's new
            # empty folder with a link to any file.
            os.rmdir(target)
            os.symlink(victim, target)

        def mkdir_then_attack(p, mode=0o777, *, dir_fd=None):
            real_mkdir(p, mode, dir_fd=dir_fd)
            if os.path.basename(os.fsdecode(p)) == "models":
                as_nobody(swap)

        os.mkdir = mkdir_then_attack
        try:
            self.apply_models_dir("/var/home/eve/models")
        finally:
            os.mkdir = real_mkdir
        self.assertEqual(os.stat(victim).st_uid, 0, "root chowned a file through a swapped link")

    @NEED_ROOT
    def test_models_dir_link_in_parent_path_refused(self):
        # The attacker points a folder in the path at /etc, outside the allowed places.
        os.symlink(os.path.join(self.root, "etc"), os.path.join(self.home, "link"))
        mod, _ = self.apply_models_dir("/var/home/eve/link/models")
        self.assertFalse(os.path.lexists(os.path.join(self.root, "etc/models")))
        self.assertTrue(any("models" in line and "note" in line for line in mod.log), mod.log)

    @NEED_ROOT
    def test_models_dir_missing_parent_skipped(self):
        mod, _ = self.apply_models_dir("/var/home/eve/a/b/models")
        self.assertFalse(os.path.lexists(os.path.join(self.home, "a")))
        self.assertTrue(any("note" in line and "/var/home/eve/a/b" in line for line in mod.log), mod.log)

    @NEED_ROOT
    def test_models_dir_new_folder_given_to_ollama(self):
        mod, ran = self.apply_models_dir("/var/home/eve/models")
        d = os.path.join(self.home, "models")
        st = os.lstat(d)
        self.assertTrue(os.path.isdir(d) and not os.path.islink(d))
        self.assertEqual((st.st_uid, st.st_gid), (NOBODY.pw_uid, NOBODY.pw_gid))
        self.assertFalse([c for c in ran if c[0] == "chown"], "chown by path must not be used")

    @NEED_ROOT
    def test_models_dir_existing_folder_never_chowned(self):
        d = os.path.join(self.home, "models")
        os.mkdir(d)
        mod, _ = self.apply_models_dir("/var/home/eve/models")
        self.assertEqual(os.stat(d).st_uid, 0)
        self.assertTrue(any("already exists" in line for line in mod.log), mod.log)

    @NEED_ROOT
    def test_test_seam_runs_step_in_dry_mode(self):
        # FORGE_TEST_OLLAMA_USER (only read when FORGE_ROOT is set) lets the
        # real subprocess run this one step while every command stays dry.
        imp = os.path.join(self.root, "in.json")
        with open(imp, "w") as f:
            json.dump({"ai.models_dir": "/var/home/eve/models"}, f)
        env = dict(os.environ, FORGE_ROOT=self.root, FORGE_DRYRUN="1", PYTHONPATH=LIB,
                   FORGE_CONF=os.path.join(self.root, "etc/forge/settings.json"),
                   FORGE_TEST_OLLAMA_USER="nobody")
        r = subprocess.run([sys.executable, APPLY, "--import", imp], env=env,
                           capture_output=True, text=True, timeout=TIMEOUT)
        self.assertEqual(r.returncode, 0, r.stderr)
        st = os.lstat(os.path.join(self.home, "models"))
        self.assertEqual(st.st_uid, NOBODY.pw_uid)


class ImportTest(unittest.TestCase):
    """R7: reading the settings file handed to --import."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.conf = os.path.join(self.root, "etc/forge/settings.json")
        self.core = os.path.join(self.root, "run/forge-core")
        os.makedirs(self.core)
        self.env = dict(os.environ, FORGE_ROOT=self.root, FORGE_DRYRUN="1",
                        FORGE_CONF=self.conf, PYTHONPATH=LIB)
        self.env.pop("SUDO_USER", None)
        # A file the caller must not be able to make root read.
        self.secret = os.path.join(self.root, "secret.json")
        with open(self.secret, "w") as f:
            json.dump({"ai.keep_alive_min": 777}, f)

    def run_apply(self, *args, code=None, **env):
        """Run forge-apply; returns None if it hangs past TIMEOUT."""
        cmd = [sys.executable, APPLY, *args]
        if code:
            cmd = [sys.executable, "-c", code, APPLY, *args]
        try:
            return subprocess.run(cmd, env=dict(self.env, **env), capture_output=True,
                                  text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return None

    def saved(self):
        if not os.path.exists(self.conf):
            return {}
        with open(self.conf) as f:
            return json.load(f)

    def test_fifo_import_does_not_hang(self):
        fifo = os.path.join(self.root, "in.json")
        os.mkfifo(fifo)
        r = self.run_apply("--import", fifo)
        self.assertIsNotNone(r, "forge-apply hung on a FIFO while holding the lock")
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_symlink_import_refused(self):
        link = os.path.join(self.root, "in.json")
        os.symlink(self.secret, link)
        r = self.run_apply("--import", link)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("ai.keep_alive_min", self.saved())

    def test_oversized_import_refused(self):
        big = os.path.join(self.root, "in.json")
        with open(big, "w") as f:
            f.write("{" + " " * (1024 * 1024 + 10) + '"ai.keep_alive_min": 9}')
        r = self.run_apply("--import", big)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("ai.keep_alive_min", self.saved())

    def test_normal_import_still_works(self):
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"ai.keep_alive_min": 9}, fh)
        r = self.run_apply("--import", f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.saved().get("ai.keep_alive_min"), 9)

    def test_forge_user_import_from_run_dir_works(self):
        f = os.path.join(self.core, "tmpabc123.json")
        with open(f, "w") as fh:
            json.dump({"ai.keep_alive_min": 11}, fh)
        r = self.run_apply("--import", "/run/forge-core/tmpabc123.json", SUDO_USER="forge")
        self.assertIsNotNone(r)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.saved().get("ai.keep_alive_min"), 11)

    def test_forge_user_fifo_refused(self):
        os.mkfifo(os.path.join(self.core, "a.json"))
        r = self.run_apply("--import", "/run/forge-core/a.json", SUDO_USER="forge")
        self.assertIsNotNone(r, "forge-apply hung on a FIFO while holding the lock")
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_forge_user_symlink_refused(self):
        os.symlink(self.secret, os.path.join(self.core, "a.json"))
        r = self.run_apply("--import", "/run/forge-core/a.json", SUDO_USER="forge")
        self.assertIsNotNone(r)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("ai.keep_alive_min", self.saved())

    def test_forge_user_run_dir_link_refused(self):
        # /run/forge-core itself swapped for a link to another folder.
        other = os.path.join(self.root, "other")
        os.makedirs(other)
        with open(os.path.join(other, "a.json"), "w") as f:
            json.dump({"ai.keep_alive_min": 12}, f)
        os.rmdir(self.core)
        os.symlink(other, self.core)
        r = self.run_apply("--import", "/run/forge-core/a.json", SUDO_USER="forge")
        self.assertIsNotNone(r)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("ai.keep_alive_min", self.saved())

    def test_forge_user_bad_names_refused(self):
        os.makedirs(os.path.join(self.core, "sub"))
        for name in (".hidden.json", "sub/a.json"):
            with open(os.path.join(self.core, name), "w") as f:
                json.dump({"ai.keep_alive_min": 13}, f)
        for arg in ("/run/forge-core/.hidden.json", "/run/forge-core/sub/a.json",
                    "/run/forge-core/../forge-core/sub/a.json", "/run/forge-core/"):
            r = self.run_apply("--import", arg, SUDO_USER="forge")
            self.assertIsNotNone(r, arg)
            self.assertEqual(r.returncode, 2, arg + ": " + r.stdout + r.stderr)
        self.assertNotIn("ai.keep_alive_min", self.saved())

    def test_lock_wait_has_a_limit(self):
        # Another apply holds the lock and never lets go.
        os.makedirs(os.path.join(self.root, "run"), exist_ok=True)
        holder = open(os.path.join(self.root, "run/forge-apply.lock"), "w")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)
        # Load forge-apply with a 1 second wait so the test is quick.
        code = ("import importlib.machinery, importlib.util, sys\n"
                "path = sys.argv[1]\n"
                "loader = importlib.machinery.SourceFileLoader('fa', path)\n"
                "mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('fa', loader))\n"
                "loader.exec_module(mod)\n"
                "mod.LOCK_WAIT_S = 1\n"
                "sys.argv = sys.argv[1:]\n"
                "sys.exit(mod.main())\n")
        r = self.run_apply(code=code)
        self.assertIsNotNone(r, "forge-apply waited forever for the lock")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("another forge-apply", r.stderr.lower())


class ApplyLowFindingsTest(unittest.TestCase):
    """Low findings from the second review round (forge-apply)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.chmod(self.root, 0o755)
        os.makedirs(os.path.join(self.root, "var/home/eve"))
        os.makedirs(os.path.join(self.root, "etc"))

    def values(self, **changes):
        vals = forgecfg.values(overrides={})
        vals.update(changes)
        clean, errors = forgecfg.validate(vals)
        self.assertEqual(errors, [])
        return clean

    def real_mod(self):
        mod = load_apply(self.root)
        mod.DRY = False
        mod.OLLAMA_USER = "nobody" if NOBODY else "root"
        mod.run = lambda *cmd: True
        return mod

    def test_folder_deleted_right_after_mkdir_does_not_abort_the_apply(self):
        mod = self.real_mod()
        real_stat = mod.os.stat

        def vanish(p, *a, **k):
            if "dir_fd" in k:
                raise FileNotFoundError(2, "No such file or directory")
            return real_stat(p, *a, **k)

        with mock.patch.object(mod.os, "stat", vanish):
            mod.apply(self.values(**{"ai.models_dir": "/var/home/eve/models"}))
        # The steps after the model folder still ran.
        self.assertTrue(os.path.exists(os.path.join(self.root, "etc/xdg/kded6rc")))

    def test_crypttab_write_is_atomic_and_keeps_extra_fields(self):
        tab = os.path.join(self.root, "etc/crypttab")
        original = "luks-root UUID=abc none discard,x-extra keep-me\n"
        with open(tab, "w") as f:
            f.write(original)
        mod = self.real_mod()
        vals = self.values(**{"security.usb_unlock": True})
        # The disk fills up while the new file is written: the original must stay as it was.
        with mock.patch.object(mod.os, "fsync", side_effect=OSError(28, "No space left on device")):
            with contextlib.suppress(OSError, SystemExit):
                mod.usb_crypttab(vals)
        with open(tab) as f:
            self.assertEqual(f.read(), original, "a failed write damaged /etc/crypttab")
        mod.usb_crypttab(vals)
        real_open = open
        with real_open(tab) as f:
            line = f.read().strip()
        self.assertTrue(line.endswith("keep-me"), line)
        self.assertIn("/forge_key:LABEL=", line)

    def test_models_dir_on_a_fuse_mount_is_refused_without_touching_it(self):
        info = os.path.join(self.root, "mountinfo")
        eve = os.path.join(self.root, "var/home/eve")
        with open(info, "w") as f:
            f.write("22 1 0:21 / / rw - ext4 /dev/vda1 rw\n")
            f.write(f"99 22 0:99 / {eve}/fuse rw,nosuid,nodev - fuse.sshfs eve@host:/ rw,user_id=1000\n")
        os.makedirs(os.path.join(eve, "fuse"))
        mod = self.real_mod()
        mod.MOUNTINFO = info
        mod.apply(self.values(**{"ai.models_dir": "/var/home/eve/fuse/models"}))
        self.assertFalse(os.path.exists(os.path.join(eve, "fuse/models")))
        self.assertTrue(any("fuse" in line.lower() for line in mod.log), mod.log)

    def test_import_that_is_not_a_settings_object_fails_cleanly(self):
        f = os.path.join(self.root, "in.json")
        for text in ("[1, 2]", "\"x\"", "{not json"):
            with open(f, "w") as fh:
                fh.write(text)
            env = dict(os.environ, FORGE_ROOT=self.root, FORGE_DRYRUN="1",
                       FORGE_CONF=os.path.join(self.root, "etc/forge/settings.json"), PYTHONPATH=LIB)
            env.pop("SUDO_USER", None)
            r = subprocess.run([sys.executable, APPLY, "--import", f], env=env, capture_output=True, text=True,
                               timeout=TIMEOUT)
            self.assertEqual(r.returncode, 2, text + r.stderr)
            self.assertNotIn("Traceback", r.stderr)


class ApplyLowFindingsRound2Test(ApplyLowFindingsTest):
    """Gaps the reviewer found in the first round of these fixes."""

    def test_fuse_mount_behind_a_symlink_is_never_touched(self):
        eve = os.path.join(self.root, "var/home/eve")
        os.makedirs(os.path.join(eve, "fuse"))
        os.symlink(os.path.join(eve, "fuse"), os.path.join(eve, "link"))
        info = os.path.join(self.root, "mountinfo")
        with open(info, "w") as f:
            f.write("22 1 0:21 / / rw - ext4 /dev/vda1 rw\n")
            f.write(f"99 22 0:99 / {eve}/fuse rw - fuse.sshfs x rw\n")
        mod = self.real_mod()
        mod.MOUNTINFO = info
        touched = []
        real_lstat, real_stat = os.lstat, os.stat

        def spy(real):
            def f(p, *a, **k):
                if isinstance(p, str) and p.startswith(os.path.join(eve, "fuse")):
                    touched.append(p)
                return real(p, *a, **k)
            return f
        with mock.patch.object(mod.os, "lstat", spy(real_lstat)), mock.patch.object(mod.os, "stat", spy(real_stat)):
            mod.apply(self.values(**{"ai.models_dir": "/var/home/eve/link/models"}))
        self.assertEqual(touched, [], "root looked inside the FUSE mount (that is where it hangs)")
        self.assertTrue(any("fuse" in line.lower() for line in mod.log), mod.log)

    def test_double_slash_model_folder_is_refused_by_validation(self):
        vals = forgecfg.values(overrides={})
        vals["ai.models_dir"] = "/var/home/eve//models"
        _, errors = forgecfg.validate(vals)
        self.assertTrue(errors)

    def test_mount_inside_a_fuse_mount_still_counts_as_fuse(self):
        eve = os.path.join(self.root, "var/home/eve")
        info = os.path.join(self.root, "mountinfo")
        with open(info, "w") as f:
            f.write("22 1 0:21 / / rw - ext4 /dev/vda1 rw\n")
            f.write(f"99 22 0:99 / {eve}/fuse rw - fuse.sshfs x rw\n")
            f.write(f"100 99 0:100 / {eve}/fuse/inner rw - tmpfs tmpfs rw\n")
        mod = self.real_mod()
        mod.MOUNTINFO = info
        self.assertTrue(mod.on_fuse(f"{eve}/fuse/inner/models"))

    def test_crypttab_keeps_owner_and_ignores_things_at_temp_paths(self):
        tab = os.path.join(self.root, "etc/crypttab")
        with open(tab, "w") as f:
            f.write("luks-root UUID=abc none discard\n")
        os.chown(tab, 0, 10)
        os.chmod(tab, 0o600)
        victim = os.path.join(self.root, "victim")
        with open(victim, "w") as f:
            f.write("do not touch\n")
        os.symlink(victim, tab + ".forge.tmp")
        mod = self.real_mod()
        mod.usb_crypttab(self.values(**{"security.usb_unlock": True}))
        st = os.lstat(tab)
        self.assertTrue(stat.S_ISREG(st.st_mode), "crypttab became a link")
        self.assertEqual((st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)), (0, 10, 0o600))
        self.assertEqual(open(victim).read(), "do not touch\n")
        self.assertIn("/forge_key:LABEL=", open(tab).read())

    def test_crypttab_change_always_rebuilds_the_boot_image_overlay(self):
        # rpm-ostree answers "No changes." to --track of a path that is already tracked, so a
        # second change (USB unlock off, new timeout) would stay out of the boot image until the
        # next update. --force-sync makes it deploy with the new file every time.
        tab = os.path.join(self.root, "etc/crypttab")
        with open(tab, "w") as f:
            f.write("luks-root UUID=abc none discard\n")
        mod = self.real_mod()
        calls = []
        mod.run = lambda *cmd: calls.append(cmd) or True
        mod.usb_crypttab(self.values(**{"security.usb_unlock": True}))
        mod.usb_crypttab(self.values(**{"security.usb_unlock": False}))
        ostree = [c for c in calls if c[:2] == ("rpm-ostree", "initramfs-etc")]
        self.assertEqual(ostree, [("rpm-ostree", "initramfs-etc", "--track=/etc/crypttab", "--force-sync")] * 2)

    def test_crypttab_write_problem_does_not_abort_the_apply(self):
        tab = os.path.join(self.root, "etc/crypttab")
        with open(tab, "w") as f:
            f.write("luks-root UUID=abc none discard\n")
        mod = self.real_mod()
        with mock.patch.object(mod.os, "replace", side_effect=OSError(30, "Read-only file system")):
            mod.apply(self.values(**{"security.usb_unlock": True}))
        self.assertTrue(any("crypttab" in line for line in mod.log), mod.log)
        self.assertEqual(open(tab).read(), "luks-root UUID=abc none discard\n")
        self.assertEqual([f for f in os.listdir(os.path.join(self.root, "etc")) if "forge" in f and "tmp" in f], [])

    def test_deeply_nested_import_fails_cleanly(self):
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            fh.write("[" * 200000 + "]" * 200000)
        env = dict(os.environ, FORGE_ROOT=self.root, FORGE_DRYRUN="1",
                   FORGE_CONF=os.path.join(self.root, "etc/forge/settings.json"), PYTHONPATH=LIB)
        env.pop("SUDO_USER", None)
        r = subprocess.run([sys.executable, APPLY, "--import", f], env=env, capture_output=True, text=True,
                           timeout=TIMEOUT)
        self.assertEqual(r.returncode, 2, r.stderr[-300:])
        self.assertNotIn("Traceback", r.stderr)


class ApplyLowFindingsRound3Test(ApplyLowFindingsTest):
    def test_crypttab_with_non_utf8_bytes_is_kept_and_updated(self):
        tab = os.path.join(self.root, "etc/crypttab")
        with open(tab, "wb") as f:
            f.write(b"luks-root UUID=abc none discard\n# caf\xe9 note\n")
        mod = self.real_mod()
        mod.apply(self.values(**{"security.usb_unlock": True}))
        data = open(tab, "rb").read()
        self.assertIn(b"/forge_key:LABEL=", data)
        self.assertIn(b"# caf\xe9 note", data)
        self.assertTrue(os.path.exists(os.path.join(self.root, "etc/xdg/kded6rc")), "the apply stopped early")

    def test_crypttab_comment_with_odd_line_breaks_stays_a_comment(self):
        # Python's splitlines() also breaks at U+2028, \x0b, \x0c and others; systemd only at \n.
        # A comment with one of those must never turn into a line that gets the USB key.
        tab = os.path.join(self.root, "etc/crypttab")
        tricky = ["# disabled:\u2028luks-old UUID=dead none", "# a\x0bluks-b UUID=b none",
                  "# a\x0cluks-c UUID=c none", "# a\x1cluks-d UUID=d none", "# a\x85luks-e UUID=e none"]
        with open(tab, "w", encoding="utf-8") as f:
            f.write("luks-root UUID=abc none discard\n" + "\n".join(tricky) + "\n")
        mod = self.real_mod()
        mod.apply(self.values(**{"security.usb_unlock": True}))
        text = open(tab, encoding="utf-8").read()
        self.assertEqual(text.count("/forge_key:"), 1, text)
        for t in tricky:
            self.assertIn(t, text)

    def test_crypttab_backup_is_never_readable_by_others(self):
        tab = os.path.join(self.root, "etc/crypttab")
        with open(tab, "w") as f:
            f.write("luks-root UUID=abc none discard\n")
        os.chmod(tab, 0o644)
        old = os.umask(0o022)
        try:
            self.real_mod().apply(self.values(**{"security.usb_unlock": True}))
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(tab + ".forge.bak").st_mode), 0o600)
        self.assertEqual(open(tab + ".forge.bak").read(), "luks-root UUID=abc none discard\n")

    def test_model_folder_step_cannot_hang_the_apply(self):
        mod = self.real_mod()
        mod.MODELS_DIR_TIMEOUT_S = 1
        mod.make_models_dir_unsafe = mod.make_models_dir_unsafe if hasattr(mod, "make_models_dir_unsafe") else None

        def stuck(d):
            time.sleep(30)
        if hasattr(mod, "make_models_dir_unsafe"):
            mod.make_models_dir_unsafe = stuck
        else:
            mod.make_models_dir = stuck
        t0 = time.monotonic()
        mod.apply(self.values(**{"ai.models_dir": "/var/home/eve/models"}))
        self.assertLess(time.monotonic() - t0, 10, "the apply waited for a stuck folder step")
        self.assertTrue(any("did not finish" in line for line in mod.log), mod.log)

if __name__ == "__main__":
    unittest.main()



