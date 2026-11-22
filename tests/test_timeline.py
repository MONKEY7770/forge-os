"""Activity timeline, update status and update notifications.

Forge Core parts use FastAPI's TestClient with fakes, like test_core_hardening.py.
rpm-ostree output comes from fixtures in tests/data/rpm-ostree (modeled on real
`rpm-ostree status --json` output; see the report for how sure that shape is).
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
BIN = os.path.join(ROOT, "files/system/usr/bin")
LIBEXEC = os.path.join(ROOT, "files/system/usr/libexec")
APPLY = os.path.join(LIBEXEC, "forge-apply")
PUBLISH = os.path.join(LIBEXEC, "forge-activity-publish")
FIXTURES = os.path.join(ROOT, "tests/data/rpm-ostree")
sys.path.insert(0, LIB)
TMP = tempfile.mkdtemp()
os.environ.setdefault("FORGE_SCHEMA", os.path.join(ROOT, "files/system/usr/share/forge/settings.json"))
os.environ.setdefault("FORGE_CONF", os.path.join(TMP, "settings.json"))
os.environ.setdefault("FORGE_CORE_RUN", os.path.join(TMP, "run"))
os.environ["FORGE_AGENTS_SYSTEM"] = os.path.join(ROOT, "files/system/usr/share/forge/agents")

from fastapi.testclient import TestClient  # noqa: E402
from forgecore import app as core  # noqa: E402
from forgecore import updates as U  # noqa: E402
from forgecore import timeline as T  # noqa: E402

IS_ROOT = os.geteuid() == 0


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


def show_text(result="success", status=0, exit_ts="@1794984000", active="inactive", unit="uupd.service"):
    """What `systemctl show --timestamp=unix -p ... uupd.service rpm-ostreed-automatic.service` prints."""
    return (f"Id={unit}\nLoadState=loaded\nActiveState={active}\nResult={result}\n"
            f"ExecMainStatus={status}\nExecMainStartTimestamp=@1794983000\nExecMainExitTimestamp={exit_ts}\n\n"
            "Id=rpm-ostreed-automatic.service\nLoadState=loaded\nActiveState=inactive\nResult=success\n"
            "ExecMainStatus=0\nExecMainStartTimestamp=\nExecMainExitTimestamp=\n")


class FakeRun:
    """Stands in for running rpm-ostree and systemctl. Counts calls."""

    def __init__(self, status="normal.json", show=None, rc=0):
        self.status = status
        self.show = show if show is not None else show_text()
        self.rc = rc
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def __call__(self, argv, timeout):
        with self.lock:
            self.calls.append(list(argv))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if argv[0] == "rpm-ostree":
                self.gate.wait(5)
                if self.status is None:
                    raise FileNotFoundError("rpm-ostree")
                return self.rc, fixture(self.status) if self.status.endswith(".json") else self.status
            if argv[0] == "systemctl":
                return 0, self.show
            return 1, ""
        finally:
            with self.lock:
                self.active -= 1

    def ostree_calls(self):
        return [c for c in self.calls if c[0] == "rpm-ostree"]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# rpm-ostree parsing

class UpdateParseTest(unittest.TestCase):
    def report(self, name, show=None):
        return U.UpdateStatus(run=FakeRun(name, show)).get()

    def test_normal(self):
        r = self.report("normal.json")
        self.assertTrue(r["ok"])
        self.assertEqual(r["booted"]["version"], "45.20261117")
        self.assertEqual(r["booted"]["image"], "ghcr.io/example-owner/forge-os:latest")
        self.assertFalse(r["update_ready"])
        self.assertTrue(r["rollback_available"])
        self.assertEqual(r["rollback"]["version"], "45.20261116")
        self.assertFalse(r["booted_is_rollback"])
        self.assertIn("rpm-ostree rollback", r["recovery"])
        self.assertIn("boot menu", r["recovery"])

    def test_staged_update(self):
        r = self.report("staged.json")
        self.assertTrue(r["update_ready"])
        self.assertEqual(r["staged"]["version"], "45.20261118")
        self.assertIn("restart", r["message"].lower())
        self.assertEqual(r["booted"]["version"], "45.20261117")
        self.assertEqual(r["rollback"]["version"], "45.20261116")
        self.assertFalse(r["booted_is_rollback"])

    def test_rolled_back_with_rpm_ostree_rollback(self):
        r = self.report("rolled-back.json")
        self.assertTrue(r["booted_is_rollback"])
        self.assertEqual(r["booted"]["version"], "45.20261116")
        self.assertTrue(r["rollback_available"])
        self.assertEqual(r["rollback"]["version"], "45.20261117")
        self.assertTrue(r["booted_is_default"])     # rolled back and restarted: it is the default now

    def test_old_entry_picked_in_boot_menu(self):
        r = self.report("boot-menu-old.json")
        self.assertTrue(r["booted_is_rollback"])
        self.assertEqual(r["booted"]["version"], "45.20261116")
        self.assertFalse(r["booted_is_default"])

    def test_single_deployment(self):
        r = self.report("single.json")
        self.assertFalse(r["rollback_available"])
        self.assertIsNone(r["rollback"])
        self.assertFalse(r["booted_is_rollback"])

    def test_last_attempt_from_uupd(self):
        r = self.report("normal.json")
        self.assertEqual(r["last_attempt"]["updater"], "uupd")
        self.assertEqual(r["last_attempt"]["result"], "success")
        self.assertEqual(r["last_attempt"]["ts"], 1794984000)
        r = self.report("normal.json", show_text(result="exit-code", status=1))
        self.assertEqual(r["last_attempt"]["result"], "failed")
        r = self.report("normal.json", show_text(exit_ts="", active="activating"))
        self.assertEqual(r["last_attempt"]["result"], "running")
        r = self.report("normal.json", show_text(exit_ts=""))
        self.assertEqual(r["last_attempt"]["result"], "none")

    def test_falls_back_to_rpm_ostreed_automatic(self):
        text = ("Id=uupd.service\nLoadState=not-found\nActiveState=inactive\nResult=success\n"
                "ExecMainStatus=0\nExecMainStartTimestamp=\nExecMainExitTimestamp=\n\n"
                "Id=rpm-ostreed-automatic.service\nLoadState=loaded\nActiveState=inactive\nResult=exit-code\n"
                "ExecMainStatus=1\nExecMainStartTimestamp=@1794980000\nExecMainExitTimestamp=@1794980100\n")
        r = self.report("normal.json", text)
        self.assertEqual(r["last_attempt"]["updater"], "rpm-ostreed-automatic")
        self.assertEqual(r["last_attempt"]["result"], "failed")

    def test_missing_or_broken_rpm_ostree(self):
        for status, rc in ((None, 0), ("not json {", 0), ('{"deployments": "x"}', 0), ("[]", 0),
                           (fixture("normal.json"), 1), ('{"deployments": [{"booted": "yes"}]}', 0)):
            r = U.UpdateStatus(run=FakeRun(status, rc=rc)).get()
            self.assertFalse(r["ok"], status)
            self.assertTrue(r["error"])
            self.assertIn("rpm-ostree rollback", r["recovery"])
            self.assertFalse(r["update_ready"])

    def test_hostile_strings_are_cleaned_and_cut(self):
        data = json.loads(fixture("normal.json"))
        data["deployments"][0]["version"] = "45\x1b[2J\u202e" + "v" * 5000
        data["deployments"][0]["container-image-reference"] = "ostree-image-signed:docker://x/" + "y" * 5000
        r = U.UpdateStatus(run=FakeRun(json.dumps(data))).get()
        self.assertTrue(r["ok"])
        for bad in ("\x1b", "\u202e"):
            self.assertNotIn(bad, r["booted"]["version"])
        self.assertLessEqual(len(r["booted"]["version"]), 200)
        self.assertLessEqual(len(r["booted"]["reference"]), 300)

    def test_huge_output_is_refused(self):
        r = U.UpdateStatus(run=FakeRun(" " * (U.MAX_OUTPUT + 10) + "{}")).get()
        self.assertFalse(r["ok"])

    def test_commands_are_argv_lists_without_root(self):
        run = FakeRun()
        U.UpdateStatus(run=run).get()
        self.assertIn(["rpm-ostree", "status", "--json"], run.calls)
        for c in run.calls:
            self.assertNotIn(c[0], ("sudo", "pkexec"))
            self.assertNotIn("journalctl", c[0])

    def test_real_runner_uses_no_shell(self):
        seen = {}

        def fake(argv, **kw):
            seen.update(kw, argv=argv)
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        with mock.patch.object(U.subprocess, "run", fake):
            U._run(["rpm-ostree", "status", "--json"], 30)
        self.assertNotIn("shell", seen)
        self.assertEqual(seen["timeout"], 30)
        self.assertEqual(seen["env"]["LC_ALL"], "C")


def settle(st):
    """Wait for a background refresh to finish."""
    for _ in range(100):
        if st._refreshing is None:
            return
        time.sleep(0.05)
    raise AssertionError("refresh did not finish")


class UpdateCacheTest(unittest.TestCase):
    def test_reused_for_60_seconds(self):
        run, clock = FakeRun(), Clock()
        st = U.UpdateStatus(run=run, clock=clock)
        for _ in range(5):
            st.get()
        self.assertEqual(len(run.ostree_calls()), 1)
        clock.now += 59
        st.get()
        self.assertEqual(len(run.ostree_calls()), 1)
        clock.now += 2
        st.get()
        settle(st)
        self.assertEqual(len(run.ostree_calls()), 2)

    def test_stale_result_is_served_while_one_refresh_runs(self):
        run, clock = FakeRun(), Clock()
        st = U.UpdateStatus(run=run, clock=clock)
        first = st.get()
        self.assertTrue(first["ok"])
        clock.now += 61
        run.gate.clear()   # rpm-ostree now hangs
        start = time.monotonic()
        for _ in range(5):
            self.assertEqual(st.get()["booted"], first["booted"])
        self.assertLess(time.monotonic() - start, 1.0)
        for _ in range(40):
            if len(run.ostree_calls()) == 2:
                break
            time.sleep(0.05)
        st.get()
        self.assertEqual(len(run.ostree_calls()), 2)   # one refresh at a time
        run.gate.set()
        settle(st)
        st.get()
        self.assertEqual(len(run.ostree_calls()), 2)   # fresh again

    def test_a_refresh_that_dies_oddly_does_not_block_later_ones(self):
        run, clock = FakeRun(), Clock()
        st = U.UpdateStatus(run=run, clock=clock)
        real = st._build
        calls = []

        def dying():
            calls.append(1)
            if len(calls) == 1:
                raise SystemExit("odd")
            return real()
        st._build = dying
        st.get(wait=2)
        for _ in range(40):
            if st._refreshing is None:
                break
            time.sleep(0.05)
        self.assertIsNone(st._refreshing)
        self.assertTrue(st.get(wait=2)["ok"])

    def test_first_call_waits_only_briefly(self):
        run = FakeRun()
        run.gate.clear()
        st = U.UpdateStatus(run=run, first_wait=0.3)
        start = time.monotonic()
        r = st.get()
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertFalse(r["ok"])
        self.assertIn("still checking", r["error"].lower())
        run.gate.set()
        settle(st)
        full = st.get()
        self.assertTrue(full["ok"])
        self.assertEqual(set(r), set(full))
        self.assertEqual(T.update_entries(r), [])

    def test_failures_are_cached_too(self):
        run, clock = FakeRun(None), Clock()
        st = U.UpdateStatus(run=run, clock=clock)
        for _ in range(5):
            self.assertFalse(st.get()["ok"])
        self.assertEqual(len(run.calls), 2)   # one rpm-ostree try, one systemctl

    def test_clock_going_backwards_refreshes(self):
        run, clock = FakeRun(), Clock()
        st = U.UpdateStatus(run=run, clock=clock)
        st.get()
        clock.now -= 500
        st.get()
        settle(st)
        self.assertEqual(len(run.ostree_calls()), 2)

    def test_one_rpm_ostree_at_a_time(self):
        run = FakeRun()
        run.gate.clear()
        st = U.UpdateStatus(run=run)
        out = []
        ts = [threading.Thread(target=lambda: out.append(st.get()["ok"])) for _ in range(8)]
        for t in ts:
            t.start()
        time.sleep(0.3)
        run.gate.set()
        for t in ts:
            t.join(10)
        self.assertEqual(out, [True] * 8)
        self.assertEqual(len(run.ostree_calls()), 1)
        self.assertEqual(run.max_active, 1)

    def test_callers_get_their_own_copy(self):
        st = U.UpdateStatus(run=FakeRun())
        st.get()["booted"]["version"] = "changed"
        self.assertEqual(st.get()["booted"]["version"], "45.20261117")


class UpdateSummaryTest(unittest.TestCase):
    """What forge-health reads: one key per thing it notifies about once."""

    def test_keys(self):
        s = U.summary(U.UpdateStatus(run=FakeRun("staged.json")).get())
        self.assertEqual(s["staged"], "c" * 64)
        self.assertEqual(s["staged_version"], "45.20261118")
        self.assertEqual(s["failed"], "")
        self.assertEqual(s["rolled_back"], "")
        s = U.summary(U.UpdateStatus(run=FakeRun("rolled-back.json", show_text("exit-code", 1))).get())
        self.assertEqual(s["staged"], "")
        self.assertEqual(s["rolled_back"], "a" * 64)
        self.assertEqual(s["failed"], "uupd@1794984000")

    def test_broken_output_gives_empty_keys(self):
        s = U.summary(U.UpdateStatus(run=FakeRun(None)).get())
        self.assertEqual((s["staged"], s["rolled_back"]), ("", ""))


# Forge Core endpoints

class FakeCollector:
    def current(self):
        return {"ts": 1}

    def start(self):
        pass

    def stop(self):
        pass


class FakeOllama:
    def available(self):
        return False

    def list(self):
        return []


class FakeRunner:
    def __init__(self, runs=None, broken=False):
        self.runs = runs or []
        self.broken = broken

    def list_runs(self, agent_id=None, limit=50):
        if self.broken:
            raise RuntimeError("database is locked /var/lib/forge/agents.db")
        return self.runs[:limit]

    def start_scheduler(self, **k):
        pass

    def stop_scheduler(self):
        pass


SECRET = "sk-" + "Z" * 30


class TimelineBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.activity = os.path.join(self.dir, "activity")
        os.makedirs(self.activity)
        self.settings_file = os.path.join(self.dir, "timeline", "settings.jsonl")
        os.makedirs(os.path.dirname(self.settings_file))
        self.hw = os.path.join(self.dir, "hw")
        os.makedirs(self.hw)
        self.run = FakeRun("staged.json")
        self.status = U.UpdateStatus(run=self.run)
        self.runner = FakeRunner([
            {"id": "r1", "agent": "system-checkup", "status": "finished", "trigger": "manual",
             "result": f"All good. password=hunter2 key {SECRET}", "error": None,
             "started": 1794900000.0, "ended": 1794900100.0, "steps": [], "task": "check"},
            {"id": "r2", "agent": "system-checkup", "status": "failed", "trigger": "schedule",
             "result": None, "error": "Model not found", "started": 1794800000.0, "ended": None,
             "steps": [], "task": "check"}])
        patches = [mock.patch.object(T, "ACTIVITY_DIR", self.activity),
                   mock.patch.object(T, "SETTINGS_TIMELINE", self.settings_file),
                   mock.patch.object(T, "HW_NOTICE", os.path.join(self.hw, "hardware-notice.json")),
                   mock.patch.object(T, "TRUSTED_UID", os.geteuid()),
                   mock.patch.object(U, "STATUS", self.status)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = core.create_app(token="admin-t", viewer_token="view-t", collector=FakeCollector(),
                                   ollama=FakeOllama(), start_collector=False, runner=self.runner,
                                   data_dir=os.path.join(self.dir, "data"))
        self.c = TestClient(self.app)
        self.admin = {"Authorization": "Bearer admin-t"}
        self.viewer = {"Authorization": "Bearer view-t"}
        self.uid = os.geteuid()

    def write_activity(self, lines, uid=None, name=None):
        uid = self.uid if uid is None else uid
        p = os.path.join(self.activity, name or f"{uid}.jsonl")
        with open(p, "w") as f:
            for item in lines:
                f.write((item if isinstance(item, str) else json.dumps(item)) + "\n")
        os.chmod(p, 0o644)
        return p

    def write_settings(self, entries):
        with open(self.settings_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def write_hw(self, notice):
        with open(os.path.join(self.hw, "hardware-notice.json"), "w") as f:
            json.dump(notice, f)

    def timeline(self, query="", headers=None):
        r = self.c.get("/api/timeline" + query, headers=headers or self.viewer)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def fill_all(self):
        self.write_activity([{"ts": 1794950000.0, "kind": "mode", "mode": "gaming"},
                             {"ts": 1794950100.0, "kind": "health", "text": "Disk / is 95% full"},
                             {"ts": 1794950200.0, "kind": "backup", "ok": False, "text": "Backup failed: disk full"}])
        self.write_settings([{"ts": 1794940000.0, "by": "alice", "action": "import",
                              "keys": ["ai.keep_alive_min", "security.usb_key_label"],
                              "restart": {"boot": ["security.usb_key_label"], "login": []}}])
        self.write_hw({"ts": 1794930000, "changes": ["RAM: 32 GB -> 64 GB"], "reapplied": True, "pending": False})


class TimelineTest(TimelineBase):
    def test_merges_every_source_newest_first(self):
        self.fill_all()
        out = self.timeline()
        kinds = {e["kind"] for e in out["entries"]}
        self.assertTrue({"mode", "health", "backup", "settings", "hardware", "agent", "update"} <= kinds, kinds)
        ts = [e["ts"] for e in out["entries"]]
        self.assertEqual(ts, sorted(ts, reverse=True))
        for e in out["entries"]:
            self.assertEqual(set(e), {"ts", "kind", "title", "detail"})
            self.assertIsInstance(e["title"], str)
            self.assertIsInstance(e["detail"], str)
        mode = [e for e in out["entries"] if e["kind"] == "mode"][0]
        self.assertIn("gaming", mode["title"])
        settings = [e for e in out["entries"] if e["kind"] == "settings"][0]
        self.assertIn("alice", settings["title"])
        self.assertIn("security.usb_key_label", settings["detail"])
        self.assertIn("restart", settings["detail"].lower())
        upd = [e for e in out["entries"] if e["kind"] == "update"]
        self.assertTrue(any("restart" in e["title"].lower() for e in upd), upd)
        self.assertEqual(set(out["sources"]), {"activity", "settings", "hardware", "agents", "updates"})
        self.assertTrue(all(v == "ok" for v in out["sources"].values()), out["sources"])

    def test_limit(self):
        self.fill_all()
        self.assertEqual(len(self.timeline("?limit=2")["entries"]), 2)
        newest = self.timeline("?limit=1")["entries"][0]
        self.assertEqual(newest["ts"], max(e["ts"] for e in self.timeline()["entries"]))
        for bad in ("0", "-1", "501", "abc"):
            self.assertEqual(self.c.get("/api/timeline?limit=" + bad, headers=self.viewer).status_code, 422, bad)

    def test_since(self):
        self.fill_all()
        out = self.timeline("?since=1794950000")
        self.assertTrue(out["entries"])
        self.assertTrue(all(e["ts"] > 1794950000 for e in out["entries"]))
        self.assertNotIn("mode", {e["kind"] for e in out["entries"]})
        for bad in ("-5", "nan", "abc", "inf"):
            self.assertEqual(self.c.get("/api/timeline?since=" + bad, headers=self.viewer).status_code, 422, bad)

    def test_viewer_may_read_but_not_without_token(self):
        self.assertEqual(self.c.get("/api/timeline").status_code, 401)
        self.assertEqual(self.c.get("/api/timeline", headers={"X-Forge-Token": "nope"}).status_code, 401)
        self.assertEqual(self.c.get("/api/timeline", headers=self.viewer).status_code, 200)
        self.assertEqual(self.c.get("/api/timeline", headers=self.admin).status_code, 200)
        for method in ("POST", "PUT", "DELETE"):
            self.assertIn(self.c.request(method, "/api/timeline", headers=self.viewer).status_code, (403, 405))

    def test_missing_sources(self):
        self.status = U.UpdateStatus(run=FakeRun(None, show=""))
        with mock.patch.object(U, "STATUS", self.status):
            app = core.create_app(token="admin-t", viewer_token="view-t", collector=FakeCollector(),
                                  ollama=FakeOllama(), start_collector=False, runner=FakeRunner(),
                                  data_dir=os.path.join(self.dir, "data2"))
        shutil.rmtree(self.activity)
        out = TestClient(app).get("/api/timeline", headers=self.viewer).json()
        self.assertEqual(out["entries"], [])
        self.assertEqual(out["sources"]["activity"], "missing")
        self.assertEqual(out["sources"]["settings"], "missing")
        self.assertEqual(out["sources"]["hardware"], "missing")
        self.assertEqual(out["sources"]["updates"], "error")

    def test_broken_sources_do_not_break_the_rest(self):
        self.fill_all()
        with open(self.settings_file, "w") as f:
            f.write("not json\n{\"ts\": \"x\"}\n[1,2]\n" + json.dumps({"ts": 1794940500.0, "by": "bob",
                                                                    "action": "undo", "keys": ["a.b"]}) + "\n")
        with open(os.path.join(self.hw, "hardware-notice.json"), "w") as f:
            f.write("{broken")
        self.write_activity(["garbage", "{}", json.dumps({"ts": "soon", "kind": "mode"}),
                             json.dumps({"ts": 1e300, "kind": "mode", "mode": "gaming"}),
                             json.dumps({"ts": 1794950000.0, "kind": "mode", "mode": "quiet"}),
                             json.dumps({"ts": 1794950001.0, "kind": "nope", "text": "x"})])
        self.runner.broken = True
        r = self.c.get("/api/timeline", headers=self.viewer)
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["sources"]["hardware"], "error")
        self.assertEqual(out["sources"]["agents"], "error")
        self.assertNotIn("agents.db", r.text)
        kinds = [e["kind"] for e in out["entries"]]
        self.assertEqual(kinds.count("mode"), 1)
        self.assertEqual(kinds.count("settings"), 1)
        self.assertIn("update", kinds)

    def test_redaction(self):
        self.write_activity([{"ts": 1794950100.0, "kind": "health",
                              "text": f"Failed services: x.service token={SECRET} https://u:pw123456@example.com/"}])
        self.write_settings([{"ts": 1794940000.0, "by": "alice", "action": "import",
                              "keys": ["gateway.api_key"], "restart": {}, "values": {"x": "hunter2"}}])
        body = self.c.get("/api/timeline", headers=self.viewer).text
        for secret in (SECRET, "hunter2", "pw123456"):
            self.assertNotIn(secret, body)
        self.assertIn("[hidden]", body)

    def test_entries_are_cut_and_control_characters_removed(self):
        self.write_activity([{"ts": 1794950100.0, "kind": "health", "text": "a\x1b[2J\u202eb" + "x" * 50000}])
        e = [e for e in self.timeline()["entries"] if e["kind"] == "health"][0]
        self.assertNotIn("\x1b", e["detail"])
        self.assertNotIn("\u202e", e["detail"])
        self.assertLessEqual(len(e["detail"]), T.DETAIL_MAX)

    def test_activity_file_must_belong_to_its_uid(self):
        other = 54321 if self.uid != 54321 else 54322
        self.write_activity([{"ts": 1794950000.0, "kind": "mode", "mode": "gaming"}], name=f"{other}.jsonl")
        self.write_activity([{"ts": 1794950000.0, "kind": "mode", "mode": "quiet"}], name="notes.jsonl")
        self.assertEqual([e for e in self.timeline()["entries"] if e["kind"] == "mode"], [])

    def test_links_are_not_followed(self):
        target = os.path.join(self.dir, "elsewhere.jsonl")
        with open(target, "w") as f:
            f.write(json.dumps({"ts": 1794950000.0, "kind": "mode", "mode": "gaming"}) + "\n")
        os.symlink(target, os.path.join(self.activity, f"{self.uid}.jsonl"))
        os.symlink(target, self.settings_file)
        out = self.timeline()
        self.assertEqual([e for e in out["entries"] if e["kind"] in ("mode", "settings")], [])

    @unittest.skipUnless(IS_ROOT, "needs root to give the file another owner")
    def test_settings_timeline_must_be_owned_by_root(self):
        self.write_settings([{"ts": 1794940000.0, "by": "mallory", "action": "import", "keys": ["x"]}])
        os.chown(self.settings_file, 65534, 65534)
        out = self.timeline()
        self.assertEqual([e for e in out["entries"] if e["kind"] == "settings"], [])
        self.assertEqual(out["sources"]["settings"], "error")

    def test_only_the_tail_of_a_huge_file_is_read(self):
        lines = [{"ts": 1794000000.0 + i, "kind": "mode", "mode": "gaming"} for i in range(20000)]
        self.write_activity(lines)
        out = self.timeline("?limit=500")
        modes = [e for e in out["entries"] if e["kind"] == "mode"]
        self.assertTrue(modes)
        self.assertEqual(modes[0]["ts"], 1794000000.0 + 19999)
        self.assertLess(len(modes), 20000)

    def own_file(self, name, uid, lines, mtime=None):
        p = self.write_activity(lines, name=name)
        os.chown(p, uid, uid)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    @unittest.skipUnless(IS_ROOT, "needs root to give files other owners")
    def test_decoy_names_cannot_hide_other_users(self):
        # uid 1 (daemon) fills the first 64 sorted names; nobody's (65534) real file must still be read.
        self.own_file("65534.jsonl", 65534, [{"ts": 1794950000.0, "kind": "mode", "mode": "gaming"}])
        for i in range(70):
            self.own_file(f"{i:010d}.jsonl", 1, [])
        self.own_file("0000000001.jsonl", 1, [])
        self.own_file("01.jsonl", 1, [{"ts": 1794950001.0, "kind": "mode", "mode": "quiet"}])
        out = self.timeline()
        titles = [e["title"] for e in out["entries"] if e["kind"] == "mode"]
        self.assertEqual(titles, ["nobody switched to gaming mode"])
        self.assertEqual(out["sources"]["activity"], "ok")

    @unittest.skipUnless(IS_ROOT, "needs root to give files other owners")
    def test_uid_without_an_account_is_skipped(self):
        ghost = 4000000
        while True:
            try:
                import pwd
                pwd.getpwuid(ghost)
                ghost += 1
            except KeyError:
                break
        self.own_file(f"{ghost}.jsonl", ghost, [{"ts": 1794950000.0, "kind": "mode", "mode": "gaming"}])
        self.assertEqual([e for e in self.timeline()["entries"] if e["kind"] == "mode"], [])

    @unittest.skipUnless(IS_ROOT, "needs root to give files other owners")
    def test_newest_files_first_and_partial_when_skipped(self):
        self.own_file("65534.jsonl", 65534, [{"ts": 1794950000.0, "kind": "mode", "mode": "gaming"}], mtime=2000000000)
        self.own_file("1.jsonl", 1, [{"ts": 1794950001.0, "kind": "mode", "mode": "quiet"}], mtime=1000000000)
        with mock.patch.object(T, "MAX_USERS", 1):
            out = self.timeline()
        self.assertEqual([e["title"] for e in out["entries"] if e["kind"] == "mode"], ["nobody switched to gaming mode"])
        self.assertEqual(out["sources"]["activity"], "partial")

    @unittest.skipUnless(IS_ROOT, "needs root to give files other owners")
    def test_one_user_cannot_flood_the_timeline(self):
        self.fill_all()
        self.own_file("65534.jsonl", 65534, [{"ts": 9999999999, "kind": "mode", "mode": "gaming"}] * 600 +
                      [{"ts": 1794950000.0 + i, "kind": "mode", "mode": "quiet"} for i in range(600)])
        before = time.time()
        out = self.timeline("?limit=500")
        mine = [e for e in out["entries"] if "nobody" in e["title"]]
        self.assertLessEqual(len(mine), T.PER_USER)
        self.assertTrue(all(e["ts"] <= before + T.FUTURE_MARGIN + 60 for e in out["entries"]))
        kinds = {e["kind"] for e in out["entries"]}
        self.assertTrue({"settings", "hardware", "agent", "update", "health", "backup"} <= kinds, kinds)
        self.assertTrue(any(e["kind"] == "mode" and "nobody" not in e["title"] for e in out["entries"]))

    def test_future_timestamps_are_clamped(self):
        self.write_activity([{"ts": 9999999999, "kind": "mode", "mode": "gaming"}])
        before = time.time()
        modes = [e for e in self.timeline()["entries"] if e["kind"] == "mode"]
        self.assertEqual(len(modes), 1)
        self.assertLessEqual(modes[0]["ts"], before + T.FUTURE_MARGIN + 60)

    def test_each_source_is_capped(self):
        runs = [{"id": f"r{i}", "agent": "a", "status": "finished", "result": "ok", "error": None,
                 "started": 1794900000.0 + i, "ended": 1794900000.0 + i, "steps": [], "task": "t"}
                for i in range(200)]
        self.runner.runs = runs
        self.fill_all()
        with mock.patch.object(T, "PER_SOURCE", 50):
            out = self.timeline("?limit=500")
        self.assertEqual(len([e for e in out["entries"] if e["kind"] == "agent"]), 50)
        self.assertIn("mode", {e["kind"] for e in out["entries"]})

    def test_update_entries(self):
        self.status = U.UpdateStatus(run=FakeRun("rolled-back.json", show_text("exit-code", 1)))
        entries = T.update_entries(self.status.get())
        titles = " ".join(e["title"] for e in entries).lower()
        self.assertIn("failed", titles)
        self.assertIn("older", titles)


class UpdatesEndpointTest(TimelineBase):
    def test_viewer_can_read(self):
        r = self.c.get("/api/system/updates", headers=self.viewer)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["update_ready"])
        self.assertIn("recovery", body)
        self.assertEqual(self.c.get("/api/system/updates").status_code, 401)
        self.assertIn(self.c.post("/api/system/updates", headers=self.viewer).status_code, (403, 405))

    def test_endpoint_and_timeline_share_one_cache(self):
        for _ in range(3):
            self.c.get("/api/system/updates", headers=self.viewer)
            self.c.get("/api/timeline", headers=self.viewer)
        self.assertEqual(len(self.run.ostree_calls()), 1)


# The forge-apply public timeline file

class ApplyTimelineFileTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.conf = os.path.join(self.root, "etc/forge/settings.json")
        self.env = dict(os.environ, FORGE_ROOT=self.root, FORGE_DRYRUN="1", FORGE_CONF=self.conf, PYTHONPATH=LIB)
        self.env.pop("SUDO_USER", None)
        self.env.pop("PKEXEC_UID", None)
        self.public = os.path.join(self.root, "var/lib/forge-timeline/settings.jsonl")

    def apply(self, values, **env):
        src = os.path.join(self.root, "in.json")
        with open(src, "w") as f:
            json.dump(values, f)
        r = subprocess.run([sys.executable, APPLY, "--import", src, "--merge"], env=dict(self.env, **env),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def lines(self):
        with open(self.public) as f:
            return [json.loads(line) for line in f.read().splitlines()]

    def test_written_without_values(self):
        self.apply({"security.usb_key_label": "SECRETLBL1", "ai.keep_alive_min": 17}, SUDO_USER="alice")
        entries = self.lines()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["by"], "alice")
        self.assertEqual(e["action"], "import")
        self.assertEqual(e["keys"], ["ai.keep_alive_min", "security.usb_key_label"])
        self.assertEqual(e["restart"]["boot"], ["security.usb_key_label"])
        with open(self.public) as f:
            text = f.read()
        self.assertNotIn("SECRETLBL1", text)
        self.assertNotIn("FORGEKEY", text)   # the old value either
        self.assertNotIn("changed", e)
        # The root-only history still has the values, as before.
        with open(os.path.join(self.root, "var/lib/forge-apply/history.jsonl")) as f:
            self.assertIn("SECRETLBL1", f.read())

    def test_mode_and_owner(self):
        self.apply({"ai.keep_alive_min": 17})
        st = os.stat(self.public)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o644)
        self.assertEqual(st.st_uid, os.geteuid())
        d = os.stat(os.path.dirname(self.public))
        self.assertEqual(stat.S_IMODE(d.st_mode), 0o755)
        self.assertEqual(d.st_uid, os.geteuid())

    def test_mode_fixed_even_under_a_strict_umask(self):
        code = "import os, runpy, sys; os.umask(0o077); sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
        src = os.path.join(self.root, "in.json")
        with open(src, "w") as f:
            json.dump({"ai.keep_alive_min": 18}, f)
        r = subprocess.run([sys.executable, "-c", code, APPLY, "--import", src, "--merge"], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.public).st_mode), 0o644)

    def test_capped(self):
        os.makedirs(os.path.dirname(self.public), exist_ok=True)
        with open(self.public, "w") as f:
            for i in range(5000):
                f.write(json.dumps({"ts": i, "by": "old", "action": "import", "keys": ["k" * 50]}) + "\n")
        self.apply({"ai.keep_alive_min": 19})
        entries = self.lines()
        self.assertLessEqual(len(entries), 200)
        self.assertLessEqual(os.path.getsize(self.public), 64 * 1024)
        self.assertEqual(entries[-1]["keys"], ["ai.keep_alive_min"])
        self.assertEqual(entries[-2]["ts"], 4999)   # the newest old ones are kept

    def test_atomic_replace_and_no_temp_left(self):
        self.apply({"ai.keep_alive_min": 20})
        ino = os.stat(self.public).st_ino
        self.apply({"ai.keep_alive_min": 21})
        self.assertNotEqual(os.stat(self.public).st_ino, ino, "rewritten in place, not replaced")
        self.assertEqual(os.listdir(os.path.dirname(self.public)), ["settings.jsonl"])
        self.assertEqual(len(self.lines()), 2)

    def test_link_at_the_file_name_is_replaced_not_followed(self):
        os.makedirs(os.path.dirname(self.public), exist_ok=True)
        victim = os.path.join(self.root, "victim")
        with open(victim, "w") as f:
            f.write("keep me\n")
        os.symlink(victim, self.public)
        self.apply({"ai.keep_alive_min": 22})
        with open(victim) as f:
            self.assertEqual(f.read(), "keep me\n")
        self.assertFalse(os.path.islink(self.public))
        self.assertEqual(len(self.lines()), 1)

    def test_linked_folder_is_refused_but_the_save_still_works(self):
        elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(elsewhere)
        os.makedirs(os.path.join(self.root, "var/lib"), exist_ok=True)
        os.symlink(elsewhere, os.path.dirname(self.public))
        r = self.apply({"ai.keep_alive_min": 23})
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertIn("timeline", r.stdout)
        with open(self.conf) as f:
            self.assertEqual(json.load(f)["ai.keep_alive_min"], 23)

    def test_no_change_no_entry(self):
        self.apply({"ai.keep_alive_min": 24})
        self.apply({"ai.keep_alive_min": 24})
        self.assertEqual(len(self.lines()), 1)


# The per-user publisher

class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(dir=TMP)
        self.state = os.path.join(self.home, "state")
        os.makedirs(os.path.join(self.state, "forge"))
        self.out = os.path.join(self.home, "spool")
        os.makedirs(self.out)
        self.env = dict(os.environ, HOME=self.home, XDG_STATE_HOME=self.state, FORGE_ACTIVITY_DIR=self.out,
                        FORGE_LIB=LIB)
        self.final = os.path.join(self.out, f"{os.geteuid()}.jsonl")

    def logs(self, activity="", health="", backup=None):
        with open(os.path.join(self.state, "forge/activity.log"), "w") as f:
            f.write(activity)
        with open(os.path.join(self.state, "forge/health.log"), "w") as f:
            f.write(health)
        if backup is not None:
            os.makedirs(os.path.join(self.state, "forge-backup"), exist_ok=True)
            with open(os.path.join(self.state, "forge-backup/status.json"), "w") as f:
                f.write(backup if isinstance(backup, str) else json.dumps(backup))

    def publish(self, **env):
        r = subprocess.run([sys.executable, PUBLISH], env=dict(self.env, **env), capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def entries(self):
        with open(self.final) as f:
            return [json.loads(line) for line in f.read().splitlines()]

    def test_reads_forge_backups_real_status_format(self):
        # The fields forge-backup actually writes (see its status.json notes).
        cases = {"ok": True, "ok_with_warnings": True, "failed": False, "drive_missing": False,
                 "drive_full": False, "wrong_password": False, "not_set_up": False}
        for result, ok in cases.items():
            self.logs("", "", {"version": 1, "result": result, "message": "m " + result, "last_run": 1794950000,
                               "last_success": 1794940000})
            self.publish()
            b = [e for e in self.entries() if e["kind"] == "backup"]
            self.assertEqual(len(b), 1, result)
            self.assertIs(b[0]["ok"], ok, result)

    def test_forge_backup_publishes_after_a_run(self):
        src = open(os.path.join(BIN, "forge-backup")).read()
        self.assertIn("forge-activity-publish", src)

    def test_publishes_modes_problems_and_backup(self):
        self.logs("2026-11-18T10:00:00+07:00 mode=gaming\n2026-11-18T11:00:00+07:00 mode=quiet\n",
                  "2026-11-18T10:05:00+07:00 ok gpu=40C mem_free=30G\n"
                  "2026-11-18T10:15:00+07:00 PROBLEM Disk / is 95% full; GPU is hot: 90C\n"
                  "2026-11-18T10:16:00+07:00 HARDWARE RAM changed\n",
                  {"finished": 1794950000, "ok": False, "message": "Backup failed: disk full"})
        self.publish()
        e = self.entries()
        modes = [x for x in e if x["kind"] == "mode"]
        self.assertEqual([m["mode"] for m in modes], ["gaming", "quiet"])
        self.assertEqual(modes[0]["ts"], 1794970800.0)
        health = [x for x in e if x["kind"] == "health"]
        self.assertEqual(len(health), 1)
        self.assertIn("Disk / is 95% full", health[0]["text"])
        backup = [x for x in e if x["kind"] == "backup"]
        self.assertEqual(backup, [{"ts": 1794950000.0, "kind": "backup", "ok": False,
                                   "text": "Backup failed: disk full"}])
        self.assertEqual(stat.S_IMODE(os.stat(self.final).st_mode), 0o644)

    def test_mode_0644_even_with_a_private_umask(self):
        self.logs("2026-11-18T10:00:00+07:00 mode=gaming\n")
        code = "import os, runpy, sys; os.umask(0o077); runpy.run_path(sys.argv[1], run_name='__main__')"
        r = subprocess.run([sys.executable, "-c", code, PUBLISH], env=self.env, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.final).st_mode), 0o644)

    def test_capped_and_cleaned(self):
        lines = "".join(f"2026-11-18T10:00:{i % 60:02d}+07:00 mode=gaming\n" for i in range(3000))
        self.logs(lines, "2026-11-18T10:15:00+07:00 PROBLEM " + "\x1b[2J" + "x" * 5000 + "\n")
        self.publish()
        e = self.entries()
        self.assertLessEqual(len(e), 200)
        h = [x for x in e if x["kind"] == "health"][0]
        self.assertNotIn("\x1b", h["text"])
        self.assertLessEqual(len(h["text"]), 300)
        self.assertLessEqual(os.path.getsize(self.final), 128 * 1024)

    def test_secret_is_hidden_before_the_cut(self):
        secret = "https://alice:hunter2@backup.example/repo"
        pre = "Failed services: " + "x" * (300 - len("Failed services: ") - len("https://alice:hunter2") - 1) + " "
        self.logs("", "2026-11-18T10:15:00+07:00 PROBLEM " + pre + secret + "\n")
        self.publish(FORGE_LIB=LIB)
        h = [x for x in self.entries() if x["kind"] == "health"][0]
        self.assertNotIn("hunter2", h["text"])
        self.assertLessEqual(len(h["text"]), 300)
        self.assertNotIn("hunter2", T.entry(1, "health", "t", h["text"])["detail"])

    def test_no_free_text_without_forge_cores_helpers(self):
        self.logs("", "2026-11-18T10:15:00+07:00 PROBLEM token=abc https://u:pw@x.example/\n")
        self.publish(FORGE_LIB=os.path.join(self.home, "nope"))
        self.assertEqual([x["text"] for x in self.entries() if x["kind"] == "health"], [""])

    def test_missing_logs_and_backup(self):
        self.publish()
        self.assertEqual(self.entries(), [])
        self.logs("", "", "not json")
        self.publish()
        self.assertEqual(self.entries(), [])

    def test_missing_or_unwritable_folder_is_quiet(self):
        self.logs("2026-11-18T10:00:00+07:00 mode=gaming\n")
        r = self.publish(FORGE_ACTIVITY_DIR=os.path.join(self.home, "nope"))
        self.assertEqual(r.stdout + r.stderr, "")

    def test_link_at_the_file_name_is_replaced_not_followed(self):
        victim = os.path.join(self.home, "victim")
        with open(victim, "w") as f:
            f.write("keep me\n")
        os.symlink(victim, self.final)
        self.logs("2026-11-18T10:00:00+07:00 mode=gaming\n")
        self.publish()
        with open(victim) as f:
            self.assertEqual(f.read(), "keep me\n")
        self.assertFalse(os.path.islink(self.final))

    def test_linked_folder_is_refused(self):
        real = os.path.join(self.home, "real")
        os.makedirs(real)
        link = os.path.join(self.home, "linked")
        os.symlink(real, link)
        self.logs("2026-11-18T10:00:00+07:00 mode=gaming\n")
        self.publish(FORGE_ACTIVITY_DIR=link)
        self.assertEqual(os.listdir(real), [])

    def test_atomic_and_no_temp_left(self):
        self.logs("2026-11-18T10:00:00+07:00 mode=gaming\n")
        self.publish()
        ino = os.stat(self.final).st_ino
        self.logs("2026-11-18T10:00:00+07:00 mode=gaming\n2026-11-18T10:01:00+07:00 mode=quiet\n")
        self.publish()
        self.assertNotEqual(os.stat(self.final).st_ino, ino)
        self.assertEqual(os.listdir(self.out), [os.path.basename(self.final)])

    def test_logs_that_are_links_are_not_read(self):
        secret = os.path.join(self.home, "secret.txt")
        with open(secret, "w") as f:
            f.write("2026-11-18T10:15:00+07:00 PROBLEM topsecret\n")
        os.symlink(secret, os.path.join(self.state, "forge/health.log"))
        self.publish()
        self.assertNotIn("topsecret", json.dumps(self.entries()))


class ForgeModePublishTest(unittest.TestCase):
    def test_mode_switch_publishes(self):
        f = tempfile.mkdtemp(dir=TMP)
        called = os.path.join(f, "called")
        write_exec(os.path.join(f, "forge-config"), "#!/bin/sh\necho 1\n")
        for tool in ("pkexec", "powerprofilesctl"):
            write_exec(os.path.join(f, tool), "#!/bin/sh\nexit 0\n")
        write_exec(os.path.join(f, "publish"), f"#!/bin/sh\ntouch {called}\n")
        env = dict(os.environ, PATH=f + ":/usr/bin:/bin", HOME=f, XDG_STATE_HOME=os.path.join(f, "state"),
                   FORGE_ACTIVITY_PUBLISH=os.path.join(f, "publish"))
        r = subprocess.run(["bash", os.path.join(BIN, "forge-mode"), "gaming"], env=env, capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(called))
        with open(os.path.join(f, "state/forge/activity.log")) as fh:
            self.assertIn("mode=gaming", fh.read())


# forge-health update notifications

class ForgeHealthUpdateTest(unittest.TestCase):
    def setUp(self):
        self.fake = f = tempfile.mkdtemp(dir=TMP)
        self.log = os.path.join(f, "notify.log")
        write_exec(os.path.join(f, "notify-send"),
                   "#!/usr/bin/env python3\nimport json, sys\n"
                   f"open({self.log!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
        self.cfg = {"monitor.gpu_temp_warn": "200", "monitor.disk_warn_pct": "101", "monitor.mem_warn_gb": "0",
                    "ai.ollama": "off", "monitor.check_ollama": "off", "core.enabled": "off",
                    "core.port": "47900", "monitor.notify": "on", "monitor.notify_updates": "on"}
        self.write_config()
        write_exec(os.path.join(f, "nvidia-smi"), "#!/bin/sh\nexit 1\n")
        self.set_status("normal.json")
        self.set_show(show_text())
        self.env = dict(os.environ, PATH=f + ":" + os.environ["PATH"], HOME=f,
                        XDG_STATE_HOME=os.path.join(f, "state"), FORGE_LIB=LIB,
                        FORGE_VIEWER_TOKEN=os.path.join(f, "missing"),
                        FORGE_HW_NOTICE=os.path.join(f, "missing.json"),
                        FORGE_ACTIVITY_PUBLISH=os.path.join(f, "missing-publish"))

    def write_config(self):
        cases = "\n".join(f'  {k}) echo "{v}" ;;' for k, v in self.cfg.items())
        write_exec(os.path.join(self.fake, "forge-config"), f'#!/bin/sh\ncase "$2" in\n{cases}\nesac\n')

    def set_status(self, name, text=None):
        p = os.path.join(self.fake, "status.json")
        with open(p, "w") as fh:
            fh.write(text if text is not None else fixture(name))
        write_exec(os.path.join(self.fake, "rpm-ostree"), f"#!/bin/sh\ncat {p}\n")

    def set_show(self, text):
        p = os.path.join(self.fake, "show.txt")
        with open(p, "w") as fh:
            fh.write(text)
        write_exec(os.path.join(self.fake, "systemctl"),
                   f'#!/bin/sh\nif [ "$1" = show ]; then cat {p}; fi\nexit 0\n')

    def run_health(self):
        if os.path.exists(self.log):
            os.unlink(self.log)
        r = subprocess.run(["bash", os.path.join(BIN, "forge-health")], env=self.env,
                           capture_output=True, text=True, timeout=60)
        calls = []
        if os.path.exists(self.log):
            with open(self.log) as fh:
                calls = [json.loads(line) for line in fh]
        return r, calls

    def update_calls(self, calls):
        return [c for c in calls if "update" in json.dumps(c).lower() or "older" in json.dumps(c).lower()]

    def test_nothing_when_up_to_date(self):
        r, calls = self.run_health()
        self.assertEqual(self.update_calls(calls), [], (calls, r.stderr))

    def test_staged_update_notifies_once(self):
        self.set_status("staged.json")
        r, calls = self.run_health()
        upd = self.update_calls(calls)
        self.assertEqual(len(upd), 1, (calls, r.stderr))
        self.assertIn("restart", json.dumps(upd).lower())
        self.assertIn("45.20261118", json.dumps(upd))
        r, calls = self.run_health()
        self.assertEqual(self.update_calls(calls), [])
        # A newer staged update is a new notice.
        data = json.loads(fixture("staged.json"))
        data["deployments"][0]["checksum"] = "d" * 64
        data["deployments"][0]["version"] = "45.20261119"
        self.set_status(None, json.dumps(data))
        r, calls = self.run_health()
        self.assertEqual(len(self.update_calls(calls)), 1)

    def test_failed_update_notifies_once(self):
        self.set_show(show_text("exit-code", 1))
        r, calls = self.run_health()
        upd = self.update_calls(calls)
        self.assertEqual(len(upd), 1, (calls, r.stderr))
        self.assertIn("failed", json.dumps(upd).lower())
        r, calls = self.run_health()
        self.assertEqual(self.update_calls(calls), [])
        self.set_show(show_text("exit-code", 1, exit_ts="@1795070400"))
        r, calls = self.run_health()
        self.assertEqual(len(self.update_calls(calls)), 1)

    def test_rollback_notifies_once(self):
        self.set_status("rolled-back.json")
        r, calls = self.run_health()
        upd = self.update_calls(calls)
        self.assertEqual(len(upd), 1, (calls, r.stderr))
        self.assertIn("older", json.dumps(upd).lower())
        r, calls = self.run_health()
        self.assertEqual(self.update_calls(calls), [])

    def test_can_be_turned_off(self):
        self.cfg["monitor.notify_updates"] = "off"
        self.write_config()
        self.set_status("staged.json")
        self.set_show(show_text("exit-code", 1))
        r, calls = self.run_health()
        self.assertEqual(self.update_calls(calls), [])

    def test_notifications_are_escaped(self):
        data = json.loads(fixture("staged.json"))
        data["deployments"][0]["version"] = "<a href='http://x'>-u critical</a>"
        self.set_status(None, json.dumps(data))
        r, calls = self.run_health()
        upd = self.update_calls(calls)
        self.assertEqual(len(upd), 1, (calls, r.stderr))
        c = upd[0]
        self.assertIn("--", c)
        for text in c[c.index("--") + 1:]:
            self.assertNotIn("<", text)

    def test_broken_rpm_ostree_is_quiet(self):
        self.set_status(None, "garbage")
        r, calls = self.run_health()
        self.assertEqual(self.update_calls(calls), [])
        os.unlink(os.path.join(self.fake, "rpm-ostree"))
        r, calls = self.run_health()
        self.assertEqual(self.update_calls(calls), [])

    def test_setting_exists_in_schema(self):
        with open(os.environ["FORGE_SCHEMA"]) as fh:
            schema = json.load(fh)
        s = [x for x in schema["settings"] if x["key"] == "monitor.notify_updates"]
        self.assertEqual(len(s), 1)
        for field in ("label", "help", "tags", "group", "default"):
            self.assertIn(field, s[0])
        self.assertIs(s[0]["default"], True)


class PackagingTest(unittest.TestCase):
    def test_tmpfiles_and_finalize(self):
        with open(os.path.join(ROOT, "files/system/usr/lib/tmpfiles.d/forge-activity.conf")) as f:
            lines = [line.split() for line in f if line.strip() and not line.startswith("#")]
        self.assertIn(["d", "/var/lib/forge-activity", "3770", "forge", "wheel", "-"], lines)
        with open(os.path.join(ROOT, "files/scripts/finalize.sh")) as f:
            self.assertIn("/usr/libexec/forge-activity-publish", f.read())


if __name__ == "__main__":
    unittest.main()
