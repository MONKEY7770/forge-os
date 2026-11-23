"""Review finding: this base uses tuned-ppd, which has power-profiles-daemon's D-Bus API but no
powerprofilesctl command, so forge-mode, forge-game and the charger switch never changed the power
profile (the error was swallowed). A small stand-in does what Forge uses: get and set."""
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/libexec/forge/powerprofilesctl")

FAKE_BUSCTL = r'''#!/bin/sh
echo "busctl $*" >> "$CALLS"
[ "$3" = "${ANSWERS:-org.freedesktop.UPower.PowerProfiles}" ] || exit 1
case "$2" in
  get-property) echo 's "balanced"' ;;
  set-property) exit 0 ;;
esac
'''


class StandInTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.t, "bin"))
        with open(os.path.join(self.t, "bin", "busctl"), "w") as f:
            f.write(FAKE_BUSCTL)
        os.chmod(os.path.join(self.t, "bin", "busctl"), 0o755)
        self.calls = os.path.join(self.t, "calls")

    def run_tool(self, *args, answers=None):
        env = dict(os.environ, PATH=os.path.join(self.t, "bin") + ":" + os.environ["PATH"], CALLS=self.calls)
        if answers:
            env["ANSWERS"] = answers
        return subprocess.run(["bash", TOOL, *args], capture_output=True, text=True, env=env)

    def test_get(self):
        r = self.run_tool("get")
        self.assertEqual((r.returncode, r.stdout), (0, "balanced\n"), r.stderr)

    def test_set(self):
        r = self.run_tool("set", "performance")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.calls) as f:
            self.assertIn("busctl --system set-property org.freedesktop.UPower.PowerProfiles "
                          "/org/freedesktop/UPower/PowerProfiles org.freedesktop.UPower.PowerProfiles "
                          "ActiveProfile s performance", f.read())

    def test_older_bus_name_is_tried_too(self):
        r = self.run_tool("get", answers="net.hadess.PowerProfiles")
        self.assertEqual((r.returncode, r.stdout), (0, "balanced\n"), r.stderr)

    def test_bad_profile_or_command_is_refused(self):
        for args in (("set", "turbo"), ("set", "performance;reboot"), ("list",), ()):
            self.assertEqual(self.run_tool(*args).returncode, 2, args)

    def test_no_service_fails(self):
        self.assertEqual(self.run_tool("get", answers="none").returncode, 1)

    def test_linked_only_when_the_real_one_is_missing(self):
        with open(os.path.join(ROOT, "files/scripts/finalize.sh")) as f:
            text = f.read()
        self.assertIn("[ -e /usr/bin/powerprofilesctl ] || ln -s /usr/libexec/forge/powerprofilesctl "
                      "/usr/bin/powerprofilesctl", text)
        self.assertIn("/usr/libexec/forge/powerprofilesctl", text.split("chmod 0755", 1)[1].split("\n#", 1)[0])


if __name__ == "__main__":
    unittest.main()
