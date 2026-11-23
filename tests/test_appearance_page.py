"""Forge Settings: the Appearance endpoints (values, schema, media upload and media files).

Same setup as test_settings_page: the server runs as a test-only uid and requests come from
child processes under chosen uids, so the real owner check is used. Needs root to switch uids.
"""
import json
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_settings_page import request, request_headers, refused, token_of, as_uid  # noqa: E402
from test_appearance import png, gif, SAMPLES  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "files/system/usr/bin/forge-settings")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
DEMO = os.path.join(ROOT, "tests/data/appearance-demo.json")
OWNER = 65533
OTHER = 65534

# Sends a body from a file, or only the headers (to test that a too large upload is refused up front).
UPLOAD = r"""
import http.client, json, sys
host, port, path, headers, body_file = sys.argv[1], int(sys.argv[2]), sys.argv[3], json.loads(sys.argv[4]), sys.argv[5]
c = http.client.HTTPConnection(host, port, timeout=20)
c.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
for k, v in headers.items():
    c.putheader(k, v)
c.endheaders()
if body_file != "-":
    with open(body_file, "rb") as f:
        c.send(f.read())
r = c.getresponse()
print(json.dumps({"status": r.status, "body": r.read().decode(errors="replace"), "headers": dict(r.getheaders())}))
"""

