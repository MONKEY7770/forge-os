"""AI model storage limit: the rule, the settings, the pull check, the watcher and the setup page."""
import errno
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "files/system/usr/lib/forge")
SCHEMA = os.path.join(ROOT, "files/system/usr/share/forge/settings.json")
GUARD = os.path.join(ROOT, "files/system/usr/libexec/forge-model-guard")
APPLY = os.path.join(ROOT, "files/system/usr/libexec/forge-apply")
HEALTH = os.path.join(ROOT, "files/system/usr/bin/forge-health")
UNITS = os.path.join(ROOT, "files/system/usr/lib/systemd/system")
TMPFILES = os.path.join(ROOT, "files/system/usr/lib/tmpfiles.d/forge-ai-storage.conf")
PAGE = os.path.join(ROOT, "files/system/usr/share/plasma/packages/org.forgeos.setup.aistorage")
I18N = "/tmp/i18n-storage.json"
sys.path.insert(0, LIB)
os.environ["FORGE_SCHEMA"] = SCHEMA

import forgecfg  # noqa: E402
from forgecore import storage as S  # noqa: E402
from forgecore import models as M  # noqa: E402

GB = 1000 ** 3


def defaults(**changes):
    vals = forgecfg.values(overrides={})
    for k, v in changes.items():
        vals["ai.storage_" + k] = v
    return vals


def load_guard():
    loader = importlib.machinery.SourceFileLoader("forge_model_guard_under_test", GUARD)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class RuleTest(unittest.TestCase):
    """The default reserve: 30% of the disk, rounded to 10 GB, at least 128 GB, at most 600 GB."""

    def test_auto_reserve_for_common_disks(self):
        for total_gb, reserve_gb in ((256, 128), (512, 150), (1000, 300), (2000, 600), (4000, 600)):
            self.assertEqual(S.auto_reserve_bytes(total_gb * GB), reserve_gb * GB, total_gb)

    def test_a_1tb_disk_as_the_filesystem_reports_it_keeps_300_gb(self):
        # A "1 TB" drive shows a little less than 1000 GB once formatted.
        self.assertEqual(S.auto_reserve_bytes(984 * GB), 300 * GB)

    def test_reserve_never_more_than_the_disk(self):
        self.assertEqual(S.auto_reserve_bytes(100 * GB), 100 * GB)
        self.assertEqual(S.reserve_bytes(100 * GB, defaults(reserve_mode="gb", reserve_gb=500)), 100 * GB)

    def test_reserve_modes(self):
        total = 1000 * GB
        self.assertEqual(S.reserve_bytes(total, defaults()), 300 * GB)
        self.assertEqual(S.reserve_bytes(total, defaults(reserve_mode="percent", reserve_pct=50)), 500 * GB)
        self.assertEqual(S.reserve_bytes(total, defaults(reserve_mode="percent", reserve_pct=0)), 0)
        self.assertEqual(S.reserve_bytes(total, defaults(reserve_mode="gb", reserve_gb=200)), 200 * GB)

    def test_empty_disks_of_every_size(self):
        # Fake numbers: an empty disk, nothing but AI models on it. Room = budget = disk minus reserve.
        for total_gb, room_gb in ((256, 128), (512, 362), (1000, 700), (2000, 1400), (4000, 3400)):
            r = S.compute(total_gb * GB, total_gb * GB, 0, 0, defaults())
            self.assertEqual(r["room"], room_gb * GB, total_gb)
            self.assertEqual(r["budget"], room_gb * GB, total_gb)
            self.assertEqual(r["status"], "ok")

    def test_used_disks_of_every_size(self):
        # Each disk: 60% used by games and files, 10% by models. AI room = min(budget left, free left).
        for total_gb in (256, 512, 1000, 2000, 4000):
            total = total_gb * GB
            models, other = total // 10, total * 6 // 10
            free = total - models - other
            r = S.compute(total, free, models, 0, defaults())
            reserve = S.auto_reserve_bytes(total)
            budget_left = total - reserve - models
            free_left = free - max(max(0, reserve - other), 30 * GB)
            self.assertEqual(r["room"], min(budget_left, free_left), total_gb)
            self.assertEqual(r["other_used"], other)

    def test_games_used_more_than_the_reserve_only_the_floor_is_kept(self):
        # 1 TB disk: 650 GB of games (more than the 300 GB reserve), 100 GB of models, 250 GB free.
        r = S.compute(1000 * GB, 250 * GB, 100 * GB, 0, defaults())
        self.assertEqual(r["need_free"], 30 * GB)
        self.assertEqual(r["room"], 220 * GB)   # free 250 minus the 30 GB floor

    def test_models_over_the_budget(self):
        r = S.compute(1000 * GB, 200 * GB, 750 * GB, 0, defaults())
        self.assertEqual(r["room"], -50 * GB)
        self.assertEqual(r["status"], "over_budget")
        self.assertIn("over_budget", r["problems"])

    def test_free_space_below_the_floor(self):
        # Custom budget bigger than the disk: only free space limits, and the floor wins.
        r = S.compute(1000 * GB, 20 * GB, 100 * GB, 0, defaults(budget_gb=5000, reserve_mode="gb", reserve_gb=0))
        self.assertEqual(r["room"], -10 * GB)
        self.assertEqual(r["status"], "reserve")

    def test_custom_budget(self):
        r = S.compute(1000 * GB, 900 * GB, 50 * GB, 0, defaults(budget_gb=100))
        self.assertEqual(r["budget"], 100 * GB)
        self.assertEqual(r["room"], 50 * GB)

    def test_unfinished_downloads_count_at_full_size(self):
        # A 40 GB download that has written 10 GB so far: 30 GB more will be used.
        # On disk: 10 GB of models, 300 GB of other files (exactly the reserve), so only the floor is kept.
        r = S.compute(1000 * GB, 690 * GB, 40 * GB, 30 * GB, defaults())
        self.assertEqual(r["free_eff"], 660 * GB)
        self.assertEqual(r["other_used"], 300 * GB)
        self.assertEqual(r["room"], 630 * GB)

    def test_low_room(self):
        r = S.compute(1000 * GB, 300 * GB, 690 * GB, 0, defaults())
        self.assertEqual(r["room"], 10 * GB)
        self.assertEqual(r["status"], "low")

    def test_limit_off_has_no_problems(self):
        r = S.compute(1000 * GB, 200 * GB, 750 * GB, 0, defaults(limit="off"))
        self.assertEqual(r["status"], "off")
        self.assertEqual(r["problems"], [])

    def test_problem_lines(self):
        self.assertEqual(S.problem_lines(S.compute(1000 * GB, 200 * GB, 750 * GB, 0, defaults())),
                         ["AI models use 750 GB, more than their limit of 700 GB"])
        self.assertEqual(S.problem_lines(S.compute(1000 * GB, 300 * GB, 690 * GB, 0, defaults())),
                         ["Little room left for AI models: 10 GB"])
        self.assertEqual(S.problem_lines(S.compute(1000 * GB, 900 * GB, 0, 0, defaults())), [])


class DiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def fake_statvfs(self, calls):
        def f(p):
            calls.append(p)
            if not os.path.exists(p):
                raise FileNotFoundError(errno.ENOENT, "missing", p)
            return os.statvfs_result((4096, 4096, 1000 * GB // 4096, 500 * GB // 4096, 400 * GB // 4096,
                                      0, 0, 0, 0, 255))
        return f

    def test_nearest_existing_parent(self):
        calls = []
        d = S.disk_of(os.path.join(self.tmp, "a/b/models"), statvfs=self.fake_statvfs(calls))
        self.assertEqual(d["path"], self.tmp)
        self.assertEqual(d["total"], (1000 * GB // 4096) * 4096)
        self.assertEqual(d["free"], (400 * GB // 4096) * 4096)   # f_bavail: what normal users get

    def test_permission_problem_means_not_sure(self):
        def denied(p):
            raise PermissionError(errno.EACCES, "denied")
        self.assertIsNone(S.disk_of(self.tmp, statvfs=denied))

    def test_models_usage(self):
        blobs = os.path.join(self.tmp, "models/blobs")
        os.makedirs(blobs)
        with open(os.path.join(blobs, "sha256-aa"), "wb") as f:
            f.write(b"x" * 5000)
        with open(os.path.join(blobs, "sha256-bb-partial"), "wb") as f:
            f.truncate(10 * 1024 * 1024)     # Ollama sizes the file first, then fills it
        outside = os.path.join(self.tmp, "big")
        with open(outside, "wb") as f:
            f.write(b"y" * 70000)
        os.symlink(outside, os.path.join(blobs, "sha256-link"))
        os.mkdir(os.path.join(blobs, "sha256-dir"))
        u = S.models_usage(os.path.join(self.tmp, "models"))
        part = os.stat(os.path.join(blobs, "sha256-bb-partial")).st_blocks * 512
        # Links and folders are not counted; an unfinished download counts what is written so far,
        # and the rest is kept free while it is still growing.
        self.assertEqual(u["models"], 5000 + part)
        self.assertEqual(u["pending"], 10 * 1024 * 1024 - part)
        self.assertEqual(u["partials"], 1)

    def test_an_abandoned_download_reserves_nothing_more(self):
        # A refused or stopped download leaves a sized, mostly empty file: it must not keep the PC
        # "over budget" (which would refuse every later download and restart Ollama).
        blobs = os.path.join(self.tmp, "models/blobs")
        os.makedirs(blobs)
        p = os.path.join(blobs, "sha256-cc-partial")
        with open(p, "wb") as f:
            f.truncate(60 * 1024 * 1024)
        old = time.time() - 600
        os.utime(p, (old, old))
        u = S.models_usage(os.path.join(self.tmp, "models"))
        self.assertEqual(u["models"], os.stat(p).st_blocks * 512)
        self.assertEqual(u["pending"], 0)
        self.assertEqual(u["partials"], 0)

    def test_a_linked_blobs_folder_is_not_followed(self):
        # Root must not read (and publish sizes of) a folder the link points to.
        secret = os.path.join(self.tmp, "secret")
        os.makedirs(secret)
        with open(os.path.join(secret, "f"), "wb") as f:
            f.write(b"s" * 4000)
        os.makedirs(os.path.join(self.tmp, "models"))
        os.symlink(secret, os.path.join(self.tmp, "models/blobs"))
        self.assertIsNone(S.models_usage(os.path.join(self.tmp, "models")))

    def test_models_usage_missing_folder_is_zero(self):
        self.assertEqual(S.models_usage(os.path.join(self.tmp, "none"))["models"], 0)

    def test_models_usage_unreadable_is_not_sure(self):
        with mock.patch("os.scandir", side_effect=PermissionError(errno.EACCES, "denied")):
            os.makedirs(os.path.join(self.tmp, "m/blobs"))
            self.assertIsNone(S.models_usage(os.path.join(self.tmp, "m")))


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.defs = forgecfg.settings_by_key()

    def test_schema_entries(self):
        want = {"ai.storage_limit": ("select", "block"), "ai.storage_reserve_mode": ("select", "auto"),
                "ai.storage_reserve_pct": ("number", 30), "ai.storage_reserve_gb": ("number", 300),
                "ai.storage_budget_gb": ("number", 0), "ai.storage_min_free_gb": ("number", 30)}
        for k, (t, d) in want.items():
            s = self.defs[k]
            self.assertEqual((s["type"], s["default"], s["group"], s.get("machine")), (t, d, "ai", True), k)
            self.assertTrue(s["label"] and s["help"], k)
        self.assertEqual(self.defs["ai.storage_limit"]["options"], ["block", "warn", "off"])
        self.assertEqual(self.defs["ai.storage_reserve_mode"]["options"], ["auto", "percent", "gb"])
        self.assertEqual((self.defs["ai.storage_reserve_pct"]["min"], self.defs["ai.storage_reserve_pct"]["max"]), (0, 95))
        self.assertEqual((self.defs["ai.storage_reserve_gb"]["min"], self.defs["ai.storage_reserve_gb"]["max"]), (0, 100000))
        self.assertEqual((self.defs["ai.storage_budget_gb"]["min"], self.defs["ai.storage_budget_gb"]["max"]), (0, 100000))
        self.assertEqual((self.defs["ai.storage_min_free_gb"]["min"], self.defs["ai.storage_min_free_gb"]["max"]), (5, 1000))
        self.assertTrue(self.defs["ai.storage_min_free_gb"].get("advanced"))
        for k in want:
            if k != "ai.storage_min_free_gb":
                self.assertFalse(self.defs[k].get("advanced"), k)

    def test_validation(self):
        def errs(**c):
            v = forgecfg.values(overrides={})
            v.update(c)
            return forgecfg.validate(v)[1]
        self.assertEqual(errs(**{"ai.storage_limit": "warn", "ai.storage_reserve_mode": "gb",
                                 "ai.storage_reserve_gb": 0, "ai.storage_budget_gb": 100000}), [])
        for bad in ({"ai.storage_limit": "maybe"}, {"ai.storage_reserve_mode": "half"},
                    {"ai.storage_reserve_pct": 96}, {"ai.storage_reserve_pct": -1}, {"ai.storage_reserve_pct": 30.5},
                    {"ai.storage_reserve_gb": 100001}, {"ai.storage_budget_gb": -5}, {"ai.storage_budget_gb": "nan"},
                    {"ai.storage_min_free_gb": 4}, {"ai.storage_min_free_gb": 1001}):
            self.assertTrue(errs(**bad), bad)

    def test_strict_loader_fails_open(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        conf = os.path.join(tmp, "settings.json")
        self.assertEqual(S.load_settings(SCHEMA, conf)["ai.storage_limit"], "block")   # missing: defaults
        for text in ("{broken", "[1]", json.dumps({"ai.storage_reserve_pct": 500})):
            with open(conf, "w") as f:
                f.write(text)
            self.assertIsNone(S.load_settings(SCHEMA, conf), text)
        with open(conf, "w") as f:
            json.dump({"ai.storage_limit": "warn"}, f)
        self.assertEqual(S.load_settings(SCHEMA, conf)["ai.storage_limit"], "warn")


class PullCheckTest(unittest.TestCase):
    MSG = ("Not downloaded: qwen3:32b needs about 20.0 GB, but only 10.0 GB is left for AI models. "
           "Delete a model you no longer use, or raise the limit in Forge Settings.")

    def steps(self):
        return [{"status": "pulling manifest"},
                {"status": "pulling aaa", "digest": "sha256:aaa", "total": 20 * GB, "completed": 0},
                {"status": "pulling aaa", "digest": "sha256:aaa", "total": 20 * GB, "completed": 5 * GB},
                {"status": "success"}]

    def test_block_refuses_with_the_owner_message(self):
        g = S.PullGuard("qwen3:32b", 10 * GB, "block")
        self.assertIsNone(g.before())
        out = [g.observe(s) for s in self.steps()[:2]]
        self.assertEqual(out[0], None)
        self.assertEqual(out[1], ("error", self.MSG))

    def test_warn_only_warns_once(self):
        g = S.PullGuard("qwen3:32b", 10 * GB, "warn")
        out = [g.observe(s) for s in self.steps()]
        self.assertEqual([o for o in out if o], [("warning", self.MSG.replace("Not downloaded", "Warning"))])

    def test_off_never_checks(self):
        g = S.PullGuard("x", -10 * GB, "off")
        self.assertIsNone(g.before())
        self.assertEqual([g.observe(s) for s in self.steps()], [None] * 4)

    def test_layers_already_on_disk_need_no_room(self):
        g = S.PullGuard("x", 10 * GB, "block")
        self.assertIsNone(g.observe({"status": "pulling b", "digest": "sha256:b", "total": 30 * GB,
                                     "completed": 30 * GB}))
        self.assertIsNone(g.observe({"status": "pulling c", "digest": "sha256:c", "total": 4 * GB, "completed": 0}))
        self.assertEqual(g.observe({"status": "pulling d", "digest": "sha256:d", "total": 7 * GB,
                                    "completed": 0})[0], "error")

    def test_unknown_size_refuses_only_without_room(self):
        self.assertIsNone(S.PullGuard("x", 1, "block").before())
        kind, msg = S.PullGuard("x", 0, "block").before()
        self.assertEqual(kind, "error")
        self.assertTrue(msg.startswith("Not downloaded: x needs more room, but only 0.0 GB is left for AI models."))
        self.assertEqual(S.PullGuard("x", -GB, "warn").before()[0], "warning")
        self.assertIsNone(S.PullGuard("x", None, "block").before())   # room not known: never refuse

    def test_client_pull_stops_with_the_guard(self):
        client = M.OllamaClient("http://127.0.0.1:1")

        class Resp:
            status = 200

            def __iter__(self):
                return iter(json.dumps(s).encode() + b"\n" for s in PullCheckTest.steps(None))

        class Conn:
            closed = False

            def __init__(self, *a, **k):
                self.sock = mock.Mock()

            def connect(self):
                pass

            def request(self, *a, **k):
                pass

            def getresponse(self):
                return Resp()

            def close(self):
                Conn.closed = True

        with mock.patch("http.client.HTTPConnection", Conn):
            with self.assertRaises(M.OllamaError) as cm:
                list(client.pull("qwen3:32b", guard=S.PullGuard("qwen3:32b", 10 * GB, "block")))
        self.assertEqual(str(cm.exception), self.MSG)
        self.assertTrue(Conn.closed)


class CoreRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ.setdefault("FORGE_CORE_RUN", os.path.join(cls.tmp, "run"))
        os.environ.setdefault("FORGE_CORE_DATA", os.path.join(cls.tmp, "data"))
        os.environ["FORGE_LIB"] = LIB
        from fastapi.testclient import TestClient
        from forgecore import app as core
        cls.core, cls.TestClient = core, TestClient

    def client(self, steps):
        class FakeOllama:
            closed = False

            def available(self):
                return True

            def pull(self, name):
                try:
                    for s in steps:
                        yield s
                finally:
                    FakeOllama.closed = True

        class FakeCollector:
            def current(self):
                return {}
        self.fake = FakeOllama
        app = self.core.create_app(token="t", collector=FakeCollector(), ollama=FakeOllama(), start_collector=False,
                                   data_dir=os.path.join(self.tmp, "data"))
        return self.TestClient(app)

    def pull(self, steps, room, limit):
        with mock.patch.object(S, "room_for_pull", return_value=(room, limit)):
            return self.client(steps).post("/api/models/pull", headers={"X-Forge-Token": "t"},
                                           json={"name": "qwen3:32b"})

    def test_refused_up_front_when_no_room(self):
        r = self.pull([{"status": "success"}], 0, "block")
        self.assertEqual(r.status_code, 507)
        self.assertIn("is left for AI models", r.json()["detail"])

    def test_refused_when_the_size_is_known(self):
        steps = PullCheckTest.steps(None)
        r = self.pull(steps, 10 * GB, "block")
        lines = [json.loads(x) for x in r.text.strip().splitlines()]
        self.assertEqual(lines[-1], {"error": PullCheckTest.MSG})
        self.assertNotIn({"status": "success"}, lines)
        self.assertTrue(self.fake.closed)

    def test_warn_passes_through(self):
        r = self.pull(PullCheckTest.steps(None), 10 * GB, "warn")
        lines = [json.loads(x) for x in r.text.strip().splitlines()]
        self.assertEqual(lines[-1], {"status": "success"})
        self.assertTrue(any("warning" in x for x in lines))

    def test_not_sure_never_blocks(self):
        r = self.pull(PullCheckTest.steps(None), None, "block")
        self.assertEqual([json.loads(x) for x in r.text.strip().splitlines()][-1], {"status": "success"})


class WatcherDecisionTest(unittest.TestCase):
    def setUp(self):
        self.g = load_guard()

    def rep(self, room_gb, limit="block"):
        r = S.compute(1000 * GB, 300 * GB, 700 * GB - room_gb * GB, 0, defaults(limit=limit))
        r["limit"] = limit
        return r

    def test_over_and_downloading_restarts(self):
        acts, st = self.g.decide(self.rep(-5), {}, now=1000, ollama_active=True, downloading=True)
        self.assertEqual(acts, ["restart", "notify"])
        self.assertEqual(st["last_restart"], 1000)

    def test_restarts_are_rate_limited(self):
        acts, st = self.g.decide(self.rep(-5), {"last_restart": 900, "notified": "over"}, now=1000,
                                 ollama_active=True, downloading=True)
        self.assertEqual(acts, [])
        acts, _ = self.g.decide(self.rep(-5), st, now=900 + self.g.RESTART_MIN_S, ollama_active=True,
                                downloading=True)
        self.assertEqual(acts, ["restart", "notify"])

    def test_no_restart_without_a_download_or_when_ollama_is_off(self):
        acts, st = self.g.decide(self.rep(-5), {}, now=1000, ollama_active=True, downloading=False)
        self.assertEqual(acts, ["notify"])
        acts, _ = self.g.decide(self.rep(-5), st, now=1100, ollama_active=True, downloading=False)
        self.assertEqual(acts, [])     # once per problem
        acts, _ = self.g.decide(self.rep(-5), {}, now=1000, ollama_active=False, downloading=True)
        self.assertEqual(acts, ["notify"])

    def test_warn_only_notifies_once(self):
        acts, st = self.g.decide(self.rep(-5, "warn"), {}, now=1000, ollama_active=True, downloading=True)
        self.assertEqual(acts, ["notify"])
        acts, _ = self.g.decide(self.rep(-5, "warn"), st, now=1030, ollama_active=True, downloading=True)
        self.assertEqual(acts, [])

    def test_off_and_unknown_do_nothing(self):
        self.assertEqual(self.g.decide(self.rep(-5, "off"), {}, now=1, ollama_active=True, downloading=True)[0], [])
        self.assertEqual(self.g.decide(None, {}, now=1, ollama_active=True, downloading=True)[0], [])

    def test_problem_gone_resets_the_notice(self):
        _, st = self.g.decide(self.rep(-5), {}, now=1000, ollama_active=True, downloading=False)
        _, st = self.g.decide(self.rep(50), st, now=1030, ollama_active=True, downloading=False)
        self.assertIsNone(st.get("notified"))
        acts, _ = self.g.decide(self.rep(-5), st, now=1060, ollama_active=True, downloading=False)
        self.assertEqual(acts, ["notify"])

    def test_clock_going_back_does_not_block_restarts_forever(self):
        acts, _ = self.g.decide(self.rep(-5), {"last_restart": 10 ** 10, "notified": "over"}, now=1000,
                                ollama_active=True, downloading=True)
        self.assertIn("restart", acts)


class WatcherRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.g = load_guard()
        self.g.STATE_DIR = os.path.join(self.tmp, "run/forge")
        self.g.CONF = os.path.join(self.tmp, "settings.json")
        self.g.SCHEMA = SCHEMA
        self.models = os.path.join(self.tmp, "models")
        os.makedirs(os.path.join(self.models, "blobs"))
        with open(self.g.CONF, "w") as f:
            json.dump({"ai.models_dir": "/var/lib/ollama/models"}, f)
        self.cmds = []
        self.g.run_cmd = lambda cmd, timeout=60: (self.cmds.append(cmd), (0, ""))[1]
        self.g.models_path = lambda vals: self.models

    def statvfs(self, free_gb, total_gb=1000):
        return lambda p: os.statvfs_result((4096, 4096, total_gb * GB // 4096, free_gb * GB // 4096,
                                            free_gb * GB // 4096, 0, 0, 0, 0, 255))

    def test_writes_state_and_wizard_disk_info(self):
        with mock.patch.object(S.os, "statvfs", self.statvfs(900)):
            self.assertEqual(self.g.main([]), 0)
        st = json.load(open(os.path.join(self.g.STATE_DIR, "ai-storage.json")))
        self.assertEqual(st["status"], "ok")
        self.assertEqual(st["reserve_gb"], 300)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.g.STATE_DIR, "ai-storage.json")).st_mode), 0o644)
        ini = open(os.path.join(self.g.STATE_DIR, "disk.ini")).read()
        self.assertIn("[disk]\n", ini)
        self.assertIn("total_gb=1000\n", ini)
        self.assertIn("auto_reserve_gb=300\n", ini)
        self.assertEqual([c for c in self.cmds if "restart" in c], [])

    def test_a_finished_download_does_not_restart_ollama(self):
        # Models grew and a blob is new, but nothing is still downloading.
        with mock.patch.object(S, "models_usage", return_value={"models": 800 * GB, "pending": 0, "partials": 0,
                                                                 "newest_mtime": time.time()}), \
                mock.patch.object(S.os, "statvfs", self.statvfs(100)):
            self.g.ollama_active = lambda: True
            self.g.notify_users = lambda s, b: self.cmds.append(["notify", s, b])
            self.assertEqual(self.g.main([]), 0)
        self.assertEqual([c for c in self.cmds if "restart" in c], [])

    def test_restarts_ollama_and_never_deletes(self):
        blob = os.path.join(self.models, "blobs/sha256-x-partial")
        with open(blob, "wb") as f:
            f.write(b"z" * 1000)
        self.g.models_usage_override = None
        with mock.patch.object(S, "models_usage", return_value={"models": 800 * GB, "pending": 0, "partials": 1,
                                                                 "newest_mtime": time.time()}), \
                mock.patch.object(S.os, "statvfs", self.statvfs(100)):
            self.g.ollama_active = lambda: True
            self.g.notify_users = lambda s, b: self.cmds.append(["notify", s, b])
            self.assertEqual(self.g.main([]), 0)
        self.assertIn(["systemctl", "restart", "ollama.service"], self.cmds)
        self.assertTrue(any(c[0] == "notify" for c in self.cmds))
        self.assertTrue(os.path.exists(blob))
        st = json.load(open(os.path.join(self.g.STATE_DIR, "ai-storage.json")))
        self.assertEqual(st["status"], "over_budget")
        self.assertEqual(st["last_action"], "restart")

    def test_unreadable_settings_fail_open(self):
        with open(self.g.CONF, "w") as f:
            f.write("{oops")
        self.g.ollama_active = lambda: True
        with mock.patch.object(S.os, "statvfs", self.statvfs(1)):
            self.assertEqual(self.g.main([]), 0)
        self.assertEqual(self.cmds, [])
        st = json.load(open(os.path.join(self.g.STATE_DIR, "ai-storage.json")))
        self.assertEqual(st["status"], "unknown")

    def test_state_dir_that_is_a_link_is_refused(self):
        os.makedirs(os.path.join(self.tmp, "run"))
        os.symlink(self.tmp, self.g.STATE_DIR)
        with mock.patch.object(S.os, "statvfs", self.statvfs(900)):
            self.g.main([])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ai-storage.json")))


class SetupSpoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.g = load_guard()
        self.g.SPOOL_DIR = os.path.join(self.tmp, "spool")
        self.g.STATE_DIR = os.path.join(self.tmp, "run/forge")
        os.makedirs(self.g.SPOOL_DIR)
        self.spool = os.path.join(self.g.SPOOL_DIR, "storage.ini")
        self.imported = []

        def fake_run(cmd, timeout=60):
            if "--import" in cmd:
                self.imported.append(json.load(open(cmd[cmd.index("--import") + 1])))
                self.assertIn("--merge", cmd)
            return 0, ""
        self.g.run_cmd = fake_run

    def put(self, text):
        with open(self.spool, "w") as f:
            f.write(text)

    def test_a_folder_in_place_of_the_file_is_removed(self):
        # Otherwise the path unit would start the service again and again until systemd gives up.
        os.makedirs(os.path.join(self.spool, "inner"))
        with open(os.path.join(self.spool, "inner/x"), "w") as f:
            f.write("x")
        self.assertNotEqual(self.g.main(["--apply-setup"]), 0)
        self.assertFalse(os.path.lexists(self.spool))
        self.assertEqual(self.imported, [])

    def test_parse(self):
        p = self.g.parse_setup
        self.assertEqual(p("[storage]\nmode=percent\npct=40\n"),
                         {"ai.storage_reserve_mode": "percent", "ai.storage_reserve_pct": 40})
        self.assertEqual(p("[storage]\ngb=250\nmode=gb\n"), {"ai.storage_reserve_mode": "gb", "ai.storage_reserve_gb": 250})
        self.assertEqual(p("[storage]\nmode=auto\npct=30\ngb=300\n"), {"ai.storage_reserve_mode": "auto"})
        for bad in ("", "mode=auto\n", "[storage]\nmode=all\n", "[storage]\nmode=percent\n",
                    "[storage]\nmode=percent\npct=96\n", "[storage]\nmode=gb\ngb=100001\n",
                    "[storage]\nmode=gb\ngb=-1\n", "[storage]\nmode=gb\ngb=1e3\n", "[storage]\nmode=auto\nx=1\n",
                    "[storage]\nmode=auto\nmode=gb\n", "[other]\nmode=auto\n", "[storage]\nmode=auto\n[x]\n",
                    "[storage]\nmode=percent\npct=40.5\n", "[storage]\nmode=percent\npct= 40\n",
                    "[storage]\nmode=auto\n\x00\n"):
            self.assertIsNone(p(bad), repr(bad))

    def test_valid_choice_is_imported_and_the_spool_removed(self):
        self.put("[storage]\nmode=gb\ngb=200\n")
        self.assertEqual(self.g.main(["--apply-setup"]), 0)
        self.assertEqual(self.imported, [{"ai.storage_reserve_mode": "gb", "ai.storage_reserve_gb": 200}])
        self.assertFalse(os.path.exists(self.spool))
        self.assertEqual([f for f in os.listdir(self.g.STATE_DIR) if f.startswith("setup-")], [])

    def test_bad_choice_is_removed_and_not_imported(self):
        self.put("[storage]\nmode=gb\ngb=999999999\n")
        self.assertEqual(self.g.main(["--apply-setup"]), 2)
        self.assertEqual(self.imported, [])
        self.assertFalse(os.path.exists(self.spool))

    def test_link_fifo_and_big_files_are_refused(self):
        target = os.path.join(self.tmp, "secret")
        with open(target, "w") as f:
            f.write("[storage]\nmode=auto\n")
        os.symlink(target, self.spool)
        self.assertEqual(self.g.main(["--apply-setup"]), 2)
        self.assertTrue(os.path.exists(target))          # the link is removed, never its target
        self.assertFalse(os.path.lexists(self.spool))
        os.mkfifo(self.spool)
        self.assertEqual(self.g.main(["--apply-setup"]), 2)
        self.put("[storage]\nmode=auto\n" + "#" * 10000)
        self.assertEqual(self.g.main(["--apply-setup"]), 2)
        self.assertEqual(self.imported, [])

    def test_nothing_to_do(self):
        self.assertEqual(self.g.main(["--apply-setup"]), 0)
        self.assertEqual(self.imported, [])


class ApplyDropInTest(unittest.TestCase):
    def test_ollama_waits_for_the_models_drive(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        conf = os.path.join(root, "etc/forge/settings.json")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w") as f:
            json.dump({"ai.models_dir": "/var/mnt/games/models"}, f)
        env = dict(os.environ, FORGE_ROOT=root, FORGE_DRYRUN="1", FORGE_CONF=conf, PYTHONPATH=LIB)
        r = subprocess.run([sys.executable, APPLY], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = open(os.path.join(root, "etc/systemd/system/ollama.service.d/90-forge.conf")).read()
        self.assertIn("[Unit]\nRequiresMountsFor=/var/mnt/games/models\n", text)
        self.assertLess(text.index("[Unit]"), text.index("[Service]"))
        self.assertIn("ReadWritePaths=-/var/mnt/games/models\n", text)


class ApplyWhilePausedTest(unittest.TestCase):
    def run_apply(self, paused):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        conf = os.path.join(root, "etc/forge/settings.json")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w") as f:
            json.dump({"ai.ollama": True}, f)
        if paused:
            os.makedirs(os.path.join(root, "run/forge/gaming"))
            open(os.path.join(root, "run/forge/gaming/ollama-paused"), "w").close()
        env = dict(os.environ, FORGE_ROOT=root, FORGE_DRYRUN="1", FORGE_CONF=conf, PYTHONPATH=LIB)
        r = subprocess.run([sys.executable, APPLY], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_settings_apply_during_a_paused_game_keeps_ollama_paused(self):
        # Review: an Apply during a game restarted Ollama but left the pause in place.
        out = self.run_apply(paused=True)
        self.assertIn("would run: systemctl enable ollama.service", out)
        self.assertNotIn("restart ollama.service", out)
        self.assertNotIn("enable --now ollama.service", out)
        out = self.run_apply(paused=False)
        self.assertIn("would run: systemctl enable --now ollama.service", out)
        self.assertIn("would run: systemctl restart ollama.service", out)


class ReportTest(unittest.TestCase):
    def test_doctor_storage_section_has_ai_lines(self):
        from forgecore import doctor as D
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        rep = S.compute(1000 * GB, 200 * GB, 750 * GB, 0, defaults())
        with mock.patch.object(S, "current", return_value=dict(rep, limit="block")):
            sec = D.storage(root, run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        self.assertEqual(sec["status"], "warn")
        self.assertTrue(any("AI models use 750 GB" in i for i in sec["items"]))
        rep = S.compute(1000 * GB, 900 * GB, 0, 0, defaults())
        with mock.patch.object(S, "current", return_value=dict(rep, limit="block")):
            sec = D.storage(root, run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        self.assertTrue(any(i.startswith("AI models: 0 GB used, 700 GB left") for i in sec["items"]), sec)
        with mock.patch.object(S, "current", return_value=None):
            sec = D.storage(root, run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        self.assertTrue(any("not sure" in i for i in sec["items"]), sec)

    def test_state_keeps_the_space_kept_for_normal_use(self):
        # A user who can't read the model folder gets the numbers from the watcher's state; the
        # doctor must not then say "0 GB is kept for normal use".
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        rep = S.compute(1000 * GB, 300 * GB, 690 * GB, 0, defaults())
        with open(os.path.join(tmp, "ai-storage.json"), "w") as f:
            json.dump(S.state_doc(rep, "/var/lib/ollama/models", time.time()), f)
        with mock.patch.object(S, "load_settings", return_value=None), \
                mock.patch.object(S, "STATE_PATH", os.path.join(tmp, "ai-storage.json")):
            cur = S.current()
        self.assertEqual(cur["need_free"], S.gb_int(rep["need_free"]) * GB)
        self.assertGreater(cur["need_free"], 0)

    def test_problems_cli_reads_the_watcher_state(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        rep = S.compute(1000 * GB, 300 * GB, 690 * GB, 0, defaults())
        state = os.path.join(tmp, "ai-storage.json")
        with open(state, "w") as f:
            json.dump(S.state_doc(rep, "/var/lib/ollama/models", time.time()), f)
        r = subprocess.run([sys.executable, os.path.join(LIB, "forgecore/storage.py"), "--problems"],
                           env=dict(os.environ, FORGE_AI_STORAGE_STATE=state, PYTHONPATH=LIB),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "Little room left for AI models: 10 GB\n")

    def test_forge_health_reports_storage_problems(self):
        text = open(HEALTH).read()
        self.assertIn("forgecore/storage.py\" --problems", text)
        self.assertRegex(text, r'problems\+=\("\$line"\)')

    def test_shellcheck_clean(self):
        if not shutil.which("shellcheck"):
            self.skipTest("shellcheck is not installed")
        r = subprocess.run(["shellcheck", HEALTH], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


class PackagingTest(unittest.TestCase):
    def test_units(self):
        timer = open(os.path.join(UNITS, "forge-model-guard.timer")).read()
        self.assertIn("OnUnitActiveSec=30s", timer)
        self.assertIn("AccuracySec=", timer)
        svc = open(os.path.join(UNITS, "forge-model-guard.service")).read()
        self.assertIn("ExecStart=/usr/libexec/forge-model-guard\n", svc)
        self.assertIn("Type=oneshot", svc)
        path = open(os.path.join(UNITS, "forge-setup-storage.path")).read()
        self.assertIn("PathExists=/var/lib/forge-setup/storage.ini", path)
        svc = open(os.path.join(UNITS, "forge-setup-storage.service")).read()
        self.assertIn("ExecStart=/usr/libexec/forge-model-guard --apply-setup", svc)
        tmpf = open(TMPFILES).read()
        self.assertIn("d /var/lib/forge-setup 0700 plasma-setup plasma-setup -", tmpf)
        self.assertIn("d /run/forge 0755 root root -", tmpf)
        rec = open(os.path.join(ROOT, "recipes/common-modules.yml")).read()
        self.assertIn("- forge-model-guard.timer", rec)
        self.assertIn("- forge-setup-storage.path", rec)
        self.assertIn("/usr/libexec/forge-model-guard", open(os.path.join(ROOT, "files/scripts/finalize.sh")).read())

    def test_setup_page_package(self):
        meta = json.load(open(os.path.join(PAGE, "metadata.json")))
        self.assertEqual(meta["KPackageStructure"], "KDE/PlasmaSetup")
        self.assertEqual(meta["X-KDE-Weight"], 45)
        self.assertEqual(meta["KPlugin"]["Id"], "org.forgeos.setup.aistorage")
        qml = open(os.path.join(PAGE, "contents/ui/main.qml"), encoding="utf-8").read()
        self.assertIn("PlasmaSetupComponents.SetupModule", qml)
        # Plasma Setup 6.8.3 (as installed in this image): nextRequested is a signal, contentItem is
        # required, the wizard calls onPageActivated(), and pages name the wizard as their parent app.
        self.assertEqual(meta["X-KDE-ParentApp"], "org.kde.plasmasetup")
        for word in ("available:", "nextEnabled:", "onNextRequested:", "contentItem:", "function onPageActivated()",
                     "root.cardWidth", "file:///run/forge/disk.ini", "file:///var/lib/forge-setup/storage.ini"):
            self.assertIn(word, qml)
        self.assertNotIn("function nextRequested", qml)
        self.assertNotIn("anchors.centerIn: parent", qml)   # SetupModule is not a visual item

    def test_new_text_has_no_em_dash_or_double_hyphen(self):
        files = [GUARD, os.path.join(LIB, "forgecore/storage.py"), TMPFILES,
                 os.path.join(PAGE, "metadata.json"), os.path.join(PAGE, "contents/ui/main.qml")]
        files += [os.path.join(UNITS, n) for n in ("forge-model-guard.timer", "forge-model-guard.service",
                                                     "forge-setup-storage.path", "forge-setup-storage.service")]
        for p in files:
            text = open(p, encoding="utf-8").read()
            self.assertNotIn("—", text, p)
            # "--" as a command line argument ("--") or an option (--merge) is code, not punctuation.
            self.assertNotRegex(text, r"(?<![-\w\"])--(?![a-z\"])", p)
        for s in forgecfg.settings_by_key().values():
            if s["key"].startswith("ai.storage_"):
                self.assertNotIn("—", s["label"] + s["help"])
                self.assertNotIn("--", s["label"] + s["help"])

    def test_translations(self):
        if not os.path.exists(I18N):
            self.skipTest("the new strings were already merged into the i18n tables")
        data = json.load(open(I18N, encoding="utf-8"))
        en, th = data["en"], data["th"]
        self.assertEqual(sorted(en), sorted(th))
        need = set()
        for s in forgecfg.settings_by_key().values():
            if s["key"].startswith("ai.storage_"):
                need |= {f"set.{s['key']}.label", f"set.{s['key']}.help"}
                need |= {f"set.opt.{o}" for o in s.get("options", []) if o != "off"}
        self.assertEqual(sorted(need - set(th)), [])
        for k in need:
            if k.endswith((".label", ".help")) and k.startswith("set.ai."):
                key, part = k[4:].rsplit(".", 1)
                self.assertEqual(en[k], forgecfg.settings_by_key()[key][part], k)
        for lang, t in (("en", en), ("th", th)):
            for k, v in t.items():
                self.assertNotIn("—", v, k)
                self.assertNotRegex(v, r"--(?![a-z])", k)
        thai = sum(1 for v in th.values() if re.search(r"[฀-๿]", v))
        self.assertGreater(thai / len(th), 0.8)


if __name__ == "__main__":
    unittest.main()
