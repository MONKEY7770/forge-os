"""Regression tests for review findings R3, R5, R8, R9, R10 (forge-health part) and R11."""
import contextlib
import ipaddress
import json
import io
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
BIN = os.path.join(ROOT, "files/system/usr/bin")
INIT = os.path.join(ROOT, "files/system/usr/libexec/forge-core-init")
sys.path.insert(0, LIB)
TMP = tempfile.mkdtemp()
os.environ.setdefault("FORGE_SCHEMA", os.path.join(ROOT, "files/system/usr/share/forge/settings.json"))
os.environ.setdefault("FORGE_CONF", os.path.join(TMP, "settings.json"))
os.environ.setdefault("FORGE_CORE_RUN", os.path.join(TMP, "run"))
os.environ["FORGE_AGENTS_SYSTEM"] = os.path.join(ROOT, "files/system/usr/share/forge/agents")

from fastapi.testclient import TestClient  # noqa: E402
from forgecore import app as core  # noqa: E402


def read(path):
    with open(path) as f:
        return f.read()


def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


# R3: a slow local server, reached only because the test allows 127.0.0.1.

class SlowServer:
    """Drips one byte every 0.5 s. mode: headers, body or silent."""

    def __init__(self, mode):
        self.mode = mode
        self.stop = threading.Event()
        self.lsock = socket.socket()
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(5)
        self.port = self.lsock.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self.lsock.accept()
        except OSError:
            return
        self.conns.append(conn)
        try:
            conn.recv(4096)
            if self.mode == "headers":
                conn.sendall(b"HTTP/1.1 200 OK\r\n")
                drip = b"X-Pad: " + b"a" * 1000
            elif self.mode == "body":
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
                drip = b"b" * 100000
            else:
                drip = b""
            for i in range(len(drip)):
                if self.stop.wait(0.5):
                    return
                conn.sendall(drip[i:i + 1])
            self.stop.wait(60)
        except OSError:
            pass

    def close(self):
        self.stop.set()
        for c in self.conns + [self.lsock]:
            try:
                c.close()
            except OSError:
                pass


class SafeFetchDeadlineTest(unittest.TestCase):
    def fetch_slow(self, mode, deadline=2):
        srv = SlowServer(mode)
        self.addCleanup(srv.close)
        out = {}

        def run():
            # Test seam: allow 127.0.0.1 here only. Production keeps refusing it.
            with mock.patch.object(core, "_ip_allowed", lambda ip: True):
                out["text"] = core.safe_fetch(f"http://127.0.0.1:{srv.port}/", deadline_s=deadline)
        start = time.monotonic()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(8)
        return out.get("text"), time.monotonic() - start

    def assert_stopped_in_time(self, text, took):
        self.assertIsNotNone(text, f"safe_fetch still running after {took:.1f} s")
        self.assertLess(took, 3.5)
        self.assertTrue(text.startswith("Error") or "[stopped: took too long]" in text, text)

    def test_slow_headers_stop_at_deadline(self):
        self.assert_stopped_in_time(*self.fetch_slow("headers"))

    def test_slow_body_stops_at_deadline(self):
        text, took = self.fetch_slow("body")
        self.assert_stopped_in_time(text, took)
        self.assertIn("[stopped: took too long]", text)
        self.assertTrue(text.startswith("HTTP 200"), text)

    def test_silent_server_stops_at_deadline(self):
        self.assert_stopped_in_time(*self.fetch_slow("silent"))

    def test_loopback_still_refused_without_seam(self):
        self.assertIn("not allowed", core.safe_fetch("http://127.0.0.1:1/"))


# R8: NAT64 and site-local IPv6 forms.

class Nat64Test(unittest.TestCase):
    BAD = ["64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::c0a8:101", "64:ff9b::a9fe:a9fe",
           "64:ff9b:1:7f00:0:100::", "64:ff9b:1:a00:0:100::", "fec0::1", "fec0:1::5", "feff::1"]

    def test_private_ipv4_inside_nat64_refused(self):
        for a in self.BAD:
            self.assertFalse(core._ip_allowed(ipaddress.ip_address(a)), a)

    def test_public_ipv4_inside_nat64_allowed(self):
        self.assertTrue(core._ip_allowed(ipaddress.ip_address("64:ff9b::808:808")))
        self.assertTrue(core._ip_allowed(ipaddress.ip_address("2001:4860:4860::8888")))
        self.assertTrue(core._ip_allowed(ipaddress.ip_address("8.8.8.8")))

    def test_safe_fetch_never_connects_to_them(self):
        seen = []

        def record(addr, *a, **k):
            seen.append(addr)
            raise OSError("stop here")
        for a in self.BAD:
            infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, 80, 0, 0))]
            with mock.patch.object(core.socket, "getaddrinfo", return_value=infos), \
                 mock.patch.object(core.socket, "create_connection", side_effect=record):
                self.assertIn("not allowed", core.safe_fetch("http://nat64.example/"), a)
        self.assertEqual(seen, [])


