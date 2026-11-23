"""The system and the installer show "Forge OS", not Bazzite or Fedora Kinoite. Only display names
change: ID, VARIANT_ID and the other fields that Bazzite's tools and the installer's settings
(chosen by ID and VARIANT_ID) read stay the same."""
import os
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "files/scripts/brand-os-release.sh")
TEMPLATE = os.path.join(ROOT, "iso/forge-installer-name.tmpl")

BAZZITE = '''NAME="Bazzite"
VERSION="45.20261122 (Kinoite)"
RELEASE_TYPE=stable
ID=bazzite
ID_LIKE="fedora"
VERSION_ID=45
VERSION_CODENAME="Kinoite"
PRETTY_NAME="Bazzite"
ANSI_COLOR="0;38;2;138;43;226"
LOGO=bazzite-logo
DEFAULT_HOSTNAME="bazzite"
VARIANT="Kinoite"
VARIANT_ID=bazzite
BOOTLOADER_NAME="Bazzite 45.20261122"
IMAGE_ID="bazzite-45.20261122"
'''


class OsReleaseTests(unittest.TestCase):
    def run_script(self, text, release="Bazzite release 45 (Kinoite)\n"):
        tmp = tempfile.mkdtemp()
        osr, sysr = os.path.join(tmp, "os-release"), os.path.join(tmp, "system-release")
        with open(osr, "w") as f:
            f.write(text)
        with open(sysr, "w") as f:
            f.write(release)
        r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True,
                           env=dict(os.environ, FORGE_OS_RELEASE=osr, FORGE_SYSTEM_RELEASE=sysr))
        with open(osr) as f, open(sysr) as g:
            return r, f.read(), g.read()

    def test_display_names_change_and_ids_stay(self):
        r, osr, sysr = self.run_script(BAZZITE)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('NAME="Forge OS"\n', osr)
        self.assertIn('PRETTY_NAME="Forge OS 45"\n', osr)
        self.assertIn('BOOTLOADER_NAME="Forge OS 45"\n', osr)
        for line in ("ID=bazzite", "VARIANT_ID=bazzite", "VERSION_ID=45", 'ID_LIKE="fedora"', 'DEFAULT_HOSTNAME="bazzite"'):
            self.assertIn(line + "\n", osr)
        self.assertNotIn("Bazzite\"", osr.replace('VERSION="45.20261122 (Kinoite)"', ""))
        self.assertEqual(sysr, "Forge OS release 45 (Kinoite)\n")   # GRUB's distributor name

    def test_missing_fields_are_added(self):
        text = "\n".join(l for l in BAZZITE.splitlines() if not l.startswith(("PRETTY_NAME", "BOOTLOADER_NAME"))) + "\n"
        r, osr, _ = self.run_script(text)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('PRETTY_NAME="Forge OS 45"\n', osr)
        self.assertIn('BOOTLOADER_NAME="Forge OS 45"\n', osr)

    def test_no_version_fails_the_build(self):
        r, _, _ = self.run_script(BAZZITE.replace("VERSION_ID=45\n", ""))
        self.assertNotEqual(r.returncode, 0)

    def test_the_script_runs_in_the_image_build(self):
        with open(os.path.join(ROOT, "recipes/common-modules.yml")) as f:
            mods = yaml.safe_load(f)["modules"]
        scripts = [s for m in mods if m.get("type") == "script" for s in m.get("scripts", [])]
        self.assertIn("brand-os-release.sh", scripts)


class InstallerNameTests(unittest.TestCase):
    def test_template_renames_only_the_display_names(self):
        with open(TEMPLATE) as f:
            lines = [l for l in f.read().splitlines() if l.strip() and not l.startswith("##")]
        self.assertEqual(lines, [
            'replace "^NAME=.*" "NAME=\\"Forge OS\\"" usr/lib/os-release',
            'replace "^PRETTY_NAME=.*" "PRETTY_NAME=\\"Forge OS\\"" usr/lib/os-release',
        ])

    def test_iso_build_passes_the_template(self):
        with open(os.path.join(ROOT, ".github/workflows/build-iso.yml")) as f:
            text = f.read()
        self.assertIn('ADDITIONAL_TEMPLATES="/github/workspace/iso/forge-installer-name.tmpl"', text)
        self.assertNotIn('ADDITIONAL_TEMPLATES=""', text)


if __name__ == "__main__":
    unittest.main()
