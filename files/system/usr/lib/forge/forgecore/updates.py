#!/usr/bin/env python3
"""System updates and recovery status, read without root.

Sources:
  rpm-ostree status --json   booted, staged and rollback deployments
  systemctl show             the last automatic update run. Bazzite runs uupd
                             (uupd.timer) and turns rpm-ostreed-automatic.timer off;
                             both are checked, uupd first.

The system journal is not used: Forge Core's "forge" user can't read it.
Only one refresh runs at a time, and a result is reused for 60 seconds.

Run as a script, `updates.py --summary` prints the few keys forge-health
needs to notify once per update, failure or rollback.
"""
import copy
import json
import os
import re
import subprocess
import threading
import time

CACHE_S = 60
MAX_OUTPUT = 2 * 1024 * 1024        # rpm-ostree output larger than this is refused
RUN_TIMEOUT_S = 30
FIRST_WAIT_S = 5    # only the very first caller waits for rpm-ostree, and at most this long
UPDATERS = ("uupd.service", "rpm-ostreed-automatic.service")
SHOW_PROPS = "Id,LoadState,ActiveState,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp"

RECOVERY = (
    "If an update causes problems:\n"
    "1. Restart the PC and pick the previous entry in the boot menu. Your files and settings stay as they are.\n"
    "2. To keep using that older version, run: rpm-ostree rollback, then restart.\n"
    "3. To cancel an update that is ready but not started yet, run: rpm-ostree cleanup -p\n"
    "Windows in the boot menu is not touched by any of this.")

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_HEX = re.compile(r"[0-9a-f]{1,128}")


def _text(value, n=200):
    """A short, single-line, printable string, or None."""
    if not isinstance(value, str):
        return None
    return _CONTROL.sub("", value)[:n]


def _run(argv, timeout):
    """Run a read-only system tool. Returns (exit code, stdout). No shell."""
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                       env=dict(os.environ, LC_ALL="C"), stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout


def _image(ref):
    """ "ostree-image-signed:docker://ghcr.io/x/forge-os:latest" -> "ghcr.io/x/forge-os:latest" """
    if not ref:
        return None
    return ref.split("://", 1)[1] if "://" in ref else ref.split(":", 1)[-1]


def _deployment(d):
    ref = _text(d.get("container-image-reference"), 300)
    ts = d.get("timestamp")
    checksum = d.get("checksum")
    return {
        "image": _text(_image(ref), 300),
        "reference": ref,
        "version": _text(d.get("version")),
        "timestamp": ts if isinstance(ts, int) and not isinstance(ts, bool) and 0 < ts < 2 ** 40 else None,
        "digest": _text(d.get("container-image-reference-digest"), 80),
        "checksum": checksum if isinstance(checksum, str) and _HEX.fullmatch(checksum) else None,
        "pinned": d.get("pinned") is True,
    }


def parse_status(data):
    """The parts of `rpm-ostree status --json` the dashboard needs. Raises ValueError."""
    if not isinstance(data, dict) or not isinstance(data.get("deployments"), list):
        raise ValueError("unexpected rpm-ostree output")
    deps = data["deployments"]
    if not deps or not all(isinstance(d, dict) for d in deps):
        raise ValueError("unexpected rpm-ostree output")
    for d in deps:
        if not isinstance(d.get("booted", False), bool) or not isinstance(d.get("staged", False), bool):
            raise ValueError("unexpected rpm-ostree output")
    staged = next((d for d in deps if d.get("staged")), None)
    # The boot menu order without the staged one: the first is the default, the next the rollback.
    order = [d for d in deps if not d.get("staged")]
    booted = next((d for d in order if d.get("booted")), None)
    if booted is None:
        raise ValueError("rpm-ostree shows no booted system")
    bi = order.index(booted)
    if bi == 0:
        rollback = order[1] if len(order) > 1 else None
    else:
        rollback = order[0]   # booted from an older entry: the default is the way back
    # Booted into an older version: either an older boot menu entry was picked, or
    # "rpm-ostree rollback" made it the default while a newer one of the same image is kept.
    is_rollback = bi > 0
    if not is_rollback and rollback is not None:
        same_image = rollback.get("container-image-reference") == booted.get("container-image-reference")
        bts, rts = booted.get("timestamp"), rollback.get("timestamp")
        if same_image and isinstance(bts, int) and isinstance(rts, int) and rts > bts:
            is_rollback = True
    return {
        "booted": _deployment(booted),
        "staged": _deployment(staged) if staged else None,
        "rollback": _deployment(rollback) if rollback else None,
        "update_ready": staged is not None,
        "rollback_available": rollback is not None,
        "booted_is_rollback": is_rollback,
        # The default boot entry is running: "rpm-ostree rollback" then switches to the other entry.
        "booted_is_default": bi == 0,
    }


