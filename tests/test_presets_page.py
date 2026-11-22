"""Forge Settings: the appearance preset endpoints and the language switch.

Same setup as test_appearance_page: the server runs as a test-only uid and requests come from child
processes under chosen uids, so the real owner check is used. Needs root to switch uids.
"""
import base64
import io
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_settings_page import request, refused, token_of, as_uid  # noqa: E402
from test_appearance_page import UPLOAD, GETRAW  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "files/system/usr/bin/forge-settings")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
LOOK = os.path.join(ROOT, "files/system/usr/share/forge/appearance.json")
I18N = os.path.join(ROOT, "files/system/usr/share/forge/i18n")
OWNER = 65533
OTHER = 65534


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the server and clients as other uids")
class PresetsPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o755)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        os.chown(self.home, OWNER, OWNER)
        self.libexec = os.path.join(self.tmp, "libexec")
        os.mkdir(self.libexec)
        os.chmod(self.libexec, 0o755)
        self.env = dict(os.environ, FORGE_LIB=LIB, FORGE_SCHEMA=SCHEMA, HOME=self.home,
                        FORGE_CONF=os.path.join(self.tmp, "settings.json"), FORGE_NO_BROWSER="1", FORGE_PORT="0",
                        FORGE_APPEARANCE_SCHEMA=LOOK, FORGE_APPEARANCE_LIBEXEC=self.libexec, FORGE_I18N_DIR=I18N,
                        LANG="C.UTF-8", PATH="/usr/bin:/bin")
        for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "LANGUAGE", "LC_ALL",
                  "LC_MESSAGES"):
            self.env.pop(k, None)
        self.proc = None

    def tearDown(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc.stdout.close()
            self.proc.stderr.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, *args):
        self.proc = subprocess.Popen([sys.executable, "-u", APP, *args], env=self.env, preexec_fn=as_uid(OWNER),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        self.assertTrue(ready, "forge-settings printed no URL")
        m = re.search(r"(http://127\.0\.0\.1:\d+)/\?k=(\S+)", self.proc.stdout.readline())
        time.sleep(0.2)
        self.base, key = m.group(1), m.group(2)
        status, self.page = request(OWNER, "GET", f"{self.base}/?k={key}")
        self.assertEqual(status, 200)
        self.h = {"X-Forge-Token": token_of(self.page), "Content-Type": "application/json"}

    def call(self, method, path, body=None):
        status, text = request(OWNER, method, self.base + path, self.h, json.dumps(body) if body is not None else "")
        return status, json.loads(text)

    def post_raw(self, path, data, length=None):
        p = os.path.join(self.tmp, "body.bin")
        with open(p, "wb") as f:
            f.write(data)
        os.chmod(p, 0o644)
        h = {"X-Forge-Token": self.h["X-Forge-Token"], "Content-Type": "application/octet-stream",
             "Content-Length": str(len(data) if length is None else length), "Host": self.base[7:]}
        host, port = self.base[7:].split(":")
        r = subprocess.run([sys.executable, "-I", "-c", UPLOAD, host, port, path, json.dumps(h),
                            p if length is None else "-"], preexec_fn=as_uid(OWNER), capture_output=True,
                           text=True, timeout=30)
        return json.loads(r.stdout)

    def get_raw(self, path):
        host, port = self.base[7:].split(":")
        h = {"Host": self.base[7:], "X-Forge-Token": self.h["X-Forge-Token"]}
        r = subprocess.run([sys.executable, "-I", "-c", GETRAW, host, port, path, json.dumps(h)],
                           preexec_fn=as_uid(OWNER), capture_output=True, text=True, timeout=30)
        out = json.loads(r.stdout)
        out["body"] = base64.b64decode(out["body"])
        out["headers"] = {k.lower(): v for k, v in out["headers"].items()}
        return out

    def test_presets_need_the_token_and_the_owner(self):
        self.start()
        self.assertEqual(request(OWNER, "GET", self.base + "/api/appearance/presets")[0], 404)
        self.assertTrue(refused(OTHER, "GET", self.base + "/api/appearance/presets", self.h))
        self.assertTrue(refused(OTHER, "POST", self.base + "/api/appearance/presets/save", self.h, '{"name": "x"}'))
        status, d = self.call("GET", "/api/appearance/presets")
        self.assertEqual(status, 200)
        self.assertEqual(d["presets"], [])
        self.assertFalse(d["sync"]["configured"])

    def test_save_rename_export_import_delete(self):
        self.start()
        status, d = self.call("POST", "/api/appearance/save", {"values": {"dock": {"style": "floating"},
                                                                          "theme": {"scheme_mode": "dark"}}})
        self.assertTrue(d["ok"], d)
        _, d = self.call("POST", "/api/appearance/presets/save", {"name": "Evening"})
        self.assertTrue(d["ok"], d)
        pid = d["preset"]["id"]
        _, d = self.call("POST", "/api/appearance/presets/save", {"name": "\n"})
        self.assertFalse(d["ok"])
        _, d = self.call("POST", "/api/appearance/presets/rename", {"id": pid, "name": "Night"})
        self.assertTrue(d["ok"], d)
        _, d = self.call("GET", "/api/appearance/presets")
        self.assertEqual([p["name"] for p in d["presets"]], ["Night"])
        r = self.get_raw("/api/appearance/presets/export?id=" + pid)
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["headers"]["content-type"], "application/octet-stream")
        self.assertEqual(r["headers"]["x-content-type-options"], "nosniff")
        self.assertIn('attachment; filename="Night.forgelook"', r["headers"]["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(r["body"])) as z:
            self.assertEqual(json.loads(z.read("forge-look.json"))["values"]["dock"]["style"], "floating")
        self.assertEqual(self.get_raw("/api/appearance/presets/export?id=../../x")["status"], 404)
        out = self.post_raw("/api/appearance/presets/import", r["body"])
        body = json.loads(out["body"])
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["preset"]["name"], "Night (2)")
        _, d = self.call("POST", "/api/appearance/presets/delete", {"id": pid})
        self.assertTrue(d["ok"])
        _, d = self.call("GET", "/api/appearance/presets")
        self.assertEqual([p["name"] for p in d["presets"]], ["Night (2)"])

    def test_bad_imports_are_refused_with_a_message(self):
        self.start()
        out = self.post_raw("/api/appearance/presets/import", b"#!/bin/sh\necho hi\n")
        body = json.loads(out["body"])
        self.assertFalse(body["ok"])
        self.assertIn("not a Forge preset file", body["output"])
        out = self.post_raw("/api/appearance/presets/import", b"", length=20 * 1024 ** 3)
        self.assertEqual(out["status"], 413)

    def test_apply_runs_each_section(self):
        mark = os.path.join(self.tmp, "ran")
        open(mark, "w").close()
        os.chmod(mark, 0o666)
        for s in ("background", "dock", "boot", "theme"):
            p = os.path.join(self.libexec, "forge-appearance-" + s)
            with open(p, "w") as f:
                f.write(f"#!/bin/sh\necho {s} >> {mark}\necho {s} ok\n")
            os.chmod(p, 0o755)
        self.start()
        _, d = self.call("POST", "/api/appearance/presets/save", {"name": "A"})
        _, d = self.call("POST", "/api/appearance/presets/apply", {"id": d["preset"]["id"], "system": False})
        self.assertTrue(d["ok"], d)
        self.assertEqual([r["state"] for r in d["results"]], ["applied"] * 4)
        with open(mark) as f:
            self.assertEqual(f.read().split(), ["background", "dock", "boot", "theme"])
        _, d = self.call("POST", "/api/appearance/presets/apply", {"id": "nope"})
        self.assertFalse(d["ok"])

    def test_sync_without_a_folder_says_so(self):
        self.start()
        _, d = self.call("POST", "/api/appearance/presets/sync", {})
        self.assertFalse(d["ok"])
        self.assertIn("Settings sync is off", d["output"])

    def test_language_switch(self):
        self.start()
        self.assertIn('id="forge-i18n"', self.page)
        nonce = re.search(r'<script nonce="([^"]+)">', self.page).group(1)
        self.assertIn(f'<script type="application/json" id="forge-i18n" nonce="{nonce}">', self.page)
        self.assertEqual(request(OWNER, "GET", self.base + "/api/i18n")[0], 404)
        _, d = self.call("GET", "/api/i18n")
        self.assertEqual((d["lang"], d["pref"]), ("en", "auto"))
        _, d = self.call("POST", "/api/ui/language", {"language": "th"})
        self.assertTrue(d["ok"])
        self.assertEqual(d["i18n"]["lang"], "th")
        self.assertEqual(d["i18n"]["strings"]["ui.nav.appearance"], "รูปลักษณ์")
        with open(os.path.join(self.home, ".config", "forge", "ui.json")) as f:
            self.assertEqual(json.load(f), {"language": "th"})
        _, d = self.call("POST", "/api/ui/language", {"language": "klingon"})
        self.assertFalse(d["ok"])
        self.assertTrue(refused(OTHER, "POST", self.base + "/api/ui/language", self.h, '{"language": "en"}'))

    def test_thai_locale_gives_the_thai_page(self):
        self.env["LANG"] = "th_TH.UTF-8"
        self.start()
        blob = re.search(r'id="forge-i18n" nonce="[^"]+">(.*?)</script>', self.page, re.S).group(1)
        self.assertEqual(json.loads(blob)["lang"], "th")

    def test_dashboard_and_welcome_pages_get_the_table(self):
        self.start("--dashboard")
        self.assertIn('id="forge-i18n"', self.page)
        self.proc.terminate()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.start("--welcome")
        self.assertIn('id="forge-i18n"', self.page)


if __name__ == "__main__":
    unittest.main()
