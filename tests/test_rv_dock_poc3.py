"""rvDock round 3 probes against 36ab0d4 (review only). A failing test is a reproduced finding."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_appearance_dock import Desktop, NEEDS_NODE, OWNER  # noqa: E402

PIN = "PinnedLaunchers=applications:pinned-in-animated.desktop\n"


def own(path):
    if os.geteuid() == 0:
        os.lchown(path, OWNER, OWNER)


@NEEDS_NODE
class Kept(unittest.TestCase):
    def setUp(self):
        self.d = Desktop()
        self.addCleanup(self.d.close)
        self.krc = os.path.join(self.d.config, "kremarc")
        self.kept = os.path.join(self.d.backup, "kremarc-kept")

    def ok(self, values):
        r = self.d.apply(values)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def round_trip(self, after_default=None):
        # The user had their own kremarc before Forge, pins an app while Animated runs, goes Default and back.
        self.d.write("home/.config/kremarc", "[General]\nIconSize=30\n")
        self.ok({"style": "animated"})
        self.d.write("home/.config/kremarc", self.d.read("home/.config/kremarc") + PIN)
        self.ok({"style": "default"})
        m = (os.stat(self.krc).st_mtime_ns, os.stat(self.kept).st_mtime_ns)
        if after_default:
            after_default()
        self.ok({"style": "animated"})
        return m, "pinned-in-animated" in self.d.read("home/.config/kremarc")

    def test_t1_kept_survives_when_written_in_the_same_clock_tick(self):
        lost = 0
        for _ in range(10):
            self.tearDown_one()
            m, kept = self.round_trip()
            lost += (not kept)
            print("\nT1 mtimes equal:", m[0] == m[1], "pins kept:", kept)
        self.assertEqual(lost, 0)

    def tearDown_one(self):
        self.d.close()
        self.d = Desktop()
        self.krc = os.path.join(self.d.config, "kremarc")
        self.kept = os.path.join(self.d.backup, "kremarc-kept")

    def test_t2_clock_behind_after_default(self):
        # Dual boot: the clock is hours behind after Default (RTC in local time). Krema then saves new pins.
        def krema_saves_with_clock_behind():
            self.d.write("home/.config/kremarc", "[General]\nIconSize=30\nPinnedLaunchers=applications:newest.desktop\n")
            t = os.stat(self.kept).st_mtime - 7 * 3600
            os.utime(self.krc, (t, t))
        _, _ = self.round_trip(krema_saves_with_clock_behind)
        self.assertIn("newest.desktop", self.d.read("home/.config/kremarc"))

    def test_t3_kremarc_touched_by_another_tool(self):
        # A sync or dotfile tool rewrites kremarc unchanged after Default (new mtime, same content).
        def touch():
            t = time.time() + 5
            os.utime(self.krc, (t, t))
        _, kept = self.round_trip(touch)
        self.assertTrue(kept, "pins made while Animated ran are lost")


@NEEDS_NODE
class BackupLinks(unittest.TestCase):
    def test_l5_relative_and_chained_links_into_the_backup_are_refused(self):
        d = Desktop()
        self.addCleanup(d.close)
        d.write("home/.config/kremarc", "[General]\nIconSize=30\n")
        self.assertEqual(d.apply({"style": "floating"}).returncode, 0)
        first = os.path.join(d.backup, "first", "kremarc")
        before = open(first).read()
        link = os.path.join(d.config, "kremarc")
        hop = os.path.join(d.home, "hop")
        for kind in ("relative", "chain", "kept", "dir-link"):
            for p in (link, hop):
                if os.path.lexists(p):
                    os.unlink(p)
            if kind == "relative":
                os.symlink("../.local/share/forge/appearance/dock-backup/first/kremarc", link)
            elif kind == "chain":
                os.symlink(first, hop)
                own(hop)
                os.symlink(hop, link)
            elif kind == "kept":
                k = os.path.join(d.backup, "kremarc-kept")
                with open(k, "w") as f:
                    f.write("[General]\n")
                own(k)
                os.symlink(k, link)
            else:
                os.symlink(os.path.join(d.backup, "first"), hop)     # a link to the folder, then the file in it
                own(hop)
                os.symlink(os.path.join(hop, "kremarc"), link)
            own(link)
            r = d.apply({"style": "animated"})
            print("\nL5", kind, r.returncode, r.stdout.strip()[:90])
            self.assertEqual(r.returncode, 1, kind)
            self.assertEqual(open(first).read(), before, kind)


SLOW = r'''
import time as _t
if name == "dbus-send" and any("dumpCurrentLayoutJS" in a for a in args): _t.sleep(24)
if name == "dbus-send" and any("read every panel" in a for a in args): _t.sleep(24)
if name == "systemctl" and "is-active" in args: _t.sleep(24)
if name == "systemctl" and "stop" in args:
    drop("plasma_up"); _t.sleep(40)
if name == "systemctl" and "start" in args: _t.sleep(30)
'''


@NEEDS_NODE
class Budget(unittest.TestCase):
    def test_b2_worst_case_stays_under_120s_and_restart_is_tried(self):
        d = Desktop()
        self.addCleanup(d.close)
        for name in ("dbus-send", "systemctl"):
            p = os.path.join(d.bin, name)
            with open(p) as f:
                src = f.read().replace('HEAD = "method', SLOW + 'HEAD = "method', 1)
            with open(p, "w") as f:
                f.write(src)
        import subprocess as sp
        orig = sp.run
        sp.run = lambda *a, **k: orig(*a, **dict(k, timeout=150))
        t0 = time.monotonic()
        try:
            r = d.apply({"style": "floating"})
        finally:
            sp.run = orig
        took = time.monotonic() - t0
        print("\nB2: %.1fs rc=%s %s" % (took, r.returncode, r.stdout.strip()))
        self.assertTrue([c for c in d.calls() if c[:3] == ["systemctl", "--user", "start"]])
        self.assertIn("systemctl --user start", r.stdout)
        self.assertLess(took, 115)


if __name__ == "__main__":
    unittest.main()
