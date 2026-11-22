"""Appearance presets (forgepresets): save the current look, apply it in one click, rename, delete,
export as one .forgelook file, import one (hostile files included) and sync through the settings sync folder.

Library tests run in this process with temporary XDG folders and the shipped appearance schema. Section
programs are small fakes that record what they got.
"""
import hashlib
import importlib
import io
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SHIPPED = os.path.join(ROOT, "files/system/usr/share/forge/appearance.json")
SETTINGS_SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
sys.path.insert(0, LIB)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_appearance import png, gif  # noqa: E402


def png_of(color):
    """A different small valid PNG for each color byte, so the files differ."""
    def chunk(kind, data=b""):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, color, color, color]))) + chunk(b"IEND"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = mock.patch.dict(os.environ, {
            "HOME": self.home,
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
            "XDG_DATA_HOME": os.path.join(self.tmp, "data"),
            "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
            "XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
            "FORGE_APPEARANCE_SCHEMA": SHIPPED,
            "FORGE_APPEARANCE_LIBEXEC": os.path.join(self.tmp, "libexec"),
            "FORGE_SCHEMA": SETTINGS_SCHEMA,
            "FORGE_CONF": os.path.join(self.tmp, "etc-settings.json"),
        })
        self.env.start()
        import forgeappearance
        import forgecfg
        import forgepresets
        self.fa = importlib.reload(forgeappearance)
        importlib.reload(forgecfg)
        self.fp = importlib.reload(forgepresets)
        self.schema = self.fa.load_schema()
        self.euid = mock.patch.object(self.fa.os, "geteuid", return_value=1000)
        self.euid.start()

    def tearDown(self):
        self.euid.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def add(self, name, data):
        return self.fa.media_add(self.file(name, data))["id"]

    def backend(self, section, rc=0):
        d = os.path.join(self.tmp, "libexec")
        os.makedirs(d, exist_ok=True)
        out = os.path.join(self.tmp, f"got-{section}.json")
        p = os.path.join(d, "forge-appearance-" + section)
        with open(p, "w") as f:
            f.write("#!%s\nimport json, sys\nd = json.load(sys.stdin)\n"
                    "open(%r, 'w').write(json.dumps({'argv': sys.argv[1:], 'values': d['values']}))\n"
                    "print('%s done')\nsys.exit(%d)\n" % (sys.executable, out, section, rc))
        os.chmod(p, 0o755)
        return out

    def got(self, section):
        with open(os.path.join(self.tmp, f"got-{section}.json")) as f:
            return json.load(f)

    def set(self, section, key, value):
        self.fa.set_value(section, key, value, self.schema)

    def no_choices(self, sid, schema):
        return "ok", {}, ""


class SaveListRenameDeleteTest(Base):
    def test_save_current_look_with_its_media(self):
        pic = self.add("beach.png", png())
        self.set("background", "picture", pic)
        self.set("dock", "style", "floating")
        self.set("theme", "scheme_mode", "dark")
        p = self.fp.save_current("Beach  day ")
        self.assertRegex(p["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(p["name"], "Beach day")
        self.assertEqual(p["values"]["background"]["picture"], pic)
        self.assertEqual(p["values"]["dock"]["style"], "floating")
        self.assertEqual(p["values"]["theme"]["scheme_mode"], "dark")
        self.assertEqual(list(p["media"]), [pic])
        self.assertEqual(p["media"][pic]["sha256"], hashlib.sha256(png()).hexdigest())
        path = os.path.join(self.tmp, "data", "forge", "appearance", "presets", p["id"] + ".json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        listed = self.fp.list_presets()
        self.assertEqual([x["name"] for x in listed], ["Beach day"])
        self.assertEqual(listed[0]["media_count"], 1)

    def test_names_are_checked(self):
        for bad in ("", "   ", "a" * 61, "line\nbreak", "bidi‮", 5, None):
            with self.assertRaises(self.fp.PresetError):
                self.fp.save_current(bad)
        self.fp.save_current("Same")
        self.assertEqual(self.fp.save_current("Same")["name"], "Same (2)")

    def test_rename_and_delete(self):
        p = self.fp.save_current("Old")
        self.fp.rename(p["id"], "New name")
        self.assertEqual(self.fp.list_presets()[0]["name"], "New name")
        with self.assertRaises(self.fp.PresetError):
            self.fp.rename("0" * 32, "x")
        with self.assertRaises(self.fp.PresetError):
            self.fp.rename("../../etc/passwd", "x")
        self.assertTrue(self.fp.delete(p["id"]))
        self.assertFalse(self.fp.delete(p["id"]))
        self.assertEqual(self.fp.list_presets(), [])

    def test_odd_files_in_the_presets_folder_are_ignored(self):
        p = self.fp.save_current("Good")
        d = os.path.dirname(self.fp.preset_path(p["id"]))
        with open(os.path.join(d, "a" * 32 + ".json"), "w") as f:
            f.write("{broken")
        os.symlink("/etc/passwd", os.path.join(d, "b" * 32 + ".json"))
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("x")
        self.assertEqual([x["name"] for x in self.fp.list_presets()], ["Good"])


class ApplyTest(Base):
    def test_apply_saves_every_section_and_runs_each_backend(self):
        for s in ("background", "dock", "boot", "theme"):
            self.backend(s)
        self.set("dock", "style", "floating")
        self.set("theme", "scheme_mode", "dark")
        p = self.fp.save_current("Mine")
        self.set("dock", "style", "default")
        self.set("theme", "scheme_mode", "light")
        r = self.fp.apply(p["id"], choices=self.no_choices)
        self.assertTrue(r["ok"], r)
        self.assertEqual([x["section"] for x in r["results"]], ["background", "dock", "boot", "theme"])
        self.assertTrue(all(x["state"] == "applied" for x in r["results"]))
        vals = self.fa.load(self.schema)
        self.assertEqual(vals["dock"]["style"], "floating")
        self.assertEqual(vals["theme"]["scheme_mode"], "dark")
        self.assertEqual(self.got("theme")["argv"], ["apply"])
        self.assertEqual(self.got("theme")["values"]["scheme_mode"], "dark")

    def test_boot_splash_and_login_screen_need_the_admin_choice(self):
        pic = self.add("splash.png", png())
        self.set("boot", "splash", "on")
        self.set("boot", "splash_media", pic)
        self.set("boot", "lock_screen", "desktop")
        p = self.fp.save_current("With splash")
        self.set("boot", "splash", "off")
        self.set("boot", "lock_screen", "stock")
        info = self.fp.get(p["id"])
        self.assertTrue(self.fp.system_differs(info, self.fa.load(self.schema)))
        r = self.fp.apply(p["id"], include_system=False, choices=self.no_choices)
        vals = self.fa.load(self.schema)
        self.assertEqual(vals["boot"]["splash"], "off")              # kept: needs the admin password
        self.assertEqual(vals["boot"]["lock_screen"], "desktop")     # per user: changed
        self.assertTrue(any("admin password" in n for n in r["notes"]), r["notes"])
        self.fp.apply(p["id"], include_system=True, choices=self.no_choices)
        self.assertEqual(self.fa.load(self.schema)["boot"]["splash"], "on")

    def test_unticked_apply_runs_bootup_in_user_only_mode(self):
        """Without the tick box the boot program gets argv ["apply", "--user-only"]: it never asks for a password
        (review: it called pkexec whenever the system state differed, for example after a cancelled prompt)."""
        for s in ("background", "dock", "boot", "theme"):
            self.backend(s)
        p = self.fp.save_current("Any")
        self.fp.apply(p["id"], include_system=False, choices=self.no_choices)
        self.assertEqual(self.got("boot")["argv"], ["apply", "--user-only"])
        self.assertEqual(self.got("dock")["argv"], ["apply"])
        self.fp.apply(p["id"], include_system=True, choices=self.no_choices)
        self.assertEqual(self.got("boot")["argv"], ["apply"])

    def test_missing_media_keeps_the_current_value(self):
        pic = self.add("a.png", png())
        self.set("background", "picture", pic)
        p = self.fp.save_current("Gone")
        other = self.add("b.png", png_of(9))
        self.set("background", "picture", other)
        self.fa.media_remove(pic)
        r = self.fp.apply(p["id"], choices=self.no_choices)
        self.assertEqual(self.fa.load(self.schema)["background"]["picture"], other)
        self.assertTrue(any("a.png" in n for n in r["notes"]), r["notes"])

    def test_machine_specific_choices_must_match_this_pc(self):
        self.set("background", "screen", "one")
        self.set("background", "screen_connector", "DP-3")
        self.set("theme", "icons", "Papirus")
        p = self.fp.save_current("Other PC")
        self.set("background", "screen", "all")
        self.set("background", "screen_connector", "")
        self.set("theme", "icons", "")

        def here(sid, schema):
            if sid == "background":
                return "ok", {"screen_connector": {"choices": [{"value": "HDMI-A-1", "label": "HDMI-A-1"}]},
                              "we_item": {"choices": []}}, ""
            if sid == "theme":
                return "ok", {"icons": {"choices": [{"value": "Papirus", "label": "Papirus"}]}}, ""
            return "none", {}, ""
        r = self.fp.apply(p["id"], choices=here)
        vals = self.fa.load(self.schema)
        self.assertEqual(vals["background"]["screen"], "all")          # DP-3 is not on this PC
        self.assertEqual(vals["background"]["screen_connector"], "")
        self.assertEqual(vals["theme"]["icons"], "Papirus")            # installed here: used
        self.assertTrue(any("DP-3" in n for n in r["notes"]), r["notes"])

    def test_unknown_preset(self):
        with self.assertRaises(self.fp.PresetError):
            self.fp.apply("f" * 32, choices=self.no_choices)


class ExportImportTest(Base):
    def make(self):
        pic = self.add("beach.png", png())
        anim = self.add("loop.gif", gif(2))
        self.set("background", "picture", pic)
        self.set("boot", "lock_screen", "custom")
        self.set("boot", "lock_media", anim)
        self.set("theme", "accent", "custom")
        self.set("theme", "accent_color", "#ff7a3d")
        return self.fp.save_current("Share me")

    def test_export_then_import_on_another_pc(self):
        p = self.make()
        out = self.file("x.forgelook", b"")
        with open(out, "wb") as f:
            self.fp.export(p["id"], f)
        with zipfile.ZipFile(out) as z:
            names = sorted(z.namelist())
            doc = json.loads(z.read("forge-look.json"))
        self.assertEqual(names, ["forge-look.json", "media/0.png", "media/1.gif"])
        self.assertEqual(doc["values"]["background"]["picture"], "media:0")
        # "Another PC": an empty library and no presets.
        shutil.rmtree(os.path.join(self.tmp, "data"))
        with open(out, "rb") as f:
            q = self.fp.import_file(f)
        self.assertEqual(q["name"], "Share me")
        self.assertNotEqual(q["id"], p["id"])
        lib = self.fa.media_index()
        self.assertEqual(sorted(i["name"] for i in lib.values()), ["beach.png", "loop.gif"])
        self.assertIn(q["values"]["background"]["picture"], lib)
        self.assertEqual(q["values"]["theme"]["accent_color"], "#ff7a3d")

    def test_export_name_keeps_thai_marks(self):
        p = self.fp.save_current("ภาพสวยที่สุด ไม้เอก่")
        self.assertEqual(self.fp.export_name(p["id"]), "ภาพสวยที่สุด ไม้เอก่.forgelook")
        q = self.fp.save_current('a/b\\c"d<e>')
        self.assertEqual(self.fp.export_name(q["id"]), "abcde.forgelook")

    def test_importing_twice_reuses_the_same_media(self):
        p = self.make()
        buf = io.BytesIO()
        self.fp.export(p["id"], buf)
        before = len(self.fa.media_index())
        buf.seek(0)
        q = self.fp.import_file(buf)
        self.assertEqual(len(self.fa.media_index()), before)
        self.assertEqual(q["name"], "Share me (2)")


def zip_bytes(entries, comment=b"", method=zipfile.ZIP_STORED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in entries:
            z.writestr(name, data)
        z.comment = comment
    return buf.getvalue()


def good_doc(**over):
    doc = {"format": "forge-look", "version": 1, "id": "a" * 32, "name": "Hostile", "created": 1700000000.0,
           "updated": 1700000000.0, "values": {"dock": {"style": "floating"}}, "media": []}
    doc.update(over)
    return doc


class HostileImportTest(Base):
    def refused(self, data, why=None):
        with self.assertRaises(self.fp.PresetError) as c:
            self.fp.import_file(io.BytesIO(data))
        if why:
            self.assertIn(why, str(c.exception))
        self.assertEqual(self.fp.list_presets(), [])
        self.assertEqual(self.fa.media_index(), {})

    def test_a_good_minimal_file_works(self):
        q = self.fp.import_file(io.BytesIO(zip_bytes([("forge-look.json", json.dumps(good_doc()))])))
        self.assertEqual(q["values"]["dock"]["style"], "floating")

    def test_not_a_zip(self):
        self.refused(b"")
        self.refused(b"hello")
        self.refused(png())
        tar = io.BytesIO()
        import tarfile
        with tarfile.open(fileobj=tar, mode="w") as t:
            info = tarfile.TarInfo("forge-look.json")
            data = json.dumps(good_doc()).encode()
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        self.refused(tar.getvalue())

    def test_paths_outside_and_unknown_names(self):
        doc = json.dumps(good_doc())
        for name in ("../forge-look.json", "/etc/passwd", "media/../../x.png", "media/0.png/../../a", "other.txt",
                     "media/x.png", "media/0.exe", "media/", "MEDIA/0.png", "media\\0.png"):
            self.refused(zip_bytes([("forge-look.json", doc), (name, png())]))

    def test_duplicate_names(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            with mock.patch("warnings.warn"):
                z.writestr("forge-look.json", json.dumps(good_doc()))
                z.writestr("forge-look.json", json.dumps(good_doc(name="Second")))
        self.refused(buf.getvalue())

    def test_symlink_entry(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("forge-look.json", json.dumps(good_doc()))
            info = zipfile.ZipInfo("media/0.png")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
        self.refused(buf.getvalue())

    def test_deflate_bomb(self):
        bomb = b"\0" * (50 * 1024 * 1024)
        self.refused(zip_bytes([("forge-look.json", json.dumps(good_doc())), ("media/0.png", bomb)],
                               method=zipfile.ZIP_DEFLATED))
        big_json = json.dumps(good_doc(name="x" * 10)) + " " * (5 * 1024 * 1024)
        self.refused(zip_bytes([("forge-look.json", big_json)], method=zipfile.ZIP_DEFLATED))

    def test_too_many_entries(self):
        entries = [("forge-look.json", json.dumps(good_doc()))] + [(f"media/{i}.png", png()) for i in range(40)]
        self.refused(zip_bytes(entries))

    def test_unknown_keys_and_bad_values(self):
        z = lambda d: zip_bytes([("forge-look.json", json.dumps(d))])  # noqa: E731
        self.refused(z(dict(good_doc(), extra=1)))
        self.refused(z(good_doc(format="other")))
        self.refused(z(good_doc(version=2)))
        self.refused(z(good_doc(id="../x")))
        self.refused(z(good_doc(name="")))
        self.refused(z(good_doc(name="a‮b")))
        self.refused(z(good_doc(values={"evil": {"x": 1}})))
        self.refused(z(good_doc(values={"dock": {"nope": 1}})))
        self.refused(z(good_doc(values={"dock": {"style": "rm -rf /"}})))
        self.refused(z(good_doc(values={"theme": {"accent_color": "red"}})))
        self.refused(z(good_doc(values={"background": {"picture": "0" * 32}})))   # only media: references
        self.refused(z(good_doc(values={"background": {"picture": "media:0"}})))  # no such media entry
        self.refused(z(good_doc(created="yesterday")))
        self.refused(zip_bytes([("forge-look.json", "{not json")]))
        self.refused(zip_bytes([("forge-look.json", "[]")]))
        self.refused(zip_bytes([("media/0.png", png())]))

    def test_media_is_checked_by_the_normal_media_path(self):
        def with_media(data, ext="png", **m):
            meta = {"file": f"media/0.{ext}", "name": "x." + ext, "kind": "image", "format": "png", "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
            meta.update(m)
            doc = good_doc(values={"background": {"picture": "media:0"}}, media=[meta])
            return zip_bytes([("forge-look.json", json.dumps(doc)), (f"media/0.{ext}", data)])
        self.refused(with_media(b"#!/bin/sh\nrm -rf ~\n"))                         # not a picture
        self.refused(with_media(png(), sha256="0" * 64))                            # changed on the way
        self.refused(with_media(png(), size=5))
        self.refused(with_media(png(), extra="x"))
        self.refused(with_media(b"<svg xmlns='http://www.w3.org/2000/svg'/>", ext="png"))  # says png, is svg
        q = self.fp.import_file(io.BytesIO(with_media(png())))
        self.assertIn(q["values"]["background"]["picture"], self.fa.media_index())

    def test_a_video_in_a_picture_field_is_refused(self):
        mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64
        meta = {"file": "media/0.mp4", "name": "v.mp4", "kind": "video", "format": "mp4", "size": len(mp4),
                "sha256": hashlib.sha256(mp4).hexdigest()}
        doc = good_doc(values={"background": {"picture": "media:0"}}, media=[meta])
        with mock.patch.object(self.fa, "_ffprobe_ok", return_value=True):
            self.refused(zip_bytes([("forge-look.json", json.dumps(doc)), ("media/0.mp4", mp4)]))

    def test_size_cap_before_reading(self):
        with mock.patch.object(self.fp, "IMPORT_MAX", 1000):
            self.refused(zip_bytes([("forge-look.json", json.dumps(good_doc())), ("media/0.png", b"x" * 2000)]),
                         "too large")

    def test_import_never_runs_anything(self):
        marker = os.path.join(self.tmp, "ran")
        d = os.path.join(self.tmp, "libexec")
        os.makedirs(d)
        for s in ("background", "dock", "boot", "theme"):
            with open(os.path.join(d, "forge-appearance-" + s), "w") as f:
                f.write(f"#!/bin/sh\ntouch {marker}\n")
            os.chmod(os.path.join(d, "forge-appearance-" + s), 0o755)
        self.fp.import_file(io.BytesIO(zip_bytes([("forge-look.json", json.dumps(good_doc()))])))
        self.assertFalse(os.path.exists(marker))


class SyncTest(Base):
    def setUp(self):
        super().setUp()
        # Like the forge-settings-sync tests: FORGE_ROOT stands for "/", the sync folder is in the home folder.
        root = os.path.join(self.tmp, "root")
        os.environ.update(FORGE_ROOT=root, HOME="/home/tester")
        self.sync = os.path.join(root, "home", "tester", "Sync")
        os.makedirs(self.sync)
        with open(os.environ["FORGE_CONF"], "w") as f:
            json.dump({"sync.enabled": True, "sync.folder": "/home/tester/Sync"}, f)
        self.user_dir = os.path.join(self.sync, "forge-looks-" + self.fp.user_name())

    def other_pc(self):
        """Switch to a second PC: new data folders, same sync folder."""
        for k, sub in (("XDG_DATA_HOME", "data2"), ("XDG_STATE_HOME", "state2"), ("XDG_CONFIG_HOME", "config2")):
            os.environ[k] = os.path.join(self.tmp, sub)

    def test_off_or_unset_does_nothing(self):
        with open(os.environ["FORGE_CONF"], "w") as f:
            json.dump({"sync.enabled": False}, f)
        r = self.fp.sync()
        self.assertFalse(r["ok"])
        self.assertFalse(os.path.exists(self.user_dir))

    def test_presets_travel_between_pcs(self):
        pic = self.add("beach.png", png())
        self.set("background", "picture", pic)
        p = self.fp.save_current("Travel")
        r = self.fp.sync()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["sent"], 1)
        self.assertTrue(os.path.isfile(os.path.join(self.user_dir, p["id"] + ".forgelook")))
        self.other_pc()
        r = self.fp.sync()
        self.assertEqual(r["received"], 1, r)
        got = self.fp.list_presets()
        self.assertEqual([(x["id"], x["name"]) for x in got], [(p["id"], "Travel")])
        self.assertEqual(len(self.fa.media_index()), 1)
        # Nothing new: nothing moves.
        r = self.fp.sync()
        self.assertEqual((r["sent"], r["received"]), (0, 0))

    def test_a_change_on_another_pc_arrives_as_a_copy(self):
        p = self.fp.save_current("First")
        self.fp.sync()
        self.other_pc()
        self.fp.sync()
        os.environ["XDG_DATA_HOME"] = os.path.join(self.tmp, "data")
        os.environ["XDG_STATE_HOME"] = os.path.join(self.tmp, "state")
        self.fp.rename(p["id"], "Renamed")
        self.fp.sync()                                   # PC 1 sends the renamed preset
        self.other_pc()
        self.fp.sync()
        names = sorted(x["name"] for x in self.fp.list_presets())
        self.assertEqual(names, ["First", "Renamed"])    # PC 2 keeps its own and gets a copy
        self.assertEqual(self.fp.get(p["id"])["name"], "First")
        self.fp.sync()
        self.assertEqual(len(self.fp.list_presets()), 2)  # once, not on every sync

    def hostile(self, pid, name, updated, style="floating"):
        return zip_bytes([("forge-look.json", json.dumps(good_doc(id=pid, name=name, updated=updated,
                                                                   values={"dock": {"style": style}})))])

    def test_a_sync_folder_writer_can_not_replace_my_preset(self):
        """Review proof (rvLook): a newer file with my preset's id used to overwrite it silently."""
        mine = self.fp.save_current("My work look")
        self.fp.sync()
        with open(os.path.join(self.user_dir, mine["id"] + ".forgelook"), "wb") as f:
            f.write(self.hostile(mine["id"], "Replaced", 9.9e10, "animated"))
        self.fp.sync()
        got = self.fp.get(mine["id"])
        self.assertEqual(got["name"], "My work look")
        self.assertEqual(got["values"]["dock"]["style"], self.fa.load(self.schema)["dock"]["style"])
        copies = [x for x in self.fp.list_presets() if x["id"] != mine["id"]]
        self.assertEqual([c["name"] for c in copies], ["Replaced"])

    def test_race_after_the_check_can_not_bring_back_a_deleted_preset(self):
        """Review proof (rvLook): the file was read twice, so it could be swapped after the check."""
        gone = self.fp.save_current("Deleted here")
        self.fp.sync()
        self.fp.delete(gone["id"])
        decoy = "e" * 32
        path = os.path.join(self.user_dir, decoy + ".forgelook")
        with open(path, "wb") as f:
            f.write(self.hostile(decoy, "Decoy", 1700000000.0))
        real_peek = self.fp._peek

        def peek_then_swap(f):
            head = real_peek(f)
            with open(path, "r+b") as w:
                w.write(self.hostile(gone["id"], "Back from the dead", 1700000000.0))
                w.truncate()
            return head
        with mock.patch.object(self.fp, "_peek", peek_then_swap):
            self.fp.sync()
        ids = [p["id"] for p in self.fp.list_presets()]
        self.assertNotIn(gone["id"], ids)
        self.assertIn(decoy, ids)

    def test_a_flood_from_the_sync_folder_can_not_fill_my_presets(self):
        """Review proof (rvLook): 200 synced files used up PRESETS_MAX, so I could not save."""
        os.makedirs(self.user_dir, mode=0o700)
        for i in range(200):
            pid = "%032x" % (0xabc000 + i)
            with open(os.path.join(self.user_dir, pid + ".forgelook"), "wb") as f:
                f.write(self.hostile(pid, "Spam %d" % i, 1700000000.0))
        r = self.fp.sync()
        self.assertEqual(r["received"], self.fp.SYNCED_MAX)
        self.assertTrue(self.fp.save_current("Mine"))

    def test_summary_says_where_a_preset_came_from(self):
        mine = self.fp.save_current("Mine")
        self.assertEqual(self.fp.summary(mine)["origin"], "local")
        self.fp.sync()
        self.other_pc()
        self.fp.sync()
        self.assertEqual(self.fp.list_presets()[0]["origin"], "sync")

    def test_deleting_a_synced_preset_removes_only_its_own_media(self):
        pic = self.add("beach.png", png())
        self.set("background", "picture", pic)
        shared = self.add("shared.png", png_of(7))
        self.set("boot", "lock_screen", "custom")
        self.set("boot", "lock_media", shared)
        self.fp.save_current("Travel")
        self.fp.sync()
        self.other_pc()
        self.fp.sync()
        got = self.fp.list_presets()[0]
        lib = self.fa.media_index()
        self.assertEqual(len(lib), 2)
        keep = next(m for m, it in lib.items() if it["name"] == "shared.png")
        self.set("boot", "lock_screen", "custom")
        self.set("boot", "lock_media", keep)              # I use this one now: it must stay
        mine = self.add("mine.png", png_of(9))            # added by me: stays
        self.fp.delete(got["id"])
        names = sorted(it["name"] for it in self.fa.media_index().values())
        self.assertEqual(names, ["mine.png", "shared.png"])
        self.assertIn(mine, self.fa.media_index())

    def mountinfo(self, fstype, point=None):
        """A stand-in for /proc/self/mountinfo: "/" on ext4, and the sync folder's drive with fstype."""
        point = point or self.sync
        p = os.path.join(self.tmp, "mountinfo")
        with open(p, "w") as f:
            f.write("22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n")
            f.write("90 22 8:17 / %s rw,relatime shared:50 - %s /dev/sdb1 rw,uid=0,umask=000\n"
                    % (point.replace(" ", "\\040"), fstype))
        os.environ["FORGE_MOUNTINFO"] = p

    def test_shared_drive_without_permissions_still_syncs(self):
        """Review proof (rvLook N1): an NTFS or FAT drive mounted with umask=000 shows every folder as 0777, so
        preset sync refused it while settings sync kept working. Such drives have no real permissions: the owner
        and mode checks are skipped there (copies never overwrite, and the cap of 50 stays)."""
        for fstype in ("vfat", "exfat", "ntfs3", "ntfs", "fuseblk"):
            self.mountinfo(fstype)
            self.assertTrue(self.fp.sync_reduced(), fstype)
        self.fp.save_current("Mine")
        os.makedirs(self.user_dir, exist_ok=True)
        os.chmod(self.user_dir, 0o777)
        loose = os.path.join(self.user_dir, "b" * 32 + ".forgelook")
        with open(loose, "wb") as f:
            f.write(self.hostile("b" * 32, "From my laptop", 1700000000.0))
        os.chmod(loose, 0o777)
        os.chown(loose, 65534, 65534)
        r = self.fp.sync()
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["reduced"])
        self.assertEqual((r["sent"], r["received"]), (1, 1))

    def test_real_linux_filesystems_keep_the_checks(self):
        self.mountinfo("btrfs")
        self.assertFalse(self.fp.sync_reduced())
        os.makedirs(self.user_dir, exist_ok=True)
        os.chmod(self.user_dir, 0o777)
        self.assertFalse(self.fp.sync()["ok"])

    def test_the_mount_with_the_longest_matching_path_decides(self):
        self.mountinfo("vfat", point=os.path.dirname(self.sync) + "/Sync Drive")
        self.assertFalse(self.fp.sync_reduced())          # a different folder with a similar name
        self.mountinfo("vfat", point=os.path.dirname(self.sync))
        self.assertTrue(self.fp.sync_reduced())

    def test_files_and_folders_others_can_write_are_refused(self):
        self.fp.save_current("Mine")
        self.fp.sync()
        other = os.path.join(self.user_dir, "a" * 32 + ".forgelook")
        with open(other, "wb") as f:
            f.write(self.hostile("a" * 32, "Not mine", 1700000000.0))
        os.chown(other, 65534, 65534)
        loose = os.path.join(self.user_dir, "b" * 32 + ".forgelook")
        with open(loose, "wb") as f:
            f.write(self.hostile("b" * 32, "World writable", 1700000000.0))
        os.chmod(loose, 0o666)
        r = self.fp.sync()
        self.assertEqual(r["received"], 0)
        self.assertEqual(len(r["skipped"]), 2, r)
        os.chmod(self.user_dir, 0o777)
        r = self.fp.sync()
        self.assertFalse(r["ok"])

    def test_deleted_here_is_not_brought_back(self):
        p = self.fp.save_current("Bye")
        self.fp.sync()
        self.fp.delete(p["id"])
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, p["id"] + ".forgelook")))
        self.fp.sync()
        self.assertEqual(self.fp.list_presets(), [])

    def test_hostile_files_in_the_sync_folder_are_skipped(self):
        os.makedirs(self.user_dir, mode=0o700)
        with open(os.path.join(self.user_dir, "c" * 32 + ".forgelook"), "wb") as f:
            f.write(b"PK\x03\x04 not really")
        os.symlink("/etc/passwd", os.path.join(self.user_dir, "d" * 32 + ".forgelook"))
        with open(os.path.join(self.user_dir, "..evil.forgelook"), "wb") as f:
            f.write(zip_bytes([("forge-look.json", json.dumps(good_doc()))]))
        r = self.fp.sync()
        self.assertTrue(r["ok"])
        self.assertEqual(r["received"], 0)
        self.assertEqual(len(r["skipped"]), 2, r)
        self.assertEqual(self.fp.list_presets(), [])

    def test_a_link_as_the_user_folder_is_refused(self):
        os.symlink(self.tmp, self.user_dir)
        self.fp.save_current("x")
        r = self.fp.sync()
        self.assertFalse(r["ok"])
        self.assertFalse(any(n.endswith(".forgelook") for n in os.listdir(self.tmp)))


BOOT = os.path.join(ROOT, "files/system/usr/libexec/forge-appearance-boot")


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the backend as a test user")
class BootUserOnlyTest(unittest.TestCase):
    """forge-appearance-boot with argv ["apply", "--user-only"] never runs pkexec, even when the system state
    differs from the saved values (review proof rvLook: UntickedApplyAsksPassword)."""

    def setUp(self):
        import subprocess
        import test_boot_appearance as tba
        self.sp = subprocess
        self.h = tba.BackendTest("test_defaults_change_nothing_and_ask_nothing")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        os.makedirs(self.h.root + "/var/lib/forge-boot")
        with open(self.h.root + "/var/lib/forge-boot/state.json", "w") as f:
            json.dump({"version": 1, "splash": {"enabled": True, "fingerprint": "x", "by_uid": tba.OWNER}}, f)
        self.tba = tba

    def run_boot(self, *argv):
        h, tba = self.h, self.tba
        vals, fields = h.values()
        payload = {"section": "boot", "values": vals, "fields": fields, "media": {},
                   "all": {"background": {"image": "", "color": "#223344"}, "dock": {}, "boot": vals}}
        env = {"HOME": h.home, "PATH": h.bin + ":" + tba.TOOL_PATH, "FORGE_LIB": LIB, "LANG": "C.UTF-8",
               "FORGE_APPEARANCE_SECTION": "boot", "FORGE_APPEARANCE_SCHEMA": h.schema,
               "FORGE_BOOT_ROOT": h.root, "FORGE_BOOT_PKEXEC": os.path.join(h.bin, "pkexec"),
               "FORGE_BOOT_HELPER": "/usr/libexec/forge-boot-apply"}
        return self.sp.run([sys.executable, BOOT, *argv], input=json.dumps(payload), env=env, cwd=h.home,
                           preexec_fn=tba.as_owner, capture_output=True, text=True, timeout=120)

    def test_user_only_never_asks_for_a_password(self):
        r = self.run_boot("apply", "--user-only")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.h.pk_calls(), [])
        self.assertIn("not changed", r.stdout)

    def test_full_apply_still_asks_when_the_system_part_differs(self):
        self.run_boot("apply")
        self.assertEqual(len(self.h.pk_calls()), 1)

    def test_other_argv_is_refused(self):
        self.assertEqual(self.run_boot("apply", "--system").returncode, 2)


TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-appearance")


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the tool as a test uid")
class CliTest(unittest.TestCase):
    def setUp(self):
        import subprocess
        self.sp = subprocess
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o755)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        os.chown(self.home, 65533, 65533)
        self.env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "FORGE_LIB": LIB,
                    "FORGE_APPEARANCE_SCHEMA": SHIPPED, "FORGE_APPEARANCE_LIBEXEC": os.path.join(self.tmp, "libexec"),
                    "FORGE_SCHEMA": SETTINGS_SCHEMA, "FORGE_CONF": os.path.join(self.tmp, "conf.json")}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tool(self, *args):
        def drop():
            os.setgroups([]); os.setgid(65533); os.setuid(65533)
        return self.sp.run([sys.executable, TOOL, *args], env=self.env, preexec_fn=drop, capture_output=True,
                           text=True, timeout=60, cwd=self.home)

    def test_save_list_export_import_rename_delete(self):
        r = self.tool("preset", "save", "Night")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.tool("preset", "list", "--json")
        pid = json.loads(r.stdout)[0]["id"]
        out = os.path.join(self.home, "night.forgelook")
        self.assertEqual(self.tool("preset", "export", pid, out).returncode, 0)
        r = self.tool("preset", "import", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Night (2)", r.stdout)
        self.assertEqual(self.tool("preset", "rename", pid, "Late").returncode, 0)
        r = self.tool("preset", "list")
        self.assertIn("Late", r.stdout)
        self.assertEqual(self.tool("preset", "delete", pid).returncode, 0)
        r = self.tool("preset", "apply", "0" * 32)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no such preset", r.stderr)
        bad = os.path.join(self.home, "bad.forgelook")
        with open(bad, "wb") as f:
            f.write(b"not a zip")
        os.chown(bad, 65533, 65533)
        r = self.tool("preset", "import", bad)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.tool("preset", "frobnicate").returncode, 2)


if __name__ == "__main__":
    unittest.main()
