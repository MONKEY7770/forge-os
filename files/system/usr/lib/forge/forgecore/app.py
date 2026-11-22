#!/usr/bin/env python3
"""Forge Core: the local API for Forge OS.

Listens on 127.0.0.1 only (port from Forge Settings, default 47900).
Every call except /api/health needs a token, sent as "Authorization: Bearer <token>"
or "X-Forge-Token: <token>". The admin token (/etc/forge/core.token, root and Forge Core
only) can do everything. The viewer token (/etc/forge/viewer.token, admin users) can only
read, and can't read secrets, gateway keys and usage, the audit log or the policy.
Interactive docs for every endpoint: http://127.0.0.1:47900/docs

It runs as the unprivileged "forge" user. The only root action it can take
is running forge-apply, which validates every setting before applying it.
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
from contextlib import asynccontextmanager

sys.path.insert(0, os.environ.get("FORGE_LIB", "/usr/lib/forge"))

import forgecfg  # noqa: E402
from forgecore import models as M  # noqa: E402
from forgecore.metrics import METRICS, Collector  # noqa: E402
from forgecore import agents as A  # noqa: E402
from forgecore import gateway as G  # noqa: E402
from forgecore import policy as P  # noqa: E402
from forgecore import doctor as D  # noqa: E402
from forgecore import updates as U  # noqa: E402
from forgecore import timeline as T  # noqa: E402
import ipaddress  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import PlainTextResponse, StreamingResponse  # noqa: E402

VERSION = "0.1.0"
TOKEN_PATH = os.environ.get("FORGE_CORE_TOKEN", "/etc/forge/core.token")
VIEWER_TOKEN_PATH = os.environ.get("FORGE_VIEWER_TOKEN", "/etc/forge/viewer.token")
DB_PATH = os.environ.get("FORGE_CORE_DB", "/var/lib/forge/metrics.db")
RUN_DIR = os.environ.get("FORGE_CORE_RUN", "/run/forge-core")
HW_STATE = os.environ.get("FORGE_HW_STATE", "/var/lib/forge-hw")
DATA = os.environ.get("FORGE_CORE_DATA", "/var/lib/forge")
AGENTS_SYSTEM_DIR = os.environ.get("FORGE_AGENTS_SYSTEM", "/usr/share/forge/agents")
# How settings get applied. Root runs forge-apply directly, the forge user goes through sudo.
APPLY_CMD = os.environ.get("FORGE_CORE_APPLY", "sudo -n /usr/libexec/forge-apply").split()


def read_token(path=TOKEN_PATH):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")
# IPv4-translated addresses (SIIT, RFC 2765): they also carry an IPv4 address inside.
_SIIT = ipaddress.ip_network("::ffff:0:0:0/96")


# Reads the viewer token may not do: secrets, keys and usage, the audit log and the policy.
# Approvals show exactly what an agent wants to do (secrets included), so they are admin
# only too. The viewer can still see how many are waiting: /api/approval-count.
# The doctor report describes the PC's security setup, so it is admin only as well.
VIEWER_BLOCKED = ("/api/gateway", "/api/audit", "/api/policy", "/api/approvals", "/api/doctor")


def _viewer_blocked(path):
    # Normalise the way a router might, so odd spellings can't slip past.
    path = "/" + "/".join(p for p in path.split("/") if p and p != ".")
    path = path.lower()
    if "/.." in path:
        return True
    return any(path == b or path.startswith(b + "/") for b in VIEWER_BLOCKED)


def _nat64_ipv4(ip):
    """The IPv4 address inside a NAT64 address (RFC 6052), or None."""
    b = ip.packed
    if ip in _NAT64_WELL_KNOWN:
        return ipaddress.IPv4Address(b[12:16])
    if ip in _NAT64_LOCAL:
        # For a /48 prefix the IPv4 bytes sit around the reserved byte 8.
        return ipaddress.IPv4Address(b[6:8] + b[9:11])
    return None


def _ip_allowed(ip):
    """Only public internet addresses. IPv6 forms that hide an IPv4 address are refused too."""
    if ip.version == 6:
        if (ip.ipv4_mapped or ip.sixtofour or ip.teredo
                or ip in ipaddress.ip_network("::/96") or ip in _SITE_LOCAL or ip in _SIIT):
            return False
        inner = _nat64_ipv4(ip)
        if inner is not None and not inner.is_global:
            return False
    return ip.is_global


def _wall_clock():
    return time.time()


def _uptime_s():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _clock_trusted():
    """Can the wall clock be trusted for deleting "old" data? Right after boot, before time sync,
    it can be years off. Trusted when synchronized, after a day of uptime (an offline PC still
    cleans up), or when it can't be checked at all (the earlier behaviour)."""
    try:
        r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    if (r.stdout or "").strip() == "yes":
        return True
    return _uptime_s() >= 86400


