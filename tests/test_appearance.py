"""Appearance settings: per user, no root. The forgeappearance library (schema, values,
media library) and the forge-appearance tool.

Library tests run in this process with a temporary XDG_CONFIG_HOME and XDG_DATA_HOME.
The tool refuses to run as root, so its tests run it as a test-only uid (needs root to switch).
"""
import importlib
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
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-appearance")
SHIPPED = os.path.join(ROOT, "files/system/usr/share/forge/appearance.json")
DEMO = os.path.join(ROOT, "tests/data/appearance-demo.json")
FINALIZE = os.path.join(ROOT, "files/scripts/finalize.sh")
OWNER = 65533

sys.path.insert(0, LIB)


# Small sample files: just enough bytes for each format's magic numbers.

def png_chunk(kind, data=b""):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(animated=False):
    out = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    if animated:
        out += png_chunk(b"acTL", struct.pack(">II", 2, 0))
    return out + png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + png_chunk(b"IEND")


def gif(frames=1):
    out = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0, 0, 0)
    for _ in range(frames):
        out += b"\x21\xf9\x04\x00\x0a\x00\x00\x00"           # graphic control extension
        out += b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0) + b"\x02\x02\x4c\x01\x00"
    return out + b"\x3b"


def webp(animated=False):
    if animated:
        body = b"WEBPVP8X" + struct.pack("<I", 10) + bytes([0x02, 0, 0, 0]) + b"\x00" * 6
    else:
        body = b"WEBPVP8 " + struct.pack("<I", 10) + b"\x00" * 10
    return b"RIFF" + struct.pack("<I", len(body)) + body


def ftyp(major, *compat):
    data = major + b"\x00\x00\x00\x00" + b"".join(compat)
    return struct.pack(">I", len(data) + 8) + b"ftyp" + data + struct.pack(">I", 8) + b"mdat"


SAMPLES = {
    "png": (png(), "image", "png"),
    "apng": (png(animated=True), "animated-image", "apng"),
    "jpg": (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32, "image", "jpeg"),
    "gif": (gif(1), "image", "gif"),
    "gif2": (gif(2), "animated-image", "gif"),
    "webp": (webp(), "image", "webp"),
    "webp-anim": (webp(animated=True), "animated-image", "webp"),
    "avif": (ftyp(b"avif", b"mif1", b"miaf"), "image", "avif"),
    "avis": (ftyp(b"avis", b"avif", b"msf1"), "animated-image", "avif"),
    "heic": (ftyp(b"heic", b"mif1"), "image", "heic"),
    "bmp": (b"BM" + struct.pack("<IHHII", 62, 0, 0, 54, 40) + b"\x00" * 44, "image", "bmp"),
    "tif": (b"II*\x00" + b"\x00" * 60, "image", "tiff"),
    "jxl": (b"\xff\x0a" + b"\x00" * 60, "image", "jxl"),
    "ico": (b"\x00\x00\x01\x00\x01\x00" + b"\x00" * 60, "image", "ico"),
    "svg": (b'\xef\xbb\xbf<?xml version="1.0"?>\n<!-- hi -->\n<svg xmlns="http://www.w3.org/2000/svg"></svg>',
            "image", "svg"),
    "mp4": (ftyp(b"isom", b"isom", b"iso2", b"mp41"), "video", "mp4"),
    "mov": (ftyp(b"qt  ", b"qt  "), "video", "mov"),
    "3gp": (ftyp(b"3gp5", b"3gp5"), "video", "3gp"),
    "mkv": (b"\x1a\x45\xdf\xa3\x9f\x42\x82\x88matroska" + b"\x00" * 40, "video", "mkv"),
    "webm": (b"\x1a\x45\xdf\xa3\x9f\x42\x82\x84webm" + b"\x00" * 40, "video", "webm"),
    "avi": (b"RIFF\x00\x00\x00\x00AVI LIST" + b"\x00" * 40, "video", "avi"),
    "wmv": (bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c") + b"\x00" * 40, "video", "wmv"),
    "flv": (b"FLV\x01\x05" + b"\x00" * 40, "video", "flv"),
    "mpg": (b"\x00\x00\x01\xba" + b"\x00" * 40, "video", "mpeg"),
    "ogv": (b"OggS\x00\x02" + b"\x00" * 22 + b"\x80theora" + b"\x00" * 30, "video", "ogv"),
    "ts": ((b"\x47" + b"\x00" * 187) * 4, "video", "ts"),
}

NOT_MEDIA = {
    "text.png": b"hello, this is not a picture\n",
    "elf.jpg": b"\x7fELF\x02\x01\x01" + b"\x00" * 60,
    "page.svg": b"<html><body><script>alert(1)</script></body></html>",
    "audio.ogv": b"OggS\x00\x02" + b"\x00" * 22 + b"\x01vorbis" + b"\x00" * 30,
    "audio.mp4": ftyp(b"M4A ", b"M4A ", b"mp42"),
    "empty.png": b"",
    "words.bmp": b"BMW cars are fast, this is a text file\n" * 3,
    "ebml.mkv": b"\x1a\x45\xdf\xa3" + b"\x00" * 60,
}


class LibBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
            "XDG_DATA_HOME": os.path.join(self.tmp, "data"),
            "FORGE_APPEARANCE_SCHEMA": DEMO,
            "FORGE_APPEARANCE_LIBEXEC": os.path.join(self.tmp, "libexec"),
        })
        self.env.start()
        import forgeappearance
        self.fa = importlib.reload(forgeappearance)
        self.schema = self.fa.load_schema()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p


class SchemaTest(LibBase):
    def test_shipped_schema_has_the_four_sections_and_is_valid(self):
        with open(SHIPPED) as f:
            raw = json.load(f)
        self.assertEqual(self.fa.schema_problems(raw), [])
        self.assertEqual([(s["id"], s["label"]) for s in raw["sections"]],
                         [("background", "Background"), ("dock", "Dock"), ("boot", "Bootup"), ("theme", "Theme")])
        for s in raw["sections"]:
            self.assertIsInstance(s["fields"], list)

    def test_demo_schema_is_valid_and_covers_every_field_type(self):
        with open(DEMO) as f:
            raw = json.load(f)
        self.assertEqual(self.fa.schema_problems(raw), [])
        types = {fd["type"] for s in raw["sections"] for fd in s["fields"]}
        self.assertEqual(types, set(self.fa.FIELD_TYPES))

    def test_bad_fields_are_reported_and_left_out(self):
        raw = {"version": 1, "sections": [{"id": "background", "label": "Background", "help": "", "fields": [
            {"key": "ok", "type": "toggle", "label": "Ok", "help": "", "default": True},
            {"key": "Bad Key", "type": "toggle", "label": "x", "help": "", "default": True},
            {"key": "n", "type": "number", "label": "n", "help": "", "default": 500, "min": 0, "max": 10},
            {"key": "c", "type": "color", "label": "c", "help": "", "default": "red"},
            {"key": "m", "type": "media", "label": "m", "help": "", "default": "", "accept": "audio"},
            {"key": "s", "type": "select", "label": "s", "help": "", "default": "a", "options": []},
            {"key": "w", "type": "wobble", "label": "w", "help": "", "default": 1},
            {"key": "x", "type": "toggle", "label": "x", "help": "", "default": True,
             "show_if": {"key": "nope", "equals": 1}},
            {"key": "ok", "type": "toggle", "label": "dup", "help": "", "default": True},
        ]}, {"id": "../etc", "label": "Evil", "help": "", "fields": []}]}
        problems = self.fa.schema_problems(raw)
        self.assertGreaterEqual(len(problems), 8, problems)
        clean = self.fa.clean_schema(raw)
        self.assertEqual([s["id"] for s in clean["sections"]], ["background"])
        self.assertEqual([f["key"] for f in clean["sections"][0]["fields"]], ["ok"])

    def test_missing_or_broken_schema_never_crashes(self):
        for text in (None, "", "{", "[]", '{"sections": 5}', '{"sections": [5]}'):
            p = os.path.join(self.tmp, "schema.json")
            if text is None:
                if os.path.exists(p):
                    os.unlink(p)
            else:
                with open(p, "w") as f:
                    f.write(text)
            with mock.patch.object(self.fa, "SCHEMA_PATH", p):
                self.assertEqual(self.fa.load_schema(), {"version": 1, "sections": []}, text)


