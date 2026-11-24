"""AI model storage limit for Forge OS: how much of the disk AI models may use.

Used by Forge Core (before and during a model download), the doctor report, forge-health
and the root watcher forge-model-guard. Standard library only, and it never changes anything.

The rule (all sizes in bytes; settings are in GB, 1 GB = 1000^3 bytes, as disks are sold):

  disk          the filesystem that holds ai.models_dir (statvfs of the folder, or of its
                nearest existing parent when the folder does not exist yet).
  total         f_blocks * f_frsize, the size of that filesystem.
  free          f_bavail * f_frsize, the free space a normal program may use.
  reserve       the part of the disk kept for normal use (games, apps, files):
                  auto     30% of total, rounded to the nearest 10 GB, then at least 128 GB and at
                           most 600 GB (a 1 TB disk keeps 300 GB)
                  percent  ai.storage_reserve_pct % of total
                  gb       ai.storage_reserve_gb
                and never more than total.
  budget        the most AI models may use: ai.storage_budget_gb, or total - reserve when that is 0.
  models        the sum of the file sizes (st_size) of the regular files in <models_dir>/blobs.
                Links are never followed and folders are not counted. Unfinished downloads
                (files named *-partial*) count at their full size: Ollama sizes them first.
  pending       the part of those unfinished downloads not written yet: st_size minus the
                space the file already takes (st_blocks * 512). It will still be used.
  free_eff      free - pending: what will be free when the running downloads finish.
  other_used    total - free - (models - pending): everything on the disk that is not an AI model.
  need_free     max(reserve - other_used, min_free): normal use may still grow into the part of
                the reserve it does not use yet, and min_free (ai.storage_min_free_gb) always
                stays free for system updates.
  budget_left   budget - models
  free_left     free_eff - need_free
  room          min(budget_left, free_left): what AI models may still use. Below 0 means over.

With the default budget (total - reserve) and other_used below the reserve, both limits are the
same number. They differ when a custom budget is set, when other files use more than the reserve
(then only min_free is kept), or when the disk is nearly full.

Status: "off" when ai.storage_limit is off, else "over_budget" (budget_left < 0), "reserve"
(free_left < 0: the disk is too full for more models), "low" (room under LOW_ROOM), or "ok".
"""
from __future__ import annotations

import json
import math
import os
import stat
import sys
import time

GB = 1000 ** 3
AUTO_PCT = 30
AUTO_MIN_GB = 128
AUTO_MAX_GB = 600
AUTO_ROUND_GB = 10
LOW_ROOM = 20 * GB          # "little room left": less than one big model
STATE_PATH = os.environ.get("FORGE_AI_STORAGE_STATE", "/run/forge/ai-storage.json")
STATE_FRESH_S = 600         # the watcher writes every 30 s; older state is not trusted
SCHEMA_PATH = "/usr/share/forge/settings.json"
CONF_PATH = "/etc/forge/settings.json"
CONF_MAX = 1024 * 1024


def gb_text(n: float) -> str:
    """Bytes as GB with one decimal, never below 0 (for "x GB left")."""
    return f"{max(0.0, math.floor(n / GB * 10) / 10):.1f}"


def gb_int(n: float) -> int:
    return int(round(n / GB))


# The rule

def auto_reserve_bytes(total: int) -> int:
    gb = math.floor(total * AUTO_PCT / 100 / GB / AUTO_ROUND_GB + 0.5) * AUTO_ROUND_GB
    gb = min(max(gb, AUTO_MIN_GB), AUTO_MAX_GB)
    return min(gb * GB, total)


def reserve_bytes(total: int, s: dict) -> int:
    mode = s["ai.storage_reserve_mode"]
    if mode == "percent":
        r = total * s["ai.storage_reserve_pct"] // 100
    elif mode == "gb":
        r = s["ai.storage_reserve_gb"] * GB
    else:
        return auto_reserve_bytes(total)
    return max(0, min(int(r), total))


