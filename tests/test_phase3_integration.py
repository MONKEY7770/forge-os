"""Phase 3 wiring: gateway, policy and agent routes inside Forge Core."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
sys.path.insert(0, LIB)
TMP = tempfile.mkdtemp()
os.environ.setdefault("FORGE_SCHEMA", os.path.join(ROOT, "files/system/usr/share/forge/settings.json"))
os.environ.setdefault("FORGE_CONF", os.path.join(TMP, "settings.json"))
os.environ["FORGE_AGENTS_SYSTEM"] = os.path.join(ROOT, "files/system/usr/share/forge/agents")

from fastapi.testclient import TestClient  # noqa: E402
from forgecore import app as core  # noqa: E402


class FakeCollector:
    def current(self):
        return {"ts": 1, "vram_total_mb": 12227.0, "mem_total_gb": 62.6}


class FakeOllama:
    def available(self):
        return False

    def list(self):
        return []


class Phase3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = core.create_app(token="t", collector=FakeCollector(), ollama=FakeOllama(),
                              start_collector=False, data_dir=os.path.join(TMP, "data"))
        cls.c = TestClient(app)
        cls.h = {"Authorization": "Bearer t"}

    def test_admin_routes_need_token(self):
        for path in ["/api/gateway/keys", "/api/policy", "/api/approvals", "/api/audit",
                     "/api/agents", "/api/runs", "/api/tools", "/api/notices"]:
            self.assertEqual(self.c.get(path).status_code, 401, path)
            self.assertEqual(self.c.get(path, headers=self.h).status_code, 200, path)

    def test_v1_needs_gateway_key_not_admin_token(self):
        self.assertEqual(self.c.get("/v1/models").status_code, 401)
        self.assertEqual(self.c.get("/v1/models", headers=self.h).status_code, 401)
        key = self.c.post("/api/gateway/keys", headers=self.h, json={"name": "phone"}).json()["key"]
        self.assertEqual(self.c.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code, 200)

    def test_example_agent_listed_and_run_command_not_offered(self):
        agents = self.c.get("/api/agents", headers=self.h).json()
        self.assertIn("system-checkup", [a["id"] for a in agents])
        tools = [t["name"] for t in self.c.get("/api/tools", headers=self.h).json()]
        self.assertIn("get_metrics", tools)
        self.assertNotIn("run_command", tools)

    def test_safe_fetch_blocks_local_and_private(self):
        for url in ["http://127.0.0.1:47900/api/health", "http://localhost/", "http://192.168.1.1/",
                    "http://10.0.0.5/", "file:///etc/passwd", "ftp://example.com/", "http://[::1]/"]:
            self.assertTrue(core.safe_fetch(url).startswith("Error"), url)


class SafeFetchTest(unittest.TestCase):
    def test_hidden_ipv4_in_ipv6_blocked(self):
        for url in ["http://[::127.0.0.1]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:10.0.0.1]/",
                    "http://[2002:7f00:1::]/", "http://2130706433/"]:
            self.assertTrue(core.safe_fetch(url).startswith("Error"), url)

    def test_connects_only_to_checked_address(self):
        import socket
        from unittest import mock
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        seen = []

        def fake_connect(addr, timeout=None):
            seen.append(addr)
            raise OSError("stop here")

        with mock.patch.object(core.socket, "getaddrinfo", return_value=public), \
             mock.patch.object(core.socket, "create_connection", side_effect=fake_connect):
            out = core.safe_fetch("http://rebind.example/")
        self.assertTrue(out.startswith("Error"))
        self.assertEqual(seen, [("93.184.216.34", 80)])

    def test_mixed_public_and_private_answers_blocked(self):
        import socket
        from unittest import mock
        mixed = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with mock.patch.object(core.socket, "getaddrinfo", return_value=mixed):
            self.assertIn("not allowed", core.safe_fetch("http://mixed.example/"))


if __name__ == "__main__":
    unittest.main()