class ValuesTest(LibBase):
    def test_defaults_when_nothing_is_saved(self):
        vals = self.fa.load(self.schema)
        self.assertEqual(vals["background"]["type"], "color")
        self.assertEqual(vals["background"]["color"], "#2b3a4a")
        self.assertEqual(vals["dock"]["size"], 48)
        self.assertEqual(vals["boot"], {})

    def test_paths_follow_xdg_and_home(self):
        self.assertEqual(self.fa.config_path(), os.path.join(self.tmp, "config", "forge", "appearance.json"))
        self.assertEqual(self.fa.media_dir(), os.path.join(self.tmp, "data", "forge", "appearance", "media"))
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/path", "XDG_DATA_HOME": "",
                                          "HOME": os.path.join(self.tmp, "home")}):
            self.assertEqual(self.fa.config_path(),
                             os.path.join(self.tmp, "home", ".config", "forge", "appearance.json"))
            self.assertEqual(self.fa.media_dir(),
                             os.path.join(self.tmp, "home", ".local", "share", "forge", "appearance", "media"))

    def test_save_is_atomic_and_private(self):
        self.fa.set_value("dock", "size", "64", self.schema)
        p = self.fa.config_path()
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode), 0o700)
        self.assertEqual(self.fa.load(self.schema)["dock"]["size"], 64)
        self.assertEqual([n for n in os.listdir(os.path.dirname(p)) if n != "appearance.json"], [])
        # A write that fails half way leaves the old file as it was.
        with mock.patch.object(self.fa.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.fa.set_value("dock", "size", "72", self.schema)
        self.assertEqual(self.fa.load(self.schema)["dock"]["size"], 64)
        self.assertEqual([n for n in os.listdir(os.path.dirname(p)) if n != "appearance.json"], [])

    def test_save_replaces_a_symlink_instead_of_writing_through_it(self):
        target = self.file("victim.txt", b"keep me")
        os.makedirs(os.path.dirname(self.fa.config_path()), exist_ok=True)
        os.symlink(target, self.fa.config_path())
        self.fa.set_value("dock", "autohide", "on", self.schema)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"keep me")
        self.assertFalse(os.path.islink(self.fa.config_path()))

    def test_load_cleans_a_bad_file(self):
        os.makedirs(os.path.dirname(self.fa.config_path()), exist_ok=True)
        bad = {"background": {"type": "video", "color": "red", "dim": 7, "fps_cap": 1000, "caption": "a\nb",
                              "nope": 1, "image": "../../etc/passwd"},
               "dock": {"size": "48", "autohide": "yes", "position": "top"},
               "boot": [1, 2], "evil": {"x": 1}}
        with open(self.fa.config_path(), "w") as f:
            json.dump(bad, f)
        vals = self.fa.load(self.schema)
        self.assertEqual(vals["background"]["type"], "video")          # good values are kept
        self.assertEqual(vals["background"]["color"], "#2b3a4a")       # bad ones become the default
        self.assertEqual(vals["background"]["dim"], 0)                 # off the 5 step grid
        self.assertEqual(vals["background"]["fps_cap"], 30)
        self.assertEqual(vals["background"]["caption"], "")
        self.assertEqual(vals["background"]["image"], "")
        self.assertNotIn("nope", vals["background"])
        self.assertEqual(vals["dock"], {"position": "bottom", "size": 48, "autohide": False})
        self.assertEqual(vals["boot"], {})
        self.assertNotIn("evil", vals)
        for text in ("", "{", "[1]", "null", '"x"', "{" * 5000, "\x00\xff"):
            with open(self.fa.config_path(), "w", encoding="latin-1") as f:
                f.write(text)
            self.assertEqual(self.fa.load(self.schema)["dock"]["size"], 48, text[:20])

    def test_load_refuses_huge_or_special_files(self):
        os.makedirs(os.path.dirname(self.fa.config_path()), exist_ok=True)
        with open(self.fa.config_path(), "w") as f:
            f.write('{"dock": {"size": 64}, "pad": "' + "x" * (self.fa.VALUES_MAX + 10) + '"}')
        self.assertEqual(self.fa.load(self.schema)["dock"]["size"], 48)
        os.unlink(self.fa.config_path())
        os.mkfifo(self.fa.config_path())
        self.assertEqual(self.fa.load(self.schema)["dock"]["size"], 48)   # and does not hang

    def test_set_refuses_bad_input(self):
        for section, key, value in (("dock", "size", "47"), ("dock", "size", "200"), ("dock", "size", "big"),
                                    ("dock", "position", "top"), ("dock", "autohide", "maybe"),
                                    ("background", "color", "#12345"), ("background", "color", "blue"),
                                    ("background", "caption", "x" * 41), ("background", "caption", "a\x1bb"),
                                    ("background", "image", "0" * 32), ("dock", "nope", "1"),
                                    ("nope", "size", "1"), ("background", "fps_cap", "nan"),
                                    ("background", "fps_cap", "1e999")):
            with self.assertRaises(ValueError, msg=(section, key, value)):
                self.fa.set_value(section, key, value, self.schema)
        self.assertFalse(os.path.exists(self.fa.config_path()))

    def test_set_accepts_good_input(self):
        self.fa.set_value("background", "color", "#ABCDEF", self.schema)
        self.fa.set_value("background", "dim", "35", self.schema)
        self.fa.set_value("background", "caption", "  Hello  ", self.schema)
        self.fa.set_value("dock", "autohide", "on", self.schema)
        vals = self.fa.load(self.schema)
        self.assertEqual(vals["background"]["color"], "#abcdef")
        self.assertEqual(vals["background"]["dim"], 35)
        self.assertEqual(vals["background"]["caption"], "Hello")
        self.assertIs(vals["dock"]["autohide"], True)

    def test_check_values_is_strict(self):
        clean, errors = self.fa.check_values({"dock": {"size": 52, "autohide": True}}, self.schema)
        self.assertEqual(errors, [])
        self.assertEqual(clean["dock"]["size"], 52)
        self.assertEqual(clean["background"]["type"], "color")
        _, errors = self.fa.check_values({"dock": {"size": True}}, self.schema)      # a bool is not a number
        self.assertTrue(errors)
        _, errors = self.fa.check_values({"dock": {"autohide": "on"}}, self.schema)  # JSON wants a real bool
        self.assertTrue(errors)
        _, errors = self.fa.check_values({"dock": {"nope": 1}}, self.schema)
        self.assertTrue(errors)
        _, errors = self.fa.check_values({"nope": {}}, self.schema)
        self.assertTrue(errors)
        _, errors = self.fa.check_values([], self.schema)
        self.assertTrue(errors)

    def test_show_if(self):
        sec = {s["id"]: s for s in self.schema["sections"]}["background"]
        f = {x["key"]: x for x in sec["fields"]}
        vals = {"type": "color"}
        self.assertTrue(self.fa.visible(f["color"], vals))
        self.assertFalse(self.fa.visible(f["video"], vals))
        self.assertFalse(self.fa.visible(f["dim"], vals))
        self.assertTrue(self.fa.visible(f["type"], vals))
        vals = {"type": "video"}
        self.assertTrue(self.fa.visible(f["video"], vals))
        self.assertTrue(self.fa.visible(f["dim"], vals))
        self.assertFalse(self.fa.visible(f["color"], vals))


