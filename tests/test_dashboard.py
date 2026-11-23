"""Forge Dashboard (forge-settings --dashboard): the home screen of Forge OS.

Runs the real server as a test-only uid (OWNER), like test_settings_page, so the owner check on
every connection is the real one. A small fake Forge Core runs in this process, and every tool
the dashboard calls (forge-backup, forge-shared-drive, forge-settings-sync, forge-hub, virsh,
lsblk, pkexec, forge-doctor) is a fake on PATH that records its argv.
"""
import hashlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_settings_page import (APP, LIB, OTHER, OWNER, ROOT, SCHEMA, as_uid, refused, request,  # noqa: E402
                                request_headers, token_of)

PAGE = os.path.join(LIB, "dashboard.html")
TEMPERED = os.path.join(ROOT, "files/system/usr/share/forge/tempered.css")
DESKTOP = os.path.join(ROOT, "files/system/usr/share/applications/forge-dashboard.desktop")
FIX_HELPER = os.path.join(ROOT, "files/system/usr/libexec/forge-doctor-fix")
VIEWER = "viewer-token-for-tests-only"
APPROVAL = "0123456789abcdef0123456789abcdef"
OTHER_APPROVAL = "fedcba9876543210fedcba9876543210"
UNKNOWN_APPROVAL = "00000000000000000000000000000000"

NOW = {"ts": 1763550000, "cpu_pct": 23.5, "mem_used_gb": 14.2, "mem_total_gb": 64.0, "gpu_util_pct": 61.0,
       "vram_used_mb": 7300.0, "vram_total_mb": 12227.0, "gpu_temp_c": 64.0, "cpu_temp_c": 58.5,
       "disk_root_pct": 41.0, "load1": 1.2, "swap_used_gb": 0.0, "gpu_power_w": 140.0,
       "battery_pct": None, "on_ac": None}
UPDATES = {"ok": True, "message": "Update ready, restart to use it.", "update_ready": True,
           "rollback_available": True, "booted_is_rollback": False,
           "booted": {"version": "45.20261110.0", "timestamp": 1762800000, "image": "ghcr.io/x/forge-os:latest"},
           "staged": {"version": "45.20261118.0", "timestamp": 1763450000, "image": "ghcr.io/x/forge-os:latest"},
           "rollback": {"version": "45.20261101.0", "timestamp": 1762000000, "image": "ghcr.io/x/forge-os:latest"},
           "last_attempt": {"updater": "uupd", "result": "success", "ts": 1763450000, "detail": "ok"},
           "recovery": "If an update causes problems: ..."}
TIMELINE = {"entries": [{"ts": 1763549000, "kind": "mode", "title": "owner switched to gaming mode", "detail": ""},
                        {"ts": 1763540000, "kind": "agent", "title": "Agent system-checkup: done",
                         "detail": "<img src=x onerror=alert(1)>"}],
            "sources": {"activity": "ok"}}
AGENTS = [{"id": "system-checkup", "name": "System checkup", "enabled": True, "model": "qwen3:8b"}]
RUNS = [{"id": "r1", "agent": "system-checkup", "task": "Is anything wrong?", "status": "done",
         "result": "All fine.", "error": None, "started": 1763540000, "ended": 1763540100, "steps": []}]
APPROVALS = [{"id": APPROVAL, "agent": "system-checkup", "action": "settings.change",
              "details": {"key": "ai.keep_alive_min"}, "status": "pending", "created": 1763549900,
              "expires": 1763550800},
             {"id": OTHER_APPROVAL, "agent": "system-checkup", "action": "web.fetch",
              "details": {"url": "https://example.org"}, "status": "pending", "created": 1763549950,
              "expires": 1763550850}]

DOCTOR = {"created": "2026-11-19 10:00", "summary": {"ok": 6, "warn": 1, "unknown": 1, "info": 0},
          "sections": [{"name": "AI readiness", "status": "warn", "items": ["Ollama is not running."]}],
          "fixes": [{"id": "restart-ollama", "title": "Start Ollama again", "problem": "Ollama is not running.",
                     "explanation": "Restarts the Ollama service.", "commands": [["systemctl", "restart",
                                                                                   "ollama.service"]],
                     "needs_root": True, "reversible": True, "risk": "low", "automatic": True},
                    {"id": "image-mismatch", "title": "Switch image", "problem": "x", "explanation": "y",
                     "commands": [["rpm-ostree", "rebase", "z"]], "needs_root": True, "reversible": True,
                     "risk": "medium", "automatic": False}]}
BACKUP = {"set_up": True, "automatic": True, "every_hours": 24, "drive_label": "FORGEBACKUP",
          "last_success": 1763500000, "last_run": 1763500000, "result": "ok", "snapshot_count": 12,
          "size_bytes": 52000000000, "drive_free_bytes": 400000000000, "overdue": False}
SHARED = {"enabled": True, "label": "SHARED", "state": "mounted", "device": "/dev/nvme1n1p2", "fstype": "ntfs",
          "mountpoint": "/run/media/owner/SHARED", "link": "ok", "message": "Shared drive is open."}
SYNC = {"this_pc": "desk", "enabled": True, "message": None,
        "hosts": [{"host": "laptop", "saved_at": "2026-11-18T10:00:00Z", "differences": 2, "newer": True,
                   "problem": None}]}
HUB = {"set_up": True, "hub_built": True, "commit": "a" * 40, "hub_running": True, "pending_approvals": 0,
       "claude_entry_present": True, "apps": [{"id": "blender", "status": "connected", "lastError": None}]}
LSBLK = {"blockdevices": [{"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 1000204886016, "type": "disk",
                           "fstype": None, "label": None, "mountpoint": None, "fssize": None, "fsused": None,
                           "fsavail": None, "rm": False, "tran": "nvme", "model": "Samsung 990",
                           "children": [{"name": "nvme0n1p3", "path": "/dev/nvme0n1p3", "size": 900000000000,
                                         "type": "part", "fstype": "btrfs", "label": "fedora",
                                         "mountpoint": "/sysroot", "fssize": 900000000000,
                                         "fsused": 300000000000, "fsavail": 600000000000, "rm": False,
                                         "tran": None, "model": None}]}]}
