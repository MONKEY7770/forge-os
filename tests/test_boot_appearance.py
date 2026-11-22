"""Bootup, user part: forgebootmedia (media to boot splash frames, with real ffmpeg) and the backend
/usr/libexec/forge-appearance-boot (desktop splash, lock screen, and the hand-off to the root helper).

Everything here runs as a test-only uid, never as root (the converter refuses root). Needs root to switch
uid. The conversion tests need ffmpeg and ffprobe on PATH (here: /usr/local/bin/ffmpeg).
"""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
BACKEND = os.path.join(ROOT, "files/system/usr/libexec/forge-appearance-boot")
HELPER = os.path.join(ROOT, "files/system/usr/libexec/forge-boot-apply")
SHIPPED = os.path.join(ROOT, "files/system/usr/share/forge/appearance.json")
PREVIEW = os.path.join(ROOT, "files/system/usr/share/forge/appearance-preview/boot.js")
FINALIZE = os.path.join(ROOT, "files/scripts/finalize.sh")
OWNER = 65533
FFMPEG = shutil.which("ffmpeg")
TOOL_PATH = (os.path.dirname(FFMPEG) + ":" if FFMPEG else "") + "/usr/bin:/bin"
VIDEO_PLUGIN = "luisbocanegra.smart.video.wallpaper.reborn"

sys.path.insert(0, LIB)


def as_owner():
    os.setgroups([])
    os.setgid(OWNER)
    os.setuid(OWNER)


