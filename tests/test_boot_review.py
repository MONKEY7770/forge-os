"""Regression tests from the independent review (rvBoot) of Bootup. Each asserts the safe behaviour."""
import json
import os
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_boot_apply import (BootApplyTest, bundle, chunk, png, splash_on, FP, THEME_DIR, CONF)  # noqa: E402


def bad_deflate_png(w=16, h=12):
    # Valid CRCs and layout, but the IDAT is not a valid zlib stream: zlib raises zlib.error, not Refused.
    idat = b"\x78\x9c\xff\xff\xff\xff" + b"\x00" * 20
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def dict_png(w=4, h=4):
    # zlib stream with a preset dictionary flag (FDICT): "need dictionary" -> zlib.error
    raw = b"".join(b"\x00" + b"\x01\x02\x03\x04" * w for _ in range(h))
    c = zlib.compressobj(zdict=b"forge")
    data = c.compress(raw) + c.flush()
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", data) + chunk(b"IEND", b""))


class PocRootHelper(BootApplyTest):
    def test_poc_corrupt_frame_after_login_leaves_unrecorded_login_change(self):
        login = {"action": "on", "fingerprint": FP, "kind": "image", "format": "png"}
        b = bundle(splash=splash_on(), login=login, frames=[bad_deflate_png()],
                   login_file=("login.png", png(32, 32)))
        r = self.run_helper("apply", stdin=b)
        print("\n--- apply output ---\n" + r.text[-600:])
        dropin = self.root + "/etc/plasmalogin.conf.d/90-forge-login.conf"
        state = self.root + "/var/lib/forge-boot/state.json"
        print("drop-in exists:", os.path.exists(dropin), " state exists:", os.path.exists(state))
        r2 = self.run_helper("off")
        print("--- off output ---\n" + r2.text)
        # Safe behaviour: either nothing was written, or "off" removes what was written.
        self.assertFalse(os.path.exists(dropin), "login drop-in left behind and 'off' does not remove it")

    def test_poc_preset_dictionary_png_crashes_helper(self):
        r = self.run_helper("apply", stdin=bundle(splash=splash_on(), frames=[dict_png()]))
        self.assertNotIn("Traceback", r.text, r.text[-400:])


if __name__ == "__main__":
    unittest.main()


SLOW_DAEMON = r'''#!/usr/bin/env python3
# Simulates the client giving up (error or timeout) while the daemon's transaction still finishes later.
import json, os, subprocess, sys, time
args = sys.argv[1:]
real = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "rpm-ostree.real")
if any(a.startswith("--track=") for a in args) and not os.path.exists(os.environ["FAKE_STATE"] + ".once"):
    open(os.environ["FAKE_STATE"] + ".once", "w").close()
    subprocess.Popen([sys.executable, "-c",
        "import time,subprocess,sys; time.sleep(1.5); subprocess.run([sys.argv[1]] + sys.argv[2:])", real] + args,
        start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("error: Timeout was reached", file=sys.stderr)
    sys.exit(1)
os.execv(real, [real] + args)
'''


class PocLateTransaction(BootApplyTest):
    def test_poc_late_transaction_leaves_tracked_path_missing(self):
        import time
        real = os.path.join(self.bin, "rpm-ostree.real")
        os.rename(os.path.join(self.bin, "rpm-ostree"), real)
        self.fake("rpm-ostree", SLOW_DAEMON)
        os.environ["FAKE_REAL"] = real
        try:
            r = self.enable(frames=2)
        finally:
            pass
        print("\n--- apply output ---\n" + r.text)
        time.sleep(3)
        tracked = self.rpm()["tracked"]
        print("tracked now:", tracked, " theme dir exists:", os.path.exists(self.root + THEME_DIR))
        print("calls:", self.rpm()["calls"])
        self.assertFalse(THEME_DIR in tracked and not os.path.exists(self.root + THEME_DIR),
                         "a tracked path is missing: later deployments fail")


