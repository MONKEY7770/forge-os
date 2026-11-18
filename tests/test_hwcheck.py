"""Tests for forge-hwcheck using a fake /proc, /sys and fake lspci."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HWCHECK = os.path.join(ROOT, "files/system/usr/libexec/forge-hwcheck")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class HwCheckTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.sys = os.path.join(self.t, "sys")
        self.state = os.path.join(self.t, "state")
        self.bin = os.path.join(self.t, "bin")
        self.applied = os.path.join(self.t, "applied")
        write(self.sys + "/proc/cpuinfo", "processor : 0\nmodel name : Ryzen 7 7700\nprocessor : 1\n")
        self.set_ram(64)
        write(self.sys + "/sys/block/nvme0n1/size", str(2_000_000_000_000 // 512))
        write(self.sys + "/sys/block/nvme0n1/device/model", "WD_BLACK SN850X")
        write(self.sys + "/sys/block/loop0/size", "100")
        write(self.sys + "/sys/class/dmi/id/chassis_type", "3")
        self.set_gpu("NVIDIA Corporation", "GB205 [GeForce RTX 5070]")
        write(self.bin + "/systemd-detect-virt", "#!/bin/sh\necho none\n")
        write(self.bin + "/fake-apply", f"#!/bin/sh\necho applied > {self.applied}\n")
        for f in os.listdir(self.bin):
            os.chmod(os.path.join(self.bin, f), 0o755)

    def set_ram(self, gb):
        write(self.sys + "/proc/meminfo", f"MemTotal: {gb * 1024 * 1024} kB\n")

    def set_gpu(self, vendor, name):
        write(self.bin + "/lspci",
              f'#!/bin/sh\necho \'01:00.0 "VGA compatible controller" "{vendor}" "{name}" -r00 "" ""\'\n')
        os.chmod(self.bin + "/lspci", 0o755)

    def run_check(self):
        env = dict(os.environ, FORGE_SYS=self.sys, FORGE_HW_STATE=self.state,
                   FORGE_HW_APPLY=self.bin + "/fake-apply", PATH=self.bin + ":" + os.environ["PATH"])
        return subprocess.run([sys.executable, HWCHECK], env=env, capture_output=True, text=True)

    def test_first_run_records_profile_without_notice(self):
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        prof = json.load(open(self.state + "/hardware.json"))
        self.assertEqual(prof["type"], "desktop")
        self.assertEqual(prof["ram_gb"], 64)
        self.assertEqual(prof["disks"], ["WD_BLACK SN850X (2000 GB)"])
        self.assertIn("RTX 5070", prof["gpus"][0])
        self.assertFalse(os.path.exists(self.state + "/hardware-notice.json"))
        self.assertFalse(os.path.exists(self.applied))

    def test_no_change_does_nothing(self):
        self.run_check()
        self.run_check()
        self.assertFalse(os.path.exists(self.applied))

    def test_ram_and_gpu_upgrade_detected_and_applied(self):
        self.run_check()
        self.set_ram(128)
        self.set_gpu("NVIDIA Corporation", "GB202 [GeForce RTX 5090]")
        r = self.run_check()
        notice = json.load(open(self.state + "/hardware-notice.json"))
        text = " ".join(notice["changes"])
        self.assertIn("RAM (GB) changed from 64 to 128", text)
        self.assertIn("RTX 5090", text)
        self.assertTrue(os.path.exists(self.applied))
        log = open(self.state + "/hardware-changes.log").read()
        self.assertEqual(len(log.strip().splitlines()), 2)
        self.assertNotIn("\u2014", r.stdout)

    def test_laptop_and_vm_detection(self):
        write(self.sys + "/sys/class/power_supply/BAT0/type", "Battery")
        self.run_check()
        self.assertEqual(json.load(open(self.state + "/hardware.json"))["type"], "laptop")
        write(self.bin + "/systemd-detect-virt", "#!/bin/sh\necho kvm\n")
        os.chmod(self.bin + "/systemd-detect-virt", 0o755)
        self.run_check()
        prof = json.load(open(self.state + "/hardware.json"))
        self.assertEqual(prof["type"], "virtual machine")
        self.assertEqual(prof["virtualization"], "kvm")


if __name__ == "__main__":
    unittest.main()
