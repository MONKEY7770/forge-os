"""Forge Dashboard backend, used by `forge-settings --dashboard` (runs as you, never as root).

Data is gathered read-only:
  Forge Core   over HTTP with the read-only viewer token. The token stays in this process.
               Before it is sent, the program listening on Forge Core's port, and the server
               side of our exact connection, must belong to the forge user (as forge-api checks).
  Your tools   forge-backup, forge-shared-drive, forge-settings-sync and forge-hub status --json,
               forge-doctor --json, lsblk, and virsh (only when virtual machines are on).

Logs         journalctl (system and your user journal), read only, bounded in lines, bytes and time.
             Every line goes through forgecore.policy.clean_details (secrets hidden) before it is
             searched or sent to the page. Without that redaction no lines are shown at all.

Actions are a fixed list. Each one is a fixed command. The only parts that vary are ids checked
against a fixed list or the tool's own fresh list (approval ids, doctor fix ids, sync PC names),
and no text from the page is ever put into a command. Every action needs "confirmed": true,
which the page only sends from its confirm dialog. Root steps go through pkexec (your password).
"""
import hashlib
import hmac
import http.client
import ipaddress
import json
import grp
import os
import pwd
import re
import selectors
import socket
import subprocess
import sys
import threading
import time
import unicodedata

import forgecfg
import forgerelease

LOG_SYSTEM_UNITS = ("forge-core.service", "forge-hwcheck.service", "forge-power-switch.service", "forge-virt.service",
                    "forge-apply-firstboot.service", "forge-mode-apply@*.service", "ollama.service")
LOG_USER_UNITS = ("forge-health.service", "forge-backup.service", "forge-backup-restore-test.service",
                  "forge-settings-sync.service", "forge-shared-drive.service", "forge-game.service",
                  "forge-game-overlay.service")
LOG_SERVICES = ("all", "forge") + LOG_SYSTEM_UNITS + LOG_USER_UNITS
LOG_LEVELS = {"error": "err", "warning": "warning", "info": "info"}
LOG_SINCE = {"1h": "-1h", "24h": "-24h", "7d": "-7d"}
LOG_FIELDS = "MESSAGE,PRIORITY,_SYSTEMD_UNIT,_SYSTEMD_USER_UNIT,SYSLOG_IDENTIFIER,_COMM"
LOG_PER_JOURNAL = 1000           # newest entries read from each journal
LOG_BYTES = 4 * 1024 * 1024      # bytes read from each journalctl
LOG_TIMEOUT_S = 15               # each journalctl is stopped after this
LOG_SHOWN = 500                  # lines sent to the page
LOG_MSG_MAX = 2000               # characters per message (after hiding secrets)
LOG_SEARCH_MAX = 100
LOG_PRIORITY = ("emergency", "alert", "critical", "error", "warning", "notice", "info", "debug")
LOG_GROUPS = ("wheel", "adm", "systemd-journal")   # groups that may read the whole system journal

# Test seams: only read when FORGE_DASH_TEST=1, and the Forge Core user only when no forge user exists.
TEST = os.environ.get("FORGE_DASH_TEST") == "1"
VIEWER_TOKEN = (os.environ.get("FORGE_VIEWER_TOKEN") if TEST else None) or "/etc/forge/viewer.token"
TEMPERED_CSS = (os.environ.get("FORGE_TEMPERED_CSS") if TEST else None) or "/usr/share/forge/tempered.css"
PROC_NET = "/proc/net"

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
TEMPERED_MARK = "/*__TEMPERED__*/"      # where the page's <style> takes the shared tokens
TEMPERED_MAX = 64 * 1024
# Used only when tempered.css is missing or unusable: the few colours the page needs to stay readable.
TEMPERED_FALLBACK = """:root { color-scheme: light dark; --bg: #f7f6f3; --panel: #ffffff; --rail: #efede9; --field: #fbfaf8;
  --text: #1c1b19; --muted: #64605a; --line: #d6d2cb; --border: #8a857d; --accent: #d9622b; --accent-fill: #c2541f;
  --accent-text: #a9461a; --on-accent: #ffffff; --accent-soft: #f7e6dc; --glow: #ff9a5c; --ok: #276b45; --ok-soft: #e1f0e7;
  --warn: #8f5a07; --warn-soft: #f6ebd6; --bad: #b3261e; --bad-soft: #f7e0dd; --info: #3f6383; --info-soft: #e3ebf3;
  --radius-s: 4px; --radius-m: 8px; --radius-pill: 999px; --edge: none; --shadow-1: none; --shadow-2: none;
  --font: system-ui, "Noto Sans", "Noto Sans Thai", sans-serif; --font-mono: "Noto Sans Mono", monospace;
  --text-s: 13px; --text-m: 14px; --text-l: 18px; --leading: 1.45; --ease: ease; --dur-1: 120ms; --dur-2: 200ms;
  --dur-3: 320ms; --dur-cool: 0ms; }
@media (prefers-color-scheme: dark) { :root { --bg: #141312; --panel: #1e1d1b; --rail: #181715; --field: #191816;
  --text: #ecebe8; --muted: #a39e96; --line: #34312d; --border: #6e6962; --accent: #ff7a3d; --accent-fill: #ff7a3d;
  --accent-text: #ff7a3d; --on-accent: #1c1b19; --accent-soft: #3a2418; --ok: #5cc98d; --ok-soft: #16301f; --warn: #e8b04a;
  --warn-soft: #362a14; --bad: #ff7b6e; --bad-soft: #3b1d1a; --info: #8fb3d6; --info-soft: #1a2633; } }
:lang(th) { --leading: 1.6; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0ms !important; } }"""

