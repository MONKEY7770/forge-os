"""P1 laptop: switch the performance mode when the charger is plugged in or out (optional)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/libexec/forge-power-switch")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
RULE = os.path.join(ROOT, "files/system/usr/lib/udev/rules.d/90-forge-power.rules")
UNIT = os.path.join(ROOT, "files/system/usr/lib/systemd/system/forge-power-switch.service")


class PowerSwitchTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.sys = os.path.join(self.t, "sys")
        self.bin = os.path.join(self.t, "bin")
        self.log = os.path.join(self.t, "calls")
        os.makedirs(self.bin)
        for name in ("systemctl", "powerprofilesctl", "ollama"):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                if name == "ollama":
                    f.write(f'#!/bin/sh\necho "{name} $*" >> {self.log}\n'
                            '[ "$1" = ps ] && printf "NAME ID\\nqwen3:8b abc\\n"\nexit 0\n')
                else:
                    f.write(f'#!/bin/sh\necho "{name} $*" >> {self.log}\n')
            os.chmod(p, 0o755)
        self.conf = os.path.join(self.t, "settings.json")
        self.state = os.path.join(self.t, "state")
        self.battery(True)

    def battery(self, on_ac):
        for name, kind, extra in (("BAT0", "Battery", {"capacity": "50"}),
                                  ("AC", "Mains", {"online": "1" if on_ac else "0"})):
            d = os.path.join(self.sys, "class/power_supply", name)
            os.makedirs(d, exist_ok=True)
            for k, v in dict(type=kind, **extra).items():
                with open(os.path.join(d, k), "w") as f:
                    f.write(v + "\n")

    def settings(self, **vals):
        with open(self.conf, "w") as f:
            json.dump({k.replace("__", "."): v for k, v in vals.items()}, f)

    def run_tool(self):
        env = dict(os.environ, FORGE_SYSFS=self.sys, FORGE_CONF=self.conf, FORGE_SCHEMA=SCHEMA,
                   FORGE_LIB=LIB, FORGE_POWER_STATE=self.state, PATH=self.bin + ":" + os.environ["PATH"])
        return subprocess.run([sys.executable, TOOL], env=env, capture_output=True, text=True, timeout=30)

    def calls(self):
        return open(self.log).read().splitlines() if os.path.exists(self.log) else []

    def test_a_mouse_battery_on_a_desktop_switches_nothing(self):
        shutil.rmtree(os.path.join(self.sys, "class/power_supply"))
        d = os.path.join(self.sys, "class/power_supply/hidpp_battery_0")
        os.makedirs(d)
        for k, v in (("type", "Battery"), ("scope", "Device"), ("capacity", "40")):
            with open(os.path.join(d, k), "w") as f:
                f.write(v + "\n")
        a = os.path.join(self.sys, "class/power_supply/AC")
        os.makedirs(a)
        for k, v in (("type", "Mains"), ("online", "0")):
            with open(os.path.join(a, k), "w") as f:
                f.write(v + "\n")
        self.settings(modes__auto_switch=True)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(self.calls(), [])

    def fake(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write(f'#!/bin/sh\necho "{name} $*" >> {self.log}\n' + body)
        os.chmod(p, 0o755)

    def test_models_are_not_asked_for_when_ollama_is_not_running(self):
        # Then anyone could be listening on Ollama's port and answer with model names.
        self.fake("systemctl", '[ "$1" = is-active ] && exit 3\nexit 0\n')
        self.settings(modes__auto_switch=True, modes__on_battery="quiet")
        self.battery(False)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertFalse([c for c in self.calls() if c.startswith("ollama")])

    def test_a_long_model_list_is_capped(self):
        rows = "".join(f"m{i}:latest x\\n" for i in range(50))
        self.fake("ollama", f'[ "$1" = ps ] && printf "NAME ID\\n{rows}"\nexit 0\n')
        self.settings(modes__auto_switch=True, modes__on_battery="quiet")
        self.battery(False)
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertLessEqual(len([c for c in self.calls() if c.startswith("ollama stop")]), 8)

    def test_worst_case_fits_in_the_unit_time_limit(self):
        # set-property, powerprofilesctl, is-active and ollama ps at 60 s, plus at most 8 stops.
        src = open(TOOL).read()
        m = re.search(r'run\("ollama", "stop", name, timeout=(\d+)\)', src)
        self.assertTrue(m, "ollama stop needs its own short time limit")
        limit = re.search(r"TimeoutStartSec=(\d+)min", open(UNIT).read())
        self.assertTrue(limit)
        self.assertLess(4 * 60 + 8 * int(m.group(1)), int(limit.group(1)) * 60)

    def test_unit_has_an_overall_time_limit(self):
        self.assertIn("TimeoutStartSec=", open(UNIT).read())

    def test_off_by_default_does_nothing(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), [])

    def test_on_battery_switches_to_the_battery_mode(self):
        self.settings(modes__auto_switch=True, modes__on_battery="quiet", modes__quiet__power="power-saver")
        self.battery(False)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertIn("powerprofilesctl set power-saver", calls)
        self.assertTrue(any(c.startswith("systemctl set-property --runtime agents.slice CPUWeight=")
                            for c in calls), calls)
        self.assertIn("ollama stop qwen3:8b", calls)       # quiet mode unloads models by default

    def test_on_charger_switches_back(self):
        self.settings(modes__auto_switch=True, modes__on_charger="agents")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("powerprofilesctl set performance", self.calls())

    def test_same_state_twice_does_nothing_the_second_time(self):
        self.settings(modes__auto_switch=True)
        self.run_tool()
        n = len(self.calls())
        self.run_tool()
        self.assertEqual(len(self.calls()), n)

    def test_desktop_without_battery_does_nothing(self):
        shutil.rmtree(os.path.join(self.sys, "class/power_supply/BAT0"))
        self.settings(modes__auto_switch=True)
        self.run_tool()
        self.assertEqual(self.calls(), [])

    def test_bad_settings_do_nothing(self):
        self.settings(modes__auto_switch=True, modes__on_battery="; rm -rf /")
        self.battery(False)
        r = self.run_tool()
        self.assertEqual(self.calls(), [])
        self.assertNotIn("Traceback", r.stderr)

    def test_udev_rule_and_unit(self):
        rule = open(RULE).read()
        self.assertIn('SUBSYSTEM=="power_supply"', rule)
        self.assertIn("systemctl --no-block start forge-power-switch.service", rule)
        unit = open(UNIT).read()
        self.assertIn("ExecStart=/usr/libexec/forge-power-switch", unit)
        self.assertIn("Type=oneshot", unit)


if __name__ == "__main__":
    unittest.main()
