"""Settings on several PCs: forge-config export/import, portable exports, and the
per-user forge-settings-sync tool that shares settings through a folder both PCs reach.

Files in the sync folder come from other PCs (or anyone who can write to a shared drive),
so every defense against a hostile file has its own test here.
"""
import calendar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
CONFIG = os.path.join(ROOT, "files/system/usr/bin/forge-config")
SYNC = os.path.join(ROOT, "files/system/usr/bin/forge-settings-sync")
APPLY = os.path.join(ROOT, "files/system/usr/libexec/forge-apply")
UNITS = os.path.join(ROOT, "files/system/usr/lib/systemd/user")

sys.path.insert(0, LIB)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for test_settings_page's helpers
import forgecfg  # noqa: E402

with open(SCHEMA) as _f:
    SCHEMA_DATA = json.load(_f)
DEFS = {s["key"]: s for s in SCHEMA_DATA["settings"]}

# Settings that belong to one PC and are left out of portable exports.
EXPECTED_MACHINE = {
    "security.usb_unlock", "security.usb_key_label", "security.usb_key_timeout_sec",
    "ai.models_dir", "ai.max_loaded_models",
    "modes.auto_switch", "modes.on_battery", "modes.on_charger",
    "modes.gaming.power", "modes.agents.power", "modes.creative.power", "modes.quiet.power",
    "devices.mobile_data", "monitor.gpu_temp_warn", "monitor.mem_warn_gb",
    "virt.enabled", "virt.memory_max_pct", "virt.cpu_weight",
    "sync.enabled", "sync.folder",
}


def ts(text):
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


def export_doc(settings, host="laptop", when="2026-06-01T00:00:00Z", **extra):
    doc = {"format": "forge-settings", "version": 1, "exported_at": when, "from_host": host,
           "image": "forge-os", "settings": settings}
    doc.update(extra)
    return doc