# R9 and R5 need a Forge Core app.

class SecondReviewFetchTest(unittest.TestCase):
    """Gaps the independent reviewer found in the first R3 and R8 fixes."""

    def test_r3_slow_dns_lookup_counts_against_the_deadline(self):
        def slow_lookup(*a, **k):
            time.sleep(5)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        with mock.patch.object(core.socket, "getaddrinfo", slow_lookup):
            t0 = time.monotonic()
            out = core.safe_fetch("http://slow-dns.example/", deadline_s=1)
            took = time.monotonic() - t0
        self.assertLess(took, 2.5, f"took {took:.1f}s with a 1s deadline")
        self.assertIn("Error", out)

    def test_r8_siit_translated_addresses_are_refused(self):
        for a in ("::ffff:0:a00:1", "::ffff:0:a9fe:a9fe", "::ffff:0:7f00:1", "::ffff:0:c0a8:101"):
            self.assertFalse(core._ip_allowed(ipaddress.ip_address(a)), a)
        self.assertTrue(core._ip_allowed(ipaddress.ip_address("2001:4860:4860::8888")))


class FakeCollector:
    def current(self):
        return {"ts": 1, "vram_total_mb": 12227.0, "mem_total_gb": 62.6}

    def start(self):
        pass

    def stop(self):
        pass


class FakeOllama:
    def available(self):
        return False

    def list(self):
        return []


class Pruner:
    def __init__(self):
        self.calls = []

    def prune(self, days):
        self.calls.append(days)


class ClockTrustTest(unittest.TestCase):
    def test_clock_trust_rules(self):
        def fake_run(out=None, exc=None):
            def f(*a, **k):
                if exc:
                    raise exc
                return subprocess.CompletedProcess(a, 0, out, "")
            return f
        with mock.patch.object(core.subprocess, "run", fake_run("yes\n")):
            self.assertTrue(core._clock_trusted())
        with mock.patch.object(core.subprocess, "run", fake_run(exc=FileNotFoundError())):
            self.assertTrue(core._clock_trusted(), "no timedatectl: keep the old behaviour")
        with mock.patch.object(core.subprocess, "run", fake_run("no\n")), \
                mock.patch.object(core, "_uptime_s", lambda: 600, create=True):
            self.assertFalse(core._clock_trusted())
        with mock.patch.object(core.subprocess, "run", fake_run("no\n")), \
                mock.patch.object(core, "_uptime_s", lambda: 2 * 86400, create=True):
            self.assertTrue(core._clock_trusted(), "an offline PC still cleans up after a day")


class CleanupAtStartTest(unittest.TestCase):
    def setUp(self):
        # These tests are about timing, not about clock trust: don't depend on this machine's
        # time sync state (test_no_cleanup_while_the_clock_may_be_wrong sets its own).
        p = mock.patch.object(core, "_clock_trusted", lambda: True)
        p.start()
        self.addCleanup(p.stop)

    def test_cleanup_runs_soon_after_start(self):
        data = tempfile.mkdtemp(dir=TMP)
        from forgecore import policy as P
        pol = P.Policy(os.path.join(data, "p.db"), os.path.join(data, "p.json"))
        pruned = []
        pol.prune = lambda days: pruned.append(("policy", days))
        gw = Pruner()
        gw.chat = lambda *a, **k: None
        runner = Pruner()
        runner.start_scheduler = runner.stop_scheduler = lambda *a, **k: None
        app = core.create_app(token="a", viewer_token="v", collector=FakeCollector(), ollama=FakeOllama(),
                              gateway=gw, policy=pol, runner=runner, data_dir=data,
                              cleanup_delay_s=0.2)
        with TestClient(app):
            end = time.monotonic() + 5
            while time.monotonic() < end and not (pruned and gw.calls and runner.calls):
                time.sleep(0.05)
        self.assertEqual(pruned, [("policy", 90)])
        self.assertEqual(gw.calls, [90])
        self.assertEqual(runner.calls, [30])

    def test_daily_cleanup_counts_time_spent_asleep(self):
        # A laptop that suspends instead of shutting down: a day passes on the wall clock while
        # the process's own (monotonic) clock barely moves. The next cleanup must still come.
        data = tempfile.mkdtemp(dir=TMP)
        from forgecore import policy as P
        pol = P.Policy(os.path.join(data, "p.db"), os.path.join(data, "p.json"))
        pruned = []
        pol.prune = lambda days: pruned.append(days)
        runner = Pruner()
        runner.start_scheduler = runner.stop_scheduler = lambda *a, **k: None
        clock = [1_000_000.0]
        with mock.patch.object(core, "_wall_clock", lambda: clock[0], create=True):
            app = core.create_app(token="a", viewer_token="v", collector=FakeCollector(), ollama=FakeOllama(),
                                  gateway=Pruner(), policy=pol, runner=runner, data_dir=data,
                                  cleanup_delay_s=0.1, cleanup_check_s=0.1)
            with TestClient(app):
                end = time.monotonic() + 5
                while time.monotonic() < end and len(pruned) < 1:
                    time.sleep(0.05)
                clock[0] += 86400 + 1          # a day asleep
                end = time.monotonic() + 5
                while time.monotonic() < end and len(pruned) < 2:
                    time.sleep(0.05)
        self.assertEqual(len(pruned), 2, "the cleanup after a day asleep did not run")

    def test_no_cleanup_while_the_clock_may_be_wrong(self):
        # Right after boot, before time sync, the clock can be years ahead: a cleanup then would
        # delete everything as "old". It waits until the clock can be trusted.
        data = tempfile.mkdtemp(dir=TMP)
        from forgecore import policy as P
        pol = P.Policy(os.path.join(data, "p.db"), os.path.join(data, "p.json"))
        pruned = []
        pol.prune = lambda days: pruned.append(days)
        runner = Pruner()
        runner.start_scheduler = runner.stop_scheduler = lambda *a, **k: None
        trusted = [False]
        with mock.patch.object(core, "_clock_trusted", lambda: trusted[0], create=True):
            app = core.create_app(token="a", viewer_token="v", collector=FakeCollector(), ollama=FakeOllama(),
                                  gateway=Pruner(), policy=pol, runner=runner, data_dir=data,
                                  cleanup_delay_s=0.1, cleanup_check_s=0.1)
            with TestClient(app):
                time.sleep(0.8)
                self.assertEqual(pruned, [], "cleanup ran while the clock could not be trusted")
                trusted[0] = True
                end = time.monotonic() + 5
                while time.monotonic() < end and not pruned:
                    time.sleep(0.05)
        self.assertEqual(len(pruned), 1)

    def test_default_first_cleanup_is_within_minutes(self):
        import inspect
        default = inspect.signature(core.create_app).parameters["cleanup_delay_s"].default
        self.assertLessEqual(default, 300)