FORGE_API = "/usr/bin/forge-api"
DOCTOR_FIX = "/usr/libexec/forge-doctor-fix"
RPM_OSTREE = "/usr/bin/rpm-ostree"
# Must match FIXES in /usr/libexec/forge-doctor-fix (a test checks that).
FIX_IDS = ("restart-ollama", "restart-forge-core", "usb-unlock-off", "run-hwcheck", "start-forge-virt")
MODES = ("gaming", "agents", "creative", "quiet")
HISTORY = ("cpu_pct", "gpu_util_pct", "vram_used_mb", "mem_used_gb", "gpu_temp_c", "cpu_temp_c")
APPROVAL_ID = re.compile(r"[0-9a-f]{32}")
HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")   # forge-settings-sync's own rule
ROLLBACK_WORD = "rollback"

OUT_MAX = 2 * 1024 * 1024        # bytes read from any tool or from Forge Core
TEXT_MAX = 4000                  # tool output shown on the page
DOCTOR_CACHE_S = 60
DOCTOR_FAIL_S = 10               # a failed doctor run is reused this long, so a broken doctor is not run on every call
DOCTOR_WAIT_S = 20               # how long a caller waits for the first doctor run when there is no result yet
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_HIDDEN = ("Cc", "Cf", "Cs")     # control, format (bidi, zero-width) and lone surrogate characters
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


class CoreDown(Exception):
    pass


def clean(text, n=TEXT_MAX):
    """Printable text, cut to n characters (the page shows it with textContent anyway)."""
    return _CONTROL.sub("", str(text))[:n]