from test_boot_appearance import BackendTest, png_bytes, load_helper  # noqa: E402


class PocBackend(BackendTest):
    def test_poc_slow_root_part_then_off_never_turns_splash_off(self):
        import time as _t
        pic = self.add_media("p.png", png_bytes(16, 16), "image", "png")
        p = os.path.join(self.bin, "pkexec")
        with open(p, "w") as f:   # password given, root part keeps running past the backend's budget
            f.write(f"#!/bin/sh\necho \"$*\" >> {self.calls}\ncat > {self.captured}\nsleep 60\n")
        os.chmod(p, 0o755)
        r = self.apply(splash="on", splash_media=pic, extra_env={"FORGE_BOOT_BUDGET_S": "20"}, timeout=60)
        print("\n--- 1st apply ---\n" + r.out)
        # The root part finishes later and records the splash as on (what forge-boot-apply writes).
        with open(self.captured, "rb") as f:
            fp = load_helper().read_bundle(f)[0]["fingerprint"]
        os.makedirs(self.root + "/var/lib/forge-boot", exist_ok=True)
        with open(self.root + "/var/lib/forge-boot/state.json", "w") as f:
            json.dump({"version": 1, "splash": {"enabled": True, "fingerprint": fp}}, f)
        self.pkexec(0, "Boot splash: back to Bazzite's own after the next restart.")
        r = self.apply(splash="on", splash_media=pic)
        print("--- 2nd apply (same) ---\n" + r.out)
        r = self.apply(splash="off")
        print("--- 3rd apply (off) ---\n" + r.out, "rc", r.returncode)
        print("pkexec calls:", self.pk_calls())
        self.assertEqual(len(self.pk_calls()), 2, "choosing Bazzite's own never asks the root part to turn it off")


class PocCatchAll(BootApplyTest):
    def test_poc_unexpected_error_after_theme_placed_is_not_recorded(self):
        import subprocess as sp
        d = self.root + "/etc/plymouth"
        sp.run(["chattr", "+i", d], check=True)
        self.addCleanup(sp.run, ["chattr", "-i", d])
        r = self.enable(frames=2)
        print("\n--- apply ---\n" + r.text)
        sp.run(["chattr", "-i", d], check=True)
        theme = os.path.exists(self.root + THEME_DIR)
        state = open(self.root + "/var/lib/forge-boot/state.json").read() if os.path.exists(
            self.root + "/var/lib/forge-boot/state.json") else None
        print("theme dir exists:", theme, " state:", state)
        r2 = self.run_helper("off")
        print("--- off ---\n" + r2.text, "theme dir after off:", os.path.exists(self.root + THEME_DIR))
        self.assertFalse(os.path.exists(self.root + THEME_DIR), "theme left behind although the message says off undoes it")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (PocRootHelper, PocLateTransaction, PocBackend, ReviewRootHelper, ReviewConverter, PocCatchAll):
        for name in loader.getTestCaseNames(cls):
            if name.startswith("test_poc") or (cls.__name__.startswith("Review") and name in cls.__dict__):
                suite.addTest(cls(name))
    return suite


# Further regression tests for the review follow-ups (written before the fixes).