def compute(total: int, free: int, models: int, pending: int, s: dict) -> dict:
    """The rule above, on plain numbers. s holds the (checked) Forge settings."""
    reserve = reserve_bytes(total, s)
    budget = s["ai.storage_budget_gb"] * GB if s["ai.storage_budget_gb"] > 0 else max(0, total - reserve)
    min_free = s["ai.storage_min_free_gb"] * GB
    pending = max(0, min(pending, models))
    free_eff = free - pending
    other_used = max(0, total - free - (models - pending))
    need_free = max(reserve - other_used, min_free)
    budget_left = budget - models
    free_left = free_eff - need_free
    room = min(budget_left, free_left)
    problems = []
    if s["ai.storage_limit"] != "off":
        if budget_left < 0:
            problems.append("over_budget")
        elif free_left < 0:
            problems.append("reserve")
        elif room < LOW_ROOM:
            problems.append("low")
    status = "off" if s["ai.storage_limit"] == "off" else (problems[0] if problems else "ok")
    return {"total": total, "free": free, "models": models, "pending": pending, "free_eff": free_eff,
            "reserve": reserve, "budget": budget, "min_free": min_free, "other_used": other_used,
            "need_free": need_free, "budget_left": budget_left, "free_left": free_left, "room": room,
            "status": status, "problems": problems, "limit": s["ai.storage_limit"]}


def problem_lines(r: dict) -> list[str]:
    """One line per problem, for forge-health and the doctor (numbers rounded to whole GB). With no
    models on the PC, "too full" and "little room" are not problems (a small disk, such as a VM's, is
    normal); a download is still refused with its reason."""
    out = []
    for p in r.get("problems") or []:
        if p in ("reserve", "low") and r.get("models", 0) <= 0:
            continue
        if p == "over_budget":
            out.append(f"AI models use {gb_int(r['models'])} GB, more than their limit of {gb_int(r['budget'])} GB")
        elif p == "reserve":
            out.append(f"The AI model disk is too full for more models: {gb_int(r['free'])} GB free, "
                       f"{gb_int(r['need_free'])} GB is kept for normal use and updates")
        elif p == "low":
            out.append(f"Little room left for AI models: {gb_int(max(0, r['room']))} GB")
    return out


# Reading the disk

