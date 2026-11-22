"""forge-doctor: the hardware compatibility and health report (P1)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-doctor")
sys.path.insert(0, LIB)

from forgecore import doctor as D  # noqa: E402


def write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


class FakeRun:
    """Answers commands from a table; anything else is "not installed"."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        for prefix, (rc, out) in self.answers.items():
            if " ".join(cmd).startswith(prefix):
                return subprocess.CompletedProcess(cmd, rc, out, "")
        raise FileNotFoundError(cmd[0])


RPM_OSTREE = json.dumps({"deployments": [{"booted": True, "version": "45.20261119",
                                           "container-image-reference":
                                           "ostree-image-signed:docker://ghcr.io/monkey7770/forge-os:latest"}]})


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.hw = os.path.join(self.root, "var/lib/forge-hw")
        write(os.path.join(self.hw, "hardware.json"), json.dumps({
            "type": "desktop", "cpu": "Ryzen 7 7700 (16 threads)", "ram_gb": 64, "gpus": ["NVIDIA RTX 5070"],
            "disks": ["SSD (2000 GB)"], "battery": False,
            "details": {"gpus": [{"vendor": "NVIDIA", "driver": "nvidia", "vram_mb": 12227}],
                        "cpu_ai": ["avx2", "avx512f"], "npu": False, "battery": [],
                        "network": [{"name": "enp4s0", "kind": "ethernet"}], "bluetooth": ["hci0"], "audio": []},
            "checks": []}))
        write(os.path.join(self.root, "proc/cpuinfo"), "flags : fpu svm avx2\n")
        os.makedirs(os.path.join(self.root, "dev"))
        write(os.path.join(self.root, "dev/kvm"), "")
        os.makedirs(os.path.join(self.root, "sys/kernel/iommu_groups/0"))
        write(os.path.join(self.root, "etc/crypttab"), "luks-root UUID=abc none discard\n")
        self.run = FakeRun({
            "rpm-ostree status --json": (0, RPM_OSTREE),
            "mokutil --sb-state": (0, "SecureBoot enabled\n"),
            "systemctl is-active": (0, "active\n"),
        })

    def report(self):
        return D.build_report(root=self.root, run=self.run)

    def section(self, rep, name):
        [s] = [s for s in rep["sections"] if s["name"] == name]
        return s

    def test_report_has_every_section(self):
        rep = self.report()
        names = [s["name"] for s in rep["sections"]]
        for n in ("Image", "Hardware", "AI readiness", "Security", "Virtualization", "Services", "Storage"):
            self.assertIn(n, names)
        for s in rep["sections"]:
            self.assertIn(s["status"], ("ok", "warn", "unknown", "info"))

    def test_healthy_box_is_ok(self):
        rep = self.report()
        self.assertEqual(self.section(rep, "Image")["status"], "ok")
        self.assertEqual(self.section(rep, "Security")["status"], "ok")
        self.assertEqual(self.section(rep, "Virtualization")["status"], "ok")
        ai = self.section(rep, "AI readiness")
        self.assertEqual(ai["status"], "ok")
        self.assertTrue(any("12" in i for i in ai["items"]), ai)   # 12 GB VRAM shown

    def test_problems_become_warnings(self):
        self.run.answers["rpm-ostree status --json"] = (0, RPM_OSTREE.replace("ostree-image-signed", "ostree-unverified-registry"))
        self.run.answers["mokutil --sb-state"] = (0, "SecureBoot disabled\n")
        os.unlink(os.path.join(self.root, "dev/kvm"))
        hw = json.load(open(os.path.join(self.hw, "hardware.json")))
        hw["checks"] = ["This PC has an NVIDIA GPU, but runs the universal image."]
        write(os.path.join(self.hw, "hardware.json"), json.dumps(hw))
        rep = self.report()
        for name in ("Image", "Security", "Virtualization", "Hardware"):
            self.assertEqual(self.section(rep, name)["status"], "warn", name)
        self.assertEqual(rep["summary"]["warn"], 4)

    def test_missing_tools_are_unknown_not_errors(self):
        self.run.answers.clear()
        rep = self.report()
        self.assertEqual(self.section(rep, "Image")["status"], "unknown")
        self.assertIn("not sure", " ".join(self.section(rep, "Image")["items"]).lower())

    def test_text_report_is_readable(self):
        text = D.to_text(self.report())
        self.assertIn("Forge OS doctor", text)
        self.assertIn("AI readiness", text)
        for bad in ("—", " -- "):
            self.assertNotIn(bad, text)

    def test_report_contains_no_setting_values_or_secrets(self):
        write(os.path.join(self.root, "etc/forge/settings.json"), json.dumps({"gateway.secret": "SECRETX"}))
        self.assertNotIn("SECRETX", json.dumps(self.report()))

    def test_cli_json_and_save(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        env = dict(os.environ, FORGE_LIB=LIB, FORGE_DOCTOR_ROOT=self.root, HOME=out, PATH="/usr/bin:/bin")
        r = subprocess.run([sys.executable, TOOL, "--json"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sections", json.loads(r.stdout))
        r = subprocess.run([sys.executable, TOOL, "--save"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = [f for f in os.listdir(out) if f.startswith("forge-doctor-")]
        self.assertEqual(len(saved), 1)
        self.assertEqual(os.stat(os.path.join(out, saved[0])).st_mode & 0o077, 0, "report is private")



class DoctorReviewRound4Test(DoctorTest):
    def test_unreadable_crypttab_is_not_sure_not_unencrypted(self):
        tab = os.path.join(self.root, "etc/crypttab")
        os.unlink(tab)
        os.mkdir(tab)          # exists but can't be read as a file (as a desktop user can't read a 0600 one)
        sec = self.section(self.report(), "Security")
        self.assertEqual(sec["status"], "unknown")
        self.assertFalse(any("not encrypted" in i for i in sec["items"]), sec)

    def test_two_saves_in_the_same_second_both_work(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        import time as _t
        now = _t.time()
        for d in range(0, 4):    # names for the next few seconds are already taken
            name = _t.strftime("forge-doctor-%Y%m%d-%H%M%S.txt", _t.localtime(now + d))
            open(os.path.join(out, name), "w").close()
        env = dict(os.environ, FORGE_LIB=LIB, FORGE_DOCTOR_ROOT=self.root, HOME=out, PATH="/usr/bin:/bin")
        r = subprocess.run([sys.executable, TOOL, "--save"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(os.listdir(out)), 5)


class DoctorReviewRound5Test(DoctorTest):
    def test_a_write_error_leaves_no_partial_report(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        env = dict(os.environ, FORGE_LIB=LIB, FORGE_DOCTOR_ROOT=self.root, HOME=out, PATH="/usr/bin:/bin")
        import resource

        def small_files():
            resource.setrlimit(resource.RLIMIT_FSIZE, (10, 10))
        r = subprocess.run([sys.executable, TOOL, "--save"], env=env, capture_output=True, text=True,
                           timeout=60, preexec_fn=small_files)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(os.listdir(out), [])

    def test_crypttab_with_only_comments_is_not_encryption(self):
        for text in ("# old\n# luks-a UUID=a none\n", "   # luks-a UUID=a none\n", "\n\n"):
            write(os.path.join(self.root, "etc/crypttab"), text)
            sec = self.section(self.report(), "Security")
            self.assertNotIn("Disk encryption: on", sec["items"], text)

    def test_save_to_a_folder_that_cannot_be_written_fails_cleanly(self):
        home = os.path.join(self.root, "not-a-folder")
        open(home, "w").close()          # HOME is a file, so nothing can be saved in it
        env = dict(os.environ, FORGE_LIB=LIB, FORGE_DOCTOR_ROOT=self.root, HOME=home, PATH="/usr/bin:/bin")
        r = subprocess.run([sys.executable, TOOL, "--save"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("Not saved", r.stderr)


class PassthroughTest(unittest.TestCase):
    """P1 virtualization: is a GPU ready to be passed to a VM (VFIO)?"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        write(os.path.join(self.root, "proc/cpuinfo"), "flags : fpu svm avx2\n")
        write(os.path.join(self.root, "dev/kvm"), "")

    def pci(self, addr, cls, vendor, group):
        dev = os.path.join(self.root, "sys/bus/pci/devices", addr)
        write(os.path.join(dev, "class"), cls + "\n")
        write(os.path.join(dev, "vendor"), vendor + "\n")
        g = os.path.join(self.root, "sys/kernel/iommu_groups", str(group))
        os.makedirs(os.path.join(g, "devices"), exist_ok=True)
        os.symlink(g, os.path.join(dev, "iommu_group"))
        os.symlink(dev, os.path.join(g, "devices", addr))

    def items(self):
        [s] = [s for s in D.build_report(root=self.root, run=FakeRun({}))["sections"] if s["name"] == "Virtualization"]
        return s

    def test_gpu_alone_in_its_group_is_ready(self):
        self.pci("0000:01:00.0", "0x030000", "0x10de", 14)
        self.pci("0000:01:00.1", "0x040300", "0x10de", 14)   # the card's own HDMI audio
        self.pci("0000:00:02.0", "0x030000", "0x8086", 0)
        s = self.items()
        text = "\n".join(s["items"])
        self.assertIn("NVIDIA GPU (0000:01:00.0): in its own IOMMU group", text)
        self.assertIn("Intel GPU (0000:00:02.0): in its own IOMMU group", text)
        self.assertNotIn("Only one graphics device", text)
        self.assertEqual(s["status"], "ok")

    def test_gpu_sharing_a_group_is_explained_not_a_warning(self):
        self.pci("0000:01:00.0", "0x030000", "0x1002", 1)
        self.pci("0000:02:00.0", "0x0c0330", "0x1022", 1)    # a USB controller in the same group
        self.pci("0000:00:01.1", "0x060400", "0x1022", 1)    # a PCI bridge
        s = self.items()
        text = "\n".join(s["items"])
        self.assertIn("AMD GPU (0000:01:00.0): shares its IOMMU group with 1 other device", text)
        self.assertIn("Only one graphics device", text)
        self.assertEqual(s["status"], "ok")    # passthrough is optional, so this is information

    def test_no_iommu_means_no_passthrough_lines(self):
        os.makedirs(os.path.join(self.root, "sys/bus/pci/devices/0000:01:00.0"))
        write(os.path.join(self.root, "sys/bus/pci/devices/0000:01:00.0/class"), "0x030000\n")
        text = "\n".join(self.items()["items"])
        self.assertIn("IOMMU: off", text)
        self.assertNotIn("IOMMU group", text)

    def test_devices_in_a_five_digit_pci_domain_still_count(self):
        # Intel VMD puts devices in domain 10000; they still share the GPU's group.
        self.pci("0000:01:00.0", "0x030000", "0x10de", 2)
        self.pci("10000:e1:00.0", "0x010802", "0x8086", 2)
        text = "\n".join(self.items()["items"])
        self.assertIn("shares its IOMMU group with 1 other device", text)
        self.assertNotIn("ready for passthrough", text)

    def test_odd_sysfs_text_does_not_break_the_report(self):
        self.pci("0000:01:00.0", "garbage", "\x1b[31mred", 3)
        self.pci("0000:02:00.0", "0x030000", "\x1b[31mred", 4)
        text = "\n".join(self.items()["items"])
        self.assertIn("GPU (0000:02:00.0)", text)
        self.assertNotIn("\x1b", text)


if __name__ == "__main__":
    unittest.main()
