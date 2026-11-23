"""The first user account: the installer ISO uses Fedora Kinoite's installer, which leaves
account creation to Plasma Setup on first boot. Bazzite removes Plasma Setup (its own installer
creates the account), so without these lines a fresh install has no account to log in to."""
import os
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def modules():
    with open(os.path.join(ROOT, "recipes/common-modules.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)["modules"]


class FirstUserTests(unittest.TestCase):
    def test_plasma_setup_is_installed(self):
        packages = [p for m in modules() if m.get("type") == "dnf"
                    for p in m.get("install", {}).get("packages", [])]
        self.assertIn("plasma-setup", packages)

    def test_plasma_setup_runs_on_first_boot(self):
        enabled = [u for m in modules() if m.get("type") == "systemd"
                   for u in m.get("system", {}).get("enabled", [])]
        self.assertIn("plasma-setup.service", enabled)

    def test_the_iso_uses_the_kinoite_installer(self):
        # If the ISO moves to an installer that creates the account itself, Plasma Setup can go.
        with open(os.path.join(ROOT, ".github/workflows/build-iso.yml"), encoding="utf-8") as f:
            self.assertIn('VARIANT="Kinoite"', f.read())


if __name__ == "__main__":
    unittest.main()