def safe_fetch(url, max_bytes=100_000, deadline_s=20):
    """Fetch a public web page for an agent. Refuses this PC, the local network and
    anything that is not http(s), so agents can't reach Forge Core or other local services.
    The address is checked once and the connection goes to exactly that address, so a
    second DNS answer can't swap in a private one. Size and total time are capped."""
    import http.client
    import ssl
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "Error: only http and https web addresses are allowed"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    end = time.monotonic() + deadline_s
    # The name lookup has no timeout of its own, so it runs in a helper thread and the
    # deadline covers it too. A stuck lookup is left behind; the agent goes on.
    found = {}

    def lookup():
        try:
            found["infos"] = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as e:
            found["error"] = e

    helper = threading.Thread(target=lookup, name="forge-fetch-dns", daemon=True)
    helper.start()
    helper.join(max(0.0, end - time.monotonic()))
    if helper.is_alive():
        return "Error: the website took too long to answer"
    if "infos" not in found:
        return "Error: could not find that website"
    ips = [ipaddress.ip_address(i[4][0]) for i in found["infos"]]
    if not ips or not all(_ip_allowed(ip) for ip in ips):
        return "Error: local and private network addresses are not allowed"
    pinned = str(ips[0])

    def time_left():
        left = end - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline")
        return left

    # Every send and receive first sets the socket timeout to the time that is left,
    # so a server sending one byte at a time still can't go past the deadline.
    class Capped:
        def _cap(self):
            self.settimeout(time_left())

        def recv(self, *a, **k):
            self._cap()
            return super().recv(*a, **k)

        def recv_into(self, *a, **k):
            self._cap()
            return super().recv_into(*a, **k)

        def send(self, *a, **k):
            self._cap()
            return super().send(*a, **k)

        def sendall(self, *a, **k):
            self._cap()
            return super().sendall(*a, **k)

    class CappedSocket(Capped, socket.socket):
        pass

    class CappedSSLSocket(Capped, ssl.SSLSocket):
        pass

    class Pinned(http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection):
        def connect(self):
            raw = socket.create_connection((pinned, port), timeout=min(10, time_left()))
            sock = CappedSocket(raw.family, raw.type, raw.proto, raw.detach())
            if parts.scheme == "https":
                ctx = ssl.create_default_context()
                ctx.sslsocket_class = CappedSSLSocket
                sock.settimeout(time_left())   # the handshake is limited by this
                sock = ctx.wrap_socket(sock, server_hostname=parts.hostname)
            self.sock = sock

    conn = Pinned(parts.hostname, port, timeout=10)
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request("GET", path, headers={"User-Agent": "ForgeOS-agent", "Accept-Encoding": "identity"})
        r = conn.getresponse()   # redirects are not followed
        body = b""
        while len(body) < max_bytes:
            try:
                time_left()
                chunk = r.read1(min(16384, max_bytes - len(body)))
            except TimeoutError:
                body += b"\n[stopped: took too long]"
                break
            if not chunk:
                break
            body += chunk
        return f"HTTP {r.status}\n" + body.decode("utf-8", "replace")
    except TimeoutError:
        return "Error: the website took too long [stopped: took too long]"
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        return f"Error: {type(e).__name__}"
    finally:
        conn.close()