def disk_of(path: str, statvfs=None) -> dict | None:
    """statvfs of the folder, or of its nearest existing parent. None if not sure (no access)."""
    statvfs = statvfs or os.statvfs
    p = os.path.normpath(path) if path else "/"
    while True:
        try:
            st = statvfs(p)
        except (FileNotFoundError, NotADirectoryError):
            if p in ("/", ""):
                return None
            p = os.path.dirname(p) or "/"
            continue
        except OSError:
            return None
        return {"path": p, "total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}


# An unfinished download ("-partial") changed this recently is still going.
ACTIVE_PARTIAL_S = 120


def models_usage(models_dir: str, now: float | None = None) -> dict | None:
    """Sizes of the blob files (see the rule). A missing folder is 0; None if it can't be read
    or "blobs" is a link (root never follows it).
    Ollama sizes a download's file first and then fills it, so an unfinished download counts what
    is written so far; the rest is kept free ("pending") only while it is still going. A stopped or
    refused download then holds no room it will never use."""
    out = {"models": 0, "pending": 0, "partials": 0, "newest_mtime": 0.0}
    now = time.time() if now is None else now
    blobs = os.path.join(models_dir, "blobs")
    try:
        dfd = os.open(blobs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return out
    except NotADirectoryError:
        # A link is refused with this error too (O_NOFOLLOW): not sure, rather than "no models".
        return None if os.path.islink(blobs) else out
    except OSError:
        return None
    try:
        it = os.scandir(dfd)
    except OSError:
        os.close(dfd)
        return None
    try:
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                out["newest_mtime"] = max(out["newest_mtime"], st.st_mtime)
                if "-partial" in e.name:
                    written = min(st.st_size, st.st_blocks * 512)
                    out["models"] += written
                    if 0 <= now - st.st_mtime < ACTIVE_PARTIAL_S:
                        out["partials"] += 1
                        out["pending"] += st.st_size - written
                else:
                    out["models"] += st.st_size
    except OSError:
        return None
    finally:
        os.close(dfd)
    return out


def report(s: dict, models_dir: str, statvfs=None) -> dict | None:
    """The rule for this PC now, or None if not sure."""
    d = disk_of(models_dir, statvfs)
    u = models_usage(models_dir) if d else None
    if not d or u is None:
        return None
    r = compute(d["total"], d["free"], u["models"], u["pending"], s)
    r.update(disk_path=d["path"], partials=u["partials"], newest_mtime=u["newest_mtime"])
    return r


# Settings and the watcher's state

def load_settings(schema_path: str | None = None, conf_path: str | None = None) -> dict | None:
    """Checked settings, or None when the settings file can't be read or has problems.
    Unlike forgecfg.values(), a broken file never means "defaults" here: nothing acts on a guess."""
    import forgecfg
    try:
        with open(schema_path or forgecfg.SCHEMA_PATH) as f:
            schema = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        with open(conf_path or forgecfg.OVERRIDES_PATH, "rb") as f:
            data = f.read(CONF_MAX + 1)
        if len(data) > CONF_MAX:
            return None
        overrides = json.loads(data)
    except FileNotFoundError:
        overrides = {}
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(overrides, dict):
        return None
    try:
        clean, errors = forgecfg.validate(forgecfg.values(schema, overrides), schema)
    except (KeyError, TypeError, ValueError):
        return None
    return None if errors else clean


def state_doc(r: dict, models_dir: str, ts: float) -> dict:
    """What the watcher shares in /run/forge/ai-storage.json (readable by everyone, no secrets)."""
    return {"ts": int(ts), "status": r["status"], "limit": r["limit"], "models_dir": models_dir,
            "disk_total_gb": gb_int(r["total"]), "disk_free_gb": gb_int(r["free"]),
            "reserve_gb": gb_int(r["reserve"]), "budget_gb": gb_int(r["budget"]),
            "models_gb": gb_int(r["models"]), "models_bytes": int(r["models"]), "room_gb": gb_int(r["room"]),
            "min_free_gb": gb_int(r["min_free"]), "need_free_gb": gb_int(r["need_free"]),
            "room_bytes": int(r["room"]),
            "problems": list(r["problems"]), "messages": problem_lines(r)}


def read_state(path: str | None = None, now: float | None = None) -> dict | None:
    """The watcher's last state if it is fresh, else None."""
    try:
        with open(path or STATE_PATH, "rb") as f:
            doc = json.loads(f.read(64 * 1024))
    except (OSError, ValueError, RecursionError):
        return None
    now = time.time() if now is None else now
    if not isinstance(doc, dict) or not isinstance(doc.get("ts"), int) or not (-60 <= now - doc["ts"] <= STATE_FRESH_S):
        return None
    return doc


def current(root: str = "") -> dict | None:
    """The rule for this PC, worked out here, or from the watcher's fresh state when this
    user can't read the model folder. None if not sure. root is for tests (the doctor's fake root)."""
    if root:
        s = load_settings(root + SCHEMA_PATH, root + CONF_PATH)
    else:
        s = load_settings()
    if s is not None:
        r = report(s, root + s["ai.models_dir"])
        if r is not None:
            r["models_dir"] = s["ai.models_dir"]
            return r
    st = read_state(root + STATE_PATH if root else None)
    if not st or st.get("status") == "unknown":
        return None
    try:
        n = {k: st[k] * GB for k in ("disk_total_gb", "disk_free_gb", "reserve_gb", "budget_gb", "models_gb",
                                     "min_free_gb")}
        return {"total": n["disk_total_gb"], "free": n["disk_free_gb"], "reserve": n["reserve_gb"],
                "budget": n["budget_gb"], "models": int(st.get("models_bytes", n["models_gb"])),
                "min_free": n["min_free_gb"], "room": int(st["room_bytes"]), "status": str(st["status"]), "limit": str(st["limit"]),
                "problems": [p for p in st.get("problems", []) if p in ("over_budget", "reserve", "low")],
                "need_free": int(st.get("need_free_gb", 0)) * GB, "models_dir": str(st.get("models_dir", "")), "from_state": True}
    except (KeyError, TypeError, ValueError):
        return None


# Before and during a download

def room_for_pull() -> tuple[int | None, str]:
    """(room in bytes or None if not sure, limit). Forge Core asks this before each download.
    From forge-model-guard's state (root works it out every 30 s and can read every model folder,
    Forge Core's own user maybe not). No fresh state means not sure: nothing is refused."""
    st = read_state()
    if not st or st.get("status") == "unknown" or st.get("limit") not in ("block", "warn", "off"):
        return None, "off"
    room = st.get("room_bytes")
    if not isinstance(room, int) or isinstance(room, bool):
        return None, "off"
    return room, st["limit"]


NOT_DOWNLOADED = ("Not downloaded: {model} needs about {need} GB, but only {room} GB is left for AI models. "
                  "Delete a model you no longer use, or raise the limit in Forge Settings.")
NOT_DOWNLOADED_UNKNOWN = ("Not downloaded: {model} needs more room, but only {room} GB is left for AI models. "
                          "Delete a model you no longer use, or raise the limit in Forge Settings.")


class PullGuard:
    """Checks one download. Ollama tells each layer's size (total) and what is already there
    (completed) in its first progress line for that layer, before writing it, so the size is
    known as the download starts. Returns ("error", text) to stop, ("warning", text) to tell,
    or None. A room of None (not sure) never stops anything."""

    def __init__(self, model: str, room: int | None, limit: str):
        self.model, self.room, self.limit = model, room, limit
        self.need = 0
        self.seen = set()
        self.warned = False

    def _verdict(self, text: str):
        if self.limit == "block":
            return "error", text
        if self.limit == "warn" and not self.warned:
            self.warned = True
            return "warning", text.replace("Not downloaded", "Warning", 1)
        return None

    def before(self):
        """Before the download, when its size is not known yet: only stop when there is no room at all."""
        if self.limit == "off" or self.room is None or self.room > 0:
            return None
        return self._verdict(NOT_DOWNLOADED_UNKNOWN.format(model=self.model, room=gb_text(self.room)))

    def observe(self, step: dict):
        if self.limit == "off" or self.room is None or not isinstance(step, dict):
            return None
        total, done = step.get("total"), step.get("completed") or 0
        digest = step.get("digest") or str(step.get("status", ""))
        if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0 or digest in self.seen:
            return None
        if not isinstance(done, (int, float)) or isinstance(done, bool):
            done = 0
        self.seen.add(digest)
        self.need += max(0, int(total) - int(done))
        if self.need <= self.room:
            return None
        return self._verdict(NOT_DOWNLOADED.format(model=self.model, need=gb_text(self.need),
                                                   room=gb_text(self.room)))


def main(argv):
    """storage.py --problems: one line per AI storage problem (for forge-health), from
    forge-model-guard's fresh state. Prints nothing when all is well, the limit is off, or it is
    not sure (no fresh state)."""
    if argv != ["--problems"]:
        print("usage: storage.py --problems", file=sys.stderr)
        return 2
    st = read_state()
    lines = []
    if st is not None and st.get("status") not in ("unknown", "off") and isinstance(st.get("messages"), list):
        lines = [x for x in st["messages"][:5] if isinstance(x, str)]
    for line in lines:
        print("".join(c for c in line if c.isprintable())[:300])
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.environ.get("FORGE_LIB", "/usr/lib/forge"))
    sys.exit(main(sys.argv[1:]))
