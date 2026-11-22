"""Tests for forge-hwcheck using a fake /proc, /sys and fake lspci."""
import json
import os
import shutil
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
        self.assertTrue(notice["reapplied"])
        text = " ".join(notice["changes"])
        self.assertIn("RAM (GB) changed from 64 to 128", text)
        self.assertIn("RTX 5090", text)
        self.assertTrue(os.path.exists(self.applied))
        log = open(self.state + "/hardware-changes.log").read()
        self.assertEqual(len(log.strip().splitlines()), 2)
        self.assertNotIn("\u2014", r.stdout)

    def test_a_failed_reapply_is_not_reported_as_done(self):
        self.run_check()
        write(self.bin + "/fake-apply", "#!/bin/sh\necho 'another forge-apply is still running' >&2\nexit 1\n")
        self.set_ram(128)
        self.run_check()
        notice = json.load(open(self.state + "/hardware-notice.json"))
        self.assertIs(notice["reapplied"], False)

    def test_a_notice_is_written_even_if_the_reapply_cannot_start(self):
        self.run_check()
        os.unlink(self.bin + "/fake-apply")
        self.set_ram(128)
        self.run_check()
        notice = json.load(open(self.state + "/hardware-notice.json"))
        self.assertIs(notice["reapplied"], False)
        self.assertTrue(any("RAM" in c for c in notice["changes"]))

    def test_notice_is_marked_pending_during_the_reapply_and_written_in_one_step(self):
        self.run_check()
        seen = os.path.join(self.t, "seen")
        write(self.bin + "/fake-apply", f"#!/bin/sh\ncat {self.state}/hardware-notice.json > {seen}\n")
        os.chmod(self.bin + "/fake-apply", 0o755)
        self.set_ram(128)
        self.run_check()
        during = json.load(open(seen))
        self.assertIs(during["pending"], True)
        after = json.load(open(self.state + "/hardware-notice.json"))
        self.assertIs(after["pending"], False)
        self.assertIs(after["reapplied"], True)
        self.assertEqual([f for f in os.listdir(self.state) if f.startswith(".")], [])

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

    def test_mouse_or_controller_battery_does_not_make_a_laptop(self):
        # Wireless mice and game controllers show up as batteries with scope "Device".
        mouse = self.sys + "/sys/class/power_supply/hidpp_battery_0"
        write(mouse + "/type", "Battery")
        write(mouse + "/scope", "Device")
        write(mouse + "/capacity", "40")
        self.run_check()
        prof = json.load(open(self.state + "/hardware.json"))
        self.assertEqual(prof["type"], "desktop")
        self.assertFalse(prof["battery"])
        self.assertEqual(prof["details"]["battery"], [])
        shutil.rmtree(mouse)                     # unplugged: no "hardware changed"
        self.run_check()
        self.assertFalse(os.path.exists(self.state + "/hardware-notice.json"))

    def add_usb_disk(self, name, model, removable):
        # Real sysfs: /sys/block/sdX is a symlink into the device tree, through usbN for USB disks.
        real = (self.sys + "/sys/devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/host6/"
                f"target6:0:0/6:0:0:0/block/{name}")
        write(real + "/size", str(32_000_000_000 // 512))
        write(real + "/removable", removable)
        write(real + "/device/model", model)
        os.symlink(real, self.sys + f"/sys/block/{name}")

    def test_r10_usb_and_removable_disks_are_not_hardware(self):
        self.run_check()
        # The FORGEKEY stick: a USB disk that says it is removable.
        self.add_usb_disk("sdb", "FORGEKEY Stick", "1")
        # Many USB SSDs say removable 0, so the usb path must be enough on its own.
        self.add_usb_disk("sdc", "USB SSD", "0")
        # A removable disk that is not on usb (for example an SD card slot).
        write(self.sys + "/sys/block/mmcblk0/size", str(64_000_000_000 // 512))
        write(self.sys + "/sys/block/mmcblk0/removable", "1")
        # An empty card reader slot.
        write(self.sys + "/sys/block/sdd/size", "0")
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        prof = json.load(open(self.state + "/hardware.json"))
        self.assertEqual(prof["disks"], ["WD_BLACK SN850X (2000 GB)"])
        self.assertFalse(os.path.exists(self.state + "/hardware-notice.json"))
        self.assertFalse(os.path.exists(self.applied))

    def test_r10_new_internal_disk_still_detected(self):
        self.run_check()
        write(self.sys + "/sys/block/sda/size", str(1_000_000_000_000 // 512))
        write(self.sys + "/sys/block/sda/removable", "0")
        write(self.sys + "/sys/block/sda/device/model", "Samsung 870 EVO")
        self.run_check()
        prof = json.load(open(self.state + "/hardware.json"))
        self.assertIn("Samsung 870 EVO (1000 GB)", prof["disks"])
        self.assertTrue(os.path.exists(self.applied))


    def add_linked_disk(self, name, devpath, size_gb, model=None, extra=None):
        real = self.sys + "/sys/devices/" + devpath + "/block/" + name
        write(real + "/size", str(size_gb * 1_000_000_000 // 512))
        write(real + "/removable", "0")
        if model:
            write(real + "/device/model", model)
        for rel, text in (extra or {}).items():
            write(self.sys + "/sys/devices/" + rel, text)
        os.symlink(real, self.sys + f"/sys/block/{name}")

    def test_r10_sd_card_and_thunderbolt_drive_are_not_hardware(self):
        self.run_check()
        # An SD card in a PCI card reader: the kernel reports removable 0 for mmc cards.
        self.add_linked_disk("mmcblk1", "pci0000:00/0000:00:1c.0/0000:02:00.0/mmc_host/mmc1/mmc1:aaaa", 64,
                             extra={"pci0000:00/0000:00:1c.0/0000:02:00.0/mmc_host/mmc1/mmc1:aaaa/block/"
                                    "mmcblk1/device/type": "SD"})
        # An NVMe drive in a Thunderbolt / USB4 enclosure: PCIe behind an external-facing port.
        self.add_linked_disk("nvme1n1", "pci0000:00/0000:00:07.0/0000:05:00.0/nvme/nvme1", 1000, "TB SSD",
                             extra={"pci0000:00/0000:00:07.0/0000:05:00.0/removable": "removable"})
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        prof = json.load(open(self.state + "/hardware.json"))
        self.assertEqual(prof["disks"], ["WD_BLACK SN850X (2000 GB)"])
        self.assertFalse(os.path.exists(self.applied))

    def test_r10_internal_emmc_still_counts(self):
        self.run_check()
        self.add_linked_disk("mmcblk0", "pci0000:00/0000:00:1a.0/mmc_host/mmc0/mmc0:0001", 128,
                             extra={"pci0000:00/0000:00:1a.0/mmc_host/mmc0/mmc0:0001/block/mmcblk0/device/type": "MMC",
                                    "pci0000:00/0000:00:1a.0/removable": "fixed"})
        self.run_check()
        prof = json.load(open(self.state + "/hardware.json"))
        self.assertEqual(len(prof["disks"]), 2, prof["disks"])
        self.assertTrue(os.path.exists(self.applied))


    def test_r10_round2_sdcombo_and_multipath_thunderbolt_are_not_hardware(self):
        self.run_check()
        self.add_linked_disk("mmcblk2", "pci0000:00/0000:00:1c.0/0000:03:00.0/mmc_host/mmc2/mmc2:bbbb", 32,
                             extra={"pci0000:00/0000:00:1c.0/0000:03:00.0/mmc_host/mmc2/mmc2:bbbb/block/"
                                    "mmcblk2/device/type": "SDcombo"})
        # NVMe multipath: the disk sits under the virtual nvme-subsystem; its controller is behind
        # a Thunderbolt port.
        sub = "virtual/nvme-subsystem/nvme-subsys1"
        self.add_linked_disk("nvme1n1", sub, 1000, "TB SSD")
        ctrl = self.sys + "/sys/devices/pci0000:00/0000:00:07.0/0000:05:00.0/nvme/nvme1"
        os.makedirs(ctrl)
        write(self.sys + "/sys/devices/pci0000:00/0000:00:07.0/removable", "removable")
        os.symlink(ctrl, self.sys + "/sys/devices/" + sub + "/nvme1")
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        prof = json.load(open(self.state + "/hardware.json"))
        self.assertEqual(prof["disks"], ["WD_BLACK SN850X (2000 GB)"])

    def test_r10_round2_internal_multipath_nvme_still_counts(self):
        self.run_check()
        sub = "virtual/nvme-subsystem/nvme-subsys2"
        self.add_linked_disk("nvme2n1", sub, 500, "Internal SSD")
        ctrl = self.sys + "/sys/devices/pci0000:00/0000:00:01.1/0000:06:00.0/nvme/nvme2"
        os.makedirs(ctrl)
        write(self.sys + "/sys/devices/pci0000:00/0000:00:01.1/removable", "fixed")
        os.symlink(ctrl, self.sys + "/sys/devices/" + sub + "/nvme2")
        self.run_check()
        prof = json.load(open(self.state + "/hardware.json"))
        self.assertIn("Internal SSD (500 GB)", prof["disks"])


    # P0: more hardware detail, and compatibility checks.

    def add_gpu_sysfs(self, card, vendor, driver, extra=None):
        dev = self.sys + f"/sys/devices/pci0000:00/0000:0{card}:00.0"
        write(dev + "/vendor", vendor)
        write(dev + "/device", "0x1234")
        os.makedirs(self.sys + f"/sys/bus/pci/drivers/{driver}", exist_ok=True)
        os.symlink(self.sys + f"/sys/bus/pci/drivers/{driver}", dev + "/driver")
        for k, v in (extra or {}).items():
            write(dev + "/" + k, v)
        os.makedirs(self.sys + "/sys/class/drm", exist_ok=True)
        os.makedirs(self.sys + f"/sys/class/drm/card{card}")
        os.symlink(dev, self.sys + f"/sys/class/drm/card{card}/device")

    def full_box(self):
        write(self.sys + "/proc/cpuinfo", "processor : 0\nmodel name : Ryzen 7 7700\n"
              "flags : fpu sse4_2 fma f16c avx avx2 avx512f avx512_vnni\n")
        self.add_gpu_sysfs(1, "0x10de", "nvidia")
        write(self.bin + "/nvidia-smi", "#!/bin/sh\necho 12227\n")
        os.chmod(self.bin + "/nvidia-smi", 0o755)
        bat = self.sys + "/sys/class/power_supply/BAT0"
        for k, v in {"type": "Battery", "capacity": "81", "status": "Discharging", "energy_full": "45000000",
                     "energy_full_design": "50000000", "cycle_count": "212"}.items():
            write(bat + "/" + k, v)
        for name, wifi in (("wlp3s0", True), ("enp4s0", False)):
            d = self.sys + f"/sys/class/net/{name}"
            os.makedirs(d + "/device")
            if wifi:
                os.makedirs(d + "/wireless")
        os.makedirs(self.sys + "/sys/class/net/lo")          # loopback: no device, ignored
        os.makedirs(self.sys + "/sys/class/bluetooth/hci0")
        write(self.sys + "/proc/asound/cards", " 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n"
              "                      HDA Intel PCH at 0xfe200000 irq 136\n")
        os.makedirs(self.sys + "/sys/class/accel/accel0")

    def test_details_are_recorded(self):
        self.full_box()
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.load(open(self.state + "/hardware.json"))["details"]
        self.assertEqual(d["gpus"][0]["vendor"], "NVIDIA")
        self.assertEqual(d["gpus"][0]["driver"], "nvidia")
        self.assertEqual(d["gpus"][0]["vram_mb"], 12227)
        self.assertEqual(set(d["cpu_ai"]), {"avx2", "avx512f", "avx512_vnni", "fma", "f16c"})
        self.assertTrue(d["npu"])
        self.assertEqual(d["battery"][0]["health_pct"], 90)
        self.assertEqual(d["battery"][0]["cycles"], 212)
        self.assertEqual(sorted((n["name"], n["kind"]) for n in d["network"]),
                         [("enp4s0", "ethernet"), ("wlp3s0", "wifi")])
        self.assertEqual(d["bluetooth"], ["hci0"])
        self.assertEqual(d["audio"], ["HDA Intel PCH"])

    def test_upgrade_from_an_older_profile_is_not_a_hardware_change(self):
        self.run_check()
        prof = json.load(open(self.state + "/hardware.json"))
        for k in ("details", "checks"):
            prof.pop(k, None)
        with open(self.state + "/hardware.json", "w") as f:
            json.dump(prof, f)
        self.run_check()
        self.assertFalse(os.path.exists(self.state + "/hardware-notice.json"))
        self.assertFalse(os.path.exists(self.applied))

    def test_changing_details_are_not_a_hardware_change(self):
        self.full_box()
        self.run_check()
        write(self.sys + "/sys/class/power_supply/BAT0/capacity", "40")
        write(self.sys + "/sys/class/power_supply/BAT0/energy_full", "44000000")
        self.run_check()
        self.assertFalse(os.path.exists(self.applied))

    def test_nvidia_gpu_on_the_universal_image_is_flagged_once(self):
        self.full_box()
        write(self.sys + "/usr/share/ublue-os/image-info.json",
              '{"image-name": "bazzite", "image-flavor": "main"}')
        self.run_check()
        checks = json.load(open(self.state + "/hardware.json"))["checks"]
        self.assertTrue(any("NVIDIA" in c and "forge-os" in c for c in checks), checks)
        notice = json.load(open(self.state + "/hardware-notice.json"))
        self.assertTrue(any("NVIDIA" in c for c in notice["changes"]))
        self.assertFalse(notice["reapplied"])     # only a check: nothing was re-applied
        os.unlink(self.state + "/hardware-notice.json")
        self.run_check()
        self.assertFalse(os.path.exists(self.state + "/hardware-notice.json"), "the same check again")

    def test_nvidia_image_without_nvidia_gpu_is_flagged(self):
        self.add_gpu_sysfs(1, "0x1002", "amdgpu", {"mem_info_vram_total": str(16 * 1024 ** 3)})
        write(self.sys + "/usr/share/ublue-os/image-info.json",
              '{"image-name": "bazzite-nvidia-open", "image-flavor": "nvidia-open"}')
        self.run_check()
        prof = json.load(open(self.state + "/hardware.json"))
        self.assertEqual(prof["details"]["gpus"][0]["vram_mb"], 16384)
        self.assertTrue(any("universal" in c for c in prof["checks"]), prof["checks"])

    def test_matching_image_has_no_check(self):
        self.add_gpu_sysfs(1, "0x10de", "nvidia")
        write(self.sys + "/usr/share/ublue-os/image-info.json", '{"image-flavor": "nvidia-open"}')
        self.run_check()
        self.assertEqual(json.load(open(self.state + "/hardware.json"))["checks"], [])

    def test_usb_unlock_on_without_an_encrypted_disk_is_flagged(self):
        write(self.sys + "/etc/forge/settings.json", '{"security.usb_unlock": true}')
        self.run_check()
        checks = json.load(open(self.state + "/hardware.json"))["checks"]
        self.assertTrue(any("encrypted" in c for c in checks), checks)


if __name__ == "__main__":
    unittest.main()