def create_app(token=None, collector=None, ollama=None, start_collector=True,
               gateway=None, policy=None, agent_store=None, runner=None, data_dir=None,
               viewer_token=None, cleanup_delay_s=60, cleanup_check_s=600, doctor=None, doctor_cache_s=30):
    token = token if token is not None else read_token()
    viewer_token = viewer_token if viewer_token is not None else read_token(VIEWER_TOKEN_PATH)
    collector = collector or Collector(DB_PATH, float(os.environ.get("FORGE_CORE_INTERVAL", "15")))
    ollama = ollama or M.OllamaClient(M.base_url_from_env())
    data = data_dir or DATA
    os.makedirs(data, exist_ok=True)
    secrets_store = None
    if gateway is None:
        secrets_store = G.SecretStore(os.path.join(data, "secrets.json"))
        gateway = G.Gateway(secrets_store, os.path.join(data, "gateway.db"),
                            providers=G.load_providers(os.path.join(data, "providers.json")))
    policy = policy or P.Policy(os.path.join(data, "policy.db"), os.path.join(data, "policy.json"))
    agent_store = agent_store or A.AgentStore(AGENTS_SYSTEM_DIR, os.path.join(data, "agents"))

    # Tools agents can use. Each one is checked against the permission policy first.
    notices_path = os.path.join(data, "notices.jsonl")

    def notify(message: str):
        line = json.dumps({"ts": int(time.time()), "message": str(message)[:500]})
        with open(notices_path, "a") as f:
            f.write(line + "\n")
        # Keep the file small: when it passes 200 KB, keep only the newest 500 messages.
        if os.path.getsize(notices_path) > 200_000:
            with open(notices_path) as f:
                tail = f.readlines()[-500:]
            with open(notices_path, "w") as f:
                f.writelines(tail)
        return "Notification queued"

    def set_setting_tool(key: str, value):
        merged = forgecfg.values()
        if key not in merged:
            return f"Error: unknown setting {key}"
        merged[key] = value
        try:
            return json.dumps(save(merged))
        except HTTPException as e:
            return f"Error: {e.detail}"

    handlers = {
        "get_metrics": lambda: collector.current(),
        "list_models": lambda: ollama.list(),
        "get_setting": lambda key: forgecfg.values().get(key, f"Unknown setting {key}"),
        "set_setting": set_setting_tool,
        "notify": notify,
        "fetch_url": lambda url: safe_fetch(url),
        # run_command is not offered yet: there is no sandbox for it.
    }
    tools = A.Tools(handlers, os.path.join(data, "workspaces"))
    runner_kwargs = {"max_parallel": int(os.environ.get("FORGE_AGENTS_MAX_PARALLEL", "2"))}
    if hasattr(gateway, "is_local"):
        runner_kwargs["is_local"] = gateway.is_local   # any non-local model needs "ai.cloud" approval
    runner = runner or A.Runner(agent_store, tools, policy, gateway.chat, os.path.join(data, "agents.db"),
                                **runner_kwargs)

    # Daily cleanup so audit logs, usage and run history never fill the disk.
    stop_cleanup = threading.Event()

    # First run soon after start, so a PC that is off every night still gets cleaned.
    def cleanup_loop():
        # Runs soon after start, then once a day by the wall clock, checked every few minutes.
        # (A sleep of a whole day would not count time the PC spends suspended.)
        if stop_cleanup.wait(cleanup_delay_s):
            return
        last = None
        while True:
            now = _wall_clock()
            if (last is not None and 0 <= now - last < 86400) or not _clock_trusted():
                if stop_cleanup.wait(cleanup_check_s):
                    return
                continue
            last = now
            for job in (lambda: policy.prune(90), lambda: getattr(gateway, "prune", lambda d: None)(90),
                        lambda: getattr(runner, "prune", lambda d: None)(30)):
                try:
                    job()
                except Exception:   # cleanup must never take Forge Core down
                    pass
    scheduler_on = os.environ.get("FORGE_AGENTS_SCHEDULER", "1") == "1"

    @asynccontextmanager
    async def lifespan(app):
        if start_collector:
            collector.start()
            threading.Thread(target=cleanup_loop, daemon=True, name="forge-cleanup").start()
            if scheduler_on:
                runner.start_scheduler(prune_ok=_clock_trusted)
        yield
        stop_cleanup.set()
        if start_collector:
            if scheduler_on:
                runner.stop_scheduler()
            collector.stop()

    app = FastAPI(title="Forge Core", version=VERSION, lifespan=lifespan,
                  description="Local API for Forge OS: settings, monitoring and AI models.")

    # Two tokens. The admin token can do everything. The viewer token (readable by admin
    # users without a password) can only read, and not secrets, audit log or policy.
    def auth(request: Request):
        x_forge_token = request.headers.get("x-forge-token", "")
        authorization = request.headers.get("authorization", "")
        given = x_forge_token or (authorization[7:] if authorization.startswith("Bearer ") else "")
        if token and secrets.compare_digest(given, token):
            return
        if not viewer_token or not secrets.compare_digest(given, viewer_token):
            raise HTTPException(401, "Missing or wrong Forge token. Run forge-api, or sudo forge-api for changes.")
        if request.method != "GET" or _viewer_blocked(request.scope.get("path", "")):
            raise HTTPException(403, "This needs admin rights: run sudo forge-api ...")

    def ollama_call(fn, *args):
        try:
            return fn(*args)
        except M.OllamaError as e:
            raise HTTPException(503, str(e))

    def hardware():
        now = collector.current()
        vram = now.get("vram_total_mb")
        ram = now.get("mem_total_gb")
        return vram, ram

    # Health

    @app.get("/api/health", tags=["system"])
    def health():
        return {"ok": True, "version": VERSION}

    # Settings

    @app.get("/api/settings", tags=["settings"], dependencies=[Depends(auth)])
    def get_settings(tag: str | None = None):
        schema = forgecfg.load_schema()
        vals = forgecfg.values(schema)
        items = [dict(s, value=vals[s["key"]]) for s in schema["settings"] if not tag or tag in s["tags"]]
        return {"groups": schema["groups"], "settings": items}

    @app.get("/api/settings/{key}", tags=["settings"], dependencies=[Depends(auth)])
    def get_setting(key: str):
        vals = forgecfg.values()
        if key not in vals:
            raise HTTPException(404, f"Unknown setting: {key}")
        return {"key": key, "value": vals[key]}

    apply_lock = threading.Lock()

    def save(new_values):
        schema = forgecfg.load_schema()
        clean, errors = forgecfg.validate(new_values, schema)
        if errors:
            raise HTTPException(422, {"errors": errors})
        os.makedirs(RUN_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=RUN_DIR)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(clean, f)
            os.chmod(tmp, 0o644)
            with apply_lock:
                r = subprocess.run(APPLY_CMD + ["--import", tmp], capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Still applying (this can take a few minutes after disk changes). Check again soon.")
        finally:
            os.unlink(tmp)
        if r.returncode != 0:
            raise HTTPException(500, (r.stderr or r.stdout).strip()[:2000])
        return {"ok": True, "output": r.stdout.strip()}

    @app.put("/api/settings", tags=["settings"], dependencies=[Depends(auth)])
    def put_settings(values: dict = Body(..., embed=True)):
        """Change one or more settings, for example {"values": {"ai.keep_alive_min": 10}}."""
        defs = forgecfg.settings_by_key()
        unknown = [k for k in values if k not in defs]
        if unknown:
            raise HTTPException(422, {"errors": [f"Unknown setting: {k}" for k in unknown]})
        merged = forgecfg.values()
        merged.update(values)
        return save(merged)

    @app.post("/api/settings/reset", tags=["settings"], dependencies=[Depends(auth)])
    def reset_settings(keys: list[str] | None = Body(default=None, embed=True)):
        """Reset the listed settings to their defaults, or all of them if no list is given."""
        defs = forgecfg.settings_by_key()
        merged = forgecfg.values()
        for k in (keys or list(defs)):
            if k not in defs:
                raise HTTPException(422, {"errors": [f"Unknown setting: {k}"]})
            merged[k] = defs[k]["default"]
        return save(merged)

    # Monitoring

    @app.get("/api/metrics", tags=["monitoring"], dependencies=[Depends(auth)])
    def list_metrics():
        return [{"name": n, "unit": u, "description": d} for n, u, d in METRICS]

    @app.get("/api/metrics/now", tags=["monitoring"], dependencies=[Depends(auth)])
    def metrics_now():
        return collector.current()

    @app.get("/api/metrics/history", tags=["monitoring"], dependencies=[Depends(auth)])
    def metrics_history(metric: str,
                        since: int = Query(3600, ge=60, le=90 * 86400, description="Seconds back"),
                        step: int = Query(60, ge=15, le=86400, description="Bucket size in seconds")):
        try:
            return collector.history(metric, since, step)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/hardware", tags=["monitoring"], dependencies=[Depends(auth)])
    def get_hardware():
        vram, ram = hardware()
        out = {"vram_total_mb": vram, "ram_total_gb": ram, "ollama_running": ollama.available(),
               "profile": None, "recent_changes": []}
        try:
            with open(os.path.join(HW_STATE, "hardware.json")) as f:
                out["profile"] = json.load(f)
            with open(os.path.join(HW_STATE, "hardware-changes.log")) as f:
                out["recent_changes"] = [line.strip() for line in f.readlines()[-20:]]
        except (OSError, ValueError):
            pass
        return out

    # The doctor report runs a few system tools, so only one is built at a time
    # and a recent one is reused.
    doctor = doctor or D.build_report
    doctor_lock = threading.Lock()
    doctor_last = {"at": None, "report": None}

    def doctor_report():
        with doctor_lock:
            at = doctor_last["at"]
            if at is None or not (0 <= time.monotonic() - at < doctor_cache_s):
                try:
                    report = doctor()
                except Exception:
                    raise HTTPException(500, "The doctor report could not be built. Run forge-doctor for details.")
                doctor_last.update(at=time.monotonic(), report=report)
            return doctor_last["report"]

    @app.get("/api/doctor", tags=["monitoring"], dependencies=[Depends(auth)])
    def get_doctor(format: str = Query("json", pattern="^(json|text)$")):
        report = doctor_report()
        if format == "text":
            return PlainTextResponse(D.to_text(report), headers={
                "Content-Disposition": 'attachment; filename="forge-doctor.txt"'})
        return report

    # AI models

    @app.get("/api/models", tags=["models"], dependencies=[Depends(auth)])
    def list_models():
        vram, ram = hardware()
        out = ollama_call(ollama.list)
        for m in out:
            m["fit"] = M.fit(m.get("size_bytes") or 0, vram, ram)
        return out

    @app.get("/api/models/loaded", tags=["models"], dependencies=[Depends(auth)])
    def loaded_models():
        return ollama_call(ollama.loaded)

    @app.post("/api/models/fit", tags=["models"], dependencies=[Depends(auth)])
    def check_fit(size_bytes: int = Body(..., embed=True, ge=0)):
        """Will a model of this size run on the GPU? Check before downloading."""
        vram, ram = hardware()
        return M.fit(size_bytes, vram, ram)

    @app.post("/api/models/pull", tags=["models"], dependencies=[Depends(auth)])
    def pull_model(name: str = Body(..., embed=True)):
        """Download a model. Ollama names (qwen3:8b) and Hugging Face GGUF names
        (hf.co/user/repo:Q4_K_M) both work. Streams progress as JSON lines."""
        try:
            name = M.normalize_name(name)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if not ollama.available():
            raise HTTPException(503, "Ollama is not running")

        def stream():
            try:
                for step in ollama.pull(name):
                    yield json.dumps(step) + "\n"
            except M.OllamaError as e:
                yield json.dumps({"error": str(e)}) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.delete("/api/models/{name:path}", tags=["models"], dependencies=[Depends(auth)])
    def delete_model(name: str):
        try:
            name = M.normalize_name(name)
        except ValueError as e:
            raise HTTPException(422, str(e))
        ollama_call(ollama.delete, name)
        return {"ok": True}

    @app.post("/api/models/{name:path}/unload", tags=["models"], dependencies=[Depends(auth)])
    def unload_model(name: str):
        """Free the VRAM a loaded model is using."""
        try:
            name = M.normalize_name(name)
        except ValueError as e:
            raise HTTPException(422, str(e))
        ollama_call(ollama.unload, name)
        return {"ok": True}

    # Phase 3: AI gateway, permissions and agents
    app.include_router(G.make_v1_router(gateway))
    app.include_router(G.make_admin_router(gateway, auth))
    app.include_router(P.make_router(policy, auth), prefix="/api")
    app.include_router(A.make_router(agent_store, runner, tools, auth))
    app.include_router(U.make_router(auth))
    app.include_router(T.make_router(auth, runner))

    @app.get("/api/notices", tags=["agents"], dependencies=[Depends(auth)])
    def notices(since: int = 0):
        """Messages agents sent with the notify tool (forge-health shows them on the desktop)."""
        out = []
        try:
            with open(os.path.join(data, "notices.jsonl")) as f:
                for line in f.readlines()[-200:]:
                    item = json.loads(line)
                    if item["ts"] > since:
                        out.append(item)
        except (OSError, ValueError):
            pass
        return out

    return app


def main():
    import uvicorn
    port = int(os.environ.get("FORGE_CORE_PORT", "47900"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
