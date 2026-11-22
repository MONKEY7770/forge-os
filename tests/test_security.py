"""Regression tests for the Phase 2 security review findings."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
sys.path.insert(0, LIB)
os.environ["FORGE_SCHEMA"] = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
APPLY = os.path.join(ROOT, "files/system/usr/libexec/forge-apply")

import forgecfg  # noqa: E402


def check(**changes):
    vals = forgecfg.values(overrides={})
    vals.update(changes)
    return forgecfg.validate(vals)[1]


class ValidationTest(unittest.TestCase):
    def test_newline_injection_rejected(self):
        self.assertTrue(check(**{"ai.models_dir": "/var/x\nExecStartPre=+/bin/sh -c id"}))
        self.assertTrue(check(**{"security.usb_key_label": "AB\rCD"}))

    def test_dangerous_model_dirs_rejected(self):
        for bad in ["/", "/etc", "/usr/local", "/var/mnt", "/home", "/var/lib/ollama/../../etc",
                    "/var/mnt/./x/../../etc", "relative/path", "/var/mnt/a b"]:
            self.assertTrue(check(**{"ai.models_dir": bad}), bad)

    def test_good_model_dirs_accepted(self):
        for good in ["/var/lib/ollama/models", "/var/mnt/games/models", "/var/home/me/models/",
                     "/run/media/me/SSD/models"]:
            self.assertEqual(check(**{"ai.models_dir": good}), [], good)

    def test_numbers_must_be_whole_and_finite(self):
        self.assertTrue(check(**{"core.port": 47900.5}))
        self.assertTrue(check(**{"core.port": "inf"}))
        self.assertTrue(check(**{"core.port": "1e400"}))
        self.assertTrue(check(**{"agents.cpu_weight": "nan"}))
        self.assertEqual(check(**{"core.port": "48000"}), [])


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.env = dict(os.environ, FORGE_ROOT=self.root, FORGE_DRYRUN="1",
                        FORGE_CONF=os.path.join(self.root, "etc/forge/settings.json"),
                        PYTHONPATH=LIB)

    def run_apply(self, *args, **env):
        e = dict(self.env, **env)
        return subprocess.run([sys.executable, APPLY, *args], env=e, capture_output=True, text=True)

    def test_forge_user_strict_argv(self):
        r = self.run_apply("--import", "/run/forge-core/a.json", "--import", "/etc/x", SUDO_USER="forge")
        self.assertEqual(r.returncode, 2)
        r = self.run_apply(SUDO_USER="forge")
        self.assertEqual(r.returncode, 2)

    def test_forge_user_only_from_run_dir(self):
        f = os.path.join(self.root, "x.json")
        json.dump({}, open(f, "w"))
        r = self.run_apply("--import", f, SUDO_USER="forge")
        self.assertEqual(r.returncode, 2)

    def test_injected_import_never_written(self):
        f = os.path.join(self.root, "evil.json")
        json.dump({"ai.models_dir": "/var/x\nExecStartPre=+/bin/sh -c id"}, open(f, "w"))
        r = self.run_apply("--import", f)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc/systemd/system/ollama.service.d")))

    def test_core_control_is_delayed_and_unique(self):
        r = self.run_apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [l for l in r.stdout.splitlines() if "forge-core" in l and "systemd-run" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("--on-active=2", lines[0])
        self.assertIn("RemainAfterElapse=no", lines[0])
        # Forge Core control comes after everything else.
        self.assertTrue(r.stdout.strip().splitlines()[-1].startswith("would run: systemd-run"))


class TestEnvironmentTest(unittest.TestCase):
    def test_no_stale_installed_library_shadows_the_repo(self):
        # forge-apply and forge-config look in /usr/lib/forge first. An old copy there would make
        # the tests check old code without anyone noticing.
        import filecmp
        installed = "/usr/lib/forge/forgecfg.py"
        repo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "files/system/usr/lib/forge/forgecfg.py")
        if os.path.exists(installed):
            self.assertTrue(filecmp.cmp(installed, repo, shallow=False),
                            f"{installed} differs from the repo: remove it before running the tests")

if __name__ == "__main__":
    unittest.main()

