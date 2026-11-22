"""Release builder: the version number (45.YYYYMMDD.N plus the git commit), written at build time by
.github/scripts/forge_release.py into /usr/share/forge/version.json, and shown in Forge Settings and
the Forge Dashboard."""
import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCRIPT = os.path.join(ROOT, ".github/scripts/forge_release.py")
APP = os.path.join(ROOT, "files/system/usr/bin/forge-settings")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
sys.path.insert(0, LIB)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SHA = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret (a fake commit id)


def load_script():
    spec = importlib.util.spec_from_file_location("forge_release_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CiVersionScriptTests(unittest.TestCase):
    def setUp(self):
        self.r = load_script()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_fedora_major_comes_from_the_base_image_label(self):
        for label, want in (("45.20261119", 45), ("45.20261119.1", 45), ("stable-45.20261119", 45),
                            ("latest-46.20270501.2", 46), ("v45", 45)):
            self.assertEqual(self.r.fedora_from_inspect({"Labels": {"org.opencontainers.image.version": label}}), want)
        for bad in ({}, {"Labels": None}, {"Labels": {}}, {"Labels": {"org.opencontainers.image.version": "stable"}},
                    {"Labels": {"org.opencontainers.image.version": "9.1"}},
                    {"Labels": {"org.opencontainers.image.version": "450.1"}}, [], "x"):
            with self.assertRaises(ValueError, msg=bad):
                self.r.fedora_from_inspect(bad)

    def test_channel_comes_from_the_branch(self):
        self.assertEqual(self.r.channel_for("refs/heads/main", "push"), "stable")
        self.assertEqual(self.r.channel_for("refs/heads/main", "schedule"), "stable")
        self.assertEqual(self.r.channel_for("refs/heads/testing", "push"), "testing")
        self.assertEqual(self.r.channel_for("refs/heads/testing", "pull_request"), "dev")
        self.assertEqual(self.r.channel_for("refs/heads/feature/x", "push"), "dev")
        self.assertEqual(self.r.channel_for("refs/heads/main", "pull_request"), "dev")
        # rvRel 7: a tag named main is not the main branch.
        self.assertEqual(self.r.channel_for("refs/tags/main", "push"), "dev")
        self.assertEqual(self.r.channel_for("main", "push"), "dev")

    def test_version_scheme(self):
        when = datetime.datetime(2026, 11, 20, 6, 0, 5, tzinfo=datetime.timezone.utc)
        v = self.r.make_version(45, when, "57", SHA, "stable")
        self.assertEqual(v, {"format": 1, "version": "45.20261120.57", "fedora": 45, "date": "20261120", "build": 57,
                             "attempt": 1, "commit": SHA, "channel": "stable", "built": "2026-11-20T06:00:05Z"})
        # rvRel 7: a re-run of the same workflow run gets its own version.
        v2 = self.r.make_version(45, when, "57", SHA, "stable", attempt="2")
        self.assertEqual(v2["version"], "45.20261120.57.2")
        with self.assertRaises(ValueError):
            self.r.make_version(45, when, "57", SHA, "stable", attempt="0")
        for bad in ((45, when, "0", SHA, "stable"), (45, when, "x", SHA, "stable"), (45, when, "5", "nothex", "stable"),
                    (45, when, "5", SHA, "nightly"), (4, when, "5", SHA, "stable")):
            with self.assertRaises(ValueError, msg=bad):
                self.r.make_version(*bad)

    def test_built_tag_matches_bluebuild_branch_tags(self):
        # With alt-tags: [candidate], BlueBuild tags every image <sha7>-candidate-<fedora>.
        self.assertEqual(self.r.built_tag(SHA, 45), "0123456-candidate-45")

    def test_iso_tag_allowlist(self):
        for ok in ("latest", "testing", "0123456-candidate-45", "br-test-candidate-45", "br-feat_x.y-candidate-46"):
            self.assertEqual(self.r.iso_tag(ok), ok)
        self.assertEqual(self.r.iso_tag(""), "latest", "scheduled runs have no input")
        for bad in ("Latest", "latest ", "candidate", "candidate-45", "20261122-candidate-45", "0123456-45",
                    "br--candidate-45", "br-x-candidate-4", "br-a;b-candidate-45", "br-a$(id)-candidate-45",
                    "0123456-candidate-45\nX", "br-" + "a" * 120 + "-candidate-45", "pr-3-candidate-45",
                    "sha256:" + "a" * 64, "ghcr.io/x/y:latest", "../latest"):
            with self.assertRaises(ValueError, msg=bad):
                self.r.iso_tag(bad)
        r = self.run_cli("iso-tag", "br-a;b-candidate-45")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_iso_follow_tag_fits_the_iso_volume_id(self):
        # lorax refuses a volume id over 32 characters; the action makes it forge-x86_64-<tag>.
        self.assertEqual(self.r.iso_follow_tag("latest"), "latest")
        self.assertEqual(self.r.iso_follow_tag("testing"), "testing")
        self.assertEqual(self.r.iso_follow_tag("br-vmtest-cand-45"), "br-vmtest-cand-45")
        self.assertEqual(self.r.iso_follow_tag("0123456-candidate-45"), "latest")
        self.assertEqual(self.r.iso_follow_tag("br-test-candidate-45"), "latest")
        with self.assertRaises(ValueError):
            self.r.iso_follow_tag("a;b")

    def test_read_digest(self):
        p = os.path.join(self.tmp, "digest.txt")
        d = "sha256:" + "ab" * 32
        with open(p, "w") as f:
            f.write(d + "\n")
        r = self.run_cli("read-digest", p)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, d))
        for bad in ("sha256:xyz\n", d + "\n" + d + "\n", "", "sha256:" + "ab" * 32 + " x\n"):
            with open(p, "w") as f:
                f.write(bad)
            self.assertNotEqual(self.run_cli("read-digest", p).returncode, 0, bad)

    def test_plan_from_a_fedora_number(self):
        out = os.path.join(self.tmp, "o")
        r = self.run_cli("plan", "--fedora", "45", "--github-output", out,
                         env={"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push", "GITHUB_RUN_NUMBER": "9",
                              "GITHUB_RUN_ATTEMPT": "3", "GITHUB_SHA": SHA})
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out) as f:
            outputs = dict(line.split("=", 1) for line in f.read().splitlines())
        self.assertRegex(outputs["version"], r"^45\.\d{8}\.9\.3$")
        self.assertEqual(outputs["channel"], "stable")
        for bad in ("4", "x", "450"):
            self.assertNotEqual(self.run_cli("plan", "--fedora", bad, "--github-output", out,
                                             env={"GITHUB_RUN_NUMBER": "9", "GITHUB_SHA": SHA}).returncode, 0)

    def test_digest_from_cosign_verify_output(self):
        d = "sha256:" + "ab" * 32
        out = json.dumps([{"critical": {"image": {"docker-manifest-digest": d}}}] * 2)
        self.assertEqual(self.r.digest_from_cosign(out), d)
        for bad in ("[]", "{}", "not json", json.dumps([{"critical": {"image": {"docker-manifest-digest": "sha256:x"}}}]),
                    json.dumps([{"critical": {"image": {"docker-manifest-digest": d}}},
                                {"critical": {"image": {"docker-manifest-digest": "sha256:" + "cd" * 32}}}])):
            with self.assertRaises(ValueError, msg=bad):
                self.r.digest_from_cosign(bad)

    def test_label_check_after_the_push(self):
        good = {"Labels": {"org.forgeos.version": "45.20261120.57", "org.opencontainers.image.version": "45.20261119"}}
        self.assertIsNone(self.r.label_problem(good, "45.20261120.57"))
        for bad in ({"Labels": {"org.forgeos.version": "${FORGE_VERSION}"}}, {"Labels": {"org.forgeos.version": ""}},
                    {"Labels": {}}, {}, {"Labels": {"org.forgeos.version": "45.20261120.56"}}):
            self.assertIsNotNone(self.r.label_problem(bad, "45.20261120.57"), bad)

    def run_cli(self, *args, env=None, stdin=None):
        return subprocess.run([sys.executable, "-I", SCRIPT, *args], capture_output=True, text=True, timeout=30,
                              env=dict(os.environ, **(env or {})), input=stdin)

    def test_plan_and_write_round_trip(self):
        inspect = os.path.join(self.tmp, "inspect.json")
        with open(inspect, "w") as f:
            json.dump({"Labels": {"org.opencontainers.image.version": "45.20261119"}}, f)
        out = os.path.join(self.tmp, "gh_output")
        r = self.run_cli("plan", "--inspect", inspect, "--github-output", out,
                         env={"GITHUB_REF": "refs/heads/testing", "GITHUB_EVENT_NAME": "push", "GITHUB_RUN_NUMBER": "57",
                              "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": SHA})
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out) as f:
            outputs = dict(line.split("=", 1) for line in f.read().splitlines())
        self.assertEqual(outputs["fedora"], "45")
        self.assertEqual(outputs["channel"], "testing")
        self.assertEqual(outputs["built_tag"], "0123456-candidate-45")
        self.assertRegex(outputs["version"], r"^45\.\d{8}\.57$")
        info = json.loads(outputs["version_json"])
        target = os.path.join(self.tmp, "files/system/usr/share/forge/version.json")
        envfile = os.path.join(self.tmp, "gh_env")
        r = self.run_cli("write", "--out", target, "--github-env", envfile,
                         env={"FORGE_VERSION_JSON": outputs["version_json"]})
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(target) as f:
            self.assertEqual(json.load(f), info)
        with open(envfile) as f:
            self.assertEqual(f.read(), f"FORGE_VERSION={info['version']}\n")
        import forgerelease
        got = forgerelease.read_version(target)
        self.assertEqual(got["version"], info["version"])
        self.assertEqual(forgerelease.version_text(got), f"{info['version']} (testing, 0123456)")

    def test_write_refuses_tampered_values(self):
        target = os.path.join(self.tmp, "v.json")
        good = self.r.make_version(45, datetime.datetime(2026, 11, 20, tzinfo=datetime.timezone.utc), "3", SHA, "stable")
        for change in ({"version": "45.20261120.3\nFOO=1"}, {"channel": "evil"}, {"extra": 1}, {"commit": "x"},
                       {"version": "46.20261120.3"}):
            bad = json.dumps(dict(good, **change))
            r = self.run_cli("write", "--out", target, "--github-env", os.path.join(self.tmp, "env"),
                             env={"FORGE_VERSION_JSON": bad})
            self.assertNotEqual(r.returncode, 0, change)
            self.assertFalse(os.path.exists(target), change)

    def test_bad_label_fails_the_plan(self):
        inspect = os.path.join(self.tmp, "inspect.json")
        with open(inspect, "w") as f:
            json.dump({"Labels": {"org.opencontainers.image.version": "stable"}}, f)
        r = self.run_cli("plan", "--inspect", inspect, "--github-output", os.path.join(self.tmp, "o"),
                         env={"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push", "GITHUB_RUN_NUMBER": "1",
                              "GITHUB_SHA": SHA})
        self.assertNotEqual(r.returncode, 0)