# R5: two tokens.

class TwoTokenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = core.create_app(token="admin-t", viewer_token="view-t", collector=FakeCollector(),
                              ollama=FakeOllama(), start_collector=False,
                              data_dir=os.path.join(TMP, "twotoken"))
        cls.c = TestClient(app)
        cls.admin = {"Authorization": "Bearer admin-t"}
        cls.viewer = {"Authorization": "Bearer view-t"}
        cls.viewer_x = {"X-Forge-Token": "view-t"}

    def test_viewer_can_read_plain_status(self):
        for path in ["/api/settings", "/api/settings/core.port", "/api/metrics", "/api/metrics/now",
                     "/api/hardware", "/api/agents", "/api/runs", "/api/tools", "/api/notices",
                     "/api/approval-count"]:
            self.assertEqual(self.c.get(path, headers=self.viewer).status_code, 200, path)
            self.assertEqual(self.c.get(path, headers=self.viewer_x).status_code, 200, path)

    def test_viewer_cannot_read_sensitive(self):
        for path in ["/api/gateway/keys", "/api/gateway/secrets", "/api/gateway/usage",
                     "/api/gateway/providers", "/api/audit", "/api/policy", "/api/audit?limit=5",
                     # Approvals show exactly what an agent wants to do, secrets included: admin only.
                     "/api/approvals", "/api/approvals?status=pending"]:
            self.assertEqual(self.c.get(path, headers=self.viewer).status_code, 403, path)
            self.assertEqual(self.c.get(path, headers=self.admin).status_code, 200, path)

    def test_approval_count_has_no_details(self):
        r = self.c.get("/api/approval-count", headers=self.viewer)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), {"pending"})

    def test_viewer_path_tricks_do_not_leak(self):
        for path in ["/api/gateway/keys/", "/api//gateway/keys", "/api/%61udit", "/api/gateway/%6Beys",
                     "/api/./audit", "/api/x/../audit"]:
            r = self.c.get(path, headers=self.viewer, follow_redirects=True)
            self.assertNotEqual(r.status_code, 200, path)

    def test_viewer_cannot_change_anything(self):
        calls = [("put", "/api/settings", {"values": {"ai.keep_alive_min": 10}}),
                 ("post", "/api/settings/reset", {"keys": ["core.port"]}),
                 ("post", "/api/models/pull", {"name": "qwen3:8b"}),
                 ("post", "/api/models/fit", {"size_bytes": 1}),
                 ("delete", "/api/models/qwen3:8b", None),
                 ("post", "/api/gateway/keys", {"name": "x"}),
                 ("put", "/api/gateway/secrets/openai", {"value": "x"}),
                 ("put", "/api/policy", {"config": {}}),
                 ("post", "/api/approvals/abc/approve", {}),
                 ("post", "/api/approvals/abc/deny", {}),
                 ("post", "/api/agents/system-checkup/run", {}),
                 ("put", "/api/agents/x", {}),
                 ("post", "/api/runs/1/cancel", {})]
        for method, path, body in calls:
            r = self.c.request(method.upper(), path, headers=self.viewer, json=body)
            self.assertEqual(r.status_code, 403, f"{method} {path}")
            self.assertIn("sudo forge-api", r.text)
        for method in ("HEAD", "OPTIONS", "PATCH"):
            self.assertIn(self.c.request(method, "/api/settings", headers=self.viewer).status_code,
                          (403, 405), method)

    def test_admin_can_change_and_approve(self):
        self.assertEqual(self.c.post("/api/models/fit", headers=self.admin, json={"size_bytes": 1}).status_code, 200)
        self.assertEqual(self.c.post("/api/approvals/abc/approve", headers=self.admin, json={}).status_code, 404)

    def test_wrong_or_empty_tokens_refused(self):
        self.assertEqual(self.c.get("/api/settings").status_code, 401)
        self.assertEqual(self.c.get("/api/settings", headers={"X-Forge-Token": "nope"}).status_code, 401)
        c = TestClient(core.create_app(token="", viewer_token="", collector=FakeCollector(),
                                       ollama=FakeOllama(), start_collector=False,
                                       data_dir=os.path.join(TMP, "empty")))
        self.assertEqual(c.get("/api/settings", headers={"X-Forge-Token": ""}).status_code, 401)
        self.assertEqual(c.get("/api/settings", headers={"Authorization": "Bearer "}).status_code, 401)


