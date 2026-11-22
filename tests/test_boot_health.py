"""Release builder: Forge OS start-up checks for greenboot (automatic rollback).

forge-boot-health only says "this start is broken" (exit 3) on the first start of a new system
version, and only for: the login screen service failed, or the NVIDIA driver did not load on a PC
with an NVIDIA card and a driver in the image. Everything else, including any error inside the
check itself, passes. /sys, /proc and the tools it asks are fakes."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/libexec/forge-boot-health")
WRAPPER = os.path.join(ROOT, "files/system/usr/lib/greenboot/check/required.d/60_forge_boot_health.sh")
GREEN = os.path.join(ROOT, "files/system/usr/lib/greenboot/green.d/60_forge_boot_health.sh")
CONF = os.path.join(ROOT, "files/system/etc/greenboot/greenboot.conf")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
BROKEN = 3


class BootHealthTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bin = os.path.join(self.root, "testbin")
        self.calls = os.path.join(self.root, "calls")
        os.makedirs(self.bin)
        self.conf = os.path.join(self.root, "etc/forge/settings.json")
        with open(self.write("proc/cmdline"), "a") as f:
            f.write("BOOT_IMAGE=/vmlinuz root=UUID=x rhgb quiet\n")
        self.state = {"grubenv": "greenboot_next_deployment_id=abc\nboot_success=0\n", "default": "graphical.target",
                      "dm": "LoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=0\n"
                            "After=systemd-user-sessions.service plymouth-quit.service\n",
                      "modinfo_rc": 0}
        self.fake("grub2-editenv", 'cat "$T/grubenv"\n')
        self.fake("systemctl", 'if [ "$1" = get-default ]; then cat "$T/default"; else cat "$T/dm"; fi\n')
        self.fake("modinfo", 'exit "$(cat "$T/modinfo_rc")"\n')
        self.fake("rpm-ostree", 'cat "$T/status"\n')
        self.state["status"] = json.dumps({"deployments": [
            {"booted": True, "checksum": "abc", "container-image-reference-digest": "sha256:" + "d" * 64}]})
        # The last good start had the NVIDIA driver loaded (written by "forge-boot-health record").
        self.write("var/lib/forge-boot-health/last-good.json",
                   json.dumps({"format": 1, "nvidia_loaded": True, "gpus": ["10de:2c05"], "secure_boot": None}))

    def write(self, rel, text=""):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def fake(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\nT="{self.root}/state"\n' + body)
        os.chmod(p, 0o755)

    def gpu(self, slot="0000:01:00.0", vendor="0x10de", cls="0x030000", driver=None, device="0x2c05"):
        d = os.path.join(self.root, "sys/bus/pci/devices", slot)
        os.makedirs(d, exist_ok=True)
        for k, v in (("vendor", vendor), ("class", cls), ("device", device)):
            with open(os.path.join(d, k), "w") as f:
                f.write(v + "\n")
        if driver:
            target = os.path.join(self.root, "sys/bus/pci/drivers", driver)
            os.makedirs(target, exist_ok=True)
            os.symlink(target, os.path.join(d, "driver"))

    def nvidia_loaded(self):
        os.makedirs(os.path.join(self.root, "sys/module/nvidia"), exist_ok=True)

    def run_tool(self, *args, wrapper=False, **env):
        sd = os.path.join(self.root, "state")
        os.makedirs(sd, exist_ok=True)
        for k, v in self.state.items():
            with open(os.path.join(sd, k), "w") as f:
                f.write(str(v))
        e = dict(os.environ, FORGE_ROOT=self.root, FORGE_TEST_BIN=self.bin, FORGE_TEST_FAST="1",
                 FORGE_CONF=self.conf, FORGE_SCHEMA=SCHEMA, FORGE_LIB=LIB, **env)
        if wrapper:
            copy = os.path.join(self.root, "wrapper.sh")
            launcher = os.path.join(self.root, "forge-boot-health")
            with open(launcher, "w") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" "{TOOL}" "$@"\n')
            os.chmod(launcher, 0o755)
            with open(WRAPPER) as f:
                text = f.read().replace("/usr/libexec/forge-boot-health", launcher)
            with open(copy, "w") as f:
                f.write(text)
            return subprocess.run(["bash", copy], env=e, capture_output=True, text=True, timeout=60)
        return subprocess.run([sys.executable, TOOL, *(args or ("check",))], env=e, capture_output=True,
                              text=True, timeout=60)

    def calls_of(self, name):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return [c for c in f.read().splitlines() if c.startswith(name + " ")]

    # Not the first start of a new version: nothing is ever failed.

    def test_normal_start_checks_nothing(self):
        self.state["grubenv"] = "boot_success=1\n"
        self.state["dm"] = "LoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=9\nAfter=\n"
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("not the first start", r.stdout)
        self.assertEqual(self.calls_of("systemctl"), [])

    def test_trial_is_only_a_mark_for_this_very_deployment(self):
        failed = "LoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=0\nAfter=\n"
        trial = ("greenboot_next_deployment_id=abc\n", "greenboot_next_deployment_id=abc\nboot_counter=2\n",
                 "greenboot_next_deployment_id=sha256:" + "d" * 64 + "\n",      # bootc names it by image digest
                 "greenboot_rollback_trigger=1\nboot_counter=2\n",              # greenboot 0.16.3 and older
                 "greenboot_rollback_trigger=1\nboot_counter=0\n")              # its last try (0.16.6 rolls back)
        # rvRel 2a/2b: marks that outlive the trial (the restored old version boots with boot_counter=-1,
        # 0.16.4 never clears a 0.16.3 rollback trigger) are not a trial start.
        not_trial = ("boot_counter=-1\nboot_success=0\n", "boot_counter=0\n", "boot_counter=2\n",
                     "greenboot_rollback_trigger=1\nboot_success=1\n", "greenboot_rollback_trigger=1\nboot_counter=-1\n",
                     "greenboot_next_deployment_id=other\nboot_counter=-1\n", "boot_success=1\n")
        for env in trial:
            self.state["grubenv"], self.state["dm"] = env, failed
            self.assertEqual(self.run_tool().returncode, BROKEN, env)
        for env in not_trial:
            self.state["grubenv"], self.state["dm"] = env, failed
            r = self.run_tool()
            self.assertEqual(r.returncode, 0, env + r.stdout)
            self.assertIn("not the first start", r.stdout)

    def test_unreadable_deployment_means_no_trial(self):
        self.state["status"] = "not json"
        self.state["dm"] = "LoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=0\nAfter=\n"
        self.assertEqual(self.run_tool().returncode, 0)

    def test_off_in_settings_never_fails(self):
        self.write("etc/forge/settings.json", json.dumps({"updates.auto_rollback": False}))
        self.state["dm"] = "LoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=0\nAfter=\n"
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("off in Forge Settings", r.stdout)

    # Login screen

    def test_running_login_screen_passes(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("login screen: ok", r.stdout)

    def test_failed_login_screen_is_broken(self):
        self.state["dm"] = "LoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=0\nAfter=\n"
        r = self.run_tool()
        self.assertEqual(r.returncode, BROKEN, r.stdout)
        self.assertIn("BROKEN", r.stdout)

    def test_crash_loop_is_broken(self):
        self.state["dm"] = "LoadState=loaded\nActiveState=activating\nSubState=auto-restart\nNRestarts=4\nAfter=\n"
        self.assertEqual(self.run_tool().returncode, BROKEN)

    def test_still_starting_is_not_sure_and_passes(self):
        self.state["dm"] = "LoadState=loaded\nActiveState=activating\nSubState=start\nNRestarts=0\nAfter=\n"
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("not sure", r.stdout)

    def test_no_desktop_on_purpose_or_no_login_manager_is_skipped(self):
        self.state["default"] = "multi-user.target"
        self.state["dm"] = "LoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=0\nAfter=\n"
        self.assertEqual(self.run_tool().returncode, 0)
        self.state["default"] = "graphical.target"
        self.state["dm"] = "LoadState=not-found\nActiveState=inactive\nSubState=dead\nNRestarts=0\nAfter=\n"
        self.assertEqual(self.run_tool().returncode, 0)

    def test_login_manager_ordered_after_the_check_is_never_waited_for(self):
        # It could only start after this check ends: waiting would always time out.
        self.state["dm"] = ("LoadState=loaded\nActiveState=inactive\nSubState=dead\nNRestarts=0\n"
                            "After=multi-user.target systemd-user-sessions.service\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("skipped", r.stdout)

    # NVIDIA

    def test_nvidia_loaded_passes(self):
        self.gpu(driver="nvidia")
        self.nvidia_loaded()
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("NVIDIA driver: ok", r.stdout)

    def test_nvidia_card_without_driver_is_broken(self):
        self.gpu()
        r = self.run_tool()
        self.assertEqual(r.returncode, BROKEN, r.stdout)
        self.assertIn("NVIDIA", r.stdout)

    def test_no_nvidia_card_or_no_driver_in_the_image_is_skipped(self):
        self.gpu(vendor="0x1002")          # AMD
        self.assertEqual(self.run_tool().returncode, 0)
        self.gpu(slot="0000:02:00.0")
        self.state["modinfo_rc"] = 1       # universal image: no nvidia module
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("no NVIDIA driver in this image", r.stdout)

    def test_nvidia_that_never_loaded_before_never_fails(self):
        # rvRel 1: Secure Boot key not enrolled, an older card on the open module, or a driver blocked
        # in /etc/modprobe.d: the driver did not load on the last good start either.
        self.gpu()
        os.unlink(os.path.join(self.root, "var/lib/forge-boot-health/last-good.json"))
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("last good start", r.stdout)
        self.write("var/lib/forge-boot-health/last-good.json", json.dumps({"format": 1, "nvidia_loaded": False}))
        self.assertEqual(self.run_tool().returncode, 0)
        for junk in ("{", json.dumps({"format": 1, "nvidia_loaded": "yes"}), json.dumps([1])):
            self.write("var/lib/forge-boot-health/last-good.json", junk)
            self.assertEqual(self.run_tool().returncode, 0, junk)

    def test_blacklist_in_modprobe_d_is_respected(self):
        self.gpu()
        for line in ("blacklist nvidia\n", "install nvidia /bin/false\n", "  blacklist   nvidia  # off\n"):
            self.write("etc/modprobe.d/blacklist-nvidia.conf", line)
            r = self.run_tool()
            self.assertEqual(r.returncode, 0, line + r.stdout)
            self.assertIn("blocked on purpose", r.stdout)
        self.write("etc/modprobe.d/blacklist-nvidia.conf", "blacklist nouveau\noptions nvidia NVreg_X=1\n")
        self.assertEqual(self.run_tool().returncode, BROKEN)

    def test_changed_gpu_or_secure_boot_means_no_baseline(self):
        # rvRel round 2, item 5: a new card or a Secure Boot change together with an update.
        self.gpu(device="0x2d04")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("changed since the last good start", r.stdout)
        shutil.rmtree(os.path.join(self.root, "sys/bus/pci/devices"))
        self.gpu()
        efivar = "sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
        self.write(efivar, "\x06\x00\x00\x00\x01")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("changed since the last good start", r.stdout)
        # Old records without these fields: not a usable baseline either.
        os.unlink(os.path.join(self.root, efivar))
        self.write("var/lib/forge-boot-health/last-good.json", json.dumps({"format": 1, "nvidia_loaded": True}))
        self.assertEqual(self.run_tool().returncode, 0)

    def test_record_keeps_gpu_and_secure_boot_and_late_record_redoes_it(self):
        base = os.path.join(self.root, "var/lib/forge-boot-health/last-good.json")
        os.unlink(base)
        self.gpu(driver="nvidia")
        self.write("sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c", "\x06\x00\x00\x00\x01")
        self.assertEqual(self.run_tool("record").returncode, 0)
        with open(base) as f:
            rec = json.load(f)
        self.assertEqual((rec["nvidia_loaded"], rec["gpus"], rec["secure_boot"]), (False, ["10de:2c05"], True))
        self.nvidia_loaded()                      # the driver came up after the early record
        r = self.run_tool("record", "--late")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(base) as f:
            self.assertIs(json.load(f)["nvidia_loaded"], True)
        with open(GREEN) as f:
            hook = f.read()
        self.assertIn("systemd-run", hook)
        self.assertIn("--on-active=600", hook)
        self.assertIn("record --late", hook)

    def test_restored_old_entry_with_the_same_image_digest_is_not_a_trial(self):
        # rvRel round 2, item 4: a redeploy keeps the image digest (kernel options, initramfs-etc).
        mod = self.load_module()
        d = "sha256:" + "d" * 64
        self.assertFalse(mod.is_trial({"greenboot_next_deployment_id": d, "boot_counter": "-1"}, {d}))
        self.assertTrue(mod.is_trial({"greenboot_next_deployment_id": d, "boot_counter": "2"}, {d}))
        # Started from an older boot entry (not the first one): never a trial, whatever the marks say.
        self.state["status"] = json.dumps({"deployments": [
            {"booted": False, "checksum": "new", "container-image-reference-digest": d},
            {"booted": True, "checksum": "abc", "container-image-reference-digest": d}]})
        self.state["grubenv"] = "greenboot_next_deployment_id=" + d + "\n"
        self.state["dm"] = "LoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=0\nAfter=\n"
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("not the first start", r.stdout)
        # A staged deployment listed first does not change that the booted one is the default entry.
        self.state["status"] = json.dumps({"deployments": [
            {"booted": False, "staged": True, "checksum": "next"},
            {"booted": True, "checksum": "abc", "container-image-reference-digest": d}]})
        self.assertEqual(self.run_tool().returncode, BROKEN)

    def load_module(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("fbh_unit", TOOL)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def test_record_writes_the_last_good_start(self):
        base = os.path.join(self.root, "var/lib/forge-boot-health/last-good.json")
        os.unlink(base)
        self.gpu(driver="nvidia")
        self.nvidia_loaded()
        r = self.run_tool("record")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(base) as f:
            self.assertIs(json.load(f)["nvidia_loaded"], True)
        self.assertEqual(os.stat(base).st_mode & 0o777, 0o600)
        shutil.rmtree(os.path.join(self.root, "sys/module/nvidia"))
        self.run_tool("record")
        with open(base) as f:
            self.assertIs(json.load(f)["nvidia_loaded"], False)
        # Never through a link someone left there.
        os.unlink(base)
        target = os.path.join(self.root, "elsewhere")
        os.symlink(target, base)
        self.assertEqual(self.run_tool("record").returncode, 0)
        self.assertFalse(os.path.exists(target))

    def test_green_hook_records_and_never_fails(self):
        with open(GREEN) as f:
            text = f.read()
        self.assertIn("/usr/libexec/forge-boot-health record", text)
        r = subprocess.run(["bash", "-c", text.replace("/usr/libexec/forge-boot-health", "/nonexistent/x")],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)

    def test_passthrough_and_blacklist_are_respected(self):
        self.gpu(driver="vfio-pci")
        self.assertEqual(self.run_tool().returncode, 0)
        self.gpu(slot="0000:02:00.0")
        self.write("proc/cmdline", "root=x modprobe.blacklist=nouveau,nvidia quiet\n")
        self.assertEqual(self.run_tool().returncode, 0)

    def test_audio_function_of_the_card_is_not_a_gpu(self):
        self.gpu(cls="0x040300")
        self.assertEqual(self.run_tool().returncode, 0)

    def test_odd_sysfs_text_never_crashes_into_a_failure(self):
        self.gpu(vendor="\x00\xff garbage", cls="")
        self.assertEqual(self.run_tool().returncode, 0)

    # Never a false failure from the check itself

    def test_internal_errors_pass(self):
        r = self.run_tool(FORGE_TEST_CRASH="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("not sure", r.stdout)

    def test_missing_tools_pass(self):
        shutil.rmtree(self.bin)
        os.makedirs(self.bin)
        self.assertEqual(self.run_tool().returncode, 0)

    def test_wrapper_fails_only_on_broken(self):
        r = self.run_tool(wrapper=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.state["dm"] = "LoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=0\nAfter=\n"
        r = self.run_tool(wrapper=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        r = self.run_tool(wrapper=True, FORGE_TEST_CRASH="1")
        self.assertEqual(r.returncode, 0)

    def test_wrapper_passes_when_the_tool_is_missing(self):
        with open(WRAPPER) as f:
            text = f.read()
        self.assertIn('[ -x /usr/libexec/forge-boot-health ] || exit 0', text)
        r = subprocess.run(["bash", "-c", text.replace("/usr/libexec/forge-boot-health", "/nonexistent/x")],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)

    def test_never_looks_at_optional_forge_services(self):
        with open(TOOL) as f:
            text = f.read()
        for unit in ("forge-core", "ollama", "forge-hwcheck", "forge-virt", "tailscaled", "forge-apply-firstboot"):
            self.assertNotIn(unit, text, unit)


class GreenbootConfigTests(unittest.TestCase):
    def setUp(self):
        with open(CONF) as f:
            self.text = f.read()
        self.values = dict(l.split("=", 1) for l in self.text.splitlines() if l and not l.startswith("#"))

    def test_bounded_attempts_and_no_offline_failures(self):
        self.assertEqual(self.values["GREENBOOT_MAX_BOOT_ATTEMPTS"], "3")
        # A start without internet is not a broken start.
        self.assertEqual(self.values["DISABLED_HEALTHCHECKS"], '("01_repository_dns_check.sh")')
        self.assertEqual(self.values["GREENBOOT_WATCHDOG_CHECK_ENABLED"], "false")

    def test_wrapper_is_a_required_check_and_tool_is_executable_in_the_image(self):
        self.assertTrue(WRAPPER.endswith(".sh"), "greenboot runs .sh files with bash")
        with open(os.path.join(ROOT, "files/scripts/finalize.sh")) as f:
            self.assertIn("/usr/libexec/forge-boot-health", f.read())


if __name__ == "__main__":
    unittest.main()


class LateRecordTests(BootHealthTests):
    """Review rvRel round 3: a driver that only loads after the check's wait was recorded as loaded by the
    late pass, so every later update failed its check. A late-only "loaded" is never a baseline, and the
    late pass never replaces a good early record."""

    def record(self, *extra):
        e = dict(os.environ, FORGE_ROOT=self.root, FORGE_LIB=LIB)
        subprocess.run([sys.executable, TOOL, "record", *extra], env=e, check=True, capture_output=True)
        with open(os.path.join(self.root, "var/lib/forge-boot-health/last-good.json")) as f:
            return json.load(f)

    def test_a_driver_that_only_loads_late_is_never_a_baseline(self):
        self.gpu(driver="nvidia")
        self.record()                                             # at the good start: not loaded yet
        self.nvidia_loaded()
        self.record("--late")                                     # 10 minutes later: loaded
        os.rmdir(os.path.join(self.root, "sys/module/nvidia"))    # next trial start: not loaded at 2 min
        self.assertNotEqual(self.run_tool().returncode, BROKEN)

    def test_the_late_pass_keeps_a_good_early_record(self):
        self.gpu(driver="nvidia")
        self.nvidia_loaded()
        early = self.record()
        late = self.record("--late")
        self.assertEqual((late["nvidia_loaded"], late["late"]), (True, False))
        self.assertEqual(late["ts"], early["ts"])
        os.rmdir(os.path.join(self.root, "sys/module/nvidia"))    # a real regression is still caught
        self.assertEqual(self.run_tool().returncode, BROKEN)

