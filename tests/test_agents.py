"""Tests for Forge agents, using a fake policy and a scripted fake chat model."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
sys.path.insert(0, LIB)

from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from forgecore import agents as A  # noqa: E402

EXAMPLE = os.path.join(ROOT, "files/system/usr/share/forge/agents/system-checkup.json")


class FakePolicy:
    """Decisions per action. wait_answer is what wait() returns for any approval."""

    def __init__(self, decisions=None, wait_answer="approved"):
        self.decisions = decisions or {}
        self.wait_answer = wait_answer
        self.checks = []
        self.waits = []
        self.audits = []
        self.ttls = []
        self.expired = []

    def check(self, agent_id, action, details, run_id=None, ttl_s=None):
        self.checks.append((agent_id, action, details, run_id))
        self.ttls.append(ttl_s)
        d = self.decisions.get(action, "allow")
        return {"decision": d, "approval_id": "ap1" if d == "ask" else None}

    def wait(self, approval_id, timeout_s):
        self.waits.append((approval_id, timeout_s))
        return self.wait_answer

    def audit(self, actor, action, decision, details=None, result=None):
        self.audits.append((actor, action, decision))

    def expire(self, approval_id, reason):
        self.expired.append((approval_id, reason))


def tool_call(name, args, cid="c1"):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def answer(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class FakeChat:
    """Returns scripted responses in order; repeats the last one when the script runs out."""

    def __init__(self, script, gate=None):
        self.script = list(script)
        self.calls = []
        self.gate = gate
        self.lock = threading.Lock()

    def __call__(self, payload, caller):
        with self.lock:
            self.calls.append((json.loads(json.dumps(payload)), caller))
            i = min(len(self.calls) - 1, len(self.script) - 1)
        if self.gate is not None:
            self.gate.wait(5)
        return self.script[i]


def wait_for(fn, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.01)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.sys_dir = os.path.join(self.t, "sys")
        self.user_dir = os.path.join(self.t, "user", "agents")
        self.ws_root = os.path.join(self.t, "ws")
        os.makedirs(self.sys_dir)
        self.store = A.AgentStore(self.sys_dir, self.user_dir)
        self.handlers = {
            "get_metrics": lambda: {"cpu": 5},
            "list_models": lambda: [{"name": "qwen3:8b"}],
            "notify": lambda message: "sent",
            "fetch_url": lambda url: "page:" + url,
        }
        self.tools = A.Tools(self.handlers, self.ws_root)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def agent(self, **kw):
        spec = {"id": "helper", "model": "qwen3:8b", "tools": ["get_metrics", "notify", "read_file", "write_file"]}
        spec.update(kw)
        return self.store.save(spec)

    def runner(self, script, policy=None, gate=None, max_parallel=2, is_local=None):
        self.policy = policy or FakePolicy()
        self.chat = FakeChat(script, gate)
        return A.Runner(self.store, self.tools, self.policy, self.chat,
                        os.path.join(self.t, "db", "agents.db"), max_parallel, is_local=is_local)


class StoreTest(Base):
    def test_defaults_filled(self):
        s = self.store.save({"id": "abc", "model": "qwen3:8b"})
        self.assertEqual(s["name"], "abc")
        self.assertEqual(s["max_steps"], 10)
        self.assertEqual(s["approval_timeout_s"], 600)
        self.assertEqual(s["lifecycle"], "on-demand")
        self.assertIsNone(s["interval_min"])
        self.assertTrue(s["enabled"])
        self.assertEqual(s["tools"], [])
        self.assertEqual(s["source"], "user")
        self.assertTrue(os.path.isfile(os.path.join(self.user_dir, "abc.json")))

    def test_validation(self):
        bad = [
            {"id": "A", "model": "m"},
            {"id": "x", "model": "m"},
            {"id": "-ab", "model": "m"},
            {"id": "ok-id"},
            {"id": "ok-id", "model": "m", "tools": ["rm_rf"]},
            {"id": "ok-id", "model": "m", "lifecycle": "forever"},
            {"id": "ok-id", "model": "m", "interval_min": 4},
            {"id": "ok-id", "model": "m", "interval_min": "10"},
            {"id": "ok-id", "model": "m", "max_steps": 0},
            {"id": "ok-id", "model": "m", "max_steps": 51},
            {"id": "ok-id", "model": "m", "enabled": "yes"},
            {"id": "ok-id", "model": "m", "surprise": 1},
            "not a dict",
        ]
        for spec in bad:
            with self.assertRaises(ValueError, msg=spec):
                self.store.save(spec)
        self.assertEqual(self.store.list(), [])

    def test_workspace_in_spec_is_ignored(self):
        s = self.store.save({"id": "abc", "model": "m", "workspace": "/etc"})
        self.assertNotIn("workspace", s)
        self.assertEqual(self.tools.workspace_for(s), os.path.join(self.ws_root, "abc"))

    def test_user_overrides_system_and_system_cannot_be_deleted(self):
        with open(os.path.join(self.sys_dir, "base.json"), "w") as f:
            json.dump({"id": "base", "model": "qwen3:8b", "name": "System one"}, f)
        self.assertEqual(self.store.get("base")["source"], "system")
        self.store.save({"id": "base", "model": "qwen3:14b", "name": "Mine"})
        got = self.store.get("base")
        self.assertEqual((got["source"], got["name"]), ("user", "Mine"))
        self.assertEqual(len(self.store.list()), 1)
        self.assertTrue(self.store.delete("base"))
        self.assertEqual(self.store.get("base")["source"], "system")
        with self.assertRaises(PermissionError):
            self.store.delete("base")
        self.assertFalse(self.store.delete("nope"))

    def test_broken_files_are_skipped(self):
        with open(os.path.join(self.sys_dir, "broken.json"), "w") as f:
            f.write("{nope")
        with open(os.path.join(self.sys_dir, "mismatch.json"), "w") as f:
            json.dump({"id": "other", "model": "m"}, f)
        self.assertEqual(self.store.list(), [])

    def test_example_spec_validates(self):
        with open(EXAMPLE) as f:
            spec = A.validate_spec(json.load(f))
        self.assertEqual(spec["id"], "system-checkup")
        self.assertEqual(spec["model"], "qwen3:8b")
        self.assertEqual(spec["tools"], ["get_metrics", "list_models", "notify"])
        self.assertIsNone(spec["interval_min"])
        store = A.AgentStore(os.path.dirname(EXAMPLE), self.user_dir)
        self.assertEqual(store.get("system-checkup")["source"], "system")


class ToolsTest(Base):
    def setUp(self):
        super().setUp()
        self.spec = {"id": "helper"}
        self.ws = self.tools.workspace_for(self.spec)

    def test_registry(self):
        names = self.tools.available()
        self.assertIn("read_file", names)
        self.assertNotIn("run_command", names)  # no handler given
        self.assertNotIn("set_setting", names)
        schemas = self.tools.schemas(["get_metrics", "run_command", "bogus", "read_file"])
        self.assertEqual([s["function"]["name"] for s in schemas], ["get_metrics", "read_file"])
        self.assertEqual(schemas[1]["function"]["parameters"]["required"], ["path"])
        self.assertEqual(self.tools.action_for("run_command"), "command.run")
        self.assertEqual(self.tools.action_for("write_file"), "files.write")
        self.assertIn({"name": "notify", "action": "notify",
                       "description": A.BUILTIN_TOOLS["notify"][1]}, self.tools.describe())

    def test_call_results(self):
        self.assertEqual(json.loads(self.tools.call("get_metrics", {}, self.spec)), {"cpu": 5})
        self.assertEqual(self.tools.call("notify", {"message": "hi"}, self.spec), "sent")
        self.assertTrue(self.tools.call("notify", {}, self.spec).startswith("Error:"))
        self.assertTrue(self.tools.call("run_command", {"command": "id"}, self.spec).startswith("Error:"))
        boom = A.Tools({"get_metrics": lambda: 1 / 0}, self.ws_root)
        self.assertTrue(boom.call("get_metrics", {}, self.spec).startswith("Error:"))

    def test_fetch_url_schemes(self):
        self.assertEqual(self.tools.call("fetch_url", {"url": "https://x.org"}, self.spec), "page:https://x.org")
        for url in ("file:///etc/passwd", "ftp://x.org", "javascript:alert(1)", "http://", 5):
            self.assertTrue(self.tools.call("fetch_url", {"url": url}, self.spec).startswith("Error:"), url)

    def test_write_then_read(self):
        r = json.loads(self.tools.call("write_file", {"path": "sub/a.txt", "content": "hello"}, self.spec))
        self.assertEqual(r["bytes"], 5)
        self.assertEqual(self.tools.call("read_file", {"path": "sub/a.txt"}, self.spec), "hello")
        self.assertTrue(os.path.isfile(os.path.join(self.ws, "sub", "a.txt")))

    def test_path_sandbox(self):
        outside = os.path.join(self.t, "secret.txt")
        with open(outside, "w") as f:
            f.write("secret")
        for p in ("../secret.txt", "a/../../secret.txt", outside, "/etc/passwd", "~/x", "", ".", "a\x00b"):
            self.assertTrue(self.tools.call("read_file", {"path": p}, self.spec).startswith("Error:"), p)
            self.assertTrue(self.tools.call("write_file", {"path": p, "content": "x"}, self.spec)
                            .startswith("Error:"), p)
        with open(outside) as f:
            self.assertEqual(f.read(), "secret")

    def test_symlink_escape(self):
        os.makedirs(self.ws, exist_ok=True)
        outside_dir = os.path.join(self.t, "outside")
        os.makedirs(outside_dir)
        with open(os.path.join(outside_dir, "s.txt"), "w") as f:
            f.write("secret")
        os.symlink(outside_dir, os.path.join(self.ws, "link"))
        os.symlink(os.path.join(outside_dir, "s.txt"), os.path.join(self.ws, "file-link"))
        for p in ("link/s.txt", "file-link"):
            self.assertTrue(self.tools.call("read_file", {"path": p}, self.spec).startswith("Error:"), p)
            self.assertTrue(self.tools.call("write_file", {"path": p, "content": "x"}, self.spec)
                            .startswith("Error:"), p)
        self.assertTrue(self.tools.call("write_file", {"path": "link/new.txt", "content": "x"}, self.spec)
                        .startswith("Error:"))
        self.assertEqual(os.listdir(outside_dir), ["s.txt"])
        with open(os.path.join(outside_dir, "s.txt")) as f:
            self.assertEqual(f.read(), "secret")

    def test_size_limits(self):
        os.makedirs(self.ws, exist_ok=True)
        with open(os.path.join(self.ws, "big.txt"), "w") as f:
            f.write("x" * (A.READ_MAX + 1))
        self.assertIn("larger", self.tools.call("read_file", {"path": "big.txt"}, self.spec))
        with open(os.path.join(self.ws, "ok.txt"), "w") as f:
            f.write("x" * A.READ_MAX)
        self.assertEqual(len(self.tools.call("read_file", {"path": "ok.txt"}, self.spec)), A.READ_MAX)
        r = self.tools.call("write_file", {"path": "w.txt", "content": "x" * (A.WRITE_MAX + 1)}, self.spec)
        self.assertIn("larger", r)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "w.txt")))
        self.assertTrue(self.tools.call("write_file", {"path": "w.txt", "content": 5}, self.spec)
                        .startswith("Error:"))


class RunnerTest(Base):
    def test_allow_flow(self):
        self.agent(system_prompt="Be brief.")
        r = self.runner([tool_call("get_metrics", {}), answer("All good.")])
        run = r.run_sync("helper", "check the PC")
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["result"], "All good.")
        self.assertEqual([s["type"] for s in run["steps"]], ["model", "tool", "model"])
        self.assertEqual(run["steps"][1]["decision"], "allow")
        self.assertEqual(self.policy.checks[0][:2], ("helper", "metrics.read"))
        self.assertEqual(self.policy.checks[0][2], {"tool": "get_metrics", "args": {}})
        self.assertEqual(self.policy.checks[0][3], run["id"])
        payload, caller = self.chat.calls[1]
        self.assertEqual(caller, "agent:helper")
        self.assertEqual(payload["model"], "qwen3:8b")
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "Be brief."})
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "check the PC"})
        self.assertEqual(payload["messages"][-1], {"role": "tool", "tool_call_id": "c1", "content": '{"cpu": 5}'})
        self.assertEqual({t["function"]["name"] for t in payload["tools"]},
                         {"get_metrics", "notify", "read_file", "write_file"})
        self.assertIsNotNone(run["ended"])
        self.assertEqual(r.list_runs("helper")[0]["id"], run["id"])
        self.assertEqual(r.list_runs("other"), [])

    def last_tool_result(self):
        return self.chat.calls[-1][0]["messages"][-1]["content"]

    def test_deny(self):
        self.agent()
        called = []
        self.handlers["notify"] = lambda message: called.append(message) or "sent"
        self.tools = A.Tools(self.handlers, self.ws_root)
        r = self.runner([tool_call("notify", {"message": "hi"}), answer("ok")],
                        FakePolicy({"notify": "deny"}))
        run = r.run_sync("helper", "t")
        self.assertEqual(run["status"], "done")
        self.assertEqual(self.last_tool_result(), "Not allowed by policy")
        self.assertEqual(run["steps"][1]["decision"], "deny")
        self.assertEqual(called, [])

    def test_ask_approved(self):
        self.agent(approval_timeout_s=30)
        r = self.runner([tool_call("notify", {"message": "hi"}), answer("ok")],
                        FakePolicy({"notify": "ask"}, "approved"))
        run = r.run_sync("helper", "t")
        self.assertEqual(self.last_tool_result(), "sent")
        self.assertEqual(run["steps"][1]["decision"], "ask:approved")
        self.assertEqual(self.policy.waits[0][0], "ap1")
        self.assertLessEqual(self.policy.waits[0][1], 30)

    def test_ttl_passed_and_no_expire_when_answered(self):
        self.agent(approval_timeout_s=42)
        r = self.runner([tool_call("notify", {"message": "hi"}), answer("ok")],
                        FakePolicy({"notify": "ask"}, "approved"))
        r.run_sync("helper", "t")
        self.assertEqual(self.policy.ttls, [42])
        self.assertEqual(self.policy.expired, [])

    def test_expire_on_cancel_and_on_error(self):
        self.agent(approval_timeout_s=30)
        started = threading.Event()

        class SlowPolicy(FakePolicy):
            def wait(self, approval_id, timeout_s):
                started.set()
                time.sleep(0.02)
                return "pending"

        r = self.runner([tool_call("notify", {"message": "hi"}), answer("ok")], SlowPolicy({"notify": "ask"}))
        rid = r.start_run("helper", "t")
        self.assertTrue(started.wait(5))
        r.cancel(rid)
        self.assertTrue(wait_for(lambda: r.get_run(rid)["status"] == "cancelled"))
        self.assertEqual(self.policy.expired, [("ap1", "cancelled")])

        class BrokenPolicy(FakePolicy):
            def wait(self, approval_id, timeout_s):
                raise RuntimeError("boom")

        r = self.runner([tool_call("notify", {"message": "hi"}), answer("ok")], BrokenPolicy({"notify": "ask"}))
        r.run_sync("helper", "t")
        self.assertEqual(self.policy.expired, [("ap1", "error")])
        self.assertEqual(self.last_tool_result(), "No answer in time")

    def test_policy_without_expire_is_fine(self):
        self.agent(approval_timeout_s=1)
        policy = FakePolicy({"notify": "ask"}, "expired")
        policy.expire = None
        r = self.runner([tool_call("notify", {"message": "hi"}), answer("ok")], policy)
        self.assertEqual(r.run_sync("helper", "t")["status"], "done")

    def test_too_many_tool_calls_in_one_step(self):
        self.agent()
        calls = [{"id": f"c{i}", "type": "function", "function": {"name": "get_metrics", "arguments": "{}"}}
                 for i in range(10)]
        many = {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}
        r = self.runner([many, answer("ok")])
        run = r.run_sync("helper", "t")
        tool_msgs = [m for m in self.chat.calls[-1][0]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 10)
        self.assertEqual(len(self.policy.checks), A.MAX_TOOL_CALLS_PER_STEP)
        self.assertEqual([m["content"] for m in tool_msgs[8:]], ["Skipped: too many tool calls in one step"] * 2)
        self.assertEqual(run["status"], "done")

    def test_tool_result_and_conversation_caps(self):
        self.agent()
        self.handlers["get_metrics"] = lambda: "y" * 50000
        self.tools = A.Tools(self.handlers, self.ws_root)
        r = self.runner([tool_call("get_metrics", {}), answer("ok")])
        r.run_sync("helper", "t")
        content = self.last_tool_result()
        self.assertLessEqual(len(content), A.TOOL_RESULT_MAX + 20)
        self.assertTrue(content.endswith("[truncated]"))

        # Each step adds about 20000 chars, so 400000 is passed well before 50 steps.
        self.agent(max_steps=50)
        r = self.runner([tool_call("get_metrics", {})])
        run = r.run_sync("helper", "t")
        self.assertEqual(run["status"], "done")
        self.assertIn("too long", run["result"])
        self.assertLess(len(self.chat.calls), 50)

    def test_steps_written_once_per_model_step(self):
        self.agent()
        calls = [{"id": f"c{i}", "type": "function", "function": {"name": "get_metrics", "arguments": "{}"}}
                 for i in range(5)]
        many = {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}
        r = self.runner([many, answer("ok")])
        writes = []
        orig = r._update
        r._update = lambda run_id, **f: (writes.append(f) if "steps" in f else None) or orig(run_id, **f)
        run = r.run_sync("helper", "t")
        self.assertEqual(len(run["steps"]), 7)
        # One write for the tool step, plus the final write when the run ends.
        self.assertEqual(len(writes), 2)

    def test_long_result_truncated(self):
        self.agent()
        r = self.runner([answer("z" * 30000)])
        run = r.run_sync("helper", "t")
        self.assertLessEqual(len(run["result"]), A.RESULT_MAX + 20)

    def test_prune(self):
        self.agent()
        r = self.runner([answer("ok")])
        old = r.run_sync("helper", "t")["id"]
        new = r.run_sync("helper", "t")["id"]
        r._update(old, ended=time.time() - 40 * 86400)
        r._update(new, ended=time.time() - 5 * 86400)
        self.assertEqual(r.prune(keep_days=30), 1)
        self.assertIsNone(r.get_run(old))
        self.assertIsNotNone(r.get_run(new))
        self.assertEqual(r.prune(keep_days=1), 1)

    def test_scheduler_runs_daily_jobs(self):
        r = self.runner([answer("ok")])
        hits = []
        r.start_scheduler(every_s=0.02, daily=[lambda: hits.append(1)])
        try:
            self.assertTrue(wait_for(lambda: len(hits) == 1))
            time.sleep(0.1)
            self.assertEqual(len(hits), 1)
        finally:
            r.stop_scheduler()

    def test_ask_denied(self):
        self.agent()
        r = self.runner([tool_call("notify", {"message": "hi"}), answer("ok")],
                        FakePolicy({"notify": "ask"}, "denied"))
        r.run_sync("helper", "t")
        self.assertEqual(self.last_tool_result(), "Denied by the user")

    def test_ask_timeout(self):
        self.agent(approval_timeout_s=1)
        r = self.runner([tool_call("notify", {"message": "hi"}), answer("ok")],
                        FakePolicy({"notify": "ask"}, "pending"))
        A.WAIT_CHUNK_S, old = 0.05, A.WAIT_CHUNK_S
        try:
            t0 = time.time()
            run = r.run_sync("helper", "t")
        finally:
            A.WAIT_CHUNK_S = old
        self.assertGreaterEqual(time.time() - t0, 0.9)
        self.assertEqual(self.last_tool_result(), "No answer in time")
        self.assertEqual(run["status"], "done")
        self.assertEqual(self.policy.expired, [("ap1", "timeout")])

    def test_waiting_approval_status(self):
        self.agent(approval_timeout_s=5)
        release = threading.Event()

        class SlowPolicy(FakePolicy):
            def wait(self, approval_id, timeout_s):
                release.wait(timeout_s)
                return "approved" if release.is_set() else "pending"

        r = self.runner([tool_call("notify", {"message": "hi"}), answer("ok")], SlowPolicy({"notify": "ask"}))
        rid = r.start_run("helper", "t")
        self.assertTrue(wait_for(lambda: r.get_run(rid)["status"] == "waiting_approval"))
        release.set()
        self.assertTrue(wait_for(lambda: r.get_run(rid)["status"] == "done"))

    def test_tool_not_in_spec_is_refused_without_policy(self):
        self.agent(tools=["notify"])
        r = self.runner([tool_call("get_metrics", {}), answer("ok")])
        r.run_sync("helper", "t")
        self.assertIn("not available", self.last_tool_result())
        self.assertEqual(self.policy.checks, [])

    def test_bad_arguments(self):
        self.agent()
        bad = {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "notify", "arguments": "{oops"}}]}}]}
        r = self.runner([bad, answer("ok")])
        run = r.run_sync("helper", "t")
        self.assertIn("not a valid JSON", self.last_tool_result())
        self.assertEqual(run["status"], "done")

    def test_cloud_model_needs_ai_cloud(self):
        self.agent(model="openai/gpt-x")
        r = self.runner([answer("hi")], FakePolicy({"ai.cloud": "deny"}))
        run = r.run_sync("helper", "t")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.chat.calls, [])
        self.assertEqual(self.policy.checks[0][1], "ai.cloud")

        r = self.runner([tool_call("get_metrics", {}), answer("hi")], FakePolicy({"ai.cloud": "ask"}, "approved"))
        run = r.run_sync("helper", "t")
        self.assertEqual(run["status"], "done")
        self.assertEqual([c[1] for c in self.policy.checks].count("ai.cloud"), 1)
        self.assertEqual(self.policy.checks[0][1], "ai.cloud")

        r = self.runner([answer("hi")], FakePolicy({"ai.cloud": "ask"}, "denied"))
        self.assertEqual(r.run_sync("helper", "t")["status"], "failed")

    def test_is_local_decides_cloud(self):
        self.agent(model="qwen3:8b")
        r = self.runner([answer("hi")], FakePolicy({"ai.cloud": "deny"}), is_local=lambda m: False)
        self.assertEqual(r.run_sync("helper", "t")["status"], "failed")
        self.agent(model="openai/gpt-x")
        r = self.runner([answer("hi")], FakePolicy({"ai.cloud": "deny"}), is_local=lambda m: True)
        self.assertEqual(r.run_sync("helper", "t")["status"], "done")
        self.agent(model="mystery/model")
        r = self.runner([answer("hi")], FakePolicy({"ai.cloud": "ask"}, "approved"), is_local=lambda m: False)
        self.assertEqual(r.run_sync("helper", "t")["status"], "done")
        self.assertEqual([c[1] for c in self.policy.checks], ["ai.cloud"])

    def test_local_and_other_slash_models_skip_cloud_check(self):
        self.agent(model="hf.co/user/repo:Q4_K_M")
        r = self.runner([answer("hi")], FakePolicy({"ai.cloud": "deny"}))
        self.assertEqual(r.run_sync("helper", "t")["status"], "done")
        self.assertTrue(A.is_cloud_model("anthropic/claude"))
        self.assertTrue(A.is_cloud_model("openrouter/x/y"))
        self.assertFalse(A.is_cloud_model("qwen3:8b"))

    def test_max_steps(self):
        self.agent(max_steps=3)
        r = self.runner([tool_call("get_metrics", {})])
        run = r.run_sync("helper", "t")
        self.assertEqual(run["status"], "done")
        self.assertIn("Stopped after 3 steps", run["result"])
        self.assertEqual(len(self.chat.calls), 3)

    def test_chat_error_fails_run(self):
        self.agent()

        def broken(payload, caller):
            raise RuntimeError("model offline")

        r = A.Runner(self.store, self.tools, FakePolicy(), broken, os.path.join(self.t, "x.db"))
        run = r.run_sync("helper", "t")
        self.assertEqual(run["status"], "failed")
        self.assertIn("model offline", run["error"])

    def test_unknown_and_disabled(self):
        r = self.runner([answer("hi")])
        with self.assertRaises(KeyError):
            r.start_run("nope", "t")
        self.agent(enabled=False)
        with self.assertRaises(A.AgentDisabled):
            r.start_run("helper", "t")

    def test_cancel(self):
        self.agent()
        gate = threading.Event()
        r = self.runner([tool_call("get_metrics", {})], gate=gate)
        rid = r.start_run("helper", "t")
        self.assertTrue(wait_for(lambda: len(self.chat.calls) == 1))
        self.assertTrue(r.cancel(rid))
        gate.set()
        self.assertTrue(wait_for(lambda: r.get_run(rid)["status"] == "cancelled"))
        self.assertLessEqual(len(self.chat.calls), 2)
        self.assertFalse(r.cancel(rid))
        self.assertFalse(r.cancel("missing"))

    def test_queued_when_full(self):
        self.agent()
        gate = threading.Event()
        r = self.runner([answer("ok")], gate=gate, max_parallel=1)
        a = r.start_run("helper", "one")
        self.assertTrue(wait_for(lambda: r.get_run(a)["status"] == "running"))
        b = r.start_run("helper", "two")
        time.sleep(0.1)
        self.assertEqual(r.get_run(b)["status"], "queued")
        c = r.start_run("helper", "three")
        self.assertTrue(r.cancel(c))
        gate.set()
        self.assertTrue(wait_for(lambda: r.get_run(a)["status"] == "done" and r.get_run(b)["status"] == "done"))
        self.assertTrue(wait_for(lambda: r.get_run(c)["status"] == "cancelled"))
        self.assertEqual(len(self.chat.calls), 2)

    def test_tick(self):
        self.agent(id="every10", interval_min=10)
        self.agent(id="manual")
        self.agent(id="off", interval_min=5, enabled=False)
        r = self.runner([answer("ok")])
        first = r.tick(now=1000)
        self.assertEqual(len(first), 1)
        self.assertEqual(r.get_run(first[0])["agent"], "every10")
        self.assertEqual(r.get_run(first[0])["trigger"], "schedule")
        self.assertTrue(wait_for(lambda: r.get_run(first[0])["status"] == "done"))
        self.assertEqual(r.tick(now=1001), [])
        self.assertEqual(r.tick(now=1000 + 599), [])
        self.assertEqual(len(r.tick(now=1000 + 600)), 1)

    def test_tick_skips_agent_still_running(self):
        self.agent(id="every10", interval_min=10)
        gate = threading.Event()
        r = self.runner([answer("ok")], gate=gate)
        self.assertEqual(len(r.tick(now=0)), 1)
        self.assertEqual(r.tick(now=10_000), [])
        gate.set()

    def test_scheduler_thread(self):
        self.agent(id="every10", interval_min=10)
        r = self.runner([answer("ok")])
        r.start_scheduler(every_s=0.05)
        try:
            self.assertTrue(wait_for(lambda: len(r.list_runs("every10")) == 1))
            time.sleep(0.2)
            self.assertEqual(len(r.list_runs("every10")), 1)
        finally:
            r.stop_scheduler()

    def test_history_survives_restart(self):
        self.agent()
        r = self.runner([answer("ok")])
        rid = r.run_sync("helper", "t")["id"]
        r2 = A.Runner(self.store, self.tools, FakePolicy(), FakeChat([answer("x")]),
                      os.path.join(self.t, "db", "agents.db"))
        self.assertEqual(r2.get_run(rid)["result"], "ok")


class RouterTest(Base):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.sys_dir, "base.json"), "w") as f:
            json.dump({"id": "base", "model": "qwen3:8b"}, f)
        self.r = self.runner([answer("done here")])

        def auth(x_forge_token: str = Header(default="")):
            if x_forge_token != "secret":
                raise HTTPException(401, "bad token")

        app = FastAPI()
        app.include_router(A.make_router(self.store, self.r, self.tools, auth))
        self.c = TestClient(app)
        self.h = {"X-Forge-Token": "secret"}

    def test_auth_required_everywhere(self):
        for method, path in (("get", "/api/agents"), ("get", "/api/agents/base"), ("put", "/api/agents/x1"),
                             ("delete", "/api/agents/base"), ("post", "/api/agents/base/run"),
                             ("get", "/api/runs"), ("get", "/api/runs/x"), ("post", "/api/runs/x/cancel"),
                             ("get", "/api/tools")):
            self.assertEqual(getattr(self.c, method)(path).status_code, 401, path)

    def test_crud(self):
        self.assertEqual([a["id"] for a in self.c.get("/api/agents", headers=self.h).json()], ["base"])
        got = self.c.get("/api/agents/base", headers=self.h).json()
        self.assertEqual(got["source"], "system")
        self.assertEqual(got["workspace"], os.path.join(self.ws_root, "base"))
        self.assertEqual(self.c.get("/api/agents/nope", headers=self.h).status_code, 404)

        r = self.c.put("/api/agents/mine", headers=self.h, json={"id": "mine", "model": "qwen3:8b"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["source"], "user")
        self.assertEqual(self.c.put("/api/agents/mine", headers=self.h,
                                    json={"id": "other", "model": "m"}).status_code, 422)
        bad = self.c.put("/api/agents/mine", headers=self.h, json={"id": "mine", "model": "m", "max_steps": 99})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("max steps", bad.json()["detail"])
        # A spec read from GET can be sent straight back.
        self.assertEqual(self.c.put("/api/agents/base", headers=self.h, json=got).status_code, 200)

        self.assertEqual(self.c.delete("/api/agents/mine", headers=self.h).status_code, 200)
        self.assertEqual(self.c.delete("/api/agents/mine", headers=self.h).status_code, 404)
        self.assertEqual(self.c.delete("/api/agents/base", headers=self.h).status_code, 200)  # user override
        self.assertEqual(self.c.delete("/api/agents/base", headers=self.h).status_code, 403)

    def test_run_and_runs(self):
        r = self.c.post("/api/agents/base/run", headers=self.h, json={"task": "hello"})
        self.assertEqual(r.status_code, 200, r.text)
        rid = r.json()["run_id"]
        self.assertTrue(wait_for(lambda: self.c.get(f"/api/runs/{rid}", headers=self.h).json()["status"] == "done"))
        run = self.c.get(f"/api/runs/{rid}", headers=self.h).json()
        self.assertEqual((run["result"], run["trigger"], run["agent"]), ("done here", "manual", "base"))
        self.assertEqual(len(self.c.get("/api/runs?agent=base&limit=5", headers=self.h).json()), 1)
        self.assertEqual(self.c.get("/api/runs?agent=zzz", headers=self.h).json(), [])
        self.assertEqual(self.c.get("/api/runs/missing", headers=self.h).status_code, 404)
        self.assertEqual(self.c.post(f"/api/runs/{rid}/cancel", headers=self.h).json(), {"ok": False})
        self.assertEqual(self.c.post("/api/runs/missing/cancel", headers=self.h).status_code, 404)

        self.assertEqual(self.c.post("/api/agents/nope/run", headers=self.h, json={"task": "x"}).status_code, 404)
        self.assertEqual(self.c.post("/api/agents/base/run", headers=self.h, json={}).status_code, 422)
        self.store.save({"id": "base", "model": "qwen3:8b", "enabled": False})
        self.assertEqual(self.c.post("/api/agents/base/run", headers=self.h, json={"task": "x"}).status_code, 409)

    def test_tools_endpoint(self):
        names = {t["name"]: t["action"] for t in self.c.get("/api/tools", headers=self.h).json()}
        self.assertEqual(names["get_metrics"], "metrics.read")
        self.assertEqual(names["read_file"], "files.read")
        self.assertNotIn("run_command", names)


class RunHistoryRedactionTest(Base):
    def test_stored_steps_and_result_hide_secrets(self):
        self.agent()
        r = self.runner([tool_call("notify", {"message": "password=hunter2SECRET API_KEY=sk-ABCDEF123456"}),
                         answer("done, token=TOKSECRET9")])
        rid = r.start_run("helper", "t")
        self.assertTrue(wait_for(lambda: r.get_run(rid)["status"] in ("done", "failed")))
        raw = json.dumps(r.get_run(rid))
        for secret in ("hunter2SECRET", "sk-ABCDEF123456", "TOKSECRET9"):
            self.assertNotIn(secret, raw)
        # The model itself still got the real text (redaction is only for what is stored).
        sent = json.dumps(self.chat.calls[-1][0])
        self.assertIn("hunter2SECRET", sent)


class RunHistoryRedactionRound2Test(Base):
    def run_one(self, task, args, tool="write_file"):
        self.agent()
        r = self.runner([tool_call(tool, args), answer("ok")])
        rid = r.start_run("helper", task)
        self.assertTrue(wait_for(lambda: r.get_run(rid)["status"] in ("done", "failed")))
        return r, json.dumps(r.get_run(rid))

    def test_task_is_stored_redacted(self):
        _, raw = self.run_one('use password: "hunter2 hunter2" and sk-abcdefghijklmnop1234', {"path": "a", "content": "x"})
        self.assertNotIn("hunter2", raw)
        self.assertNotIn("sk-abcdefghijklmnop1234", raw)

    def test_quoted_values_inside_tool_args_are_hidden(self):
        _, raw = self.run_one("t", {"path": "a.py", "content": 'db_password = "Sup3rSecretValue"\n'})
        self.assertNotIn("Sup3rSecretValue", raw)

    def test_a_cut_summary_cannot_reveal_a_secret(self):
        _, raw = self.run_one("t", {"path": "a", "content": '{"password": "correct horse battery staple ' + "x" * 300})
        self.assertNotIn("correct horse", raw)

    def test_non_standard_numbers_in_tool_args_are_refused(self):
        self.agent()
        bad = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "notify", "arguments": '{"message": NaN}'}}]}}]}
        r = self.runner([bad, answer("ok")])
        rid = r.start_run("helper", "t")
        self.assertTrue(wait_for(lambda: r.get_run(rid)["status"] in ("done", "failed")))
        self.assertEqual(self.policy.checks, [], "a NaN argument reached the policy")
        json.dumps(r.get_run(rid), allow_nan=False)

    def test_deeply_nested_tool_args_do_not_fail_the_run(self):
        self.agent()
        bad = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "notify",
                                                           "arguments": "[" * 100000 + "]" * 100000}}]}}]}
        r = self.runner([bad, answer("ok")])
        rid = r.start_run("helper", "t")
        self.assertTrue(wait_for(lambda: r.get_run(rid)["status"] in ("done", "failed")))
        self.assertEqual(r.get_run(rid)["status"], "done")

    def test_scheduler_cleanup_waits_for_a_trusted_clock(self):
        r = self.runner([answer("ok")])
        pruned = []
        r.prune = lambda *a, **k: pruned.append(1)
        r.start_scheduler(every_s=0.05, prune_ok=lambda: False)
        time.sleep(0.3)
        r.stop_scheduler()
        self.assertEqual(pruned, [])

if __name__ == "__main__":
    unittest.main()