# `rpm-ostree status --json`, read fresh right before a rollback (booted from the default entry).
IMAGE_REF = "ostree-image-signed:docker://ghcr.io/x/forge-os:latest"
RPM_STATUS = {"deployments": [
    {"booted": True, "staged": False, "version": "45.20261110.0", "timestamp": 1762800000,
     "container-image-reference": IMAGE_REF, "checksum": "a" * 64},
    {"booted": False, "staged": False, "version": "45.20261101.0", "timestamp": 1762000000,
     "container-image-reference": IMAGE_REF, "checksum": "b" * 64}]}
RPM_STAGED = {"deployments": [dict(RPM_STATUS["deployments"][0], booted=False, staged=True, version="45.20261118.0",
                                   checksum="c" * 64)] + RPM_STATUS["deployments"]}
# `forge-settings-sync diff laptop`, with a hidden right-to-left mark in a value from the other PC.
SYNC_DIFF = ("Settings from laptop (forge-os), saved 2026-11-18T10:00:00Z:\n"
             "  ai.keep_alive_min (Keep models loaded): 5 -> 30\n"
             "  modes.default (Mode at login): default -> gam\u202eing\n"
             "Use them here with: forge-settings-sync apply laptop\n")
VIRSH = (" Id   Name          State\n"
         "------------------------------\n"
         " 1    win11         running\n"
         " -    fedora test   shut off\n\n")


def forgedash_module():
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    os.environ.setdefault("FORGE_SCHEMA", SCHEMA)
    import forgedash
    return forgedash


class FakeCore(http.server.ThreadingHTTPServer):
    daemon_threads = True


class CoreHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + VIEWER:
            return self.reply(401, {"detail": "no"})
        path = self.path.split("?")[0]
        if path == "/api/doctor":
            return self.reply(403, {"detail": "The viewer token can't read this."})
        if path == "/api/metrics/history":
            return self.reply(200, [{"ts": NOW["ts"] - 30, "avg": 20.0, "max": 25.0},
                                    {"ts": NOW["ts"] - 15, "avg": 22.0, "max": 24.0}])
        data = {"/api/metrics/now": NOW, "/api/system/updates": UPDATES, "/api/timeline": TIMELINE,
                "/api/agents": AGENTS, "/api/runs": RUNS, "/api/approval-count": {"pending": 1}}.get(path)
        if data is None:
            return self.reply(404, {"detail": "Not Found"})
        self.reply(200, data)

    def reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