def scrub(value):
    """Text from Forge Core and the tools, made safe to show: every string (and key) loses its control
    and format characters (Unicode Cc, Cf and lone surrogates: bidi overrides, zero-width marks, escape
    codes), except tab and newline. forge-settings runs everything it sends to the page through this."""
    if isinstance(value, str):
        if value.isprintable():
            return value
        return "".join(c for c in value if c in "\t\n" or unicodedata.category(c) not in _HIDDEN)
    if isinstance(value, dict):
        return {scrub(k) if isinstance(k, str) else k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


# ---------- running tools ----------

def run(argv, timeout=60):
    """Run a fixed argv. Returns (exit code, stdout, stderr); code None if it could not run.
    Output is read while the tool runs, at most OUT_MAX bytes each from stdout and stderr. Past that,
    or past the timeout, the tool is stopped, so a runaway tool never fills this process's memory."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return None, "", f"{argv[0]} is not installed."
    except OSError as e:
        return None, "", f"{argv[0]} could not run ({e.strerror})."
    bufs = {p.stdout: bytearray(), p.stderr: bytearray()}
    problem = None
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as sel:
            for f in bufs:
                sel.register(f, selectors.EVENT_READ)
            while sel.get_map() and problem is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    problem = "took too long and was stopped."
                    break
                for key, _ in sel.select(left):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    bufs[key.fileobj] += chunk
                    if len(bufs[key.fileobj]) > OUT_MAX:
                        problem = "gave too much output."
                        break
        if problem is None:
            try:
                p.wait(max(deadline - time.monotonic(), 0.01))
            except subprocess.TimeoutExpired:
                problem = "took too long and was stopped."
    finally:
        if p.poll() is None:
            try:
                p.kill()
                p.wait(5)
            except (OSError, subprocess.TimeoutExpired):
                # Started through pkexec it runs as root, so it can't be stopped from here.
                problem = (problem or "did not finish.").replace(" and was stopped", "") + \
                    " It could not be stopped and may still be running."
        p.stdout.close()
        p.stderr.close()
    if problem:
        return None, "", f"{argv[0]} {problem}"
    return p.returncode, bufs[p.stdout].decode("utf-8", "replace"), bufs[p.stderr].decode("utf-8", "replace")


def tool_json(argv, timeout=60):
    """(data, error) from a tool that prints JSON."""
    code, out, err = run(argv, timeout)
    if code is None:
        return None, err
    try:
        data = json.loads(out)
    except ValueError:
        return None, clean(f"{argv[0]} gave no usable answer. " + (err.strip().splitlines() or [""])[-1], 300)
    return data, None


def spawn(argv):
    """Start a program in the background (its own session, output discarded)."""
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return False, f"{argv[0]} could not start ({e.strerror})."
    return True, ""


def password_step(argv, timeout=900):
    """Run argv through pkexec, which asks for your password. Returns (ok, text for the page)."""
    code, out, err = run(["pkexec"] + argv, timeout)
    if code is None:
        return False, err
    if code in (126, 127):
        return False, "Cancelled: the password was not entered. Nothing changed."
    text = clean((out + err).strip())
    return code == 0, text or ("Done." if code == 0 else f"Failed (exit code {code}).")


# ---------- Forge Core ----------

def _hex_addr(ip, port):
    raw = ipaddress.ip_address(ip).packed
    if sys.byteorder == "little":
        raw = raw[::-1]
    return f"{raw.hex().upper()}:{port:04X}"


def _tcp_rows():
    try:
        with open(os.path.join(PROC_NET, "tcp")) as f:
            return [line.split() for line in f.readlines()[1:]]
    except OSError:
        return []


def listener_uid(port):
    want = _hex_addr("127.0.0.1", port)
    for cols in _tcp_rows():
        if len(cols) > 7 and cols[1] == want and cols[3] == "0A":
            return int(cols[7]) if cols[7].isdigit() else None
    return None


def connection_owner(port, client_port):
    """Owner of the server side of our connection 127.0.0.1:client_port -> 127.0.0.1:port."""
    local, remote = _hex_addr("127.0.0.1", port), _hex_addr("127.0.0.1", client_port)
    for _ in range(20):   # it shows up as soon as the kernel accepts the connection
        found = [cols for cols in _tcp_rows() if len(cols) > 7 and cols[1] == local and cols[2] == remote
                 and cols[3] == "01"]
        if len(found) == 1 and found[0][7].isdigit():
            return int(found[0][7])
        if found:
            return None
        time.sleep(0.05)
    return None


def core_uid():
    try:
        return pwd.getpwnam("forge").pw_uid
    except KeyError:
        uid = os.environ.get("FORGE_DASH_CORE_UID", "") if TEST else ""
        return int(uid) if uid.isdigit() else None


def core_settings():
    vals = forgecfg.values(forgecfg.load_schema())
    port = vals.get("core.port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        port = 47900
    return vals.get("core.enabled", True) is not False, port, vals


def core_get(path):
    """GET a Forge Core path with the viewer token. Returns (status, data). Raises CoreDown."""
    enabled, port, _ = core_settings()
    if not enabled:
        raise CoreDown("Forge Core is turned off in Forge Settings.")
    uid = core_uid()
    if uid is None:
        raise CoreDown("Forge Core's user was not found on this PC.")
    try:
        with open(VIEWER_TOKEN) as f:
            token = f.read(4096).strip()
    except OSError:
        token = ""
    if not token:
        raise CoreDown("Can't read Forge Core's viewer token. Only administrators can use the dashboard's live data.")
    if listener_uid(port) != uid:
        raise CoreDown("Forge Core is not running (or another program is using its port).")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.connect()
        # Checked again on this exact connection, before the token is sent.
        if connection_owner(port, conn.sock.getsockname()[1]) != uid:
            raise CoreDown("The program on Forge Core's port is not Forge Core. The token was not sent.")
        conn.request("GET", path, headers={"Authorization": f"Bearer {token}"})
        r = conn.getresponse()
        body = r.read(OUT_MAX + 1)
    except (OSError, http.client.HTTPException):
        raise CoreDown("Forge Core is not answering. Check: systemctl status forge-core")
    finally:
        conn.close()
    if len(body) > OUT_MAX:
        raise CoreDown("Forge Core's answer was too large.")
    try:
        return r.status, json.loads(body)
    except ValueError:
        return r.status, None


def core_data(path):
    """(data, error) for a path the viewer token may read."""
    try:
        status, data = core_get(path)
    except CoreDown as e:
        return None, str(e)
    if status != 200 or data is None:
        return None, f"Forge Core could not answer ({status})."
    return data, None


def system_versions():
    """`rpm-ostree status --json`, read now (Forge Core's answer can be up to 60 seconds old) and read the
    way Forge Core reads it. Returns (data, error)."""
    code, out, err = run(["rpm-ostree", "status", "--json"], 60)
    if code != 0:
        return None, clean(err.strip() or "rpm-ostree could not say which versions this PC has.", 300)
    try:
        from forgecore import updates
        data = updates.parse_status(json.loads(out))
    except (ImportError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return None, "rpm-ostree gave no usable answer."
    data["ok"] = True
    return data, None


# ---------- small readers ----------

def current_mode():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local/state")
    try:
        fd = os.open(os.path.join(base, "forge", "mode"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as f:
            mode = f.read(64).strip()
    except (OSError, ValueError):
        return "default"
    return mode if mode in MODES else "default"


def game_status():
    """Whether forge-game is still waiting to switch back from gaming mode (the root step failed so far).
    Read from its own state file, never through a link; the mode name is checked."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local/state")
    try:
        fd = os.open(os.path.join(base, "forge", "game-mode.json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            st = json.loads(f.read(4096))
    except (OSError, ValueError):
        return {"pending": False, "back_to": None}
    if not isinstance(st, dict) or st.get("pending") is not True:
        return {"pending": False, "back_to": None}
    prev = st.get("prev")
    return {"pending": True, "back_to": prev if prev in MODES + ("default",) else "default"}


def parse_virsh(text):
    """`virsh list --all` table to [{id, name, state}]. Columns are found from the header."""
    lines = text.splitlines()
    head = next((i for i, line in enumerate(lines) if "Name" in line and "State" in line), None)
    if head is None:
        return []
    n, s = lines[head].index("Name"), lines[head].index("State")
    out = []
    for line in lines[head + 1:]:
        if not line.strip() or set(line.strip()) <= {"-"}:
            continue
        vid, name, state = line[:n].strip(), line[n:s].strip(), line[s:].strip()
        if not name:
            continue
        out.append({"id": clean(vid, 12), "name": clean(name, 200), "state": clean(state, 40)})
        if len(out) >= 200:
            break
    return out


DISK_KEYS = ("name", "path", "size", "type", "fstype", "label", "mountpoint", "fssize", "fsused", "fsavail", "rm",
             "tran", "model")


def disks():
    data, err = tool_json(["lsblk", "--json", "-b", "-o", ",".join(k.upper() for k in DISK_KEYS)], 30)
    if err:
        return None, err

    def one(d, depth):
        item = {k: d.get(k) for k in DISK_KEYS}
        for k in ("size", "fssize", "fsused", "fsavail"):
            v = item[k]
            if isinstance(v, str) and v.isdigit():   # older lsblk prints numbers as strings
                v = int(v)
            item[k] = v if isinstance(v, int) and not isinstance(v, bool) else None
        for k in ("name", "path", "type", "fstype", "label", "mountpoint", "tran", "model"):
            item[k] = clean(item[k], 200) if isinstance(item[k], str) else None
        item["rm"] = item["rm"] in (True, "1", 1)
        kids = d.get("children") if depth < 3 else None
        item["children"] = [one(c, depth + 1) for c in kids[:64] if isinstance(c, dict)] if isinstance(kids, list) else []
        return item

    devs = data.get("blockdevices") if isinstance(data, dict) else None
    if not isinstance(devs, list):
        return None, "lsblk gave no usable answer."
    return [one(d, 0) for d in devs[:64] if isinstance(d, dict) and d.get("type") in ("disk", "rom")], None


# ---------- the page ----------

def tempered_css():
    """The shared Forge "Tempered" tokens (tempered.css), as text that is safe inside the page's <style>.
    A missing, oversized or odd file gives the small built-in fallback, so the page always has its colours."""
    try:
        fd = os.open(TEMPERED_CSS, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as f:
            data = f.read(TEMPERED_MAX + 1)
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return TEMPERED_FALLBACK
    # Never anything that could end the <style> element or load something from elsewhere.
    if len(data) > TEMPERED_MAX or "<" in text or "@import" in text or "url(" in text or not text.strip():
        return TEMPERED_FALLBACK
    return text


def inline_tempered(page):
    """The page with tempered.css put in at its /*__TEMPERED__*/ mark (once)."""
    return page.replace(TEMPERED_MARK, tempered_css(), 1)


def page_template():
    """dashboard.html with the shared tokens inlined, ready for the token, nonce and string tables."""
    with open(PAGE_FILE, encoding="utf-8") as f:
        return inline_tempered(f.read())


# ---------- the dashboard ----------

class Dashboard:
    def __init__(self):
        self.lock = threading.Lock()
        self.doctor = {"at": None, "data": None}
        self.doctor_run = threading.Lock()      # one doctor run at a time
        self.approval_ids = set()     # from the last admin list, the only ids approve/deny accept
        self.rollback_lock = threading.Lock()   # one rollback at a time
        self.rollback_ran = False               # a rollback ran from this dashboard: never a second one
        self.logs_lock = threading.Lock()       # one log read at a time

    # Data

    def home(self):
        metrics, err = core_data("/api/metrics/now")
        count, _ = core_data("/api/approval-count")
        updates, _ = core_data("/api/system/updates")
        timeline, _ = core_data("/api/timeline?limit=6")
        backup, berr = tool_json(["forge-backup", "status", "--json"])
        return {"host": clean(socket.gethostname(), 64), "mode": current_mode(), "metrics": metrics, "error": err,
                "approvals": (count or {}).get("pending") if isinstance(count, dict) else None,
                "updates": updates, "timeline": (timeline or {}).get("entries", []) if isinstance(timeline, dict) else [],
                "backup": backup if isinstance(backup, dict) else None, "backup_error": berr,
                "game": game_status()}

    def metrics(self):
        data, err = core_data("/api/metrics/now")
        return {"metrics": data, "error": err}

    def history(self):
        out, err = {}, None
        for m in HISTORY:
            data, e = core_data(f"/api/metrics/history?metric={m}&since=900&step=15")
            out[m] = data if isinstance(data, list) else []
            err = err or e
        return {"history": out, "error": err}

    def agents(self):
        agents, err = core_data("/api/agents")
        runs, _ = core_data("/api/runs?limit=25")
        count, _ = core_data("/api/approval-count")
        return {"agents": agents or [], "runs": runs or [], "error": err,
                "approvals": count.get("pending") if isinstance(count, dict) else None}

    def timeline(self):
        data, err = core_data("/api/timeline?limit=300")
        return {"entries": (data or {}).get("entries", []), "sources": (data or {}).get("sources", {}), "error": err}

    def updates(self):
        data, err = core_data("/api/system/updates")
        return {"updates": data, "error": err, "rollback_button": self._rollback_allowed(data),
                "forge_version": forgerelease.version_text(forgerelease.read_version())}

    @staticmethod
    def _rollback_allowed(data):
        return bool(isinstance(data, dict) and data.get("ok") and data.get("rollback_available") is True
                    and data.get("update_ready") is False)

    def backup(self):
        data, err = tool_json(["forge-backup", "status", "--json"])
        return {"backup": data, "error": err}

    def storage(self):
        found, err = disks()
        shared, serr = tool_json(["forge-shared-drive", "status", "--json"])
        metrics, _ = core_data("/api/metrics/now")
        return {"disks": found or [], "error": err, "shared": shared, "shared_error": serr,
                "root_pct": (metrics or {}).get("disk_root_pct") if isinstance(metrics, dict) else None}

    def sync(self):
        data, err = tool_json(["forge-settings-sync", "status", "--json"])
        return {"sync": data, "error": err}

    def hub(self):
        data, err = tool_json(["forge-hub", "status", "--json"])
        return {"hub": data, "error": err}

    def vms(self):
        _, _, vals = core_settings()
        if vals.get("virt.enabled") is not True:
            return {"enabled": False, "vms": [], "error": None}
        code, out, err = run(["virsh", "-c", "qemu:///system", "list", "--all"], 30)
        if code != 0:
            return {"enabled": True, "vms": [], "error": clean(err.strip() or "virsh could not list the VMs.", 300)}
        return {"enabled": True, "vms": parse_virsh(out), "error": None}

    def _doctor_cached(self, since=None):
        """The last result if it is still good: a report for DOCTOR_CACHE_S, a failure for DOCTOR_FAIL_S.
        With since, only a result made after that moment."""
        with self.lock:
            at, data = self.doctor["at"], self.doctor["data"]
        if at is None or data is None:
            return None
        if since is not None:
            return data if at >= since else None
        keep = DOCTOR_CACHE_S if data.get("report") is not None else DOCTOR_FAIL_S
        return data if 0 <= time.monotonic() - at < keep else None

    def doctor_report(self, fresh=False):
        """The doctor report: from Forge Core if the viewer may read it, else forge-doctor --json here.
        Only one run at a time. While one runs, others get the last result, or (when there is none yet)
        wait up to DOCTOR_WAIT_S for it."""
        asked = time.monotonic()
        if not fresh:
            hit = self._doctor_cached()
            if hit is not None:
                return hit
        with self.lock:
            last = self.doctor["data"]
        if not (self.doctor_run.acquire(blocking=False) if last else self.doctor_run.acquire(timeout=DOCTOR_WAIT_S)):
            return last or {"report": None, "error": "The compatibility check is still running. Try again in a moment.",
                            "source": None}
        try:
            hit = self._doctor_cached(since=asked if fresh else None)   # made while this call waited
            if hit is not None:
                return hit
            try:
                status, data = core_get("/api/doctor")
                source = "core"
            except CoreDown:
                status, data = None, None
            err = None
            if status != 200 or not isinstance(data, dict):
                data, err = tool_json(["forge-doctor", "--json"], 180)
                source = "local"
            out = {"report": None, "error": err, "source": source} if err else \
                {"report": data, "error": None, "source": source}
            with self.lock:
                self.doctor.update(at=time.monotonic(), data=out)
            return out
        finally:
            self.doctor_run.release()

    def doctor_text(self):
        code, out, err = run(["forge-doctor"], 180)
        if code != 0:
            return None
        return out

    def runnable_fixes(self, report):
        fixes = (report or {}).get("fixes") if isinstance(report, dict) else None
        ids = set()
        for f in fixes if isinstance(fixes, list) else []:
            if isinstance(f, dict) and f.get("automatic") is True and f.get("id") in FIX_IDS:
                ids.add(f["id"])
        return ids

    # Approvals (the list needs the admin token, so it asks for your password)

    def load_approvals(self):
        code, out, err = run(["pkexec", FORGE_API, "GET", "/api/approvals"], 900)
        if code in (126, 127):
            return {"ok": False, "output": "Cancelled: the password was not entered.", "approvals": []}
        try:
            data = json.loads(out) if code == 0 else None
        except ValueError:
            data = None
        if not isinstance(data, list):
            return {"ok": False, "output": clean(err.strip() or "The approvals could not be read."), "approvals": []}
        items = [a for a in data if isinstance(a, dict) and isinstance(a.get("id"), str)
                 and APPROVAL_ID.fullmatch(a["id"]) and a.get("status") == "pending"][:200]
        with self.lock:
            self.approval_ids = {a["id"] for a in items}
        return {"ok": True, "output": "", "approvals": items}

    def logs(self, body):
        return read_logs(body, self.logs_lock)

    # Actions

    def action(self, body):
        """One confirmed action. Returns {"ok", "output"}."""
        if not isinstance(body, dict):
            return {"ok": False, "output": "Unknown action."}
        name = body.get("action")
        handler = {"backup-now": self._backup_now, "open-shared-drive": self._open_shared,
                   "sync-apply": self._sync_apply, "doctor-fix": self._doctor_fix, "hub-dashboard": self._hub,
                   "approve": lambda b: self._answer(b, "approve"), "deny": lambda b: self._answer(b, "deny"),
                   "rollback": self._rollback, "open-settings": self._open_settings,
                   "restore-test": self._restore_test}.get(name) \
            if isinstance(name, str) else None
        if handler is None:
            return {"ok": False, "output": "Unknown action."}
        if name != "open-settings" and body.get("confirmed") is not True:
            return {"ok": False, "output": "Not done: this action needs to be confirmed first."}
        ok, text = handler(body)
        return {"ok": ok, "output": text}

    def _backup_now(self, _body):
        ok, err = spawn(["forge-backup", "run"])
        return ok, ("Backup started. It runs in the background; this page shows the result when it is done."
                    if ok else err)

    def _restore_test(self, _body):
        ok, err = spawn(["forge-backup", "restore-test"])
        return ok, ("Restore test started. It runs in the background and takes a minute or two; this page "
                    "shows the result when it is done." if ok else err)

    def _open_shared(self, _body):
        code, out, err = run(["forge-shared-drive"], 180)
        if code is None:
            return False, err
        text = clean((out + err).strip())
        return code == 0, text or ("The shared drive is open." if code == 0 else "The shared drive was not opened.")

    @staticmethod
    def _sync_host_problem(host):
        """None if host is a PC in forge-settings-sync's own list right now, else why not."""
        if not isinstance(host, str) or not HOST_RE.fullmatch(host):
            return "Unknown PC."
        data, err = tool_json(["forge-settings-sync", "status", "--json"])
        hosts = {h.get("host") for h in (data or {}).get("hosts", []) if isinstance(h, dict)} \
            if isinstance(data, dict) else set()
        if host not in hosts:
            return "That PC is not in the sync folder's list right now."
        return None

    @staticmethod
    def _sync_changes(host):
        """(ok, text): `forge-settings-sync diff HOST` as shown on the page, or why it could not be read."""
        code, out, err = run(["forge-settings-sync", "diff", host], 120)
        if code != 0:
            return False, clean((err or out).strip(), 300) or "The changes could not be read."
        return True, scrub(clean(out.strip(), OUT_MAX))

    def sync_diff(self, body):
        """Each setting another PC's settings would change here, as text, and the hash of exactly that
        text. Applying needs the hash back, so it applies only what was shown."""
        host = body.get("host") if isinstance(body, dict) else None
        problem = self._sync_host_problem(host)
        if problem:
            return {"ok": False, "output": problem, "text": "", "hash": ""}
        ok, text = self._sync_changes(host)
        if not ok:
            return {"ok": False, "output": text, "text": "", "hash": ""}
        return {"ok": True, "output": "", "text": text, "hash": hashlib.sha256(text.encode()).hexdigest()}

    def _sync_apply(self, body):
        host = body.get("host")
        problem = self._sync_host_problem(host)
        if problem:
            return False, problem
        shown = body.get("diff_hash")
        if not isinstance(shown, str) or not SHA256_HEX.fullmatch(shown):
            return False, "Not applied: look at the list of changes first."
        # Checked again right before applying. forge-settings-sync apply reads the file once more itself,
        # so a file swapped in the few milliseconds between the two reads is not caught here (see NOTES.md).
        ok, text = self._sync_changes(host)
        if not ok:
            return False, "Not applied. " + text
        if not hmac.compare_digest(hashlib.sha256(text.encode()).hexdigest(), shown):
            return False, ("Not applied: the settings changed since the list was shown (on that PC or on this "
                           "one). Look at the changes again.")
        code, out, err = run(["forge-settings-sync", "apply", host], 900)
        if code is None:
            return False, err
        return code == 0, clean((out + err).strip()) or ("Applied." if code == 0 else "Not applied.")

    def _doctor_fix(self, body):
        fix = body.get("id")
        if not isinstance(fix, str) or fix not in FIX_IDS:
            return False, "Unknown fix."
        if fix not in self.runnable_fixes((self.doctor_report() or {}).get("report")):
            return False, "That fix is not in the doctor's current list. Refresh the report."
        ok, text = password_step([DOCTOR_FIX, fix])
        with self.lock:
            self.doctor["at"] = None    # check again next time
        return ok, text

    def _hub(self, _body):
        code, out, err = run(["forge-hub", "dashboard"], 60)
        if code is None:
            return False, err
        return code == 0, clean((out + err).strip()) or ("Opened." if code == 0 else "Not opened.")

    def _answer(self, body, verb):
        aid = body.get("id")
        with self.lock:
            known = isinstance(aid, str) and APPROVAL_ID.fullmatch(aid) is not None and aid in self.approval_ids
        if not known:
            return False, "Unknown approval. Show the waiting approvals again."
        ok, text = password_step([FORGE_API, "POST", f"/api/approvals/{aid}/{verb}"], 900)
        if ok:
            with self.lock:
                self.approval_ids.discard(aid)
            text = "Approved." if verb == "approve" else "Denied."
        return ok, text

    def _rollback(self, body):
        if body.get("typed") != ROLLBACK_WORD:
            return False, f"Not done: type {ROLLBACK_WORD} to confirm."
        if not self.rollback_lock.acquire(blocking=False):
            return False, "Not done: a rollback is already running from this dashboard. Wait for its result."
        try:
            if self.rollback_ran:
                return False, ("Not done: a rollback was already run from this dashboard. Running it again would "
                               "switch back to the version you had. Restart the PC to use the new default. To undo "
                               "it, run in a terminal: rpm-ostree rollback")
            data, _ = system_versions()   # fresh, right before the password prompt
            if not self._rollback_allowed(data):
                return False, ("Not done from here. Either there is no older version, or an update is waiting for "
                               "a restart. Restart first, or use the boot menu, or run in a terminal: "
                               "rpm-ostree rollback")
            code, out, err = run(["pkexec", RPM_OSTREE, "rollback"], 900)
            if code in (126, 127):
                return False, "Cancelled: the password was not entered. Nothing changed."
            if code == 0 or code is None:
                self.rollback_ran = True   # it ran, or may still finish: never start a second one
            if code == 0:
                return True, ("Done. Restart the PC to start the new default version. To undo it, run in a "
                              "terminal: rpm-ostree rollback")
            if code is None:
                return False, err + " It may still finish. Check with: rpm-ostree status"
            return False, clean((out + err).strip()) or f"Failed (exit code {code})."
        finally:
            self.rollback_lock.release()

    def _open_settings(self, _body):
        ok, err = spawn(["forge-settings"])
        return ok, "Forge Settings is opening." if ok else err


# ---------- logs (read only) ----------

def _redactor():
    """A function that hides secrets in one log line: forgecore.policy.clean_details, the same redaction
    as the audit log and the timeline (it also finds pairs inside other pairs, like
    "login failed: password=x", and passwords on curl, mysql and sshpass command lines)."""
    from forgecore import policy
    clean_details = policy.clean_details

    def redact(unit, msg):
        safe = clean_details({"unit": unit, "msg": msg}, max_str=LOG_MSG_MAX)
        return str(safe["unit"]), str(safe["msg"])
    return redact


def log_argvs(service, level, since):
    """The fixed journalctl argv lists for one filter. Nothing from the page goes in except values
    checked against the fixed lists above."""
    common = ["--no-pager", "-o", "json", f"--output-fields={LOG_FIELDS}", "--reverse",
              "-n", str(LOG_PER_JOURNAL), f"--priority={LOG_LEVELS[level]}", f"--since={LOG_SINCE[since]}"]
    out = []
    if service in ("all", "forge") or service in LOG_SYSTEM_UNITS:
        units = LOG_SYSTEM_UNITS if service == "forge" else (service,) if service in LOG_SYSTEM_UNITS else ()
        out.append(["journalctl", "--system"] + common + [f"--unit={u}" for u in units])
    if service in ("all", "forge") or service in LOG_USER_UNITS:
        units = LOG_USER_UNITS if service == "forge" else (service,) if service in LOG_USER_UNITS else ()
        out.append(["journalctl", "--user"] + common + [f"--user-unit={u}" for u in units])
    return out


def read_bounded(argv, max_bytes, max_lines, timeout):
    """Run argv and read at most max_bytes and max_lines of its output, for at most timeout seconds.
    Returns (exit code or None, stdout bytes, stderr text, cut)."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return None, b"", f"{argv[0]} is not installed or could not run.", False
    out, err, cut, lines = bytearray(), bytearray(), False, 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(p.stdout, selectors.EVENT_READ)
            sel.register(p.stderr, selectors.EVENT_READ)
            while sel.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    cut = True
                    break
                for key, _ in sel.select(left):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                    elif key.fileobj is p.stdout:
                        out += chunk
                        lines += chunk.count(b"\n")
                    elif len(err) < 65536:
                        err += chunk
                if len(out) >= max_bytes or lines >= max_lines:
                    cut = len(out) >= max_bytes or lines > max_lines
                    break
    finally:
        if p.poll() is None:
            p.kill()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            pass
        p.stdout.close()
        p.stderr.close()
    code = p.returncode if not cut else None
    return code, bytes(out[:max_bytes]), err.decode("utf-8", "replace"), cut


def _journal_access(code, err):
    if "No journal files were opened due to insufficient permissions" in err:
        return "none"
    if "not seeing messages from other users and the system" in err:
        return "own"
    if "not opened due to insufficient permissions" in err:
        return "partial"
    return "full" if code in (0, None) else "error"


def _journal_entry(line, scope):
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not isinstance(e, dict):
        return None
    ts = e.get("__REALTIME_TIMESTAMP")
    if not isinstance(ts, str) or not ts.isdigit() or len(ts) > 20:
        return None
    msg = e.get("MESSAGE")
    if isinstance(msg, list):   # journald sends non-UTF-8 text as a list of byte values
        msg = bytes(b for b in msg[:LOG_MSG_MAX * 4] if isinstance(b, int) and 0 <= b < 256).decode("utf-8", "replace")
    if not isinstance(msg, str):
        msg = ""
    prio = e.get("PRIORITY")
    level = LOG_PRIORITY[int(prio)] if isinstance(prio, str) and prio.isdigit() and int(prio) < 8 else "info"
    unit = next((e[k] for k in ("_SYSTEMD_UNIT", "_SYSTEMD_USER_UNIT", "SYSLOG_IDENTIFIER", "_COMM")
                 if isinstance(e.get(k), str) and e[k]), "")
    # Cut very long text before the redaction, but well past what is shown (policy hides first,
    # then cuts, and drops a partial secret at an early cut).
    return {"ts": int(ts) / 1e6, "level": level, "scope": scope, "unit": clean(unit, 120),
            "msg": msg[:LOG_MSG_MAX * 8]}


def read_logs(body, lock):
    """Recent journal lines for the dashboard's log viewer. Read only, no shell, bounded, redacted."""
    base = {"ok": False, "entries": [], "cut": False, "journals": {}, "services": list(LOG_SERVICES),
            "groups": [], "error": None}
    if not isinstance(body, dict):
        return dict(base, error="Unknown filter.")
    service, level, since, search = (body.get("service", "all"), body.get("level", "warning"),
                                     body.get("since", "24h"), body.get("search", ""))
    if not isinstance(service, str) or service not in LOG_SERVICES:
        return dict(base, error="Unknown service.")
    if not isinstance(level, str) or level not in LOG_LEVELS:
        return dict(base, error="Unknown level.")
    if not isinstance(since, str) or since not in LOG_SINCE:
        return dict(base, error="Unknown time range.")
    if not isinstance(search, str) or len(search) > LOG_SEARCH_MAX or not (search.isprintable() or search == ""):
        return dict(base, error=f"Search text: up to {LOG_SEARCH_MAX} plain characters.")
    try:
        redact = _redactor()
    except Exception:   # never show lines that could not be checked for secrets
        return dict(base, error="Log lines can't be checked for secrets here (Forge Core's redaction is "
                                "missing), so none are shown. Use journalctl in a terminal.")
    if not lock.acquire(blocking=False):
        return dict(base, error="The logs are being read already. Try again in a moment.")
    try:
        try:
            names = {grp.getgrgid(g).gr_name for g in os.getgroups()}
        except (KeyError, OSError):
            names = set()
        base["groups"] = sorted(names & set(LOG_GROUPS))
        entries, cut, errors = [], False, []
        for argv in log_argvs(service, level, since):
            scope = argv[1].lstrip("-")
            code, out, err, was_cut = read_bounded(argv, LOG_BYTES, LOG_PER_JOURNAL, LOG_TIMEOUT_S)
            if code is None and not out and not was_cut:
                return dict(base, error=clean(err, 300))
            cut = cut or was_cut
            access = _journal_access(code, err)
            last = [x.strip() for x in err.splitlines() if x.strip() and not x.strip().startswith(
                ("Hint:", "Users in", "Pass -q"))][-1:]
            base["journals"][scope] = {"access": access,
                                       "error": clean(last[0], 300) if access in ("error", "none") and last else None}
            if access == "error" and last:
                errors.append(clean(last[0], 300))
            for line in out.decode("utf-8", "replace").splitlines():
                e = _journal_entry(line, scope)
                if e:
                    entries.append(e)
        shown, seen = [], set()
        needle = search.casefold()
        for e in sorted(entries, key=lambda x: x["ts"], reverse=True):
            key = (e["ts"], e["unit"], e["msg"])
            if key in seen:
                continue    # the same line from both journals
            seen.add(key)
            unit, msg = redact(e["unit"], e["msg"])
            e = dict(e, unit=clean(unit, 120), msg=_CONTROL.sub("", msg))
            if needle and needle not in (e["unit"] + "\n" + e["msg"]).casefold():
                continue
            if len(shown) >= LOG_SHOWN:
                cut = True
                break
            shown.append(e)
        return dict(base, ok=True, entries=shown, cut=cut, error="; ".join(errors) or None)
    finally:
        lock.release()
