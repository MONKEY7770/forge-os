"""Release builder: update channels (stable and testing) and the automatic rollback setting.

The channel switch is done by forge-apply as root, only with --confirm, always to a signed image
reference. rpm-ostree, systemctl and grub2-editenv are fakes on a test folder that record argv."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPLY = os.path.join(ROOT, "files/system/usr/libexec/forge-apply")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
sys.path.insert(0, LIB)
os.environ.setdefault("FORGE_SCHEMA", SCHEMA)

import forgecfg  # noqa: E402

OWNER = "ghcr.io/monkey7770"


def deployment(ref, booted=False, staged=False, version="45.20261117.1"):
    return {"container-image-reference": ref, "booted": booted, "staged": staged, "version": version,
            "timestamp": 1794897600, "checksum": "b" * 64, "id": "default-x.0"}


def status(*deps):
    return {"deployments": list(deps)}


SIGNED_STABLE = f"ostree-image-signed:docker://{OWNER}/forge-os:latest"
SIGNED_TESTING = f"ostree-image-signed:docker://{OWNER}/forge-os:testing"


class SettingsSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = forgecfg.load_schema()
        self.by = forgecfg.settings_by_key(self.schema)

    def test_updates_group_is_last_and_settings_exist(self):
        ids = [g["id"] for g in self.schema["groups"]]
        self.assertIn("updates", ids)   # other new groups (gaming) may come after it
        self.assertLess(ids.index("backup"), ids.index("updates"))
        ch = self.by["updates.channel"]
        self.assertEqual(ch["type"], "select")
        self.assertEqual(ch["options"], ["stable", "testing"])
        self.assertEqual(ch["default"], "stable")
        rb = self.by["updates.auto_rollback"]
        self.assertEqual((rb["type"], rb["default"]), ("toggle", True))
        for s in (ch, rb):
            self.assertEqual(s["group"], "updates")
            self.assertTrue(s["machine"], "a PC's own choice, never shared by settings sync")
            for k in ("label", "help", "tags"):
                self.assertTrue(s[k], k)
            self.assertNotIn("—", s["help"])
            self.assertNotIn("--", s["help"].replace("--switch-channel", "").replace("--confirm", ""))

    def test_new_settings_are_appended_at_the_end(self):
        # Appended after every setting that existed before, never inserted in the middle
        # (other new features may append after them).
        keys = [s["key"] for s in self.schema["settings"]]
        i = keys.index("updates.channel")
        self.assertEqual(keys[i + 1], "updates.auto_rollback")
        self.assertGreater(i, keys.index("monitor.notify_updates"))

    def test_validation_refuses_unknown_channels(self):
        _, errors = forgecfg.validate({"updates.channel": "nightly"}, self.schema)
        self.assertTrue(any("updates.channel" in e for e in errors))
        clean, errors = forgecfg.validate({"updates.channel": "testing"}, self.schema)
        self.assertEqual(errors, [])
        self.assertEqual(clean["updates.channel"], "testing")
        _, errors = forgecfg.validate({"updates.auto_rollback": "maybe"}, self.schema)
        self.assertTrue(errors)

    def test_help_is_accurate(self):
        rb = self.by["updates.auto_rollback"]["help"]
        # greenboot sets boot_counter to 3 before the restart; GRUB counts it down on each start of
        # the new version and boots the previous one when it reaches 0: 3 failed starts.
        self.assertIn("3 failed starts", rb)
        ch = self.by["updates.channel"]["help"]
        self.assertNotIn("boot menu", ch, "the boot menu does not change the channel updates come from")
        import forgerelease
        self.assertNotIn("boot menu always keeps", forgerelease.go_back_text("stable"))

    def test_portable_export_leaves_both_out(self):
        self.assertIn("updates.channel", forgecfg.machine_keys(self.schema))
        self.assertIn("updates.auto_rollback", forgecfg.machine_keys(self.schema))


class ForgeReleaseLibTests(unittest.TestCase):
    def setUp(self):
        import forgerelease
        self.fr = forgerelease

    def test_parse_ref_accepts_only_forge_images(self):
        p = self.fr.parse_ref(SIGNED_STABLE)
        self.assertEqual(p, {"image": "forge-os", "tag": "latest", "signed": True})
        p = self.fr.parse_ref(f"ostree-unverified-registry:{OWNER}/forge-os-universal:testing")
        self.assertEqual(p, {"image": "forge-os-universal", "tag": "testing", "signed": False})
        for bad in ("ostree-image-signed:docker://ghcr.io/ublue-os/bazzite:stable",
                    "ostree-image-signed:docker://ghcr.io/monkey7770/forge-os-evil:latest",
                    "ostree-image-signed:docker://ghcr.io/monkey7770/forge-os@sha256:" + "a" * 64,
                    "ostree-image-signed:docker://ghcr.io/monkey7770/forge-os:latest\nx",
                    "", None, 5):
            self.assertIsNone(self.fr.parse_ref(bad), bad)

    def test_channel_of_tag(self):
        self.assertEqual(self.fr.channel_of_tag("latest"), "stable")
        self.assertEqual(self.fr.channel_of_tag("testing"), "testing")
        self.assertIsNone(self.fr.channel_of_tag("20261120"))

    def test_target_ref_is_always_signed(self):
        self.assertEqual(self.fr.target_ref("forge-os", "testing"), SIGNED_TESTING)
        self.assertEqual(self.fr.target_ref("forge-os-universal", "stable"),
                         f"ostree-image-signed:docker://{OWNER}/forge-os-universal:latest")
        with self.assertRaises(ValueError):
            self.fr.target_ref("forge-os", "nightly")
        with self.assertRaises(ValueError):
            self.fr.target_ref("bazzite", "stable")

    def test_followed_prefers_the_staged_deployment(self):
        st = status(deployment(SIGNED_TESTING, staged=True), deployment(SIGNED_STABLE, booted=True))
        f = self.fr.followed(st)
        self.assertEqual((f["image"], f["channel"], f["staged"]), ("forge-os", "testing", True))
        # Started an older entry from the boot menu: updates still come from the default entry.
        f = self.fr.followed(status(deployment(SIGNED_TESTING), deployment(SIGNED_STABLE, booted=True)))
        self.assertEqual(f["channel"], "testing")
        f = self.fr.followed(status(deployment(SIGNED_STABLE, booted=True)))
        self.assertEqual((f["channel"], f["staged"], f["signed"]), ("stable", False, True))
        self.assertIsNone(self.fr.followed(status(deployment("ostree-image-signed:docker://ghcr.io/ublue-os/bazzite:stable",
                                                             booted=True))))
        for junk in (None, {}, {"deployments": "x"}, {"deployments": [5]}, status(deployment(SIGNED_STABLE))):
            self.assertIsNone(self.fr.followed(junk), junk)


class FakeSystem(unittest.TestCase):
    """A fake root with fake rpm-ostree, systemctl and grub2-editenv that record their argv."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.conf = os.path.join(self.root, "etc/forge/settings.json")
        self.bin = os.path.join(self.root, "testbin")
        self.calls = os.path.join(self.root, "calls")
        os.makedirs(self.bin)
        self.set_status(status(deployment(SIGNED_STABLE, booted=True)))
        self.fake("rpm-ostree", 'if [ "$1" = status ]; then cat "$FAKE_STATUS"; exit 0; fi\n'
                                'exit "${FAKE_REBASE_RC:-0}"\n')
        self.fake("systemctl", 'if [ "$1" = is-enabled ]; then echo "${FAKE_ENABLED:-enabled}"; fi\nexit 0\n')
        self.fake("grub2-editenv", 'if [ "$2" = list ]; then printf "%b" "${FAKE_GRUBENV:-}"; fi\nexit 0\n')
        self.env = dict(os.environ, FORGE_ROOT=self.root, FORGE_DRYRUN="1", FORGE_CONF=self.conf,
                        PYTHONPATH=LIB, FORGE_SCHEMA=SCHEMA, FORGE_TEST_BIN=self.bin,
                        FAKE_STATUS=os.path.join(self.root, "status.json"))
        self.env.pop("SUDO_USER", None)

    def fake(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\n' + body)
        os.chmod(p, 0o755)

    def set_status(self, st):
        with open(os.path.join(self.root, "status.json"), "w") as f:
            json.dump(st, f)

    def apply(self, *args, **env):
        return subprocess.run([sys.executable, APPLY, *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def recorded(self, prog="rpm-ostree"):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return [line for line in f.read().splitlines() if line.startswith(prog + " ")]

    def rebases(self):
        return [line for line in self.recorded() if line.startswith("rpm-ostree rebase")]

    def saved(self):
        if not os.path.exists(self.conf):
            return {}
        with open(self.conf) as f:
            return json.load(f)


class ApplyChannelSwitchTests(FakeSystem):
    """forge-apply --switch-channel with a fake rpm-ostree that records its argv."""

    def test_without_confirm_nothing_happens(self):
        r = self.apply("--switch-channel", "testing")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--confirm", r.stdout + r.stderr)
        self.assertIn(SIGNED_TESTING.split("docker://")[1], r.stdout + r.stderr)
        self.assertEqual(self.rebases(), [])
        self.assertEqual(self.saved(), {})

    def test_confirmed_switch_rebases_to_the_signed_reference_and_saves_the_choice(self):
        r = self.apply("--switch-channel", "testing", "--confirm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.rebases(), ["rpm-ostree rebase " + SIGNED_TESTING])
        self.assertEqual(self.saved().get("updates.channel"), "testing")
        out = r.stdout
        self.assertIn("restart", out)
        self.assertIn("--switch-channel stable --confirm", out, "says how to go back")

    def test_back_to_stable_after_picking_it_in_the_boot_menu_still_rebases(self):
        # rvRel 4: rpm-ostree updates from deployments[0] (the default entry), not the booted one.
        self.set_status(status(deployment(SIGNED_TESTING), deployment(SIGNED_STABLE, booted=True)))
        r = self.apply("--switch-channel", "stable", "--confirm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.rebases(), ["rpm-ostree rebase " + SIGNED_STABLE])
        self.assertNotIn("already follows", r.stdout + r.stderr)

    def test_reminder_uses_the_default_entry_too(self):
        self.set_status(status(deployment(SIGNED_TESTING), deployment(SIGNED_STABLE, booted=True)))
        r = self.apply()
        self.assertIn("this PC follows testing", r.stdout)

    def test_universal_image_stays_universal(self):
        self.set_status(status(deployment(f"ostree-image-signed:docker://{OWNER}/forge-os-universal:latest", booted=True)))
        r = self.apply("--switch-channel", "testing", "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rebases(),
                         [f"rpm-ostree rebase ostree-image-signed:docker://{OWNER}/forge-os-universal:testing"])

    def test_an_unverified_install_is_moved_to_a_signed_reference(self):
        self.set_status(status(deployment(f"ostree-unverified-registry:{OWNER}/forge-os:latest", booted=True)))
        r = self.apply("--switch-channel", "stable", "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rebases(), ["rpm-ostree rebase " + SIGNED_STABLE])

    def test_already_on_that_channel_does_not_rebase(self):
        r = self.apply("--switch-channel", "stable", "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rebases(), [])
        self.assertIn("already", r.stdout)

    def test_not_a_forge_image_is_refused(self):
        self.set_status(status(deployment("ostree-image-signed:docker://ghcr.io/ublue-os/bazzite:stable", booted=True)))
        r = self.apply("--switch-channel", "testing", "--confirm")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.rebases(), [])
        self.assertEqual(self.saved(), {})

    def test_failed_rebase_saves_nothing(self):
        r = self.apply("--switch-channel", "testing", "--confirm", FAKE_REBASE_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.rebases()), 1)
        self.assertEqual(self.saved(), {})
        self.assertIn("Not saved", r.stderr)

    def test_bad_channel_names_are_refused_before_anything_runs(self):
        for bad in ("nightly", "", "testing;reboot", "--confirm"):
            r = self.apply("--switch-channel", bad, "--confirm")
            self.assertEqual(r.returncode, 2, bad)
        r = self.apply("--switch-channel")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.recorded(), [])

    def test_forge_core_can_never_switch(self):
        r = self.apply("--switch-channel", "testing", "--confirm", SUDO_USER="forge")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.rebases(), [])

    def test_import_with_switch_must_match_the_imported_choice(self):
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"updates.channel": "stable"}, fh)
        r = self.apply("--import", f, "--switch-channel", "testing", "--confirm")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.rebases(), [])
        with open(f, "w") as fh:
            json.dump({"updates.channel": "testing", "ai.keep_alive_min": 7}, fh)
        r = self.apply("--import", f, "--switch-channel", "testing", "--confirm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.rebases(), ["rpm-ostree rebase " + SIGNED_TESTING])
        self.assertEqual(self.saved(), {"updates.channel": "testing", "ai.keep_alive_min": 7})

    def test_saving_the_setting_alone_never_rebases(self):
        # Settings page without the confirmation, forge-config, the API, sync and undo all end here.
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({"updates.channel": "testing"}, fh)
        r = self.apply("--import", f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rebases(), [])
        self.assertIn("Nothing was switched", r.stdout)
        self.assertIn("--switch-channel testing --confirm", r.stdout)
        r = self.apply("--undo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rebases(), [])

    def test_plain_apply_never_rebases(self):
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rebases(), [])

    def test_undo_cannot_be_combined_with_a_switch(self):
        r = self.apply("--undo", "--switch-channel", "testing", "--confirm")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.rebases(), [])


