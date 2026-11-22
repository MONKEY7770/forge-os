"""Tests for the Forge Core API, using a fake collector, fake Ollama and fake forge-apply."""
import json
import os
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
sys.path.insert(0, LIB)
os.environ["FORGE_LIB"] = LIB
os.environ["FORGE_SCHEMA"] = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")

TMP = tempfile.mkdtemp()
os.environ["FORGE_CONF"] = os.path.join(TMP, "settings.json")
os.environ["FORGE_CORE_RUN"] = os.path.join(TMP, "run")
os.environ["FORGE_CORE_DATA"] = os.path.join(TMP, "data")
os.environ["FORGE_AGENTS_SYSTEM"] = os.path.join(ROOT, "files/system/usr/share/forge/agents")
# A fake forge-apply: copies the imported file to show what would be saved.
FAKE_APPLY = os.path.join(TMP, "fake-apply")
with open(FAKE_APPLY, "w") as f:
    f.write("#!/bin/sh\ncp \"$2\" \"%s\"\necho applied\n" % os.path.join(TMP, "applied.json"))
os.chmod(FAKE_APPLY, os.stat(FAKE_APPLY).st_mode | stat.S_IEXEC)
os.environ["FORGE_CORE_APPLY"] = FAKE_APPLY

from fastapi.testclient import TestClient  # noqa: E402
from forgecore import app as core  # noqa: E402
from forgecore import models as M  # noqa: E402

# Another test module may have imported Forge Core first, with its own settings: point this
# module's fake forge-apply in directly, so the test order doesn't matter.
core.APPLY_CMD = [FAKE_APPLY]


class FakeCollector:
    def current(self):
        return {"ts": 1, "vram_total_mb": 12227.0, "mem_total_gb": 62.6, "gpu_temp_c": 50.0}

    def history(self, metric, since, step):
        if metric != "gpu_temp_c":
            raise ValueError("Unknown metric")
        return [{"ts": 0, "avg": 50.0, "max": 55.0}]


class FakeOllama:
    up = True

    def available(self):
        return self.up

    def list(self):
        if not self.up:
            raise M.OllamaError("Ollama is not running")
        return [{"name": "qwen3:8b", "size_bytes": 5_200_000_000}]

    def loaded(self):
        return []

    def pull(self, name):
        yield {"status": "pulling", "percent": 50}
        yield {"status": "success"}

    def delete(self, name):
        self.deleted = name

    def unload(self, name):
        self.unloaded = name


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.ollama = FakeOllama()
        self.app = core.create_app(token="secret", collector=FakeCollector(),
                                   ollama=self.ollama, start_collector=False)
        self.c = TestClient(self.app)
        self.h = {"Authorization": "Bearer secret"}

    def test_health_needs_no_token(self):
        self.assertEqual(self.c.get("/api/health").json()["ok"], True)

    def test_token_required(self):
        self.assertEqual(self.c.get("/api/settings").status_code, 401)
        self.assertEqual(self.c.get("/api/settings", headers={"X-Forge-Token": "wrong"}).status_code, 401)
        self.assertEqual(self.c.get("/api/settings", headers={"X-Forge-Token": "secret"}).status_code, 200)

    def test_empty_token_never_authorizes(self):
        c = TestClient(core.create_app(token="", collector=FakeCollector(), ollama=self.ollama,
                                       start_collector=False))
        self.assertEqual(c.get("/api/settings", headers={"X-Forge-Token": ""}).status_code, 401)

    def test_settings_read_and_filter(self):
        r = self.c.get("/api/settings?tag=usb", headers=self.h).json()
        self.assertTrue(all("usb" in s["tags"] for s in r["settings"]))
        self.assertEqual(self.c.get("/api/settings/core.port", headers=self.h).json()["value"], 47900)
        self.assertEqual(self.c.get("/api/settings/nope", headers=self.h).status_code, 404)

    def test_settings_write_validates_then_applies(self):
        r = self.c.put("/api/settings", headers=self.h, json={"values": {"ai.keep_alive_min": 10}})
        self.assertEqual(r.status_code, 200, r.text)
        applied = json.load(open(os.path.join(TMP, "applied.json")))
        self.assertEqual(applied["ai.keep_alive_min"], 10)
        bad = self.c.put("/api/settings", headers=self.h, json={"values": {"ai.keep_alive_min": 99999}})
        self.assertEqual(bad.status_code, 422)
        unknown = self.c.put("/api/settings", headers=self.h, json={"values": {"x.y": 1}})
        self.assertEqual(unknown.status_code, 422)

    def test_settings_reset(self):
        r = self.c.post("/api/settings/reset", headers=self.h, json={"keys": ["core.port"]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_metrics(self):
        self.assertEqual(self.c.get("/api/metrics/now", headers=self.h).json()["gpu_temp_c"], 50.0)
        self.assertEqual(len(self.c.get("/api/metrics/history?metric=gpu_temp_c", headers=self.h).json()), 1)
        self.assertEqual(self.c.get("/api/metrics/history?metric=nope", headers=self.h).status_code, 404)
        self.assertEqual(self.c.get("/api/metrics/history?metric=gpu_temp_c&since=1", headers=self.h).status_code, 422)

    def test_models_with_fit(self):
        m = self.c.get("/api/models", headers=self.h).json()[0]
        self.assertEqual(m["fit"]["verdict"], "gpu")

    def test_models_when_ollama_down(self):
        self.ollama.up = False
        self.assertEqual(self.c.get("/api/models", headers=self.h).status_code, 503)
        self.assertEqual(self.c.post("/api/models/pull", headers=self.h, json={"name": "qwen3:8b"}).status_code, 503)

    def test_pull_streams_and_rejects_bad_names(self):
        r = self.c.post("/api/models/pull", headers=self.h, json={"name": "qwen3:8b"})
        lines = [json.loads(x) for x in r.text.strip().splitlines()]
        self.assertEqual(lines[-1]["status"], "success")
        bad = self.c.post("/api/models/pull", headers=self.h, json={"name": "x; rm -rf /"})
        self.assertEqual(bad.status_code, 422)

    def test_delete_and_unload(self):
        self.assertEqual(self.c.delete("/api/models/hf.co/user/repo:Q4_K_M", headers=self.h).status_code, 200)
        self.assertEqual(self.ollama.deleted, "hf.co/user/repo:Q4_K_M")
        self.assertEqual(self.c.post("/api/models/qwen3:8b/unload", headers=self.h).status_code, 200)

    def test_fit_endpoint(self):
        r = self.c.post("/api/models/fit", headers=self.h, json={"size_bytes": 30_000_000_000}).json()
        self.assertEqual(r["verdict"], "partial")


if __name__ == "__main__":
    unittest.main()
