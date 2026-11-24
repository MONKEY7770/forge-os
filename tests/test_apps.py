"""forge-apps: optional apps chosen in the setup wizard or in Forge Settings, Apps.

Rules under test: nothing optional is installed before setup is finished; a switch that is on
installs its app; turning a switch off deletes the app once; an app you deleted yourself is not
put back (its switch is turned off instead); an app you installed yourself is never deleted unless
you turn its switch from on to off."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
TOOL = os.path.join(ROOT, "files/system/usr/libexec/forge-apps")
CATALOG = os.path.join(ROOT, "files/system/usr/share/forge/apps.json")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
UNITS = os.path.join(ROOT, "files/system/usr/lib/systemd/system")
sys.path.insert(0, LIB)


def load_tool():
    loader = importlib.machinery.SourceFileLoader("forge_apps_t", TOOL)
    spec = importlib.util.spec_from_loader("forge_apps_t", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def catalog():
    with open(CATALOG) as f:
        return json.load(f)


def defaults():
    with open(SCHEMA) as f:
        schema = json.load(f)["settings"]
    keys = {it["key"] for it in catalog()["optional"]}
    return {s["key"]: s["default"] for s in schema if s["key"] in keys}


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.a = load_tool()
        self.a.CATALOG = CATALOG
        self.a.STATE_DIR = os.path.join(self.t, "state")
        os.makedirs(self.a.STATE_DIR, mode=0o700)
        self.a.WAYDROID_DIR = os.path.join(self.t, "waydroid")
        self.a.SPOOL_DIR = os.path.join(self.t, "spool")
        os.makedirs(self.a.SPOOL_DIR, mode=0o700)
        self.a.TMP_DIR = os.path.join(self.t, "run")
        os.makedirs(self.a.TMP_DIR, mode=0o755)
        self.vals = defaults()
        self.a.settings = lambda: dict(self.vals)
        self.users = [1000]
        self.a.regular_users = lambda: list(self.users)
        self.installed = set()
        self.fail = set()
        self.calls = []
        self.imported = []
        self.notes = []
        self.a.notify_users = lambda s, b: self.notes.append((s, b))
        self.nvidia = False
        self.a.has_nvidia_setup = lambda: self.nvidia
        self.versions = "6.17.1|580.1"
        self.a.driver_versions = lambda: self.versions
        self.a.run_cmd = self.fake_run
        self.mounts = []
        self.a.mounted_under = lambda path: [m for m in self.mounts if m == path or m.startswith(path + "/")]
        self.now = 1_000_000.0
        self.a.now = lambda: self.now
        self.setup_fails = False

    def fake_run(self, cmd, timeout=60):
        self.calls.append(list(cmd))
        if cmd[:2] == ["flatpak", "list"]:
            return 0, "".join(a + "\n" for a in sorted(self.installed))
        if cmd[:2] == ["flatpak", "install"]:
            app = cmd[-1]
            if app in self.fail:
                return 1, "error: network"
            self.installed.add(app)
            return 0, ""
        if cmd[:2] == ["flatpak", "uninstall"] and "--unused" not in cmd:
            for app in cmd[4:]:
                self.installed.discard(app)
            return 0, ""
        if cmd[0] == self.a.APPLY_CMD[0]:
            with open(cmd[2]) as f:
                self.imported.append(json.load(f))
            return 0, ""
        if cmd[:2] in (["waydroid", "init"], ["waydroid-nvidia-setup", "--mode"]):
            if self.setup_fails:
                return 1, "setup failed"
            os.makedirs(os.path.join(self.a.WAYDROID_DIR, "images"), exist_ok=True)
            return 0, ""
        return 0, ""

    def flatpak_calls(self, verb):
        return [c[-1] for c in self.calls if c[:2] == ["flatpak", verb] and "--unused" not in c]

    def state(self):
        with open(os.path.join(self.a.STATE_DIR, "state.json")) as f:
            return json.load(f)


class SetupGateTest(Base):
    def test_nothing_is_installed_before_setup_is_finished(self):
        self.users = []      # the wizard has not made your account yet, and no choice was saved
        self.assertEqual(self.a.main([]), 0)
        self.assertEqual(self.flatpak_calls("install"), [])

    def test_a_choice_saved_in_the_wizard_starts_it_before_the_account_exists(self):
        self.users = []
        open(os.path.join(self.a.STATE_DIR, "chosen"), "w").close()
        self.a.main([])
        self.assertIn("com.discordapp.Discord", self.flatpak_calls("install"))


class ReviewFixTest(Base):
    OLD_LIST = ("com.discordapp.Discord", "org.blender.Blender", "org.kde.krita", "org.prismlauncher.PrismLauncher")

    def test_upgrade_keeps_apps_you_had_removed_removed(self):
        # An older Forge installed every app; you had deleted Krita. No state yet, no wizard choice.
        self.installed.update(a for a in self.OLD_LIST if a != "org.kde.krita")
        self.a.main([])
        self.assertNotIn("org.kde.krita", self.flatpak_calls("install"))
        self.assertEqual(len(self.imported), 1)                # one save for all of them
        self.assertIs(self.imported[0]["apps.krita"], False)
        self.assertNotIn("apps.discord", self.imported[0])      # present: stays on

    def test_fresh_install_without_the_wizard_page_gets_the_defaults(self):
        self.a.main([])                                         # nothing optional present: not an upgrade
        self.assertIn("org.kde.krita", self.flatpak_calls("install"))
        self.assertEqual(self.imported, [])

    def test_a_list_missing_many_apps_at_once_is_not_trusted(self):
        self.a.main([])
        before = set(self.installed)
        self.installed.clear()                                  # flatpak list came back empty
        self.calls.clear()
        self.a.main([])
        self.assertEqual(self.imported, [])
        self.assertEqual(self.flatpak_calls("install"), [])
        self.installed = before

    def test_several_apps_deleted_by_hand_are_one_save(self):
        self.a.main([])
        self.installed -= {"org.kde.krita", "org.blender.Blender"}
        self.a.main([])
        self.assertEqual(self.imported, [{"apps.blender": False, "apps.krita": False}])

    def test_the_wizard_choice_is_marked_before_forge_apply_starts_the_installer(self):
        seen = []
        real = self.fake_run

        def run(cmd, timeout=60):
            if cmd[0] == self.a.APPLY_CMD[0]:
                seen.append(os.path.exists(os.path.join(self.a.STATE_DIR, "chosen")))
            return real(cmd, timeout)
        self.a.run_cmd = run
        self.users = []
        with open(os.path.join(self.a.SPOOL_DIR, "apps.ini"), "w") as f:
            f.write("[apps]\ndiscord=false\n")
        self.assertEqual(self.a.main(["--apply-setup"]), 0)
        self.assertEqual(seen, [True])

    def test_a_choice_not_saved_is_kept_and_saved_later(self):
        real = self.fake_run
        self.a.run_cmd = lambda cmd, timeout=60: (1, "busy") if cmd[0] == self.a.APPLY_CMD[0] else real(cmd, timeout)
        with open(os.path.join(self.a.SPOOL_DIR, "apps.ini"), "w") as f:
            f.write("[apps]\ndiscord=false\n")
        self.assertNotEqual(self.a.main(["--apply-setup"]), 0)
        self.a.run_cmd = real
        self.a.main([])
        self.assertEqual(self.imported[0], {"apps.discord": False})
        self.assertFalse(os.path.exists(os.path.join(self.a.STATE_DIR, "pending.json")))

    def test_the_wizards_file_is_ignored_long_after_setup(self):
        open(os.path.join(self.a.STATE_DIR, "chosen"), "w").close()
        os.utime(os.path.join(self.a.STATE_DIR, "chosen"), (self.now - 3 * 3600, self.now - 3 * 3600))
        with open(os.path.join(self.a.SPOOL_DIR, "apps.ini"), "w") as f:
            f.write("[apps]\ndiscord=false\n")
        self.assertNotEqual(self.a.main(["--apply-setup"]), 0)
        self.assertEqual(self.imported, [])
        self.assertFalse(os.path.lexists(os.path.join(self.a.SPOOL_DIR, "apps.ini")))

    def test_the_wizards_file_can_change_the_choice_soon_after(self):
        # Back and Next again in the wizard.
        open(os.path.join(self.a.STATE_DIR, "chosen"), "w").close()
        os.utime(os.path.join(self.a.STATE_DIR, "chosen"), (self.now - 60, self.now - 60))
        with open(os.path.join(self.a.SPOOL_DIR, "apps.ini"), "w") as f:
            f.write("[apps]\ndiscord=true\n")
        self.assertEqual(self.a.main(["--apply-setup"]), 0)


class InstallRemoveTest(Base):
    def test_switches_that_are_on_install_their_app(self):
        self.a.main([])
        want = {it["flatpak"] for it in catalog()["optional"] if it["kind"] == "flatpak" and self.vals[it["key"]]}
        self.assertEqual(set(self.flatpak_calls("install")), want)
        self.assertNotIn("net.retrodeck.retrodeck", self.flatpak_calls("install"))   # off by default
        for c in self.calls:
            if c[:2] == ["flatpak", "install"]:
                self.assertEqual(c[2:6], ["--system", "--noninteractive", "--or-update", "flathub"])

    def test_turning_a_switch_off_deletes_the_app_once(self):
        self.a.main([])
        self.vals["apps.discord"] = False
        self.calls.clear()
        self.a.main([])
        self.assertEqual(self.flatpak_calls("uninstall"), ["com.discordapp.Discord"])
        self.assertIn(["flatpak", "uninstall", "--system", "--noninteractive", "--unused"], self.calls)
        self.assertNotIn("com.discordapp.Discord", self.installed)
        self.calls.clear()
        self.a.main([])
        self.assertEqual(self.flatpak_calls("uninstall"), [])

    def test_an_app_you_installed_yourself_is_left_alone(self):
        # RetroDECK installed from Discover while its switch was never on.
        self.installed.add("net.retrodeck.retrodeck")
        self.a.main([])
        self.assertNotIn("net.retrodeck.retrodeck", self.flatpak_calls("uninstall"))
        self.assertIn("net.retrodeck.retrodeck", self.installed)

    def test_an_app_you_deleted_yourself_is_not_put_back(self):
        self.a.main([])
        self.installed.discard("org.kde.krita")      # removed in Discover
        self.calls.clear()
        self.a.main([])
        self.assertNotIn("org.kde.krita", self.flatpak_calls("install"))
        self.assertIn({"apps.krita": False}, self.imported)    # its switch now says off

    def test_a_failed_install_is_tried_again_next_time(self):
        self.fail.add("org.blender.Blender")
        self.a.main([])
        self.assertFalse(self.state()["installed"].get("apps.blender"))
        self.fail.clear()
        self.calls.clear()
        self.a.main([])
        self.assertIn("org.blender.Blender", self.flatpak_calls("install"))
        self.assertTrue(self.state()["installed"]["apps.blender"])
        self.assertEqual(self.imported, [])          # a failed install is not "deleted by you"

    def test_you_are_told_what_changed(self):
        self.a.main([])
        self.assertTrue(any(b.startswith("Installed: Discord") for _, b in self.notes), self.notes)
        self.notes.clear()
        self.a.main([])
        self.assertEqual(self.notes, [])             # nothing changed, nothing to say

    def test_virtual_machines_are_left_to_forge_virt(self):
        self.vals["virt.enabled"] = True
        self.a.main([])
        self.assertFalse([c for c in self.calls if "virt" in " ".join(c)])

    def test_broken_settings_change_nothing(self):
        self.a.settings = lambda: None
        self.assertEqual(self.a.main([]), 1)
        self.assertEqual(self.calls, [])


class AndroidTest(Base):
    def test_turning_android_on_sets_it_up(self):
        self.vals["apps.android"] = True
        self.a.main([])
        self.assertIn(["systemctl", "enable", "--now", "waydroid-container.service"], self.calls)
        init = [c for c in self.calls if c[:2] == ["waydroid", "init"]]
        self.assertEqual(init, [["waydroid", "init", "-c", "https://ota.waydro.id/system",
                                 "-v", "https://ota.waydro.id/vendor"]])
        self.assertIn(["restorecon", "-R", self.a.WAYDROID_DIR], self.calls)
        self.calls.clear()
        self.a.main([])
        self.assertFalse([c for c in self.calls if c[0].startswith("waydroid")])   # set up once

    def test_nvidia_uses_bazzites_default_mode_and_redoes_it_after_driver_updates(self):
        self.nvidia = True
        self.vals["apps.android"] = True
        self.a.main([])
        setup = [c for c in self.calls if c[0] == "waydroid-nvidia-setup"]
        self.assertEqual(setup, [["waydroid-nvidia-setup", "--mode", "nvidia-prolapse"]])
        stop = self.calls.index(["systemctl", "stop", "waydroid-container.service"])
        self.assertLess(stop, self.calls.index(setup[0]))       # it fails while the container runs
        self.calls.clear()
        self.a.main([])
        self.assertFalse([c for c in self.calls if c[0] == "waydroid-nvidia-setup"])
        self.versions = "6.18.0|580.2"                          # new kernel or driver
        self.a.main([])
        self.assertEqual(len([c for c in self.calls if c[0] == "waydroid-nvidia-setup"]), 1)

    def test_turning_android_off_deletes_the_android_system(self):
        self.vals["apps.android"] = True
        self.a.main([])
        self.vals["apps.android"] = False
        self.calls.clear()
        self.a.main([])
        self.assertIn(["systemctl", "disable", "--now", "waydroid-container.service"], self.calls)
        self.assertFalse(os.path.lexists(self.a.WAYDROID_DIR))

    def test_a_failed_nvidia_setup_restarts_android_and_waits_before_trying_again(self):
        self.nvidia = True
        self.vals["apps.android"] = True
        self.a.main([])
        self.versions = "6.18.0|580.2"
        self.setup_fails = True
        self.notes.clear()
        self.calls.clear()
        self.a.main([])
        self.assertIn(["systemctl", "start", "waydroid-container.service"], self.calls)   # not left stopped
        self.calls.clear()
        self.now += 1800
        self.a.main([])                                         # 30 minutes later: not again yet
        self.assertFalse([c for c in self.calls if c[0] == "waydroid-nvidia-setup"])
        self.now += 6 * 3600
        self.setup_fails = False
        self.a.main([])
        self.assertTrue([c for c in self.calls if c[0] == "waydroid-nvidia-setup"])
        self.assertFalse([b for _, b in self.notes if "Installed" in b])   # a re-setup is not news

    def test_android_is_not_deleted_while_something_is_still_mounted_there(self):
        self.vals["apps.android"] = True
        self.a.main([])
        self.vals["apps.android"] = False
        self.mounts = [os.path.join(self.a.WAYDROID_DIR, "rootfs")]
        self.a.main([])
        self.assertTrue(os.path.isdir(os.path.join(self.a.WAYDROID_DIR, "images")))
        self.mounts = []
        self.a.main([])
        self.assertFalse(os.path.lexists(self.a.WAYDROID_DIR))

    def test_android_never_set_up_by_forge_is_left_alone(self):
        os.makedirs(os.path.join(self.a.WAYDROID_DIR, "images"))    # set up by hand (ujust)
        self.a.main([])
        self.assertTrue(os.path.isdir(self.a.WAYDROID_DIR))
        self.assertFalse([c for c in self.calls if "waydroid-container.service" in c])

    def test_the_android_folder_is_never_followed_as_a_link(self):
        self.vals["apps.android"] = True
        self.a.main([])
        self.vals["apps.android"] = False
        shutil.rmtree(self.a.WAYDROID_DIR)
        target = os.path.join(self.t, "precious")
        os.makedirs(target)
        open(os.path.join(target, "keep"), "w").close()
        os.symlink(target, self.a.WAYDROID_DIR)
        self.a.main([])
        self.assertTrue(os.path.exists(os.path.join(target, "keep")))


class SetupSpoolTest(Base):
    def spool(self, text):
        with open(os.path.join(self.a.SPOOL_DIR, "apps.ini"), "w") as f:
            f.write(text)

    def test_the_wizard_choice_is_saved_through_forge_apply(self):
        self.spool("[apps]\ndiscord=false\nretrodeck=true\nvms=true\n")
        self.assertEqual(self.a.main(["--apply-setup"]), 0)
        self.assertEqual(self.imported, [{"apps.discord": False, "apps.retrodeck": True, "virt.enabled": True}])
        self.assertTrue(os.path.exists(os.path.join(self.a.STATE_DIR, "chosen")))
        self.assertFalse(os.path.lexists(os.path.join(self.a.SPOOL_DIR, "apps.ini")))

    def test_anything_unexpected_is_refused(self):
        for bad in ("[apps]\nnot_an_app=true\n", "[apps]\ndiscord=yes\n", "discord=true\n",
                    "[apps]\ndiscord=true\n[other]\nx=1\n", "[apps]\nfirefox=false\n", "[apps]\nก=true\n",
                    "[apps]\n" + "discord=true\n" * 600):
            self.imported.clear()
            self.spool(bad)
            self.assertNotEqual(self.a.main(["--apply-setup"]), 0, bad[:40])
            self.assertEqual(self.imported, [])
            self.assertFalse(os.path.exists(os.path.join(self.a.STATE_DIR, "chosen")))

    def test_a_link_in_place_of_the_file_is_never_followed(self):
        target = os.path.join(self.t, "secret")
        with open(target, "w") as f:
            f.write("[apps]\ndiscord=false\n")
        os.symlink(target, os.path.join(self.a.SPOOL_DIR, "apps.ini"))
        self.assertNotEqual(self.a.main(["--apply-setup"]), 0)
        self.assertEqual(self.imported, [])
        self.assertTrue(os.path.exists(target))

    def test_no_file_is_fine(self):
        self.assertEqual(self.a.main(["--apply-setup"]), 0)
        self.assertEqual(self.imported, [])


class ConsistencyTest(unittest.TestCase):
    def test_every_switch_is_in_settings_with_the_agreed_default(self):
        with open(SCHEMA) as f:
            schema = {s["key"]: s for s in json.load(f)["settings"]}
        for it in catalog()["optional"]:
            s = schema[it["key"]]
            self.assertEqual(s["type"], "toggle")
            self.assertTrue(s.get("machine"), it["key"])
            # Agreed: today's apps start on; VMs, retro gaming and Android start off.
            self.assertEqual(s["default"], it["key"] not in ("apps.retrodeck", "apps.android", "virt.enabled"), it["key"])

    def test_the_boot_installer_only_has_the_always_on_apps(self):
        import yaml
        with open(os.path.join(ROOT, "recipes/common-modules.yml")) as f:
            mods = yaml.safe_load(f)["modules"]
        lists = [c.get("install", []) for m in mods if m.get("type") == "default-flatpaks"
                 for c in m.get("configurations", [])]
        self.assertEqual(lists, [catalog()["always"]])

    def test_units_and_triggers(self):
        with open(os.path.join(UNITS, "forge-apps.service")) as f:
            svc = f.read()
        self.assertIn("ExecStart=/usr/libexec/forge-apps\n", svc)
        self.assertIn("StateDirectory=forge-apps\n", svc)
        with open(os.path.join(UNITS, "forge-setup-apps.path")) as f:
            self.assertIn("PathExists=/var/lib/forge-setup/apps.ini", f.read())
        with open(os.path.join(UNITS, "forge-setup-apps.service")) as f:
            self.assertIn("ExecStart=/usr/libexec/forge-apps --apply-setup\n", f.read())
        with open(os.path.join(ROOT, "files/system/usr/libexec/forge-apply")) as f:
            self.assertIn('run("systemctl", "--no-block", "start", "forge-apps.service")', f.read())


if __name__ == "__main__":
    unittest.main()
