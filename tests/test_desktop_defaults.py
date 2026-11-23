"""Forge's desktop defaults are added to the system-wide KDE files Bazzite already ships
(/etc/xdg/kdeglobals locks the update page and sets fonts), key by key with kwriteconfig6, never by
replacing the files. They are defaults only: each person's own settings win, nothing is locked."""
import os
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "files/scripts/desktop-defaults.sh")


class DesktopDefaultsTests(unittest.TestCase):
    def run_script(self, fail=False):
        t = tempfile.mkdtemp()
        os.makedirs(os.path.join(t, "bin"))
        log = os.path.join(t, "calls")
        with open(os.path.join(t, "bin", "kwriteconfig6"), "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> "{log}"\n' + ("exit 1\n" if fail else ""))
        os.chmod(os.path.join(t, "bin", "kwriteconfig6"), 0o755)
        r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True,
                           env=dict(os.environ, PATH=os.path.join(t, "bin") + ":" + os.environ["PATH"]))
        calls = open(log).read().splitlines() if os.path.exists(log) else []
        return r, calls

    def test_keys(self):
        r, calls = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        for c in ("--file /etc/xdg/kdeglobals --group General --key ColorScheme ForgeTempered",
                  "--file /etc/xdg/kdeglobals --group KDE --key AnimationDurationFactor 0.8",
                  "--file /etc/xdg/kwinrc --group Plugins --key magiclampEnabled true",
                  "--file /etc/xdg/kwinrc --group Plugins --key squashEnabled false"):
            self.assertIn(c, calls)

    def test_never_locks_or_replaces(self):
        with open(SCRIPT) as f:
            text = f.read()
        self.assertNotIn("[$i]", text)
        self.assertNotRegex(text, r"(>|cp |mv |install ).*/etc/xdg/(kdeglobals|kwinrc)")

    def test_a_failed_write_stops_the_build(self):
        r, _ = self.run_script(fail=True)
        self.assertNotEqual(r.returncode, 0)

    def test_scheme_files_exist(self):
        for name in ("ForgeTempered", "ForgeTemperedDark"):
            path = os.path.join(ROOT, f"files/system/usr/share/color-schemes/{name}.colors")
            with open(path) as f:
                self.assertIn(f"ColorScheme={name}\n", f.read())

    def test_runs_in_the_image_build(self):
        with open(os.path.join(ROOT, "recipes/common-modules.yml")) as f:
            mods = yaml.safe_load(f)["modules"]
        scripts = [s for m in mods if m.get("type") == "script" for s in m.get("scripts", [])]
        self.assertIn("desktop-defaults.sh", scripts)

    def test_plex_fonts_are_the_defaults(self):
        r, calls = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        for key, size in (("font", 10), ("menuFont", 10), ("toolBarFont", 9), ("smallestReadableFont", 8)):
            self.assertIn(f"--file /etc/xdg/kdeglobals --group General --key {key} "
                          f"IBM Plex Sans,{size},-1,5,50,0,0,0,0,0", calls)
        self.assertIn("--file /etc/xdg/kdeglobals --group General --key fixed "
                      "IBM Plex Mono,10,-1,5,50,0,0,0,0,0", calls)

    def test_plex_font_packages_are_installed(self):
        with open(os.path.join(ROOT, "recipes/common-modules.yml")) as f:
            mods = yaml.safe_load(f)["modules"]
        pkgs = [p for m in mods if m.get("type") == "dnf"
                for p in (m.get("install") or {}).get("packages", [])]
        for p in ("ibm-plex-sans-fonts", "ibm-plex-sans-thai-fonts", "ibm-plex-mono-fonts"):
            self.assertIn(p, pkgs)

    def test_thai_text_uses_plex_sans_thai(self):
        import xml.etree.ElementTree as ET
        conf = os.path.join(ROOT, "files/system/etc/fonts/conf.d/60-forge-plex.conf")
        root = ET.parse(conf).getroot()
        pairs = [(a.findtext("family"), [f.text for f in a.iter("family")][1:]) for a in root.iter("alias")]
        self.assertIn(("IBM Plex Sans", ["IBM Plex Sans Thai"]), pairs)


if __name__ == "__main__":
    unittest.main()
