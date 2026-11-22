"""rvDock round 2 probes against f9dd614 (review only). A failing test is a reproduced finding."""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_appearance_dock import Desktop, NEEDS_NODE, OWNER, load_backend  # noqa: E402
from test_rv_dock_poc import set_panel  # noqa: E402


def chown(d, path):
    if os.geteuid() == 0:
        os.lchown(path, OWNER, OWNER)


@NEEDS_NODE
class Links(unittest.TestCase):
    def setUp(self):
        self.d = Desktop()
        self.addCleanup(self.d.close)
        self.link = os.path.join(self.d.config, "kremarc")

    def ok(self, values):
        r = self.d.apply(values)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_l1_link_into_the_first_backup_never_changes_it(self):
        self.d.write("home/.config/kremarc", "[General]\nIconSize=30\n")
        self.ok({"style": "floating"})
        first = os.path.join(self.d.backup, "first", "kremarc")
        before = open(first).read()
        os.unlink(self.link)
        os.symlink(first, self.link)
        chown(self.d, self.link)
        r = self.d.apply({"style": "animated"})
        print("\nL1:", r.returncode, r.stdout.strip())
        self.assertEqual(open(first).read(), before, "the never-overwritten backup was rewritten")

    def test_l2_link_to_a_file_of_another_user_is_refused(self):
        if os.geteuid() != 0:
            self.skipTest("needs root to make a file of another user")
        other = os.path.join(self.d.tmp, "rootfile")
        with open(other, "w") as f:
            f.write("[General]\nIconSize=30\n")
        os.chmod(other, 0o666)
        os.symlink(other, self.link)
        chown(self.d, self.link)
        r = self.d.apply({"style": "animated"})
        print("\nL2:", r.returncode, r.stdout.strip())
        self.assertEqual(r.returncode, 1)
        self.assertEqual(open(other).read(), "[General]\nIconSize=30\n")
        self.assertTrue(os.path.islink(self.link))

    def test_l3_loop_and_dir_and_device_links_are_refused(self):
        for target in ("loop", os.path.join(self.d.home, "adir"), "/dev/null"):
            if os.path.lexists(self.link):
                os.unlink(self.link)
            if target == "loop":
                a = os.path.join(self.d.config, "loopa")
                os.symlink(self.link, a)
                chown(self.d, a)
                os.symlink(a, self.link)
            else:
                if target.endswith("adir"):
                    os.makedirs(target, exist_ok=True)
                    chown(self.d, target)
                os.symlink(target, self.link)
            chown(self.d, self.link)
            r = self.d.apply({"style": "animated"})
            print("\nL3", target, r.returncode, r.stdout.strip()[:160])
            self.assertEqual(r.returncode, 1, target)
            self.assertNotIn("Traceback", r.stderr)
            self.assertTrue(os.path.islink(self.link))

    def test_l4_swap_after_check_does_not_follow(self):
        b = load_backend()
        real, elsewhere = os.path.join(self.d.home, "real-kremarc"), os.path.join(self.d.home, "elsewhere")
        for path, text in ((real, "x\n"), (elsewhere, "keep\n")):
            with open(path, "w") as f:
                f.write(text)
        os.symlink(real, self.link)
        target = b.followed(self.link)
        os.unlink(real)
        os.symlink(elsewhere, real)          # swapped between check and write
        b.write_atomic(target, "new\n")
        self.assertEqual(open(elsewhere).read(), "keep\n")

    def test_k1_stale_kept_pins_do_not_beat_newer_pins(self):
        # The user had their own Krema before Forge (so the backup has a kremarc).
        self.d.write("home/.config/kremarc", "[General]\nPinnedLaunchers=applications:old.desktop\n")
        self.ok({"style": "animated"})
        self.ok({"style": "default"})
        # Back on Default, the user keeps running their own Krema and pins something new.
        self.d.write("home/.config/kremarc", "[General]\nPinnedLaunchers=applications:newest.desktop\n")
        self.ok({"style": "animated"})
        self.assertIn("newest.desktop", self.d.read("home/.config/kremarc"))

    def test_p1_animated_leaves_other_panels_untouched(self):
        self.ok({"style": "floating"})
        set_panel(self.d, 7, location="right", opacity="translucent")
        for sp in ("top_bar", "autohide", "keep"):
            self.ok({"style": "animated", "stock_panel": sp})
            self.assertEqual(self.d.panels()[7]["location"], "right", sp)
            self.assertEqual(self.d.panels()[7]["opacity"], "translucent", sp)
        self.ok({"style": "default"})
        self.assertEqual(self.d.panels()[7]["location"], "top")


