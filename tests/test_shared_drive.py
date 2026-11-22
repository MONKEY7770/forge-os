"""Shared drive with Windows: forge-shared-drive finds the drive by its label, mounts it
through udisks (no root) and keeps ~/Shared pointing at it. Fake lsblk, udisksctl and
notify-send stand in for the real tools."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-shared-drive")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
APPLY = os.path.join(ROOT, "files/system/usr/libexec/forge-apply")
UNITS = os.path.join(ROOT, "files/system/usr/lib/systemd/user")
sys.path.insert(0, LIB)

import forgecfg  # noqa: E402

# Fake lsblk: prints the devices in $FAKE_DISKS as one disk with partitions.
FAKE_LSBLK = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("lsblk " + " ".join(sys.argv[1:]) + "\n")
devs = json.load(open(os.environ["FAKE_DISKS"]))
cols = ("name", "path", "label", "fstype", "mountpoint", "uuid", "partuuid")
kids = [{k: d.get(k) for k in cols} for d in devs]
print(json.dumps({"blockdevices": [{"name": "nvme0n1", "path": "/dev/nvme0n1", "label": None,
                                     "fstype": None, "mountpoint": None, "uuid": None, "children": kids}]}))
'''

# Fake udisksctl: mount/unmount, with the result each device asks for in $FAKE_DISKS.
FAKE_UDISKSCTL = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("udisksctl " + " ".join(sys.argv[1:]) + "\n")
p = os.environ["FAKE_DISKS"]
devs = json.load(open(p))
verb, dev = sys.argv[1], sys.argv[sys.argv.index("-b") + 1]
[d] = [d for d in devs if d["path"] == dev]
if verb == "mount":
    res = d.get("mount", {"rc": 0})
    if res["rc"] != 0:
        sys.stderr.write(res["err"] + "\n")
        sys.exit(res["rc"])
    mp = os.path.join(os.environ["FAKE_MEDIA"], d.get("label") or d["uuid"])
    os.makedirs(mp, exist_ok=True)
    for rel in d.get("files", []):
        os.makedirs(os.path.join(mp, rel), exist_ok=True)
    d["mountpoint"] = mp
    with open(os.environ["FORGE_MOUNTINFO"], "a") as f:
        ro = d.get("ro") or ("-o" in sys.argv and sys.argv[sys.argv.index("-o") + 1] == "ro")
        f.write("90 30 259:5 / %s %s,nosuid,nodev - %s %s rw\n"
                % (mp, "ro" if ro else "rw", d["fstype"], dev))
    print("Mounted %s at %s" % (dev, mp))
elif verb == "unmount":
    mi = os.environ["FORGE_MOUNTINFO"]
    keep = [l for l in open(mi).read().splitlines(True) if " %s " % d["mountpoint"] not in l]
    open(mi, "w").write("".join(keep))
    d["mountpoint"] = None
    print("Unmounted %s." % dev)
json.dump(devs, open(p, "w"))
'''

FAKE_NOTIFY = r'''#!/bin/sh
printf 'notify-send' >> "$FAKE_LOG"
for a in "$@"; do printf ' [%s]' "$a" >> "$FAKE_LOG"; done
echo >> "$FAKE_LOG"
'''

DIRTY_ERR = ("Error mounting /dev/nvme0n1p5 at /run/media/alice/SHARED: wrong fs type, bad option, "
             "bad superblock on /dev/nvme0n1p5, missing codepage or helper program, or other error")


class SharedDriveTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.bin = os.path.join(self.t, "bin")
        self.home = os.path.join(self.t, "home")
        self.media = os.path.join(self.t, "media")
        for d in (self.bin, self.home, self.media):
            os.makedirs(d)
        for name, body in (("lsblk", FAKE_LSBLK), ("udisksctl", FAKE_UDISKSCTL), ("notify-send", FAKE_NOTIFY)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.log = os.path.join(self.t, "calls")
        self.disks = os.path.join(self.t, "disks.json")
        self.mountinfo = os.path.join(self.t, "mountinfo")
        open(self.mountinfo, "w").close()
        self.conf = os.path.join(self.t, "settings.json")
        self.settings(storage__shared_drive_enabled=True)
        self.devices([])

    # helpers
    def settings(self, **vals):
        with open(self.conf, "w") as f:
            json.dump({k.replace("__", "."): v for k, v in vals.items()}, f)

    def devices(self, devs):
        with open(self.disks, "w") as f:
            json.dump(devs, f)

    def dev(self, fstype="exfat", label="SHARED", path="/dev/nvme0n1p5", **extra):
        d = {"name": os.path.basename(path), "path": path, "label": label, "fstype": fstype,
             "mountpoint": None, "uuid": "1234-ABCD"}
        d.update(extra)
        return d

    def run_tool(self, *args):
        env = dict(os.environ, FORGE_LIB=LIB, FORGE_SCHEMA=SCHEMA, FORGE_CONF=self.conf,
                   HOME=self.home, XDG_STATE_HOME=os.path.join(self.home, ".local/state"),
                   FAKE_LOG=self.log, FAKE_DISKS=self.disks, FAKE_MEDIA=self.media,
                   FORGE_MOUNTINFO=self.mountinfo, FORGE_SHARED_MEDIA=self.media + "/",
                   PATH=self.bin + ":" + os.environ["PATH"])
        return subprocess.run([sys.executable, TOOL, *args], env=env, capture_output=True,
                              text=True, timeout=60)

    def calls(self, prefix=""):
        if not os.path.exists(self.log):
            return []
        return [c for c in open(self.log).read().splitlines() if c.startswith(prefix)]

    @property
    def link(self):
        return os.path.join(self.home, "Shared")

    def mounted_at(self, label="SHARED"):
        return os.path.join(self.media, label)

    # happy path
    def test_exfat_drive_is_mounted_and_linked(self):
        self.devices([self.dev()])
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls("udisksctl"),
                         ["udisksctl mount -b /dev/nvme0n1p5 --no-user-interaction"])
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(os.readlink(self.link), self.mounted_at())
        self.assertEqual(self.calls("notify-send"), [])
        self.assertIn("--json", self.calls("lsblk")[0])

    def test_ntfs_drive_is_mounted_and_linked(self):
        self.devices([self.dev(fstype="ntfs")])
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(os.readlink(self.link), self.mounted_at())

    def test_second_run_is_quiet_and_changes_nothing(self):
        self.devices([self.dev()])
        self.run_tool()
        open(self.log, "w").close()
        r = self.run_tool()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.calls("udisksctl"), [])
        self.assertEqual(self.calls("notify-send"), [])
        self.assertEqual(os.readlink(self.link), self.mounted_at())

    def test_turned_off_does_nothing(self):
        self.settings(storage__shared_drive_enabled=False)
        self.devices([self.dev()])
        r = self.run_tool()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.lexists(self.link))

    def test_off_by_default(self):
        os.unlink(self.conf)
        self.devices([self.dev()])
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_label_match_ignores_case(self):
        # Windows shows drive names in any case; "Shared" typed in Windows still matches.
        self.devices([self.dev(label="Shared")])
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(os.readlink(self.link), self.mounted_at("Shared"))

    def test_drive_not_found_is_not_an_error(self):
        self.devices([self.dev(label="DATA")])
        r = self.run_tool()
        self.assertEqual(r.returncode, 0)
        self.assertIn("SHARED", r.stdout)
        self.assertEqual(self.calls("udisksctl"), [])
        self.assertEqual(self.calls("notify-send"), [])

    def test_link_setting_off_makes_no_link(self):
        self.settings(storage__shared_drive_enabled=True, storage__shared_drive_link=False)
        self.devices([self.dev()])
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(len(self.calls("udisksctl mount")), 1)
        self.assertFalse(os.path.lexists(self.link))

    def test_link_setting_off_removes_only_forges_own_link(self):
        self.devices([self.dev()])
        self.run_tool()
        self.settings(storage__shared_drive_enabled=True, storage__shared_drive_link=False)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertFalse(os.path.lexists(self.link))

    # refusals
    def assert_refused(self, r, *words):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        out = (r.stdout + r.stderr).lower()
        for w in words:
            self.assertIn(w.lower(), out)
        self.assertFalse(os.path.lexists(self.link))
        self.assertEqual(len(self.calls("notify-send")), 1)

    def test_fat32_is_refused(self):
        self.devices([self.dev(fstype="vfat")])
        self.assert_refused(self.run_tool(), "FAT32", "exFAT or NTFS")
        self.assertEqual(self.calls("udisksctl"), [])

    def test_linux_filesystems_are_refused(self):
        for fs in ("ext4", "btrfs", "xfs", "crypto_LUKS", "swap"):
            with self.subTest(fs=fs):
                os.path.exists(self.log) and os.unlink(self.log)
                self.devices([self.dev(fstype=fs)])
                self.assert_refused(self.run_tool(), "Windows can't read", "exFAT or NTFS")
                self.assertEqual(self.calls("udisksctl"), [])

    def test_unformatted_partition_is_refused(self):
        self.devices([self.dev(fstype=None)])
        self.assert_refused(self.run_tool(), "exFAT or NTFS")
        self.assertEqual(self.calls("udisksctl"), [])

    def test_bitlocker_is_refused(self):
        self.devices([self.dev(fstype="BitLocker")])
        self.assert_refused(self.run_tool(), "BitLocker")
        self.assertEqual(self.calls("udisksctl"), [])

    def test_windows_system_drive_is_refused_and_unmounted(self):
        self.devices([self.dev(fstype="ntfs", files=["Windows/System32", "Users"])])
        self.assert_refused(self.run_tool(), "Windows system drive")
        # Looked at read-only first, so the Windows drive is never opened for writing.
        self.assertEqual(self.calls("udisksctl"),
                         ["udisksctl mount -b /dev/nvme0n1p5 --no-user-interaction -o ro",
                          "udisksctl unmount -b /dev/nvme0n1p5 --no-user-interaction"])

    def test_ntfs_is_looked_at_read_only_then_opened_for_writing(self):
        self.devices([self.dev(fstype="ntfs")])
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls("udisksctl"),
                         ["udisksctl mount -b /dev/nvme0n1p5 --no-user-interaction -o ro",
                          "udisksctl unmount -b /dev/nvme0n1p5 --no-user-interaction",
                          "udisksctl mount -b /dev/nvme0n1p5 --no-user-interaction"])
        self.assertEqual(os.readlink(self.link), self.mounted_at())

    def test_a_different_drive_with_the_same_name_is_refused_until_accepted(self):
        self.devices([self.dev()])
        self.assertEqual(self.run_tool().returncode, 0)
        os.unlink(self.link)
        # Someone plugs in their own stick named SHARED while the real drive is away.
        self.devices([self.dev(path="/dev/sdb1", uuid="9999-EEEE")])
        open(self.log, "w").close()
        r = self.run_tool()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("forge-shared-drive accept", r.stdout + r.stderr)
        self.assertEqual(self.calls("udisksctl"), [])
        self.assertFalse(os.path.lexists(self.link))
        self.assertEqual(len(self.calls("notify-send")), 1)
        # The owner really replaced the drive: accept it once, then it is used.
        self.assertEqual(self.run_tool("accept").returncode, 0)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertTrue(os.path.islink(self.link))

    def state(self):
        p = os.path.join(self.home, ".local/state/forge/shared-drive.json")
        if not os.path.exists(p):
            return {}
        with open(p) as f:
            return json.load(f)

    def swap_drive(self, **extra):
        """The remembered drive is unplugged and another one named SHARED is plugged in."""
        if os.path.lexists(self.link):
            os.unlink(self.link)
        open(self.mountinfo, "w").close()
        self.devices([self.dev(path="/dev/sdb1", **extra)])
        open(self.log, "w").close()
        return self.run_tool()

    def assert_other_drive_refused(self, r):
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("forge-shared-drive accept", r.stdout + r.stderr)
        self.assertEqual(self.calls("udisksctl"), [])
        self.assertFalse(os.path.lexists(self.link))

    def test_drive_without_uuid_is_refused_once_a_drive_is_remembered(self):
        # Review finding: an exFAT or NTFS drive with serial 0 has no UUID and skipped the check.
        self.devices([self.dev(uuid="1111-AAAA")])
        self.assertEqual(self.run_tool().returncode, 0)
        self.assert_other_drive_refused(self.swap_drive(uuid=None))
        self.assert_other_drive_refused(self.swap_drive(uuid=""))

    def test_drive_without_any_id_is_used_but_not_remembered(self):
        self.devices([self.dev(uuid=None)])
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.readlink(self.link), self.mounted_at())
        self.assertIn("can't recognise this drive later", r.stdout)
        self.assertNotIn("drive_uuid", self.state())
        self.assertNotIn("drive_partuuid", self.state())
        # Nothing was remembered, so a later drive with an id is used and remembered.
        r = self.swap_drive(uuid="2222-BBBB")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state().get("drive_uuid"), "2222-BBBB")

    def test_partition_id_is_used_when_the_drive_has_no_uuid(self):
        part = "0f3c9a51-7d2e-4b8a-9c1d-5e6f7a8b9c0d"
        self.devices([self.dev(uuid=None, partuuid=part)])
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state().get("drive_partuuid"), part)
        self.assert_other_drive_refused(self.swap_drive(uuid=None, partuuid="11111111-02"))
        self.assert_other_drive_refused(self.swap_drive(uuid=None, partuuid=None))
        r = self.swap_drive(uuid=None, partuuid=part)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_both_ids_must_match_when_both_were_remembered(self):
        part = "0f3c9a51-7d2e-4b8a-9c1d-5e6f7a8b9c0d"
        self.devices([self.dev(uuid="1111-AAAA", partuuid=part)])
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual((self.state().get("drive_uuid"), self.state().get("drive_partuuid")), ("1111-AAAA", part))
        self.assert_other_drive_refused(self.swap_drive(uuid="1111-AAAA", partuuid="11111111-02"))
        self.assert_other_drive_refused(self.swap_drive(uuid="1111-AAAA", partuuid=None))
        # accept forgets both ids, so the replacement drive is used and remembered.
        self.assertEqual(self.run_tool("accept").returncode, 0)
        r = self.swap_drive(uuid="3333-CCCC", partuuid=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.state().get("drive_uuid"), self.state().get("drive_partuuid")), ("3333-CCCC", None))

    def test_windows_system_drive_found_in_any_letter_case(self):
        self.devices([self.dev(fstype="ntfs", files=["WINDOWS/system32"])])
        self.assert_refused(self.run_tool(), "Windows system drive")

    def test_already_mounted_system_drive_is_refused_but_left_mounted(self):
        mp = os.path.join(self.media, "SHARED")
        os.makedirs(os.path.join(mp, "Windows/System32"))
        self.devices([self.dev(fstype="ntfs", mountpoint=mp)])
        self.assert_refused(self.run_tool(), "Windows system drive")
        self.assertEqual(self.calls("udisksctl"), [])   # we did not mount it, so we don't unmount it

    def test_two_drives_with_the_same_name_are_refused(self):
        self.devices([self.dev(), self.dev(path="/dev/sda1", label="shared")])
        self.assert_refused(self.run_tool(), "more than one")
        self.assertEqual(self.calls("udisksctl"), [])

    def test_strange_device_path_from_lsblk_is_refused(self):
        for bad in ("/dev/../etc/shadow", "nvme0n1p5", "/dev/sd a1", "/dev/sda1\n--help"):
            with self.subTest(path=bad):
                os.path.exists(self.log) and os.unlink(self.log)
                self.devices([self.dev(path=bad)])
                r = self.run_tool()
                self.assertEqual(r.returncode, 1)
                self.assertEqual(self.calls("udisksctl"), [])
                self.assertNotIn("\n--help", r.stdout + r.stderr)

    def test_mounted_in_an_unexpected_place_is_refused(self):
        self.devices([self.dev(mountpoint="/etc")])
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("unexpected", (r.stdout + r.stderr).lower())
        self.assertFalse(os.path.lexists(self.link))

    def test_not_allowed_by_polkit_says_so(self):
        self.devices([self.dev(mount={"rc": 1, "err": "Error mounting /dev/nvme0n1p5: "
                                      "GDBus.Error:org.freedesktop.UDisks2.Error.NotAuthorized: "
                                      "Not authorized to perform operation"})])
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not allowed", (r.stdout + r.stderr).lower())
        self.assertNotIn("fast startup", (r.stdout + r.stderr).lower())

    # Windows hibernated or Fast Startup
    def test_dirty_ntfs_tells_the_user_and_forces_nothing(self):
        self.devices([self.dev(fstype="ntfs", mount={"rc": 1, "err": DIRTY_ERR})])
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        out = r.stdout + r.stderr
        self.assertIn("Fast Startup", out)
        self.assertIn("shut Windows down fully", out)
        [mount] = self.calls("udisksctl")      # the read-only look already said "in use": stop there
        self.assertNotIn("force", mount)
        self.assertEqual(mount.split()[-2:], ["-o", "ro"])   # the only option ever used
        [note] = self.calls("notify-send")
        self.assertIn("Fast Startup", note)
        self.assertIn("[--]", note)
        self.assertFalse(os.path.lexists(self.link))

    def test_ntfs3_dirty_message_is_recognised(self):
        self.devices([self.dev(fstype="ntfs", mount={
            "rc": 1, "err": 'Error mounting /dev/nvme0n1p5: volume is dirty and "force" flag is not set!'})])
        r = self.run_tool()
        self.assertIn("Fast Startup", r.stdout + r.stderr)

    def test_ntfs_mounted_read_only_is_reported(self):
        self.devices([self.dev(fstype="ntfs", ro=True)])
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("Fast Startup", r.stdout + r.stderr)
        self.assertIn("read-only", (r.stdout + r.stderr).lower())
        self.assertEqual(len(self.calls("notify-send")), 1)

    def test_same_problem_notifies_once_per_boot(self):
        self.devices([self.dev(fstype="ntfs", mount={"rc": 1, "err": DIRTY_ERR})])
        self.run_tool()
        r = self.run_tool()
        self.assertIn("Fast Startup", r.stdout + r.stderr)   # still says it in the log
        self.assertEqual(len(self.calls("notify-send")), 1)

    # ~/Shared safety
    def test_existing_folder_is_kept(self):
        os.makedirs(self.link)
        with open(os.path.join(self.link, "notes.txt"), "w") as f:
            f.write("mine")
        self.devices([self.dev()])
        r = self.run_tool()
        self.assertTrue(os.path.isdir(self.link) and not os.path.islink(self.link))
        self.assertEqual(open(os.path.join(self.link, "notes.txt")).read(), "mine")
        self.assertIn("already", (r.stdout + r.stderr).lower())
        self.assertEqual(len(self.calls("udisksctl mount")), 1)   # the drive is still mounted

    def test_existing_file_is_kept(self):
        with open(self.link, "w") as f:
            f.write("mine")
        self.devices([self.dev()])
        self.run_tool()
        self.assertFalse(os.path.islink(self.link))
        self.assertEqual(open(self.link).read(), "mine")

    def test_someone_elses_symlink_is_left_alone(self):
        other = os.path.join(self.t, "elsewhere")
        os.makedirs(other)
        os.symlink(other, self.link)
        self.devices([self.dev()])
        r = self.run_tool()
        self.assertEqual(os.readlink(self.link), other)
        self.assertIn("already", (r.stdout + r.stderr).lower())

    def test_forges_old_link_is_repaired(self):
        # Mounted at SHARED first, later udisks picks SHARED1 (for example after a crash).
        self.devices([self.dev()])
        self.run_tool()
        devs = json.load(open(self.disks))
        devs[0]["mountpoint"] = os.path.join(self.media, "SHARED1")
        os.makedirs(devs[0]["mountpoint"])
        self.devices(devs)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(os.readlink(self.link), os.path.join(self.media, "SHARED1"))

    def test_dangling_symlink_not_made_by_forge_is_left_alone(self):
        os.symlink("/nowhere/at/all", self.link)
        self.devices([self.dev()])
        self.run_tool()
        self.assertEqual(os.readlink(self.link), "/nowhere/at/all")

    def test_edited_state_file_cannot_make_forge_replace_a_folder(self):
        state = os.path.join(self.home, ".local/state/forge")
        os.makedirs(state)
        with open(os.path.join(state, "shared-drive.json"), "w") as f:
            json.dump({"link_target": self.link}, f)
        os.makedirs(self.link)
        self.devices([self.dev()])
        self.run_tool()
        self.assertTrue(os.path.isdir(self.link) and not os.path.islink(self.link))

    # labels
    def test_odd_but_valid_label_works(self):
        self.settings(storage__shared_drive_enabled=True, storage__shared_drive_label="Proj_2026-x")
        self.devices([self.dev(label="PROJ_2026-X")])
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(os.readlink(self.link), self.mounted_at("PROJ_2026-X"))

    def test_invalid_labels_are_refused_before_looking_at_drives(self):
        for bad in ("My Drive", "../SHARED", "", "x" * 33, "SHARED*", "ÄBC", 7, None, ["SHARED"]):
            with self.subTest(label=bad):
                os.path.exists(self.log) and os.unlink(self.log)
                self.settings(storage__shared_drive_enabled=True, storage__shared_drive_label=bad)
                self.devices([self.dev()])
                r = self.run_tool()
                self.assertEqual(r.returncode, 1)
                self.assertIn("drive name", (r.stdout + r.stderr).lower())
                self.assertEqual(self.calls("lsblk"), [])
                self.assertEqual(self.calls("udisksctl"), [])

    def test_label_with_control_characters_is_refused_and_never_echoed(self):
        for bad in ("SHA\nRED", "SHA\x1b[2JRED", "SHARED\r", "SH\x00ARED", "SHA\x7fRED"):
            with self.subTest(label=repr(bad)):
                os.path.exists(self.log) and os.unlink(self.log)
                self.settings(storage__shared_drive_enabled=True, storage__shared_drive_label=bad)
                self.devices([self.dev(label=bad)])
                r = self.run_tool()
                self.assertEqual(r.returncode, 1)
                out = r.stdout + r.stderr
                self.assertNotIn("\x1b", out)
                self.assertNotIn("\r", out)
                self.assertNotIn("\x00", out)
                self.assertNotIn("SHA\nRED", out)
                self.assertEqual(self.calls("udisksctl"), [])
                for note in self.calls("notify-send"):
                    self.assertNotIn("\x1b", note)

    def test_hostile_label_on_another_drive_is_not_echoed(self):
        self.devices([self.dev(label="EVIL\x1b]0;x\x07", path="/dev/sdb1"), self.dev(fstype="vfat")])
        r = self.run_tool()
        self.assertNotIn("\x1b", r.stdout + r.stderr)

    def test_schema_rejects_bad_labels_and_keeps_good_ones(self):
        s = forgecfg.settings_by_key(json.load(open(SCHEMA)))["storage.shared_drive_label"]
        self.assertEqual(forgecfg.coerce(s, "Proj_2026-x"), "Proj_2026-x")
        self.assertEqual(forgecfg.coerce(s, "x" * 32), "x" * 32)
        for bad in ("My Drive", "x" * 33, "SHA\nRED", "SHA\x1bRED", "a/b"):
            with self.assertRaises(ValueError):
                forgecfg.coerce(s, bad)

    # status
    def test_status_json_reports_without_changing_anything(self):
        self.devices([self.dev()])
        r = self.run_tool("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        st = json.loads(r.stdout)
        self.assertEqual(st["state"], "not_mounted")
        self.assertEqual((st["enabled"], st["label"], st["device"], st["fstype"]),
                         (True, "SHARED", "/dev/nvme0n1p5", "exfat"))
        self.assertEqual(self.calls("udisksctl"), [])
        self.assertFalse(os.path.lexists(self.link))
        self.run_tool()
        st = json.loads(self.run_tool("status", "--json").stdout)
        self.assertEqual((st["state"], st["mountpoint"], st["link"]), ("mounted", self.mounted_at(), "ok"))

    def test_status_json_for_each_problem(self):
        cases = [([self.dev(fstype="vfat")], "refused"), ([self.dev(label="X")], "not_found"),
                 ([self.dev(), self.dev(path="/dev/sda1")], "several")]
        for devs, state in cases:
            with self.subTest(state=state):
                self.devices(devs)
                st = json.loads(self.run_tool("status", "--json").stdout)
                self.assertEqual(st["state"], state)
                self.assertTrue(st["message"])
        self.settings(storage__shared_drive_enabled=False)
        self.assertEqual(json.loads(self.run_tool("status", "--json").stdout)["state"], "off")
        self.settings(storage__shared_drive_enabled=True, storage__shared_drive_label="bad label")
        self.assertEqual(json.loads(self.run_tool("status", "--json").stdout)["state"], "bad_label")
        self.assertEqual(self.calls("notify-send"), [])

    def test_status_text_is_plain(self):
        self.devices([self.dev()])
        r = self.run_tool("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("SHARED", r.stdout)

    def test_unknown_command_shows_help(self):
        r = self.run_tool("format")
        self.assertEqual(r.returncode, 2)
        self.assertIn("status", r.stdout + r.stderr)


class SharedDrivePackagingTest(unittest.TestCase):
    def test_settings_exist_with_the_right_defaults(self):
        schema = json.load(open(SCHEMA))
        [g] = [g for g in schema["groups"] if g["id"] == "storage"]
        self.assertEqual(g["label"], "Storage and shared drive")
        keys = {s["key"]: s for s in schema["settings"]}
        for k, typ, default in (("storage.shared_drive_enabled", "toggle", False),
                                ("storage.shared_drive_label", "text", "SHARED"),
                                ("storage.shared_drive_link", "toggle", True)):
            s = keys[k]
            self.assertEqual((s["type"], s["default"], s["group"]), (typ, default, "storage"))
            for field in ("label", "help", "tags"):
                self.assertTrue(s[field], (k, field))

    def test_tool_is_executable_in_the_image(self):
        with open(os.path.join(ROOT, "files/scripts/finalize.sh")) as f:
            self.assertIn("/usr/bin/forge-shared-drive", f.read())

    def test_user_units_run_the_tool_at_login_and_on_plug_in(self):
        svc = open(os.path.join(UNITS, "forge-shared-drive.service")).read()
        path = open(os.path.join(UNITS, "forge-shared-drive.path")).read()
        self.assertIn("ExecStart=/usr/bin/forge-shared-drive", svc)
        self.assertIn("graphical-session.target", svc)
        self.assertIn("StartLimitIntervalSec=0", svc)
        self.assertIn("PathChanged=/dev/disk/by-label", path)

    def run_apply(self, values):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        conf = os.path.join(root, "etc/forge/settings.json")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w") as f:
            json.dump(values, f)
        env = dict(os.environ, FORGE_ROOT=root, FORGE_DRYRUN="1", FORGE_CONF=conf, FORGE_SCHEMA=SCHEMA,
                   PYTHONPATH=LIB)
        r = subprocess.run([sys.executable, APPLY], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return root, r.stdout

    RULE = "etc/polkit-1/rules.d/90-forge-shared-drive.rules"

    def test_apply_turns_units_and_mount_permission_on_and_off(self):
        root, out = self.run_apply({"storage.shared_drive_enabled": True,
                                    "storage.shared_drive_label": "Proj_2026-x"})
        self.assertIn("would run: systemctl enable --global forge-shared-drive.service", out)
        self.assertIn("would run: systemctl enable --global forge-shared-drive.path", out)
        rule = open(os.path.join(root, self.RULE)).read()
        self.assertIn('"PROJ_2026-X"', rule)
        self.assertNotIn("#", rule)   # polkit rules are JavaScript: a "#" header would break them
        root2, out = self.run_apply({"storage.shared_drive_label": "Proj_2026-x"})
        self.assertIn("would run: systemctl disable --global forge-shared-drive.path", out)
        self.assertFalse(os.path.exists(os.path.join(root2, self.RULE)))

    def test_apply_refuses_a_bad_label(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        f = os.path.join(root, "in.json")
        with open(f, "w") as fh:
            json.dump({"storage.shared_drive_enabled": True,
                       "storage.shared_drive_label": 'X"});polkit.addRule(function(){return "yes"'}, fh)
        env = dict(os.environ, FORGE_ROOT=root, FORGE_DRYRUN="1", FORGE_SCHEMA=SCHEMA, PYTHONPATH=LIB,
                   FORGE_CONF=os.path.join(root, "etc/forge/settings.json"))
        env.pop("SUDO_USER", None)
        r = subprocess.run([sys.executable, APPLY, "--import", f], env=env, capture_output=True,
                           text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(root, self.RULE)))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_polkit_rule_is_valid_javascript_and_only_allows_the_shared_drive(self):
        root, _ = self.run_apply({"storage.shared_drive_enabled": True})
        rule = open(os.path.join(root, self.RULE)).read()
        harness = """
