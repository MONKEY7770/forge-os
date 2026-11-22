"""Activity timeline: one list of what happened on this PC, newest first.

Sources (each one may be missing or broken without breaking the others):
  activity  /var/lib/forge-activity/<uid>.jsonl   mode changes, health problems and backup
            results, published by each admin's own forge-activity-publish (Forge Core can't
            read home folders). Only used when the file's owner is that uid.
  settings  /var/lib/forge-timeline/settings.jsonl  written by forge-apply: who changed which
            settings and when. Never the values.
  hardware  /var/lib/forge-hw/hardware-notice.json  from forge-hwcheck
  agents    Forge Core's own run history
  updates   the update status (see updates.py)

Every file is read without following links, only the end of it, and only if it is a
regular file with the expected owner. Titles and details have secrets hidden with the
policy's helpers and are cut, so the read-only viewer token may read the timeline.
"""
import json
import math
import os
import pwd
import re
import stat
import time

from fastapi import APIRouter, Depends, Query

from forgecore import policy as P
from forgecore import updates as U

ACTIVITY_DIR = os.environ.get("FORGE_ACTIVITY_DIR", "/var/lib/forge-activity")
SETTINGS_TIMELINE = os.environ.get("FORGE_SETTINGS_TIMELINE", "/var/lib/forge-timeline/settings.jsonl")
HW_NOTICE = os.path.join(os.environ.get("FORGE_HW_STATE", "/var/lib/forge-hw"), "hardware-notice.json")
TRUSTED_UID = 0          # owner of the settings timeline and the hardware notice (root)