class CoreInitTest(unittest.TestCase):
    """Runs forge-core-init with a fake chown that records owners, in a temp folder."""

    def run_init(self, etc):
        fake = tempfile.mkdtemp(dir=TMP)
        log = os.path.join(fake, "chown.log")
        write_exec(os.path.join(fake, "chown"), f'#!/bin/sh\necho "$@" >> {log}\n')
        env = dict(os.environ, PATH=fake + ":" + os.environ["PATH"], FORGE_ETC_DIR=etc)
        r = subprocess.run(["bash", INIT], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(log) as f:
            return f.read().splitlines()

    def test_creates_both_tokens_with_right_owner_and_mode(self):
        etc = tempfile.mkdtemp(dir=TMP)
        chowns = self.run_init(etc)
        admin, viewer = os.path.join(etc, "core.token"), os.path.join(etc, "viewer.token")
        self.assertIn(f"root:forge {admin}", chowns)
        self.assertIn(f"forge:wheel {viewer}", chowns)
        self.assertFalse(any(c.endswith(admin) and "wheel" in c for c in chowns))
        for p in (admin, viewer):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640, p)
            self.assertGreaterEqual(len(read(p).strip()), 32, p)
        self.assertNotEqual(read(admin), read(viewer))

    def test_settings_folder_stays_readable_under_a_strict_umask(self):
        # Forge Core runs with UMask=0077. If it starts first on a new install, /etc/forge
        # must still be readable, or desktop users silently get default settings.
        etc = os.path.join(tempfile.mkdtemp(dir=TMP), "forge")
        old = os.umask(0o077)
        try:
            self.run_init(etc)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(etc).st_mode), 0o755)
        os.chmod(etc, 0o700)          # a folder already made private by the old code is repaired
        self.run_init(etc)
        self.assertEqual(stat.S_IMODE(os.stat(etc).st_mode), 0o755)

    def test_idempotent_and_fixes_modes(self):
        etc = tempfile.mkdtemp(dir=TMP)
        self.run_init(etc)
        admin, viewer = os.path.join(etc, "core.token"), os.path.join(etc, "viewer.token")
        before = (read(admin), read(viewer))
        os.chmod(admin, 0o664)
        os.chmod(viewer, 0o666)
        chowns = self.run_init(etc)
        self.assertEqual((read(admin), read(viewer)), before)
        self.assertEqual(stat.S_IMODE(os.stat(admin).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(viewer).st_mode), 0o640)
        self.assertIn(f"root:forge {admin}", chowns)

    def test_token_never_written_into_a_file_someone_already_has_open(self):
        etc = tempfile.mkdtemp(dir=TMP)
        for name in ("core.token", "viewer.token"):
            p = os.path.join(etc, name)
            open(p, "w").close()
            os.chmod(p, 0o666)
        held = [open(os.path.join(etc, n)) for n in ("core.token", "viewer.token")]
        try:
            self.run_init(etc)
            for f in held:
                self.assertEqual(f.read(), "", "a handle opened before init could read the new token")
        finally:
            for f in held:
                f.close()
        for n in ("core.token", "viewer.token"):
            p = os.path.join(etc, n)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640)
            self.assertGreaterEqual(len(read(p).strip()), 32)

    def test_failed_chown_leaves_no_token_copy_behind(self):
        etc = tempfile.mkdtemp(dir=TMP)
        fake = tempfile.mkdtemp(dir=TMP)
        write_exec(os.path.join(fake, "chown"), "#!/bin/sh\nexit 1\n")
        env = dict(os.environ, PATH=fake + ":" + os.environ["PATH"], FORGE_ETC_DIR=etc)
        r = subprocess.run(["bash", INIT], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([f for f in os.listdir(etc) if ".token." in f], [], "a token copy was left behind")

    def test_old_single_token_is_replaced_on_upgrade(self):
        etc = tempfile.mkdtemp(dir=TMP)
        admin = os.path.join(etc, "core.token")
        with open(admin, "w") as f:
            f.write("old-token-admins-could-read\n")
        self.run_init(etc)
        self.assertNotIn("old-token", read(admin))
        self.assertTrue(os.path.exists(os.path.join(etc, "viewer.token")))


class RecordingHandler(BaseHTTPRequestHandler):
    seen = []

    def _reply(self):
        self.seen.append((self.command, self.path, self.headers.get("Authorization")))
        body = b"[]"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_PUT = do_POST = do_DELETE = _reply

    def log_message(self, *a):
        pass


class ForgeApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.fake = tempfile.mkdtemp(dir=TMP)
        write_exec(os.path.join(cls.fake, "forge-config"), f"#!/bin/sh\necho {cls.srv.server_port}\n")
        cls.admin = os.path.join(cls.fake, "core.token")
        cls.viewer = os.path.join(cls.fake, "viewer.token")
        with open(cls.admin, "w") as f:
            f.write("admin-t\n")
        with open(cls.viewer, "w") as f:
            f.write("view-t\n")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def api(self, *args, admin=None):
        RecordingHandler.seen.clear()
        env = dict(os.environ, PATH=self.fake + ":" + os.environ["PATH"],
                   FORGE_CORE_TOKEN=admin or os.path.join(self.fake, "missing"),
                   FORGE_VIEWER_TOKEN=self.viewer, FORGE_API_SKIP_OWNER_CHECK="1")
        return subprocess.run([sys.executable, os.path.join(BIN, "forge-api"), *args],
                              env=env, capture_output=True, text=True, timeout=30)

    def test_get_uses_viewer_token_without_admin(self):
        r = self.api("GET", "/api/notices")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(RecordingHandler.seen, [("GET", "/api/notices", "Bearer view-t")])

    def test_change_without_admin_token_is_refused_locally(self):
        r = self.api("PUT", "/api/settings", '{"values": {}}')
        self.assertEqual(r.returncode, 1)
        self.assertIn("Changes need admin rights: run sudo forge-api", r.stderr)
        self.assertEqual(RecordingHandler.seen, [])

    def test_admin_token_used_when_readable(self):
        r = self.api("PUT", "/api/settings", '{"values": {}}', admin=self.admin)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(RecordingHandler.seen, [("PUT", "/api/settings", "Bearer admin-t")])

    def test_viewer_only_mode_ignores_admin_token(self):
        env_admin = self.admin
        RecordingHandler.seen.clear()
        env = dict(os.environ, PATH=self.fake + ":" + os.environ["PATH"], FORGE_CORE_TOKEN=env_admin,
                   FORGE_VIEWER_TOKEN=self.viewer, FORGE_API_SKIP_OWNER_CHECK="1", FORGE_API_VIEWER="1")
        r = subprocess.run([sys.executable, os.path.join(BIN, "forge-api"), "GET", "/api/notices"],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(RecordingHandler.seen, [("GET", "/api/notices", "Bearer view-t")])

    def test_port_owner_check_kept(self):
        env = dict(os.environ, PATH=self.fake + ":" + os.environ["PATH"], FORGE_CORE_TOKEN=self.admin,
                   FORGE_VIEWER_TOKEN=self.viewer)
        env.pop("FORGE_API_SKIP_OWNER_CHECK", None)
        RecordingHandler.seen.clear()
        r = subprocess.run([sys.executable, os.path.join(BIN, "forge-api"), "GET", "/api/notices"],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(RecordingHandler.seen, [])


class ForgeApiConnectionOwnerTest(unittest.TestCase):
    """The port owner is checked again on the actual connection, before the token is sent:
    Forge Core restarting between the first check and the connect must not leak the token."""

    def load(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("forge_api_under_test", os.path.join(BIN, "forge-api"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def call(self, forge_uid):
        srv = ForgeApiTest  # reuse its recording server and fake forge-config
        srv.setUpClass()
        self.addCleanup(srv.tearDownClass)
        mod = self.load()
        mod.listener_is_forge = lambda port: True   # the first check passed (Forge Core was there)
        RecordingHandler.seen.clear()
        env = {"PATH": srv.fake + ":" + os.environ["PATH"], "FORGE_CORE_TOKEN": srv.admin,
               "FORGE_VIEWER_TOKEN": srv.viewer, "FORGE_API_FORGE_UID": str(forge_uid)}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", ["forge-api", "GET", "/api/x"]):
            mod.TOKEN_PATH, mod.VIEWER_TOKEN_PATH = srv.admin, srv.viewer
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return mod.main()

    def test_token_not_sent_when_someone_else_answers(self):
        # The server answering is owned by root (this test), not the forge user (uid 65533 here).
        rc = self.call(65533)
        self.assertEqual(RecordingHandler.seen, [], "the admin token was sent to a server not run by forge")
        self.assertEqual(rc, 1)

    def test_token_sent_when_forge_answers(self):
        rc = self.call(os.geteuid())
        self.assertEqual(rc, 0)
        self.assertEqual(len(RecordingHandler.seen), 1)


# R10 and R11: forge-health notifications.

class ForgeApiOutputTest(unittest.TestCase):
    def test_text_direction_and_control_characters_are_shown_escaped(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("forge_api_esc", os.path.join(BIN, "forge-api"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        out = mod.safe_for_terminal('{"content": "rm -rf /\u202e#txt.\u0007\x1b[2J ok"}')
        for bad in ("\u202e", "\u0007", "\x1b"):
            self.assertNotIn(bad, out)
        self.assertIn("\\u202e", out)
        self.assertIn("ok", out)


class ForgeHealthNotifyTest(unittest.TestCase):
    def setUp(self):
        self.fake = tempfile.mkdtemp(dir=TMP)
        self.log = os.path.join(self.fake, "notify.log")
        f = self.fake
        write_exec(os.path.join(f, "notify-send"),
                   "#!/usr/bin/env python3\nimport json, sys\n"
                   f"open({self.log!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
        cfg = {"monitor.gpu_temp_warn": "200", "monitor.disk_warn_pct": "101", "monitor.mem_warn_gb": "999999",
               "ai.ollama": "off", "monitor.check_ollama": "off", "core.enabled": "on", "core.port": "47900",
               "monitor.notify": "on"}
        cases = "\n".join(f'  {k}) echo "{v}" ;;' for k, v in cfg.items())
        write_exec(os.path.join(f, "forge-config"), f'#!/bin/sh\ncase "$2" in\n{cases}\nesac\n')
        write_exec(os.path.join(f, "systemctl"), "#!/bin/sh\nexit 0\n")
        write_exec(os.path.join(f, "nvidia-smi"), "#!/bin/sh\nexit 1\n")
        notices = [{"ts": 5, "message": "-u critical <a href='http://x'>click</a>"}]
        write_exec(os.path.join(f, "forge-api"),
                   "#!/bin/sh\necho \"$FORGE_API_VIEWER $@\" >> " + os.path.join(f, "api.log") + "\n"
                   'case "$2" in\n'
                   f"  /api/notices*) echo '{json.dumps(notices)}' ;;\n"
                   "  /api/approval-count) echo '{\"pending\": 1}' ;;\nesac\n")
        self.viewer = os.path.join(f, "viewer.token")
        with open(self.viewer, "w") as fh:
            fh.write("view-t\n")
        self.hw = os.path.join(f, "hardware-notice.json")
        with open(self.hw, "w") as fh:
            json.dump({"changes": ["-x <b>Evil USB</b> & <a href='http://x'>more</a>"]}, fh)
        self.env = dict(os.environ, PATH=f + ":" + os.environ["PATH"], HOME=f, XDG_STATE_HOME=os.path.join(f, "state"),
                        FORGE_VIEWER_TOKEN=self.viewer, FORGE_HW_NOTICE=self.hw)

    def run_health(self):
        r = subprocess.run(["bash", os.path.join(BIN, "forge-health")], env=self.env,
                           capture_output=True, text=True, timeout=60)
        calls = []
        if os.path.exists(self.log):
            with open(self.log) as fh:
                calls = [json.loads(line) for line in fh]
        return r, calls

    def test_every_notification_ends_options_and_is_escaped(self):
        r, calls = self.run_health()
        summaries = [c[c.index("--") + 1] if "--" in c else c for c in calls]
        self.assertEqual(len(calls), 4, (calls, r.stderr))   # hardware, agent, approvals, health
        for c in calls:
            self.assertIn("--", c, c)
            rest = c[c.index("--") + 1:]
            self.assertEqual(len(rest), 2, c)
            for text in rest:
                self.assertNotIn("<", text, c)
            opts = c[:c.index("--")]
            self.assertIn("-a", opts, c)
            self.assertEqual(opts[opts.index("-a") + 1], "Forge OS", c)
            self.assertIn("-i", opts, c)                       # every notice has an icon
            for i, opt in enumerate(opts):
                if i and opts[i - 1] in ("-a", "-i"):
                    continue
                self.assertIn(opt, ("-u", "critical", "normal", "-a", "-i"), c)
        joined = json.dumps(calls)
        self.assertIn("&lt;b&gt;Evil USB&lt;/b&gt; &amp;", joined)
        self.assertIn("&lt;a href=", joined)
        self.assertIn("Forge OS: hardware changed", json.dumps(summaries))

    def approval_calls(self, calls):
        return [c for c in calls if any("need your OK" in a or "needs your OK" in a for a in c)]

    def test_approval_requests_are_shown_once_and_not_critical(self):
        # Audit finding: a critical notice came back every 10 minutes while anything waited, and it
        # pointed to a web address and a terminal command.
        r, calls = self.run_health()
        first = self.approval_calls(calls)
        self.assertEqual(len(first), 1, calls)
        self.assertNotIn("critical", first[0])
        text = json.dumps(first[0])
        self.assertIn("1 agent action needs your OK", text)
        self.assertIn("Forge Dashboard", text)
        self.assertNotIn("forge-api", text)
        self.assertNotIn("http://", text)
        os.remove(self.log)
        r, calls = self.run_health()            # still the same one waiting: no new notice
        self.assertEqual(self.approval_calls(calls), [], calls)

    def test_ollama_paused_for_a_game_is_not_a_health_problem(self):
        cfg = {"monitor.gpu_temp_warn": "200", "monitor.disk_warn_pct": "101", "monitor.mem_warn_gb": "0",
               "ai.ollama": "on", "monitor.check_ollama": "on", "core.enabled": "off", "monitor.notify": "on"}
        cases = "\n".join(f'  {k}) echo "{v}" ;;' for k, v in cfg.items())
        write_exec(os.path.join(self.fake, "forge-config"), f'#!/bin/sh\ncase "$2" in\n{cases}\nesac\n')
        write_exec(os.path.join(self.fake, "systemctl"), "#!/bin/sh\nexit 3\n")      # Ollama is stopped
        gaming = os.path.join(self.fake, "gaming")
        os.makedirs(gaming)
        self.env["FORGE_GAMING_DIR"] = gaming
        _, calls = self.run_health()
        self.assertIn("Ollama is not running", json.dumps(self.health_calls(calls)))
        os.remove(self.log)
        self.env["XDG_STATE_HOME"] = os.path.join(self.fake, "state2")   # nothing shown before
        open(os.path.join(gaming, "ollama-paused"), "w").close()     # paused for a game, on purpose
        _, calls = self.run_health()
        self.assertNotIn("Ollama is not running", json.dumps(calls))
        os.remove(os.path.join(gaming, "ollama-paused"))             # the same fresh state, not paused
        _, calls = self.run_health()
        self.assertIn("Ollama is not running", json.dumps(self.health_calls(calls)))

    def test_hot_gpu_shows_degrees(self):
        with open(os.path.join(ROOT, "files/system/usr/bin/forge-health")) as f:
            self.assertIn("GPU is hot: ${temp}°C", f.read())

    def health_calls(self, calls):
        return [c for c in calls if "Forge OS health" in c]

    def test_the_same_problems_are_shown_once(self):
        # Review finding: a critical alert came back every 10 minutes for as long as a problem
        # lasted (for example a failed service on a first boot without network).
        r, calls = self.run_health()
        self.assertEqual(len(self.health_calls(calls)), 1, (calls, r.stderr))
        os.remove(self.log)
        r, calls = self.run_health()        # same problem, other numbers: low memory again
        self.assertEqual(self.health_calls(calls), [], calls)
        write_exec(os.path.join(self.fake, "systemctl"),
                   "#!/bin/sh\n[ \"$1\" = --failed ] && echo 'x.service loaded failed failed X'\nexit 0\n")
        r, calls = self.run_health()        # a new problem: shown, with the others
        health = self.health_calls(calls)
        self.assertEqual(len(health), 1, calls)
        self.assertIn("x.service", json.dumps(health))
        os.remove(self.log)
        r, calls = self.run_health()
        self.assertEqual(self.health_calls(calls), [], calls)

    def test_a_problem_that_comes_back_after_a_healthy_run_is_shown_again(self):
        self.run_health()
        os.remove(self.log)
        write_exec(os.path.join(self.fake, "forge-config"),
                   open(os.path.join(self.fake, "forge-config")).read().replace('echo "999999"', 'echo "0"'))
        r, calls = self.run_health()        # healthy
        self.assertEqual(self.health_calls(calls), [], calls)
        write_exec(os.path.join(self.fake, "forge-config"),
                   open(os.path.join(self.fake, "forge-config")).read().replace('mem_warn_gb) echo "0"', 'mem_warn_gb) echo "999999"'))
        r, calls = self.run_health()
        self.assertEqual(len(self.health_calls(calls)), 1, calls)

    def test_a_new_check_alone_does_not_claim_settings_were_reapplied(self):
        with open(self.hw, "w") as fh:
            json.dump({"changes": ["This PC has an NVIDIA GPU, but runs the universal image."],
                       "reapplied": False}, fh)
        r, calls = self.run_health()
        hw = [c for c in calls if "NVIDIA" in json.dumps(c)]
        self.assertEqual(len(hw), 1, calls)
        self.assertNotIn("re-applied", json.dumps(hw))
        self.assertNotIn("hardware changed", json.dumps(hw))

    def test_a_pending_notice_waits_for_the_reapply(self):
        import time as _t
        with open(self.hw, "w") as fh:
            json.dump({"ts": int(_t.time()), "changes": ["RAM changed"], "reapplied": False, "pending": True}, fh)
        r, calls = self.run_health()
        self.assertFalse([c for c in calls if "RAM changed" in json.dumps(c)], calls)
        # Still pending after 15 minutes means the re-apply was stopped: show it then.
        with open(self.hw, "w") as fh:
            json.dump({"ts": int(_t.time()) - 900, "changes": ["RAM changed"], "reapplied": False, "pending": True}, fh)
        r, calls = self.run_health()
        hw = [c for c in calls if "RAM changed" in json.dumps(c)]
        self.assertEqual(len(hw), 1, calls)
        self.assertNotIn("re-applied to match", json.dumps(hw))

    def test_a_pending_notice_from_a_clock_that_was_ahead_is_not_hidden(self):
        import time as _t
        with open(self.hw, "w") as fh:
            json.dump({"ts": int(_t.time()) + 86400, "changes": ["RAM changed"], "reapplied": False,
                       "pending": True}, fh)
        r, calls = self.run_health()
        self.assertEqual(len([c for c in calls if "RAM changed" in json.dumps(c)]), 1, calls)

    def test_forge_health_asks_forge_api_for_viewer_token_only(self):
        self.run_health()
        with open(os.path.join(self.fake, "api.log")) as fh:
            lines = fh.read().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("1 GET ") for line in lines), lines)

    def test_no_viewer_token_means_no_api_calls(self):
        self.env["FORGE_VIEWER_TOKEN"] = os.path.join(self.fake, "missing")
        self.run_health()
        self.assertFalse(os.path.exists(os.path.join(self.fake, "api.log")))


if __name__ == "__main__":
    unittest.main()


# P1: the doctor report over the API.

class DoctorEndpointTest(unittest.TestCase):
    REPORT = {"created": "2026-11-19 10:00:00", "summary": {"ok": 1, "warn": 1, "info": 0, "unknown": 0},
              "sections": [{"name": "Image", "status": "ok", "items": ["signed image"]},
                           {"name": "Security", "status": "warn", "items": ["no USB key set up"]}]}

    def setUp(self):
        self.calls = 0
        self.gate = threading.Event()
        self.gate.set()

        def doctor():
            self.calls += 1
            self.gate.wait(5)
            return json.loads(json.dumps(self.REPORT))
        app = core.create_app(token="admin-t", viewer_token="view-t", collector=FakeCollector(),
                              ollama=FakeOllama(), start_collector=False, doctor=doctor,
                              data_dir=os.path.join(TMP, "doctor"))
        self.c = TestClient(app)
        self.admin = {"Authorization": "Bearer admin-t"}
        self.viewer = {"Authorization": "Bearer view-t"}

    def test_admin_gets_the_report(self):
        r = self.c.get("/api/doctor", headers=self.admin)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), self.REPORT)

    def test_text_download(self):
        r = self.c.get("/api/doctor?format=text", headers=self.admin)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertIn("attachment", r.headers.get("content-disposition", ""))
        self.assertIn("[WARN] Security", r.text)
        self.assertIn("no USB key set up", r.text)

    def test_odd_format_is_refused(self):
        self.assertEqual(self.c.get("/api/doctor?format=html", headers=self.admin).status_code, 422)

    def test_needs_a_token_and_viewer_is_refused(self):
        # The report describes the PC's security setup, so it is admin only.
        self.assertEqual(self.c.get("/api/doctor").status_code, 401)
        for path in ["/api/doctor", "/api/doctor?format=text", "/api/doctor/"]:
            self.assertEqual(self.c.get(path, headers=self.viewer, follow_redirects=True).status_code, 403, path)
        for path in ["/API/Doctor", "/api/./doctor", "/api//doctor", "/api/%64octor", "/api/x/../doctor"]:
            self.assertNotEqual(self.c.get(path, headers=self.viewer, follow_redirects=True).status_code, 200, path)
        self.assertEqual(self.calls, 0)

    def test_repeated_calls_reuse_a_recent_report(self):
        for _ in range(5):
            self.assertEqual(self.c.get("/api/doctor", headers=self.admin).status_code, 200)
        self.assertEqual(self.calls, 1)

    def test_parallel_calls_build_only_once(self):
        # The report runs system tools; many callers at once must not start many copies.
        self.gate.clear()
        out = []
        ts = [threading.Thread(target=lambda: out.append(self.c.get("/api/doctor", headers=self.admin).status_code))
              for _ in range(6)]
        for t in ts:
            t.start()
        time.sleep(0.5)
        self.gate.set()
        for t in ts:
            t.join(10)
        self.assertEqual(out, [200] * 6)
        self.assertEqual(self.calls, 1)

    def test_a_failing_report_gives_a_plain_error(self):
        def broken():
            raise RuntimeError("secret detail /etc/thing")
        app = core.create_app(token="admin-t", viewer_token="view-t", collector=FakeCollector(),
                              ollama=FakeOllama(), start_collector=False, doctor=broken,
                              data_dir=os.path.join(TMP, "doctor2"))
        r = TestClient(app, raise_server_exceptions=False).get("/api/doctor", headers=self.admin)
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("secret detail", r.text)
