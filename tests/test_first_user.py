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


class InstalledRepoTests(unittest.TestCase):
    """Claude Desktop arrives with the image, so its repo is turned off once installed (as Bazzite
    does with its own extra repos): nothing on the installed system loads it. Its checks stay on."""

    def test_finalize_turns_the_claude_repo_off(self):
        with open(os.path.join(ROOT, "files/scripts/finalize.sh"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("sed -i 's/^enabled=1$/enabled=0/' /etc/yum.repos.d/forge-claude-desktop.repo", text)
        self.assertIn("grep -qx 'enabled=0' /etc/yum.repos.d/forge-claude-desktop.repo", text)
        self.assertNotIn("gpgcheck=0", text)

    def test_finalize_runs_after_the_package_install(self):
        types = [m.get("type") for m in modules()]
        scripts = [i for i, m in enumerate(modules()) if "finalize.sh" in m.get("scripts", [])]
        self.assertTrue(scripts and types.index("dnf") < scripts[0])


import subprocess  # noqa: E402
import tempfile  # noqa: E402

TOOL = os.path.join(ROOT, "files/system/usr/libexec/forge-first-user")
DROPIN = os.path.join(ROOT, "files/system/usr/lib/systemd/system/plasma-setup.service.d/forge.conf")


class FirstUserGuardTests(unittest.TestCase):
    """Review findings: Plasma Setup ran on PCs that already have accounts (it only checks for
    /etc/plasma-setup-done, which Bazzite systems never have), and after a failed account creation
    it never came back. It now runs exactly while there is no regular account."""

    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.passwd = os.path.join(self.t, "passwd")
        self.etc_group = os.path.join(self.t, "group")
        self.lib_group = os.path.join(self.t, "lib-group")

    def run_tool(self, mode, passwd):
        with open(self.passwd, "w") as f:
            f.write(passwd)
        return subprocess.run(["bash", TOOL, mode], capture_output=True, text=True,
                              env=dict(os.environ, FORGE_PASSWD=self.passwd))

    SYSTEM = "root:x:0:0::/root:/bin/bash\nforge:x:977:977::/var/lib/forge:/sbin/nologin\n" \
             "plasma-setup:x:976:976::/run/plasma-setup:/bin/bash\nnobody:x:65534:65534::/:/sbin/nologin\n"

    def test_runs_while_there_is_no_regular_account(self):
        self.assertEqual(self.run_tool("check", self.SYSTEM).returncode, 0)

    def test_skipped_once_an_account_exists(self):
        r = self.run_tool("check", self.SYSTEM + "anna:x:1000:1000::/var/home/anna:/bin/bash\n")
        self.assertEqual(r.returncode, 1)

    def test_accounts_come_from_the_user_database(self):
        # Review finding: only /etc/passwd was read, so systemd-homed or network accounts were missed;
        # and a read error let the wizard (passwordless account creation) run. getent sees them all.
        fake = os.path.join(self.t, "bin")
        os.makedirs(fake, exist_ok=True)
        with open(os.path.join(fake, "getent"), "w") as f:
            f.write('#!/bin/sh\n[ "$1" = passwd ] || exit 2\n[ -n "$GETENT_FAIL" ] && exit 2\ncat "$GETENT_DATA"\n')
        os.chmod(os.path.join(fake, "getent"), 0o755)
        data = os.path.join(self.t, "getent-data")

        def run(text, fail=False):
            with open(data, "w") as f:
                f.write(text)
            env = dict(os.environ, PATH=fake + ":" + os.environ["PATH"], GETENT_DATA=data)
            env.pop("FORGE_PASSWD", None)
            if fail:
                env["GETENT_FAIL"] = "1"
            return subprocess.run(["bash", TOOL, "check"], capture_output=True, text=True, env=env).returncode
        self.assertEqual(run(self.SYSTEM), 0)
        self.assertEqual(run(self.SYSTEM + "homed:x:60001:60001::/home/homed:/bin/bash\n"), 1)   # systemd-homed range
        self.assertEqual(run(self.SYSTEM + "anna:x:1000:1000::/home/anna:/bin/bash\n"), 1)
        self.assertEqual(run(self.SYSTEM, fail=True), 1, "a failed lookup must not start the wizard")

    def test_unreadable_passwd_does_not_start_the_wizard(self):
        r = subprocess.run(["bash", TOOL, "check"], capture_output=True, text=True,
                           env=dict(os.environ, FORGE_PASSWD=os.path.join(self.t, "missing")))
        self.assertEqual(r.returncode, 1)

    def test_unknown_mode_fails(self):
        self.assertNotEqual(self.run_tool("x", self.SYSTEM).returncode, 0)

    def test_dropin(self):
        with open(DROPIN) as f:
            text = f.read()
        self.assertIn("[Unit]\n", text)
        self.assertIn("\nConditionPathExists=\n", text)
        self.assertIn("ExecCondition=/usr/libexec/forge-first-user check\n", text)
        # The wizard's launcher also checks the "done" file itself: with no account it must go,
        # so the wizard comes back after a failed account creation. Runs only after the check.
        self.assertIn("ExecStartPre=/usr/bin/rm -f /etc/plasma-setup-done\n", text)

    def test_tool_is_executable_in_the_image(self):
        with open(os.path.join(ROOT, "files/scripts/finalize.sh")) as f:
            self.assertIn("/usr/libexec/forge-first-user", f.read())


class FlatpakRetryTests(unittest.TestCase):
    """Review finding: the first-boot app install (Firefox and the other Flatpaks) ran once,
    30 seconds after boot. Offline, or on Wi-Fi joined later in the setup wizard, it failed and only
    tried again at the next boot, so the first session had no browser."""

    def test_retries_until_it_works(self):
        path = os.path.join(ROOT, "files/system/usr/lib/systemd/system/system-flatpak-setup.service.d/forge-retry.conf")
        with open(path) as f:
            text = f.read()
        # Limited (review finding): a lasting failure, such as an app removed from Flathub or a full
        # disk, must not download and notify again every few minutes forever.
        self.assertIn("[Unit]\nStartLimitIntervalSec=2h\nStartLimitBurst=20\n", text)
        self.assertIn("[Service]\nRestart=on-failure\nRestartSec=3min\n", text)


class UserUnitTests(unittest.TestCase):
    """Review finding: Forge's user units also ran for system accounts (Plasma Setup's temporary
    user, the login screen's user). Like Bazzite's own user units, they only run for real people."""

    def test_every_user_unit_skips_system_accounts(self):
        folder = os.path.join(ROOT, "files/system/usr/lib/systemd/user")
        units = sorted(os.listdir(folder))
        self.assertTrue(units)
        for name in units:
            with open(os.path.join(folder, name)) as f:
                text = f.read()
            unit = text.split("[Unit]\n", 1)[1].split("\n[", 1)[0]
            self.assertIn("ConditionUser=!@system\n", unit + "\n", name)


class OneWelcomeTests(unittest.TestCase):
    """Review finding: a new account's first login opened Forge Welcome and Bazzite Portal together
    (Bazzite puts the Portal in /etc/skel). Forge Welcome is the one shown; the Portal stays in the menu."""

    def test_bazzite_portal_does_not_autostart_for_new_accounts(self):
        with open(os.path.join(ROOT, "files/scripts/finalize.sh")) as f:
            self.assertIn("rm -f /etc/skel/.config/autostart/bazzite-portal.desktop", f.read())


class SystemUserTests(unittest.TestCase):
    """Review finding: the forge and ollama users come only from sysusers.d, and on a PC rebased from
    Bazzite it is not sure systemd-sysusers runs again. A "+" ExecStartPre can't help (systemd looks
    up User= for every command first), so a small service without User= creates them before both."""

    def test_a_service_without_user_creates_them_first(self):
        with open(os.path.join(ROOT, "files/system/usr/lib/systemd/system/forge-sysusers.service")) as f:
            unit = f.read()
        self.assertFalse([l for l in unit.splitlines() if l.startswith("User=")])
        self.assertIn("Type=oneshot\n", unit)
        self.assertIn("ExecStart=/usr/bin/systemd-sysusers forge.conf ollama.conf\n", unit)
        for path in ("usr/lib/systemd/system/forge-core.service", "etc/systemd/system/ollama.service"):
            with open(os.path.join(ROOT, "files/system", path)) as f:
                text = f.read()
            head = text.split("[Service]", 1)[0]
            self.assertIn("Requires=forge-sysusers.service\n", head, path)
            self.assertIn("After=forge-sysusers.service\n", head, path)
            self.assertNotIn("systemd-sysusers", text.split("[Service]", 1)[1], path)


class PreinstalledAppTests(unittest.TestCase):
    def test_prism_launcher_is_preinstalled(self):
        # Minecraft Java Edition (and mods), next to the Bedrock launcher.
        apps = [a for m in modules() if m.get("type") == "default-flatpaks"
                for c in m.get("configurations", []) for a in c.get("install", [])]
        self.assertIn("org.prismlauncher.PrismLauncher", apps)
        self.assertIn("io.mrarm.mcpelauncher", apps)