class Merge(unittest.TestCase):
    def setUp(self):
        self.b = load_backend()
        self.v = self.b.clean_values({"style": "animated"})

    def test_m1_kept_file_cannot_add_hostile_groups_or_keys(self):
        hostile = ("[General]\nX=1\nIconSize[$i]=5\n[Evil]\nIconSize=1\n[Screen-A][$i]\nIconSize=1\n"
                   "[Screen-B]\nShowDelay=9\nPinnedLaunchers=a\x0bb\nIconSize=24\n[Screen-..]\nIconSize=1\n")
        out = self.b.kremarc_text(self.v, "", hostile)
        self.assertNotIn("Evil", out)
        self.assertNotIn("$", out)
        self.assertNotIn("ShowDelay=9", out)
        self.assertNotIn("Screen-..", out)
        self.assertNotIn("\x0b", out)

    def test_m2_bom_and_line_endings(self):
        bom = "﻿[General]\nPinnedLaunchers=applications:a.desktop\n"
        out = self.b.kremarc_text(self.v, bom)
        print("\nM2 BOM kept pins:", "a.desktop" in out)
        crlf = "[General]\r\nPinnedLaunchers=applications:b.desktop\r\n"
        self.assertIn("b.desktop", self.b.kremarc_text(self.v, crlf))
        sep = "[General]\nPinnedLaunchers=x [Evil]\nIconSize=1\n"
        out = self.b.kremarc_text(self.v, sep)
        self.assertNotIn("Evil", out)
        long = "[General]\nPinnedLaunchers=" + "a" * 200000 + "\n"
        self.assertNotIn("a" * 100, self.b.kremarc_text(self.v, long))

    def test_m3_merged_output_stays_readable_next_time(self):
        a = "".join(f"[Screen-A{i}]\nPinnedLaunchers=applications:x.desktop\n" for i in range(4000))
        k = "".join(f"[Screen-B{i}]\nPinnedLaunchers=applications:x.desktop\n" for i in range(4000))
        self.assertLess(len(a.encode()), self.b.KREMARC_MAX)
        self.assertLess(len(k.encode()), self.b.KREMARC_MAX)
        out = self.b.kremarc_text(self.v, a, k)
        print("\nM3 merged bytes:", len(out.encode()), "limit", self.b.KREMARC_MAX)
        self.assertLessEqual(len(out.encode()), self.b.KREMARC_MAX)


SLOW = r'''
import time as _t
if name == "dbus-send" and any("dumpCurrentLayoutJS" in a for a in args): _t.sleep(24)
if name == "dbus-send" and any("read every panel" in a for a in args): _t.sleep(24)
if name == "systemctl" and "is-active" in args: _t.sleep(24)
if name == "systemctl" and "stop" in args:
    drop("plasma_up"); _t.sleep(40)
'''


@NEEDS_NODE
class Budget(unittest.TestCase):
    def test_b1_budget_never_skips_restarting_plasmashell(self):
        d = Desktop()
        self.addCleanup(d.close)
        for name in ("dbus-send", "systemctl"):
            p = os.path.join(d.bin, name)
            src = open(p).read().replace('HEAD = "method', SLOW + 'HEAD = "method', 1)
            with open(p, "w") as f:
                f.write(src)
        t0 = time.monotonic()
        import subprocess as sp
        orig = sp.run
        sp.run = lambda *a, **k: orig(*a, **dict(k, timeout=150))
        try:
            r = d.apply({"style": "floating"})
        finally:
            sp.run = orig
        print("\nB1: %.0fs" % (time.monotonic() - t0), r.returncode, r.stdout.strip(), r.stderr[-800:], d.calls())
        starts = [c for c in d.calls() if c[:3] == ["systemctl", "--user", "start"]]
        self.assertTrue(starts or "systemctl --user start" in r.stdout,
                        "plasmashell left stopped and the user is not told how to start it")


if __name__ == "__main__":
    unittest.main()