FILE_TAIL = 256 * 1024   # only the end of each file is read
MAX_USERS = 64           # per-user activity files read at most (the newest ones)
PER_USER = 100           # entries kept from one user's activity file (the newest ones)
PER_SOURCE = 250         # entries kept from one source, so no source can fill the whole list
FUTURE_MARGIN = 300      # seconds: no entry is shown as newer than now plus this
TITLE_MAX = 200
DETAIL_MAX = 1000
TS_MAX = 1e10            # timestamps after the year 2286 are treated as broken
USER_FILE = re.compile(r"[0-9]{1,10}\.jsonl")
KEY_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,100}")
MODE_NAME = re.compile(r"[a-z]{1,20}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


class Untrusted(Exception):
    pass


def _clean(text, n):
    """Printable, secrets hidden (policy's clean_details), then cut to n characters."""
    # Much wider than policy's largest scan, so policy's own edge-safe cut is the one that applies.
    text = _CONTROL.sub(" ", str(text)[:4 * P.EARLY_CUT_MAX])
    return P.clean_details(text, n)


def entry(ts, kind, title, detail=""):
    return {"ts": ts, "kind": kind, "title": _clean(title, TITLE_MAX), "detail": _clean(detail, DETAIL_MAX)}


def _ts(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 < value < TS_MAX else None


def read_tail(name, owner, dir_fd=None, max_bytes=FILE_TAIL):
    """The end of a regular file, as text, or None if it doesn't exist.
    Raises OSError or Untrusted for links, special files and files with another owner."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Untrusted("not a regular file")
        if st.st_uid != owner:
            raise Untrusted("unexpected owner")
        start = max(0, st.st_size - max_bytes)
        os.lseek(fd, start, os.SEEK_SET)
        chunks, size = [], 0
        while size < max_bytes:
            chunk = os.read(fd, max_bytes - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    text = b"".join(chunks).decode("utf-8", "replace")
    if start > 0:
        text = text.partition("\n")[2]   # the first line was cut
    return text


def _json_lines(text):
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(item, dict):
            yield item


def _user_name(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return f"user {uid}"


# Sources. Each returns a list of entries, None when the source doesn't exist, or raises.

def _user_files(dfd):
    """[(uid, name)] of real users' own files, newest first, and whether any were left out.
    Only "<uid>.jsonl" (no leading zeros) owned by that uid, which must be a real account:
    one user can't add names that push other users' files out."""
    found = []
    for name in os.listdir(dfd):
        if not USER_FILE.fullmatch(name):
            continue
        uid = int(name.split(".")[0])
        if name != f"{uid}.jsonl":
            continue
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            pwd.getpwuid(uid)
        except (OSError, KeyError, OverflowError):
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid == uid:
            found.append((st.st_mtime, uid, name))
    found.sort(reverse=True)
    return [(uid, name) for _, uid, name in found[:MAX_USERS]], len(found) > MAX_USERS


def activity_entries():
    try:
        dfd = os.open(ACTIVITY_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    out = []
    try:
        files, skipped = _user_files(dfd)
        for uid, name in files:
            try:
                text = read_tail(name, uid, dir_fd=dfd)
            except (OSError, Untrusted):
                skipped = True   # changed into a link or another owner's file since the check
                continue
            if not text:
                continue
            user = _user_name(uid)
            mine = []
            for item in _json_lines(text):
                ts = _ts(item.get("ts"))
                kind = item.get("kind")
                if ts is None:
                    continue
                if kind == "mode":
                    mode = item.get("mode")
                    if isinstance(mode, str) and MODE_NAME.fullmatch(mode):
                        mine.append(entry(ts, "mode", f"{user} switched to {mode} mode"))
                    else:
                        mine.append(entry(ts, "mode", f"{user} changed the mode"))
                elif kind == "health" and isinstance(item.get("text"), str):
                    mine.append(entry(ts, "health", "Health check found a problem",
                                      f"{item['text']} (seen by {user})"))
                elif kind == "backup":
                    ok = item.get("ok")
                    title = "Backup finished" if ok is True else "Backup failed" if ok is False else "Backup ran"
                    text_ = item.get("text") if isinstance(item.get("text"), str) else ""
                    mine.append(entry(ts, "backup", title, f"{text_} ({user})" if text_ else user))
            mine.sort(key=lambda e: e["ts"], reverse=True)
            out.extend(mine[:PER_USER])
    finally:
        os.close(dfd)
    if skipped:
        raise _Partial(out, "partial")
    return out


def settings_entries():
    text = read_tail(SETTINGS_TIMELINE, TRUSTED_UID)
    if text is None:
        return None
    out = []
    for item in _json_lines(text):
        ts = _ts(item.get("ts"))
        keys = item.get("keys")
        if ts is None or not isinstance(keys, list):
            continue
        keys = [k for k in keys if isinstance(k, str) and KEY_NAME.fullmatch(k)]
        by = item.get("by") if isinstance(item.get("by"), str) else "someone"
        verb = "undone" if item.get("action") == "undo" else "changed"
        shown = ", ".join(keys[:30]) + (f" and {len(keys) - 30} more" if len(keys) > 30 else "")
        detail = f"Settings: {shown}." if shown else "No settings changed."
        restart = item.get("restart") if isinstance(item.get("restart"), dict) else {}
        for kind, label in (("boot", "Needs a restart of the PC"), ("login", "Needs a new login")):
            need = [k for k in restart.get(kind) or [] if isinstance(k, str) and KEY_NAME.fullmatch(k)]
            if need:
                detail += f" {label} for {', '.join(need[:30])}."
        out.append(entry(ts, "settings", f"Settings {verb} by {by}", detail))
    return out


def hardware_entries():
    text = read_tail(HW_NOTICE, TRUSTED_UID)
    if text is None:
        return None
    notice = json.loads(text)
    ts = _ts(notice.get("ts")) if isinstance(notice, dict) else None
    changes = notice.get("changes") if isinstance(notice, dict) else None
    if ts is None or not isinstance(changes, list):
        raise ValueError("unexpected hardware notice")
    if notice.get("pending") is True:
        title = "Hardware changed, settings are being re-applied"
    elif notice.get("reapplied") is False:
        title = "Hardware check: please look at your setup"
    else:
        title = "Hardware changed, settings re-applied"
    detail = "; ".join(c for c in changes[:20] if isinstance(c, str))
    return [entry(ts, "hardware", title, detail)]


def agent_entries(runner):
    if runner is None:
        return None
    out = []
    for run in runner.list_runs(None, 200):
        ts = _ts(run.get("ended")) or _ts(run.get("started"))
        if ts is None:
            continue
        agent = run.get("agent") if isinstance(run.get("agent"), str) else "?"
        status = run.get("status") if isinstance(run.get("status"), str) else "?"
        text = run.get("error") or run.get("result") or ""
        out.append(entry(ts, "agent", f"Agent {agent}: {status}", text if isinstance(text, str) else ""))
    return out


def update_entries(report):
    out = []
    last = report.get("last_attempt") or {}
    last_ts = _ts(last.get("ts"))
    if last_ts is not None and last.get("result") in ("success", "failed", "running"):
        title = {"success": "Automatic update ran", "failed": "Automatic update failed",
                 "running": "Automatic update running"}[last["result"]]
        out.append(entry(last_ts, "update", title, last.get("detail") or ""))
    staged = report.get("staged") if report.get("update_ready") else None
    if staged:
        ts = (last_ts if last.get("result") == "success" else None) or _ts(staged.get("timestamp"))
        if ts is not None:
            out.append(entry(ts, "update", "Update ready: restart to use it",
                             f"Version {staged.get('version') or '?'} is downloaded."))
    if report.get("booted_is_rollback"):
        ts = _ts(report.get("booted_at")) or _ts(report.get("checked_at"))
        if ts is not None:
            booted = report.get("booted") or {}
            out.append(entry(ts, "update", "Started an older system version (rollback)",
                             f"Running version {booted.get('version') or '?'}. "
                             "See /api/system/updates for how to go back or forward."))
    return out


def make_router(auth_dep, runner=None, updates=None):
    router = APIRouter(tags=["system"], dependencies=[Depends(auth_dep)])
    status = updates or U.STATUS

    def updates_source():
        report = status.get()
        entries = update_entries(report)
        if not report.get("ok"):
            raise _Partial(entries)
        return entries

    sources = {"activity": activity_entries, "settings": settings_entries, "hardware": hardware_entries,
               "agents": lambda: agent_entries(runner), "updates": updates_source}

    @router.get("/api/timeline")
    def timeline(limit: int = Query(100, ge=1, le=500, description="Most entries to return"),
                 since: float = Query(0, ge=0, allow_inf_nan=False,
                                      description="Only entries after this Unix time")):
        """What happened on this PC, newest first: mode changes, settings changes, hardware notices,
        health problems, agent runs, backups and updates. Safe for the viewer token (secrets hidden,
        no setting values). "sources" says which sources were ok, missing or could not be read."""
        entries, state = [], {}
        for name, fn in sources.items():
            try:
                got = fn()
                state[name] = "missing" if got is None else "ok"
            except _Partial as e:
                state[name] = e.state
                got = e.entries
            except Exception:   # one broken source never breaks the timeline, and gives no details
                state[name] = "error"
                continue
            # No entry from the future (a wrong clock, or a file made to stay on top), and no
            # source fills more than its share.
            newest = time.time() + FUTURE_MARGIN
            got = [dict(e, ts=min(e["ts"], newest)) for e in got or [] if e["ts"] > since]
            got.sort(key=lambda e: e["ts"], reverse=True)
            entries.extend(got[:PER_SOURCE])
        entries.sort(key=lambda e: e["ts"], reverse=True)
        return {"entries": entries[:limit], "sources": state}

    return router


class _Partial(Exception):
    """A source that failed but still has some entries to show."""

    def __init__(self, entries, state="error"):
        super().__init__("partial")
        self.entries = entries
        self.state = state