class ReviewRootHelper(BootApplyTest):
    def test_keymap_render_is_copied_for_the_layout_indicator(self):
        self.put("/usr/share/plymouth/themes/spinner/keymap-render.png", png(40, 8))
        r = self.enable()
        self.assertEqual(r.returncode, 0, r.text)
        self.assertEqual(self.read(THEME_DIR + "/keymap-render.png"),
                         self.read("/usr/share/plymouth/themes/spinner/keymap-render.png"))

    def test_every_frame_is_checked_before_anything_is_written(self):
        before = self.snapshot()
        login = {"action": "on", "fingerprint": FP, "kind": "image", "format": "png"}
        for bad in (bad_deflate_png(), dict_png()):
            r = self.run_helper("apply", stdin=bundle(splash=splash_on(), login=login, frames=[png(4, 4), bad],
                                                      login_file=("login.png", png(32, 32))))
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.text)
            self.assertEqual(self.snapshot(), before)

    def test_unexpected_error_in_one_part_still_records_the_other(self):
        import io
        import types
        from unittest import mock
        login = {"action": "on", "fingerprint": FP, "kind": "image", "format": "png"}
        data = bundle(splash=splash_on(), login=login, frames=[png(4, 4)], login_file=("login.png", png(32, 32)))
        env = {"FORGE_BOOT_ROOT": self.root, "FORGE_BOOT_TEST": "1", "FAKE_ROOT": self.root,
               "FAKE_STATE": self.state, "PATH": self.bin + ":/usr/bin:/bin"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env):
            helper = load_helper()

            def boom(*a):
                raise RuntimeError("something nobody expected")
            helper.splash_on = boom
            with mock.patch.object(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(data))), \
                    mock.patch.object(sys, "stdout", out):
                rc = helper.main(["apply"])
        self.assertEqual(rc, 1)
        self.assertIn("Boot splash", out.getvalue())
        state = json.loads(self.read("/var/lib/forge-boot/state.json"))
        self.assertTrue(state["login"]["enabled"])
        r = self.run_helper("off")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertFalse(os.path.exists(self.root + "/etc/plasmalogin.conf.d/90-forge-login.conf"))

    def test_test_switches_are_ignored_in_the_installed_helper(self):
        helper = load_helper()
        self.assertFalse(helper.test_mode_allowed("/usr/libexec/forge-boot-apply"))
        self.assertTrue(helper.test_mode_allowed("/tmp/x/repo/files/system/usr/libexec/forge-boot-apply"))

    def test_folder_swap_is_atomic_or_falls_back(self):
        helper = load_helper()
        a, b = os.path.join(self.t, "a"), os.path.join(self.t, "b")
        os.mkdir(a)
        os.mkdir(b)
        open(os.path.join(a, "1"), "w").close()
        open(os.path.join(b, "2"), "w").close()
        helper.exchange(a, b)
        self.assertEqual((os.listdir(a), os.listdir(b)), (["2"], ["1"]))


class ReviewConverter(unittest.TestCase):
    def test_svg_fallback_renders_from_a_folder_with_only_that_file(self):
        import shutil
        import subprocess
        import tempfile
        from test_boot_appearance import as_owner, LIB, OWNER
        if os.geteuid() != 0:
            self.skipTest("needs root to run as a test user")
        t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, t, True)
        os.chmod(t, 0o755)
        media = os.path.join(t, "media")
        os.mkdir(media)
        with open(os.path.join(media, "secret.png"), "wb") as f:
            f.write(png(4, 4))
        with open(os.path.join(media, "a.svg"), "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                    '<image xlink:href="secret.png" width="4" height="4"/></svg>')
        fake = os.path.join(t, "bin")
        os.mkdir(fake)
        log = os.path.join(t, "log")
        with open(os.path.join(fake, "rsvg-convert"), "w") as f:
            f.write(f'#!/bin/sh\nfor a; do last="$a"; done\nls "$(dirname "$last")" > {log}\n'
                    'while [ "$1" != "-o" ]; do shift; done; cp /dev/null "$2"\n')
        os.chmod(os.path.join(fake, "rsvg-convert"), 0o755)
        work = os.path.join(t, "work")
        os.mkdir(work)
        for p in (media, work, t):
            os.chown(p, OWNER, OWNER)
        code = ("import sys; sys.path.insert(0, %r); import forgebootmedia as m; "
                "m._fallback_decode(%r, 'image', 'svg', %r, 480, 1, scratch=%r)" %
                (LIB, os.path.join(media, "a.svg"), work, work))
        r = subprocess.run([sys.executable, "-c", code], preexec_fn=as_owner, capture_output=True, text=True,
                           env={"PATH": fake + ":/usr/bin:/bin", "FORGE_BOOT_BWRAP": "/nonexistent"}, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(log).read().split(), ["input.svg"])
