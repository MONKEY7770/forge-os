"""Release builder: the first-boot test (forge_screenshot.py firstboot on the runner,
.github/screenshot/firstboot/ for the test image and its probe inside the VM, the firstboot job of
screenshot-test.yml).

A fresh install has no account, so Plasma Setup must run on first boot and show its wizard. It can't
boot a real image here: the guest is replaced by fakes, and the logic (which tag, the probe's state
reports, pass and fail decisions, timeouts, the log request) is tested on its own. The real run is
Untested (needs CI)."""
import contextlib
import io
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_screenshot_test import ROOT, load  # noqa: E402

HOST = os.path.join(ROOT, ".github/scripts/forge_screenshot.py")
TEST_IMAGE = os.path.join(ROOT, ".github/screenshot/firstboot")
PROBE = os.path.join(TEST_IMAGE, "files/usr/libexec/forge-firstboot-probe")
GOOD = "unit=ran session=yes regular=0 setup-uid=974 wheel=yes wizard=yes autologin=yes"


def state_text(**kw):
    fields = dict(item.split("=") for item in GOOD.split())
    fields.update({k.replace("_", "-"): v for k, v in kw.items()})
    return " ".join(f"{k}={v}" for k, v in fields.items())


class TagTests(unittest.TestCase):
    def setUp(self):
        self.h = load(HOST, "forge_screenshot_fb_tag")

    def test_by_hand_any_tag_our_build_makes(self):
        for tag in ("latest", "testing", "br-fix-first-user-candidate-45", "0123abc-candidate-45"):
            self.assertEqual(self.h.firstboot_tag("workflow_dispatch", "stable", "", tag), tag)

    def test_empty_tag_means_the_channel_like_the_screenshot_job(self):
        self.assertEqual(self.h.firstboot_tag("workflow_dispatch", "stable", "", ""), "latest")
        self.assertEqual(self.h.firstboot_tag("workflow_dispatch", "testing", "", ""), "testing")
        self.assertEqual(self.h.firstboot_tag("workflow_run", "", "main", ""), "latest")
        self.assertEqual(self.h.firstboot_tag("workflow_run", "", "testing", ""), "testing")
        with self.assertRaises(ValueError):
            self.h.firstboot_tag("workflow_run", "", "feature", "")

    def test_bad_tags_are_refused(self):
        for tag in ("main", "br-x-candidate-45;rm -rf /", "latest\nx=y", "../latest", "br--candidate-45",
                    "0123abc-candidate-4", "sha256:" + "0" * 64, " latest"):
            with self.assertRaises(ValueError, msg=tag):
                self.h.firstboot_tag("workflow_dispatch", "stable", "", tag)

    def test_tag_only_by_hand(self):
        with self.assertRaises(ValueError):
            self.h.firstboot_tag("workflow_run", "", "main", "br-x-candidate-45")

    def test_same_rule_as_build_iso(self):
        release = load(os.path.join(ROOT, ".github/scripts/forge_release.py"), "forge_release_fb")
        for tag in ("latest", "br-a.b_c-candidate-45", "nope", "br-x-candidate-45 ", "abcdef0-candidate-45"):
            try:
                want = release.iso_tag(tag)
            except ValueError:
                want = None
            try:
                got = self.h.firstboot_tag("workflow_dispatch", "stable", "", tag)
            except ValueError:
                got = None
            self.assertEqual(got, want, tag)

    def run_cli(self, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = self.h.main(["firstboot-tag"])
        return rc, out.getvalue(), err.getvalue()

    def test_cli_prints_one_output_line_or_fails(self):
        base = {"EVENT": "workflow_dispatch", "CHANNEL_INPUT": "stable", "RUN_BRANCH": ""}
        rc, out, _ = self.run_cli({**base, "IMAGE_TAG_INPUT": "br-test-candidate-45"})
        self.assertEqual((rc, out), (0, "tag=br-test-candidate-45\n"))
        rc, out, _ = self.run_cli({**base, "IMAGE_TAG_INPUT": ""})
        self.assertEqual((rc, out), (0, "tag=latest\n"))
        rc, out, err = self.run_cli({**base, "IMAGE_TAG_INPUT": "x\ntag=evil"})
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("ERROR", err)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.h = load(HOST, "forge_screenshot_fb_state")

    def test_good_state(self):
        st = self.h.parse_state(GOOD)
        self.assertEqual(st, {"unit": "ran", "session": "yes", "regular": 0, "setup-uid": 974, "wheel": "yes",
                              "wizard": "yes", "autologin": "yes"})
        self.assertIsNone(self.h.parse_state(state_text(setup_uid="none"))["setup-uid"])

    def test_strict(self):
        bad = [GOOD.replace("unit=ran", "unit=done"), GOOD.replace(" wheel=yes", ""), GOOD + " extra=1",
               GOOD + " unit=ran", GOOD.replace("regular=0", "regular=-1"), GOOD.replace("regular=0", "regular=1234567"),
               GOOD.replace("session=yes", "session=YES"), GOOD.replace("setup-uid=974", "setup-uid=97a"),
               GOOD.replace("wizard=yes", "wizard"), "", "::error::x"]
        for text in bad:
            self.assertIsNone(self.h.parse_state(text), text)

    def verdict(self, **kw):
        return self.h.firstboot_verdict(self.h.parse_state(state_text(**kw)))

    def test_pass_needs_ran_session_and_no_account(self):
        ok, text = self.verdict()
        self.assertTrue(ok)
        self.assertIn("plasma-setup.service ran", text)
        self.assertIsNone(self.verdict(session="no"))
        for unit in ("waiting", "disabled", "running"):
            self.assertIsNone(self.verdict(unit=unit, session="no"), unit)
            self.assertIsNone(self.verdict(unit=unit), unit)

    def test_failures(self):
        cases = {"skipped-exec-condition": "was skipped", "skipped-condition": "was skipped",
                 "failed": "failed", "not-loaded": "not loaded"}
        for unit, needle in cases.items():
            ok, text = self.verdict(unit=unit, session="no")
            self.assertFalse(ok)
            self.assertIn(needle, text)
            self.assertIn("plasma-setup.service", text)
        ok, text = self.verdict(regular="1")
        self.assertFalse(ok)
        self.assertIn("UID 1000 to 60000", text)
        ok, text = self.verdict(unit="waiting", regular="2")
        self.assertFalse(ok, "an account before the wizard means it can never come")

    def test_setup_user_uid_must_be_below_1000(self):
        self.assertTrue(self.verdict(setup_uid="999")[0])
        for uid in ("1000", "1500", "60001"):
            ok, text = self.verdict(setup_uid=uid)
            self.assertFalse(ok, uid)
            self.assertIn(f"UID {uid}", text)

    def test_wheel_is_reported_not_decisive(self):
        self.assertTrue(self.verdict(wheel="no")[0])
        lines = self.h.info_lines(self.h.parse_state(state_text(wheel="no")))
        self.assertIn("wheel group in /etc/group: no", lines)
        self.assertIn("plasma-setup UID: 974 (below 1000, ok)", lines)
        lines = self.h.info_lines(self.h.parse_state(state_text(setup_uid="1200")))
        self.assertIn("plasma-setup UID: 1200 (1000 or more, FAIL)", lines)
        self.assertIn("unknown", self.h.info_lines(None)[0])

    def test_probe_events(self):
        events, _ = self.h.parse_probe(b"FORGE-PROBE state " + GOOD.encode() + b"\nFORGE-PROBE log a \x1b[1mline\n"
                                       b"FORGE-PROBE logs-end\nFORGE-PROBE desktop-ready\n", self.h.FIRSTBOOT_EVENTS)
        self.assertEqual(events[0], ("state", GOOD))
        self.assertEqual(events[1][0], "log")
        self.assertNotIn("\x1b", events[1][1])
        self.assertEqual(events[2], ("logs-end", ""))
        self.assertEqual(len(events), 3, "the screenshot test's events are not first-boot events")
        events, _ = self.h.parse_probe(b"FORGE-PROBE state " + GOOD.encode() + b"\n")
        self.assertEqual(events, [], "the screenshot test does not take first-boot events")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.h = load(HOST, "forge_screenshot_fb_session")

    def session(self, **kw):
        args = dict(start=0.0, wizard_timeout=600, shots=((30, "wizard"), (90, "wizard-2")), logs_timeout=120,
                    max_frames=3)
        args.update(kw)
        return self.h.FirstbootSession(**args)

    def test_happy_path(self):
        s = self.session()
        acts = []
        for t in range(0, 5):
            acts += s.tick(float(t), [])
        self.assertEqual(acts, [("frame", 0), ("frame", 1), ("frame", 2)])
        self.assertEqual(s.tick(10.0, [("started", ""), ("state", state_text(unit="waiting", session="no"))]), [])
        self.assertEqual(s.tick(100.0, [("state", GOOD)]), [])
        self.assertEqual(s.tick(129.0, []), [])
        self.assertEqual(s.tick(130.0, []), [("shot", "wizard")])
        self.assertEqual(s.tick(190.0, []), [("shot", "wizard-2"), ("send", "send-logs")])
        self.assertEqual(s.tick(195.0, [("log", "x")]), [])
        acts = s.tick(200.0, [("log", "y"), ("logs-end", "")])
        self.assertEqual(acts[0][:2], ("done", True))
        self.assertIn("after 100 s", acts[0][2])
        self.assertTrue(s.finished)
        self.assertEqual(s.final_state["setup-uid"], 974)
        self.assertEqual(s.tick(300.0, [("state", state_text(unit="failed"))]), [])

    def test_skipped_fails_at_once_with_screen_and_logs(self):
        s = self.session()
        acts = s.tick(50.0, [("started", ""), ("state", state_text(unit="skipped-exec-condition", session="no",
                                                                     regular="1"))])
        self.assertEqual(acts, [("shot", "failure"), ("send", "send-logs")])
        self.assertFalse(s.finished)
        acts = s.tick(60.0, [("logs-end", "")])
        self.assertEqual(acts[0][:2], ("done", False))
        self.assertIn("plasma-setup.service was skipped", acts[0][2])

    def test_logs_that_never_come_do_not_hang(self):
        s = self.session()
        s.tick(50.0, [("state", state_text(unit="failed"))])
        self.assertEqual(s.tick(170.0, []), [])
        acts = s.tick(171.0, [])
        self.assertEqual(acts[0][:2], ("done", False))
        self.assertIn("the guest logs did not arrive", acts[0][2])

    def test_no_wizard_in_time(self):
        s = self.session(max_frames=0)
        s.tick(10.0, [("started", ""), ("state", state_text(unit="ran", session="no"))])
        self.assertEqual(s.tick(600.0, []), [])
        acts = s.tick(601.0, [])
        self.assertEqual(acts, [("shot", "last"), ("send", "send-logs")])
        acts = s.tick(602.0, [("logs-end", "")])
        self.assertFalse(acts[0][1])
        self.assertIn("no wizard session after 10 minutes", acts[0][2])
        self.assertIn("plasma-setup.service ran", acts[0][2])
        self.assertIn("plasma-setup session no", acts[0][2])

    def test_silent_probe_fails_without_asking_for_logs(self):
        s = self.session(max_frames=0)
        acts = s.tick(601.0, [])
        self.assertEqual(acts[0], ("shot", "last"))
        self.assertEqual(acts[1][:2], ("done", False))
        self.assertIn("the probe never reported", acts[1][2])
        self.assertTrue(s.finished)

    def test_bad_state_lines_are_ignored(self):
        s = self.session(max_frames=0)
        self.assertEqual(s.tick(1.0, [("state", GOOD + " x=1"), ("state", "unit=failed")]), [])
        self.assertIsNone(s.state)

    def test_wrong_uid_fails(self):
        s = self.session(max_frames=0)
        acts = s.tick(1.0, [("state", state_text(setup_uid="1001"))])
        self.assertEqual(acts[0], ("shot", "failure"))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.h = load(HOST, "forge_screenshot_fb_cli")

    def main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                return self.h.main(argv), out.getvalue()
            except SystemExit as e:
                return e.code, out.getvalue()

    def test_values_checked(self):
        for extra in (["--wizard-minutes", "0"], ["--wizard-minutes", "121"], ["--memory", "512"],
                      ["--interval", "0"], ["--cpus", "0"]):
            rc, out = self.main(["firstboot", "--disk", "/nonexistent", "--out", "/tmp/x"] + extra)
            self.assertEqual(rc, 2, extra)
            self.assertIn("out of range", out)
        rc, _ = self.main(["firstboot", "--disk", "/x", "--out", "/y", "--desktop-minutes", "5"])
        self.assertEqual(rc, 2, "the screenshot test's option is not taken")

    def test_missing_disk(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        rc, text = self.main(["firstboot", "--disk", os.path.join(out, "none.raw"), "--out", out])
        self.assertEqual(rc, 2)
        self.assertIn("disk not found", text)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.p = load(PROBE, "forge_firstboot_probe_under_test")
        self.h = load(HOST, "forge_screenshot_fb_probe")
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_unit_status(self):
        base = {"LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "inactive", "SubState": "dead",
                "Result": "success", "ConditionResult": "no", "ConditionTimestampMonotonic": "0",
                "ExecMainStartTimestampMonotonic": "0"}
        cases = [({}, "waiting"), ({"UnitFileState": "disabled"}, "disabled"),
                 ({"LoadState": "not-found"}, "not-loaded"), ({"LoadState": "masked"}, "not-loaded"),
                 ({"Result": "exec-condition", "ConditionResult": "yes", "ConditionTimestampMonotonic": "5"},
                  "skipped-exec-condition"),
                 ({"ConditionTimestampMonotonic": "5"}, "skipped-condition"),
                 ({"ActiveState": "activating", "ConditionResult": "yes", "ConditionTimestampMonotonic": "5"}, "running"),
                 ({"ActiveState": "failed", "Result": "exit-code", "ExecMainStartTimestampMonotonic": "9"}, "failed"),
                 ({"ConditionResult": "yes", "ConditionTimestampMonotonic": "5",
                   "ExecMainStartTimestampMonotonic": "9"}, "ran"),
                 ({"ActiveState": "active", "ConditionResult": "yes", "ConditionTimestampMonotonic": "5",
                   "ExecMainStartTimestampMonotonic": "9"}, "ran")]
        for change, want in cases:
            self.assertEqual(self.p.unit_status({**base, **change}), want, change)
        for word in self.h.UNIT_WORDS:
            self.assertRegex(word, r"^[a-z-]+$")

    def test_systemctl_show_parsing(self):
        p = self.p.props("LoadState=loaded\nResult=exec-condition\nnoise\nExecMainStatus=0\n")
        self.assertEqual(p, {"LoadState": "loaded", "Result": "exec-condition", "ExecMainStatus": "0"})

    def test_wizard_session(self):
        s = {"Name": "plasma-setup", "Type": "wayland", "State": "active", "Active": "yes", "Class": "user"}
        self.assertTrue(self.p.wizard_session([s]))
        self.assertTrue(self.p.wizard_session([{**s, "Type": "x11"}]))
        self.assertTrue(self.p.wizard_session([{**s, "Class": "user-early"}]))
        for change in ({"Name": "plasmalogin"}, {"Type": "tty"}, {"State": "online", "Active": "no"},
                       {"Class": "greeter"}):
            self.assertFalse(self.p.wizard_session([{**s, **change}]), change)
        self.assertFalse(self.p.wizard_session([]))

    def test_accounts(self):
        passwd = ("root:x:0:0:root:/root:/bin/bash\nplasma-setup:x:974:974:Plasma Setup:/run/plasma-setup:/bin/sh\n"
                  "nobody:x:65534:65534::/:/sbin/nologin\ndyn:x:61184:61184::/:/sbin/nologin\n")
        self.assertEqual(self.p.regular_count(passwd), 0)
        self.assertEqual(self.p.uid_of(passwd, "plasma-setup"), 974)
        self.assertIsNone(self.p.uid_of(passwd, "forgetest"))
        self.assertEqual(self.p.regular_count(passwd + "anna:x:1000:1000::/var/home/anna:/bin/bash\n"
                                              "x:x:60000:60000::/:/bin/bash\n"), 2)
        self.assertTrue(self.p.has_wheel("root:x:0:\nwheel:x:10:\n"))
        self.assertFalse(self.p.has_wheel("root:x:0:\nwheelx\n# wheel:x:10:\n"))

    def fake_run(self, outputs):
        calls = []

        def run(argv, timeout=30, stderr=False):
            calls.append(argv)
            for key, text in outputs.items():
                if key in " ".join(argv):
                    return text
            return ""
        return run, calls

    def test_collect_and_state_line_match_the_host(self):
        proc = os.path.join(self.root, "proc", "42")
        os.makedirs(proc)
        with open(os.path.join(proc, "comm"), "w") as f:
            f.write("plasma-setup\n")
        with open(os.path.join(proc, "status"), "w") as f:
            f.write("Name:\tplasma-setup\nUid:\t974\t974\t974\t974\n")
        group = os.path.join(self.root, "group")
        with open(group, "w") as f:
            f.write("root:x:0:\nwheel:x:10:\n")
        run, calls = self.fake_run({
            "systemctl show plasma-setup.service": "LoadState=loaded\nActiveState=inactive\nResult=success\n"
                                                   "ConditionResult=yes\nConditionTimestampMonotonic=5\n"
                                                   "ExecMainStartTimestampMonotonic=7\n",
            "list-sessions": "3 974 plasma-setup seat0 1234 user tty1 no -\nbad;id x\n",
            "show-session 3": "Name=plasma-setup\nType=wayland\nState=active\nActive=yes\nClass=user\n",
            "getent passwd": "plasma-setup:x:974:974::/run/plasma-setup:/bin/sh\n"})
        st = self.p.collect(run_fn=run, proc_root=os.path.join(self.root, "proc"), group_path=group,
                            autologin=os.path.join(self.root, "none"))
        self.assertEqual(st, {"unit": "ran", "session": True, "regular": 0, "setup-uid": 974, "wheel": True,
                              "wizard": True, "autologin": False})
        self.assertFalse(any("bad;id" in " ".join(c) for c in calls), "odd session ids are not passed on")
        line = self.p.state_line(st)
        self.assertEqual(self.h.parse_state(line)["setup-uid"], 974)
        self.assertTrue(self.h.firstboot_verdict(self.h.parse_state(line))[0])
        st.update(**{"setup-uid": None, "regular": 10 ** 9})
        self.assertIsNotNone(self.h.parse_state(self.p.state_line(st)))

    def test_logs_cover_the_journal_and_failed_units(self):
        run, calls = self.fake_run({"journalctl": "line one\nline two\n"})
        lines = self.p.guest_logs({"setup-uid": 974}, run_fn=run)
        text = "\n".join(" ".join(c) for c in calls)
        self.assertIn("journalctl -b -u plasma-setup.service -u plasmalogin.service", text)
        self.assertIn("systemctl list-units --failed", text)
        self.assertIn("loginctl list-sessions", text)
        self.assertIn("journalctl -b _UID=974", text)
        self.assertIn("line two", lines)
        self.assertTrue(any(x.startswith("## journalctl") for x in lines))
        run, calls = self.fake_run({})
        self.p.guest_logs(None, run_fn=run)
        self.assertFalse(any("_UID=" in " ".join(c) for c in calls), "no user journal without a known UID")

    def test_protocol_with_a_fake_host(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        states = [{"unit": "waiting", "session": False, "regular": 0, "setup-uid": 974, "wheel": True,
                   "wizard": False, "autologin": True}]

        def collect():
            return dict(states[-1])
        t = threading.Thread(target=self.p.serve, args=(b.fileno(),),
                             kwargs=dict(collect_fn=collect, logs_fn=lambda st: ["journal " + "x" * 400, ""],
                                         poll=0.01, heartbeat=3600, max_runtime=3))
        t.start()
        f = a.makefile("rwb", buffering=0)
        self.assertEqual(f.readline(), b"FORGE-PROBE started\n")
        first = f.readline().decode()
        self.assertTrue(first.startswith("FORGE-PROBE state unit=waiting session=no"), first)
        states.append({**states[0], "unit": "ran", "session": True, "wizard": True})
        self.assertEqual(f.readline().decode(), f"FORGE-PROBE state {GOOD}\n")
        f.write(b"send-logs\n")
        log = f.readline()
        self.assertTrue(log.startswith(b"FORGE-PROBE log journal "))
        self.assertLess(len(log), 300)
        self.assertEqual(f.readline(), b"FORGE-PROBE log  \n")
        self.assertEqual(f.readline(), b"FORGE-PROBE logs-end\n")
        t.join(5)
        self.assertFalse(t.is_alive())


class TestImageFilesTests(unittest.TestCase):
    def read(self, rel):
        with open(os.path.join(TEST_IMAGE, rel)) as f:
            return f.read()

    def test_only_the_probe_no_account(self):
        text = self.read("Containerfile")
        self.assertIn("ARG BASE\nFROM ${BASE}\n", text)
        self.assertIn("systemctl enable forge-firstboot-probe.service", text)
        for word in ("useradd", "passwd", "sysusers", "Autologin", "forgetest", "dnf"):
            self.assertNotIn(word, text)
        files = []
        for d, _, names in os.walk(os.path.join(TEST_IMAGE, "files")):
            files += [os.path.relpath(os.path.join(d, n), TEST_IMAGE) for n in names if not n.endswith(".pyc")]
        self.assertEqual(sorted(files), ["files/usr/lib/systemd/system/forge-firstboot-probe.service",
                                         "files/usr/libexec/forge-firstboot-probe"])
        unit = self.read("files/usr/lib/systemd/system/forge-firstboot-probe.service")
        self.assertIn("ExecStart=/usr/libexec/forge-firstboot-probe", unit)
        self.assertNotIn("User=", unit)
        self.assertTrue(os.access(PROBE, os.X_OK))


try:
    import yaml
except ImportError:
    yaml = None


@unittest.skipUnless(yaml is not None or os.environ.get("GITHUB_ACTIONS"), "PyYAML not installed")
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        from test_release_workflows import SHA_PIN, UNTRUSTED_IN_SHELL, load as load_wf, runs, steps
        self.SHA_PIN, self.UNTRUSTED, self.runs, self.steps = SHA_PIN, UNTRUSTED_IN_SHELL, runs, steps
        self.wf = load_wf("screenshot-test.yml")
        self.job = self.wf["jobs"]["firstboot"]

    def test_input_and_triggers(self):
        inp = self.wf["on"]["workflow_dispatch"]["inputs"]["image_tag"]
        self.assertEqual((inp["type"], inp["default"]), ("string", ""))
        self.assertEqual(self.job["if"], self.wf["jobs"]["screenshots"]["if"])
        pick = [s for s in self.steps(self.job) if "firstboot-tag" in s.get("run", "")][0]
        self.assertEqual(pick["env"]["IMAGE_TAG_INPUT"], "${{ inputs.image_tag }}")
        self.assertEqual(pick["env"]["RUN_BRANCH"], "${{ github.event.workflow_run.head_branch }}")
        for s in self.steps(self.job):
            self.assertNotRegex(s.get("run", ""), self.UNTRUSTED, s.get("name"))
        self.assertNotIn("firstboot", self.runs(self.wf["jobs"]["screenshots"]))

    def test_read_only_signature_checked_digest_used(self):
        self.assertEqual(self.job["permissions"], {"contents": "read", "packages": "read"})
        self.assertNotIn("secrets.", str(self.job))
        self.assertTrue(0 < self.job["timeout-minutes"] <= 180)
        text = self.runs(self.job)
        verify = text.find("cosign verify --key cosign.pub")
        self.assertTrue(0 <= text.find("firstboot-tag") < verify < text.find("podman build"))
        self.assertIn('--build-arg "BASE=$IMAGE@$DIGEST"', text)
        self.assertIn("-f .github/screenshot/firstboot/Containerfile .github/screenshot/firstboot", text)
        self.assertIn("forge_screenshot.py install --image localhost/forge-firstboot-test:latest", text)
        self.assertIn("forge_screenshot.py firstboot --disk work/disk.raw --out firstboot", text)

    def test_results_uploaded_pass_or_fail_and_actions_pinned(self):
        up = [s for s in self.steps(self.job) if s.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0]["if"], "${{ always() }}")
        self.assertEqual((up[0]["with"]["name"], up[0]["with"]["path"]), ("firstboot", "firstboot/"))
        for s in self.steps(self.job):
            if "uses" in s:
                self.assertRegex(s["uses"], self.SHA_PIN)


if __name__ == "__main__":
    unittest.main()
