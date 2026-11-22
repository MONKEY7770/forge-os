"""forge-usbkey: picking the key stick safely, mounting it safely, reading the key safely (R6)."""
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-usbkey")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
HAVE = shutil.which("cryptsetup") is not None
FAST = ["--pbkdf", "pbkdf2", "--pbkdf-force-iterations", "1000"]


def load_tool(**env):
    """Import forge-usbkey as a module with the given environment."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("FORGE_")}
    clean.update(FORGE_LIB=LIB, FORGE_SCHEMA=SCHEMA, **env)
    with mock.patch.dict(os.environ, clean, clear=True):
        loader = importlib.machinery.SourceFileLoader("forge_usbkey_under_test", TOOL)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    return mod


def dev(name, label=None, type_="part", tran=None, rm=False, hotplug=False, fstype="vfat",
        pkname=None, mounts=None, children=None):
    d = {"name": name, "label": label, "type": type_, "tran": tran, "rm": rm, "hotplug": hotplug,
         "fstype": fstype, "pkname": pkname, "mountpoints": mounts or [None]}
    if children:
        d["children"] = children
    return d


def usb_disk(disk="/dev/sdb", label="FORGEKEY", **part):
    """A USB stick with one partition. Like real lsblk, only the disk reports TRAN usb."""
    p = dev(disk + "1", label=label, tran=None, rm=True, hotplug=True, pkname=disk, **part)
    return dev(disk, type_="disk", tran="usb", rm=True, hotplug=True, fstype=None, children=[p])


def internal_disk():
    return dev("/dev/nvme0n1", type_="disk", tran="nvme", fstype=None,
               children=[dev("/dev/nvme0n1p1", fstype="vfat", pkname="/dev/nvme0n1", label="EFI")])


class StickSelectionTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_tool()
        self.calls = []
        self.mount_rc = 0
        self.mount_err = ""
        self.made = []

    def fake_run(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[0] == "mount":
            self.made.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, self.mount_rc, "", self.mount_err)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def find(self, blockdevices, writable=False):
        """Call find_usb with a fake lsblk listing and no real mounting."""
        data = {"blockdevices": blockdevices}
        real_exists = os.path.exists
        patches = [mock.patch.object(self.mod, "run", self.fake_run),
                   # The old code looked at /dev/disk/by-label: pretend the label exists there.
                   mock.patch.object(self.mod.os.path, "exists",
                                     lambda p: True if str(p).startswith("/dev/disk/by-label/") else real_exists(p))]
        if hasattr(self.mod, "lsblk_json"):
            patches.append(mock.patch.object(self.mod, "lsblk_json", lambda: data))
        for p in patches:
            p.start()
        try:
            return self.mod.find_usb("FORGEKEY", writable=writable)
        finally:
            for p in reversed(patches):
                p.stop()

    def tearDown(self):
        for d in self.made:
            if os.path.isdir(d):
                os.rmdir(d)
                if os.path.basename(os.path.dirname(d)).startswith("forge-usbkey-"):
                    os.rmdir(os.path.dirname(d))

    def mounts(self):
        return [c for c in self.calls if c[0] == "mount"]

    def assert_refused(self, blockdevices, pattern):
        with self.assertRaises(self.mod.Fail) as cm:
            self.find(blockdevices)
        self.assertRegex(str(cm.exception), pattern)
        self.assertEqual(self.mounts(), [], "nothing may be mounted")

    def test_loop_image_with_the_label_is_not_used(self):
        loop = dev("/dev/loop0", label="FORGEKEY", type_="loop", fstype="vfat")
        self.assert_refused([internal_disk(), loop], "not a removable USB")

    def test_partition_on_a_loop_image_is_not_used(self):
        loop = dev("/dev/loop1", type_="loop", fstype=None, rm=True, hotplug=True,
                   children=[dev("/dev/loop1p1", label="FORGEKEY", pkname="/dev/loop1", rm=True, hotplug=True)])
        self.assert_refused([loop], "not a removable USB")

    def test_internal_disk_with_the_label_is_not_used(self):
        sata = dev("/dev/sda", type_="disk", tran="sata", fstype=None,
                   children=[dev("/dev/sda2", label="FORGEKEY", pkname="/dev/sda")])
        self.assert_refused([sata], "not a removable USB")

    def test_device_mapper_volume_is_not_used(self):
        disk = usb_disk(fstype="crypto_LUKS")
        disk["children"][0]["label"] = None
        disk["children"][0]["children"] = [dev("/dev/dm-3", label="FORGEKEY", type_="crypt", tran="usb",
                                               rm=True, hotplug=True, pkname="/dev/sdb1")]
        self.assert_refused([disk], "not a removable USB")

    def test_wrong_file_system_is_not_used(self):
        self.assert_refused([usb_disk(fstype="ext4")], "not a removable USB")

    def test_two_drives_with_the_label_are_refused(self):
        self.assert_refused([usb_disk("/dev/sdb"), usb_disk("/dev/sdc")], "one key stick at a time")

    def test_no_drive_with_the_label(self):
        self.assert_refused([internal_disk()], "No drive named FORGEKEY")

    def test_already_mounted_stick_is_not_reused(self):
        self.assert_refused([usb_disk(mounts=["/run/media/me/FORGEKEY"])], "[Ee]ject")

    def test_mount_is_private_with_safe_options(self):
        folder, tmp = self.find([internal_disk(), usb_disk()])
        self.assertEqual(folder, tmp)
        [cmd] = self.mounts()
        self.assertEqual(cmd[:3], ["mount", "-t", "vfat"])
        self.assertEqual(cmd[3], "-o")
        safe = {"nosuid", "nodev", "noexec", "noatime", "uid=0", "gid=0", "umask=077"}
        self.assertEqual(set(cmd[4].split(",")), {"ro"} | safe)
        self.assertEqual(cmd[5], "/dev/sdb1")
        self.assertEqual(stat.S_IMODE(os.stat(tmp).st_mode), 0o700)
        # The mount root takes its mode from the stick, so the folder around it must be root-only too.
        parent = os.path.dirname(tmp)
        self.assertEqual(stat.S_IMODE(os.stat(parent).st_mode), 0o700)
        self.assertEqual(os.stat(parent).st_uid, os.geteuid())
        self.calls.clear()
        folder2, _ = self.find([usb_disk(fstype="exfat")], writable=True)
        self.assertNotEqual(folder, folder2, "each run gets a fresh folder")
        [cmd] = self.mounts()
        self.assertEqual(cmd[2], "exfat")
        self.assertEqual(set(cmd[4].split(",")), {"rw"} | safe)

    def test_whole_disk_stick_and_string_flags_from_older_lsblk(self):
        whole = dev("/dev/sdd", label="FORGEKEY", type_="disk", tran="usb", rm="0", hotplug="1")
        self.find([whole])
        self.assertEqual(self.mounts()[0][5], "/dev/sdd")

    def test_busy_device_asks_to_eject(self):
        self.mount_rc, self.mount_err = 32, "mount: /tmp/x: /dev/sdb1 already mounted or mount point busy."
        with self.assertRaises(self.mod.Fail) as cm:
            self.find([usb_disk()])
        self.assertIn("Eject", str(cm.exception))


class KeyFileTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_tool()
        self.t = tempfile.mkdtemp()
        self.key = os.path.join(self.t, "forge_key")

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def assert_rejected(self):
        with self.assertRaises(self.mod.Fail):
            self.mod.read_key_file(self.t)

    def test_symlink_rejected(self):
        with open(os.path.join(self.t, "real"), "w") as f:
            f.write("x" * 40)
        os.symlink(os.path.join(self.t, "real"), self.key)
        self.assert_rejected()

    def test_fifo_rejected_without_hanging(self):
        os.mkfifo(self.key)
        self.assert_rejected()

    def test_directory_rejected(self):
        os.mkdir(self.key)
        self.assert_rejected()

    def test_too_small_and_too_large_rejected(self):
        for size in (15, 8193):
            with open(self.key, "w") as f:
                f.write("x" * size)
            self.assert_rejected()

    def test_missing_rejected(self):
        self.assert_rejected()


@unittest.skipUnless(HAVE, "cryptsetup not installed")
class KeySwapTest(unittest.TestCase):
    """The key that is checked must be the key that is enrolled."""

    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.img = os.path.join(self.t, "disk.img")
        self.pw = os.path.join(self.t, "password")
        with open(self.pw, "w") as f:
            f.write("my-disk-password")
        subprocess.run(["truncate", "-s", "32M", self.img], check=True)
        subprocess.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", *FAST,
                        self.img, self.pw], check=True)
        with open(os.path.join(self.t, "crypttab"), "w") as f:
            f.write(f"luks-test {self.img} none\n")
        apply = os.path.join(self.t, "apply")
        with open(apply, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(apply, 0o755)
        self.stick = os.path.join(self.t, "stick")
        os.makedirs(self.stick)
        self.good = "G" * 40
        self.evil = "E" * 40
        with open(os.path.join(self.stick, "forge_key"), "w") as f:
            f.write(self.good)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def opens(self, text):
        p = os.path.join(self.t, "probe")
        with open(p, "w") as f:
            f.write(text)
        return subprocess.run(["cryptsetup", "open", "--test-passphrase", "--key-file", p, self.img],
                              capture_output=True).returncode == 0

    def test_key_swapped_after_check_is_not_enrolled_and_copy_is_wiped(self):
        mod = load_tool(FORGE_CRYPTTAB=os.path.join(self.t, "crypttab"), FORGE_USB_DIR=self.stick,
                        FORGE_TEST_EXISTING_KEYFILE=self.pw, FORGE_APPLY=apply_path(self.t),
                        FORGE_CONF=os.path.join(self.t, "settings.json"))
        seen = {}
        real_run = subprocess.run

        def run(cmd, **kw):
            cmd = list(cmd)
            if cmd[:2] == ["cryptsetup", "luksAddKey"]:
                # Swap the stick's file after all checks, just before enrolling.
                with open(os.path.join(self.stick, "forge_key"), "w") as f:
                    f.write(self.evil)
                keyfile = cmd[-1]
                seen["keyfile"] = keyfile
                seen["mode"] = stat.S_IMODE(os.stat(keyfile).st_mode)
                seen["dirmode"] = stat.S_IMODE(os.stat(os.path.dirname(keyfile)).st_mode)
                cmd = cmd[:2] + FAST + cmd[2:]
            return real_run(cmd, **kw)

        with mock.patch.object(mod, "run", run), mock.patch.dict(os.environ, {"FORGE_SCHEMA": SCHEMA,
                                                                       "FORGE_APPLY": apply_path(self.t)}):
            mod.main(["add", "home"])
        self.assertTrue(self.opens(self.good), "the checked key must be the enrolled key")
        self.assertFalse(self.opens(self.evil), "a swapped-in key must not be enrolled")
        self.assertFalse(os.path.realpath(seen["keyfile"]).startswith(os.path.realpath(self.stick)))
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["dirmode"], 0o700)
        self.assertFalse(os.path.exists(seen["keyfile"]), "the private copy must be deleted")
        self.assertFalse(os.path.exists(os.path.dirname(seen["keyfile"])))


def apply_path(t):
    return os.path.join(t, "apply")


class NoEnvTrustTest(unittest.TestCase):
    def test_lsblk_listing_is_not_taken_from_the_environment(self):
        src = open(TOOL).read()
        self.assertTrue(re.search(r"def lsblk_json\(", src))
        body = src.split("def lsblk_json(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("environ", body)


if __name__ == "__main__":
    unittest.main()