class MediaTest(LibBase):
    def test_sniffs_every_known_format_from_its_bytes(self):
        for name, (data, kind, fmt) in SAMPLES.items():
            with self.subTest(name=name):
                self.assertEqual(self.fa.sniff(self.file("s-" + name, data)), (kind, fmt))
        for name, data in NOT_MEDIA.items():
            with self.subTest(name=name):
                self.assertIsNone(self.fa.sniff(self.file(name, data)))

    def test_extension_allowlist(self):
        for ext in ("png jpg jpeg webp avif heic heif gif apng svg bmp tif tiff jxl ico "
                    "mp4 m4v mkv webm mov avi wmv flv mpg mpeg ogv 3gp ts").split():
            self.assertIn(ext, self.fa.MEDIA_EXTS)
        for ext in ("html", "exe", "sh", "js", "desktop", "py", "xml"):
            self.assertNotIn(ext, self.fa.MEDIA_EXTS)

    def test_add_copies_into_a_private_library(self):
        src = self.file("Holiday Photo.PNG", png())
        item = self.fa.media_add(src)
        self.assertRegex(item["id"], r"^[0-9a-f]{32}$")
        self.assertEqual((item["kind"], item["format"], item["mime"]), ("image", "png", "image/png"))
        self.assertEqual(item["name"], "Holiday Photo.PNG")
        self.assertTrue(os.path.exists(src), "the original must stay (copy, never move)")
        d = self.fa.media_dir()
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        path = self.fa.media_path(item["id"])
        self.assertEqual(os.path.dirname(path), d)
        self.assertEqual(os.path.basename(path), item["id"] + ".png")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), png())
        self.assertEqual([m["id"] for m in self.fa.media_list()], [item["id"]])

    def test_extension_comes_from_the_bytes_when_it_does_not_fit(self):
        # A JPEG called .png, a PNG called .exe, an SVG called .png.
        a = self.fa.media_add(self.file("a.png", SAMPLES["jpg"][0]))
        b = self.fa.media_add(self.file("b.exe", png()))
        c = self.fa.media_add(self.file("c.png", SAMPLES["svg"][0]))
        d = self.fa.media_add(self.file("d.jpeg", SAMPLES["jpg"][0]))
        e = self.fa.media_add(self.file("e.m4v", SAMPLES["mp4"][0]))
        self.assertTrue(self.fa.media_path(a["id"]).endswith(".jpg"))
        self.assertTrue(self.fa.media_path(b["id"]).endswith(".png"))
        self.assertTrue(self.fa.media_path(c["id"]).endswith(".svg"))
        self.assertEqual(c["mime"], "image/svg+xml")
        self.assertTrue(self.fa.media_path(d["id"]).endswith(".jpeg"))
        self.assertTrue(self.fa.media_path(e["id"]).endswith(".m4v"))
        self.assertEqual(e["kind"], "video")

    def test_refuses_what_is_not_a_picture_or_video(self):
        for name, data in NOT_MEDIA.items():
            with self.subTest(name=name):
                with self.assertRaises(self.fa.MediaError):
                    self.fa.media_add(self.file(name, data))
        self.assertEqual(self.fa.media_list(), [])
        self.assertEqual(os.listdir(self.fa.media_dir()), [], "a refused file left something behind")

    def test_refuses_links_and_special_files(self):
        real = self.file("real.png", png())
        link = os.path.join(self.tmp, "link.png")
        os.symlink(real, link)
        fifo = os.path.join(self.tmp, "pipe.png")
        os.mkfifo(fifo)
        folder = os.path.join(self.tmp, "folder.png")
        os.mkdir(folder)
        for p in (link, fifo, folder, "/dev/null", os.path.join(self.tmp, "missing.png")):
            with self.subTest(path=p):
                with self.assertRaises(self.fa.MediaError):
                    self.fa.media_add(p)
        self.assertEqual(self.fa.media_list(), [])

    def test_refuses_a_linked_media_folder(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        os.makedirs(os.path.dirname(self.fa.media_dir()))
        os.symlink(elsewhere, self.fa.media_dir())
        with self.assertRaises(self.fa.MediaError):
            self.fa.media_add(self.file("a.png", png()))
        self.assertEqual(os.listdir(elsewhere), [])

    def test_size_caps(self):
        self.assertEqual(self.fa.FILE_CAP_DEFAULT, 4 * 1024 ** 3)
        self.assertEqual(self.fa.TOTAL_CAP_DEFAULT, 20 * 1024 ** 3)
        big = self.file("big.png", png() + b"\x00" * 2000)
        with mock.patch.object(self.fa, "FILE_CAP", 1000):
            with self.assertRaises(self.fa.MediaError) as e:
                self.fa.media_add(big)
            self.assertIn("too large", str(e.exception))
        with mock.patch.object(self.fa, "TOTAL_CAP", 3000):
            self.fa.media_add(big)
            with self.assertRaises(self.fa.MediaError) as e:
                self.fa.media_add(big)
            self.assertIn("full", str(e.exception))
        self.assertEqual(len(self.fa.media_list()), 1)

    def test_caps_can_only_be_lowered_from_the_environment(self):
        with mock.patch.dict(os.environ, {"FORGE_APPEARANCE_FILE_CAP": str(10 ** 15),
                                          "FORGE_APPEARANCE_TOTAL_CAP": "1000"}):
            fa = importlib.reload(self.fa)
            self.assertEqual(fa.FILE_CAP, 4 * 1024 ** 3)
            self.assertEqual(fa.TOTAL_CAP, 1000)

    def test_stream_add_stops_at_the_cap(self):
        import io
        data = png() + b"\x00" * 5000
        with mock.patch.object(self.fa, "FILE_CAP", 2000):
            with self.assertRaises(self.fa.MediaError):            # said too much up front
                self.fa.media_add_stream(io.BytesIO(data), len(data), "x.png")
            with self.assertRaises(self.fa.MediaError):            # no length: stops while copying
                self.fa.media_add_stream(io.BytesIO(data), None, "x.png")
        with self.assertRaises(self.fa.MediaError):          # shorter than it said
            self.fa.media_add_stream(io.BytesIO(png()), 10 ** 6, "x.png")
        item = self.fa.media_add_stream(io.BytesIO(data), len(data), "../../evil\n.png")
        self.assertEqual(item["name"], "evil.png")
        self.assertEqual(os.listdir(self.fa.media_dir()), [item["id"] + ".png"])

    def test_remove(self):
        item = self.fa.media_add(self.file("a.png", png()))
        self.fa.set_value("background", "type", "image", self.schema)
        self.fa.set_value("background", "image", item["id"], self.schema)
        self.assertEqual(self.fa.load(self.schema)["background"]["image"], item["id"])
        self.assertTrue(self.fa.media_remove(item["id"]))
        self.assertEqual(self.fa.media_list(), [])
        self.assertEqual(os.listdir(self.fa.media_dir()), [])
        self.assertEqual(self.fa.load(self.schema)["background"]["image"], "")   # the setting lets go of it
        for bad in ("../x", "", "A" * 32, item["id"]):
            self.assertFalse(self.fa.media_remove(bad))

    def test_media_field_needs_the_right_kind(self):
        pic = self.fa.media_add(self.file("a.png", png()))
        anim = self.fa.media_add(self.file("a.gif", gif(2)))
        vid = self.fa.media_add(self.file("v.mp4", SAMPLES["mp4"][0]))
        self.fa.set_value("background", "image", pic["id"], self.schema)
        self.fa.set_value("background", "image", anim["id"], self.schema)
        self.fa.set_value("background", "video", vid["id"], self.schema)
        with self.assertRaises(ValueError):
            self.fa.set_value("background", "image", vid["id"], self.schema)
        with self.assertRaises(ValueError):
            self.fa.set_value("background", "video", pic["id"], self.schema)

    def test_list_skips_entries_whose_file_is_gone_or_swapped(self):
        a = self.fa.media_add(self.file("a.png", png()))
        b = self.fa.media_add(self.file("b.png", png()))
        os.unlink(self.fa.media_path(a["id"]))
        p = self.fa.media_path(b["id"])
        os.unlink(p)
        os.symlink("/etc/passwd", p)
        self.assertEqual(self.fa.media_list(), [])
        self.assertIsNone(self.fa.media_path(b["id"]))

    def test_broken_index_never_crashes(self):
        self.fa.media_add(self.file("a.png", png()))
        for text in ("", "{", "[]", '{"items": 5}', '{"items": [5, {"id": "../x"}]}'):
            with open(self.fa.index_path(), "w") as f:
                f.write(text)
            self.assertEqual(self.fa.media_list(), [], text)

    def test_ffprobe_is_an_extra_check_for_videos_only_and_never_as_root(self):
        fake = os.path.join(self.tmp, "bin")
        os.mkdir(fake)
        log = os.path.join(self.tmp, "ffprobe.log")
        mode = os.path.join(self.tmp, "ffprobe.mode")

        def ffprobe(behaviour):
            with open(mode, "w") as f:
                f.write(behaviour)
        with open(os.path.join(fake, "ffprobe"), "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> %s\nm=$(cat %s)\n"
                    "[ \"$m\" = slow ] && sleep 5\n"
                    "[ \"$m\" = bad ] && exit 1\n"
                    "[ \"$m\" = audio ] && echo '{\"streams\": [{\"codec_type\": \"audio\"}]}' && exit 0\n"
                    "echo '{\"streams\": [{\"codec_type\": \"video\"}]}'\n" % (log, mode))
        os.chmod(os.path.join(fake, "ffprobe"), 0o755)
        vid = self.file("v.mp4", SAMPLES["mp4"][0])
        with mock.patch.dict(os.environ, {"PATH": fake}), mock.patch.object(self.fa, "FFPROBE_TIMEOUT_S", 1):
            with mock.patch.object(self.fa.os, "geteuid", return_value=1000):
                ffprobe("ok")
                self.fa.media_add(vid)
                with open(log) as f:
                    args = f.read().split()
                self.assertIn("-show_entries", args)
                for behaviour in ("bad", "slow", "audio"):
                    ffprobe(behaviour)
                    with self.assertRaises(self.fa.MediaError, msg=behaviour):
                        self.fa.media_add(vid)
                os.unlink(log)
                self.fa.media_add(self.file("p.png", png()))       # pictures are not checked with it
                self.assertFalse(os.path.exists(log))
            with mock.patch.object(self.fa.os, "geteuid", return_value=0):
                ffprobe("bad")
                self.fa.media_add(vid)                              # root: never runs it
                self.assertFalse(os.path.exists(log))
        self.assertEqual(len(self.fa.media_list()), 3)


class ApplyTest(LibBase):
    def setUp(self):
        super().setUp()
        # The library refuses to run a backend as root; these tests run as root, so pretend a normal user.
        self.euid = mock.patch.object(self.fa.os, "geteuid", return_value=1000)
        self.euid.start()

    def tearDown(self):
        self.euid.stop()
        super().tearDown()

    def backend(self, section, body):
        d = os.path.join(self.tmp, "libexec")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "forge-appearance-" + section)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
        return p

    def test_missing_backend_is_not_installed_yet(self):
        state, out = self.fa.apply_section("dock", self.schema)
        self.assertEqual(state, "not-installed")
        self.assertIn("not installed yet", out)

    def test_unknown_section_is_refused(self):
        self.backend("evil", "#!/bin/sh\ntouch %s/ran\n" % self.tmp)
        for sid in ("evil", "../dock", "dock/../evil", "", "DOCK"):
            with self.assertRaises(ValueError):
                self.fa.apply_section(sid, self.schema)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ran")))

    def test_backend_gets_argv_apply_and_its_values_on_stdin(self):
        out = os.path.join(self.tmp, "got.json")
        self.backend("dock", "#!%s\nimport json, os, sys\n"
                             "json.dump({'argv': sys.argv[1:], 'stdin': json.load(sys.stdin),\n"
                             "           'env': {k: v for k, v in os.environ.items() if k.startswith(('FORGE', 'SECRET'))}},\n"
                             "          open(%r, 'w'))\nprint('dock applied')\n" % (sys.executable, out))
        pic = self.fa.media_add(self.file("a.png", png()))
        self.fa.set_value("dock", "size", "64", self.schema)
        self.fa.set_value("background", "image", pic["id"], self.schema)
        with mock.patch.dict(os.environ, {"SECRET_TOKEN": "fake-test-value"}):
            state, text = self.fa.apply_section("dock", self.schema)
        self.assertEqual(state, "applied", text)
        self.assertIn("dock applied", text)
        with open(out) as f:
            got = json.load(f)
        self.assertEqual(got["argv"], ["apply"])
        self.assertEqual(got["stdin"]["section"], "dock")
        self.assertEqual(got["stdin"]["values"], {"position": "bottom", "size": 64, "autohide": False})
        self.assertEqual(got["stdin"]["all"]["background"]["image"], pic["id"])
        self.assertEqual(got["stdin"]["media"][pic["id"]]["path"], self.fa.media_path(pic["id"]))
        self.assertNotIn("SECRET_TOKEN", got["env"])
        self.assertEqual(got["env"]["FORGE_APPEARANCE_SECTION"], "dock")
        self.assertEqual(got["env"]["FORGE_APPEARANCE_FILE"], self.fa.config_path())
        self.assertEqual(got["env"]["FORGE_APPEARANCE_MEDIA"], self.fa.media_dir())

    def test_failing_or_slow_backend(self):
        self.backend("dock", "#!/bin/sh\necho broken >&2\nexit 3\n")
        state, text = self.fa.apply_section("dock", self.schema)
        self.assertEqual(state, "failed")
        self.assertIn("broken", text)
        self.backend("dock", "#!/bin/sh\nsleep 5\n")
        with mock.patch.object(self.fa, "BACKEND_TIMEOUT_S", 1):
            t0 = time.time()
            state, text = self.fa.apply_section("dock", self.schema)
        self.assertEqual(state, "failed")
        self.assertLess(time.time() - t0, 4)

    def test_backend_that_is_not_executable_is_not_installed(self):
        p = self.backend("dock", "#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o644)
        self.assertEqual(self.fa.apply_section("dock", self.schema)[0], "not-installed")


class ReviewFixTest(LibBase):
    """Regression tests for the independent review (rvUI)."""

    def backend(self, section, body):
        d = os.path.join(self.tmp, "libexec")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "forge-appearance-" + section)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
        return p

    def setUp(self):
        super().setUp()
        self.euid = mock.patch.object(self.fa.os, "geteuid", return_value=1000)
        self.euid.start()

    def tearDown(self):
        self.euid.stop()
        super().tearDown()

    def alive(self, pidfile):
        with open(pidfile) as f:
            pid = int(f.read())
        try:
            os.kill(pid, 0)
            return pid
        except ProcessLookupError:
            return None

    # Review 1: helpers a backend leaves running must not hold up or break apply.
    def test_background_helper_does_not_block_or_get_killed(self):
        pidfile = os.path.join(self.tmp, "helper.pid")
        for start in ("sleep 30 &", "setsid sleep 30 &"):
            with self.subTest(start=start):
                self.backend("dock", f"#!/bin/sh\n{start}\necho $! > {pidfile}\necho started player\nexit 0\n")
                t0 = time.time()
                state, text = self.fa.apply_section("dock", self.schema)
                self.assertLess(time.time() - t0, 5, "apply waited for a helper the backend left running")
                self.assertEqual(state, "applied", text)
                self.assertIn("started player", text)
                time.sleep(0.3)
                pid = self.alive(pidfile)
                self.assertTrue(pid, "a helper of a backend that exited 0 was killed")
                os.kill(pid, 9)

    def test_timeout_holds_even_when_a_helper_keeps_the_output_open(self):
        self.backend("dock", "#!/bin/sh\nsetsid sleep 30 &\nsleep 30\n")
        with mock.patch.object(self.fa, "BACKEND_TIMEOUT_S", 1):
            t0 = time.time()
            state, text = self.fa.apply_section("dock", self.schema)
        self.assertEqual(state, "failed")
        self.assertIn("longer than", text)
        self.assertLess(time.time() - t0, 5)
        subprocess.run(["pkill", "-f", "^sleep 30$"])

    # Review 3: output is never buffered whole.
    def test_huge_output_keeps_only_a_short_tail(self):
        import resource
        self.backend("dock", "#!/bin/sh\nyes 'chatty backend line' | head -c 150000000\necho last line\nexit 0\n")
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        state, text = self.fa.apply_section("dock", self.schema)
        grew_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024
        self.assertEqual(state, "applied")
        self.assertLessEqual(len(text), self.fa.OUTPUT_MAX)
        self.assertTrue(text.endswith("last line"), text[-50:])
        self.assertLess(grew_mb, 40, f"memory grew by {grew_mb:.0f} MB")

    # Review: the library itself never runs a backend as root.
    def test_apply_refuses_root(self):
        ran = os.path.join(self.tmp, "ran")
        self.backend("dock", f"#!/bin/sh\ntouch {ran}\n")
        with mock.patch.object(self.fa.os, "geteuid", return_value=0):
            with self.assertRaises(PermissionError):
                self.fa.apply_section("dock", self.schema)
        self.assertFalse(os.path.exists(ran))

    # Review 4: a slow upload does not hold the library lock.
    def test_slow_upload_does_not_block_other_adds(self):
        import threading
        go = threading.Event()

        class Slow:
            def __init__(self):
                self.parts = [png(), b"\x00" * 100]

            def read(self, n):
                go.wait(20)
                return self.parts.pop(0) if self.parts else b""
        slow = threading.Thread(target=lambda: self.fa.media_add_stream(Slow(), None, "slow.png"))
        slow.start()
        time.sleep(0.3)
        done = []
        other = threading.Thread(target=lambda: done.append(self.fa.media_add(self.file("b.png", png()))))
        other.start()
        other.join(5)
        blocked = other.is_alive()
        go.set()
        slow.join(10)
        other.join(10)
        self.assertFalse(blocked, "a second add waited for a slow upload")
        self.assertEqual(len(self.fa.media_list()), 2)

    # Review 5: leftovers of a killed upload don't count, and are cleaned up safely.
    def test_leftover_temp_files_do_not_count_against_the_cap(self):
        os.makedirs(self.fa.media_dir(), exist_ok=True)
        with open(os.path.join(self.fa.media_dir(), ".upload-killed"), "wb") as f:
            f.write(b"\x00" * 5000)
        with mock.patch.object(self.fa, "TOTAL_CAP", 3000):
            self.fa.media_add(self.file("a.png", png()))
        self.assertEqual(len(self.fa.media_list()), 1)

    def test_cleanup_removes_leftovers_without_following_links(self):
        keep = self.fa.media_add(self.file("a.png", png()))
        d = self.fa.media_dir()
        outside = self.file("outside.txt", b"keep me")
        old = os.path.join(d, ".upload-old")
        fresh = os.path.join(d, ".upload-fresh")
        for p in (old, fresh):
            with open(p, "wb") as f:
                f.write(b"x")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        stray = os.path.join(d, "0" * 32 + ".png")
        with open(stray, "wb") as f:
            f.write(png())
        link = os.path.join(d, "1" * 32 + ".png")
        os.symlink(outside, link)
        os.mkdir(os.path.join(d, "somefolder"))
        self.fa.media_cleanup()
        left = sorted(os.listdir(d))
        self.assertIn(keep["file"], left)
        self.assertIn(".upload-fresh", left, "an upload that may still be running was removed")
        for gone in (".upload-old", os.path.basename(stray), os.path.basename(link)):
            self.assertNotIn(gone, left)
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), b"keep me")
        self.assertIn("somefolder", left)

    # Review 6: saves at the same time never lose each other's change.
    def test_concurrent_saves_keep_both_changes(self):
        import threading
        real_load = self.fa.load

        def slow_load(*a, **kw):
            v = real_load(*a, **kw)
            time.sleep(0.05)
            return v
        with mock.patch.object(self.fa, "load", slow_load):
            for n in range(5):
                size, color = str(24 + 4 * n), "#%06x" % (n + 1)
                ts = [threading.Thread(target=self.fa.set_value, args=("dock", "size", size, self.schema)),
                      threading.Thread(target=self.fa.set_value, args=("background", "color", color, self.schema))]
                [t.start() for t in ts]
                [t.join() for t in ts]
                vals = real_load(self.schema)
                self.assertEqual((vals["dock"]["size"], vals["background"]["color"]), (int(size), color))
                ts = [threading.Thread(target=self.fa.save_checked, args=({"dock": {"size": 96 - 4 * n}}, self.schema)),
                      threading.Thread(target=self.fa.save_checked, args=({"background": {"dim": 5 * n}}, self.schema))]
                [t.start() for t in ts]
                [t.join() for t in ts]
                vals = real_load(self.schema)
                self.assertEqual((vals["dock"]["size"], vals["background"]["dim"]), (96 - 4 * n, 5 * n))

    def test_save_checked_is_strict(self):
        values, errors = self.fa.save_checked({"dock": {"size": 1000}}, self.schema)
        self.assertTrue(errors)
        self.assertFalse(os.path.exists(self.fa.config_path()))
        values, errors = self.fa.save_checked({"dock": {"size": 52}}, self.schema)
        self.assertEqual(errors, [])
        self.assertEqual(self.fa.load(self.schema)["dock"]["size"], 52)

    # Review: an index with odd numbers never breaks listing.
    def test_index_with_odd_numbers(self):
        self.fa.media_add(self.file("a.png", png()))
        for field, value in (("added", 1e300), ("added", -1), ("size", -5), ("added", float("nan"))):
            with open(self.fa.index_path()) as f:
                d = json.load(f)
            d["items"][0]["added"], d["items"][0]["size"] = 1.0, 100
            d["items"][0][field] = value
            with open(self.fa.index_path(), "w") as f:
                json.dump(d, f)
            self.assertEqual(self.fa.media_list(), [], (field, value))


