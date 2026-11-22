"""Bootup, root part: forge-boot-apply (started with pkexec) writes the custom boot splash (Plymouth) and the
login screen background from a bundle the user prepared, and undoes exactly what it changed.

Runs against a fake root (FORGE_BOOT_ROOT) with a fake rpm-ostree that keeps the initramfs-etc tracking
the way rpm-ostree does (a set: --track adds, --untrack removes only the named paths) and records argv.
The most important rule: the tracking of /etc/crypttab (USB key unlock) is never removed.
"""
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "files/system/usr/libexec/forge-boot-apply")
POLICY = os.path.join(ROOT, "files/system/usr/share/polkit-1/actions/org.forgeos.boot-apply.policy")
FINALIZE = os.path.join(ROOT, "files/scripts/finalize.sh")

THEME_DIR = "/etc/forge-boot/plymouth/forge-boot"
CONF = "/etc/plymouth/plymouthd.conf"
FEDORA_CONF = b"# Administrator customizations go in this file\n#[Daemon]\n#Theme=fade-in\n"
FP = "ab" * 32


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(w=8, h=8, color=(255, 0, 0, 255), ctype=6, raw=None, extra=b""):
    bpp = 4 if ctype == 6 else 3
    if raw is None:
        raw = b"".join(b"\x00" + bytes(color[:bpp]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)) + extra
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def bundle(splash=None, login=None, frames=(), login_file=None, version=1):
    files = [(f"frame-{i + 1:04d}.png", b) for i, b in enumerate(frames)]
    if login_file:
        files.append(login_file)
    header = {"version": version, "splash": splash or {"action": "keep"}, "login": login or {"action": "keep"},
              "files": [{"name": n, "size": len(b)} for n, b in files]}
    h = json.dumps(header).encode()
    return b"FORGEBOOT1\n" + str(len(h)).encode() + b"\n" + h + b"".join(b for _, b in files)


def splash_on(**kw):
    s = {"action": "on", "fingerprint": FP, "play": "loop", "background": "#102030", "logo": True}
    s.update(kw)
    return s


FAKE_RPM_OSTREE = r'''#!/usr/bin/env python3
# Fake rpm-ostree: only "initramfs-etc", kept as a set like rust/src/treefile.rs does.
import hashlib, json, os, sys
root, state = os.environ["FAKE_ROOT"], os.environ["FAKE_STATE"]
with open(state) as f:
    st = json.load(f)
st["calls"].append(sys.argv[1:])
def save():
    with open(state, "w") as f:
        json.dump(st, f)
args = sys.argv[1:]
if len(args) > 1 and os.path.exists(os.path.join(os.path.dirname(state), "fail")):
    save(); print("error: Transaction in progress", file=sys.stderr); sys.exit(1)
if args[:1] != ["initramfs-etc"]:
    save(); print("fake rpm-ostree: unexpected " + " ".join(args), file=sys.stderr); sys.exit(2)
track = [a.split("=", 1)[1] for a in args if a.startswith("--track=")]
untrack = [a.split("=", 1)[1] for a in args if a.startswith("--untrack=")]
force = "--force-sync" in args
if len(args) == 1:
    save()
    if st["tracked"]:
        print("Tracked files:"); [print("  " + p) for p in st["tracked"]]
    else:
        print("No tracked files.")
    sys.exit(0)
for p in track:
    if not p.startswith("/etc/") or not os.path.lexists(root + p):
        save(); print("error: Invalid or missing /etc path: " + p, file=sys.stderr); sys.exit(1)
before = list(st["tracked"])
s = set(st["tracked"]) | set(track)
if "--untrack-all" in args:
    s = set()
s -= set(untrack)
st["tracked"] = sorted(s)
if st["tracked"] != before or force:
    overlay = {}
    for p in st["tracked"]:
        full = root + p
        if not os.path.lexists(full):
            save(); print("error: Adding " + p + ": stat: No such file", file=sys.stderr); sys.exit(1)
        if os.path.isdir(full) and not os.path.islink(full):
            for d, _, fs in os.walk(full):
                for n in fs:
                    q = os.path.join(d, n)
                    overlay[q[len(root):]] = hashlib.sha256(open(q, "rb").read()).hexdigest()
        else:
            overlay[p] = hashlib.sha256(open(full, "rb").read()).hexdigest()
    st["deployments"].append(overlay)
save()
'''