def fake(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


class SchemaTest(unittest.TestCase):
    def test_machine_flags_and_sync_settings(self):
        flagged = {k for k, s in DEFS.items() if s.get("machine") is True}
        self.assertTrue(EXPECTED_MACHINE <= flagged, EXPECTED_MACHINE - flagged)
        groups = {g["id"]: g for g in SCHEMA_DATA["groups"]}
        self.assertEqual(groups["sync"]["label"], "Settings sync")
        self.assertEqual(DEFS["sync.enabled"]["type"], "toggle")
        self.assertIs(DEFS["sync.enabled"]["default"], False)
        self.assertEqual(DEFS["sync.folder"]["type"], "text")
        for k in ("sync.enabled", "sync.folder"):
            for field in ("label", "help", "tags", "group", "default"):
                self.assertIn(field, DEFS[k])
        # The defaults still validate.
        _, errors = forgecfg.validate({}, SCHEMA_DATA)
        self.assertEqual(errors, [])

    def test_sync_folder_rules(self):
        def check(folder, enabled=True):
            return forgecfg.validate({"sync.enabled": enabled, "sync.folder": folder}, SCHEMA_DATA)
        for good in ("/home/alex/Sync", "/var/home/alex/Shared/forge", "/run/media/alex/FORGESYNC",
                     "/mnt/share", "/var/mnt/share/forge", "/run/media/alex/My Drive/forge"):
            clean, errors = check(good)
            self.assertEqual(errors, [], good)
        clean, errors = check("/home/alex/Sync/")
        self.assertEqual(clean["sync.folder"], "/home/alex/Sync")
        for bad in ("relative/path", "/etc/forge", "/home/alex/../../etc", "/home/alex/a..b", "/home/alex",
                    "/home", "/mnt", "/run/media/alex", "/home/alex/x\ny", "/home/alex//x", "/home/alex/./x",
                    "/tmp/sync", "/root/sync", "/home/alex/x\x7f"):
            _, errors = check(bad)
            self.assertTrue(errors, bad)
        # Review finding: bidi, C1 control and other invisible format characters are refused.
        for ch in ("\u202e", "\u202d", "\u2066", "\u2069", "\u200b", "\u200e", "\ufeff", "\u0080",
                   "\u0085", "\u009b", "\u009f", "\u2028", "\u2029", "\ud800", "\U000e0001", "\ue000"):
            _, errors = check(f"/home/alex/Sync{ch}txt.exe")
            self.assertTrue(errors, repr(ch))
        # Second review: invisible letters and odd spaces that are not in those categories.
        for ch in ("\u3164", "\u115f", "\u1160", "\uffa0", "\u2800", "\u034f", "\ufe0f", "\u17b4",
                   "\u00a0", "\u3000", "\u2002", "\u180e"):
            _, errors = check(f"/home/alex/Sync{ch}x")
            self.assertTrue(errors, repr(ch))
        # Folder names in other languages stay fine (Thai has combining marks).
        for good in ("/home/alex/\u0e40\u0e2d\u0e01\u0e2a\u0e32\u0e23", "/run/media/alex/D\u00e9j\u00e0 vu"):
            self.assertEqual(check(good)[1], [], good)
        # Sync on needs a folder; with sync off an empty folder is fine.
        self.assertTrue(check("")[1])
        self.assertEqual(check("", enabled=False)[1], [])


class ExportFormatTest(unittest.TestCase):
    def values(self):
        return forgecfg.values(SCHEMA_DATA, {"ai.keep_alive_min": 7, "ai.models_dir": "/mnt/big/models"})

    def test_full_export_has_header_and_every_setting(self):
        doc = forgecfg.export_doc(self.values(), SCHEMA_DATA, portable=False, host="desk", image="forge-os",
                                  when="2026-11-19T10:00:00Z")
        self.assertEqual({k: doc[k] for k in ("format", "version", "exported_at", "from_host", "image")},
                         {"format": "forge-settings", "version": 1, "exported_at": "2026-11-19T10:00:00Z",
                          "from_host": "desk", "image": "forge-os"})
        self.assertEqual(set(doc["settings"]), set(DEFS))
        self.assertEqual(doc["settings"]["ai.models_dir"], "/mnt/big/models")

    def test_portable_export_leaves_out_machine_settings(self):
        doc = forgecfg.export_doc(self.values(), SCHEMA_DATA, portable=True, host="desk", image="forge-os",
                                  when="2026-11-19T10:00:00Z")
        self.assertEqual(set(doc["settings"]), set(DEFS) - forgecfg.machine_keys(SCHEMA_DATA))
        self.assertNotIn("ai.models_dir", doc["settings"])
        self.assertEqual(doc["settings"]["ai.keep_alive_min"], 7)

    def test_round_trip(self):
        doc = forgecfg.export_doc(self.values(), SCHEMA_DATA, portable=True, host="desk", image="forge-os",
                                  when="2026-11-19T10:00:00Z")
        settings, header, notes, err = forgecfg.parse_export(json.dumps(doc).encode(), SCHEMA_DATA)
        self.assertIsNone(err)
        self.assertEqual(settings, doc["settings"])
        self.assertEqual(header["from_host"], "desk")

    def test_host_names_are_cleaned(self):
        self.assertEqual(forgecfg.clean_host("Alex-PC.local"), "alex-pc-local")
        self.assertEqual(forgecfg.clean_host("../../etc/passwd"), "etc-passwd")
        self.assertEqual(forgecfg.clean_host("\x1b[31mred"), "31mred")
        self.assertEqual(forgecfg.clean_host("x" * 100), "x" * 63)
        self.assertEqual(forgecfg.clean_host("???"), "")


class ParseDefenseTest(unittest.TestCase):
    """Each defense against a hostile file, at the parser."""

    def parse(self, doc, **kw):
        data = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
        return forgecfg.parse_export(data, SCHEMA_DATA, **kw)

    def test_size_cap(self):
        big = export_doc({"ai.keep_alive_min": 7}, padding="x" * (forgecfg.EXPORT_MAX + 10))
        self.assertIn("too large", self.parse(big)[3])

    def test_json_depth(self):
        deep = b'{"settings": ' + b"[" * 5000 + b"]" * 5000 + b"}"
        err = self.parse(deep)[3]
        self.assertIn("nested", err)
        nested_value = export_doc({"ai.keep_alive_min": [[1]]})
        self.assertIsNotNone(self.parse(nested_value)[3])

    def test_only_known_keys(self):
        settings, _, notes, err = self.parse(export_doc({"ai.keep_alive_min": 7, "evil.key": "x"}))
        self.assertIsNone(err)
        self.assertEqual(settings, {"ai.keep_alive_min": 7})
        self.assertTrue(any("1 unknown" in n for n in notes))
        # Unknown top-level fields make the whole file unusable.
        self.assertIsNotNone(self.parse(export_doc({}, extra_field=1))[3])

    def test_values_are_validated(self):
        for bad in ({"ai.keep_alive_min": 99999}, {"ai.keep_alive_min": True}, {"ai.keep_alive_min": "7"},
                    {"privacy.dns_provider": "evil"}, {"privacy.encrypted_dns": "yes"},
                    {"ai.models_dir": "/var/x\nExecStartPre=+/bin/sh"}, {"ai.keep_alive_min": None}):
            self.assertIsNotNone(self.parse(export_doc(bad))[3], bad)

    def test_header_is_checked(self):
        for bad in (dict(export_doc({}), format="other"), dict(export_doc({}), version=2),
                    dict(export_doc({}), version=True), dict(export_doc({}), exported_at="yesterday"),
                    dict(export_doc({}), from_host="Bad Host"), dict(export_doc({}), image="a b\n"),
                    {"settings": {}}, [1, 2], "text"):
            self.assertIsNotNone(self.parse(bad)[3], bad)
        self.assertIsNotNone(self.parse(b"\xff\xfe not utf8")[3])
        self.assertIsNotNone(self.parse(b"{not json")[3])

    def test_plain_settings_only_when_allowed(self):
        self.assertIsNotNone(self.parse({"ai.keep_alive_min": 7})[3])
        settings, header, _, err = self.parse({"ai.keep_alive_min": 7}, allow_plain=True)
        self.assertIsNone(err)
        self.assertEqual(settings, {"ai.keep_alive_min": 7})

    def test_dropped_keys(self):
        settings, _, _, err = self.parse(export_doc({"ai.keep_alive_min": 7, "ai.models_dir": "/mnt/x/m"}),
                                         drop=forgecfg.machine_keys(SCHEMA_DATA))
        self.assertIsNone(err)
        self.assertEqual(settings, {"ai.keep_alive_min": 7})


class ForgeConfigTest(unittest.TestCase):
    """forge-config export and import, with a fake forge-apply that records what it is given."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conf = os.path.join(self.tmp, "settings.json")
        self.rec = os.path.join(self.tmp, "rec")
        fake_apply = os.path.join(self.tmp, "forge-apply")
        fake(fake_apply, f'#!/bin/sh\necho "$@" >> {self.rec}.args\ncat "$2" >> {self.rec}.json\necho >> {self.rec}.json\n')
        self.env = dict(os.environ, FORGE_APPLY=fake_apply, FORGE_LIB=LIB, PYTHONPATH=LIB, FORGE_SCHEMA=SCHEMA,
                        FORGE_CONF=self.conf, FORGE_HOSTNAME="desk")
        with open(self.conf, "w") as f:
            json.dump({"ai.keep_alive_min": 7, "ai.models_dir": "/mnt/big/models",
                       "privacy.dns_provider": "mullvad"}, f)

    def run_tool(self, *args):
        return subprocess.run([sys.executable, CONFIG, *args], env=self.env, capture_output=True, text=True,
                              timeout=30)

    def sent(self):
        if not os.path.exists(self.rec + ".json"):
            return []
        return [json.loads(line) for line in open(self.rec + ".json").read().split("\n") if line.strip()]

    def test_export_full_and_portable(self):
        out = os.path.join(self.tmp, "full.json")
        r = self.run_tool("export", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.load(open(out))
        self.assertEqual(doc["format"], "forge-settings")
        self.assertEqual(doc["from_host"], "desk")
        self.assertIn("image", doc)
        self.assertEqual(doc["settings"]["ai.models_dir"], "/mnt/big/models")
        self.assertEqual(set(doc["settings"]), set(DEFS))
        out2 = os.path.join(self.tmp, "portable.json")
        r = self.run_tool("export", out2, "--portable")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.load(open(out2))
        self.assertFalse(set(doc["settings"]) & EXPECTED_MACHINE)
        self.assertEqual(doc["settings"]["ai.keep_alive_min"], 7)

    def test_export_does_not_follow_a_link(self):
        target = os.path.join(self.tmp, "target")
        open(target, "w").close()
        link = os.path.join(self.tmp, "link.json")
        os.symlink(target, link)
        r = self.run_tool("export", link)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(open(target).read(), "")

    def write(self, doc, name="in.json"):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))
        return p

    def test_import_merge_sends_only_the_files_settings(self):
        f = self.write(export_doc({"ai.keep_alive_min": 9, "privacy.dns_provider": "mullvad"}))
        r = self.run_tool("import", f, "--merge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sent(), [{"ai.keep_alive_min": 9, "privacy.dns_provider": "mullvad"}])
        self.assertTrue(open(self.rec + ".args").read().strip().endswith("--merge"))

    def test_import_without_merge_resets_shared_settings_but_keeps_this_pcs_own(self):
        f = self.write(export_doc({"ai.keep_alive_min": 9}))
        r = self.run_tool("import", f)
        self.assertEqual(r.returncode, 0, r.stderr)
        sent = self.sent()[0]
        self.assertEqual(sent["ai.keep_alive_min"], 9)
        self.assertEqual(sent["privacy.dns_provider"], DEFS["privacy.dns_provider"]["default"])
        self.assertFalse(set(sent) & EXPECTED_MACHINE)   # models_dir and others stay as they are
        # Still sent as a merge, so forge-apply keeps this PC's own settings under its lock.
        self.assertTrue(open(self.rec + ".args").read().strip().endswith("--merge"))

    def test_import_with_machine_settings_sets_them(self):
        f = self.write(export_doc({"ai.models_dir": "/mnt/other/models"}))
        r = self.run_tool("import", f, "--merge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sent(), [{"ai.models_dir": "/mnt/other/models"}])

    def test_dry_run_changes_nothing_and_lists_changes(self):
        f = self.write(export_doc({"ai.keep_alive_min": 9, "privacy.dns_provider": "mullvad"}))
        for extra in ([], ["--merge"]):
            r = self.run_tool("import", f, "--dry-run", *extra)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("ai.keep_alive_min", r.stdout)
            self.assertIn("7 -> 9", r.stdout)
        self.assertEqual(self.sent(), [])
        # dns_provider is already mullvad, so the merge lists only one change.
        r = self.run_tool("import", f, "--dry-run", "--merge")
        self.assertNotIn("privacy.dns_provider", r.stdout)

    def test_import_accepts_a_plain_settings_file(self):
        f = self.write({"ai.keep_alive_min": 11})
        r = self.run_tool("import", f, "--merge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sent(), [{"ai.keep_alive_min": 11}])

    def test_bad_imports_are_refused_before_forge_apply(self):
        target = self.write(export_doc({"ai.keep_alive_min": 9}), "target.json")
        link = os.path.join(self.tmp, "link.json")
        os.symlink(target, link)
        fifo = os.path.join(self.tmp, "fifo.json")
        os.mkfifo(fifo)
        cases = [link, fifo, self.write(export_doc({"ai.keep_alive_min": 99999}), "bad.json"),
                 self.write("{", "broken.json"), self.write(export_doc({"agents.memory_high_pct": 90}), "x.json"),
                 os.path.join(self.tmp, "missing.json")]
        for f in cases:
            r = self.run_tool("import", f)
            self.assertNotEqual(r.returncode, 0, f)
            self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.sent(), [])

    def test_unknown_options_are_refused(self):
        f = self.write(export_doc({}))
        for args in (["import"], ["import", f, "--force"], ["export"], ["export", "a", "--nope"]):
            r = self.run_tool(*args)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertEqual(self.sent(), [])


class ForgeApplyImportTest(unittest.TestCase):
    """forge-apply --import also understands the export format."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.conf = os.path.join(self.root, "etc/forge/settings.json")
        os.makedirs(os.path.dirname(self.conf))
        with open(self.conf, "w") as f:
            json.dump({"ai.models_dir": "/mnt/big/models", "privacy.dns_provider": "mullvad",
                       "ai.keep_alive_min": 7}, f)
        self.env = dict(os.environ, FORGE_ROOT=self.root, FORGE_DRYRUN="1", FORGE_CONF=self.conf,
                        PYTHONPATH=LIB, FORGE_SCHEMA=SCHEMA)
        self.env.pop("SUDO_USER", None)

    def apply(self, *args):
        return subprocess.run([sys.executable, APPLY, *args], env=self.env, capture_output=True, text=True,
                              timeout=60)

    def test_portable_export_import_keeps_this_pcs_own_settings(self):
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump(export_doc({"ai.keep_alive_min": 9}), fh)
        r = self.apply("--import", f)
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = json.load(open(self.conf))
        self.assertEqual(saved["ai.keep_alive_min"], 9)
        self.assertEqual(saved["ai.models_dir"], "/mnt/big/models")     # machine setting kept
        self.assertNotIn("privacy.dns_provider", saved)                  # shared setting back to default
        self.assertTrue(os.listdir(os.path.join(self.root, "var/lib/forge-apply/snapshots")))

    def test_bad_export_file_is_refused(self):
        f = os.path.join(self.root, "in.json")
        with open(f, "w") as fh:
            json.dump(export_doc({"ai.keep_alive_min": 9}, from_host="Not Valid"), fh)
        r = self.apply("--import", f, "--merge")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.load(open(self.conf))["ai.keep_alive_min"], 7)