class ReviewTwoTest(LibBase):
    """Regression tests for the second review (rvUI): cleanup must never lose files, helpers keep a working
    stdout, a slow upload is never cleaned away."""

    def setUp(self):
        super().setUp()
        self.euid = mock.patch.object(self.fa.os, "geteuid", return_value=1000)
        self.euid.start()

    def tearDown(self):
        self.euid.stop()
        super().tearDown()

    def three(self):
        return [self.fa.media_add(self.file(f"p{i}.png", png() + bytes([i]))) for i in range(3)]

    def files(self):
        d = self.fa.media_dir()
        return sorted(n for n in os.listdir(d) if not n.startswith("."))

    def damage(self, how):
        idx = self.fa.index_path()
        with open(idx, "rb") as f:
            data = f.read()
        if how == "truncated":
            open(idx, "wb").write(data[: len(data) // 2])
        elif how == "missing":
            os.rename(idx, idx + ".bak")
        elif how == "oversized":
            d = json.loads(data)
            d["pad"] = "x" * (self.fa.INDEX_MAX + 10)
            open(idx, "w").write(json.dumps(d))
        elif how == "not an object":
            open(idx, "w").write("[1, 2]")
        elif how == "items not a list":
            open(idx, "w").write('{"items": {"a": 1}}')
        elif how == "binary":
            open(idx, "wb").write(b"\x00\xff\xfe" * 100)
        elif how == "empty":
            open(idx, "wb").write(b"")
        elif how == "link":
            os.rename(idx, idx + ".real")
            os.symlink(idx + ".real", idx)
        elif how == "folder":
            os.rename(idx, idx + ".bak")
            os.mkdir(idx)

    DAMAGE = ("truncated", "missing", "oversized", "not an object", "items not a list", "binary", "empty", "link",
              "folder")

    def test_cleanup_never_loses_files_with_a_damaged_index(self):
        for how in self.DAMAGE:
            with self.subTest(how=how):
                self.tearDown()
                self.setUp()
                self.three()
                before = self.files()
                self.damage(how)
                self.fa.media_cleanup()
                self.fa.media_list()
                self.assertEqual(self.files(), before, "files were removed or moved")
                if how == "missing":
                    # Round 3: only id-named files and no index means an add was stopped half way: the index
                    # is rebuilt from the files, nothing is lost.
                    self.assertIsNone(self.fa.index_problem())
                    self.assertEqual(len(self.fa.media_list()), 3)
                    continue
                with self.assertRaises(self.fa.MediaError) as e:
                    self.fa.media_add(self.file("new.png", png()))
                self.assertIn("index", str(e.exception))
                self.assertEqual(self.files(), before)
                self.assertFalse(self.fa.media_remove(before[0].split(".")[0]))
                self.assertEqual(self.files(), before)
                self.assertTrue(self.fa.index_problem())

    def test_an_entry_that_does_not_pass_the_checks_keeps_its_file(self):
        items = self.three()
        with open(self.fa.index_path()) as f:
            d = json.load(f)
        d["items"][0]["added"] *= 1000           # milliseconds instead of seconds
        with open(self.fa.index_path(), "w") as f:
            json.dump(d, f)
        self.fa.media_cleanup()
        self.assertEqual(len(self.files()), 3)
        new = self.fa.media_add(self.file("new.png", png()))     # rewriting the index keeps that entry
        self.fa.media_remove(items[1]["id"])
        self.fa.media_cleanup()
        self.assertEqual(sorted(self.files()), sorted([items[0]["file"], items[2]["file"], new["file"]]))
        with open(self.fa.index_path()) as f:
            self.assertIn(items[0]["id"], f.read())

    def test_unknown_files_go_to_quarantine_never_deleted(self):
        keep = self.fa.media_add(self.file("a.png", png()))
        d = self.fa.media_dir()
        for i in range(3):
            with open(os.path.join(d, f"{i}" * 32 + ".png"), "wb") as f:
                f.write(png())
        with mock.patch.object(self.fa, "QUARANTINE_MAX", 2):
            self.fa.media_cleanup()
        q = os.path.join(d, ".quarantine")
        self.assertEqual(stat.S_IMODE(os.lstat(q).st_mode), 0o700)
        self.assertEqual(len(os.listdir(q)), 2)
        self.assertEqual(len(self.files()), 2, "the quarantine is full: the third file stays where it was")
        self.assertIn(keep["file"], self.files())
        # Quarantined files are never deleted and don't count toward the cap.
        with mock.patch.object(self.fa, "QUARANTINE_MAX", 2):
            self.fa.media_cleanup()
        self.assertEqual(len(os.listdir(q)), 2)
        self.assertEqual(self.fa._used_bytes(), keep["size"])

    def test_quarantine_that_is_a_link_is_not_used(self):
        self.fa.media_add(self.file("a.png", png()))
        d = self.fa.media_dir()
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(elsewhere)
        os.symlink(elsewhere, os.path.join(d, ".quarantine"))
        with open(os.path.join(d, "9" * 32 + ".png"), "wb") as f:
            f.write(png())
        self.fa.media_cleanup()
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertIn("9" * 32 + ".png", self.files())

    def test_cleanup_skips_an_upload_still_being_written(self):
        import threading
        go = threading.Event()

        class Slow:
            def __init__(self):
                self.parts = [png(), b"\x00" * 100]

            def read(self, n):
                if len(self.parts) == 1:
                    go.wait(20)
                return self.parts.pop(0) if self.parts else b""
        result = []
        t = threading.Thread(target=lambda: result.append(self.fa.media_add_stream(Slow(), None, "slow.png")))
        t.start()
        time.sleep(0.5)
        d = self.fa.media_dir()
        temp = [n for n in os.listdir(d) if n.startswith(".upload-")]
        self.assertEqual(len(temp), 1)
        old = time.time() - 7200
        os.utime(os.path.join(d, temp[0]), (old, old))     # looks stale by its time
        self.fa.media_cleanup()
        self.assertTrue(os.path.exists(os.path.join(d, temp[0])), "cleanup removed an upload in progress")
        go.set()
        t.join(10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "image")

    def backend(self, section, body):
        d = os.path.join(self.tmp, "libexec")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "forge-appearance-" + section)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)

    def test_helper_can_keep_writing_to_stdout_after_the_backend_exits(self):
        log = os.path.join(self.tmp, "helper.log")
        self.backend("dock", "#!/bin/sh\n(i=0; while [ $i -lt 4 ]; do echo frame $i && echo ok $i >> %s; "
                             "i=$((i+1)); sleep 0.5; done) &\necho started\nexit 0\n" % log)
        state, text = self.fa.apply_section("dock", self.schema)
        self.assertEqual(state, "applied", text)
        time.sleep(3)
        with open(log) as f:
            self.assertEqual(f.read().split(), ["ok", "0", "ok", "1", "ok", "2", "ok", "3"])

    def test_output_file_is_kept_small_while_the_backend_runs(self):
        self.backend("dock", "#!/bin/sh\nhead -c 30000000 /dev/zero | tr '\\0' x\nsleep 1.5\necho\n"
                             "stat -L -c size=%s /proc/self/fd/1\n")
        state, text = self.fa.apply_section("dock", self.schema)
        self.assertEqual(state, "applied")
        size = int(re.search(r"size=(\d+)", text).group(1))
        self.assertLess(size, 1024 * 1024, "the output file was not kept small")


