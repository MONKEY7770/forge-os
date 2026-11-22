"""Forge Settings page: only the user who started it can use it.

The server runs under a test-only uid (65533). Requests are sent from child
processes that switch to a uid first, so the real /proc/net/tcp owner check
is exercised. 65534 plays "another user on the same PC". Needs root to
switch uids, so these tests are skipped when not run as root.
"""
import importlib.machinery
import importlib.util
import contextlib
import json
import os
import re
import select
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "files/system/usr/bin/forge-settings")
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
OWNER = 65533   # the desktop user running forge-settings
OTHER = 65534   # another user on the same PC

# Small HTTP client run in a child process under a chosen uid.
CLIENT = r"""
import json, sys, urllib.request, urllib.error
method, url, headers, body = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
req = urllib.request.Request(url, method=method, headers=headers,
                             data=body.encode() if method == "POST" else None)
try:
    with opener.open(req, timeout=10) as r:
        out = {"status": r.status, "body": r.read().decode(), "headers": dict(r.headers)}
except urllib.error.HTTPError as e:
    out = {"status": e.code, "body": e.read().decode(), "headers": dict(e.headers)}
print(json.dumps(out))
"""


def as_uid(uid):
    def drop():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return drop


def request(uid, method, url, headers=None, body=""):
    """Send one request from a process running as uid. Returns (status, body)."""
    r = subprocess.run([sys.executable, "-I", "-c", CLIENT, method, url, json.dumps(headers or {}), body],
                       preexec_fn=as_uid(uid), capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise AssertionError("client failed: " + r.stderr)
    d = json.loads(r.stdout)
    return d["status"], d["body"]


def request_headers(uid, method, url, headers=None, body=""):
    r = subprocess.run([sys.executable, "-I", "-c", CLIENT, method, url, json.dumps(headers or {}), body],
                       preexec_fn=as_uid(uid), capture_output=True, text=True, timeout=20)
    return json.loads(r.stdout)


def refused(uid, method, url, headers=None, body=""):
    """True if the server turned the request away: a 403, or the connection was closed unanswered
    (other users' connections are closed as soon as they arrive)."""
    try:
        status, text = request(uid, method, url, headers, body)
    except AssertionError:
        return True
    return status == 403 and token_of(text) is None


RAW = r"""
import socket, sys
host, port, data = sys.argv[1], int(sys.argv[2]), sys.argv[3].encode().decode("unicode_escape").encode("latin-1")
with socket.create_connection((host, port), timeout=5) as c:
    c.sendall(data)
    out = b""
    while True:
        try:
            chunk = c.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        out += chunk
sys.stdout.write(out.decode(errors="replace"))
"""


def token_of(page):
    m = re.search(r'const TOKEN = "([^"]+)"', page)
    return m.group(1) if m else None


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the server and clients as other uids")
class SettingsPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o777)
        self.mark = os.path.join(self.tmp, "applied.json")
        # Stand-ins for pkexec and forge-apply: record what would be applied.
        fakebin = os.path.join(self.tmp, "bin")
        os.mkdir(fakebin)
        for name, text in (("pkexec", '#!/bin/sh\nexec "$@"\n'),
                           ("forge-apply", f'#!/bin/sh\ncat "$2" > "{self.mark}"\n')):
            p = os.path.join(fakebin, name)
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, 0o755)
        self.env = dict(os.environ, FORGE_LIB=LIB, FORGE_SCHEMA=SCHEMA,
                        FORGE_CONF=os.path.join(self.tmp, "settings.json"), FORGE_NO_BROWSER="1",
                        FORGE_PORT="0", FORGE_APPLY=os.path.join(fakebin, "forge-apply"),
                        PATH=fakebin + os.pathsep + os.environ.get("PATH", ""))
        self.proc = None

    def tearDown(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc.stdout.close()
            self.proc.stderr.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, *args):
        """Start forge-settings as OWNER and return (base url, launch key)."""
        self.proc = subprocess.Popen([sys.executable, "-u", APP, *args], env=self.env, preexec_fn=as_uid(OWNER),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        self.assertTrue(ready, "forge-settings printed no URL")
        line = self.proc.stdout.readline()
        m = re.search(r"(http://127\.0\.0\.1:\d+)/(?:\?k=(\S+))?", line)
        self.assertTrue(m, "no URL in: " + line)
        time.sleep(0.2)
        return m.group(1), m.group(2) or ""

    def test_other_user_cannot_get_token_or_use_api(self):
        # The reported attack: another user finds the port and asks for the page
        # first. Even with the right launch key it must get nothing.
        base, k = self.start()
        self.assertTrue(refused(OTHER, "GET", f"{base}/?k={k}"))
        # The real user's browser still gets the page.
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        self.assertEqual(status, 200)
        token = token_of(body)
        self.assertTrue(token)
        # Even holding the real token, another user can't read or save settings.
        h = {"X-Forge-Token": token, "Content-Type": "application/json"}
        self.assertTrue(refused(OTHER, "GET", f"{base}/api/settings", h))
        self.assertTrue(refused(OTHER, "GET", f"{base}/api/ping", h))
        self.assertTrue(refused(OTHER, "POST", f"{base}/api/save", h, '{"values": {}}'))
        self.assertTrue(refused(OTHER, "POST", f"{base}/api/undo", h, "{}"))
        self.assertFalse(os.path.exists(self.mark), "a save from another user reached forge-apply")

    # P1: first-boot welcome page.

    def test_welcome_page_is_served_once_with_the_launch_key(self):
        base, k = self.start("--welcome")
        self.assertEqual(request(OWNER, "GET", f"{base}/")[0], 403)
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        self.assertEqual(status, 200)
        self.assertIn("Welcome to Forge OS", body)
        self.assertTrue(token_of(body))
        for text in ("sudo forge-usbkey create home", "sudo tailscale up", "location.services",
                     "location.auto_timezone", "location.wifi_lookup", "/api/doctor"):
            self.assertIn(text, body)
        self.assertEqual(request(OWNER, "GET", f"{base}/?k={k}")[0], 403)

    def test_doctor_report_needs_the_token_and_the_owner(self):
        base, k = self.start("--welcome")
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        h = {"X-Forge-Token": token_of(body)}
        self.assertEqual(request(OWNER, "GET", f"{base}/api/doctor")[0], 404)
        self.assertTrue(refused(OTHER, "GET", f"{base}/api/doctor", h))
        status, body = request(OWNER, "GET", f"{base}/api/doctor", h)
        self.assertEqual(status, 200)
        rep = json.loads(body)
        self.assertIn("sections", rep)
        self.assertIn("Virtualization", [s["name"] for s in rep["sections"]])

    def test_welcome_once_shows_only_the_first_time(self):
        cfg = os.path.join(self.tmp, "cfg")
        os.mkdir(cfg)
        os.chown(cfg, OWNER, OWNER)
        self.env["XDG_CONFIG_HOME"] = cfg
        base, k = self.start("--welcome-once")
        self.assertEqual(request(OWNER, "GET", f"{base}/?k={k}")[0], 200)
        marker = os.path.join(cfg, "forge", "welcome-shown")
        self.assertTrue(os.path.isfile(marker))
        self.assertEqual(os.stat(marker).st_uid, OWNER)
        self.proc.terminate()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.proc = None
        r = subprocess.run([sys.executable, APP, "--welcome-once"], env=self.env, preexec_fn=as_uid(OWNER),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("http://", r.stdout)

    def test_welcome_save_changes_only_the_given_settings(self):
        base, k = self.start("--welcome")
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        self.assertIn('merge: true', body)
        h = {"X-Forge-Token": token_of(body), "Content-Type": "application/json"}
        status, out = request(OWNER, "POST", f"{base}/api/save", h,
                              '{"values": {"location.services": false}, "merge": true}')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(out)["ok"], out)
        with open(self.mark) as f:
            self.assertEqual(json.load(f), {"location.services": False})
        # Unknown or bad values are refused before anything is sent.
        for bad in ('{"values": {"nope": 1}, "merge": true}', '{"values": {"location.services": "x"}, "merge": true}',
                    '{"values": [1], "merge": true}'):
            os.unlink(self.mark)
            status, out = request(OWNER, "POST", f"{base}/api/save", h, bad)
            self.assertFalse(json.loads(out)["ok"], bad)
            self.assertFalse(os.path.exists(self.mark), bad)
            open(self.mark, "w").close()

    def test_welcome_once_is_not_used_up_if_the_page_never_opens(self):
        cfg = os.path.join(self.tmp, "cfg")
        os.mkdir(cfg)
        os.chown(cfg, OWNER, OWNER)
        self.env["XDG_CONFIG_HOME"] = cfg
        self.start("--welcome-once")          # the browser never asks for the page
        self.proc.terminate()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.proc = None
        self.assertFalse(os.path.exists(os.path.join(cfg, "forge", "welcome-shown")))
        base, k = self.start("--welcome-once")   # next login: shown again
        self.assertEqual(request(OWNER, "GET", f"{base}/?k={k}")[0], 200)
        self.assertTrue(os.path.exists(os.path.join(cfg, "forge", "welcome-shown")))

    def test_unknown_argument_is_refused(self):
        r = subprocess.run([sys.executable, APP, "--welcom"], env=self.env, preexec_fn=as_uid(OWNER),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("http://", r.stdout)

    def test_welcome_desktop_entries(self):
        auto = os.path.join(ROOT, "files/system/etc/xdg/autostart/forge-welcome.desktop")
        menu = os.path.join(ROOT, "files/system/usr/share/applications/forge-welcome.desktop")
        with open(auto) as f:
            self.assertIn("Exec=forge-settings --welcome-once", f.read())
        with open(menu) as f:
            self.assertIn("Exec=forge-settings --welcome\n", f.read())

    def test_launch_key_required_and_page_served_once(self):
        base, k = self.start()
        self.assertTrue(k, "the launch URL has no ?k= key")
        self.assertEqual(request(OWNER, "GET", f"{base}/")[0], 403)
        self.assertEqual(request(OWNER, "GET", f"{base}/?k=")[0], 403)
        self.assertEqual(request(OWNER, "GET", f"{base}/?k=wrong{k}")[0], 403)
        self.assertEqual(request(OWNER, "GET", f"{base}/?k=%C3%A9")[0], 403)
        port = base.rsplit(":", 1)[1]
        self.assertEqual(request(OWNER, "GET", f"{base}/?k={k}", {"Host": "evil.example:" + port})[0], 403)
        # Wrong tries don't burn the key: the right one still works, once.
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        self.assertEqual(status, 200)
        self.assertTrue(token_of(body))
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        self.assertEqual(status, 403)
        self.assertIsNone(token_of(body))

    def test_normal_flow_and_api(self):
        base, k = self.start()
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        self.assertEqual(status, 200)
        h = {"X-Forge-Token": token_of(body), "Content-Type": "application/json"}
        status, body = request(OWNER, "GET", f"{base}/api/settings", h)
        self.assertEqual(status, 200)
        self.assertIn("schema", json.loads(body))
        self.assertEqual(request(OWNER, "GET", f"{base}/api/ping", h)[0], 200)
        # No token, or a bad Host, is refused even for the owner.
        self.assertEqual(request(OWNER, "GET", f"{base}/api/settings")[0], 404)
        port = base.rsplit(":", 1)[1]
        bad_host = dict(h, Host="evil.example:" + port)
        self.assertEqual(request(OWNER, "GET", f"{base}/api/settings", bad_host)[0], 403)
        self.assertEqual(request(OWNER, "POST", f"{base}/api/save", bad_host, '{"values": {}}')[0], 403)
        self.assertFalse(os.path.exists(self.mark))
        status, body = request(OWNER, "POST", f"{base}/api/save", h, '{"values": {}}')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"], body)
        with open(self.mark) as f:
            self.assertIsInstance(json.load(f), dict)

    def test_rejected_requests_do_not_keep_the_page_alive(self):
        self.env["FORGE_IDLE_S"] = "2"
        base, _ = self.start()
        end = time.time() + 6
        closed = False
        while time.time() < end:
            if self.proc.poll() is not None:
                closed = True
                break
            try:
                request(OTHER, "GET", base + "/", {"Host": base[7:]})
            except AssertionError:
                closed = True   # the server closed while we were asking: the expected outcome
                break
            time.sleep(0.3)
        # Judged while the requests were still coming: no idle time after them.
        self.assertTrue(closed, "another user's 403 requests kept forge-settings running")

    def test_requests_without_the_token_do_not_keep_the_page_alive(self):
        # The owner's own browser can be made to send these by any web page: only a request that passed
        # the token check (or got the page with the launch key) counts as the page being in use.
        self.env["FORGE_IDLE_S"] = "2"
        base, k = self.start()
        status, body = request(OWNER, "GET", f"{base}/?k={k}")
        self.assertEqual(status, 200)
        end = time.time() + 7
        closed = False
        while time.time() < end:
            if self.proc.poll() is not None:
                closed = True
                break
            try:
                request(OWNER, "GET", base + "/api/ping")
                request(OWNER, "GET", base + "/api/settings", {"X-Forge-Token": "wrong"})
                request(OWNER, "GET", base + f"/?k={k}")
                request(OWNER, "POST", base + "/api/save", {}, '{"values": {}}')
            except AssertionError:
                closed = True   # it closed while we were asking
                break
            time.sleep(0.2)
        self.assertTrue(closed, "requests without the token kept forge-settings running")

    def test_idle_page_closes_by_itself(self):
        # Control for the test above: with nobody asking, it closes within the idle time.
        self.env["FORGE_IDLE_S"] = "2"
        self.start()
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.fail("forge-settings did not close when idle")

    def test_page_cannot_be_framed(self):
        base, key = self.start()
        out = request_headers(OWNER, "GET", f"{base}/?k={key}", {"Host": base[7:]})
        self.assertEqual(out["status"], 200)
        h = {k.lower(): v for k, v in out["headers"].items()}
        self.assertEqual(h.get("x-frame-options"), "DENY")
        self.assertIn("frame-ancestors 'none'", h.get("content-security-policy", ""))

    def raw(self, base, data, timeout=5):
        host, port = base[7:].split(":")
        with socket.create_connection((host, int(port)), timeout=timeout) as c:
            c.sendall(data)
            out = b""
            while True:
                try:
                    chunk = c.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                out += chunk
        return out.decode(errors="replace")

    def test_error_responses_from_the_server_itself_cannot_be_framed(self):
        base, _ = self.start()
        host, port = base[7:].split(":")
        for req in ("OPTIONS / HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n", "GARBAGE\\r\\n\\r\\n",
                    "GET / HTTP/9.9\\r\\n\\r\\n"):
            # Sent as the page's own user: other users' connections are closed unanswered.
            out = subprocess.run([sys.executable, "-I", "-c", RAW, host, port, req], preexec_fn=as_uid(OWNER),
                                 capture_output=True, text=True, timeout=20).stdout
            self.assertRegex(out, r"^HTTP/1\.[01] [45]\d\d", out[:80])
            self.assertIn("X-Frame-Options: DENY", out, req)
            self.assertIn("frame-ancestors 'none'", out, req)

    def test_idle_connections_are_closed(self):
        self.env["FORGE_CONN_TIMEOUT_S"] = "1"
        base, _ = self.start()
        host, port = base[7:].split(":")
        c = socket.create_connection((host, int(port)), timeout=5)
        try:
            c.sendall(b"GET / HTT")          # never finishes the request
            t0 = time.time()
            got = c.recv(100)                 # the server closes (or answers 408) on its own
            self.assertLess(time.time() - t0, 4, "a half-open connection was kept")
        except socket.timeout:
            self.fail("the server kept a half-open connection open")
        finally:
            c.close()

    def test_other_users_connections_get_no_thread(self):
        self.env["FORGE_CONN_TIMEOUT_S"] = "20"
        base, _ = self.start()
        host, port = base[7:].split(":")
        script = (
            "import socket, sys, time\n"
            "socks = []\n"
            "for i in range(60):\n"
            "    s = socket.create_connection((sys.argv[1], int(sys.argv[2])))\n"
            "    s.sendall(b'G')\n"
            "    socks.append(s)\n"
            "time.sleep(1.5)\n"
            "alive = 0\n"
            "for s in socks:\n"
            "    s.setblocking(False)\n"
            "    try:\n"
            "        alive += s.recv(1) == b'' and 0 or 0\n"
            "    except BlockingIOError:\n"
            "        alive += 1\n"
            "    except OSError:\n"
            "        pass\n"
            "print(alive)\n")
        r = subprocess.run([sys.executable, "-c", script, host, port], preexec_fn=as_uid(OTHER),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(int(r.stdout.strip()), 0, "another user's connections were kept open")

    def test_launch_key_is_not_printed_when_a_browser_opens_it(self):
        fake = os.path.join(self.tmp, "bin", "xdg-open")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake, 0o755)
        del self.env["FORGE_NO_BROWSER"]
        self.env["XDG_RUNTIME_DIR"] = self.runtime_dir()
        self.proc = subprocess.Popen([sys.executable, "-u", APP], env=self.env, preexec_fn=as_uid(OWNER),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        self.assertTrue(ready)
        line = self.proc.stdout.readline()
        self.assertIn("127.0.0.1", line)
        self.assertNotIn("k=", line)

    def runtime_dir(self):
        run = os.path.join(self.tmp, "run")
        os.mkdir(run, 0o700)
        os.chown(run, OWNER, OWNER)
        return run

    def test_launch_key_is_not_on_the_browser_command_line(self):
        # Any local user can read /proc/PID/cmdline, so xdg-open gets a private redirect page instead.
        log = os.path.join(self.tmp, "xdg-open.jsonl")
        fake = os.path.join(self.tmp, "bin", "xdg-open")
        with open(fake, "w") as f:
            f.write("#!%s\nimport json, os, stat, sys\n"
                    "info = {'argv': sys.argv[1:]}\n"
                    "try:\n"
                    "    p = sys.argv[1]\n"
                    "    info['mode'] = stat.S_IMODE(os.lstat(p).st_mode)\n"
                    "    info['dir_mode'] = stat.S_IMODE(os.lstat(os.path.dirname(p)).st_mode)\n"
                    "    info['body'] = open(p).read()\n"
                    "except (OSError, IndexError) as e:\n"
                    "    info['error'] = str(e)\n"
                    "with open(%r, 'a') as f:\n"
                    "    f.write(json.dumps(info) + '\\n')\n" % (sys.executable, log))
        os.chmod(fake, 0o755)
        del self.env["FORGE_NO_BROWSER"]
        run = self.runtime_dir()
        self.env["XDG_RUNTIME_DIR"] = run
        for args in ([], ["--welcome"], ["--dashboard"]):
            with self.subTest(args=args):
                if os.path.exists(log):
                    os.unlink(log)
                self.proc = subprocess.Popen([sys.executable, "-u", APP, *args], env=self.env,
                                             preexec_fn=as_uid(OWNER), stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, text=True)
                ready, _, _ = select.select([self.proc.stdout], [], [], 10)
                self.assertTrue(ready, self.proc.stderr.read() if self.proc.poll() is not None else "no output")
                base = re.search(r"http://127\.0\.0\.1:\d+", self.proc.stdout.readline()).group(0)
                deadline = time.time() + 5
                while time.time() < deadline and not os.path.exists(log):
                    time.sleep(0.1)
                with open(log) as f:
                    info = json.loads(f.readline())
                self.assertNotIn("k=", " ".join(info["argv"]))
                self.assertEqual(len(info["argv"]), 1, info)
                self.assertTrue(info["argv"][0].startswith(os.path.join(run, "forge-settings") + "/"), info)
                self.assertEqual((info.get("mode"), info.get("dir_mode")), (0o600, 0o700), info)
                key = re.search(re.escape(base) + r"/\?k=([A-Za-z0-9_-]+)", info["body"]).group(1)
                time.sleep(0.2)
                status, body = request(OWNER, "GET", f"{base}/?k={key}")
                self.assertEqual(status, 200)
                self.assertTrue(token_of(body))
                # Used up: the page is gone once the browser has the real one.
                time.sleep(0.2)
                self.assertFalse(os.path.exists(info["argv"][0]))
                self.proc.terminate()
                self.proc.wait()
                self.proc.stdout.close()
                self.proc.stderr.close()
                self.proc = None

    def test_refuses_to_run_as_root(self):
        try:
            r = subprocess.run([sys.executable, APP], env=self.env, capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("forge-settings kept running as root")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("normal user", r.stderr)


class ConnectionQueueTest(unittest.TestCase):
    def test_room_for_waiting_connections(self):
        # A small queue lets another user's connection flood delay the owner's page.
        with open(APP) as f:
            self.assertRegex(f.read(), r"request_queue_size = (6[4-9]|[7-9][0-9]|[1-9][0-9]{2,})\b")


class PeerLookupTest(unittest.TestCase):
    """The /proc/net/tcp parser, fed fake files (no root needed)."""

    def setUp(self):
        os.environ.setdefault("FORGE_LIB", LIB)
        loader = importlib.machinery.SourceFileLoader("forge_settings", APP)
        spec = importlib.util.spec_from_loader("forge_settings", loader)
        self.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(self.mod)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self, name, rows):
        head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write(head + "".join(rows))

    @staticmethod
    def row(local, remote, st, uid):
        return f"   0: {local} {remote} {st} 00000000:00000000 00:00000000 00000000 {uid:5d}        0 1 1\n"

    def test_lookup(self):
        lo = "0100007F" if sys.byteorder == "little" else "7F000001"
        mapped = ("0000000000000000FFFF00000100007F" if sys.byteorder == "little"
                  else "00000000000000000000FFFF7F000001")
        self.fake("tcp", [
            self.row(f"{lo}:BB80", f"{lo}:1F90", "01", OTHER),     # 48000 -> 8080, another user
            self.row(f"{lo}:1F90", f"{lo}:BB80", "01", OWNER),     # server side of that connection
            self.row(f"{lo}:BB81", f"{lo}:1F90", "01", OWNER),     # 48001 -> 8080, the owner
            self.row(f"{lo}:BB82", f"{lo}:1F90", "06", 0),         # old closed socket, ignored
        ])
        self.fake("tcp6", [self.row(f"{mapped}:BB83", f"{mapped}:1F90", "01", OWNER)])
        self.mod.PROC_NET = self.tmp
        self.assertEqual(self.mod.peer_uids(48000, 8080), {OTHER})
        self.assertEqual(self.mod.peer_uids(48001, 8080), {OWNER})
        self.assertEqual(self.mod.peer_uids(48002, 8080), set())
        self.assertEqual(self.mod.peer_uids(48003, 8080), {OWNER})
        self.assertEqual(self.mod.peer_uids(48009, 8080), set())
        os.unlink(os.path.join(self.tmp, "tcp6"))
        self.assertEqual(self.mod.peer_uids(48001, 8080), {OWNER})


    def check(self, client, server=("127.0.0.1", 8080)):
        from types import SimpleNamespace
        stub = SimpleNamespace(client_address=client, server=SimpleNamespace(server_address=server))
        with mock.patch.object(self.mod.os, "getuid", return_value=OWNER):
            return self.mod.Handler.peer_ok(stub)

    def test_r4_other_user_cannot_borrow_the_owner_port_from_another_address(self):
        # Second review: the attacker binds a non-loopback address with the same source port
        # as the owner's browser socket, then connects to 127.0.0.1:8080.
        lo = "0100007F" if sys.byteorder == "little" else "7F000001"
        other_ip = "020000C0" if sys.byteorder == "little" else "C0000002"   # 192.0.2.2
        self.fake("tcp", [
            self.row(f"{lo}:BB80", f"{lo}:1F90", "01", OWNER),          # owner's browser 48000 -> 8080
            self.row(f"{other_ip}:BB80", f"{lo}:1F90", "01", OTHER),    # attacker 192.0.2.2:48000 -> 8080
        ])
        self.mod.PROC_NET = self.tmp
        self.assertTrue(self.check(("127.0.0.1", 48000)))
        self.assertFalse(self.check(("192.0.2.2", 48000)))

    def test_r4_ambiguous_or_missing_socket_is_refused(self):
        lo = "0100007F" if sys.byteorder == "little" else "7F000001"
        self.fake("tcp", [self.row(f"{lo}:BB80", f"{lo}:1F90", "01", OWNER),
                          self.row(f"{lo}:BB80", f"{lo}:1F90", "01", OTHER)])
        self.mod.PROC_NET = self.tmp
        self.assertFalse(self.check(("127.0.0.1", 48000)))
        self.assertFalse(self.check(("127.0.0.1", 48001)))


if __name__ == "__main__":
    unittest.main()