class SyncToolTest(unittest.TestCase):
    """forge-settings-sync, run as a normal user, with fake notify-send and pkexec."""

    HOME = "/home/tester"
    FOLDER = "/home/tester/Sync"

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.folder = self.root + self.FOLDER
        os.makedirs(self.folder)
        self.conf = os.path.join(self.root, "etc/forge/settings.json")
        os.makedirs(os.path.dirname(self.conf))
        self.bin = os.path.join(self.root, "bin")
        os.mkdir(self.bin)
        self.log = os.path.join(self.root, "calls")
        self.sent_file = os.path.join(self.root, "sent.json")
        fake(os.path.join(self.bin, "notify-send"),
             f'#!/usr/bin/env python3\nimport json, sys\nopen("{self.log}", "a").write(json.dumps(["notify-send"] + sys.argv[1:]) + "\\n")\n')
        fake(os.path.join(self.bin, "pkexec"),
             "#!/usr/bin/env python3\nimport json, os, shutil, sys\n"
             f'open("{self.log}", "a").write(json.dumps(["pkexec"] + sys.argv[1:]) + "\\n")\n'
             f'if os.path.exists("{self.root}/cancel"): sys.exit(126)\n'
             f'shutil.copy(sys.argv[3], "{self.sent_file}")\n'
             'print("saved")\n')
        self.env = dict(os.environ, FORGE_ROOT=self.root, HOME=self.HOME, FORGE_LIB=LIB, PYTHONPATH=LIB,
                        FORGE_SCHEMA=SCHEMA, FORGE_CONF=self.conf, FORGE_HOSTNAME="desk",
                        XDG_STATE_HOME=os.path.join(self.root, "state"), FORGE_APPLY="/usr/libexec/forge-apply",
                        PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        self.settings(**{"sync.enabled": True, "sync.folder": self.FOLDER, "ai.keep_alive_min": 7,
                         "ai.models_dir": "/mnt/big/models"})

    def settings(self, when="2026-01-01T00:00:00Z", **values):
        with open(self.conf, "w") as f:
            json.dump(values, f)
        os.utime(self.conf, (ts(when), ts(when)))

    def sync(self, *args):
        return subprocess.run([sys.executable, SYNC, *args], env=self.env, capture_output=True, text=True,
                              timeout=30)

    def calls(self, name=None):
        if not os.path.exists(self.log):
            return []
        out = [json.loads(line) for line in open(self.log)]
        return [c for c in out if name is None or c[0] == name]

    def own(self):
        return os.path.join(self.folder, "forge-settings-desk.json")

    def other(self, doc, host="laptop", raw=None):
        p = os.path.join(self.folder, f"forge-settings-{host}.json")
        with open(p, "wb") as f:
            f.write(raw if raw is not None else json.dumps(doc).encode())
        return p

    # The writer

    def test_off_does_nothing(self):
        self.settings(**{"sync.enabled": False, "sync.folder": self.FOLDER})
        r = self.sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.folder), [])

    def test_writes_this_pcs_portable_export_when_settings_change(self):
        r = self.sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.load(open(self.own()))
        self.assertEqual(doc["format"], "forge-settings")
        self.assertEqual(doc["from_host"], "desk")
        self.assertEqual(doc["exported_at"], "2026-01-01T00:00:00Z")   # when the settings last changed
        self.assertEqual(doc["settings"]["ai.keep_alive_min"], 7)
        self.assertFalse(set(doc["settings"]) & EXPECTED_MACHINE)
        # Unchanged settings: the file is left alone.
        inode = os.stat(self.own()).st_ino
        self.sync()
        self.assertEqual(os.stat(self.own()).st_ino, inode)
        # Changed: written again (in one step, so a new file).
        self.settings(when="2026-02-01T00:00:00Z", **{"sync.enabled": True, "sync.folder": self.FOLDER,
                                                       "ai.keep_alive_min": 8})
        self.sync()
        doc = json.load(open(self.own()))
        self.assertEqual(doc["settings"]["ai.keep_alive_min"], 8)
        self.assertEqual(doc["exported_at"], "2026-02-01T00:00:00Z")
        self.assertEqual([n for n in os.listdir(self.folder) if n.startswith(".")], [])

    def test_a_machine_only_change_does_not_rewrite(self):
        self.sync()
        inode = os.stat(self.own()).st_ino
        self.settings(when="2026-02-01T00:00:00Z", **{"sync.enabled": True, "sync.folder": self.FOLDER,
                                                       "ai.keep_alive_min": 7, "ai.models_dir": "/mnt/new/m"})
        self.sync()
        self.assertEqual(os.stat(self.own()).st_ino, inode)

    def test_own_file_replaced_by_a_link_is_not_followed(self):
        target = os.path.join(self.root, "victim")
        with open(target, "w") as f:
            f.write("keep")
        os.symlink(target, self.own())
        r = self.sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(target).read(), "keep")
        self.assertFalse(os.path.islink(self.own()))

    def test_missing_folder_is_quiet(self):
        shutil.rmtree(self.folder)
        r = self.sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls("notify-send"), [])

    # Newer-file detection and the single notification

    def test_newer_different_file_gives_one_notification(self):
        self.other(export_doc({"ai.keep_alive_min": 9}))
        for _ in range(3):
            r = self.sync()
            self.assertEqual(r.returncode, 0, r.stderr)
        notes = self.calls("notify-send")
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("Settings from laptop are available", notes[0])
        self.assertIn("--", notes[0])
        self.assertTrue(any("forge-settings-sync diff laptop" in a for a in notes[0]))
        # A newer version of that file is announced once more.
        self.other(export_doc({"ai.keep_alive_min": 10}, when="2026-07-01T00:00:00Z"))
        self.sync()
        self.sync()
        self.assertEqual(len(self.calls("notify-send")), 2)

    def test_older_file_is_not_announced(self):
        self.other(export_doc({"ai.keep_alive_min": 9}, when="2025-06-01T00:00:00Z"))
        self.sync()
        self.assertEqual(self.calls("notify-send"), [])

    def test_newer_but_same_settings_is_not_announced(self):
        self.sync()
        mine = json.load(open(self.own()))["settings"]
        self.other(export_doc(mine))
        self.sync()
        self.assertEqual(self.calls("notify-send"), [])

    def test_only_machine_differences_are_not_announced(self):
        self.other(export_doc({"ai.keep_alive_min": 7, "ai.models_dir": "/mnt/x/models"}))
        self.sync()
        self.assertEqual(self.calls("notify-send"), [])

    def test_status_lists_hosts(self):
        self.other(export_doc({"ai.keep_alive_min": 9}))
        self.sync()
        r = self.sync("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("laptop", r.stdout)

    def test_status_json_for_the_dashboard(self):
        self.other(export_doc({"ai.keep_alive_min": 9}))
        self.sync()
        r = self.sync("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["this_pc"], "desk")
        self.assertTrue(d["enabled"])
        self.assertIsNone(d["message"])
        self.assertEqual(d["hosts"], [{"host": "laptop", "saved_at": "2026-06-01T00:00:00Z", "differences": 1,
                                       "newer": True, "problem": None}])
        # A folder that is not there is still a JSON answer.
        shutil.rmtree(self.folder)
        r = self.sync("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["hosts"], [])
        self.assertIn("can't be reached", d["message"])

    # diff and apply

    def test_diff_shows_the_differences(self):
        self.other(export_doc({"ai.keep_alive_min": 9, "privacy.dns_provider": "quad9"}))
        r = self.sync("diff", "laptop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ai.keep_alive_min", r.stdout)
        self.assertIn("7 -> 9", r.stdout)
        self.assertNotIn("dns_provider", r.stdout)   # same on both
        self.assertEqual(self.calls("pkexec"), [])

    def test_apply_goes_through_the_password_prompt(self):
        self.other(export_doc({"ai.keep_alive_min": 9, "ai.models_dir": "/mnt/x/m", "evil.key": 1}))
        r = self.sync("apply", "laptop")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls("pkexec")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:3], ["/usr/libexec/forge-apply", "--import"])
        self.assertEqual(calls[0][4:], ["--merge"])
        self.assertEqual(json.load(open(self.sent_file)), {"ai.keep_alive_min": 9})
        self.assertFalse(os.path.exists(calls[0][3]))   # temp file cleaned up

    def test_apply_cancelled_password(self):
        self.other(export_doc({"ai.keep_alive_min": 9}))
        open(os.path.join(self.root, "cancel"), "w").close()
        r = self.sync("apply", "laptop")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ancelled", r.stdout + r.stderr)

    def test_apply_with_nothing_to_change_asks_for_nothing(self):
        self.other(export_doc({"ai.keep_alive_min": 7}))
        r = self.sync("apply", "laptop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls("pkexec"), [])

    def test_apply_refuses_cross_setting_conflicts(self):
        self.other(export_doc({"agents.memory_high_pct": 90}))   # above the hard cap of 75
        r = self.sync("apply", "laptop")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls("pkexec"), [])

    # Untrusted-file defenses, through the tool

    def refused(self, host="laptop"):
        r1 = self.sync()
        r2 = self.sync("diff", host)
        r3 = self.sync("apply", host)
        for r in (r1, r2, r3):
            self.assertNotIn("Traceback", r.stderr)
        self.assertNotEqual(r2.returncode, 0)
        self.assertNotEqual(r3.returncode, 0)
        self.assertEqual(self.calls("pkexec"), [])
        self.assertEqual(self.calls("notify-send"), [])
        return r2.stdout + r2.stderr

    def test_defense_size_cap(self):
        self.other(None, raw=json.dumps(export_doc({"ai.keep_alive_min": 9},
                                                   pad="x" * (forgecfg.EXPORT_MAX + 1))).encode())
        self.assertIn("too large", self.refused())

    def test_defense_json_depth(self):
        self.other(None, raw=b'{"settings": ' + b"[" * 100000 + b"]" * 100000 + b"}")
        self.assertIn("nested", self.refused())

    def test_defense_only_known_keys(self):
        self.other(export_doc({"evil.key": "x", "ai.keep_alive_min": 7}))
        r = self.sync("diff", "laptop")
        self.assertEqual(r.returncode, 0)
        self.assertIn("unknown", r.stdout)
        self.assertNotIn("evil.key", r.stdout)   # never echoed
        self.sync("apply", "laptop")
        self.assertEqual(self.calls("pkexec"), [])  # nothing known differs
        self.other(dict(export_doc({}), settings=[1]))
        self.refused()

    def test_defense_values_validated(self):
        self.other(export_doc({"ai.keep_alive_min": 99999}))
        self.refused()
        self.other(export_doc({"privacy.dns_provider": "x\n\x1b[31m"}))
        self.refused()

    def test_defense_host_names(self):
        # Odd file names are ignored; the header must match the file name.
        with open(os.path.join(self.folder, "forge-settings-Bad Name.json"), "w") as f:
            json.dump(export_doc({"ai.keep_alive_min": 9}, host="bad-name"), f)
        with open(os.path.join(self.folder, "forge-settings-\x1b[31m.json"), "w") as f:
            json.dump(export_doc({"ai.keep_alive_min": 9}), f)
        self.sync()
        self.assertEqual(self.calls("notify-send"), [])
        self.other(export_doc({"ai.keep_alive_min": 9}, host="desk"))  # pretends to be someone else
        self.refused()
        for bad in ("../laptop", "LAPTOP", "", "a/b", "-x", "\x1b[31m"):
            r = self.sync("diff", bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertNotIn("Traceback", r.stderr)

    def test_defense_symlinks_refused(self):
        target = os.path.join(self.root, "elsewhere.json")
        with open(target, "w") as f:
            json.dump(export_doc({"ai.keep_alive_min": 9}), f)
        os.symlink(target, os.path.join(self.folder, "forge-settings-laptop.json"))
        self.assertIn("link", self.refused())

    def test_defense_special_files_do_not_hang(self):
        os.mkfifo(os.path.join(self.folder, "forge-settings-laptop.json"))
        start = time.monotonic()
        self.refused()
        self.assertLess(time.monotonic() - start, 20)

    def test_defense_too_many_files(self):
        for i in range(300):
            self.other(export_doc({"ai.keep_alive_min": 9}, host=f"pc{i}"), host=f"pc{i}")
        r = self.sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        notes = [n for n in self.calls("notify-send") if not any("too many" in a.lower() for a in n)]
        self.assertEqual(len(notes), 1)            # one notification for every PC together
        self.assertTrue(any("more" in a for a in notes[0]))
        self.assertLess(len(" ".join(notes[0])), 400)

    def test_defense_decoys_that_sort_first_do_not_hide_a_real_pc(self):
        # Review finding: 64 junk files named to sort before a real PC pushed it out of the limit.
        for i in range(64):
            p = self.other(None, host=f"a{i:02d}", raw=b"junk")
            os.utime(p, (ts("2026-01-01T00:00:00Z"),) * 2)
        self.other(export_doc({"ai.keep_alive_min": 9}))
        r = self.sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        notes = self.calls("notify-send")
        self.assertTrue(any("Settings from laptop are available" in a for n in notes for a in n), notes)
        # More files than the limit: said once, not on every check.
        self.assertIn("not looked at", r.stdout)
        warned = [n for n in notes if any("too many" in a.lower() for a in n)]
        self.assertEqual(len(warned), 1, notes)
        self.sync()
        warned = [n for n in self.calls("notify-send") if any("too many" in a.lower() for a in n)]
        self.assertEqual(len(warned), 1)

    def test_files_dated_in_the_future_do_not_crowd_out_real_pcs(self):
        for i in range(64):
            p = self.other(None, host=f"z{i:02d}", raw=b"junk")
            os.utime(p, (time.time() + 30 * 86400,) * 2)
        self.other(export_doc({"ai.keep_alive_min": 9}))
        self.sync()
        notes = self.calls("notify-send")
        self.assertTrue(any("Settings from laptop are available" in a for n in notes for a in n), notes)

    def test_a_new_export_time_with_the_same_values_is_not_announced_again(self):
        # Review finding: only exported_at changed, and every change made a new notice.
        self.other(export_doc({"ai.keep_alive_min": 9}))
        self.sync()
        self.assertEqual(len(self.calls("notify-send")), 1)
        for when in ("2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z"):
            self.other(export_doc({"ai.keep_alive_min": 9}, when=when))
            self.sync()
        self.assertEqual(len(self.calls("notify-send")), 1)
        # Different values are announced.
        self.other(export_doc({"ai.keep_alive_min": 10}, when="2026-09-01T00:00:00Z"))
        self.sync()
        self.assertEqual(len(self.calls("notify-send")), 2)

    def test_defense_notification_text_escaped(self):
        # Host names are already limited to a-z, 0-9 and "-", so nothing can be injected.
        self.other(export_doc({"ai.keep_alive_min": 9}, host="a-b"), host="a-b")
        self.sync()
        note = self.calls("notify-send")[0]
        self.assertNotIn("<", "".join(note))

    def test_defense_folder_must_be_safe(self):
        # Outside the allowed places, or someone else's home, or a link out of the home.
        for folder in ("/etc", "/home/other/Sync"):
            os.makedirs(self.root + folder, exist_ok=True)
            self.settings(**{"sync.enabled": True, "sync.folder": folder})
            r = self.sync()
            self.assertNotIn("Traceback", r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.root + folder, "forge-settings-desk.json")), folder)
        out = os.path.join(self.root, "outside")
        os.mkdir(out)
        os.symlink(out, self.root + "/home/tester/Link")
        self.settings(**{"sync.enabled": True, "sync.folder": "/home/tester/Link"})
        self.sync()
        self.assertEqual(os.listdir(out), [])

    def test_others_files_are_never_changed(self):
        p = self.other(export_doc({"ai.keep_alive_min": 9}))
        before = open(p, "rb").read()
        self.sync()
        self.sync("diff", "laptop")
        self.sync("apply", "laptop")
        self.assertEqual(open(p, "rb").read(), before)


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the server and clients as other uids")
class SettingsPageExportImportTest(unittest.TestCase):
    """The optional Export and Import buttons on the Forge Settings page."""
    import test_settings_page as tsp   # noqa: E402 (its helpers; its own tests stay in that module)
    setUp = tsp.SettingsPageTest.setUp
    tearDown = tsp.SettingsPageTest.tearDown
    start = tsp.SettingsPageTest.start

    def open_page(self):
        base, k = self.start()
        status, body = self.tsp.request(self.tsp.OWNER, "GET", f"{base}/?k={k}")
        self.assertEqual(status, 200)
        self.assertIn('id="export"', body)
        self.assertIn('id="import"', body)
        return base, {"X-Forge-Token": self.tsp.token_of(body), "Content-Type": "application/json"}

    def test_export_and_import_check(self):
        base, h = self.open_page()
        req = lambda *a: self.tsp.request(self.tsp.OWNER, *a)   # noqa: E731
        status, out = req("GET", f"{base}/api/export?portable=1", h)
        self.assertEqual(status, 200)
        doc = json.loads(out)
        self.assertEqual(doc["format"], "forge-settings")
        self.assertFalse(set(doc["settings"]) & EXPECTED_MACHINE)
        status, out = req("GET", f"{base}/api/export", h)
        self.assertIn("ai.models_dir", json.loads(out)["settings"])
        self.assertNotEqual(req("GET", f"{base}/api/export")[0], 200)          # needs the token
        # Import only checks the file and hands the values to the page; saving is the normal Save.
        text = json.dumps(export_doc({"ai.keep_alive_min": 9, "evil": 1}))
        status, out = req("POST", f"{base}/api/import-check", h, json.dumps({"text": text}))
        d = json.loads(out)
        self.assertTrue(d["ok"], out)
        self.assertEqual(d["values"], {"ai.keep_alive_min": 9})
        self.assertTrue(d["notes"])
        for bad in (json.dumps(export_doc({"ai.keep_alive_min": 99999})), "{", "[" * 5000, 5):
            status, out = req("POST", f"{base}/api/import-check", h, json.dumps({"text": bad}))
            self.assertFalse(json.loads(out)["ok"], bad)
        self.assertFalse(os.path.exists(self.mark))   # nothing was applied
        self.assertNotEqual(req("POST", f"{base}/api/import-check", {"Content-Type": "application/json"},
                                json.dumps({"text": text}))[0], 200)


class UnitsTest(unittest.TestCase):
    def test_units_and_install(self):
        for name in ("forge-settings-sync.service", "forge-settings-sync.timer", "forge-settings-sync.path"):
            self.assertTrue(os.path.exists(os.path.join(UNITS, name)), name)
        svc = open(os.path.join(UNITS, "forge-settings-sync.service")).read()
        self.assertIn("ExecStart=/usr/bin/forge-settings-sync\n", svc)
        timer = open(os.path.join(UNITS, "forge-settings-sync.timer")).read()
        self.assertIn("OnStartupSec=", timer)       # at login
        self.assertIn("OnUnitActiveSec=", timer)    # and then regularly
        fin = open(os.path.join(ROOT, "files/scripts/finalize.sh")).read()
        self.assertIn("/usr/bin/forge-settings-sync", fin)
        apply_src = open(APPLY).read()
        self.assertIn('service("forge-settings-sync.timer", v["sync.enabled"], user=True)', apply_src)


if __name__ == "__main__":
    unittest.main()