class ReviewThreeTest(LibBase):
    """Regression tests for the third review (rvUI)."""

    def setUp(self):
        super().setUp()
        self.euid = mock.patch.object(self.fa.os, "geteuid", return_value=1000)
        self.euid.start()

    def tearDown(self):
        self.euid.stop()
        super().tearDown()

    def files(self):
        return sorted(n for n in os.listdir(self.fa.media_dir()) if not n.startswith("."))

    # 1: an index entry whose id is not text
    def test_entry_with_a_non_text_id_breaks_nothing(self):
        a = self.fa.media_add(self.file("a.png", png()))
        with open(self.fa.index_path()) as f:
            d = json.load(f)
        d["items"].append({"id": ["x"], "file": "junk"})
        with open(self.fa.index_path(), "w") as f:
            json.dump(d, f)
        b = self.fa.media_add(self.file("b.png", png() + b"b"))
        self.assertTrue(self.fa.media_remove(a["id"]))
        self.assertEqual(self.files(), [b["file"]])
        with open(self.fa.index_path()) as f:
            self.assertIn('"x"', f.read(), "the odd entry is kept as it was")

    def test_remove_writes_the_index_before_deleting_the_file(self):
        a = self.fa.media_add(self.file("a.png", png()))
        with mock.patch.object(self.fa, "_write_index", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.fa.media_remove(a["id"])
        self.assertEqual(self.files(), [a["file"]])
        self.assertEqual([m["id"] for m in self.fa.media_list()], [a["id"]])

    # 2: a first upload killed between linking the file and writing the index
    def test_library_recovers_from_a_killed_first_upload(self):
        a = self.fa.media_add(self.file("a.png", png()))
        with mock.patch.object(self.fa, "_ffprobe_ok", return_value=True):   # the sample mp4 has no real video
            v = self.fa.media_add(self.file("v.mp4", SAMPLES["mp4"][0]))
        os.unlink(self.fa.index_path())
        self.assertTrue(self.fa.index_problem())
        junk = os.path.join(self.fa.media_dir(), "f" * 32 + ".png")
        with open(junk, "wb") as f:
            f.write(b"not a picture")
        with mock.patch.object(self.fa, "_ffprobe_ok", return_value=True):   # the rebuild checks videos again
            b = self.fa.media_add(self.file("b.png", png() + b"b"))
        self.assertIsNone(self.fa.index_problem())
        listed = {m["id"]: m for m in self.fa.media_list()}
        self.assertEqual(set(listed), {a["id"], v["id"], b["id"]})
        self.assertEqual((listed[a["id"]]["kind"], listed[v["id"]]["kind"]), ("image", "video"))
        self.assertNotIn(os.path.basename(junk), self.files())
        self.assertIn(os.path.basename(junk), os.listdir(os.path.join(self.fa.media_dir(), ".quarantine")))

    def test_rebuild_checks_videos_with_ffprobe_like_an_upload(self):
        # Review (round 4, Info): a junk file with only an mp4 header was indexed as a video by the rebuild,
        # although an upload of the same bytes is refused by the ffprobe check.
        a = self.fa.media_add(self.file("a.png", png()))
        with mock.patch.object(self.fa, "_ffprobe_ok", return_value=True):
            v = self.fa.media_add(self.file("v.mp4", SAMPLES["mp4"][0]))
        os.unlink(self.fa.index_path())
        with mock.patch.object(self.fa, "_ffprobe_ok", return_value=False):
            self.fa.media_cleanup()
        self.assertIsNone(self.fa.index_problem())
        self.assertEqual([m["id"] for m in self.fa.media_list()], [a["id"]])
        self.assertIn(v["file"], os.listdir(os.path.join(self.fa.media_dir(), ".quarantine")))

    def test_cleanup_alone_rebuilds_a_missing_index(self):
        a = self.fa.media_add(self.file("a.png", png()))
        os.unlink(self.fa.index_path())
        self.fa.media_cleanup()
        self.assertIsNone(self.fa.index_problem())
        self.assertEqual([m["id"] for m in self.fa.media_list()], [a["id"]])

    def test_no_rebuild_when_other_files_are_there(self):
        a = self.fa.media_add(self.file("a.png", png()))
        os.unlink(self.fa.index_path())
        for name, link in (("notes.txt", False), ("e" * 32 + ".png", True)):
            with self.subTest(name=name):
                p = os.path.join(self.fa.media_dir(), name)
                if link:
                    os.symlink(self.file("outside.png", png()), p)
                else:
                    with open(p, "w") as f:
                        f.write("mine")
                before = self.files()
                self.fa.media_cleanup()
                with self.assertRaises(self.fa.MediaError):
                    self.fa.media_add(self.file("b.png", png()))
                self.assertEqual(self.files(), before)
                self.assertTrue(self.fa.index_problem())
                os.unlink(p)
        self.assertIn(a["file"], self.files())

    # 3: a helper still writing after its backend exited never grows a file
    def backend(self, section, body):
        d = os.path.join(self.tmp, "libexec")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "forge-appearance-" + section)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)

    def test_helper_writing_after_exit_goes_to_a_pipe_not_a_file(self):
        log, pidf = os.path.join(self.tmp, "helper.log"), os.path.join(self.tmp, "helper.pid")
        helper = os.path.join(self.tmp, "helper.py")
        with open(helper, "w") as f:
            f.write("import sys, time\nfor i in range(12):\n    sys.stdout.write('x' * 1048575 + chr(10))\n"
                    f"    sys.stdout.flush()\n    open({log!r}, 'a').write(str(i) + chr(10))\n    time.sleep(0.25)\n")
        self.backend("dock", f"#!/bin/sh\n{sys.executable} {helper} &\necho $! > {pidf}\necho started\nexit 0\n")
        state, text = self.fa.apply_section("dock", self.schema)
        self.assertEqual(state, "applied", text)
        time.sleep(1.5)
        with open(pidf) as f:
            pid = f.read().strip()
        where = os.readlink(f"/proc/{pid}/fd/1")
        self.assertTrue(where.startswith("pipe:"), where)
        time.sleep(2.5)
        with open(log) as f:
            self.assertEqual(len(f.read().split()), 12, "the helper did not get to write everything")
        d = self.fa.data_dir()
        self.assertEqual([n for n in (os.listdir(d) if os.path.isdir(d) else []) if n.startswith(".output-")], [])