class ApplyAutoRollbackTests(FakeSystem):
    """updates.auto_rollback turns greenboot's two units on or off, and clears a stale rollback
    trigger only when it is switched back on (a stale one would roll back a good system)."""

    def set(self, **values):
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump({k.replace("__", "."): v for k, v in values.items()}, fh)
        return self.apply("--import", f)

    def test_on_by_default_enables_greenboot(self):
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would run: systemctl enable greenboot-healthcheck.service greenboot-set-rollback-trigger.service",
                      r.stdout)
        self.assertEqual(self.recorded("grub2-editenv"), [])

    def test_off_disables_without_stopping(self):
        r = self.set(updates__auto_rollback=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would run: systemctl disable greenboot-healthcheck.service greenboot-set-rollback-trigger.service",
                      r.stdout)
        self.assertNotIn("stop greenboot", r.stdout)
        self.assertFalse([c for c in self.recorded("grub2-editenv") if " unset " in c])

    def test_turning_back_on_clears_a_stale_trigger(self):
        r = self.apply(FAKE_ENABLED="disabled", FAKE_GRUBENV="greenboot_next_deployment_id=abc\\nboot_success=1\\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("grub2-editenv /boot/grub2/grubenv unset greenboot_next_deployment_id greenboot_rollback_trigger "
                      "boot_counter", "\n".join(self.recorded("grub2-editenv")))

    def test_turning_off_clears_the_countdown(self):
        # rvRel 3: a boot_counter left behind would keep GRUB counting down (Windows starts too)
        # and finally start the other entry every time.
        r = self.set(updates__auto_rollback=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in self.recorded("grub2-editenv") if " unset " in c], "no marks: nothing to clear")
        r = self.apply("--import", os.path.join(self.root, "in.json"), FAKE_ENABLED="enabled",
                       FAKE_GRUBENV="boot_counter=2\\nboot_success=0\\ngreenboot_next_deployment_id=x\\nfallback=1\\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("grub2-editenv /boot/grub2/grubenv unset greenboot_next_deployment_id greenboot_rollback_trigger "
                      "boot_counter fallback", "\n".join(self.recorded("grub2-editenv")))
        # Already off: nothing more to clear, and nothing touched.
        os.unlink(self.calls)
        r = self.apply(FAKE_ENABLED="disabled", FAKE_GRUBENV="boot_counter=2\\n")
        self.assertFalse([c for c in self.recorded("grub2-editenv") if " unset " in c])

    def test_staying_on_never_touches_the_trigger(self):
        r = self.apply(FAKE_ENABLED="enabled", FAKE_GRUBENV="greenboot_next_deployment_id=abc\\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in self.recorded("grub2-editenv") if " unset " in c])


if __name__ == "__main__":
    unittest.main()
