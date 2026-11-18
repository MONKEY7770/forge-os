import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, "/home/claude/forge-os-repo/files/system/usr/lib/forge")

from forgecore import metrics  # noqa: E402
from forgecore.metrics import Collector, NAMES  # noqa: E402

MEMINFO = """MemTotal:       16777216 kB
MemFree:         1000000 kB
MemAvailable:    8388608 kB
SwapTotal:       4194304 kB
SwapFree:        3145728 kB
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.proc = os.path.join(self.dir, "proc")
        os.makedirs(self.proc)
        self._write("stat", "cpu  100 0 100 800 0 0 0 0 0 0\n")
        self._write("loadavg", "1.25 0.80 0.50 1/200 1234\n")
        self._write("meminfo", MEMINFO)
        self.old_env = dict(os.environ)
        os.environ["FORGE_PROC"] = self.proc
        os.environ["FORGE_NVIDIA_SMI"] = os.path.join(self.dir, "missing-nvidia-smi")
        self.db = os.path.join(self.dir, "sub", "metrics.db")
        self.col = Collector(self.db, interval_s=0.1)

    def tearDown(self):
        self.col.stop()
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def _write(self, name, text):
        with open(os.path.join(self.proc, name), "w") as f:
            f.write(text)

    def _fake_smi(self, line):
        path = os.path.join(self.dir, "nvidia-smi")
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\necho '{line}'\necho '1, 2, 3, 4, 5'\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        os.environ["FORGE_NVIDIA_SMI"] = path


class SampleTests(Base):
    def test_sample_with_fake_proc_and_gpu(self):
        self._fake_smi("65, 40, 2048, 8192, 120.5")
        s = self.col.sample()
        self.assertIsInstance(s["ts"], int)
        self.assertEqual(set(s), {"ts", *NAMES})
        self.assertEqual(s["load1"], 1.25)
        self.assertEqual(s["mem_total_gb"], 16.0)
        self.assertEqual(s["mem_used_gb"], 8.0)
        self.assertEqual(s["swap_used_gb"], 1.0)
        self.assertIsNotNone(s["disk_root_pct"])
        self.assertEqual(s["gpu_temp_c"], 65.0)
        self.assertEqual(s["gpu_util_pct"], 40.0)
        self.assertEqual(s["vram_used_mb"], 2048.0)
        self.assertEqual(s["vram_total_mb"], 8192.0)
        self.assertEqual(s["gpu_power_w"], 120.5)

    def test_cpu_pct_from_deltas(self):
        self.col.sample()  # primes the previous reading
        # 100 more busy jiffies, 100 more idle: 50%.
        self._write("stat", "cpu  150 0 150 900 0 0 0 0 0 0\n")
        self.assertEqual(self.col.sample()["cpu_pct"], 50.0)

    def test_na_gpu_value_is_none(self):
        self._fake_smi("65, 40, 2048, 8192, [N/A]")
        s = self.col.sample()
        self.assertEqual(s["gpu_temp_c"], 65.0)
        self.assertIsNone(s["gpu_power_w"])

    def test_missing_nvidia_smi_gives_none(self):
        s = self.col.sample()
        for n in metrics.GPU_NAMES:
            self.assertIsNone(s[n])

    def test_missing_proc_files_give_none(self):
        os.environ["FORGE_PROC"] = os.path.join(self.dir, "nope")
        col = Collector(os.path.join(self.dir, "other.db"))
        s = col.sample()
        for n in ("cpu_pct", "load1", "mem_used_gb", "mem_total_gb", "swap_used_gb"):
            self.assertIsNone(s[n])

    def test_record_and_current(self):
        rec = self.col.record()
        self.assertTrue(os.path.exists(self.db))
        self.assertEqual(self.col.current(), rec)

    def test_current_without_rows_gives_fresh_sample(self):
        s = self.col.current()
        self.assertEqual(s["load1"], 1.25)


class StorageTests(Base):
    def _insert(self, ts, cpu):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO samples (ts, cpu_pct) VALUES (?, ?)", (ts, cpu))
        con.commit()
        con.close()

    def test_history_bucketing(self):
        now = int(time.time())
        base = (now - 600) // 60 * 60
        self._insert(base, 10)
        self._insert(base + 15, 20)
        self._insert(base + 30, 30)
        self._insert(base + 60, 50)
        h = self.col.history("cpu_pct", since_s=3600, step_s=60)
        self.assertEqual(h, [
            {"ts": base, "avg": 20.0, "max": 30.0},
            {"ts": base + 60, "avg": 50.0, "max": 50.0},
        ])

    def test_history_respects_since(self):
        now = int(time.time())
        self._insert(now - 7200, 99)
        self._insert(now - 10, 1)
        h = self.col.history("cpu_pct", since_s=3600, step_s=60)
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["avg"], 1.0)

    def test_unknown_metric_raises(self):
        with self.assertRaises(ValueError):
            self.col.history("bogus")
        with self.assertRaises(ValueError):
            self.col.history("cpu_pct; DROP TABLE samples")

    def test_prune_rolls_up_old_rows(self):
        now = int(time.time())
        old = (now - 50 * 3600) // 300 * 300
        self._insert(old, 10)
        self._insert(old + 60, 30)
        self._insert(old + 300, 70)
        self._insert(now - 100 * 86400, 5)  # older than 90 days
        self._insert(now - 30, 42)          # recent, stays raw
        self.col.prune()

        con = sqlite3.connect(self.db)
        raw = [r[0] for r in con.execute("SELECT cpu_pct FROM samples")]
        roll = con.execute("SELECT ts, cpu_pct_avg, cpu_pct_max FROM rollup_5m ORDER BY ts").fetchall()
        con.close()
        self.assertEqual(raw, [42.0])
        self.assertEqual(roll, [(old, 20.0, 30.0), (old + 300, 70.0, 70.0)])

        # History reads rollups and raw rows together.
        h = self.col.history("cpu_pct", since_s=60 * 3600, step_s=300)
        self.assertEqual(h[0], {"ts": old, "avg": 20.0, "max": 30.0})
        self.assertEqual(h[1], {"ts": old + 300, "avg": 70.0, "max": 70.0})
        self.assertEqual(h[-1]["avg"], 42.0)


class ThreadTests(Base):
    def test_start_stop(self):
        self.col.start()
        self.col.start()  # second call is a no-op
        time.sleep(0.6)
        self.col.stop()
        self.assertIsNone(self.col._thread)
        con = sqlite3.connect(self.db)
        n = con.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        con.close()
        self.assertGreaterEqual(n, 1)

    def test_concurrent_history_while_recording(self):
        self.col.start()
        errors = []

        def reader():
            try:
                for _ in range(50):
                    self.col.history("cpu_pct", since_s=600, step_s=1)
                    self.col.current()
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.col.stop()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
