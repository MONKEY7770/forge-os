"""Lightweight system metrics collector for Forge OS.

Samples CPU, memory, disk and NVIDIA GPU stats into SQLite and serves
current values and history. Standard library only. Sampling never raises:
a missing sensor just gives None.
"""

import os
import shutil
import random
import sqlite3
import subprocess
import threading
import time

METRICS = [  # (name, unit, description)
    ("cpu_pct", "%", "CPU use"),
    ("load1", "", "1 minute load average"),
    ("mem_used_gb", "GB", "RAM in use"),
    ("mem_total_gb", "GB", "Total RAM"),
    ("swap_used_gb", "GB", "Swap in use"),
    ("disk_root_pct", "%", "Root disk used"),
    ("gpu_temp_c", "C", "GPU temperature"),
    ("gpu_util_pct", "%", "GPU use"),
    ("vram_used_mb", "MB", "VRAM in use"),
    ("vram_total_mb", "MB", "Total VRAM"),
    ("gpu_power_w", "W", "GPU power draw"),
    ("cpu_temp_c", "C", "CPU temperature"),
    ("battery_pct", "%", "Battery charge (laptops)"),
    ("on_ac", "", "1 on the charger, 0 on battery (laptops)"),
]

NAMES = [m[0] for m in METRICS]
GPU_NAMES = ["gpu_temp_c", "gpu_util_pct", "vram_used_mb", "vram_total_mb", "gpu_power_w"]

RAW_KEEP_S = 48 * 3600      # raw rows are kept this long
ROLLUP_KEEP_S = 90 * 86400  # 5 minute rollups are kept this long
ROLLUP_STEP_S = 300
PRUNE_EVERY_S = 3600


def _proc(name):
    return os.path.join(os.environ.get("FORGE_PROC", "/proc"), name)


def _sys(*parts):
    return os.path.join(os.environ.get("FORGE_SYSFS", "/sys"), *parts)


def _read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# Sensors that report the CPU package temperature (AMD, Intel, ARM boards).
CPU_SENSORS = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "acpitz")


def _read_cpu_temp():
    """CPU temperature in C from the first CPU sensor, or None."""
    found = {}
    try:
        names = sorted(os.listdir(_sys("class", "hwmon")))
    except OSError:
        return None
    for h in names:
        name = _read_text(_sys("class", "hwmon", h, "name"))
        if name in CPU_SENSORS and name not in found:
            found[name] = _num(_read_text(_sys("class", "hwmon", h, "temp1_input")))
    for name in CPU_SENSORS:
        if found.get(name) is not None:
            return found[name] / 1000.0
    return None


def _read_power():
    """(battery %, on charger 1/0). None for each when there is no battery or no charger."""
    pcts, ac = [], None
    try:
        supplies = sorted(os.listdir(_sys("class", "power_supply")))
    except OSError:
        return None, None
    for p in supplies:
        kind = _read_text(_sys("class", "power_supply", p, "type"))
        if kind == "Battery" and _read_text(_sys("class", "power_supply", p, "scope")) != "Device":
            # (scope "Device" is a wireless mouse or controller, not the PC's battery)
            v = _num(_read_text(_sys("class", "power_supply", p, "capacity")))
            if v is not None:
                pcts.append(v)
        elif kind == "Mains":
            online = _read_text(_sys("class", "power_supply", p, "online"))
            if online in ("0", "1"):
                ac = max(ac or 0, int(online))
    if not pcts:
        return None, None   # a desktop: no battery, so charger state means nothing
    return round(sum(pcts) / len(pcts)), ac


def _num(text):
    """Turn text into a float, or None if it is not a number (e.g. "[N/A]")."""
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _rnd(value, digits=2):
    return None if value is None else round(value, digits)


def _read_cpu_times():
    """Return (total, idle) jiffies from the first line of /proc/stat, or None."""
    try:
        with open(_proc("stat")) as f:
            parts = f.readline().split()
        if not parts or parts[0] != "cpu":
            return None
        vals = [int(x) for x in parts[1:9]]  # user..steal, guest is already in user
        while len(vals) < 8:
            vals.append(0)
        idle = vals[3] + vals[4]  # idle + iowait
        return sum(vals), idle
    except Exception:
        return None


