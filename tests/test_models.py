"""Tests for forgecore.models using a fake Ollama server."""

import http.server
import json
import os
import socket
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, "/home/claude/forge-os-repo/files/system/usr/lib/forge")

from forgecore.models import (  # noqa: E402
    OllamaClient, OllamaError, base_url_from_env, fit, normalize_name,
)

GB = 1024 ** 3


class FakeOllama(http.server.BaseHTTPRequestHandler):
    """Answers like a tiny Ollama. Records the last request body."""

    last = {}

    def log_message(self, *args):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        FakeOllama.last = {"method": self.command, "path": self.path, "body": data}
        return data

    def _send(self, obj, status=200):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/version":
            self._send({"version": "0.12.3"})
        elif self.path == "/api/tags":
            self._send({"models": [{
                "name": "qwen3:8b", "model": "qwen3:8b", "size": 5_200_000_000,
                "modified_at": "2026-11-01T10:00:00Z",
                "details": {"family": "qwen3", "parameter_size": "8.2B",
                            "quantization_level": "Q4_K_M"},
            }]})
        elif self.path == "/api/ps":
            self._send({"models": [{
                "name": "qwen3:8b", "size": 6_000_000_000, "size_vram": 6_000_000_000,
                "expires_at": "2026-11-19T12:00:00Z",
            }]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        body = self._body()
        if self.path == "/api/show":
            if body.get("model") == "missing":
                self._send({"error": "model 'missing' not found"}, 404)
            else:
                self._send({"modelfile": "FROM x", "details": {"family": "qwen3"}})
        elif self.path == "/api/pull":
            if body.get("model") == "broken":
                lines = [{"status": "pulling manifest"},
                         {"error": "pull model manifest: file does not exist"}]
            else:
                lines = [{"status": "pulling manifest"},
                         {"status": "pulling abc", "total": 200, "completed": 0},
                         {"status": "pulling abc", "total": 200, "completed": 50},
                         {"status": "pulling abc", "total": 200, "completed": 200},
                         {"status": "success"}]
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for line in lines:
                self.wfile.write(json.dumps(line).encode() + b"\n")
                self.wfile.flush()
        elif self.path == "/api/generate":
            self._send({"model": body.get("model"), "done": True, "done_reason": "unload"})
        else:
            self._send({"error": "not found"}, 404)

    def do_DELETE(self):
        self._body()
        if self.path == "/api/delete":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send({"error": "not found"}, 404)


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = OllamaClient(f"http://127.0.0.1:{cls.server.server_address[1]}", timeout_s=5)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_available_and_version(self):
        self.assertTrue(self.client.available())
        self.assertEqual(self.client.version(), "0.12.3")

    def test_list(self):
        models = self.client.list()
        self.assertEqual(models, [{
            "name": "qwen3:8b", "size_bytes": 5_200_000_000, "family": "qwen3",
            "parameter_size": "8.2B", "quantization": "Q4_K_M",
            "modified_at": "2026-11-01T10:00:00Z",
        }])

    def test_loaded(self):
        self.assertEqual(self.client.loaded(), [{
            "name": "qwen3:8b", "size_bytes": 6_000_000_000,
            "size_vram_bytes": 6_000_000_000, "expires_at": "2026-11-19T12:00:00Z",
        }])

    def test_show(self):
        info = self.client.show("qwen3:8b")
        self.assertEqual(info["details"]["family"], "qwen3")
        self.assertEqual(FakeOllama.last["body"], {"model": "qwen3:8b"})

    def test_show_missing_raises(self):
        with self.assertRaises(OllamaError) as ctx:
            self.client.show("missing")
        self.assertIn("not found", str(ctx.exception))

    def test_pull_progress(self):
        items = list(self.client.pull("qwen3:8b"))
        self.assertEqual(len(items), 5)
        self.assertNotIn("percent", items[0])
        self.assertEqual([i["percent"] for i in items[1:4]], [0, 25, 100])
        self.assertEqual(items[-1]["status"], "success")
        self.assertEqual(FakeOllama.last["body"], {"model": "qwen3:8b", "stream": True})

    def test_pull_error_line(self):
        gen = self.client.pull("broken")
        self.assertEqual(next(gen)["status"], "pulling manifest")
        with self.assertRaises(OllamaError) as ctx:
            next(gen)
        self.assertIn("file does not exist", str(ctx.exception))

    def test_delete(self):
        self.assertIsNone(self.client.delete("qwen3:8b"))
        self.assertEqual(FakeOllama.last["method"], "DELETE")
        self.assertEqual(FakeOllama.last["body"], {"model": "qwen3:8b"})

    def test_unload(self):
        self.assertIsNone(self.client.unload("qwen3:8b"))
        self.assertEqual(FakeOllama.last["path"], "/api/generate")
        self.assertEqual(FakeOllama.last["body"], {"model": "qwen3:8b", "keep_alive": 0})


class UnavailableTests(unittest.TestCase):
    def setUp(self):
        self.client = OllamaClient(f"http://127.0.0.1:{closed_port()}", timeout_s=2)

    def test_available_false(self):
        self.assertFalse(self.client.available())

    def test_every_method_raises(self):
        calls = [self.client.version, self.client.list, self.client.loaded,
                 lambda: self.client.show("x"), lambda: list(self.client.pull("x")),
                 lambda: self.client.delete("x"), lambda: self.client.unload("x")]
        for call in calls:
            with self.assertRaises(OllamaError) as ctx:
                call()
            self.assertIn("not running", str(ctx.exception))


class NormalizeNameTests(unittest.TestCase):
    def test_accepts(self):
        good = {
            "qwen3:8b": "qwen3:8b",
            "library/llama3.2": "library/llama3.2",
            "user/model:tag": "user/model:tag",
            "  qwen3:8b  ": "qwen3:8b",
            "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M":
                "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
            "huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M":
                "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
        }
        for raw, expected in good.items():
            self.assertEqual(normalize_name(raw), expected, raw)

    def test_rejects(self):
        bad = ["", "qwen3 8b", "../etc/passwd", "a/../b", "qwen3;rm -rf", "x$(id)",
               "a|b", "a`b`", "a&b", "a>b", "/leading", "trailing/", "a:b:c",
               "x" * 201]
        for raw in bad:
            with self.assertRaises(ValueError, msg=raw):
                normalize_name(raw)


class FitTests(unittest.TestCase):
    VRAM = 12227
    RAM = 64

    def test_gpu(self):
        r = fit(5 * GB, self.VRAM, self.RAM)
        self.assertEqual(r["verdict"], "gpu")
        self.assertEqual(r["need_gb"], 6.0)
        self.assertEqual(r["message"], "Runs fully on your GPU (needs about 6.0 GB of 12 GB VRAM).")

    def test_partial(self):
        self.assertEqual(fit(20 * GB, self.VRAM, self.RAM)["verdict"], "partial")

    def test_too_big(self):
        self.assertEqual(fit(40 * GB, self.VRAM, self.RAM)["verdict"], "too_big")

    def test_cpu(self):
        self.assertEqual(fit(10 * GB, None, self.RAM)["verdict"], "cpu")

    def test_cpu_too_big(self):
        self.assertEqual(fit(30 * GB, None, self.RAM)["verdict"], "too_big")

    def test_unknown(self):
        self.assertEqual(fit(0, self.VRAM, self.RAM)["verdict"], "unknown")
        self.assertEqual(fit(5 * GB, None, None)["verdict"], "unknown")

    def test_every_result_has_keys(self):
        r = fit(5 * GB, self.VRAM, self.RAM)
        self.assertEqual(set(r), {"verdict", "need_gb", "message"})


class BaseUrlTests(unittest.TestCase):
    def check(self, value, expected):
        env = {} if value is None else {"OLLAMA_HOST": value}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(base_url_from_env(), expected)

    def test_cases(self):
        self.check(None, "http://127.0.0.1:11434")
        self.check("", "http://127.0.0.1:11434")
        self.check("127.0.0.1:11434", "http://127.0.0.1:11434")
        self.check("http://0.0.0.0:11434", "http://127.0.0.1:11434")
        self.check("0.0.0.0", "http://127.0.0.1:11434")
        self.check("192.168.1.5:9000", "http://192.168.1.5:9000")
        self.check("https://ollama.lan", "https://ollama.lan:11434")


if __name__ == "__main__":
    unittest.main()
