"""Tests for the agent permission system: decisions, approvals, audit log and API routes."""
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "files/system/usr/lib/forge"))

from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from forgecore import policy as P  # noqa: E402

TOKEN = "test-token"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "sub", "dir", "policy.db")
        self.conf = os.path.join(self.tmp, "conf", "agents.json")
        self.p = P.Policy(self.db, self.conf)

    def tearDown(self):
        self.p.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class DecideTests(Base):
    def test_defaults(self):
        self.assertTrue(os.path.exists(self.db))
        self.assertEqual(self.p.get_config(), {"actions": {}, "agents": {}})
        for action, (_, default) in P.ACTIONS.items():
            self.assertEqual(self.p.decide("bot", action), default)

    def test_wal_mode(self):
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        con.close()

    def test_unknown_action_denied(self):
        self.assertEqual(self.p.decide("bot", "nuke.everything"), "deny")
        self.assertEqual(self.p.check("bot", "nuke.everything", {})["decision"], "deny")

    def test_override_precedence(self):
        self.p.set_config({"actions": {"web.fetch": "allow", "files.write": "allow"},
                           "agents": {"bot": {"web.fetch": "ask", "models.pull": "allow"}}})
        self.assertEqual(self.p.decide("bot", "web.fetch"), "ask")      # agent beats global
        self.assertEqual(self.p.decide("other", "web.fetch"), "allow")  # global beats default
        self.assertEqual(self.p.decide("bot", "models.pull"), "allow")  # agent beats default
        self.assertEqual(self.p.decide("bot", "files.write"), "allow")
        self.assertEqual(self.p.decide("bot", "notify"), "allow")       # default

    def test_deny_wins(self):
        self.p.set_config({"actions": {"web.fetch": "deny", "notify": "allow"},
                           "agents": {"bot": {"web.fetch": "allow", "notify": "deny"}}})
        self.assertEqual(self.p.decide("bot", "web.fetch"), "deny")
        self.assertEqual(self.p.decide("bot", "notify"), "deny")
        self.assertEqual(self.p.decide("other", "notify"), "allow")

    def test_missing_and_broken_file(self):
        os.makedirs(os.path.dirname(self.conf))
        with open(self.conf, "w") as f:
            json.dump({"actions": {"notify": "deny"}}, f)
        p2 = P.Policy(self.db, self.conf)
        self.assertEqual(p2.decide("x", "notify"), "deny")
        p2.close()
        with open(self.conf, "w") as f:
            f.write("{not json")
        p3 = P.Policy(self.db, self.conf)
        self.assertEqual(p3.decide("x", "notify"), "allow")
        p3.close()

    def test_broken_file_fails_safe(self):
        os.makedirs(os.path.dirname(self.conf))
        for content in ("{not json", json.dumps({"actions": {"web.fetch": "sure"}}),
                        json.dumps({"actions": {"command.run": "allow", "fake": "allow"}})):
            with self.subTest(content=content):
                with open(self.conf, "w") as f:
                    f.write(content)
                p = P.Policy(self.db, self.conf)
                for action, (_, default) in P.ACTIONS.items():
                    self.assertEqual(p.decide("x", action), "allow" if default == "allow" else "deny")
                self.assertIn("error", p.get_config())
                self.assertEqual(p.check("x", "web.fetch", {})["decision"], "deny")
                # Saving a good config clears the error.
                p.set_config({"actions": {}, "agents": {}})
                self.assertNotIn("error", p.get_config())
                self.assertEqual(p.decide("x", "web.fetch"), "ask")
                p.close()

    def test_config_with_error_key_can_be_saved_back(self):
        self.p.set_config({"actions": {"notify": "ask"}, "agents": {}, "error": "old"})
        self.assertEqual(self.p.get_config(), {"actions": {"notify": "ask"}, "agents": {}})