def _unix(value):
    """systemctl --timestamp=unix prints "@1700000000"; unset is empty."""
    m = re.fullmatch(r"@([0-9]{1,12})", value or "")
    return int(m.group(1)) if m else None


def parse_show(text):
    """The last automatic update run, from `systemctl show` of the updater units."""
    blocks = []
    for chunk in (text or "").split("\n\n"):
        props = {}
        for line in chunk.splitlines():
            k, sep, v = line.partition("=")
            if sep:
                props[k] = v
        if props:
            blocks.append(props)
    for props in blocks:
        if props.get("LoadState") != "loaded" or props.get("Id") not in UPDATERS:
            continue
        name = props["Id"].rsplit(".", 1)[0]
        ended = _unix(props.get("ExecMainExitTimestamp"))
        active = props.get("ActiveState", "")
        if active in ("activating", "active", "reloading"):
            result, detail = "running", "An update is running now."
            ts = _unix(props.get("ExecMainStartTimestamp"))
        elif ended is None:
            result, detail, ts = "none", "No automatic update has run since this boot.", None
        elif props.get("Result") == "success" and props.get("ExecMainStatus", "0") == "0":
            result, detail, ts = "success", "The last automatic update finished without errors.", ended
        else:
            code = _text(props.get("ExecMainStatus"), 10) or "?"
            why = _text(props.get("Result"), 40) or "?"
            result, detail, ts = "failed", f"The last automatic update failed ({why}, exit code {code}).", ended
        return {"updater": name, "result": result, "ts": ts, "detail": detail}
    return {"updater": None, "result": "unknown", "ts": None,
            "detail": "No automatic updater found (looked for uupd and rpm-ostreed-automatic)."}


def _boot_time():
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _message(r):
    if not r["ok"]:
        return "Update status is not available right now."
    if r["booted_is_rollback"]:
        return "You are running an older system version (rollback)."
    if r["update_ready"]:
        return "Update ready, restart to use it."
    if r["last_attempt"]["result"] == "failed":
        return "The last automatic update failed."
    return "Up to date as far as this PC knows."


