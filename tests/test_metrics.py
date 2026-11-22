import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "files/system/usr/lib/forge"))

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


def _open_collector(path, q):
    try:
        Collector(path)
        q.put("ok")
    except Exception as e:      # noqa: BLE001 (the test wants to see any failure)
        q.put(repr(e))


class MigrationRaceTest(unittest.TestCase):
    def test_two_collectors_upgrading_one_old_database_both_start(self):
        import multiprocessing as mp
        old = [n for n in NAMES if n not in ("cpu_temp_c", "battery_pct", "on_ac")]
        d = tempfile.mkdtemp()
        failures = []
        for i in range(15):
            p = os.path.join(d, f"m{i}.db")
            db = sqlite3.connect(p)
            db.execute("CREATE TABLE samples (ts INTEGER PRIMARY KEY, " + ", ".join(f"{n} REAL" for n in old) + ")")
            db.execute("CREATE TABLE rollup_5m (ts INTEGER PRIMARY KEY, "
                       + ", ".join(f"{n}_avg REAL, {n}_max REAL" for n in old) + ")")
            db.commit()
            db.close()
            q = mp.Queue()
            ps = [mp.Process(target=_open_collector, args=(p, q)) for _ in range(2)]
            for x in ps:
                x.start()
            for x in ps:
                x.join(30)
            failures += [r for r in (q.get(timeout=5), q.get(timeout=5)) if r != "ok"]
        self.assertEqual(failures, [])


class LaptopMetricTests(Base):
    """P1: CPU temperature, battery charge and charger state."""

    def sysfs(self, rel, text):
        p = os.path.join(self.dir, "sys", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def test_laptop_values(self):
        os.environ["FORGE_SYSFS"] = os.path.join(self.dir, "sys")
        self.sysfs("class/hwmon/hwmon0/name", "nvme\n")
        self.sysfs("class/hwmon/hwmon0/temp1_input", "99000\n")
        self.sysfs("class/hwmon/hwmon1/name", "k10temp\n")
        self.sysfs("class/hwmon/hwmon1/temp1_input", "61500\n")
        self.sysfs("class/power_supply/BAT0/type", "Battery\n")
        self.sysfs("class/power_supply/BAT0/capacity", "77\n")
        self.sysfs("class/power_supply/AC/type", "Mains\n")
        self.sysfs("class/power_supply/AC/online", "1\n")
        s = self.col.sample()
        self.assertEqual(s["cpu_temp_c"], 61.5)        # the CPU sensor, not the SSD
        self.assertEqual(s["battery_pct"], 77)
        self.assertEqual(s["on_ac"], 1)
        self.sysfs("class/power_supply/AC/online", "0\n")
        self.assertEqual(self.col.sample()["on_ac"], 0)

    def test_mouse_battery_is_not_the_pc_battery(self):
        os.environ["FORGE_SYSFS"] = os.path.join(self.dir, "sys-mouse")
        base = "class/power_supply/hidpp_battery_0/"
        for k, v in (("type", "Battery"), ("scope", "Device"), ("capacity", "40")):
            p = os.path.join(self.dir, "sys-mouse", base + k)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(v + "\n")
        s = self.col.sample()
        self.assertIsNone(s["battery_pct"])
        self.assertIsNone(s["on_ac"])

    def test_desktop_has_no_battery_values(self):
        os.environ["FORGE_SYSFS"] = os.path.join(self.dir, "empty-sys")
        s = self.col.sample()
        self.assertIsNone(s["battery_pct"])
        self.assertIsNone(s["on_ac"])
        self.assertIsNone(s["cpu_temp_c"])

    def test_old_database_gets_the_new_columns(self):
        old = os.path.join(self.dir, "old.db")
        con = sqlite3.connect(old)
        con.execute("CREATE TABLE samples (ts INTEGER PRIMARY KEY, cpu_pct REAL, load1 REAL)")
        con.execute("CREATE TABLE rollup_5m (ts INTEGER PRIMARY KEY, cpu_pct_avg REAL, cpu_pct_max REAL)")
        con.execute("INSERT INTO samples (ts, cpu_pct) VALUES (1, 5)")
        con.commit()
        con.close()
        c = Collector(old, interval_s=0.1)
        try:
            c.record()   # failed before: the old table had no column for the new metrics
        finally:
            c.stop()
        con = sqlite3.connect(old)
        cols = {r[1] for r in con.execute("PRAGMA table_info(samples)")}
        rcols = {r[1] for r in con.execute("PRAGMA table_info(rollup_5m)")}
        rows = con.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        con.close()
        self.assertTrue({"cpu_temp_c", "battery_pct", "on_ac"} <= cols)
        self.assertTrue({"battery_pct_avg", "on_ac_max"} <= rcols)
        self.assertEqual(rows, 2)


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