def _read_load1():
    try:
        with open(_proc("loadavg")) as f:
            return _num(f.read().split()[0])
    except Exception:
        return None


def _read_meminfo():
    """Return a dict of meminfo values in kB. Empty dict if unreadable."""
    out = {}
    try:
        with open(_proc("meminfo")) as f:
            for line in f:
                key, _, rest = line.partition(":")
                val = _num(rest.split()[0]) if rest.split() else None
                if val is not None:
                    out[key.strip()] = val
    except Exception:
        pass
    return out


def _read_disk_root_pct():
    try:
        st = os.statvfs("/")
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        if used + avail <= 0:
            return None
        return used / (used + avail) * 100.0
    except Exception:
        return None


def _read_gpu():
    """Query the first NVIDIA GPU. Every value is None if anything fails."""
    result = dict.fromkeys(GPU_NAMES)
    smi = os.environ.get("FORGE_NVIDIA_SMI", "nvidia-smi")
    if not shutil.which(smi):
        return result
    try:
        proc = subprocess.run(
            [smi,
             "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode != 0:
            return result
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            return result
        fields = lines[0].split(",")
        for name, raw in zip(GPU_NAMES, fields):
            result[name] = _num(raw)
    except Exception:
        pass
    return result


class Collector:
    def __init__(self, db_path: str, interval_s: float = 15.0):
        self.db_path = db_path
        self.interval_s = interval_s
        self._lock = threading.RLock()  # guards the DB connection and CPU state
        self._cpu_prev = None
        self._thread = None
        self._stop = threading.Event()

        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        # One shared connection, only ever used while holding self._lock.
        self._db = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        # Switching to WAL and the column upgrade can answer "database is locked" at once (without the
        # busy wait) while a second collector does the same: retry for a while instead of failing.
        deadline = time.monotonic() + 10
        while True:
            try:
                self._setup_tables()
                break
            except sqlite3.OperationalError as e:
                try:
                    self._db.rollback()
                except sqlite3.Error:
                    pass
                busy = "locked" in str(e) or "busy" in str(e)
                if not busy or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05 + random.random() * 0.1)

    def _setup_tables(self):
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            cols = ", ".join(f"{n} REAL" for n in NAMES)
            self._db.execute(f"CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, {cols})")
            rcols = ", ".join(f"{n}_avg REAL, {n}_max REAL" for n in NAMES)
            self._db.execute(f"CREATE TABLE IF NOT EXISTS rollup_5m (ts INTEGER PRIMARY KEY, {rcols})")
            self._db.commit()
            # Databases from an older Forge OS lack newer metrics: add their columns. Checked and
            # changed in one write transaction, so a second process waits instead of adding them twice.
            self._db.execute("BEGIN IMMEDIATE")
            for table, wanted in (("samples", NAMES),
                                  ("rollup_5m", [f"{n}_{k}" for n in NAMES for k in ("avg", "max")])):
                have = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
                for col in wanted:
                    if col not in have:
                        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
            self._db.commit()

    # Sampling

    def _cpu_pct(self):
        with self._lock:
            prev = self._cpu_prev
            if prev is None:
                # First reading: take two snapshots a short time apart.
                prev = _read_cpu_times()
                time.sleep(0.2)
            now = _read_cpu_times()
            self._cpu_prev = now
        if prev is None or now is None:
            return None
        d_total = now[0] - prev[0]
        d_idle = now[1] - prev[1]
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, (d_total - d_idle) / d_total * 100.0))

    def sample(self) -> dict:
        """Take one reading now. Does not store it."""
        s = {"ts": int(time.time())}
        try:
            s["cpu_pct"] = _rnd(self._cpu_pct())
        except Exception:
            s["cpu_pct"] = None
        s["load1"] = _rnd(_read_load1())

        mem = _read_meminfo()
        gb = 1024 * 1024  # kB per GB
        total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
        s["mem_total_gb"] = _rnd(total / gb) if total is not None else None
        s["mem_used_gb"] = _rnd((total - avail) / gb) if total is not None and avail is not None else None
        sw_total, sw_free = mem.get("SwapTotal"), mem.get("SwapFree")
        s["swap_used_gb"] = _rnd((sw_total - sw_free) / gb) if sw_total is not None and sw_free is not None else None

        s["disk_root_pct"] = _rnd(_read_disk_root_pct())
        s.update(_read_gpu())
        try:
            temp = _read_cpu_temp()
        except Exception:
            temp = None
        s["cpu_temp_c"] = _rnd(temp, 1)
        try:
            s["battery_pct"], s["on_ac"] = _read_power()
        except Exception:
            s["battery_pct"], s["on_ac"] = None, None
        # Keep a fixed key order: ts first, then METRICS order.
        return {"ts": s["ts"], **{n: s.get(n) for n in NAMES}}

    def record(self) -> dict:
        """Take a sample and store it."""
        s = self.sample()
        cols = ", ".join(["ts"] + NAMES)
        marks = ", ".join("?" * (len(NAMES) + 1))
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO samples ({cols}) VALUES ({marks})",
                             [s["ts"]] + [s[n] for n in NAMES])
            self._db.commit()
        return s

    def current(self) -> dict:
        """Latest stored sample, or a fresh one if nothing is stored yet."""
        cols = ", ".join(["ts"] + NAMES)
        with self._lock:
            row = self._db.execute(f"SELECT {cols} FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        if row is None:
            return self.sample()
        return dict(zip(["ts"] + NAMES, row))

    # History and housekeeping

    def history(self, metric: str, since_s: int = 3600, step_s: int = 60) -> list[dict]:
        """Average one metric into step_s buckets over the last since_s seconds."""
        if metric not in NAMES:
            raise ValueError(f"unknown metric: {metric}")
        step = max(1, int(step_s))
        start = int(time.time()) - int(since_s)
        # Raw rows and rollup rows are unioned. They never overlap because
        # prune() deletes raw rows once they are rolled up.
        sql = f"""
            SELECT (ts / {step}) * {step} AS b, AVG(v), MAX(m) FROM (
                SELECT ts, {metric} AS v, {metric} AS m FROM samples
                    WHERE ts >= ? AND {metric} IS NOT NULL
                UNION ALL
                SELECT ts, {metric}_avg AS v, {metric}_max AS m FROM rollup_5m
                    WHERE ts >= ? AND {metric}_avg IS NOT NULL
            ) GROUP BY b ORDER BY b
        """
        with self._lock:
            rows = self._db.execute(sql, (start, start)).fetchall()
        return [{"ts": int(b), "avg": round(a, 2), "max": round(m, 2)} for b, a, m in rows]

    def prune(self) -> None:
        """Roll raw rows older than 48h into 5 minute buckets and drop old data."""
        now = int(time.time())
        # Align the cutoff to a bucket edge so a bucket is never split.
        cutoff = ((now - RAW_KEEP_S) // ROLLUP_STEP_S) * ROLLUP_STEP_S
        aggs = ", ".join(f"AVG({n}), MAX({n})" for n in NAMES)
        rcols = ", ".join(f"{n}_avg, {n}_max" for n in NAMES)
        with self._lock:
            try:
                self._db.execute(
                    f"INSERT OR REPLACE INTO rollup_5m (ts, {rcols}) "
                    f"SELECT (ts / {ROLLUP_STEP_S}) * {ROLLUP_STEP_S} AS b, {aggs} "
                    f"FROM samples WHERE ts < ? GROUP BY b", (cutoff,))
                self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
                self._db.execute("DELETE FROM rollup_5m WHERE ts < ?", (now - ROLLUP_KEEP_S,))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    # Background thread

    def _run(self):
        last_prune = 0.0
        while not self._stop.is_set():
            try:
                self.record()
            except Exception:
                pass  # never let the thread die
            if time.monotonic() - last_prune >= PRUNE_EVERY_S:
                try:
                    self.prune()
                except Exception:
                    pass
                last_prune = time.monotonic()
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        """Start recording in a daemon thread. Safe to call twice."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="forge-metrics", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the background thread and wait for it to finish."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=max(5.0, self.interval_s + 5.0))
        self._thread = None