FAKE = r"""#!/usr/bin/env python3
import json, os, sys
LOG, NAME, OUT = %r, %r, %r
with open(LOG, "a") as f:
    f.write(json.dumps([NAME] + sys.argv[1:]) + "\n")
key = " ".join(sys.argv[1:])
entry = OUT.get(key, OUT.get("*", ["", 0]))
text, code = entry[0], entry[1]
if len(entry) > 2:   # seconds to wait, like a password prompt
    import time
    time.sleep(entry[2])
sys.stdout.write(text)
sys.exit(code)
"""


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the server and clients as other uids")
class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o777)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        os.chmod(self.bin, 0o755)
        approvals = json.dumps(APPROVALS)
        self.fakes = {
            "forge-backup": {"status --json": [json.dumps(BACKUP), 0], "run": ["Backed up.\n", 0]},
            "forge-shared-drive": {"status --json": [json.dumps(SHARED), 0], "": ["", 0]},
            "forge-settings-sync": {"status --json": [json.dumps(SYNC), 0], "apply laptop": ["Applied 2.\n", 0],
                                    "diff laptop": [SYNC_DIFF, 0]},
            "forge-hub": {"status --json": [json.dumps(HUB), 0], "dashboard": ["Opened.\n", 0]},
            "forge-doctor": {"--json": [json.dumps(DOCTOR), 0], "": ["Forge OS doctor, report text\n", 0]},
            "virsh": {"-c qemu:///system list --all": [VIRSH, 0]},
            "lsblk": {"*": [json.dumps(LSBLK), 0]},
            "pkexec": {"/usr/bin/forge-api GET /api/approvals": [approvals, 0], "*": ["done\n", 0]},
            "forge-settings": {"*": ["", 0]},
            "rpm-ostree": {"status --json": [json.dumps(RPM_STATUS), 0]},
        }
        self.write_fakes()
        with open(os.path.join(self.tmp, "viewer.token"), "w") as f:
            f.write(VIEWER + "\n")
        os.chmod(os.path.join(self.tmp, "viewer.token"), 0o644)
        self.core = FakeCore(("127.0.0.1", 0), CoreHandler)
        self.core.seen = []
        threading.Thread(target=self.core.serve_forever, daemon=True).start()
        self.conf = os.path.join(self.tmp, "settings.json")
        self.settings({"core.port": self.core.server_address[1], "virt.enabled": True})
        state = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(state, "forge"))
        with open(os.path.join(state, "forge", "mode"), "w") as f:
            f.write("creative\n")
        for d in (state, os.path.join(state, "forge")):
            os.chmod(d, 0o755)
        self.env = dict(os.environ, FORGE_LIB=LIB, FORGE_SCHEMA=SCHEMA, FORGE_CONF=self.conf,
                        FORGE_NO_BROWSER="1", FORGE_PORT="0", XDG_STATE_HOME=state,
                        FORGE_DASH_TEST="1", FORGE_DASH_CORE_UID="0",
                        FORGE_VIEWER_TOKEN=os.path.join(self.tmp, "viewer.token"), FORGE_TEMPERED_CSS=TEMPERED,
                        PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        self.proc = None

    def tearDown(self):
        self.core.shutdown()
        self.core.server_close()
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc.stdout.close()
            self.proc.stderr.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settings(self, values):
        with open(self.conf, "w") as f:
            json.dump(values, f)
        os.chmod(self.conf, 0o644)

    def write_fakes(self):
        for name, out in self.fakes.items():
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(FAKE % (self.log, name, out))
            os.chmod(p, 0o755)

    def calls(self, name=None):
        try:
            with open(self.log) as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []
        return [r for r in rows if name is None or r[0] == name]

    READS = (["forge-backup", "status", "--json"], ["forge-shared-drive", "status", "--json"],
             ["forge-settings-sync", "status", "--json"], ["forge-hub", "status", "--json"],
             ["forge-doctor", "--json"], ["forge-doctor"], ["virsh", "-c", "qemu:///system", "list", "--all"],
             ["pkexec", "/usr/bin/forge-api", "GET", "/api/approvals"], ["rpm-ostree", "status", "--json"],
             ["forge-settings-sync", "diff", "laptop"])

    def actions(self):
        """Calls that change or open something (everything except the read-only status and list calls)."""
        return [c for c in self.calls() if c[0] != "lsblk" and c not in self.READS]

    def staged(self, ready):
        """Make the fake Forge Core report an update as staged or not."""
        saved = dict(UPDATES)
        if not ready:
            UPDATES.update(update_ready=False, staged=None)
        self.addCleanup(lambda: (UPDATES.clear(), UPDATES.update(saved)))

    def wait_for(self, call):
        deadline = time.time() + 5
        while time.time() < deadline and call not in self.calls():
            time.sleep(0.1)

    def start(self):
        import select
        self.proc = subprocess.Popen([sys.executable, "-u", APP, "--dashboard"], env=self.env,
                                     preexec_fn=as_uid(OWNER), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        self.assertTrue(ready, "forge-settings --dashboard printed no URL")
        line = self.proc.stdout.readline()
        m = re.search(r"(http://127\.0\.0\.1:\d+)/\?k=(\S+)", line)
        self.assertTrue(m, "no URL in: " + line)
        time.sleep(0.2)
        return m.group(1), m.group(2)

    def open(self):
        base, k = self.start()
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        self.assertEqual(status, 200, body)
        self.page = body
        return base, {"X-Forge-Token": token_of(body), "Content-Type": "application/json"}

    def get(self, base, h, path):
        status, body = request(OWNER, "GET", base + path, h)
        self.assertEqual(status, 200, path + ": " + body)
        return json.loads(body)

    def sync_diff(self, base, h, host):
        status, out = request(OWNER, "POST", base + "/api/dash/sync-diff", h, json.dumps({"host": host}))
        self.assertEqual(status, 200, out)
        return json.loads(out)

    def act(self, base, h, **body):
        status, out = request(OWNER, "POST", base + "/api/dash/action", h, json.dumps(body))
        self.assertEqual(status, 200, out)
        return json.loads(out)

    # The page itself

    def test_page_served_once_with_launch_key_and_strict_csp(self):
        base, k = self.start()
        self.assertEqual(request(OWNER, "GET", f"{base}/")[0], 403)
        out = request_headers(OWNER, "GET", f"{base}/?k={k}")
        self.assertEqual(out["status"], 200)
        self.assertIn("Forge Dashboard", out["body"])
        self.assertTrue(token_of(out["body"]))
        h = {key.lower(): v for key, v in out["headers"].items()}
        self.assertEqual(h.get("x-frame-options"), "DENY")
        csp = h.get("content-security-policy", "")
        for part in ("default-src 'none'", "frame-ancestors 'none'", "connect-src 'self'", "base-uri 'none'",
                     "form-action 'none'"):
            self.assertIn(part, csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("http", csp.replace("'self'", ""))
        nonce = re.search(r"'nonce-([A-Za-z0-9_-]{16,})'", csp).group(1)
        self.assertIn(f'<script nonce="{nonce}">', out["body"])
        self.assertIn(f'<style nonce="{nonce}">', out["body"])
        self.assertEqual(request(OWNER, "GET", f"{base}/?k={k}")[0], 403)

    def test_other_user_is_refused_everywhere(self):
        base, k = self.start()
        self.assertTrue(refused(OTHER, "GET", f"{base}/?k={k}"))
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        h = {"X-Forge-Token": token_of(body), "Content-Type": "application/json"}
        for path in ("/api/dash/home", "/api/dash/metrics", "/api/dash/doctor", "/api/dash/doctor.txt",
                     "/api/dash/agents", "/api/dash/backup"):
            self.assertTrue(refused(OTHER, "GET", base + path, h), path)
        for body in ({"action": "backup-now", "confirmed": True},
                     {"action": "doctor-fix", "id": "restart-ollama", "confirmed": True}):
            self.assertTrue(refused(OTHER, "POST", base + "/api/dash/action", h, json.dumps(body)))
        self.assertTrue(refused(OTHER, "POST", base + "/api/dash/approvals", h, "{}"))
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.calls("pkexec"), [])

    def test_endpoints_need_the_token(self):
        base, h = self.open()
        for path in ("/api/dash/home", "/api/dash/metrics", "/api/dash/history", "/api/dash/agents",
                     "/api/dash/timeline", "/api/dash/updates", "/api/dash/backup", "/api/dash/storage",
                     "/api/dash/sync", "/api/dash/vms", "/api/dash/hub", "/api/dash/doctor",
                     "/api/dash/doctor.txt"):
            self.assertIn(request(OWNER, "GET", base + path)[0], (403, 404), path)
            self.assertIn(request(OWNER, "GET", base + path, {"X-Forge-Token": "wrong"})[0], (403, 404), path)
        status, _ = request(OWNER, "POST", base + "/api/dash/action", {"X-Forge-Token": "wrong"},
                            '{"action": "backup-now", "confirmed": true}')
        self.assertEqual(status, 403)
        self.assertEqual(self.actions(), [])

    def test_settings_endpoints_are_closed_in_dashboard_mode(self):
        base, h = self.open()
        self.assertEqual(request(OWNER, "GET", base + "/api/settings", h)[0], 404)
        self.assertEqual(request(OWNER, "GET", base + "/api/export", h)[0], 404)
        for path in ("/api/save", "/api/undo", "/api/import-check"):
            self.assertEqual(request(OWNER, "POST", base + path, h, '{"values": {}}')[0], 404, path)

    # Data endpoints

    def test_home(self):
        base, h = self.open()
        d = self.get(base, h, "/api/dash/home")
        self.assertEqual(d["metrics"]["cpu_pct"], 23.5)
        self.assertEqual(d["mode"], "creative")
        self.assertEqual(d["approvals"], 1)
        self.assertEqual(d["backup"]["last_success"], 1763500000)
        self.assertEqual(d["updates"]["message"], "Update ready, restart to use it.")
        self.assertEqual(d["timeline"][0]["kind"], "mode")
        self.assertTrue(d["host"])
        # Forge Core got the viewer token, and nothing else asked for it.
        self.assertTrue(self.core.seen)
        self.assertTrue(all(auth == "Bearer " + VIEWER for _, auth in self.core.seen))
        # The token never reaches the page.
        self.assertNotIn(VIEWER, self.page)
        self.assertNotIn(VIEWER, json.dumps(d))

    def test_odd_mode_file_is_not_passed_on(self):
        with open(os.path.join(self.env["XDG_STATE_HOME"], "forge", "mode"), "w") as f:
            f.write("<b>evil</b>\n")
        base, h = self.open()
        self.assertEqual(self.get(base, h, "/api/dash/home")["mode"], "default")

    def test_metrics_and_history(self):
        base, h = self.open()
        self.assertEqual(self.get(base, h, "/api/dash/metrics")["metrics"]["gpu_temp_c"], 64.0)
        d = self.get(base, h, "/api/dash/history")
        for m in ("cpu_pct", "gpu_util_pct", "vram_used_mb", "mem_used_gb", "gpu_temp_c"):
            self.assertEqual(d["history"][m][-1]["avg"], 22.0, m)
        asked = [p for p, _ in self.core.seen if p.startswith("/api/metrics/history")]
        self.assertTrue(all(re.fullmatch(r"/api/metrics/history\?metric=[a-z_]+&since=\d+&step=\d+", p)
                            for p in asked), asked)

    def test_agents_timeline_updates(self):
        base, h = self.open()
        d = self.get(base, h, "/api/dash/agents")
        self.assertEqual(d["agents"][0]["id"], "system-checkup")
        self.assertEqual(d["runs"][0]["status"], "done")
        self.assertEqual(d["approvals"], 1)
        d = self.get(base, h, "/api/dash/timeline")
        self.assertEqual([e["kind"] for e in d["entries"]], ["mode", "agent"])
        self.assertEqual(d["entries"][1]["detail"], "<img src=x onerror=alert(1)>")   # shown as text by the page
        d = self.get(base, h, "/api/dash/updates")
        self.assertEqual(d["updates"]["booted"]["version"], "45.20261110.0")
        self.assertEqual(d["updates"]["staged"]["version"], "45.20261118.0")
        self.assertFalse(d["rollback_button"], "rollback is offered while an update is staged")

    def test_local_tools(self):
        base, h = self.open()
        self.assertEqual(self.get(base, h, "/api/dash/backup")["backup"]["snapshot_count"], 12)
        d = self.get(base, h, "/api/dash/storage")
        self.assertEqual(d["shared"]["state"], "mounted")
        self.assertEqual(d["disks"][0]["name"], "nvme0n1")
        self.assertEqual(d["disks"][0]["children"][0]["fsavail"], 600000000000)
        self.assertEqual(self.get(base, h, "/api/dash/sync")["sync"]["hosts"][0]["host"], "laptop")
        self.assertEqual(self.get(base, h, "/api/dash/hub")["hub"]["apps"][0]["id"], "blender")
        d = self.get(base, h, "/api/dash/vms")
        self.assertTrue(d["enabled"])
        self.assertEqual(d["vms"], [{"id": "1", "name": "win11", "state": "running"},
                                    {"id": "-", "name": "fedora test", "state": "shut off"}])
        reads = [c for c in self.calls() if c[0] != "lsblk"]
        self.assertIn(["forge-backup", "status", "--json"], reads)
        self.assertIn(["forge-shared-drive", "status", "--json"], reads)
        self.assertIn(["forge-settings-sync", "status", "--json"], reads)
        self.assertIn(["forge-hub", "status", "--json"], reads)
        self.assertIn(["virsh", "-c", "qemu:///system", "list", "--all"], reads)
        self.assertEqual(self.actions(), [])

    def test_vms_not_asked_when_virtualization_is_off(self):
        self.settings({"core.port": self.core.server_address[1]})
        base, h = self.open()
        self.assertFalse(self.get(base, h, "/api/dash/vms")["enabled"])
        self.assertEqual(self.calls("virsh"), [])

    def test_doctor_falls_back_to_the_local_report(self):
        base, h = self.open()
        d = self.get(base, h, "/api/dash/doctor")
        self.assertEqual(d["report"]["summary"]["warn"], 1)
        self.assertEqual(d["source"], "local")
        self.assertIn("/api/doctor", [p for p, _ in self.core.seen])
        self.assertIn(["forge-doctor", "--json"], self.calls())
        out = request_headers(OWNER, "GET", base + "/api/dash/doctor.txt", h)
        self.assertEqual(out["status"], 200)
        hd = {k.lower(): v for k, v in out["headers"].items()}
        self.assertTrue(hd["content-type"].startswith("text/plain"))
        self.assertRegex(hd["content-disposition"], r'^attachment; filename="forge-doctor-\d{8}-\d{6}\.txt"$')
        self.assertIn("report text", out["body"])

    def test_token_is_not_sent_to_a_program_that_is_not_forge_core(self):
        self.env["FORGE_DASH_CORE_UID"] = "12345"     # Forge Core's user is someone else
        base, h = self.open()
        d = self.get(base, h, "/api/dash/home")
        self.assertIsNone(d["metrics"])
        self.assertIn("Forge Core", d["error"])
        self.assertEqual(self.core.seen, [])

    def test_test_seams_are_ignored_without_the_test_switch(self):
        del self.env["FORGE_DASH_TEST"]
        base, h = self.open()
        self.get(base, h, "/api/dash/home")
        self.assertEqual(self.core.seen, [])

    # Actions: exact argv, confirmation, fixed ids

    def test_each_action_runs_its_exact_command(self):
        self.staged(False)
        base, h = self.open()
        self.assertTrue(self.act(base, h, action="backup-now", confirmed=True)["ok"])
        self.assertTrue(self.act(base, h, action="open-shared-drive", confirmed=True)["ok"])
        shown = self.sync_diff(base, h, "laptop")
        self.assertTrue(self.act(base, h, action="sync-apply", host="laptop", diff_hash=shown["hash"],
                                 confirmed=True)["ok"])
        self.assertTrue(self.act(base, h, action="doctor-fix", id="restart-ollama", confirmed=True)["ok"])
        self.assertTrue(self.act(base, h, action="hub-dashboard", confirmed=True)["ok"])
        self.assertTrue(self.act(base, h, action="open-settings")["ok"])
        status, out = request(OWNER, "POST", base + "/api/dash/approvals", h, "{}")
        self.assertEqual([a["id"] for a in json.loads(out)["approvals"]], [APPROVAL, OTHER_APPROVAL])
        self.assertTrue(self.act(base, h, action="approve", id=APPROVAL, confirmed=True)["ok"])
        self.assertTrue(self.act(base, h, action="deny", id=OTHER_APPROVAL, confirmed=True)["ok"])
        # An answered approval is gone from the list.
        self.assertFalse(self.act(base, h, action="deny", id=APPROVAL, confirmed=True)["ok"])
        self.assertTrue(self.act(base, h, action="rollback", typed="rollback", confirmed=True)["ok"])
        self.wait_for(["forge-backup", "run"])
        self.wait_for(["forge-settings"])
        self.assertCountEqual(self.actions(), [
            ["forge-backup", "run"],
            ["forge-shared-drive"],
            ["forge-settings-sync", "apply", "laptop"],
            ["pkexec", "/usr/libexec/forge-doctor-fix", "restart-ollama"],
            ["forge-hub", "dashboard"],
            ["forge-settings"],
            ["pkexec", "/usr/bin/forge-api", "POST", f"/api/approvals/{APPROVAL}/approve"],
            ["pkexec", "/usr/bin/forge-api", "POST", f"/api/approvals/{OTHER_APPROVAL}/deny"],
            ["pkexec", "/usr/bin/rpm-ostree", "rollback"],
        ])

    def test_every_action_needs_confirmation(self):
        base, h = self.open()
        request(OWNER, "POST", base + "/api/dash/approvals", h, "{}")
        for body in ({"action": "backup-now"}, {"action": "open-shared-drive", "confirmed": False},
                     {"action": "sync-apply", "host": "laptop", "confirmed": "yes"},
                     {"action": "doctor-fix", "id": "restart-ollama", "confirmed": 1},
                     {"action": "hub-dashboard"}, {"action": "approve", "id": APPROVAL},
                     {"action": "deny", "id": APPROVAL, "confirmed": None},
                     {"action": "rollback", "typed": "rollback"}):
            d = self.act(base, h, **body)
            self.assertFalse(d["ok"], body)
            self.assertIn("confirm", d["output"].lower(), body)
        time.sleep(0.3)
        self.assertEqual(self.actions(), [])

    def test_unknown_actions_ids_and_hosts_are_refused(self):
        self.staged(False)
        base, h = self.open()
        bad = [
            {"action": "rm", "confirmed": True},
            {"action": ["backup-now"], "confirmed": True},
            {"action": "doctor-fix", "id": "run-hwcheck", "confirmed": True},      # known, but not found now
            {"action": "doctor-fix", "id": "image-mismatch", "confirmed": True},   # shown only, never run
            {"action": "doctor-fix", "id": "restart-ollama; reboot", "confirmed": True},
            {"action": "doctor-fix", "id": "--help", "confirmed": True},
            {"action": "doctor-fix", "id": ["restart-ollama"], "confirmed": True},
            {"action": "sync-apply", "host": "evil", "confirmed": True},
            {"action": "sync-apply", "host": "laptop --merge", "confirmed": True},
            {"action": "sync-apply", "host": "-h", "confirmed": True},
            {"action": "sync-apply", "confirmed": True},
            {"action": "approve", "id": APPROVAL, "confirmed": True},              # list not loaded yet
            {"action": "rollback", "typed": "yes", "confirmed": True},
            {"action": "rollback", "typed": "rollback now", "confirmed": True},
            {"action": "rollback", "confirmed": True},
        ]
        for body in bad:
            self.assertFalse(self.act(base, h, **body)["ok"], body)
        request(OWNER, "POST", base + "/api/dash/approvals", h, "{}")
        for aid in (UNKNOWN_APPROVAL, APPROVAL.upper(), APPROVAL + "/../../settings", "x" * 32, "", None, 7):
            self.assertFalse(self.act(base, h, action="approve", id=aid, confirmed=True)["ok"], aid)
        # A bad body is refused, not crashed on.
        for raw in ("[1]", "not json", '{"action": "backup-now", "confirmed": true' + " " * 70000 + "}"):
            status, _ = request(OWNER, "POST", base + "/api/dash/action", h, raw)
            self.assertIn(status, (400, 413), raw[:20])
        time.sleep(0.3)
        self.assertEqual(self.actions(), [])

    def test_rollback_is_refused_while_an_update_is_staged(self):
        self.fakes["rpm-ostree"]["status --json"] = [json.dumps(RPM_STAGED), 0]
        self.write_fakes()
        base, h = self.open()
        d = self.act(base, h, action="rollback", typed="rollback", confirmed=True)
        self.assertFalse(d["ok"])
        self.assertIn("rpm-ostree rollback", d["output"])
        self.assertEqual(self.calls("pkexec"), [])

    def test_extra_fields_never_reach_a_command(self):
        base, h = self.open()
        self.act(base, h, action="backup-now", confirmed=True, args=["--evil"], id="--evil", host="--evil",
                 typed="--evil", argv=["sh", "-c", "id"])
        self.act(base, h, action="hub-dashboard", confirmed=True, id="remove", host="remove")
        deadline = time.time() + 5
        while time.time() < deadline and ["forge-backup", "run"] not in self.calls():
            time.sleep(0.1)
        self.assertEqual(self.actions(), [["forge-backup", "run"], ["forge-hub", "dashboard"]])

    def test_open_dashboard_from_settings(self):
        import select
        self.proc = subprocess.Popen([sys.executable, "-u", APP], env=self.env, preexec_fn=as_uid(OWNER),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        select.select([self.proc.stdout], [], [], 10)
        m = re.search(r"(http://127\.0\.0\.1:\d+)/\?k=(\S+)", self.proc.stdout.readline())
        base, k = m.group(1), m.group(2)
        time.sleep(0.2)
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        self.assertIn("Open Forge Dashboard", body)
        h = {"X-Forge-Token": token_of(body), "Content-Type": "application/json"}
        self.assertEqual(request(OWNER, "POST", base + "/api/open-dashboard", {}, "{}")[0], 403)
        self.assertTrue(refused(OTHER, "POST", base + "/api/open-dashboard", h, "{}"))
        status, out = request(OWNER, "POST", base + "/api/open-dashboard", h, '{"args": ["--welcome"]}')
        self.assertTrue(json.loads(out)["ok"])
        deadline = time.time() + 5
        while time.time() < deadline and not self.calls("forge-settings"):
            time.sleep(0.1)
        self.assertEqual(self.calls("forge-settings"), [["forge-settings", "--dashboard"]])
        # The dashboard endpoints don't exist on the Settings page.
        self.assertEqual(request(OWNER, "GET", base + "/api/dash/home", h)[0], 404)
        self.assertEqual(request(OWNER, "POST", base + "/api/dash/action", h,
                                 '{"action": "backup-now", "confirmed": true}')[0], 404)

    def test_cancelled_password_is_reported(self):
        self.fakes["pkexec"]["*"] = ["", 126]
        self.write_fakes()
        base, h = self.open()
        d = self.act(base, h, action="doctor-fix", id="restart-ollama", confirmed=True)
        self.assertFalse(d["ok"])
        self.assertIn("password", d["output"].lower())

    # Review fixes

    def test_rollback_checks_the_system_fresh_right_before_the_password(self):
        # Forge Core's cached answer (up to 60 s old) says nothing is staged, but an update was staged since.
        self.staged(False)
        self.fakes["rpm-ostree"]["status --json"] = [json.dumps(RPM_STAGED), 0]
        self.write_fakes()
        base, h = self.open()
        d = self.act(base, h, action="rollback", typed="rollback", confirmed=True)
        self.assertFalse(d["ok"])
        self.assertIn("rpm-ostree rollback", d["output"])
        self.assertIn(["rpm-ostree", "status", "--json"], self.calls())
        self.assertEqual(self.calls("pkexec"), [])

    def test_only_one_rollback_and_never_a_second_one(self):
        self.staged(False)
        self.fakes["pkexec"]["/usr/bin/rpm-ostree rollback"] = ["done\n", 0, 2]   # the password prompt is up
        self.write_fakes()
        base, h = self.open()
        res = []
        th = [threading.Thread(target=lambda: res.append(
            self.act(base, h, action="rollback", typed="rollback", confirmed=True))) for _ in range(2)]
        for t in th:
            t.start()
            time.sleep(0.3)
        for t in th:
            t.join()
        self.assertEqual(sorted(r["ok"] for r in res), [False, True], res)
        self.assertIn("already", next(r for r in res if not r["ok"])["output"])
        # After it ran, a second one would switch back: refused, and the page says why.
        d = self.act(base, h, action="rollback", typed="rollback", confirmed=True)
        self.assertFalse(d["ok"])
        self.assertIn("already", d["output"])
        self.assertIn("switch back", d["output"])
        self.assertEqual(self.calls("pkexec"), [["pkexec", "/usr/bin/rpm-ostree", "rollback"]])

    def test_cancelled_rollback_can_be_tried_again(self):
        self.staged(False)
        self.fakes["pkexec"]["*"] = ["", 126]
        self.write_fakes()
        base, h = self.open()
        self.assertIn("password", self.act(base, h, action="rollback", typed="rollback", confirmed=True)["output"])
        self.assertIn("password", self.act(base, h, action="rollback", typed="rollback", confirmed=True)["output"])
        self.assertEqual(len(self.calls("pkexec")), 2)

    def test_sync_apply_shows_each_change_and_applies_only_what_was_shown(self):
        base, h = self.open()
        shown = self.sync_diff(base, h, "laptop")
        self.assertTrue(shown["ok"], shown)
        self.assertIn("ai.keep_alive_min (Keep models loaded): 5 -> 30", shown["text"])
        self.assertIn("modes.default (Mode at login): default -> gaming", shown["text"])
        self.assertEqual(shown["hash"], hashlib.sha256(shown["text"].encode()).hexdigest())
        # Only PCs from the tool's own list, checked like apply.
        for host in ("evil", "-h", "laptop --merge", None):
            self.assertFalse(self.sync_diff(base, h, host)["ok"], host)
        # No hash, or not the one shown: nothing is applied.
        for extra in ({}, {"diff_hash": "0" * 64}, {"diff_hash": ["x"]}, {"diff_hash": shown["hash"].upper()}):
            self.assertFalse(self.act(base, h, action="sync-apply", host="laptop", confirmed=True, **extra)["ok"],
                             extra)
        # The other PC's file changed after the list was shown.
        self.fakes["forge-settings-sync"]["diff laptop"] = [SYNC_DIFF.replace("5 -> 30", "5 -> 31"), 0]
        self.write_fakes()
        d = self.act(base, h, action="sync-apply", host="laptop", diff_hash=shown["hash"], confirmed=True)
        self.assertFalse(d["ok"])
        self.assertIn("changed", d["output"])
        self.assertNotIn(["forge-settings-sync", "apply", "laptop"], self.calls())
        # As shown: applied, right after the check.
        self.fakes["forge-settings-sync"]["diff laptop"] = [SYNC_DIFF, 0]
        self.write_fakes()
        d = self.act(base, h, action="sync-apply", host="laptop", diff_hash=shown["hash"], confirmed=True)
        self.assertTrue(d["ok"], d)
        self.assertEqual(self.calls("forge-settings-sync")[-2:], [["forge-settings-sync", "diff", "laptop"],
                                                                   ["forge-settings-sync", "apply", "laptop"]])

    def test_bidi_and_format_characters_are_removed_from_all_data(self):
        hidden = "\u202e\u2066\u200f\u200b\ufeff\x1b\x07\r"
        approvals = [dict(APPROVALS[0], agent="system" + hidden + "-checkup", action="settings" + hidden + ".change",
                          details={"key" + hidden: "ai.keep" + hidden + "_alive_min", "why": ["a\u202eb", "line 1\nline 2"]})]
        self.fakes["pkexec"]["/usr/bin/forge-api GET /api/approvals"] = [json.dumps(approvals), 0]
        self.fakes["forge-backup"]["status --json"] = [json.dumps(dict(BACKUP, message="Backup" + hidden + " ok")), 0]
        self.write_fakes()
        saved = json.loads(json.dumps(TIMELINE))
        TIMELINE["entries"][0]["title"] = "owner switched to \u202egaming\u202c mode"
        self.addCleanup(lambda: (TIMELINE.clear(), TIMELINE.update(saved)))
        base, h = self.open()

        def bad(value):
            return sorted({c for text in _strings(value) for c in text
                           if unicodedata.category(c) in ("Cc", "Cf") and c not in "\t\n"})

        status, out = request(OWNER, "POST", base + "/api/dash/approvals", h, "{}")
        d = json.loads(out)
        self.assertTrue(d["ok"], d)
        a = d["approvals"][0]
        self.assertEqual(a["agent"], "system-checkup")
        self.assertEqual(a["action"], "settings.change")
        self.assertEqual(a["details"], {"key": "ai.keep_alive_min", "why": ["ab", "line 1\nline 2"]})
        self.assertEqual(bad(d), [])
        home = self.get(base, h, "/api/dash/home")
        self.assertEqual(home["timeline"][0]["title"], "owner switched to gaming mode")
        self.assertEqual(home["backup"]["message"], "Backup ok")
        self.assertEqual(bad(home), [])
        self.assertEqual(bad(self.get(base, h, "/api/dash/backup")), [])


def _strings(value):
    """Every string in a JSON value, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


class SettingsAndWelcomeLinkTest(unittest.TestCase):
    """The Settings and Welcome pages have a button that starts the dashboard (no root needed)."""

    def test_pages_have_the_dashboard_button(self):
        with open(APP) as f:
            src = f.read()
        self.assertEqual(src.count('fetch("/api/open-dashboard"'), 2)

    def test_desktop_entry(self):
        with open(DESKTOP) as f:
            text = f.read()
        self.assertIn("Name=Forge Dashboard\n", text)
        self.assertIn("Exec=forge-settings --dashboard\n", text)


class PageStaticTest(unittest.TestCase):
    """The page's own code, checked without a browser."""

    @classmethod
    def setUpClass(cls):
        with open(PAGE) as f:
            cls.page = f.read()
        cls.script = re.search(r"<script nonce=\"__NONCE__\">(.*?)</script>", cls.page, re.S).group(1)

    def test_no_html_from_data(self):
        # Untrusted text goes in with textContent only.
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                    "setAttribute(\"on", "srcdoc", "javascript:"):
            self.assertNotIn(bad, self.page, bad)
        self.assertIn("textContent", self.script)

    def test_offline_only(self):
        page = self.page.replace('"http://www.w3.org/2000/svg"', "")   # the SVG namespace name, never fetched
        self.assertNotRegex(page, r"(?i)(https?:)?//[a-z0-9.-]+\.[a-z]{2,}/")   # no CDN, fonts or scripts
        self.assertNotIn("@import", self.page)
        self.assertNotRegex(self.page, r"<link\b")
        self.assertNotRegex(self.page, r"\sstyle=\"")    # inline style attributes would break the CSP

    def test_matches_forge_settings_look(self):
        # The colours come from the shared tempered.css, put in when the page is served (forgedash.page_template).
        self.assertIn("/*__TEMPERED__*/", self.page)
        self.assertIn("prefers-color-scheme: dark", self.page)
        with mock.patch.object(forgedash_module(), "TEMPERED_CSS", TEMPERED):
            page = forgedash_module().page_template()
        for token in ("--accent: #d9622b", "--accent: #ff7a3d", "prefers-color-scheme: dark", "Noto Sans Thai"):
            self.assertIn(token, page)

    def test_live_metrics_pause_when_hidden(self):
        self.assertIn("visibilitychange", self.script)
        self.assertIn("document.hidden", self.script)

    def test_every_action_has_a_confirm_step(self):
        for action in ("backup-now", "open-shared-drive", "sync-apply", "doctor-fix", "hub-dashboard", "approve",
                       "deny", "rollback"):
            self.assertIn(f'"{action}"', self.script, action)
        self.assertIn("confirmed: true", self.script)
        self.assertEqual(self.script.count("confirmed: true"), 1, "only the confirm dialog may send confirmed")

    def test_sync_dialog_shows_the_list_and_sends_its_hash(self):
        self.assertIn('"/api/dash/sync-diff"', self.script)
        self.assertIn("diff_hash: shown.hash", self.script)
        self.assertIn('id="c-list"', self.page)
        self.assertIn('$("#c-list").textContent = opts.list', self.script)

    def test_rollback_dialog_names_a_version_only_when_it_is_sure(self):
        # Only from the default boot entry is it certain which entry rpm-ostree rollback picks, so only
        # then is a version named (second review: "not the newest" is not the same as "not the default").
        self.assertIn("const sure = u.booted_is_default", self.script)
        self.assertEqual(self.script.count("u.rollback.version"), 4)   # the guard, row, title, body: all behind sure
        # The text is in the string tables now (forgei18n); the page picks the id behind "sure".
        self.assertIn('sure ? t("dash.rb.ask", {version: u.rollback.version})', self.script)
        self.assertIn('sure ? t("dash.rb.body", {version: u.rollback.version})', self.script)
        en = self.en_strings()
        self.assertTrue(en["dash.rb.ask"].startswith("Roll back to {version}"))
        self.assertTrue(en["dash.rb.body"].startswith("Version {version} becomes"))

    def test_rollback_wording_has_three_honest_cases(self):
        # Third review: from the default entry with no version known, the dialog must not claim
        # "you are not running the default version"; and the other entry may be newer, not "previous".
        en = self.en_strings()
        self.assertNotIn("Makes the previous version the default", self.script + "".join(en.values()))
        self.assertIn('u.booted_is_default ? t("dash.rb.makes_other")', self.script)
        self.assertIn('u.booted_is_default ? t("dash.rb.body_other")', self.script)
        self.assertEqual(en["dash.rb.makes_other"], "Makes the other version in the boot menu the default.")
        self.assertTrue(en["dash.rb.body_other"].startswith("The other version in the boot menu becomes the default boot entry."))

    def en_strings(self):
        with open(os.path.join(ROOT, "files/system/usr/share/forge/i18n/en.json"), encoding="utf-8") as f:
            return json.load(f)["strings"]

    def test_long_vm_names_are_cut_and_kept_whole_in_a_title(self):
        self.assertRegex(self.page, r"\.row\.vm \.t \{[^}]*text-overflow: ellipsis")
        self.assertRegex(self.page, r"\.row\.vm \.t \{[^}]*white-space: nowrap")
        self.assertIn("cell.title = v.name", self.script)   # a property: text only

    def test_javascript_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(self.script)
        try:
            r = subprocess.run([node, "--check", f.name], capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(f.name)
        self.assertEqual(r.returncode, 0, r.stderr)


class BackendUnitTest(unittest.TestCase):
    """forgedash helpers (no root, no server)."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, LIB)
        import forgedash
        cls.d = forgedash

    def test_a_tool_that_cannot_be_stopped_does_not_break_run(self):
        # A tool started through pkexec runs as root, so this user can't kill it after a timeout.
        from unittest import mock
        real_kill = subprocess.Popen.kill
        started = []

        def refuse(proc):
            started.append(proc)
            raise PermissionError(1, "Operation not permitted")
        with mock.patch.object(subprocess.Popen, "kill", refuse):
            code, out, err = self.d.run(["sleep", "30"], 0.5)
        for proc in started:
            real_kill(proc)
            proc.wait()
        self.assertIsNone(code)
        self.assertIn("may still be running", err)

    def test_rollback_dialog_names_a_version_only_from_the_default_entry(self):
        # After a rollback and a restart the running version is the default again, so the other
        # entry is what "rpm-ostree rollback" switches to: the dialog may name it then.
        with open(os.path.join(LIB, "dashboard.html")) as f:
            page = f.read()
        self.assertIn("const sure = u.booted_is_default && u.rollback && u.rollback.version;", page)

    def test_fix_ids_match_the_root_helper(self):
        loader = importlib.machinery.SourceFileLoader("forge_doctor_fix", FIX_HELPER)
        spec = importlib.util.spec_from_loader("forge_doctor_fix", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        self.assertEqual(set(self.d.FIX_IDS), set(mod.FIXES))

    def test_virsh_parsing(self):
        self.assertEqual(self.d.parse_virsh(VIRSH), [{"id": "1", "name": "win11", "state": "running"},
                                                     {"id": "-", "name": "fedora test", "state": "shut off"}])
        self.assertEqual(self.d.parse_virsh(""), [])
        self.assertEqual(self.d.parse_virsh("garbage\n"), [])
        rows = self.d.parse_virsh(" Id   Name   State\n---\n" + " 1    x" + "y" * 5000 + "   running\n" * 600)
        self.assertLessEqual(len(rows), 200)
        self.assertTrue(all(len(r["name"]) <= 200 for r in rows))

    def test_tool_output_is_capped_while_the_tool_runs(self):
        # A tool writing 300 MB (to stdout, or to stderr) is stopped at the cap: the dashboard never
        # holds it all, and does not wait for the tool to end.
        code = (
            "import json, sys, time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import forgedash\n"
            "w = ('import sys, time\\nout = sys.stdout.buffer if sys.argv[1] == \"out\" else sys.stderr.buffer\\n'\n"
            "     'for _ in range(300): out.write(b\"a\" * 1048576)\\nout.flush()\\ntime.sleep(30)\\n')\n"
            "t = time.monotonic()\n"
            "c, out, err = forgedash.run([sys.executable, '-c', w, sys.argv[2]], 15)\n"
            "hwm = [int(x.split()[1]) for x in open('/proc/self/status') if x.startswith('VmHWM:')][0]\n"
            "print(json.dumps({'code': c, 'out': len(out), 'err': err, 'secs': time.monotonic() - t,\n"
            "                  'rss_mb': hwm // 1024}))\n")
        for stream in ("out", "err"):
            r = subprocess.run([sys.executable, "-c", code, LIB, stream], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            d = json.loads(r.stdout)
            self.assertIsNone(d["code"], stream)
            self.assertIn("too much output", d["err"], stream)
            self.assertLess(d["secs"], 10, stream)
            self.assertLess(d["rss_mb"], 100, stream)
        # Normal output is unchanged.
        self.assertEqual(self.d.run([sys.executable, "-c", "import sys; print('hi'); print('e', file=sys.stderr)"]),
                         (0, "hi\n", "e\n"))
        self.assertIn("took too long", self.d.run([sys.executable, "-c", "import time; time.sleep(5)"], 0.5)[2])

    def test_doctor_runs_one_at_a_time_and_failures_are_cached(self):
        calls = []

        def slow(argv, timeout=60):
            calls.append(argv)
            time.sleep(0.5)
            return None, "forge-doctor took too long and was stopped."

        with mock.patch.object(self.d, "core_get", side_effect=self.d.CoreDown("down")), \
                mock.patch.object(self.d, "tool_json", side_effect=slow):
            dash = self.d.Dashboard()
            out = []
            th = [threading.Thread(target=lambda: out.append(dash.doctor_report())) for _ in range(5)]
            for t in th:
                t.start()
            for t in th:
                t.join()
            self.assertEqual(len(calls), 1, "the local doctor ran more than once at a time")
            self.assertEqual(len(out), 5)
            self.assertTrue(all(o["report"] is None and o["error"] for o in out), out)
            dash.doctor_report()
            self.assertEqual(len(calls), 1, "a failure was not reused for a short while")
        self.assertLess(self.d.DOCTOR_FAIL_S, self.d.DOCTOR_CACHE_S)

    def test_scrub(self):
        s = self.d.scrub
        self.assertEqual(s("a\u202eb\u2066c\u200fd\u200be\ufefff\x1bg\x00h\ri\x85j"), "abcdefghij")
        self.assertEqual(s("tab\tand\nnewline"), "tab\tand\nnewline")
        self.assertEqual(s({"k\u202e": ["x\u2067", 1, None, True, 2.5, {"\u200e": "y"}]}),
                         {"k": ["x", 1, None, True, 2.5, {"": "y"}]})
        self.assertEqual(s("Thai \u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35, caf\u00e9"), "Thai \u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35, caf\u00e9")
        self.assertEqual(s(json.loads('"a\\ud800b"')), "ab")    # a lone surrogate from JSON

    def test_approval_ids(self):
        self.assertTrue(self.d.APPROVAL_ID.fullmatch(APPROVAL))
        for bad in (APPROVAL.upper(), APPROVAL + "\n", "../" + APPROVAL[3:], APPROVAL[:-1]):
            self.assertIsNone(self.d.APPROVAL_ID.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()