@unittest.skipUnless(os.geteuid() == 0, "the root helper test runs as root, like pkexec would")
class BootApplyTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.root = os.path.join(self.t, "root")
        self.bin = os.path.join(self.t, "bin")
        os.makedirs(self.bin)
        self.state = os.path.join(self.t, "rpmostree.json")
        with open(self.state, "w") as f:
            json.dump({"tracked": ["/etc/crypttab"], "calls": [], "deployments": []}, f)
        self.fake("rpm-ostree", FAKE_RPM_OSTREE)
        for name in ("plymouth-set-default-theme", "systemctl", "reboot", "dracut"):
            self.fake(name, f"#!/bin/sh\necho \"{name} $*\" >> {self.t}/other-calls\nexit 0\n")
        r = self.root
        for d in ("etc/plymouth", "usr/share/plymouth/themes/spinner", "usr/lib64/plymouth", "run",
                  "etc/systemd/system", "usr/lib/systemd/system", "usr/lib/plasmalogin", "var/lib"):
            os.makedirs(os.path.join(r, d), exist_ok=True)
        self.put("/etc/plymouth/plymouthd.conf", FEDORA_CONF)
        self.put("/etc/crypttab", b"luks-1234 UUID=1234 /forge_key:LABEL=FORGEKEY keyfile-timeout=10s\n")
        self.put("/run/ostree-booted", b"")
        self.put("/usr/lib64/plymouth/two-step.so", b"\x7fELF fake plugin")
        for n in ("entry", "bullet", "lock", "keyboard", "capslock"):
            self.put(f"/usr/share/plymouth/themes/spinner/{n}.png", png(4, 4, (9, 9, 9, 255)))
        self.put("/usr/lib/systemd/system/plasmalogin.service", b"[Unit]\n")
        self.put("/usr/lib/systemd/system/sddm.service", b"[Unit]\n")
        self.dm("plasmalogin")
        self.put("/usr/lib/plasmalogin/defaults.conf", b"[Greeter]\nWallpaperPlugin=org.kde.image\n")

    # helpers

    def fake(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)

    def put(self, path, data, mode=0o644):
        p = self.root + path
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        os.chmod(p, mode)

    def read(self, path):
        with open(self.root + path, "rb") as f:
            return f.read()

    def dm(self, name):
        link = self.root + "/etc/systemd/system/display-manager.service"
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(f"/usr/lib/systemd/system/{name}.service", link)

    def run_helper(self, *args, stdin=b""):
        env = {"PATH": self.bin + ":/usr/bin:/bin", "FORGE_BOOT_ROOT": self.root, "FORGE_BOOT_TEST": "1",
               "FAKE_ROOT": self.root, "FAKE_STATE": self.state, "PKEXEC_UID": "1000", "LANG": "C.UTF-8"}
        r = subprocess.run([sys.executable, HELPER, *args], input=stdin, env=env, capture_output=True,
                           timeout=60)
        r.text = (r.stdout + r.stderr).decode("utf-8", "replace")
        return r

    def rpm(self):
        with open(self.state) as f:
            return json.load(f)

    def other_calls(self):
        p = os.path.join(self.t, "other-calls")
        return open(p).read() if os.path.exists(p) else ""

    def snapshot(self):
        out = {}
        for d, dirs, files in os.walk(self.root):
            for n in dirs + files:
                p = os.path.join(d, n)
                st = os.lstat(p)
                rel = p[len(self.root):]
                if stat.S_ISLNK(st.st_mode):
                    out[rel] = ("link", os.readlink(p))
                elif stat.S_ISDIR(st.st_mode):
                    out[rel] = ("dir", stat.S_IMODE(st.st_mode))
                else:
                    with open(p, "rb") as f:
                        out[rel] = ("file", stat.S_IMODE(st.st_mode), hashlib.sha256(f.read()).hexdigest())
        return out

    def assert_crypttab_never_untracked(self):
        st = self.rpm()
        self.assertIn("/etc/crypttab", st["tracked"])
        for call in st["calls"]:
            self.assertNotIn("--untrack-all", call)
            self.assertNotIn("--untrack=/etc/crypttab", call)
            self.assertNotIn("--reboot", call)
            self.assertNotIn("-r", call)
            self.assertEqual(call[0], "initramfs-etc", call)     # never "initramfs --enable" (local dracut)
        for dep in st["deployments"]:
            self.assertIn("/etc/crypttab", dep)

    def enable(self, frames=3, **kw):
        return self.run_helper("apply", stdin=bundle(splash=splash_on(**kw),
                                                     frames=[png(16, 12, (i, 2, 3, 255)) for i in range(frames)]))

    # the boot splash

    def test_policy_and_finalize(self):
        with open(POLICY) as f:
            pol = f.read()
        self.assertIn("<allow_active>auth_admin</allow_active>", pol)
        self.assertIn("<allow_any>no</allow_any>", pol)
        self.assertIn("<allow_inactive>no</allow_inactive>", pol)
        self.assertNotIn("auth_admin_keep", pol)
        self.assertIn('key="org.freedesktop.policykit.exec.path">/usr/libexec/forge-boot-apply<', pol)
        with open(FINALIZE) as f:
            self.assertIn("/usr/libexec/forge-boot-apply", f.read())

    def test_enable_writes_theme_selects_it_and_tracks_it(self):
        r = self.enable(frames=3)
        self.assertEqual(r.returncode, 0, r.text)
        theme = self.read(THEME_DIR + "/forge-boot.plymouth").decode()
        self.assertIn("ModuleName=two-step", theme)
        self.assertIn(f"ImageDir={THEME_DIR}\n", theme)
        self.assertIn("BackgroundStartColor=0x102030", theme)
        self.assertIn("UseFirmwareBackground=true", theme)
        self.assertIn("UseEndAnimation=false", theme)
        for i in range(1, 4):
            self.assertTrue(self.read(f"{THEME_DIR}/throbber-{i:04d}.png").startswith(b"\x89PNG"))
        for n in ("entry", "bullet", "lock", "keyboard", "capslock"):
            self.assertEqual(self.read(f"{THEME_DIR}/{n}.png"), self.read(f"/usr/share/plymouth/themes/spinner/{n}.png"))
        conf = self.read(CONF).decode()
        self.assertIn("[Daemon]\n", conf)
        self.assertIn("Theme=forge-boot\n", conf)
        self.assertIn("ThemeDir=/etc/forge-boot/plymouth\n", conf)
        self.assertIn("#Theme=fade-in", conf)                       # the rest of the file is kept
        st = self.rpm()
        self.assertEqual(st["tracked"], sorted(["/etc/crypttab", CONF, THEME_DIR]))
        self.assertEqual(len(st["deployments"]), 1)
        overlay = st["deployments"][0]
        self.assertIn(THEME_DIR + "/throbber-0001.png", overlay)
        self.assertIn(THEME_DIR + "/lock.png", overlay)
        self.assertIn(CONF, overlay)
        self.assert_crypttab_never_untracked()
        self.assertEqual(self.other_calls(), "")                    # no plymouth-set-default-theme, no reboot
        self.assertIn("restart", r.text.lower())
        for p in ("/etc/forge-boot", "/etc/forge-boot/plymouth", THEME_DIR):
            st_ = os.lstat(self.root + p)
            self.assertEqual((st_.st_uid, stat.S_IMODE(st_.st_mode)), (0, 0o755), p)
        self.assertEqual(stat.S_IMODE(os.stat(self.root + THEME_DIR + "/throbber-0001.png").st_mode), 0o644)
        state = json.loads(self.read("/var/lib/forge-boot/state.json"))
        self.assertTrue(state["splash"]["enabled"])
        self.assertEqual(state["splash"]["fingerprint"], FP)

    def test_play_once_and_no_logo(self):
        r = self.enable(frames=5, play="once", logo=False, background="#000000")
        self.assertEqual(r.returncode, 0, r.text)
        theme = self.read(THEME_DIR + "/forge-boot.plymouth").decode()
        self.assertIn("UseFirmwareBackground=false", theme)
        self.assertIn("ProgressFunction=linear", theme)
        self.assertTrue(os.path.exists(self.root + THEME_DIR + "/progress-0005.png"))
        self.assertFalse(os.path.exists(self.root + THEME_DIR + "/throbber-0001.png"))

    def test_off_undoes_exactly(self):
        before = self.snapshot()
        self.assertEqual(self.enable().returncode, 0)
        self.assertNotEqual(self.snapshot(), before)
        r = self.run_helper("apply", stdin=bundle(splash={"action": "off", "fingerprint": FP}))
        self.assertEqual(r.returncode, 0, r.text)
        self.assertEqual(self.snapshot(), before)
        st = self.rpm()
        self.assertEqual(st["tracked"], ["/etc/crypttab"])
        self.assertEqual(len(st["deployments"]), 2)                 # a new deployment without our files
        self.assertNotIn(CONF, st["deployments"][-1])
        self.assert_crypttab_never_untracked()

    def test_off_restores_a_missing_conf_as_missing(self):
        os.unlink(self.root + CONF)
        before = self.snapshot()
        self.assertEqual(self.enable().returncode, 0)
        self.assertIn(b"Theme=forge-boot", self.read(CONF))
        self.assertEqual(self.run_helper("off").returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_paths_tracked_by_someone_else_stay_tracked(self):
        with open(self.state, "w") as f:
            json.dump({"tracked": ["/etc/crypttab", CONF], "calls": [], "deployments": []}, f)
        self.assertEqual(self.enable().returncode, 0)
        self.assertEqual(self.run_helper("off").returncode, 0)
        st = self.rpm()
        self.assertEqual(st["tracked"], ["/etc/crypttab", CONF])
        self.assertNotIn(f"--untrack={CONF}", sum(st["calls"], []))
        self.assertEqual(st["deployments"][-1][CONF], hashlib.sha256(FEDORA_CONF).hexdigest())

    def test_update_force_syncs_and_keeps_the_original_backup(self):
        self.assertEqual(self.enable(frames=2).returncode, 0)
        r = self.enable(frames=4, background="#ffffff")
        self.assertEqual(r.returncode, 0, r.text)
        st = self.rpm()
        self.assertIn("--force-sync", [c for c in st["calls"] if len(c) > 1][-1])
        self.assertEqual(len(st["deployments"]), 2)
        self.assertIn(THEME_DIR + "/throbber-0004.png", st["deployments"][-1])
        self.assertFalse(os.path.exists(self.root + THEME_DIR + "/throbber-0005.png"))
        self.assertEqual(self.run_helper("off").returncode, 0)
        self.assertEqual(self.read(CONF), FEDORA_CONF)
        self.assert_crypttab_never_untracked()

    def test_user_edits_after_enable_are_kept_on_off(self):
        self.assertEqual(self.enable().returncode, 0)
        self.put(CONF, self.read(CONF) + b"ShowDelay=5\n")
        self.assertEqual(self.run_helper("off").returncode, 0)
        conf = self.read(CONF).decode()
        self.assertIn("ShowDelay=5", conf)
        self.assertNotIn("forge-boot", conf)

    def test_refuses_without_the_password_box_images(self):
        os.unlink(self.root + "/usr/share/plymouth/themes/spinner/lock.png")
        before = self.snapshot()
        r = self.enable()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("lock.png", r.text)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.rpm()["calls"], [])

    def test_refuses_on_a_system_that_is_not_rpm_ostree(self):
        os.unlink(self.root + "/run/ostree-booted")
        r = self.enable()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.rpm()["calls"], [])

    def test_rpm_ostree_failure_rolls_back(self):
        before = self.snapshot()
        open(os.path.join(self.t, "fail"), "w").close()
        r = self.enable()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Transaction in progress", r.text)
        self.assertEqual(self.snapshot(), before)
        os.unlink(os.path.join(self.t, "fail"))
        self.assert_crypttab_never_untracked()
        self.assertEqual(self.enable().returncode, 0)               # works again afterwards

    def test_failed_off_keeps_tracked_files_in_place(self):
        self.assertEqual(self.enable().returncode, 0)
        open(os.path.join(self.t, "fail"), "w").close()
        r = self.run_helper("off")
        self.assertNotEqual(r.returncode, 0)
        # Never leave a tracked path missing: rpm-ostree needs it for every later update.
        for p in self.rpm()["tracked"]:
            self.assertTrue(os.path.lexists(self.root + p), p)
        os.unlink(os.path.join(self.t, "fail"))
        self.assertEqual(self.run_helper("off").returncode, 0)
        self.assertEqual(self.rpm()["tracked"], ["/etc/crypttab"])
        self.assertEqual(self.read(CONF), FEDORA_CONF)

    def test_symlinks_in_our_folders_are_refused(self):
        os.makedirs(os.path.join(self.t, "elsewhere"))
        os.makedirs(self.root + "/etc", exist_ok=True)
        os.symlink(os.path.join(self.t, "elsewhere"), self.root + "/etc/forge-boot")
        r = self.enable()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(os.path.join(self.t, "elsewhere")), [])
        os.unlink(self.root + "/etc/forge-boot")
        os.rename(self.root + CONF, self.root + "/etc/plymouth/real")
        os.symlink("real", self.root + CONF)
        r = self.enable()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("plymouthd.conf", r.text)

    def test_bad_bundles_change_nothing(self):
        good = png(16, 12)
        cases = {
            "magic": b"NOTFORGE\n2\n{}",
            "version": bundle(splash=splash_on(), frames=[good], version=2),
            "no frames": bundle(splash=splash_on(), frames=[]),
            "91 frames": bundle(splash=splash_on(), frames=[png(2, 2)] * 91),
            "too wide": bundle(splash=splash_on(), frames=[png(481, 10)]),
            "too big": bundle(splash=splash_on(), frames=[png(480, 480, raw=os.urandom(480 * (1 + 480 * 4)))] * 30),
            "not png": bundle(splash=splash_on(), frames=[b"GIF89a" + b"\x00" * 40]),
            "bad crc": bundle(splash=splash_on(), frames=[good[:-5] + b"\x00" + good[-4:]]),
            "short data": bundle(splash=splash_on(), frames=[png(16, 12, raw=b"\x00" * 10)]),
            "zip bomb": bundle(splash=splash_on(), frames=[png(16, 12, raw=b"\x00" * 10_000_000)]),
            "bad filter": bundle(splash=splash_on(), frames=[png(2, 1, raw=b"\x07" + b"\x00" * 8)]),
            "16 bit": bundle(splash=splash_on(), frames=[good[:24] + b"\x10" + good[25:]]),
            "different sizes": bundle(splash=splash_on(), frames=[png(16, 12), png(12, 16)]),
            "bad colour": bundle(splash=splash_on(background="red; rm -rf /"), frames=[good]),
            "bad play": bundle(splash=splash_on(play="../x"), frames=[good]),
            "bad fingerprint": bundle(splash=splash_on(fingerprint="x\nTheme=evil"), frames=[good]),
            "unknown key": bundle(splash=dict(splash_on(), ImageDir="/tmp"), frames=[good]),
            "trailing": bundle(splash=splash_on(), frames=[good]) + b"more",
            "truncated": bundle(splash=splash_on(), frames=[good])[:-3],
            "frames with keep": bundle(splash={"action": "keep"}, frames=[good]),
        }
        before = self.snapshot()
        for name, data in cases.items():
            with self.subTest(name):
                r = self.run_helper("apply", stdin=data)
                self.assertNotEqual(r.returncode, 0, name)
                self.assertEqual(self.snapshot(), before, name)
        self.assertEqual(self.rpm()["calls"], [])
        # A frame with a wrong name: the header names files, and only frame-NNNN.png in order are accepted.
        h = json.dumps({"version": 1, "splash": splash_on(), "login": {"action": "keep"},
                        "files": [{"name": "../../etc/shadow", "size": len(good)}]}).encode()
        r = self.run_helper("apply", stdin=b"FORGEBOOT1\n" + str(len(h)).encode() + b"\n" + h + good)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_argv_is_checked(self):
        for args in ((), ("apply", "x"), ("status",), ("--help",), ("off", "now")):
            r = self.run_helper(*args)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertEqual(self.rpm()["calls"], [])

    def test_palette_frames_are_accepted(self):
        pal = chunk(b"PLTE", bytes([0, 0, 0, 255, 0, 0])) + chunk(b"tRNS", bytes([0, 255]))
        frame = png(4, 2, ctype=3, raw=b"".join(b"\x00" + bytes([0, 1, 0, 1]) for _ in range(2)), extra=pal)
        r = self.run_helper("apply", stdin=bundle(splash=splash_on(), frames=[frame, frame]))
        self.assertEqual(r.returncode, 0, r.text)
        written = self.read(THEME_DIR + "/throbber-0002.png")
        self.assertIn(b"PLTE", written)
        self.assertIn(b"tRNS", written)

    def test_ancillary_chunks_are_dropped(self):
        frame = png(4, 4, extra=chunk(b"tEXt", b"Comment\x00hello") + chunk(b"iCCP", b"x\x00\x00" + zlib.compress(b"y")))
        self.assertEqual(self.run_helper("apply", stdin=bundle(splash=splash_on(), frames=[frame])).returncode, 0)
        written = self.read(THEME_DIR + "/throbber-0001.png")
        self.assertNotIn(b"tEXt", written)
        self.assertNotIn(b"iCCP", written)

    # the login screen

    def login_png(self):
        return ("login.png", png(64, 36, (1, 2, 3, 255)))

    def test_login_picture_on_and_off(self):
        before = self.snapshot()
        r = self.run_helper("apply", stdin=bundle(login={"action": "on", "fingerprint": FP, "kind": "image",
                                                         "format": "png"}, login_file=self.login_png()))
        self.assertEqual(r.returncode, 0, r.text)
        drop = self.read("/etc/plasmalogin.conf.d/90-forge-login.conf").decode()
        self.assertIn("[Greeter]\nWallpaperPluginId=org.kde.image\n", drop)
        self.assertIn("[Greeter][Wallpaper][org.kde.image][General]\nImage=file:///var/lib/forge-boot/login/login-", drop)
        name = drop.split("Image=file://", 1)[1].split("\n", 1)[0]
        st = os.stat(self.root + name)
        self.assertEqual((st.st_uid, stat.S_IMODE(st.st_mode)), (0, 0o644))
        self.assertEqual(stat.S_IMODE(os.stat(self.root + "/var/lib/forge-boot/login").st_mode), 0o755)
        self.assertEqual(self.rpm()["calls"], [])                   # the login screen needs no initramfs change
        r = self.run_helper("apply", stdin=bundle(login={"action": "off", "fingerprint": FP}))
        self.assertEqual(r.returncode, 0, r.text)
        self.assertEqual(self.snapshot(), before)

    def test_login_video_uses_the_video_plugin(self):
        mp4 = struct.pack(">I", 24) + b"ftypisom" + b"\x00\x00\x02\x00isomiso2" + b"\x00" * 200
        data = bundle(login={"action": "on", "fingerprint": FP, "kind": "video", "format": "mp4"},
                      login_file=("login.mp4", mp4))
        before = self.snapshot()
        r = self.run_helper("apply", stdin=data)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Smart Video Wallpaper plugin, which is not installed", r.text)
        self.assertEqual(self.snapshot(), before)
        self.put("/usr/share/plasma/wallpapers/luisbocanegra.smart.video.wallpaper.reborn/metadata.json", b"{}")
        r = self.run_helper("apply", stdin=data)
        self.assertEqual(r.returncode, 0, r.text)
        drop = self.read("/etc/plasmalogin.conf.d/90-forge-login.conf").decode()
        self.assertIn("WallpaperPluginId=luisbocanegra.smart.video.wallpaper.reborn\n", drop)
        self.assertIn('VideoUrls=[{"filename":"file:///var/lib/forge-boot/login/login-', drop)
        self.assertIn("MuteMode=5\n", drop)

    def test_login_refused_on_sddm_and_bad_media(self):
        before = self.snapshot()
        self.dm("sddm")
        r = self.run_helper("apply", stdin=bundle(login={"action": "on", "fingerprint": FP, "kind": "image",
                                                         "format": "png"}, login_file=self.login_png()))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not supported", r.text)
        self.dm("plasmalogin")
        before = self.snapshot()
        for fmt, data in (("png", b"\x7fELF" + b"\x00" * 100), ("mp4", b"#!/bin/sh\nrm -rf /\n"),
                          ("svg", b"<svg/>"), ("exe", b"MZ")):
            with self.subTest(fmt):
                r = self.run_helper("apply", stdin=bundle(login={"action": "on", "fingerprint": FP,
                                                                 "kind": "video" if fmt != "png" else "image",
                                                                 "format": fmt}, login_file=("login." + fmt, data)))
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_off_command_turns_everything_off(self):
        before = self.snapshot()
        r = self.run_helper("apply", stdin=bundle(splash=splash_on(), frames=[png(4, 4)],
                                                  login={"action": "on", "fingerprint": FP, "kind": "image",
                                                         "format": "png"}, login_file=self.login_png()))
        self.assertEqual(r.returncode, 0, r.text)
        r = self.run_helper("off")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertEqual(self.snapshot(), before)
        self.assert_crypttab_never_untracked()

    def test_crypttab_tracking_restored_if_it_ever_went_missing(self):
        # Simulate something else dropping the crypttab tracking between our list and our change.
        self.fake("rpm-ostree", FAKE_RPM_OSTREE.replace('st["tracked"] = sorted(s)',
                                                        's.discard("/etc/crypttab") if track and "/etc/crypttab" not in track else None\n'
                                                        'st["tracked"] = sorted(s)', 1))
        r = self.enable()
        self.assertIn("crypttab", r.text)
        self.assertIn("/etc/crypttab", self.rpm()["tracked"])
        self.assertIn(["initramfs-etc", "--track=/etc/crypttab"], self.rpm()["calls"])


if __name__ == "__main__":
    unittest.main()
