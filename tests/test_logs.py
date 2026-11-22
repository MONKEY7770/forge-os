"""Forge Dashboard log viewer: recent errors and warnings from the system and user journals, read only.

journalctl is a fake on PATH that prints journal JSON and records its argv. Every line must pass
through forgecore.policy.clean_details before it leaves forgedash, reads are bounded in lines, bytes
and time, and the page endpoint has the dashboard's owner and token checks.
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_dashboard as TD  # noqa: E402
from test_settings_page import LIB, OTHER, OWNER, refused, request  # noqa: E402

if LIB not in sys.path:
    sys.path.insert(0, LIB)

SECRETS = ["hunter2-fake", "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE", "ghp_FAKEfakeFAKEfakeFAKEfake1234",
           "c2VjcmV0LWZha2UtdG9rZW4=", "fake-cookie-value-123"]
MESSAGES = [
    "login failed: password=hunter2-fake for user bob",
    "calling cloud with key sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE",
    "git push https://ghp_FAKEfakeFAKEfakeFAKEfake1234@github.com/x/y failed",
    "Authorization: Bearer c2VjcmV0LWZha2UtdG9rZW4=",
    "GET /api?token=fake-cookie-value-123&x=1 refused",
    "plain warning without secrets",
    "db error: password: hunter2-fake (retrying)",
    "x=token=passwd=hunter2-fake",
]

JOURNAL = r'''#!@PY@
import json, os, sys, time
with open(os.environ["FAKE_JLOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_J_MODE", "ok")
scope = "user" if "--user" in sys.argv else "system"
if mode == "slow":
    sys.stdout.write(json.dumps({"__REALTIME_TIMESTAMP": "1763549000000000", "PRIORITY": "4", "MESSAGE": "first"}) + "\n")
    sys.stdout.flush()
    time.sleep(30)
if mode == "noaccess" and scope == "system":
    sys.stderr.write("No journal files were opened due to insufficient permissions.\n"); sys.exit(1)
if mode == "own" and scope == "system":
    sys.stderr.write("Hint: You are currently not seeing messages from other users and the system.\n"
                     "      Users in groups 'adm', 'systemd-journal', 'wheel' can see all messages.\n"
                     "      Pass -q to turn off this notice.\n")
msgs = json.loads(os.environ["FAKE_J_MSGS"])
n = int(os.environ.get("FAKE_J_COUNT", "0"))
out = []
for i, m in enumerate(msgs):
    out.append({"__REALTIME_TIMESTAMP": str(1763549000000000 + i * 1000000 + (500000 if scope == "user" else 0)),
                "PRIORITY": "3" if i % 2 == 0 else "4",
                ("_SYSTEMD_USER_UNIT" if scope == "user" else "_SYSTEMD_UNIT"):
                    "forge-backup.service" if scope == "user" else "forge-core.service",
                "SYSLOG_IDENTIFIER": "forge", "MESSAGE": m})
for i in range(n):
    out.append({"__REALTIME_TIMESTAMP": str(1763540000000000 + i), "PRIORITY": "4", "MESSAGE": "filler %d" % i})
if mode == "odd":
    out.append({"__REALTIME_TIMESTAMP": "1763549999000000", "PRIORITY": "3", "MESSAGE": [104, 105, 255, 0, 27]})
    out.append({"__REALTIME_TIMESTAMP": "not a time", "PRIORITY": "3", "MESSAGE": "bad time"})
    out.append({"__REALTIME_TIMESTAMP": "1763549998000000", "PRIORITY": "x", "MESSAGE": {"nested": 1}})
    out.append({"__REALTIME_TIMESTAMP": "1763549997000000", "PRIORITY": "2", "MESSAGE": "y" * 100000})
    out.append({"__REALTIME_TIMESTAMP": "1763549996000000", "PRIORITY": "3",
                "MESSAGE": "spoof‮ evil \x1b[31m red"})
for e in out:
    sys.stdout.write(json.dumps(e) + "\n")
if mode == "huge":
    for i in range(200000):
        sys.stdout.write(json.dumps({"__REALTIME_TIMESTAMP": "1763540000000000", "PRIORITY": "4",
                                     "MESSAGE": "z" * 200}) + "\n")
sys.exit(0)
'''


def forgedash():
    os.environ.pop("FORGE_DASH_TEST", None)
    import forgedash as fd
    return importlib.reload(fd)


class LogsUnitTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.bin = os.path.join(self.t, "bin")
        os.makedirs(self.bin)
        p = os.path.join(self.bin, "journalctl")
        with open(p, "w") as f:
            f.write(JOURNAL.replace("@PY@", sys.executable))
        os.chmod(p, 0o755)
        self.jlog = os.path.join(self.t, "jlog")
        patcher = mock.patch.dict(os.environ, {"PATH": self.bin + ":" + os.environ["PATH"], "FAKE_JLOG": self.jlog,
                                               "FAKE_J_MSGS": json.dumps(MESSAGES)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fd = forgedash()
        self.dash = self.fd.Dashboard()

    def argvs(self):
        try:
            with open(self.jlog) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def logs(self, **body):
        return self.dash.logs(dict({"service": "all", "level": "warning", "since": "24h", "search": ""}, **body))

    def test_reads_both_journals_with_fixed_bounded_arguments(self):
        d = self.logs()
        self.assertTrue(d["ok"], d)
        argvs = self.argvs()
        self.assertEqual(len(argvs), 2)
        scopes = sorted(a[0] for a in argvs)
        self.assertEqual(scopes, ["--system", "--user"])
        for a in argvs:
            self.assertIn("--priority=warning", a)
            self.assertIn("--since=-24h", a)
            self.assertIn("--no-pager", a)
            self.assertIn("--reverse", a)
            self.assertIn("-o", a)
            self.assertEqual(a[a.index("-o") + 1], "json")
            n = a[a.index("-n") + 1]
            self.assertTrue(n.isdigit() and int(n) <= 2000, a)
            self.assertFalse(any("grep" in x for x in a), "search text must never reach journalctl")
        units = [e["unit"] for e in d["entries"]]
        self.assertIn("forge-core.service", units)
        self.assertIn("forge-backup.service", units)
        ts = [e["ts"] for e in d["entries"]]
        self.assertEqual(ts, sorted(ts, reverse=True))

    def test_every_secret_is_hidden(self):
        d = self.logs()
        text = json.dumps(d)
        for s in SECRETS:
            self.assertNotIn(s, text)
        self.assertIn("[hidden]", text)
        self.assertIn("plain warning without secrets", text)

    def test_every_line_goes_through_clean_details(self):
        from forgecore import policy
        seen = []
        real = policy.clean_details

        def spy(value, *a, **k):
            seen.append(value)
            return real(value, *a, **k)
        with mock.patch.object(policy, "clean_details", spy):
            d = self.logs()
        msgs = [v.get("msg") for v in seen if isinstance(v, dict)]
        for m in MESSAGES:
            self.assertIn(m, msgs)
        self.assertEqual(len(d["entries"]), len(MESSAGES) * 2)

    def test_search_runs_on_hidden_text_only(self):
        self.assertEqual(self.logs(search="hunter2")["entries"], [])
        self.assertEqual(len(self.logs(search="PLAIN warning")["entries"]), 2)
        self.assertEqual(self.logs(search="no such words")["entries"], [])

    def test_no_redaction_means_no_lines(self):
        with mock.patch.object(self.fd, "_redactor", side_effect=ImportError("no fastapi")):
            d = self.logs()
        self.assertFalse(d["ok"])
        self.assertEqual(d["entries"], [])
        self.assertEqual(self.argvs(), [])

    def test_filters(self):
        self.logs(service="forge-core.service", level="error", since="1h")
        a = self.argvs()
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0][0], "--system")
        self.assertIn("--unit=forge-core.service", a[0])
        self.assertIn("--priority=err", a[0])
        self.assertIn("--since=-1h", a[0])
        os.remove(self.jlog)
        self.logs(service="forge-backup.service", level="info", since="7d")
        a = self.argvs()
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0][0], "--user")
        self.assertIn("--user-unit=forge-backup.service", a[0])
        self.assertIn("--priority=info", a[0])
        os.remove(self.jlog)
        self.logs(service="forge")
        a = {x[0]: x for x in self.argvs()}
        self.assertIn("--unit=forge-core.service", a["--system"])
        self.assertIn("--unit=forge-mode-apply@*.service", a["--system"])
        self.assertIn("--user-unit=forge-game.service", a["--user"])
        self.assertIn("--user-unit=forge-backup-restore-test.service", a["--user"])

    def test_odd_filters_are_refused_before_journalctl_runs(self):
        for body in ({"service": "sshd.service"}, {"service": "forge-core.service; reboot"}, {"service": ["x"]},
                     {"level": "debug"}, {"level": 3}, {"since": "-100y"}, {"since": "1h; rm"},
                     {"search": "x" * 101}, {"search": 5}):
            d = self.logs(**body)
            self.assertFalse(d["ok"], body)
        self.assertEqual(self.argvs(), [])

    def test_search_control_characters_are_refused(self):
        self.assertFalse(self.logs(search="a\nb")["ok"])
        self.assertFalse(self.logs(search="a‮b")["ok"])

    def test_shown_lines_are_bounded(self):
        with mock.patch.dict(os.environ, {"FAKE_J_COUNT": "3000"}):
            d = self.logs()
        self.assertLessEqual(len(d["entries"]), self.fd.LOG_SHOWN)
        self.assertTrue(d["cut"])

    def test_huge_output_is_cut_while_reading(self):
        with mock.patch.dict(os.environ, {"FAKE_J_MODE": "huge"}):
            started = time.monotonic()
            d = self.logs()
        self.assertTrue(d["ok"])
        self.assertLessEqual(len(d["entries"]), self.fd.LOG_SHOWN)
        self.assertLess(time.monotonic() - started, 20)

    def test_slow_journalctl_is_stopped(self):
        with mock.patch.object(self.fd, "LOG_TIMEOUT_S", 1.5), mock.patch.dict(os.environ, {"FAKE_J_MODE": "slow"}):
            started = time.monotonic()
            d = self.logs()
        self.assertLess(time.monotonic() - started, 6)
        self.assertIn("first", json.dumps(d))
        self.assertTrue(d["cut"])

    def test_odd_entries(self):
        with mock.patch.dict(os.environ, {"FAKE_J_MODE": "odd"}):
            d = self.logs()
        text = json.dumps(d)
        self.assertNotIn("bad time", text)
        self.assertNotIn("\x1b", "".join(e["msg"] for e in d["entries"]))
        self.assertIn("hi", text)
        longest = max(len(e["msg"]) for e in d["entries"])
        self.assertLessEqual(longest, self.fd.LOG_MSG_MAX + 50)
        for e in d["entries"]:
            self.assertIn(e["level"], ("emergency", "alert", "critical", "error", "warning", "notice", "info", "debug"))

    def test_no_access_to_the_system_journal(self):
        with mock.patch.dict(os.environ, {"FAKE_J_MODE": "noaccess"}):
            d = self.logs()
        self.assertTrue(d["ok"])
        self.assertEqual(d["journals"]["system"]["access"], "none")
        self.assertEqual(d["journals"]["user"]["access"], "full")
        self.assertTrue(d["entries"])   # your own journal still shows

    def test_only_your_own_messages(self):
        with mock.patch.dict(os.environ, {"FAKE_J_MODE": "own"}):
            d = self.logs()
        self.assertEqual(d["journals"]["system"]["access"], "own")

    def test_journalctl_missing(self):
        with mock.patch.dict(os.environ, {"PATH": self.t}):
            d = self.logs()
        self.assertFalse(d["ok"])
        self.assertIn("journalctl", d["error"])

    def test_one_read_at_a_time(self):
        self.assertTrue(self.dash.logs_lock.acquire(blocking=False))
        try:
            d = self.logs()
        finally:
            self.dash.logs_lock.release()
        self.assertFalse(d["ok"])
        self.assertEqual(self.argvs(), [])


@unittest.skipUnless(shutil.which("journalctl") and not os.environ.get("FORGE_TEST_NO_REAL_JOURNALCTL"),
                     "journalctl is not here")
class RealJournalctlArgvTest(unittest.TestCase):
    """The real journalctl accepts every argv the viewer builds (there is no journal in this container,
    so it only proves the options and their spelling)."""

    def test_argvs_are_valid(self):
        fd = forgedash()
        for service in fd.LOG_SERVICES:
            for argv in fd.log_argvs(service, "warning", "24h"):
                r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
                self.assertNotIn("unrecognized option", r.stderr, argv)
                self.assertNotIn("Failed to parse", r.stderr, argv)
                self.assertNotIn("invalid", r.stderr.lower(), argv)


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the server and clients as other uids")
class LogsEndpointTest(unittest.TestCase):
    """The real dashboard server, with test_dashboard's set-up (its helpers are borrowed, not its tests)."""
    _setup = TD.DashboardTest.setUp
    tearDown = TD.DashboardTest.tearDown
    settings = TD.DashboardTest.settings
    write_fakes = TD.DashboardTest.write_fakes
    calls = TD.DashboardTest.calls
    start = TD.DashboardTest.start
    open = TD.DashboardTest.open
    act = TD.DashboardTest.act
    wait_for = TD.DashboardTest.wait_for

    def setUp(self):
        self._setup()
        sample = "\n".join(json.dumps({"__REALTIME_TIMESTAMP": "1763549000000000", "PRIORITY": "3",
                                       "_SYSTEMD_UNIT": "forge-core.service", "MESSAGE": m}) for m in MESSAGES)
        self.fakes["journalctl"] = {"*": [sample + "\n", 0]}
        self.fakes["forge-backup"]["restore-test"] = ["Restore test done.\n", 0]
        self.write_fakes()

    def test_owner_with_token_gets_redacted_lines(self):
        base, h = self.open()
        status, body = request(OWNER, "POST", base + "/api/dash/logs", h,
                               json.dumps({"service": "all", "level": "warning", "since": "24h", "search": ""}))
        self.assertEqual(status, 200, body)
        d = json.loads(body)
        self.assertTrue(d["ok"], d)
        self.assertTrue(d["entries"])
        for s in SECRETS:
            self.assertNotIn(s, body)
        self.assertTrue(self.calls("journalctl"))

    def test_needs_the_token_and_the_owner(self):
        base, h = self.open()
        body = json.dumps({"service": "all", "level": "warning", "since": "24h", "search": ""})
        self.assertEqual(request(OWNER, "POST", base + "/api/dash/logs", {"X-Forge-Token": "wrong"}, body)[0], 403)
        self.assertEqual(request(OWNER, "POST", base + "/api/dash/logs", {}, body)[0], 403)
        self.assertTrue(refused(OTHER, "POST", base + "/api/dash/logs", h, body))
        self.assertEqual(request(OWNER, "GET", base + "/api/dash/logs", h)[0], 404)
        self.assertEqual(self.calls("journalctl"), [])

    def test_restore_test_button_runs_its_fixed_command_after_confirming(self):
        base, h = self.open()
        d = self.act(base, h, action="restore-test")
        self.assertFalse(d["ok"])
        self.assertNotIn(["forge-backup", "restore-test"], self.calls())
        d = self.act(base, h, action="restore-test", confirmed=True, target="/home/x")
        self.assertTrue(d["ok"], d)
        self.wait_for(["forge-backup", "restore-test"])
        self.assertIn(["forge-backup", "restore-test"], self.calls())


class PageTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(LIB, "dashboard.html")) as f:
            self.page = f.read()

    def test_log_page_and_backup_test_parts_exist(self):
        for part in ('data-page="logs"', 'id="p-logs"', "/api/dash/logs", 'id="log-service"', 'id="log-level"',
                     'id="log-since"', 'id="log-search"', "restore-test", "Test now", "wheel"):
            self.assertIn(part, self.page, part)

    def test_log_text_only_goes_in_as_text(self):
        self.assertNotIn("innerHTML", self.page)
        self.assertNotIn("insertAdjacentHTML", self.page)


if __name__ == "__main__":
    unittest.main()


class GamePendingTest(unittest.TestCase):
    """rvGame F2: the Dashboard says when switching back from gaming mode is still waiting."""

    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        os.makedirs(os.path.join(self.t, "forge"))
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.t})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fd = forgedash()

    def write(self, text):
        with open(os.path.join(self.t, "forge", "game-mode.json"), "w") as f:
            f.write(text)

    def test_pending(self):
        self.assertEqual(self.fd.game_status(), {"pending": False, "back_to": None})
        self.write(json.dumps({"prev": "agents", "pending": True}))
        self.assertEqual(self.fd.game_status(), {"pending": True, "back_to": "agents"})
        self.write(json.dumps({"prev": "<b>x</b>", "pending": True}))
        self.assertEqual(self.fd.game_status(), {"pending": True, "back_to": "default"})
        self.write("not json")
        self.assertEqual(self.fd.game_status(), {"pending": False, "back_to": None})
        os.remove(os.path.join(self.t, "forge", "game-mode.json"))
        os.symlink("/etc/passwd", os.path.join(self.t, "forge", "game-mode.json"))
        self.assertEqual(self.fd.game_status(), {"pending": False, "back_to": None})

    def test_page_shows_it(self):
        with open(os.path.join(LIB, "dashboard.html")) as f:
            page = f.read()
        self.assertTrue("dash.home.game_pending" in page and "d.game" in page)
