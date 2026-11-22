"""Tests for the Forge AI gateway, with fake providers (no network)."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "files/system/usr/lib/forge"))

import httpx  # noqa: E402
from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from forgecore import gateway as G  # noqa: E402

SECRET = "sk-test-SUPERSECRET-value-1234"


class FakeProviders:
    """Answers like Ollama and OpenAI. Records every request it gets."""

    def __init__(self):
        self.requests = []
        self.fail = None  # "connect" or an HTTP status code
        self.show = {}    # model -> Ollama /api/show reply (a dict) or an HTTP status code

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        if self.fail == "connect":
            raise httpx.ConnectError("refused", request=request)
        if isinstance(self.fail, int):
            return httpx.Response(self.fail, json={"error": {"message": "boom"}})
        path = request.url.path
        if path == "/api/show":
            model = json.loads(request.content or b"{}").get("model")
            reply = self.show.get(model, {"details": {"format": "gguf", "family": "qwen3"},
                                          "modelfile": "FROM /var/lib/ollama/blobs/sha256-abc\n"})
            if isinstance(reply, int):
                return httpx.Response(reply, json={"error": f"model '{model}' not found"})
            if isinstance(reply, bytes):
                return httpx.Response(200, content=reply)
            return httpx.Response(200, json=reply)
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "qwen3:8b", "object": "model"}]})
        body = json.loads(request.content or b"{}")
        if path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}], "model": body["model"],
                                             "usage": {"prompt_tokens": 3, "total_tokens": 3}})
        if body.get("stream"):
            sse = (b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
                   b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n'
                   b"data: [DONE]\n\n")
            return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "c1", "model": body["model"],
                                         "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 5}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.secrets = G.SecretStore(os.path.join(self.tmp, "sub", "secrets.json"))
        self.fake = FakeProviders()
        t = httpx.MockTransport(self.fake)
        self.db = os.path.join(self.tmp, "db", "gateway.db")
        self.gw = G.Gateway(self.secrets, self.db, transport=t, async_transport=t)


class SecretStoreTests(Base):
    def test_permissions_and_list_hides_values(self):
        self.secrets.set("OPENAI_API_KEY", SECRET)
        mode = stat.S_IMODE(os.stat(self.secrets.path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.secrets.get("OPENAI_API_KEY"), SECRET)
        listed = self.secrets.list()
        self.assertEqual(listed[0]["name"], "OPENAI_API_KEY")
        self.assertEqual(listed[0]["hint"], "..." + SECRET[-4:])
        self.assertNotIn(SECRET, json.dumps(listed))
        self.assertTrue(self.secrets.delete("OPENAI_API_KEY"))
        self.assertFalse(self.secrets.delete("OPENAI_API_KEY"))
        self.assertIsNone(self.secrets.get("OPENAI_API_KEY"))

    def test_name_and_value_validation(self):
        for bad in ["openai", "A", "1ABC", "ABC-DEF", "A" * 65, "ABC DEF"]:
            with self.assertRaises(ValueError, msg=bad):
                self.secrets.set(bad, "x")
        with self.assertRaises(ValueError):
            self.secrets.set("GOOD_NAME", "")
        with self.assertRaises(ValueError):
            self.secrets.set("GOOD_NAME", "abc\ndef")
        self.secrets.set("GOOD_NAME", "short")
        self.assertEqual(self.secrets.list()[0]["hint"], "...")


class RoutingTests(Base):
    def test_routes(self):
        p, m = self.gw.route("qwen3:8b")
        self.assertEqual((p["name"], m), ("ollama", "qwen3:8b"))
        p, m = self.gw.route("hf.co/user/repo:Q4_K_M")
        self.assertEqual((p["name"], m), ("ollama", "hf.co/user/repo:Q4_K_M"))
        with self.assertRaises(LookupError) as e:
            self.gw.route("openai/gpt-x")
        self.assertIn("OPENAI_API_KEY", str(e.exception))
        self.secrets.set("OPENAI_API_KEY", SECRET)
        p, m = self.gw.route("openai/gpt-x")
        self.assertEqual((p["name"], m), ("openai", "gpt-x"))
        self.secrets.set("OPENROUTER_API_KEY", SECRET)
        p, m = self.gw.route("openrouter/meta/llama-3")
        self.assertEqual(m, "meta/llama-3")
        with self.assertRaises(LookupError):
            self.gw.route("")

    def test_providers_status(self):
        st = {p["name"]: p["ready"] for p in self.gw.providers_status()}
        self.assertTrue(st["ollama"])
        self.assertFalse(st["openai"])
        self.secrets.set("OPENAI_API_KEY", SECRET)
        st = {p["name"]: p["ready"] for p in self.gw.providers_status()}
        self.assertTrue(st["openai"])


class KeyTests(Base):
    def test_create_check_revoke(self):
        k = self.gw.create_key("app", rate_per_min=5)
        self.assertTrue(k["key"].startswith("fk-"))
        self.assertEqual(len(k["key"]), 43)
        info = self.gw.check_key(k["key"])
        self.assertEqual(info["id"], k["id"])
        listed = self.gw.list_keys()
        self.assertEqual(listed[0]["last4"], k["key"][-4:])
        self.assertNotIn("key", listed[0])
        self.assertNotIn("hash", listed[0])
        with self.assertRaises(PermissionError):
            self.gw.check_key("fk-wrong")
        with self.assertRaises(PermissionError):
            self.gw.check_key("")
        self.assertTrue(self.gw.revoke_key(k["id"]))
        self.assertFalse(self.gw.revoke_key(k["id"]))
        with self.assertRaises(PermissionError):
            self.gw.check_key(k["key"])
        self.assertTrue(self.gw.list_keys()[0]["revoked"])

    def test_key_not_stored_in_plain_text(self):
        k = self.gw.create_key("app")
        with open(self.db, "rb") as f:
            raw = f.read()
        self.assertNotIn(k["key"].encode(), raw)
        self.assertNotIn(k["key"][3:].encode(), raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)

    def test_rate_window(self):
        self.gw.check_rate("k1", 2)
        self.gw.check_rate("k1", 2)
        with self.assertRaises(OverflowError):
            self.gw.check_rate("k1", 2)
        self.gw.check_rate("k2", 2)  # other keys are separate


class InternalChatTests(Base):
    def test_chat_records_usage(self):
        out = self.gw.chat({"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}]}, caller="agent-a")
        self.assertEqual(out["choices"][0]["message"]["content"], "Hello")
        req = self.fake.requests[-1]
        self.assertEqual(str(req.url), "http://127.0.0.1:11434/v1/chat/completions")
        self.assertNotIn("authorization", req.headers)
        rows = self.gw.usage(group_by="caller")
        self.assertEqual(rows[0]["caller"], "agent-a")
        self.assertEqual((rows[0]["prompt_tokens"], rows[0]["completion_tokens"]), (7, 5))

    def test_chat_cloud_adds_bearer_and_rewrites_model(self):
        self.secrets.set("OPENAI_API_KEY", SECRET)
        self.gw.chat({"model": "openai/gpt-x", "messages": []})
        req = self.fake.requests[-1]
        self.assertEqual(req.headers["authorization"], "Bearer " + SECRET)
        self.assertEqual(json.loads(req.content)["model"], "gpt-x")
        with open(self.db, "rb") as f:
            self.assertNotIn(SECRET.encode(), f.read())

    def test_chat_errors(self):
        self.fake.fail = "connect"
        with self.assertRaises(RuntimeError) as e:
            self.gw.chat({"model": "qwen3:8b", "messages": []})
        self.assertIn("Could not reach", str(e.exception))
        self.fake.fail = 500
        with self.assertRaises(RuntimeError):
            self.gw.chat({"model": "qwen3:8b", "messages": []})
        rows = self.gw.usage()
        self.assertEqual(rows[0]["errors"], 2)


class RouterTests(Base):
    def setUp(self):
        super().setUp()

        def auth(authorization: str = Header(default="")):
            if authorization != "Bearer admin":
                raise HTTPException(401, "no")

        app = FastAPI()
        app.include_router(G.make_v1_router(self.gw))
        app.include_router(G.make_admin_router(self.gw, auth))
        self.c = TestClient(app)
        self.admin = {"Authorization": "Bearer admin"}

    def key(self, **kw):
        k = self.c.post("/api/gateway/keys", json=dict({"name": "app"}, **kw), headers=self.admin).json()
        return {"Authorization": "Bearer " + k["key"]}

    def test_no_key(self):
        r = self.c.post("/v1/chat/completions", json={"model": "qwen3:8b", "messages": []})
        self.assertEqual(r.status_code, 401)
        r = self.c.get("/v1/models", headers={"Authorization": "Bearer fk-nope"})
        self.assertEqual(r.status_code, 401)

    def test_chat_non_streaming_records_usage(self):
        h = self.key()
        r = self.c.post("/v1/chat/completions", json={"model": "qwen3:8b", "messages": []}, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "Hello")
        rows = self.c.get("/api/gateway/usage?group_by=model", headers=self.admin).json()
        self.assertEqual(rows[0]["model"], "qwen3:8b")
        self.assertEqual((rows[0]["prompt_tokens"], rows[0]["completion_tokens"], rows[0]["requests"]), (7, 5, 1))

    def test_streaming_passthrough(self):
        h = self.key()
        with self.c.stream("POST", "/v1/chat/completions",
                           json={"model": "qwen3:8b", "messages": [], "stream": True}, headers=h) as r:
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
            lines = [line for line in r.iter_lines() if line]
        self.assertEqual(lines[0], 'data: {"choices":[{"delta":{"content":"Hi"}}]}')
        self.assertEqual(lines[-1], "data: [DONE]")
        rows = self.gw.usage()
        self.assertEqual((rows[0]["prompt_tokens"], rows[0]["completion_tokens"]), (4, 2))

    def test_embeddings(self):
        h = self.key()
        r = self.c.post("/v1/embeddings", json={"model": "nomic-embed-text", "input": "x"}, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.fake.requests[-1].url.path.endswith("/embeddings"))
        self.assertEqual(self.gw.usage()[0]["prompt_tokens"], 3)

    def test_rate_limit_429(self):
        h = self.key(rate_per_min=2)
        body = {"model": "qwen3:8b", "messages": []}
        self.assertEqual(self.c.post("/v1/chat/completions", json=body, headers=h).status_code, 200)
        self.assertEqual(self.c.post("/v1/chat/completions", json=body, headers=h).status_code, 200)
        r = self.c.post("/v1/chat/completions", json=body, headers=h)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Rate limit", r.json()["error"]["message"])

    def test_cloud_rules(self):
        body = {"model": "openai/gpt-x", "messages": []}
        h = self.key()
        r = self.c.post("/v1/chat/completions", json=body, headers=h)
        self.assertEqual(r.status_code, 400)  # secret missing
        self.assertIn("OPENAI_API_KEY", r.json()["error"]["message"])
        self.secrets.set("OPENAI_API_KEY", SECRET)
        r = self.c.post("/v1/chat/completions", json=body, headers=h)
        self.assertEqual(r.status_code, 403)  # local-only key
        hc = self.key(allow_cloud=True)
        r = self.c.post("/v1/chat/completions", json=body, headers=hc)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(self.fake.requests[-1].content)["model"], "gpt-x")

    def test_models_list(self):
        self.secrets.set("OPENAI_API_KEY", SECRET)
        ids = [m["id"] for m in self.c.get("/v1/models", headers=self.key(allow_cloud=True)).json()["data"]]
        self.assertIn("qwen3:8b", ids)
        self.assertIn("openai/*", ids)
        self.assertNotIn("anthropic/*", ids)
        self.fake.fail = "connect"
        ids = [m["id"] for m in self.c.get("/v1/models", headers=self.key(allow_cloud=True)).json()["data"]]
        self.assertEqual(ids, ["openai/*"])

    def test_provider_unreachable_502(self):
        h = self.key()
        self.fake.fail = "connect"
        r = self.c.post("/v1/chat/completions", json={"model": "qwen3:8b", "messages": []}, headers=h)
        self.assertEqual(r.status_code, 502)
        r = self.c.post("/v1/chat/completions", json={"model": "qwen3:8b", "messages": [], "stream": True}, headers=h)
        self.assertEqual(r.status_code, 502)
        self.fake.fail = 500
        r = self.c.post("/v1/chat/completions", json={"model": "qwen3:8b", "messages": []}, headers=h)
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.gw.usage()[0]["errors"], 3)

    def test_admin_requires_auth(self):
        for method, path in [("get", "/api/gateway/providers"), ("get", "/api/gateway/keys"),
                             ("post", "/api/gateway/keys"), ("delete", "/api/gateway/keys/x"),
                             ("get", "/api/gateway/usage"), ("get", "/api/gateway/secrets"),
                             ("put", "/api/gateway/secrets/OPENAI_API_KEY"),
                             ("delete", "/api/gateway/secrets/OPENAI_API_KEY")]:
            r = self.c.request(method.upper(), path, json={"name": "a", "value": "v"})
            self.assertEqual(r.status_code, 401, path)

    def test_admin_secrets_and_keys(self):
        r = self.c.put("/api/gateway/secrets/OPENAI_API_KEY", json={"value": SECRET}, headers=self.admin)
        self.assertEqual(r.json(), {"ok": True})
        r = self.c.get("/api/gateway/secrets", headers=self.admin)
        self.assertNotIn(SECRET, r.text)
        self.assertEqual(self.c.put("/api/gateway/secrets/bad", json={"value": "x"},
                                    headers=self.admin).status_code, 422)
        self.assertEqual(self.c.delete("/api/gateway/secrets/OPENAI_API_KEY", headers=self.admin).status_code, 200)
        self.assertEqual(self.c.delete("/api/gateway/secrets/OPENAI_API_KEY", headers=self.admin).status_code, 404)
        k = self.c.post("/api/gateway/keys", json={"name": "app"}, headers=self.admin).json()
        listed = self.c.get("/api/gateway/keys", headers=self.admin)
        self.assertNotIn(k["key"], listed.text)
        self.assertEqual(self.c.delete(f"/api/gateway/keys/{k['id']}", headers=self.admin).status_code, 200)
        r = self.c.get("/v1/models", headers={"Authorization": "Bearer " + k["key"]})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.c.get("/api/gateway/usage?group_by=nope", headers=self.admin).status_code, 422)


class SecurityFixTests(Base):
    def test_is_local_and_ollama_cloud_names(self):
        self.assertTrue(self.gw.is_local("qwen3:8b"))
        self.assertTrue(self.gw.is_local("hf.co/user/repo:Q4_K_M"))
        for name in ["gpt-oss:120b-cloud", "glm-4.6:cloud", "qwen3-coder:480b-CLOUD", "deepseek-cloud"]:
            self.assertFalse(self.gw.is_local(name), name)
        self.secrets.set("OPENAI_API_KEY", SECRET)
        self.assertFalse(self.gw.is_local("openai/gpt-x"))
        self.assertFalse(self.gw.is_local("anthropic/claude"))  # secret missing
        no_ollama = G.Gateway(self.secrets, self.db, providers=[p for p in G.DEFAULT_PROVIDERS
                                                                if p["name"] != "ollama"])
        self.assertFalse(no_ollama.is_local("qwen3:8b"))  # unknown provider

    def test_local_key_blocked_from_ollama_cloud(self):
        app = FastAPI()
        app.include_router(G.make_v1_router(self.gw))
        c = TestClient(app)
        local = {"Authorization": "Bearer " + self.gw.create_key("a")["key"]}
        cloud = {"Authorization": "Bearer " + self.gw.create_key("b", allow_cloud=True)["key"]}
        body = {"model": "gpt-oss:120b-cloud", "messages": []}
        self.assertEqual(c.post("/v1/chat/completions", json=body, headers=local).status_code, 403)
        self.assertEqual(c.post("/v1/chat/completions", json=dict(body, stream=True), headers=local).status_code, 403)
        self.assertEqual(c.post("/v1/embeddings", json=body, headers=local).status_code, 403)
        self.assertEqual(c.post("/v1/chat/completions", json=body, headers=cloud).status_code, 200)

    def test_broken_providers_file_never_raises(self):
        path = os.path.join(self.tmp, "providers.json")
        with open(path, "w") as f:
            f.write("{not json")
        provs, warns = G.load_providers_with_warnings(path)
        self.assertEqual([p["name"] for p in provs], [p["name"] for p in G.DEFAULT_PROVIDERS])
        self.assertEqual(len(warns), 1)
        with open(path, "w") as f:
            json.dump({"name": "x"}, f)
        self.assertEqual(len(G.load_providers_with_warnings(path)[1]), 1)
        entries = [
            {"name": "good", "base_url": "http://127.0.0.1:8080/v1/", "local": True},
            {"base_url": "http://a/v1"},
            {"name": "nourl"},
            {"name": "ftp", "base_url": "ftp://a/v1"},
            {"name": "badsecret", "base_url": "https://a/v1", "secret": "lower"},
            "not a dict",
        ]
        with open(path, "w") as f:
            json.dump(entries, f)
        provs, warns = G.load_providers_with_warnings(path)
        names = [p["name"] for p in provs]
        self.assertIn("good", names)
        self.assertEqual(len(warns), 5)
        for bad in ["nourl", "ftp", "badsecret"]:
            self.assertNotIn(bad, names)
        self.assertEqual([p["name"] for p in G.load_providers(path)], names)
        self.assertEqual(G.load_providers(None), G.DEFAULT_PROVIDERS)

    def test_prune(self):
        self.gw.record_usage("k1", "app", "ollama", "old", 1, 1, 200, 1)
        self.gw.record_usage("k1", "app", "ollama", "new", 1, 1, 200, 1)
        with G._Locked(self.gw._db_lock, self.db) as db:
            db.execute("UPDATE usage SET ts = ? WHERE model = 'old'", (int(time.time()) - 100 * 86400,))
        self.assertEqual(self.gw.prune(keep_days=90), 1)
        self.assertEqual([r["model"] for r in self.gw.usage(since_s=365 * 86400)], ["new"])
        self.assertEqual(self.gw.prune(), 0)


class R13CloudAliasTests(Base):
    ALIAS = {"remote_model": "gpt-oss:120b-cloud", "remote_host": "https://ollama.com:443",
             "details": {"format": "", "family": ""}}

    def chats_sent(self):
        return [r for r in self.fake.requests if r.url.path.endswith(("/chat/completions", "/embeddings"))]

    def test_r13_local_alias_of_cloud_model_is_not_local(self):
        self.fake.show["mine"] = self.ALIAS                                   # ollama cp x:120b-cloud mine
        self.fake.show["fmt"] = {"details": {"format": "remote"}}
        self.fake.show["from"] = {"details": {"format": "gguf"}, "modelfile": "# x\nFROM glm-4.6:cloud\n"}
        self.fake.show["parent"] = {"details": {"format": "gguf", "parent_model": "kimi-k2:1t-cloud"}}
        self.fake.show["host"] = {"details": {"format": "gguf"}, "remote_host": "https://x.example"}
        for name in ["mine", "fmt", "from", "parent", "host"]:
            self.assertFalse(self.gw.is_local(name), name)
        self.assertTrue(self.gw.is_local("qwen3:8b"))
        req = [r for r in self.fake.requests if r.url.path == "/api/show"][-1]
        self.assertEqual(str(req.url), "http://127.0.0.1:11434/api/show")
        self.assertEqual(json.loads(req.content), {"model": "qwen3:8b"})

    def test_r13_show_failure_fails_closed(self):
        self.fake.show["ghost"] = 404
        self.fake.show["junk"] = b"not json"
        self.assertFalse(self.gw.is_local("ghost"))
        self.assertFalse(self.gw.is_local("junk"))
        self.fake.fail = "connect"
        self.assertFalse(self.gw.is_local("qwen3:8b"))
        self.fake.fail = 500
        self.assertFalse(self.gw.is_local("qwen3:8b"))

    def test_r13_show_result_cached_300s(self):
        shows = lambda: len([r for r in self.fake.requests if r.url.path == "/api/show"])
        now = time.monotonic()
        with mock.patch("forgecore.gateway.time.monotonic", return_value=now):
            self.assertTrue(self.gw.is_local("qwen3:8b"))
            self.assertTrue(self.gw.is_local("qwen3:8b"))
        self.assertEqual(shows(), 1)
        self.fake.show["qwen3:8b"] = self.ALIAS   # replaced by a cloud alias meanwhile
        with mock.patch("forgecore.gateway.time.monotonic", return_value=now + 299):
            self.assertTrue(self.gw.is_local("qwen3:8b"))
        with mock.patch("forgecore.gateway.time.monotonic", return_value=now + 301):
            self.assertFalse(self.gw.is_local("qwen3:8b"))
        self.assertEqual(shows(), 2)

    def test_r13_v1_gate_blocks_alias_for_local_keys(self):
        self.fake.show["mine"] = self.ALIAS
        app = FastAPI()
        app.include_router(G.make_v1_router(self.gw))
        c = TestClient(app)
        local = {"Authorization": "Bearer " + self.gw.create_key("a")["key"]}
        cloud = {"Authorization": "Bearer " + self.gw.create_key("b", allow_cloud=True)["key"]}
        body = {"model": "mine", "messages": []}
        r = c.post("/v1/chat/completions", json=body, headers=local)
        self.assertEqual(r.status_code, 403)
        self.assertIn("cloud", r.json()["error"]["message"])
        self.assertEqual(c.post("/v1/chat/completions", json=dict(body, stream=True), headers=local).status_code, 403)
        self.assertEqual(c.post("/v1/embeddings", json=body, headers=local).status_code, 403)
        self.assertEqual(self.chats_sent(), [])
        self.assertEqual(c.post("/v1/chat/completions", json=body, headers=cloud).status_code, 200)
        self.assertEqual(c.post("/v1/chat/completions", json={"model": "qwen3:8b", "messages": []},
                                headers=local).status_code, 200)

    def test_r13_v1_gate_clear_error_when_check_fails(self):
        self.fake.show["ghost"] = 404
        app = FastAPI()
        app.include_router(G.make_v1_router(self.gw))
        c = TestClient(app)
        local = {"Authorization": "Bearer " + self.gw.create_key("a")["key"]}
        r = c.post("/v1/chat/completions", json={"model": "ghost", "messages": []}, headers=local)
        self.assertEqual(r.status_code, 404)
        self.assertIn("ghost", r.json()["error"]["message"])
        self.fake.fail = "connect"
        r = c.post("/v1/chat/completions", json={"model": "qwen3:8b", "messages": []}, headers=local)
        self.assertEqual(r.status_code, 502)
        self.assertIn("Could not reach ollama", r.json()["error"]["message"])
        self.assertEqual(self.chats_sent(), [])


class UsageTests(Base):
    def test_grouping(self):
        self.gw.record_usage("k1", "app", "ollama", "qwen3:8b", 10, 5, 200, 100)
        self.gw.record_usage("k1", "app", "ollama", "qwen3:8b", 1, 1, 500, 50)
        self.gw.record_usage("k2", "other", "openai", "gpt-x", 3, 4, 200, 10)
        by_model = {r["model"]: r for r in self.gw.usage(group_by="model")}
        self.assertEqual(by_model["qwen3:8b"]["requests"], 2)
        self.assertEqual(by_model["qwen3:8b"]["errors"], 1)
        self.assertEqual(by_model["qwen3:8b"]["prompt_tokens"], 11)
        self.assertEqual(by_model["qwen3:8b"]["total_tokens"], 17)
        by_provider = {r["provider"]: r["requests"] for r in self.gw.usage(group_by="provider")}
        self.assertEqual(by_provider, {"ollama": 2, "openai": 1})
        by_key = {r["key"]: r["requests"] for r in self.gw.usage(group_by="key")}
        self.assertEqual(by_key, {"k1": 2, "k2": 1})
        by_caller = {r["caller"]: r["completion_tokens"] for r in self.gw.usage(group_by="caller")}
        self.assertEqual(by_caller, {"app": 6, "other": 4})
        with self.assertRaises(ValueError):
            self.gw.usage(group_by="bad")
        self.assertEqual(self.gw.usage(since_s=-10), [])


if __name__ == "__main__":
    unittest.main()