class UpdateStatus:
    """Builds the report, at most one at a time, and reuses it for cache_s seconds."""

    def __init__(self, run=None, cache_s=CACHE_S, clock=time.monotonic, first_wait=FIRST_WAIT_S):
        self._run = run or _run
        self._cache_s = cache_s
        self._clock = clock
        self._first_wait = first_wait
        self._lock = threading.Lock()
        self._at = None
        self._report = None
        self._refreshing = None   # an Event while the one background refresh runs

    def _build(self):
        r = {"ok": False, "error": None, "checked_at": time.time(), "booted": None, "staged": None,
             "rollback": None, "update_ready": False, "rollback_available": False, "booted_is_rollback": False,
             "booted_is_default": False}
        try:
            code, out = self._run(["rpm-ostree", "status", "--json"], RUN_TIMEOUT_S)
            if code != 0:
                raise ValueError("rpm-ostree status failed")
            if len(out) > MAX_OUTPUT:
                raise ValueError("rpm-ostree output is too large")
            r.update(parse_status(json.loads(out)))
            r["ok"] = True
        except FileNotFoundError:
            r["error"] = "rpm-ostree is not available on this system."
        except subprocess.TimeoutExpired:
            r["error"] = "rpm-ostree took too long to answer."
        except (OSError, ValueError, RecursionError, TypeError, AttributeError):
            r["error"] = "rpm-ostree gave no usable answer."
        try:
            code, out = self._run(["systemctl", "show", "--timestamp=unix", "-p", SHOW_PROPS, *UPDATERS],
                                  RUN_TIMEOUT_S)
            r["last_attempt"] = parse_show(out[:65536] if code == 0 else "")
        except (OSError, subprocess.SubprocessError, ValueError):
            r["last_attempt"] = parse_show("")
        r["booted_at"] = _boot_time()
        r["message"] = _message(r)
        r["recovery"] = RECOVERY
        return r

    def _refresh(self, done):
        report = None
        try:
            report = self._build()
        except Exception:   # _build catches what it expects; never leave the refresh stuck
            report = None
        finally:
            # Even if the refresh dies some other way, the next caller can start a new one.
            with self._lock:
                if report is not None:
                    self._report, self._at = report, self._clock()
                self._refreshing = None
            done.set()

    def _still_checking(self):
        r = {"ok": False, "error": "Still checking for updates. Try again in a moment.",
             "checked_at": time.time(), "booted": None, "staged": None, "rollback": None,
             "update_ready": False, "rollback_available": False, "booted_is_rollback": False, "booted_is_default": False,
             "last_attempt": parse_show(""), "booted_at": _boot_time(), "recovery": RECOVERY}
        r["message"] = _message(r)
        return r

    def get(self, wait=None):
        """The cached report. When it is old, one background refresh starts and callers get the
        old report meanwhile, so a slow rpm-ostree never makes every caller wait. Only the very
        first call waits, for at most first_wait seconds (or wait, if given)."""
        with self._lock:
            now = self._clock()
            if self._at is not None and 0 <= now - self._at < self._cache_s:
                return copy.deepcopy(self._report)
            done = self._refreshing
            if done is None:
                done = self._refreshing = threading.Event()
                threading.Thread(target=self._refresh, args=(done,), daemon=True,
                                 name="forge-updates-refresh").start()
            if self._report is not None:
                return copy.deepcopy(self._report)
        done.wait(self._first_wait if wait is None else wait)
        with self._lock:
            if self._report is not None:
                return copy.deepcopy(self._report)
        return self._still_checking()


# Shared by /api/system/updates and the timeline, so they use one cache.
STATUS = UpdateStatus()


def summary(r):
    """Keys forge-health remembers so it notifies once each. Empty means nothing to say."""
    staged = (r.get("staged") or {}) if r.get("update_ready") else {}
    booted = r.get("booted") or {}
    last = r.get("last_attempt") or {}
    failed = ""
    if last.get("result") == "failed" and last.get("updater"):
        failed = f"{last['updater']}@{last.get('ts') or 0}"
    return {
        "staged": staged.get("checksum") or "",
        "staged_version": staged.get("version") or "",
        "failed": failed,
        "rolled_back": (booted.get("checksum") or "") if r.get("booted_is_rollback") else "",
        "booted_version": booted.get("version") or "",
    }


def make_router(auth_dep, status=None):
    from fastapi import APIRouter, Depends
    router = APIRouter(tags=["system"], dependencies=[Depends(auth_dep)])
    st = status or STATUS

    @router.get("/api/system/updates")
    def system_updates():
        """Booted, staged and rollback system versions, the last automatic update, and how to roll back.
        Read-only, safe for the viewer token. Reused for 60 seconds."""
        return st.get()

    return router


def main(argv):
    if argv[1:] != ["--summary"]:
        print("usage: updates.py --summary")
        return 2
    print(json.dumps(summary(UpdateStatus().get(wait=3 * RUN_TIMEOUT_S))))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