def as_uid(uid):
    def drop():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return drop


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the tool as a normal test user")
class ToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o755)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        os.chown(self.home, OWNER, OWNER)
        self.libexec = os.path.join(self.tmp, "libexec")
        os.mkdir(self.libexec)
        self.bin = os.path.join(self.tmp, "bin")       # PATH without ffprobe
        os.mkdir(self.bin)
        self.env = {"HOME": self.home, "PATH": self.bin, "FORGE_LIB": LIB, "FORGE_APPEARANCE_SCHEMA": DEMO,
                    "FORGE_APPEARANCE_LIBEXEC": self.libexec, "LANG": "C.UTF-8"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tool(self, *args, uid=OWNER):
        return subprocess.run([sys.executable, TOOL, *args], env=self.env, preexec_fn=as_uid(uid) if uid else None,
                              capture_output=True, text=True, timeout=30, cwd=self.tmp)

    def owned(self, name, data):
        p = os.path.join(self.home, name)
        with open(p, "wb") as f:
            f.write(data)
        os.chown(p, OWNER, OWNER)
        return p

    def test_refuses_root(self):
        r = self.run_tool("show", uid=None)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("normal user", r.stderr)

    def test_show_and_set(self):
        r = self.run_tool("show")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Background", r.stdout)
        self.assertIn("Bootup", r.stdout)
        r = self.run_tool("set", "dock", "size", "60")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_tool("show", "--json")
        data = json.loads(r.stdout)
        self.assertEqual(data["values"]["dock"]["size"], 60)
        self.assertEqual([s["id"] for s in data["sections"]], ["background", "dock", "boot"])
        cfg = os.path.join(self.home, ".config", "forge", "appearance.json")
        self.assertEqual(os.stat(cfg).st_uid, OWNER)
        self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)

    def test_set_refuses_bad_values(self):
        for args in (("dock", "size", "1000"), ("dock", "nope", "1"), ("nope", "size", "1"),
                     ("dock", "position", "top")):
            r = self.run_tool("set", *args)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("Not saved", r.stderr)

    def test_apply_reports_backends_not_installed_yet(self):
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        for label in ("Background", "Dock", "Bootup"):
            self.assertIn(label + ": not installed yet", r.stdout)
        r = self.run_tool("apply", "dock")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Dock: not installed yet", r.stdout)

    def test_apply_refuses_unknown_sections(self):
        p = os.path.join(self.libexec, "forge-appearance-evil")
        with open(p, "w") as f:
            f.write("#!/bin/sh\ntouch %s/ran\n" % self.tmp)
        os.chmod(p, 0o755)
        for sid in ("evil", "../evil", "--help"):
            r = self.run_tool("apply", sid)
            self.assertEqual(r.returncode, 2, sid)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ran")))

    def test_apply_runs_an_installed_backend(self):
        p = os.path.join(self.libexec, "forge-appearance-dock")
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho \"dock backend $1\"\n")
        os.chmod(p, 0o755)
        r = self.run_tool("apply", "dock")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dock backend apply", r.stdout)
        p2 = os.path.join(self.libexec, "forge-appearance-boot")
        with open(p2, "w") as f:
            f.write("#!/bin/sh\necho no >&2\nexit 4\n")
        os.chmod(p2, 0o755)
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Bootup: failed", r.stdout)

    def test_media_commands(self):
        src = self.owned("pic.png", png())
        r = self.run_tool("media", "add", src)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_tool("media", "list", "--json")
        items = json.loads(r.stdout)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "image")
        r = self.run_tool("media", "list")
        self.assertIn(items[0]["id"], r.stdout)
        self.assertIn("pic.png", r.stdout)
        media = os.path.join(self.home, ".local", "share", "forge", "appearance", "media")
        self.assertEqual(stat.S_IMODE(os.stat(media).st_mode), 0o700)
        self.assertEqual(os.stat(media).st_uid, OWNER)
        bad = self.owned("notes.png", b"just text")
        r = self.run_tool("media", "add", bad)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not added", r.stderr)
        link = os.path.join(self.home, "link.png")
        os.symlink(src, link)
        self.assertEqual(self.run_tool("media", "add", link).returncode, 1)
        r = self.run_tool("media", "remove", items[0]["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.run_tool("media", "list", "--json").stdout), [])
        self.assertEqual(self.run_tool("media", "remove", items[0]["id"]).returncode, 1)

    def test_media_list_survives_an_odd_index(self):
        src = self.owned("pic.png", png())
        self.assertEqual(self.run_tool("media", "add", src).returncode, 0)
        idx = os.path.join(self.home, ".local", "share", "forge", "appearance", "media.json")
        with open(idx) as f:
            d = json.load(f)
        d["items"][0]["added"] = 1e300
        with open(idx, "w") as f:
            json.dump(d, f)
        r = self.run_tool("media", "list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_damaged_index_is_reported_not_hidden(self):
        src = self.owned("pic.png", png())
        self.assertEqual(self.run_tool("media", "add", src).returncode, 0)
        idx = os.path.join(self.home, ".local", "share", "forge", "appearance", "media.json")
        with open(idx, "w") as f:
            f.write("{")
        r = self.run_tool("media", "list")
        self.assertIn("damaged", r.stderr)
        self.assertNotIn("empty", r.stdout)
        r = self.run_tool("media", "add", src)
        self.assertEqual(r.returncode, 1)
        self.assertIn("damaged", r.stderr)
        media = os.path.join(self.home, ".local", "share", "forge", "appearance", "media")
        self.assertEqual(len([n for n in os.listdir(media) if not n.startswith(".")]), 1)

    def test_helper_keeps_running_after_the_tool_exits(self):
        log = os.path.join(self.tmp, "helper.log")
        helper = os.path.join(self.tmp, "helper.py")
        with open(helper, "w") as f:
            f.write("import sys, time\nfor i in range(8):\n    print('frame', i, flush=True)\n"
                    f"    open({log!r}, 'a').write(str(i) + chr(10))\n    time.sleep(0.4)\n")
        os.chmod(helper, 0o644)
        open(log, "w").close()
        os.chown(log, OWNER, OWNER)
        p = os.path.join(self.libexec, "forge-appearance-dock")
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\n{sys.executable} {helper} &\necho started\nexit 0\n")
        os.chmod(p, 0o755)
        r = self.run_tool("apply", "dock")
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(4.5)
        with open(log) as f:
            self.assertEqual(len(f.read().split()), 8, "the helper stopped when the tool exited")

    def test_usage(self):
        for args in ((), ("bogus",), ("set", "dock"), ("media",), ("media", "add"), ("show", "--xml")):
            r = self.run_tool(*args)
            self.assertEqual(r.returncode, 2, args)

    def test_finalize_makes_it_executable(self):
        with open(FINALIZE) as f:
            self.assertIn("/usr/bin/forge-appearance", f.read())


if __name__ == "__main__":
    unittest.main()


class BackendEnvTest(unittest.TestCase):
    def test_backends_see_the_same_theme_and_config_folders_as_the_desktop(self):
        # Review finding: Bazzite's login scripts add Flatpak folders to XDG_DATA_DIRS; without it
        # (and XDG_CONFIG_DIRS) the backends and the Plasma tools they start saw fewer themes.
        import forgeappearance as fa
        for name in ("XDG_DATA_DIRS", "XDG_CONFIG_DIRS"):
            self.assertIn(name, fa.BACKEND_ENV)