GETRAW = r"""
import http.client, json, sys, base64
host, port, path, headers = sys.argv[1], int(sys.argv[2]), sys.argv[3], json.loads(sys.argv[4])
c = http.client.HTTPConnection(host, port, timeout=20)
c.request("GET", path, headers=headers)
r = c.getresponse()
print(json.dumps({"status": r.status, "body": base64.b64encode(r.read()).decode(), "headers": dict(r.getheaders())}))
"""


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the server and clients as other uids")
class AppearancePageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o755)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        os.chown(self.home, OWNER, OWNER)
        self.mark = os.path.join(self.tmp, "applied.json")
        fakebin = os.path.join(self.tmp, "bin")
        os.mkdir(fakebin)
        for name, text in (("pkexec", '#!/bin/sh\nexec "$@"\n'),
                           ("forge-apply", f'#!/bin/sh\ncat "$2" > "{self.mark}"\n')):
            p = os.path.join(fakebin, name)
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, 0o755)
        self.libexec = os.path.join(self.tmp, "libexec")
        os.mkdir(self.libexec)
        self.env = dict(os.environ, FORGE_LIB=LIB, FORGE_SCHEMA=SCHEMA, HOME=self.home,
                        FORGE_CONF=os.path.join(self.tmp, "settings.json"), FORGE_NO_BROWSER="1",
                        FORGE_PORT="0", FORGE_APPLY=os.path.join(fakebin, "forge-apply"),
                        FORGE_APPEARANCE_SCHEMA=DEMO, FORGE_APPEARANCE_LIBEXEC=self.libexec,
                        PATH=fakebin + os.pathsep + "/usr/bin:/bin")
        for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
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
        m = re.search(r'const MEDIA_KEY = "([^"]+)"', self.page)
        self.media_key = m.group(1) if m else None

    def upload(self, data, name="pic.png", headers=None, uid=OWNER, send_body=True):
        body = os.path.join(self.tmp, "body.bin")
        with open(body, "wb") as f:
            f.write(data)
        os.chmod(body, 0o644)
        h = {"X-Forge-Token": self.h["X-Forge-Token"], "Content-Type": "application/octet-stream",
             "Content-Length": str(len(data)), "Host": self.base[7:]}
        h.update(headers or {})
        host, port = self.base[7:].split(":")
        path = "/api/appearance/upload?name=" + urllib.parse.quote(name)
        r = subprocess.run([sys.executable, "-I", "-c", UPLOAD, host, port, path, json.dumps(h),
                            body if send_body else "-"], preexec_fn=as_uid(uid), capture_output=True, text=True,
                           timeout=30)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)

    def get_raw(self, path, headers=None, uid=OWNER):
        import base64
        host, port = self.base[7:].split(":")
        h = {"Host": self.base[7:]}
        h.update(headers or {})
        r = subprocess.run([sys.executable, "-I", "-c", GETRAW, host, port, path, json.dumps(h)],
                           preexec_fn=as_uid(uid), capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        out = json.loads(r.stdout)
        out["body"] = base64.b64decode(out["body"])
        out["headers"] = {k.lower(): v for k, v in out["headers"].items()}
        return out

    def media_dir(self):
        return os.path.join(self.home, ".local", "share", "forge", "appearance", "media")

    def test_page_has_the_sidebar_sections_and_a_nonce_csp(self):
        self.start()
        for label in ("Appearance", "System", "Network and devices", "Privacy and security", "AI", "Data"):
            self.assertIn(label, self.page)
        out = request_headers(OWNER, "GET", f"{self.base}/api/ping", self.h)
        csp = {k.lower(): v for k, v in out["headers"].items()}["content-security-policy"]
        nonce = re.search(r"'nonce-([A-Za-z0-9_-]{16,})'", csp).group(1)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertIn(f'<script nonce="{nonce}">', self.page)
        self.assertIn(f'<style nonce="{nonce}">', self.page)
        self.assertNotRegex(self.page, r"<script(?![^>]*nonce=)", "a script without the nonce")
        self.assertNotRegex(self.page, r"\son[a-z]+=\"", "an inline event handler")
        self.assertNotRegex(self.page, r"https?://(?!127\.0\.0\.1)[a-z]", "an outside resource")

    def test_appearance_reads_need_the_token_and_the_owner(self):
        self.start()
        for path in ("/api/appearance", "/api/appearance/media"):
            self.assertEqual(request(OWNER, "GET", self.base + path)[0], 404, path)
            self.assertTrue(refused(OTHER, "GET", self.base + path, self.h), path)
        status, body = request(OWNER, "GET", self.base + "/api/appearance", self.h)
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual([s["id"] for s in d["schema"]["sections"]], ["background", "dock", "boot"])
        self.assertEqual(d["values"]["dock"]["size"], 48)
        self.assertEqual(d["backends"], {"background": False, "dock": False, "boot": False})
        self.assertEqual(d["media"], [])

    def test_save_and_apply(self):
        self.start()
        url = self.base + "/api/appearance/save"
        body = json.dumps({"values": {"dock": {"size": 64, "autohide": True}}})
        self.assertTrue(refused(OTHER, "POST", url, self.h, body))
        self.assertEqual(request(OWNER, "POST", url, {"Content-Type": "application/json"}, body)[0], 403)
        status, out = request(OWNER, "POST", url, self.h, body)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(out)["ok"], out)
        cfg = os.path.join(self.home, ".config", "forge", "appearance.json")
        self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)
        self.assertEqual(os.stat(cfg).st_uid, OWNER)
        with open(cfg) as f:
            self.assertEqual(json.load(f)["dock"]["size"], 64)
        self.assertFalse(os.path.exists(self.mark), "appearance must never go through forge-apply")
        for bad in ({"values": {"dock": {"size": 1000}}}, {"values": {"dock": {"nope": 1}}}, {"values": [1]},
                    {"values": {"evil": {}}}):
            status, out = request(OWNER, "POST", url, self.h, json.dumps(bad))
            self.assertFalse(json.loads(out)["ok"], bad)
        with open(cfg) as f:
            self.assertEqual(json.load(f)["dock"]["size"], 64)
        # Apply: no backend yet, then one that works.
        status, out = request(OWNER, "POST", self.base + "/api/appearance/apply", self.h, '{"section": "dock"}')
        res = json.loads(out)
        self.assertEqual(res["results"][0]["state"], "not-installed")
        self.assertIn("not installed yet", res["results"][0]["output"])
        p = os.path.join(self.libexec, "forge-appearance-dock")
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho \"ran $1 as $(id -u)\"\n")
        os.chmod(p, 0o755)
        status, out = request(OWNER, "POST", self.base + "/api/appearance/apply", self.h, '{"section": "dock"}')
        res = json.loads(out)
        self.assertTrue(res["ok"], out)
        self.assertIn(f"ran apply as {OWNER}", res["results"][0]["output"])
        status, out = request(OWNER, "POST", self.base + "/api/appearance/apply", self.h, '{"section": "../dock"}')
        self.assertFalse(json.loads(out)["ok"])

    def test_upload_list_serve_and_remove(self):
        self.start()
        self.assertTrue(self.media_key)
        r = self.upload(png(), "My Picture.png")
        self.assertEqual(r["status"], 200, r)
        item = json.loads(r["body"])["item"]
        self.assertEqual((item["kind"], item["mime"], item["name"]), ("image", "image/png", "My Picture.png"))
        d = self.media_dir()
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(os.stat(d).st_uid, OWNER)
        status, body = request(OWNER, "GET", self.base + "/api/appearance/media", self.h)
        self.assertEqual([m["id"] for m in json.loads(body)], [item["id"]])
        path = f"/api/appearance/media/{item['id']}"
        # Served to the owner with the media key (for <img>) or the token, never without.
        for headers, q, want in (({}, "", 404), ({}, "?t=wrong", 404), ({}, f"?t={self.media_key}", 200),
                                 ({"X-Forge-Token": self.h["X-Forge-Token"]}, "", 200)):
            out = self.get_raw(path + q, headers)
            self.assertEqual(out["status"], want, (headers, q))
        other = self.get_raw(path + f"?t={self.media_key}", uid=OTHER)
        self.assertTrue(other is None or other["status"] == 403, "served to another user")
        out = self.get_raw(path + f"?t={self.media_key}")
        self.assertEqual(out["body"], png())
        self.assertEqual(out["headers"]["content-type"], "image/png")
        self.assertEqual(out["headers"]["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", out["headers"]["content-security-policy"])
        # Ranges, so a long video can be previewed without sending all of it.
        out = self.get_raw(path + f"?t={self.media_key}", {"Range": "bytes=0-3"})
        self.assertEqual(out["status"], 206)
        self.assertEqual(out["body"], png()[:4])
        status, out = request(OWNER, "POST", self.base + "/api/appearance/media/remove", self.h,
                              json.dumps({"id": item["id"]}))
        self.assertTrue(json.loads(out)["ok"])
        self.assertEqual(self.get_raw(path + f"?t={self.media_key}")["status"], 404)

    def test_svg_is_never_served_inline(self):
        self.start()
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        r = self.upload(svg, "x.svg")
        item = json.loads(r["body"])["item"]
        self.assertEqual(item["format"], "svg")
        out = self.get_raw(f"/api/appearance/media/{item['id']}?t={self.media_key}")
        self.assertNotIn("svg", out["headers"]["content-type"])
        self.assertTrue(out["headers"]["content-disposition"].startswith("attachment"))
        self.assertEqual(out["headers"]["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", out["headers"]["content-security-policy"])

    def test_upload_needs_the_token_and_the_owner(self):
        self.start()
        r = self.upload(png(), headers={"X-Forge-Token": "wrong"})
        self.assertEqual(r["status"], 403)
        r = self.upload(png(), uid=OTHER)
        self.assertTrue(r is None or r["status"] == 403)
        port = self.base.rsplit(":", 1)[1]
        r = self.upload(png(), headers={"Host": "evil.example:" + port})
        self.assertEqual(r["status"], 403)
        self.assertFalse(os.path.exists(self.media_dir()) and os.listdir(self.media_dir()))

    def test_upload_refuses_bad_content(self):
        self.start()
        for data, name in ((b"just some text", "a.png"), (b"\x7fELF" + b"\x00" * 60, "a.mp4"),
                           (b"<html><script>alert(1)</script></html>", "a.svg"), (b"", "a.png")):
            r = self.upload(data, name)
            self.assertIn(r["status"], (200, 400), r)
            self.assertFalse(json.loads(r["body"])["ok"], name)
        self.assertEqual(os.listdir(self.media_dir()), [])
        r = self.upload(png(), headers={"Content-Length": "abc"}, send_body=False)
        self.assertIn(r["status"], (400, 411))
        r = self.upload(png(), headers={"Transfer-Encoding": "chunked", "Content-Length": ""}, send_body=False)
        self.assertIn(r["status"], (400, 411))

    def test_upload_caps(self):
        self.env["FORGE_APPEARANCE_FILE_CAP"] = "1000"
        self.env["FORGE_APPEARANCE_TOTAL_CAP"] = "2000"
        self.start()
        # Refused from the header alone: the body is never read.
        r = self.upload(png(), headers={"Content-Length": str(5 * 1024 ** 3)}, send_body=False)
        self.assertEqual(r["status"], 413)
        big = png() + b"\x00" * 1500
        r = self.upload(big)
        self.assertEqual(r["status"], 413)
        ok = png() + b"\x00" * 800
        self.assertEqual(self.upload(ok)["status"], 200)
        self.assertEqual(self.upload(ok)["status"], 200)
        r = self.upload(ok)
        self.assertEqual(r["status"], 413)
        self.assertIn("full", json.loads(r["body"])["output"])
        self.assertEqual(len([n for n in os.listdir(self.media_dir()) if not n.startswith(".")]), 2)

    def test_media_kind_is_checked_when_used(self):
        self.start()
        anim = json.loads(self.upload(gif(2), "a.gif")["body"])["item"]
        self.assertEqual(anim["kind"], "animated-image")
        vid = json.loads(self.upload(SAMPLES["mp4"][0], "v.mp4")["body"])
        # The demo mp4 has no real video stream: ffprobe (if installed) refuses it, otherwise it is a video.
        if vid["ok"]:
            status, out = request(OWNER, "POST", self.base + "/api/appearance/save", self.h,
                                  json.dumps({"values": {"background": {"image": vid["item"]["id"]}}}))
            self.assertFalse(json.loads(out)["ok"])
        status, out = request(OWNER, "POST", self.base + "/api/appearance/save", self.h,
                              json.dumps({"values": {"background": {"type": "image", "image": anim["id"]}}}))
        self.assertTrue(json.loads(out)["ok"], out)

    def test_preview_scripts_are_separate_owner_only_files(self):
        prev = os.path.join(self.tmp, "previews")
        os.mkdir(prev)
        with open(os.path.join(prev, "dock.js"), "w") as f:
            f.write('const s = "<!--<script>"; forgeAppearance.registerPreview("dock", () => {});\n')
        with open(os.path.join(prev, "evil.js"), "w") as f:
            f.write("alert(1)\n")
        os.symlink("/etc/passwd", os.path.join(prev, "boot.js"))
        self.env["FORGE_APPEARANCE_PREVIEWS"] = prev
        self.start()
        self.assertIn('src="/appearance-preview/dock.js"', self.page)
        self.assertNotIn("registerPreview(\"dock\"", self.page, "preview code must not be pasted into the page")
        self.assertNotIn("evil.js", self.page)
        self.assertNotIn("boot.js", self.page)
        out = self.get_raw("/appearance-preview/dock.js")
        self.assertEqual(out["status"], 200)
        self.assertTrue(out["headers"]["content-type"].startswith("text/javascript"))
        self.assertEqual(out["headers"]["x-content-type-options"], "nosniff")
        for path in ("/appearance-preview/evil.js", "/appearance-preview/boot.js", "/appearance-preview/../x.js",
                     "/appearance-preview/Dock.js"):
            self.assertEqual(self.get_raw(path)["status"], 404, path)
        other = self.get_raw("/appearance-preview/dock.js", uid=OTHER)
        self.assertTrue(other is None or other["status"] == 403)

    def test_damaged_index_is_shown_and_files_are_kept(self):
        self.start()
        item = json.loads(self.upload(png(), "a.png")["body"])["item"]
        idx = os.path.join(self.home, ".local", "share", "forge", "appearance", "media.json")
        with open(idx, "w") as f:
            f.write("{")
        status, body = request(OWNER, "GET", self.base + "/api/appearance", self.h)
        self.assertIn("damaged", json.loads(body)["media_problem"])
        r = self.upload(png(), "b.png")
        self.assertFalse(json.loads(r["body"])["ok"])
        self.assertIn("damaged", json.loads(r["body"])["output"])
        self.assertEqual([n for n in os.listdir(self.media_dir()) if not n.startswith(".")], [item["file"]])

    def test_appearance_endpoints_do_not_exist_in_welcome_or_dashboard_mode(self):
        for mode in ("--welcome", "--dashboard"):
            with self.subTest(mode=mode):
                self.start(mode)
                self.assertEqual(request(OWNER, "GET", self.base + "/api/appearance", self.h)[0], 404)
                r = self.upload(png())
                self.assertIn(r["status"], (400, 404))       # the dashboard refuses any body that is not JSON
                self.assertFalse(os.path.exists(self.media_dir()))
                self.proc.terminate()
                self.proc.wait()
                self.proc.stdout.close()
                self.proc.stderr.close()
                self.proc = None


if __name__ == "__main__":
    unittest.main()


class AdvancedFlagTest(unittest.TestCase):
    """The settings page folds settings marked "advanced": true away. The list stays small and changes no
    key, default, type or group."""
    ADVANCED = {"privacy.dns_strict", "security.usb_key_timeout_sec", "core.port", "core.metrics_interval_s",
                "virt.cpu_weight", "ai.storage_min_free_gb"}

    def test_advanced_settings(self):
        with open(SCHEMA) as f:
            schema = json.load(f)
        flagged = {s["key"] for s in schema["settings"] if "advanced" in s}
        self.assertEqual(flagged, self.ADVANCED)
        for s in schema["settings"]:
            if "advanced" in s:
                self.assertIs(s["advanced"], True, s["key"])
        sys.path.insert(0, LIB)
        os.environ["FORGE_SCHEMA"] = SCHEMA
        import forgecfg
        clean, errors = forgecfg.validate({}, schema)
        self.assertEqual(errors, [])

    def test_every_group_has_a_place_in_the_sidebar(self):
        with open(APP) as f:
            src = f.read()
        with open(SCHEMA) as f:
            groups = [g["id"] for g in json.load(f)["groups"]]
        listed = re.findall(r'groups: \[([^\]]*)\]', src)
        ids = {g.strip().strip('"') for part in listed for g in part.split(",") if g.strip()}
        self.assertEqual(set(groups), ids, "every current group is mapped to a sidebar section")
        self.assertIn('const sectionOf = gid => (SECTIONS.find(s => s.groups.includes(gid)) || SECTIONS[1]).id;', src)