def load_helper():
    loader = importlib.machinery.SourceFileLoader("forge_boot_apply", HELPER)
    spec = importlib.util.spec_from_loader("forge_boot_apply", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


RUNNER = r'''
import json, os, sys
sys.path.insert(0, os.environ["FORGE_LIB"])
import forgebootmedia as m
a = json.loads(sys.argv[1])
t0 = __import__("time").monotonic()
try:
    if a["op"] == "splash":
        r = m.convert_splash(a["path"], a["kind"], a["fmt"], a["play"], a["length"], a["side"], a["work"])
        out = {"ok": True, "n": len(r["frames"]), "w": r["width"], "h": r["height"],
               "total": sum(len(f) for f in r["frames"]), "png": all(f.startswith(b"\x89PNG") for f in r["frames"]),
               "sizes": sorted({m.png_size(f) for f in r["frames"]}), "wanted": r.get("wanted")}
    else:
        ext, path = m.prepare_login(a["path"], a["kind"], a["fmt"], a["work"])
        out = {"ok": True, "ext": ext, "size": os.path.getsize(path), "head": open(path, "rb").read(8).hex()}
        if path.startswith(a["work"]):
            os.unlink(path)
except m.ConvertError as e:
    out = {"ok": False, "error": str(e)}
out["seconds"] = round(__import__("time").monotonic() - t0, 2)
print(json.dumps(out))
'''

FAKE_BWRAP = r'''#!/usr/bin/env python3
# Fake bwrap: records its arguments, then runs the command with the bind mounts mapped back to real paths.
import json, os, sys
args = sys.argv[1:]
with open("@LOG@", "a") as f:
    f.write(json.dumps(args) + "\n")
maps, i = [], 0
while args[i] != "--":
    if args[i] in ("--ro-bind", "--bind", "--ro-bind-try", "--symlink", "--setenv"):
        if args[i] in ("--ro-bind", "--bind"):
            maps.append((args[i + 2], args[i + 1]))
        i += 3
    elif args[i] in ("--proc", "--dev", "--tmpfs", "--chdir"):
        i += 2
    else:
        i += 1
cmd = args[i + 1:]
def real(a):
    for inside, outside in sorted(maps, key=lambda m: -len(m[0])):
        if a == inside or a.startswith(inside + "/"):
            return outside + a[len(inside):]
    return a
os.execv(cmd[0], [real(a) for a in cmd])
'''


def png_bytes(w, h, rgba=(10, 20, 30, 255)):
    def chunk(k, d):
        return struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d))
    raw = b"".join(b"\x00" + bytes(rgba) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@unittest.skipUnless(os.geteuid() == 0 and FFMPEG and shutil.which("ffprobe"),
                     "needs root (to run as a test user) and ffmpeg/ffprobe")
class ConvertTest(unittest.TestCase):
    """Real ffmpeg on real files in many formats, and hostile files."""

    @classmethod
    def setUpClass(cls):
        cls.t = tempfile.mkdtemp()
        os.chmod(cls.t, 0o755)
        cls.s = os.path.join(cls.t, "samples")
        os.mkdir(cls.s)
        os.chmod(cls.s, 0o755)
        cls.runner = os.path.join(cls.t, "runner.py")
        with open(cls.runner, "w") as f:
            f.write(RUNNER)
        src = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30"]
        jobs = {
            "clip.mp4": src + ["-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p"],
            "clip.webm": src + ["-t", "2", "-c:v", "libvpx", "-b:v", "500k"],
            "clip.mkv": src + ["-t", "2", "-c:v", "ffv1"],
            "clip.mov": src + ["-t", "2", "-c:v", "mpeg4"],
            "anim.gif": ["-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10", "-t", "2"],
            "anim.apng": ["-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10", "-t", "2", "-f", "apng", "-plays", "0"],
            "anim.webp": ["-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10", "-t", "2", "-c:v", "libwebp_anim",
                          "-loop", "0"],
            "still.png": src + ["-frames:v", "1"],
            "still.jpg": src + ["-frames:v", "1"],
            "still.webp": src + ["-frames:v", "1", "-c:v", "libwebp"],
            "still.bmp": src + ["-frames:v", "1"],
            "still.tiff": src + ["-frames:v", "1"],
            "big.png": ["-f", "lavfi", "-i", "testsrc2=size=3000x2000", "-frames:v", "1"],
            "noise.mp4": ["-f", "lavfi", "-i", "nullsrc=s=640x640:r=30,geq=random(1)*255:random(1)*255:random(1)*255",
                          "-t", "3", "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p"],
            "long.mp4": ["-f", "lavfi", "-i", "color=c=blue:s=32x32:r=1", "-t", "7200", "-c:v", "libx264",
                         "-preset", "ultrafast", "-pix_fmt", "yuv420p"],
        }
        for name, args in jobs.items():
            r = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args,
                                os.path.join(cls.s, name)], capture_output=True, timeout=300)
            assert r.returncode == 0, (name, r.stderr[-500:])
        with open(os.path.join(cls.s, "pic.svg"), "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200">'
                    '<rect width="300" height="200" fill="#336699"/><circle cx="150" cy="100" r="60" fill="#ffcc00"/>'
                    "</svg>")
        # Hostile files
        with open(os.path.join(cls.s, "huge.png"), "wb") as f:     # claims 40000 x 40000 pixels
            data = bytearray(png_bytes(1, 1))
            data[16:24] = struct.pack(">II", 40000, 40000)
            data[29:33] = struct.pack(">I", zlib.crc32(bytes(data[12:29])))
            f.write(bytes(data))
        with open(os.path.join(cls.s, "clip.mp4"), "rb") as f:
            mp4 = f.read()
        with open(os.path.join(cls.s, "cut.mp4"), "wb") as f:
            f.write(mp4[:3000])
        with open(os.path.join(cls.s, "garbage.mp4"), "wb") as f:
            f.write(mp4[:32] + os.urandom(20000))
        os.symlink(os.path.join(cls.s, "clip.mp4"), os.path.join(cls.s, "link.mp4"))
        for n in os.listdir(cls.s):
            p = os.path.join(cls.s, n)
            if not os.path.islink(p):
                os.chmod(p, 0o644)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.t, ignore_errors=True)

    def run_op(self, name, op="splash", play="loop", length=2, side=480, env=None, **kw):
        import forgeappearance
        path = os.path.join(self.s, name)
        found = forgeappearance.sniff(path) or ("video", "mp4")
        work = tempfile.mkdtemp(dir=self.t)
        os.chown(work, OWNER, OWNER)
        args = dict(op=op, path=path, kind=found[0], fmt=found[1], play=play, length=length, side=side, work=work)
        args.update(kw)
        e = {"PATH": TOOL_PATH, "FORGE_LIB": LIB, "HOME": work, "LANG": "C.UTF-8"}
        e.update(env or {})
        r = subprocess.run([sys.executable, self.runner, json.dumps(args)], env=e, preexec_fn=as_owner,
                           capture_output=True, text=True, timeout=200, cwd=work)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(os.listdir(work), [], "the work folder is left clean")
        return out

    def assert_caps(self, out, max_frames=60, side=480):
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["png"])
        self.assertTrue(1 <= out["n"] <= max_frames, out)
        self.assertLessEqual(out["total"], 8 * 1024 * 1024)
        self.assertEqual(len(out["sizes"]), 1, "all frames have one size")
        w, h = out["sizes"][0]
        self.assertTrue(1 <= w <= side and 1 <= h <= side, out)

    def test_every_format_converts_within_the_caps(self):
        animated = ("clip.mp4", "clip.webm", "clip.mkv", "clip.mov", "anim.gif", "anim.apng", "anim.webp")
        still = ("still.png", "still.jpg", "still.webp", "still.bmp", "still.tiff", "pic.svg", "big.png")
        for name in animated + still:
            with self.subTest(name):
                out = self.run_op(name)
                self.assert_caps(out)
                if name in animated:
                    self.assertGreater(out["n"], 1, out)
                else:
                    self.assertEqual(out["n"], 1, out)
        out = self.run_op("big.png", side=240)
        self.assertEqual(out["sizes"][0], [240, 160])

    def test_play_once_uses_up_to_90_frames(self):
        out = self.run_op("clip.mp4", play="once", length=3)
        self.assert_caps(out, max_frames=90)
        self.assertGreater(out["n"], 60)

    def test_noisy_video_is_squeezed_under_8_mb(self):
        out = self.run_op("noise.mp4", length=3)
        self.assert_caps(out)

    def test_very_long_video_takes_only_the_start(self):
        out = self.run_op("long.mp4", length=10, play="once")
        self.assert_caps(out, max_frames=90)
        self.assertLess(out["seconds"], 60)

    def test_hostile_files_are_refused_cleanly(self):
        for name in ("huge.png", "cut.mp4", "garbage.mp4"):
            with self.subTest(name):
                out = self.run_op(name)
                self.assertFalse(out["ok"], out)
                self.assertLess(out["seconds"], 60)
        out = self.run_op("link.mp4", kind="video", fmt="mp4")
        self.assertFalse(out["ok"])
        self.assertIn("link", out["error"])

    def test_input_size_cap(self):
        out = self.run_op("clip.mp4", env={"FORGE_BOOT_INPUT_MAX": "1000"})
        self.assertFalse(out["ok"])
        self.assertIn("too large", out["error"])

    def test_never_as_root(self):
        import forgebootmedia
        with self.assertRaises(forgebootmedia.ConvertError):
            forgebootmedia.convert_splash(os.path.join(self.s, "still.png"), "image", "png", "loop", 2, 480, self.t)

    def test_ffmpeg_reads_only_local_files_with_a_fixed_demuxer(self):
        import forgebootmedia as m
        argv = m.ffmpeg_argv("ffmpeg", "/in/x.mp4", "mp4", "/out", 480, 60, 2.0, palette=False, still=False)
        joined = " ".join(argv)
        self.assertIn("-protocol_whitelist file", joined)
        self.assertIn("-f mov", joined)
        self.assertIn("-nostdin", argv)
        self.assertIn("-max_pixels", argv)
        self.assertLess(argv.index("-protocol_whitelist"), argv.index("-i"))
        for fmt in ("mp4", "webm", "mkv", "mov", "gif", "apng", "webp", "png", "jpeg", "bmp", "tiff", "svg", "avi"):
            self.assertIn(fmt, m.DEMUXERS)
        with self.assertRaises(m.ConvertError):
            m.ffmpeg_argv("ffmpeg", "/in/x", "m3u8", "/out", 480, 60, 2.0, palette=False, still=False)

    def test_sandbox_wraps_ffmpeg_when_bwrap_exists(self):
        fake = os.path.join(self.t, "bwrap")
        log = os.path.join(self.t, "bwrap.log")
        with open(fake, "w") as f:
            f.write(FAKE_BWRAP.replace("/usr/bin/env python3", sys.executable).replace("@LOG@", log))
        os.chmod(fake, 0o755)
        open(log, "w").close()
        os.chmod(log, 0o666)
        out = self.run_op("clip.mp4", env={"FORGE_BOOT_BWRAP": fake})
        self.assert_caps(out)
        calls = [json.loads(x) for x in open(log).read().splitlines()]
        self.assertTrue(calls)
        for args in calls:
            for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv"):
                self.assertIn(flag, args)
            binds = [args[i + 1] for i, a in enumerate(args) if a in ("--bind", "--ro-bind")]
            for b in binds:
                self.assertTrue(b == "/usr" or b.startswith(self.t), b)   # only /usr, the input and the work folder
        self.assertTrue(any(os.path.join(self.s, "clip.mp4") in a for a in calls[-1]))

    def test_login_media(self):
        out = self.run_op("still.jpg", op="login")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["ext"], "png")
        self.assertTrue(out["head"].startswith("89504e47"))
        out = self.run_op("clip.webm", op="login")
        self.assertEqual((out["ok"], out["ext"]), (True, "webm"))
        out = self.run_op("anim.gif", op="login")
        self.assertEqual((out["ok"], out["ext"]), (True, "png"))
        avi = os.path.join(self.s, "clip.avi")
        subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", os.path.join(self.s, "clip.mov"),
                        "-c", "copy", avi], check=True, timeout=60)
        os.chmod(avi, 0o644)
        out = self.run_op("clip.avi", op="login")
        self.assertFalse(out["ok"])
        self.assertIn("MP4", out["error"])


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the backend as a test user")
class BackendTest(unittest.TestCase):
    """The backend, run like forge-appearance runs it: argv ["apply"], the payload on stdin, as the user."""

    def setUp(self):
        self.t = tempfile.mkdtemp()
        os.chmod(self.t, 0o755)
        self.addCleanup(shutil.rmtree, self.t, True)
        self.home = os.path.join(self.t, "home")
        self.root = os.path.join(self.t, "root")
        self.bin = os.path.join(self.t, "bin")
        for d in (self.home, self.bin, self.root + "/etc/systemd/system", self.root + "/var/lib"):
            os.makedirs(d)
        os.chown(self.home, OWNER, OWNER)
        os.symlink("/usr/lib/systemd/system/plasmalogin.service",
                   self.root + "/etc/systemd/system/display-manager.service")
        self.calls = os.path.join(self.t, "pkexec-calls")
        self.captured = os.path.join(self.t, "bundle")
        self.pkexec(0, "Boot splash: ready. You see it after the next restart.")
        self.media = {}
        self.schema = os.path.join(self.t, "schema.json")
        with open(SHIPPED) as f:
            schema = json.load(f)
        for s in schema["sections"]:
            if s["id"] == "background":
                s["fields"] = [{"key": "image", "type": "media", "accept": "image", "label": "Picture", "default": ""},
                               {"key": "color", "type": "color", "label": "Colour", "default": "#223344"}]
        with open(self.schema, "w") as f:
            json.dump(schema, f)
        os.chmod(self.schema, 0o644)

    def pkexec(self, rc, text):
        p = os.path.join(self.bin, "pkexec")
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\necho \"$*\" >> {self.calls}\ncat > {self.captured}\necho '{text}'\nexit {rc}\n")
        os.chmod(p, 0o755)
        for x in (self.calls, self.captured):
            open(x, "a").close()
            os.chmod(x, 0o666)

    def pk_calls(self):
        return open(self.calls).read().splitlines()

    def add_media(self, name, data, kind, fmt):
        d = os.path.join(self.home, ".local/share/forge/appearance/media")
        os.makedirs(d, exist_ok=True)
        mid = hashlib.md5(name.encode()).hexdigest()
        p = os.path.join(d, f"{mid}.{fmt}")
        with open(p, "wb") as f:
            f.write(data)
        for x in (os.path.join(self.home, ".local"), os.path.join(self.home, ".local/share"),
                  os.path.join(self.home, ".local/share/forge"), os.path.join(self.home, ".local/share/forge/appearance"),
                  d, p):
            os.chown(x, OWNER, OWNER)
        self.media[mid] = {"path": p, "kind": kind, "format": fmt, "mime": "x", "name": name}
        return mid

    def values(self, **kw):
        with open(SHIPPED) as f:
            boot = next(s for s in json.load(f)["sections"] if s["id"] == "boot")
        v = {fld["key"]: fld["default"] for fld in boot["fields"]}
        v.update(kw)
        return v, boot["fields"]

    def apply(self, background=None, extra_env=None, timeout=150, **kw):
        vals, fields = self.values(**kw)
        payload = {"section": "boot", "values": vals, "fields": fields, "media": self.media,
                   "all": {"background": background or {"image": "", "color": "#223344"}, "dock": {}, "boot": vals}}
        env = {"HOME": self.home, "PATH": self.bin + ":" + TOOL_PATH, "FORGE_LIB": LIB, "LANG": "C.UTF-8",
               "FORGE_APPEARANCE_SECTION": "boot", "FORGE_APPEARANCE_SCHEMA": self.schema,
               "FORGE_BOOT_ROOT": self.root, "FORGE_BOOT_PKEXEC": os.path.join(self.bin, "pkexec"),
               "FORGE_BOOT_HELPER": "/usr/libexec/forge-boot-apply"}
        env.update(extra_env or {})
        r = subprocess.run([sys.executable, BACKEND, "apply"], input=json.dumps(payload), env=env, cwd=self.home,
                           preexec_fn=as_owner, capture_output=True, text=True, timeout=timeout)
        r.out = r.stdout + r.stderr
        return r

    def hfile(self, rel):
        return os.path.join(self.home, rel)

    def write_home(self, rel, text):
        p = self.hfile(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        cur = self.home
        for part in os.path.relpath(p, self.home).split("/"):
            cur = os.path.join(cur, part)
            os.chown(cur, OWNER, OWNER)

    def read_home(self, rel):
        with open(self.hfile(rel)) as f:
            return f.read()

    def test_defaults_change_nothing_and_ask_nothing(self):
        r = self.apply()
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(self.pk_calls(), [])
        self.assertFalse(os.path.exists(self.hfile(".config/ksplashrc")))
        self.assertFalse(os.path.exists(self.hfile(".config/kscreenlockerrc")))

    def test_refuses_root(self):
        r = subprocess.run([sys.executable, BACKEND, "apply"], input="{}", capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "FORGE_LIB": LIB}, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("root", r.stdout + r.stderr)

    def test_desktop_splash_on_and_back_to_stock_exactly(self):
        original = "[KSplash]\nEngine=KSplashQML\nTheme=org.kde.breeze.desktop\n\n[Other]\nkeep=me\n"
        self.write_home(".config/ksplashrc", original)
        mid = self.add_media("Sunset \"; Qt.quit(); //.png", png_bytes(40, 30), "image", "png")
        r = self.apply(desktop_splash="custom", desktop_splash_media=mid)
        self.assertEqual(r.returncode, 0, r.out)
        rc = self.read_home(".config/ksplashrc")
        self.assertIn("Theme=org.forgeos.bootsplash", rc)
        self.assertIn("keep=me", rc)
        pkg = ".local/share/plasma/look-and-feel/org.forgeos.bootsplash"
        meta = json.loads(self.read_home(pkg + "/metadata.json"))
        self.assertEqual(meta["KPlugin"]["Id"], "org.forgeos.bootsplash")
        qml = self.read_home(pkg + "/contents/splash/Splash.qml")
        self.assertNotIn("Sunset", qml)                      # never user text in QML
        self.assertNotIn("Qt.quit", qml)
        m = re.search(r"readonly property var forge: \((\{.*\})\)", qml)
        self.assertTrue(m, qml)
        cfg = json.loads(m.group(1))
        self.assertEqual(cfg["file"], "images/media.png")
        self.assertTrue(os.path.isfile(self.hfile(pkg + "/contents/splash/images/media.png")))
        self.assertEqual(self.pk_calls(), [])                # no password for the desktop splash
        r = self.apply(desktop_splash="stock")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(self.read_home(".config/ksplashrc"), original)
        self.assertFalse(os.path.exists(self.hfile(pkg)))

    def test_desktop_splash_video_template(self):
        mid = self.add_media("clip.mp4", b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100, "video", "mp4")
        r = self.apply(desktop_splash="custom", desktop_splash_media=mid)
        self.assertEqual(r.returncode, 0, r.out)
        qml = self.read_home(".local/share/plasma/look-and-feel/org.forgeos.bootsplash/contents/splash/Splash.qml")
        self.assertIn("import QtMultimedia", qml)
        r = self.apply(desktop_splash="stock")
        self.assertFalse(os.path.exists(self.hfile(".config/ksplashrc")))   # it did not exist before

    def test_lock_screen_picture_video_desktop_and_back(self):
        original = "[Daemon]\nTimeout=5\n"
        self.write_home(".config/kscreenlockerrc", original)
        pic = self.add_media("p.png", png_bytes(8, 8), "image", "png")
        r = self.apply(lock_screen="custom", lock_media=pic)
        self.assertEqual(r.returncode, 0, r.out)
        rc = self.read_home(".config/kscreenlockerrc")
        self.assertIn("[Greeter]\nWallpaperPlugin=org.kde.image\n", rc)
        self.assertIn("[Greeter][Wallpaper][org.kde.image][General]\nImage=file://" + self.home, rc)
        self.assertIn("Timeout=5", rc)
        vid = self.add_media("v.mp4", b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100, "video", "mp4")
        before = self.read_home(".config/kscreenlockerrc")
        r = self.apply(lock_screen="custom", lock_media=vid)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Smart Video Wallpaper", r.out)
        self.assertEqual(self.read_home(".config/kscreenlockerrc"), before)
        self.write_home(f".local/share/plasma/wallpapers/{VIDEO_PLUGIN}/metadata.json", "{}")
        r = self.apply(lock_screen="custom", lock_media=vid)
        self.assertEqual(r.returncode, 0, r.out)
        rc = self.read_home(".config/kscreenlockerrc")
        self.assertIn(f"WallpaperPlugin={VIDEO_PLUGIN}", rc)
        self.assertIn('VideoUrls=[{"filename":"file://', rc)
        r = self.apply(lock_screen="desktop", background={"image": pic, "color": "#223344"})
        self.assertEqual(r.returncode, 0, r.out)
        self.assertIn("WallpaperPlugin=org.kde.image", self.read_home(".config/kscreenlockerrc"))
        r = self.apply(lock_screen="desktop", background={"image": "", "color": "#112233"})
        self.assertEqual(r.returncode, 0, r.out)
        self.assertIn("Color=17,34,51", self.read_home(".config/kscreenlockerrc"))
        r = self.apply(lock_screen="stock")
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(self.read_home(".config/kscreenlockerrc"), original)

    def test_symlinked_config_is_left_alone(self):
        target = os.path.join(self.t, "elsewhere")
        open(target, "w").close()
        os.makedirs(self.hfile(".config"))
        os.chown(self.hfile(".config"), OWNER, OWNER)
        os.symlink(target, self.hfile(".config/kscreenlockerrc"))
        pic = self.add_media("p.png", png_bytes(8, 8), "image", "png")
        r = self.apply(lock_screen="custom", lock_media=pic)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(open(target).read(), "")

    def test_boot_splash_asks_once_and_only_when_changed(self):
        if not FFMPEG:
            self.skipTest("needs ffmpeg")
        pic = self.add_media("p.png", png_bytes(64, 48), "image", "png")
        r = self.apply(splash="on", splash_media=pic)
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(self.pk_calls(), ["/usr/libexec/forge-boot-apply apply"])
        self.assertIn("restart", r.out)
        helper = load_helper()
        with open(self.captured, "rb") as f:
            splash, login, frames, login_bytes = helper.read_bundle(f)
        self.assertEqual(splash["action"], "on")
        self.assertEqual(login["action"], "keep")
        self.assertEqual(len(frames), 1)
        self.assertEqual(helper.clean_png(frames[0], 480)[:2], (64, 48))
        # The system now has it (the root helper writes its state file).
        os.makedirs(self.root + "/var/lib/forge-boot")
        with open(self.root + "/var/lib/forge-boot/state.json", "w") as f:
            json.dump({"version": 1, "splash": {"enabled": True, "fingerprint": splash["fingerprint"]}}, f)
        r = self.apply(splash="on", splash_media=pic)
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(len(self.pk_calls()), 1, "same values: no second password prompt")
        r = self.apply(splash="on", splash_media=pic, splash_background="#ffffff")
        self.assertEqual(len(self.pk_calls()), 2)
        r = self.apply(splash="off")
        self.assertEqual(len(self.pk_calls()), 3)
        with open(self.captured, "rb") as f:
            self.assertEqual(helper.read_bundle(f)[0]["action"], "off")

    def test_cancelled_password_is_not_recorded(self):
        pic = self.add_media("p.png", png_bytes(16, 16), "image", "png")
        self.pkexec(126, "")
        r = self.apply(splash="on", splash_media=pic)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("admin password", r.out)
        self.pkexec(0, "Boot splash: ready.")
        r = self.apply(splash="on", splash_media=pic)
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(len(self.pk_calls()), 2)

    def test_login_screen_on_sddm_is_not_supported(self):
        os.unlink(self.root + "/etc/systemd/system/display-manager.service")
        os.symlink("/usr/lib/systemd/system/sddm.service", self.root + "/etc/systemd/system/display-manager.service")
        pic = self.add_media("p.png", png_bytes(16, 16), "image", "png")
        r = self.apply(login_screen="custom", login_media=pic)
        self.assertIn("not supported", r.out)
        self.assertEqual(self.pk_calls(), [])

    def test_login_picture_goes_to_the_helper_and_works_end_to_end(self):
        if not FFMPEG:
            self.skipTest("needs ffmpeg")
        pic = self.add_media("p.png", png_bytes(64, 40), "image", "png")
        r = self.apply(login_screen="custom", login_media=pic)
        self.assertEqual(r.returncode, 0, r.out)
        self.assertEqual(len(self.pk_calls()), 1)
        # Feed exactly what the backend sent into the real root helper (fake root, as root like pkexec).
        fake = os.path.join(self.t, "fakeroot")
        os.makedirs(fake + "/etc/systemd/system")
        os.makedirs(fake + "/var/lib")
        os.makedirs(fake + "/run")
        os.symlink("/usr/lib/systemd/system/plasmalogin.service", fake + "/etc/systemd/system/display-manager.service")
        with open(self.captured, "rb") as f:
            res = subprocess.run([sys.executable, HELPER, "apply"], stdin=f, capture_output=True, timeout=60,
                                 env={"PATH": "/usr/bin:/bin", "FORGE_BOOT_ROOT": fake, "FORGE_BOOT_TEST": "1"})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(os.path.exists(fake + "/etc/plasmalogin.conf.d/90-forge-login.conf"))

    def test_slow_system_part_does_not_block_apply(self):
        # pkexec accepted the password and the root part keeps running (rpm-ostree can take minutes; a root
        # process can't be stopped by the user). The backend must return inside its time budget, and nothing
        # it started may keep its stdout open (subprocess.run below only returns at end of output).
        pic = self.add_media("p.png", png_bytes(16, 16), "image", "png")
        marker = os.path.join(self.t, "still-running")
        p = os.path.join(self.bin, "pkexec")
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\ncat > /dev/null\nsetsid sh -c 'sleep 25; touch {marker}' &\nsleep 60\n")
        os.chmod(p, 0o755)
        t0 = time.monotonic()
        r = self.apply(splash="on", splash_media=pic, extra_env={"FORGE_BOOT_BUDGET_S": "20"}, timeout=40)
        took = time.monotonic() - t0
        self.assertLess(took, 30, r.out)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("still running", r.out)
        self.assertFalse(os.path.exists(marker))

    def test_slow_conversion_stops_inside_the_budget(self):
        slow = os.path.join(self.bin, "ffmpeg")
        with open(slow, "w") as f:
            f.write("#!/bin/sh\nsleep 300\n")
        os.chmod(slow, 0o755)
        shutil.copy(slow, os.path.join(self.bin, "ffprobe"))
        pic = self.add_media("p.png", png_bytes(16, 16), "image", "png")
        t0 = time.monotonic()
        r = self.apply(splash="on", splash_media=pic, extra_env={"FORGE_BOOT_BUDGET_S": "20"}, timeout=60)
        self.assertLess(time.monotonic() - t0, 30, r.out)
        self.assertIn("too long", r.out)
        self.assertEqual(self.pk_calls(), [])
        left = subprocess.run(["pgrep", "-u", str(OWNER), "-f", "sleep 300"], capture_output=True, text=True)
        self.assertEqual(left.stdout.strip(), "", "no converter is left running")

    def test_shipped_schema_and_preview(self):
        import forgeappearance
        with open(SHIPPED) as f:
            raw = json.load(f)
        self.assertEqual(forgeappearance.schema_problems(raw), [])
        boot = next(s for s in raw["sections"] if s["id"] == "boot")
        keys = {f["key"]: f for f in boot["fields"]}
        for k in ("splash", "splash_media", "splash_play", "splash_length", "splash_size", "splash_background",
                  "splash_logo", "login_screen", "login_media", "desktop_splash", "desktop_splash_media",
                  "lock_screen", "lock_media"):
            self.assertIn(k, keys)
        self.assertEqual(keys["splash"]["default"], "off")
        self.assertEqual(keys["login_screen"]["default"], "stock")
        self.assertEqual(keys["desktop_splash"]["default"], "stock")
        self.assertEqual(keys["lock_screen"]["default"], "stock")
        for k in ("splash", "login_screen"):
            self.assertIn("admin password", keys[k]["help"])
        self.assertIn("restart", keys["splash"]["help"])
        with open(PREVIEW) as f:
            js = f.read()
        self.assertLess(len(js.encode()), 256 * 1024)
        self.assertIn('forgeAppearance.registerPreview("boot"', js)
        for bad in ("eval(", "innerHTML", "outerHTML", "insertAdjacentHTML", "new Function", 'setAttribute("style"',
                    "document.write", "http://", "https://"):
            self.assertNotIn(bad, js)
        with open(FINALIZE) as f:
            self.assertIn("/usr/libexec/forge-appearance-boot", f.read())


if __name__ == "__main__":
    unittest.main()


# Review follow-ups, added to ConvertTest so the samples are made once.

def _test_big_login_picture_through_the_fallback(self):
        if not shutil.which("convert") and not shutil.which("magick"):
            self.skipTest("needs ImageMagick")
        big = os.path.join(self.s, "bigphoto.webp")
        r = subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "nullsrc=s=3000x2000:r=2,geq=random(1)*255:random(1)*255:random(1)*255", "-t", "1",
                            "-c:v", "libwebp_anim", "-loop", "0", big], capture_output=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        os.chmod(big, 0o644)
        out = self.run_op("bigphoto.webp", op="login")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["ext"], "png")
        self.assertGreater(out["size"], 2 * 1024 * 1024)


def _test_dropped_frames_are_reported(self):
        out = self.run_op("noise.mp4", length=3)
        self.assertTrue(out["ok"], out)
        self.assertIn("wanted", out)
        self.assertGreaterEqual(out["wanted"], out["n"])


ConvertTest.test_big_login_picture_through_the_fallback = _test_big_login_picture_through_the_fallback
ConvertTest.test_dropped_frames_are_reported = _test_dropped_frames_are_reported