class ConfigTests(Base):
    def test_validation_errors(self):
        bad = [
            "nope",
            {"actions": {"notify": "maybe"}},
            {"actions": {"fake.action": "allow"}},
            {"agents": {"bot": {"fake.action": "allow"}}},
            {"agents": {"bot": {"notify": "yes"}}},
            {"agents": {"bot": "allow"}},
            {"agents": {"": {"notify": "allow"}}},
            {"agents": []},
            {"actions": []},
            {"extra": {}},
        ]
        for cfg in bad:
            with self.subTest(cfg=cfg):
                with self.assertRaises(ValueError):
                    self.p.set_config(cfg)
        self.assertFalse(os.path.exists(self.conf))

    def test_atomic_write(self):
        cfg = {"actions": {"notify": "ask"}, "agents": {"bot": {"files.write": "allow"}}}
        self.p.set_config(cfg)
        with open(self.conf) as f:
            self.assertEqual(json.load(f), cfg)
        self.assertEqual(stat.S_IMODE(os.stat(self.conf).st_mode), 0o644)
        self.assertEqual(os.listdir(os.path.dirname(self.conf)), ["agents.json"])
        self.assertEqual(self.p.get_config(), cfg)

        # A failed rename leaves the old file and no temp files behind.
        with mock.patch("forgecore.policy.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.p.set_config({"actions": {"notify": "deny"}})
        with open(self.conf) as f:
            self.assertEqual(json.load(f), cfg)
        self.assertEqual(os.listdir(os.path.dirname(self.conf)), ["agents.json"])
        self.assertEqual(self.p.decide("bot", "notify"), "ask")

    def test_get_config_is_a_copy(self):
        cfg = self.p.get_config()
        cfg["actions"]["notify"] = "deny"
        self.assertEqual(self.p.decide("bot", "notify"), "allow")


class ApprovalTests(Base):
    def test_request_and_answer(self):
        aid = self.p.request("bot", "web.fetch", {"url": "https://x"}, run_id="r1")
        self.assertEqual(len(aid), 32)
        a = self.p.get(aid)
        self.assertEqual(a["status"], "pending")
        self.assertEqual(a["agent"], "bot")
        self.assertEqual(a["details"], {"url": "https://x"})
        self.assertEqual(a["run_id"], "r1")
        self.assertEqual(set(a), {"id", "agent", "action", "details", "run_id", "status", "created",
                                  "expires", "decided_by", "decided_at"})
        self.assertEqual([x["id"] for x in self.p.list()], [aid])

        out = self.p.answer(aid, True, "alice")
        self.assertEqual(out["status"], "approved")
        self.assertEqual(out["decided_by"], "alice")
        self.assertIsNotNone(out["decided_at"])
        self.assertEqual(self.p.list(), [])
        self.assertEqual(len(self.p.list("approved")), 1)
        self.assertEqual(len(self.p.list(None)), 1)
        log = self.p.audit_log(actor="alice")
        self.assertEqual(log[0]["decision"], "approved")
        self.assertEqual(log[0]["details"]["approval_id"], aid)

    def test_deny_answer(self):
        aid = self.p.request("bot", "web.fetch", {})
        self.assertEqual(self.p.answer(aid, False, "bob")["status"], "denied")

    def test_answer_twice(self):
        aid = self.p.request("bot", "web.fetch", {})
        self.p.answer(aid, True, "alice")
        with self.assertRaises(ValueError):
            self.p.answer(aid, False, "alice")
        with self.assertRaises(ValueError):
            self.p.answer("missing", True, "alice")
        self.assertIsNone(self.p.get("missing"))

    def test_expire(self):
        aid = self.p.request("bot", "web.fetch", {}, ttl_s=-1)
        self.assertEqual(self.p.get(aid)["status"], "expired")
        self.assertEqual(self.p.list(), [])
        with self.assertRaises(ValueError):
            self.p.answer(aid, True, "alice")
        with self.assertRaises(ValueError):
            self.p.list("weird")

    def test_wait_approved_by_other_thread(self):
        aid = self.p.request("bot", "web.fetch", {})
        t = threading.Timer(0.2, lambda: self.p.answer(aid, True, "alice"))
        t.start()
        start = time.monotonic()
        self.assertEqual(self.p.wait(aid, 5, poll_s=0.05), "approved")
        self.assertLess(time.monotonic() - start, 3)
        t.join()

    def test_wait_timeout(self):
        aid = self.p.request("bot", "web.fetch", {})
        start = time.monotonic()
        self.assertEqual(self.p.wait(aid, 0.3, poll_s=0.05), "pending")
        self.assertGreaterEqual(time.monotonic() - start, 0.25)

    def test_expire_method(self):
        aid = self.p.request("bot", "web.fetch", {}, run_id="r1")
        out = self.p.expire(aid, "runner stopped waiting")
        self.assertEqual(out["status"], "expired")
        self.assertEqual(self.p.get(aid)["status"], "expired")
        log = self.p.audit_log()[0]
        self.assertEqual((log["actor"], log["action"], log["decision"]), ("bot", "web.fetch", "expired"))
        self.assertEqual(log["details"]["reason"], "runner stopped waiting")
        with self.assertRaises(ValueError):
            self.p.expire(aid, "again")
        with self.assertRaises(ValueError):
            self.p.answer(aid, True, "alice")
        with self.assertRaises(ValueError):
            self.p.expire("missing", "x")
        done = self.p.request("bot", "web.fetch", {})
        self.p.answer(done, True, "alice")
        with self.assertRaises(ValueError):
            self.p.expire(done, "late")
        self.assertEqual(self.p.get(done)["status"], "approved")

    def test_approval_details_redacted(self):
        aid = self.p.request("bot", "ai.cloud", {"api_key": "sk-123", "model": "x"})
        self.assertEqual(self.p.get(aid)["details"], {"api_key": "[hidden]", "model": "x"})

    def test_setting_key_stays_visible(self):
        aid = self.p.request("bot", "settings.write", {"key": "ai.keep_alive_min", "value": 10})
        self.assertEqual(self.p.get(aid)["details"], {"key": "ai.keep_alive_min", "value": 10})


class AuditTests(Base):
    def test_redaction_nested(self):
        self.p.audit("bot", "ai.cloud", "allow", {
            "Password": "hunter2", "API_KEY": "k", "model": "qwen",
            "nested": {"auth-token": "t", "deeper": {"client_secret": "s", "ok": 1}},
            "items": [{"token": "t2", "name": "n"}],
        })
        d = self.p.audit_log()[0]["details"]
        self.assertEqual(d["Password"], "[hidden]")
        self.assertEqual(d["API_KEY"], "[hidden]")
        self.assertEqual(d["model"], "qwen")
        self.assertEqual(d["nested"]["auth-token"], "[hidden]")
        self.assertEqual(d["nested"]["deeper"], {"client_secret": "[hidden]", "ok": 1})
        self.assertEqual(d["items"], [{"token": "[hidden]", "name": "n"}])
        con = sqlite3.connect(self.db)
        raw = con.execute("SELECT details FROM audit").fetchone()[0]
        con.close()
        for s in ("hunter2", "sk-", '"k"', '"t"', '"s"', "t2"):
            self.assertNotIn(s, raw)

    def test_redaction_names(self):
        hidden = ["password", "PASSWD", "secret", "Token", "api_key", "API-KEY", "apikey", "access_token",
                  "refresh-token", "client_secret", "Authorization", "private_key", "db_secret",
                  "github_token", "admin_password", "openai_api_key", "OPENAI-API-KEY"]
        shown = ["key", "Key", "keys", "monkey", "key_name", "setting_key", "tokens_used", "max_tokens",
                 "token_count", "keep_alive", "secretary", "password_hint", "author"]
        for name in hidden:
            self.assertEqual(P.clean_details({name: "v"}), {name: "[hidden]"}, name)
        for name in shown:
            self.assertEqual(P.clean_details({name: "v"}), {name: "v"}, name)

    def test_truncation(self):
        self.p.audit("bot", "web.fetch", "allow", {"body": "x" * 5000, "deep": {"list": ["y" * 3000]}},
                     result="z" * 5000)
        e = self.p.audit_log()[0]
        self.assertEqual(len(e["details"]["body"]), 2000)
        self.assertEqual(len(e["details"]["deep"]["list"][0]), 2000)
        self.assertEqual(len(e["result"]), 2000)

    def test_filters_and_order(self):
        self.p.audit("a", "notify", "allow")
        self.p.audit("b", "notify", "allow")
        self.p.audit("a", "web.fetch", "ask")
        log = self.p.audit_log()
        self.assertEqual([(e["actor"], e["action"]) for e in log],
                         [("a", "web.fetch"), ("b", "notify"), ("a", "notify")])
        self.assertIsNone(log[0]["details"])
        self.assertEqual(len(self.p.audit_log(actor="a")), 2)
        self.assertEqual(len(self.p.audit_log(action="notify")), 2)
        self.assertEqual(len(self.p.audit_log(actor="a", action="notify")), 1)
        self.assertEqual(len(self.p.audit_log(limit=1)), 1)
        self.assertEqual(len(self.p.audit_log(since_s=60)), 3)
        with mock.patch("forgecore.policy.time.time", return_value=time.time() - 3600):
            self.p.audit("old", "notify", "allow")
        self.assertEqual(len(self.p.audit_log(since_s=60)), 3)
        self.assertEqual(len(self.p.audit_log()), 4)

    def test_prune(self):
        old = time.time() - 100 * 86400
        with mock.patch("forgecore.policy.time.time", return_value=old):
            self.p.audit("old", "notify", "allow")
            done = self.p.request("bot", "web.fetch", {})
            self.p.answer(done, True, "alice")
            waiting = self.p.request("bot", "web.fetch", {}, ttl_s=10**9)
        self.p.audit("new", "notify", "allow")
        self.p.prune(90)
        self.assertEqual([e["actor"] for e in self.p.audit_log()], ["new"])
        self.assertIsNone(self.p.get(done))
        self.assertEqual(self.p.get(waiting)["status"], "pending")
        self.p.prune(0)
        self.assertEqual(self.p.audit_log(), [])

    def test_prune_uses_indexes(self):
        con = sqlite3.connect(self.db)
        for sql in ("DELETE FROM audit WHERE ts < 1",
                    "DELETE FROM approvals WHERE status != 'pending' AND created < 1",
                    "UPDATE approvals SET status='expired' WHERE status='pending' AND expires < 1"):
            plan = " ".join(r[-1] for r in con.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertIn("USING", plan, sql)
            self.assertNotIn("SCAN approvals\b", plan)
        con.close()

    def test_prune_many_rows_is_fast(self):
        old = time.time() - 100 * 86400
        with mock.patch("forgecore.policy.time.time", return_value=old):
            for _ in range(2000):
                self.p.audit("old", "notify", "allow")
        start = time.monotonic()
        self.p.prune(90)
        self.p.prune(90)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.p.audit_log(), [])


class CheckTests(Base):
    def test_allow(self):
        r = self.p.check("bot", "metrics.read", {"what": "cpu"})
        self.assertEqual(r, {"decision": "allow", "approval_id": None})
        log = self.p.audit_log()
        self.assertEqual((log[0]["actor"], log[0]["action"], log[0]["decision"]), ("bot", "metrics.read", "allow"))
        self.assertEqual(self.p.list(None), [])

    def test_ask_ttl(self):
        r = self.p.check("bot", "web.fetch", {}, ttl_s=60)
        a = self.p.get(r["approval_id"])
        self.assertAlmostEqual(a["expires"] - a["created"], 60, places=3)
        r = self.p.check("bot", "web.fetch", {})
        a = self.p.get(r["approval_id"])
        self.assertAlmostEqual(a["expires"] - a["created"], 900, places=3)
        r = self.p.check("bot", "web.fetch", {}, ttl_s=-1)
        self.assertEqual(self.p.get(r["approval_id"])["status"], "expired")

    def test_ask(self):
        r = self.p.check("bot", "web.fetch", {"url": "https://x", "token": "abc"}, run_id="r9")
        self.assertEqual(r["decision"], "ask")
        a = self.p.get(r["approval_id"])
        self.assertEqual((a["status"], a["run_id"], a["details"]["token"]), ("pending", "r9", "[hidden]"))
        log = self.p.audit_log()[0]
        self.assertEqual(log["decision"], "ask")
        self.assertEqual(log["details"]["approval_id"], r["approval_id"])
        self.assertEqual(log["details"]["details"]["token"], "[hidden]")

    def test_deny(self):
        r = self.p.check("bot", "command.run", {"cmd": "rm -rf /"})
        self.assertEqual(r, {"decision": "deny", "approval_id": None})
        self.assertEqual(self.p.audit_log()[0]["decision"], "deny")
        self.assertEqual(self.p.list(None), [])


class RouterTests(Base):
    def setUp(self):
        super().setUp()

        def auth(x_forge_token: str = Header(default="")):
            if x_forge_token != TOKEN:
                raise HTTPException(401, "Missing or wrong Forge token.")

        app = FastAPI()
        app.include_router(P.make_router(self.p, auth), prefix="/api")
        self.c = TestClient(app)
        self.h = {"X-Forge-Token": TOKEN}

    def test_auth_required(self):
        for method, url in [("get", "/api/policy"), ("put", "/api/policy"), ("get", "/api/approvals"),
                            ("post", "/api/approvals/x/approve"), ("post", "/api/approvals/x/deny"),
                            ("get", "/api/audit")]:
            with self.subTest(url=url):
                self.assertEqual(getattr(self.c, method)(url).status_code, 401)
                r = getattr(self.c, method)(url, headers={"X-Forge-Token": "wrong"})
                self.assertEqual(r.status_code, 401)

    def test_policy_get_put(self):
        r = self.c.get("/api/policy", headers=self.h)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["actions"]), len(P.ACTIONS))
        web = next(a for a in body["actions"] if a["id"] == "web.fetch")
        self.assertEqual((web["default"], web["effective"]), ("ask", "ask"))
        self.assertEqual(web["description"], "Read a web page")

        r = self.c.put("/api/policy", headers=self.h, json={"config": {"actions": {"web.fetch": "allow"}}})
        self.assertEqual(r.status_code, 200)
        web = next(a for a in self.c.get("/api/policy", headers=self.h).json()["actions"] if a["id"] == "web.fetch")
        self.assertEqual(web["effective"], "allow")

        r = self.c.put("/api/policy", headers=self.h, json={"config": {"actions": {"web.fetch": "sure"}}})
        self.assertEqual(r.status_code, 422)
        r = self.c.put("/api/policy", headers=self.h, json={"config": {"actions": {"bogus": "allow"}}})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.p.decide("x", "web.fetch"), "allow")

    def test_approvals_flow(self):
        a1 = self.p.request("bot", "web.fetch", {"url": "u"})
        a2 = self.p.request("bot", "models.pull", {"name": "qwen3:8b"})
        r = self.c.get("/api/approvals", headers=self.h)
        self.assertEqual({a["id"] for a in r.json()}, {a1, a2})

        r = self.c.post(f"/api/approvals/{a1}/approve", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["status"], r.json()["decided_by"]), ("approved", "admin"))
        r = self.c.post(f"/api/approvals/{a2}/deny", headers=self.h, json={"by": "alice"})
        self.assertEqual((r.json()["status"], r.json()["decided_by"]), ("denied", "alice"))

        self.assertEqual(self.c.post(f"/api/approvals/{a1}/deny", headers=self.h).status_code, 409)
        self.assertEqual(self.c.post("/api/approvals/nope/approve", headers=self.h).status_code, 404)
        self.assertEqual(self.c.get("/api/approvals", headers=self.h).json(), [])
        r = self.c.get("/api/approvals?status=approved", headers=self.h)
        self.assertEqual([a["id"] for a in r.json()], [a1])
        self.assertEqual(self.c.get("/api/approvals?status=bad", headers=self.h).status_code, 422)

    def test_audit_endpoint(self):
        self.p.audit("a", "notify", "allow", {"password": "p"})
        self.p.audit("b", "web.fetch", "ask")
        r = self.c.get("/api/audit", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([e["actor"] for e in r.json()], ["b", "a"])
        self.assertEqual(r.json()[1]["details"], {"password": "[hidden]"})
        r = self.c.get("/api/audit?actor=a&action=notify&limit=5&since=60", headers=self.h)
        self.assertEqual(len(r.json()), 1)
        self.assertEqual(len(self.c.get("/api/audit?limit=1", headers=self.h).json()), 1)
        self.assertEqual(self.c.get("/api/audit?limit=0", headers=self.h).status_code, 422)


if __name__ == "__main__":
    unittest.main()