const results = [];
const polkit = { Result: { YES: "yes" }, addRule(f) { this.f = f; } };
%s
function ask(id, details, groups) {
  const action = { id, lookup: (k) => details[k] };
  const subject = { isInGroup: (g) => groups.includes(g), active: true, local: true };
  return polkit.f(action, subject) || "no-answer";
}
const M = "org.freedesktop.udisks2.filesystem-mount-system";
console.log(JSON.stringify([
  ask(M, {"id.label": "SHARED", "id.type": "exfat"}, ["wheel"]),
  ask(M, {"id.label": "shared", "id.type": "ntfs"}, ["wheel"]),
  ask(M, {"id.label": "SHARED", "id.type": "vfat"}, ["wheel"]),
  ask(M, {"id.label": "SHARED", "id.type": "exfat"}, ["users"]),
  ask(M, {"id.label": "WINDOWS", "id.type": "ntfs"}, ["wheel"]),
  ask(M, {"id.type": "ntfs"}, ["wheel"]),
  ask("org.freedesktop.udisks2.modify-device", {"id.label": "SHARED", "id.type": "exfat"}, ["wheel"]),
  ask(M, {"id.label": "Shared", "id.type": "ntfs"}, ["wheel"]),
  ask(M, {"id.label": "\u017fhared", "id.type": "ntfs"}, ["wheel"]),
  ask(M, {"id.label": "SHARED\u200b", "id.type": "ntfs"}, ["wheel"]),
  ask(M, {"id.label": "shar\u0435d", "id.type": "ntfs"}, ["wheel"]),
]));
""" % rule
        r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout),
                         ["yes", "yes", "no-answer", "no-answer", "no-answer", "no-answer", "no-answer",
                          "yes", "no-answer", "no-answer", "no-answer"])
        # Lookalikes such as the long s fold to "S" with toUpperCase: the rule only takes ASCII labels.
        self.assertIn("/^[A-Za-z0-9_-]+$/", rule)


class SharedDriveDoctorTest(unittest.TestCase):
    def setUp(self):
        from forgecore import doctor
        self.D = doctor

    def storage(self, answer):
        def run(cmd, **kw):
            if cmd[:3] == ["forge-shared-drive", "status", "--json"]:
                if answer is None:
                    raise FileNotFoundError(cmd[0])
                return subprocess.CompletedProcess(cmd, 0, json.dumps(answer), "")
            raise FileNotFoundError(cmd[0])
        return self.D.storage("", run)

    def test_mounted_drive_is_one_line(self):
        sec = self.storage({"state": "mounted", "label": "SHARED", "fstype": "exfat",
                            "device": "/dev/nvme0n1p5", "mountpoint": "/run/media/a/SHARED",
                            "message": "ok"})
        line = [i for i in sec["items"] if i.startswith("Shared drive")]
        self.assertEqual(len(line), 1)
        self.assertIn("/run/media/a/SHARED", line[0])

    def test_problem_is_a_warning(self):
        sec = self.storage({"state": "dirty", "label": "SHARED", "message": "Turn off Fast Startup."})
        self.assertEqual(sec["status"], "warn")
        self.assertTrue(any("Fast Startup" in i for i in sec["items"]))

    def test_missing_tool_adds_nothing(self):
        sec = self.storage(None)
        self.assertFalse(any("Shared drive" in i for i in sec["items"]))


if __name__ == "__main__":
    unittest.main()