class ReadVersionTests(unittest.TestCase):
    def setUp(self):
        import forgerelease
        self.fr = forgerelease
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, obj, name="v.json"):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))
        return p

    def test_missing_or_bad_files_read_as_unknown(self):
        good = {"format": 1, "version": "45.20261120.57", "commit": SHA, "channel": "stable",
                "built": "2026-11-20T06:00:05Z"}
        self.assertEqual(self.fr.read_version(self.write(good))["version"], "45.20261120.57")
        self.assertIsNone(self.fr.read_version(os.path.join(self.tmp, "nope")))
        for bad in ("{", "[]", dict(good, format=2), dict(good, version="45.1"), dict(good, commit="XYZ"),
                    dict(good, channel="nightly"), "x" * 5000):
            self.assertIsNone(self.fr.read_version(self.write(bad)), bad)
        link = os.path.join(self.tmp, "link.json")
        os.symlink(self.write(good), link)
        self.assertIsNone(self.fr.read_version(link), "a link is never followed")
        self.assertEqual(self.fr.version_text(None), "unknown (built without the release pipeline)")

    def test_ci_never_writes_the_file_into_the_repo(self):
        with open(os.path.join(ROOT, ".gitignore")) as f:
            self.assertIn("files/system/usr/share/forge/version.json", f.read().split())
        self.assertFalse(os.path.exists(os.path.join(ROOT, "files/system/usr/share/forge/version.json")))


