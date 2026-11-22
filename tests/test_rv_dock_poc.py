"""rvDock proof-of-concept tests for findings in forge-appearance-dock (review only, not fixes).
Each test asserts the behavior the docs promise, so a failing test is a reproduced finding."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_appearance_dock import Desktop, STOCK_PANELS, NEEDS_NODE, load_backend  # noqa: E402


def set_panel(d, pid, **props):
    panels = json.loads(d.read("state/panels.json"))
    for p in panels:
        if p["id"] == pid:
            p["props"].update(props)
    d.write("state/panels.json", json.dumps(panels))


@NEEDS_NODE
class PoC(unittest.TestCase):
    def setUp(self):
        self.d = Desktop()
        self.addCleanup(self.d.close)

    def ok(self, values):
        r = self.d.apply(values)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_f1_pins_made_in_krema_survive_default_and_back(self):
        # README: "Other Krema settings you change in Krema itself (pinned apps, for example) are kept."
        self.ok({"style": "animated"})
        krc = self.d.read("home/.config/kremarc")
        # The user pins apps in Krema; Krema writes them to kremarc.
        self.d.write("home/.config/kremarc", krc + "PinnedLaunchers=applications:firefox.desktop,applications:steam.desktop\n")
        self.ok({"style": "default"})
        self.ok({"style": "animated"})
        self.assertIn("PinnedLaunchers=applications:firefox.desktop", self.d.read("home/.config/kremarc"))

    def test_f2_floating_after_default_keeps_later_manual_panel_changes(self):
        self.ok({"style": "floating"})
        self.ok({"style": "default"})
        # Later the user moves their second panel to the right edge by hand.
        set_panel(self.d, 7, location="right", height=40)
        self.ok({"style": "floating", "alignment": "left"})
        self.assertEqual(self.d.panels()[7]["location"], "right", "a panel Forge does not style was reverted")

    def test_f2b_tweaking_floating_keeps_manual_changes_to_other_panels(self):
        self.ok({"style": "floating"})
        set_panel(self.d, 7, opacity="translucent")
        self.ok({"style": "floating", "icon_size": 64})
        self.assertEqual(self.d.panels()[7]["opacity"], "translucent")

    def test_f3_non_ascii_pinned_launcher_list_is_kept(self):
        self.d.write("home/.config/kremarc",
                     "[General]\nPinnedLaunchers=applications:org.kde.dolphin.desktop,applications:NieR™.desktop\n")
        self.ok({"style": "animated"})
        self.assertIn("org.kde.dolphin.desktop", self.d.read("home/.config/kremarc"))

    def test_f4_symlinked_kremarc_survives_a_round_trip(self):
        real = self.d.write("home/dotfiles-kremarc", "[General]\nIconSize=30\n")
        os.symlink(real, os.path.join(self.d.config, "kremarc"))
        self.ok({"style": "animated"})
        self.ok({"style": "default"})
        self.assertTrue(os.path.islink(os.path.join(self.d.config, "kremarc")), "the dotfile link is gone")

    def test_f5_damaged_backup_leaves_a_way_out(self):
        self.ok({"style": "floating"})
        os.unlink(os.path.join(self.d.backup, "first", "meta.json"))
        r = self.d.apply({"style": "default"})
        r2 = self.d.apply({"style": "floating"})
        print("\nF5 default:", r.returncode, r.stdout.strip(), "\nF5 floating:", r2.returncode, r2.stdout.strip())
        self.assertTrue(r.returncode == 0 or "delete" in r.stdout.lower() or "remove" in r.stdout.lower())

    def test_f6_failed_plasmashell_restart_is_reported(self):
        # systemctl start fails (for example the unit's start limit is hit); its exit code is ignored.
        fake = os.path.join(self.d.bin, "systemctl")
        src = open(fake).read().replace('if verb[0] == "start": touch("plasma_up")',
                                        'if verb[0] == "start": print("Job failed", file=sys.stderr); sys.exit(1)')
        with open(fake, "w") as f:
            f.write(src)
        r = self.d.apply({"style": "floating"})
        print("\nF6:", r.returncode, r.stdout.strip())
        self.assertFalse(self.d.up("plasma"))
        self.assertIn("systemctl --user start", r.stdout, "user is told only to apply again; no desktop to do it")

    def test_f7_animated_partial_failure_leaves_autostart(self):
        open(os.path.join(self.d.state, "locked"), "w").close()
        # take the backup first while unlocked
        os.unlink(os.path.join(self.d.state, "locked"))
        self.ok({"style": "floating"})
        open(os.path.join(self.d.state, "locked"), "w").close()
        r = self.d.apply({"style": "animated"})
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(self.d.config, "autostart/forge-krema.desktop")),
                         "failed Animated apply still makes Krema start at next login")


class Injection(unittest.TestCase):
    def test_kremarc_and_script_injection_attempts(self):
        b = load_backend()
        hostile = ("[General]\nIconSize=48\n[General]\nPinnedLaunchers=a\\nIconSize=1\n"
                   "[Screen-DP-1]\nIconSize=99\nPinnedLaunchers=x\n[Screen-../x]\nPinnedLaunchers=y\n"
                   "[General][$i]\nIconSize=5\n[Screen-A]\nPinnedLaunchers[$e]=$(rm)\nIconSize[$i]=3\n"
                   "[General]\nTintColor=#123456\n[Evil]\nX=1\n PinnedLaunchers = a,b\n")
        v = b.clean_values({"style": "animated", "tint": "#ABCDEF\n[Evil]"})
        out = b.kremarc_text(v, hostile)
        groups = [l for l in out.splitlines() if l.startswith("[")]
        self.assertTrue(all(g == "[General]" or g.startswith("[Screen-") for g in groups), groups)
        self.assertNotIn("[$", out)
        self.assertNotIn("Evil", out)
        self.assertNotIn("../", out)
        s = b.panel_script(b.clean_saved([{"id": 1, "props": {"location": "top\";evil()//", "height": 2 ** 40}},
                                          {"id": "1;evil()", "props": {}}, {"id": True, "props": {}}]),
                           b.style_props(b.clean_values({"style": "floating", "position": "x evil()"})))
        self.assertNotIn("evil", s)


if __name__ == "__main__":
    unittest.main()