class DashboardVersionTests(unittest.TestCase):
    def test_updates_page_carries_the_forge_version(self):
        os.environ.setdefault("FORGE_SCHEMA", SCHEMA)
        import forgedash
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p = os.path.join(tmp, "v.json")
        with open(p, "w") as f:
            json.dump({"format": 1, "version": "45.20261120.57", "commit": SHA, "channel": "testing"}, f)
        with mock.patch.object(forgedash, "core_data", return_value=({"ok": True}, None)), \
                mock.patch.dict(os.environ, {"FORGE_VERSION_FILE": p}):
            d = forgedash.Dashboard().updates()
        self.assertEqual(d["forge_version"], "45.20261120.57 (testing, 0123456)")
        page = open(os.path.join(LIB, "dashboard.html")).read()
        self.assertIn("forge_version", page)


@unittest.skipUnless(os.geteuid() == 0, "needs root to run the server and clients as other uids")
class SettingsPageReleaseTests(unittest.TestCase):
    """The Settings page shows the version and only switches the channel with an explicit confirmation."""

    def setUp(self):
        import test_settings_page as sp
        self.sp = sp
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o777)
        self.args = os.path.join(self.tmp, "args")
        fakebin = os.path.join(self.tmp, "bin")
        os.mkdir(fakebin)
        for name, text in (("pkexec", '#!/bin/sh\nexec "$@"\n'),
                           ("forge-apply", f'#!/bin/sh\necho "$@" > "{self.args}"\n')):
            p = os.path.join(fakebin, name)
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, 0o755)
        vfile = os.path.join(self.tmp, "version.json")
        with open(vfile, "w") as f:
            json.dump({"format": 1, "version": "45.20261120.57", "commit": SHA, "channel": "stable"}, f)
        os.chmod(vfile, 0o644)
        self.env = dict(os.environ, FORGE_LIB=LIB, FORGE_SCHEMA=SCHEMA, FORGE_VERSION_FILE=vfile,
                        FORGE_CONF=os.path.join(self.tmp, "settings.json"), FORGE_NO_BROWSER="1",
                        FORGE_PORT="0", FORGE_APPLY=os.path.join(fakebin, "forge-apply"),
                        PATH=fakebin + os.pathsep + os.environ.get("PATH", ""))
        self.proc = None

    def tearDown(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc.stdout.close()
            self.proc.stderr.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self):
        import select
        import time
        self.proc = subprocess.Popen([sys.executable, "-u", APP], env=self.env, preexec_fn=self.sp.as_uid(self.sp.OWNER),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        self.assertTrue(ready)
        m = re.search(r"(http://127\.0\.0\.1:\d+)/(?:\?k=(\S+))?", self.proc.stdout.readline())
        time.sleep(0.2)
        base, k = m.group(1), m.group(2) or ""
        status, body = self.sp.request(self.sp.OWNER, "GET", f"{base}/?k={k}")
        self.assertEqual(status, 200)
        return base, body, {"X-Forge-Token": self.sp.token_of(body), "Content-Type": "application/json"}

    def test_version_is_shown(self):
        base, page, h = self.start()
        status, body = self.sp.request(self.sp.OWNER, "GET", f"{base}/api/settings", h)
        self.assertEqual(json.loads(body)["forge_version"], "45.20261120.57 (stable, 0123456)")
        self.assertIn("forge_version", page)

    def test_switch_needs_the_confirmed_channel_and_goes_to_forge_apply(self):
        base, page, h = self.start()
        self.assertIn("updates.channel", page, "the page asks before switching")
        values = {"updates.channel": "testing"}
        status, out = self.sp.request(self.sp.OWNER, "POST", f"{base}/api/save", h,
                                      json.dumps({"values": values, "switch_channel": "testing"}))
        self.assertTrue(json.loads(out)["ok"], out)
        with open(self.args) as f:
            argv = f.read().split()
        self.assertEqual(argv[0], "--import")
        self.assertEqual(argv[2:], ["--switch-channel", "testing", "--confirm"])
        for bad in ({"values": {"updates.channel": "stable"}, "switch_channel": "testing"},
                    {"values": values, "switch_channel": "nightly"},
                    {"values": values, "switch_channel": ["testing"]}):
            if os.path.exists(self.args):
                os.unlink(self.args)
            status, out = self.sp.request(self.sp.OWNER, "POST", f"{base}/api/save", h, json.dumps(bad))
            self.assertFalse(json.loads(out)["ok"], bad)
            self.assertFalse(os.path.exists(self.args), bad)
        # A normal save never asks forge-apply to switch.
        status, out = self.sp.request(self.sp.OWNER, "POST", f"{base}/api/save", h, json.dumps({"values": values}))
        with open(self.args) as f:
            self.assertNotIn("--switch-channel", f.read())


if __name__ == "__main__":
    unittest.main()
