"""Forge AI gateway: one OpenAI-compatible endpoint for local and cloud models.

Apps send OpenAI-style requests to /v1 with a gateway key ("fk-..."). The
model name picks the provider: "openai/gpt-x" goes to OpenAI, anything without
a known provider prefix (like "qwen3:8b" or "hf.co/user/repo:Q4") goes to Ollama.

Cloud API keys live in a small 0600 JSON secret store. Secret values and full
gateway keys are never logged, returned or written to the usage table.
"""
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
KEY_PREFIX = "fk-"
log = logging.getLogger("forge.gateway")
GROUP_COLUMNS = {"model": "model", "provider": "provider", "key": "key_id", "caller": "caller"}

DEFAULT_PROVIDERS = [
    {"name": "ollama", "base_url": "http://127.0.0.1:11434/v1", "secret": None, "local": True},
    {"name": "openai", "base_url": "https://api.openai.com/v1", "secret": "OPENAI_API_KEY", "local": False},
    {"name": "anthropic", "base_url": "https://api.anthropic.com/v1", "secret": "ANTHROPIC_API_KEY", "local": False},
    {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "secret": "OPENROUTER_API_KEY", "local": False},
]

# Long enough for a big local model to load and answer.
TIMEOUT = httpx.Timeout(300.0, connect=5.0)


def _ensure_private_file(path):
    """Create the file (and its folder) with mode 0600 if it is missing."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


def _hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _int(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _tokens(data):
    """Read prompt and completion token counts from an OpenAI-style usage block."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    return _int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens"))


class SecretStore:
    """Named secrets (cloud API keys) in a JSON file only the owner can read."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _read(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except ValueError:
            # Do not silently wipe a damaged file by saving over it.
            raise RuntimeError(f"Secret store {self.path} is not valid JSON. Fix or remove it.")
        return data if isinstance(data, dict) else {}

    def _write(self, data):
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def set(self, name: str, value: str):
        if not isinstance(name, str) or not SECRET_NAME_RE.match(name):
            raise ValueError("Secret names use capital letters, digits and _ (like OPENAI_API_KEY), 2 to 64 characters.")
        if not isinstance(value, str) or not value:
            raise ValueError("Secret value is empty.")
        if len(value) > 8192:
            raise ValueError("Secret value is too long.")
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Secret value has control characters (like a newline). Paste it again without them.")
        with self._lock:
            data = self._read()
            data[name] = {"value": value, "created": int(time.time())}
            self._write(data)

    def get(self, name: str) -> str | None:
        with self._lock:
            item = self._read().get(name)
        return item.get("value") if isinstance(item, dict) else None

    def delete(self, name: str) -> bool:
        with self._lock:
            data = self._read()
            if name not in data:
                return False
            del data[name]
            self._write(data)
            return True

    def list(self) -> list[dict]:
        with self._lock:
            data = self._read()
        out = []
        for name in sorted(data):
            value = data[name].get("value", "") if isinstance(data[name], dict) else ""
            # Short values would be mostly revealed by their last 4 characters, so hide them fully.
            hint = "..." + value[-4:] if len(value) >= 12 else "..."
            out.append({"name": name, "created": data[name].get("created", 0), "hint": hint})
        return out


def load_providers_with_warnings(path: str | None = None) -> tuple[list[dict], list[str]]:
    """DEFAULT_PROVIDERS plus any extra providers from a JSON list file, and a list of warnings.

    An extra entry with the same name as a default replaces it. Bad entries are
    skipped with a warning, and a broken file only gives a warning, never an error."""
    providers = {p["name"]: dict(p) for p in DEFAULT_PROVIDERS}
    warnings = []
    if not path or not os.path.exists(path):
        return list(providers.values()), warnings
    try:
        with open(path) as f:
            extra = json.load(f)
    except (OSError, ValueError) as e:
        warnings.append(f"Could not read {path}, using built-in providers only: {e}")
        extra = []
    if not isinstance(extra, list):
        warnings.append(f"{path} must hold a JSON list of providers. Using built-in providers only.")
        extra = []
    for i, p in enumerate(extra):
        if not isinstance(p, dict):
            warnings.append(f"Skipped provider #{i + 1} in {path}: not a JSON object.")
            continue
        name, url, secret = p.get("name"), p.get("base_url"), p.get("secret")
        if not isinstance(name, str) or not PROVIDER_NAME_RE.match(name):
            warnings.append(f"Skipped provider #{i + 1} in {path}: bad or missing name {name!r}.")
            continue
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            warnings.append(f"Skipped provider {name} in {path}: base_url must start with http:// or https://.")
            continue
        if secret is not None and (not isinstance(secret, str) or not SECRET_NAME_RE.match(secret)):
            warnings.append(f"Skipped provider {name} in {path}: bad secret name.")
            continue
        providers[name] = {"name": name, "base_url": url.rstrip("/"), "secret": secret,
                           "local": bool(p.get("local", False))}
    for w in warnings:
        log.warning(w)
    return list(providers.values()), warnings


def load_providers(path: str | None = None) -> list[dict]:
    """Like load_providers_with_warnings, but returns only the providers (warnings are logged)."""
    return load_providers_with_warnings(path)[0]


def is_ollama_cloud(model: str) -> bool:
    """Ollama sends models like "gpt-oss:120b-cloud" or "glm-4.6:cloud" to ollama.com."""
    name = (model or "").strip().lower()
    return name.endswith("-cloud") or name.endswith(":cloud")


class Gateway:
    def __init__(self, secrets: SecretStore, db_path: str, providers: list[dict] | None = None,
                 transport: "httpx.BaseTransport | None" = None, async_transport=None):
        self.secrets = secrets
        self.db_path = db_path
        self.providers = [dict(p, base_url=p["base_url"].rstrip("/")) for p in (providers or DEFAULT_PROVIDERS)]
        self._by_name = {p["name"]: p for p in self.providers}
        self.transport = transport
        self.async_transport = async_transport
        self._db_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._hits = {}
        _ensure_private_file(db_path)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS keys (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
                    last4 TEXT NOT NULL, rate_per_min INTEGER NOT NULL, allow_cloud INTEGER NOT NULL,
                    created INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS usage (
                    ts INTEGER NOT NULL, key_id TEXT, caller TEXT NOT NULL, provider TEXT NOT NULL,
                    model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
                    status INTEGER NOT NULL, ms INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
            """)

    def _db(self):
        return _Locked(self._db_lock, self.db_path)

    # Routing

    def route(self, model: str) -> tuple[dict, str]:
        if not isinstance(model, str) or not model.strip():
            raise LookupError("Request needs a \"model\", like \"qwen3:8b\" or \"openai/gpt-4o-mini\".")
        prefix, sep, rest = model.partition("/")
        if sep and prefix in self._by_name and rest:
            provider, name = self._by_name[prefix], rest
        else:
            provider, name = self._by_name.get("ollama"), model
            if provider is None:
                raise LookupError(f"No provider for model {model}. Use a provider prefix like openai/.")
        if provider.get("secret") and not self.secrets.get(provider["secret"]):
            raise LookupError(f"Provider {provider['name']} needs the secret {provider['secret']}. "
                              f"Add it in Forge Settings (gateway secrets) first.")
        return provider, name

    def is_local(self, model: str) -> bool:
        """True only if the model runs on this machine. Unknown or unusable models count as not local."""
        try:
            provider, name = self.route(model)
        except LookupError:
            return False
        return bool(provider.get("local")) and not is_ollama_cloud(name)

    def providers_status(self) -> list[dict]:
        return [{"name": p["name"], "base_url": p["base_url"], "local": bool(p.get("local")),
                 "ready": bool(p.get("local")) or not p.get("secret") or bool(self.secrets.get(p["secret"]))}
                for p in self.providers]

    def _headers(self, provider):
        headers = {"Content-Type": "application/json"}
        if provider.get("secret"):
            headers["Authorization"] = "Bearer " + (self.secrets.get(provider["secret"]) or "")
        return headers

    def _err(self, r, provider):
        """Provider error text with our secret blanked out, in case the provider echoes it."""
        text = _error_text(r)
        secret = self.secrets.get(provider["secret"]) if provider.get("secret") else None
        return text.replace(secret, "[hidden]") if secret else text

    def _leaks(self, r, provider):
        """True if a provider reply contains our secret, so it must not be passed through as is."""
        secret = self.secrets.get(provider["secret"]) if provider.get("secret") else None
        return bool(secret) and secret in r.text

    # Keys

    def create_key(self, name: str, rate_per_min: int = 60, allow_cloud: bool = False) -> dict:
        name = (name or "").strip() if isinstance(name, str) else ""
        if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
            raise ValueError("Key name must be 1 to 64 printable characters.")
        rate = int(rate_per_min)
        if rate < 1 or rate > 100000:
            raise ValueError("rate_per_min must be between 1 and 100000.")
        key = KEY_PREFIX + secrets.token_urlsafe(30)  # 30 bytes = 40 url-safe characters
        key_id = "k_" + secrets.token_hex(6)
        with self._db() as db:
            db.execute("INSERT INTO keys (id, name, hash, last4, rate_per_min, allow_cloud, created) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (key_id, name, _hash_key(key), key[-4:], rate, int(bool(allow_cloud)), int(time.time())))
        return {"id": key_id, "name": name, "key": key}

    def list_keys(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT id, name, rate_per_min, allow_cloud, created, last4, revoked "
                              "FROM keys ORDER BY created, id").fetchall()
        return [{"id": r[0], "name": r[1], "rate_per_min": r[2], "allow_cloud": bool(r[3]),
                 "created": r[4], "last4": r[5], "revoked": bool(r[6])} for r in rows]

    def revoke_key(self, key_id: str) -> bool:
        with self._db() as db:
            cur = db.execute("UPDATE keys SET revoked = 1 WHERE id = ? AND revoked = 0", (key_id,))
            changed = cur.rowcount > 0
        with self._rate_lock:
            self._hits.pop(key_id, None)
        return changed

    def check_key(self, key: str) -> dict:
        if not isinstance(key, str) or not key.startswith(KEY_PREFIX) or len(key) > 200:
            raise PermissionError("Missing or wrong gateway key. Create one in Forge Settings (AI gateway).")
        wanted = _hash_key(key)
        with self._db() as db:
            row = db.execute("SELECT id, name, hash, rate_per_min, allow_cloud, revoked FROM keys WHERE hash = ?",
                             (wanted,)).fetchone()
        # The lookup is by hash, so timing does not leak the key. Compare again in constant time anyway.
        if row is None or not secrets.compare_digest(row[2], wanted):
            raise PermissionError("Missing or wrong gateway key. Create one in Forge Settings (AI gateway).")
        if row[5]:
            raise PermissionError("This gateway key was revoked.")
        return {"id": row[0], "name": row[1], "rate_per_min": row[3], "allow_cloud": bool(row[4])}

    def check_rate(self, key_id: str, rate_per_min: int):
        now = time.monotonic()
        with self._rate_lock:
            hits = self._hits.setdefault(key_id, deque())
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= rate_per_min:
                wait = int(hits[0] + 60 - now) + 1
                raise OverflowError(f"Rate limit of {rate_per_min} requests per minute reached. Try again in {wait}s.")
            hits.append(now)

    # Usage

    def record_usage(self, key_id, caller, provider, model, prompt_tokens, completion_tokens, status, ms):
        with self._db() as db:
            db.execute("INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (int(time.time()), key_id, str(caller)[:64], str(provider)[:64], str(model)[:200],
                        _int(prompt_tokens), _int(completion_tokens), int(status), _int(ms)))

    def usage(self, since_s: int = 86400, group_by: str = "model") -> list[dict]:
        if group_by not in GROUP_COLUMNS:
            raise ValueError(f"group_by must be one of: {', '.join(GROUP_COLUMNS)}")
        col = GROUP_COLUMNS[group_by]
        with self._db() as db:
            rows = db.execute(
                f"SELECT {col}, COUNT(*), SUM(status >= 400), SUM(prompt_tokens), SUM(completion_tokens), "
                f"AVG(ms) FROM usage WHERE ts >= ? GROUP BY {col} ORDER BY COUNT(*) DESC",
                (int(time.time()) - int(since_s),)).fetchall()
        return [{group_by: r[0], "requests": r[1], "errors": r[2] or 0, "prompt_tokens": r[3] or 0,
                 "completion_tokens": r[4] or 0, "total_tokens": (r[3] or 0) + (r[4] or 0),
                 "avg_ms": int(r[5] or 0)} for r in rows]

    def prune(self, keep_days: int = 90) -> int:
        """Delete usage rows older than keep_days. Uses the ts index, so it is cheap to run daily."""
        cutoff = int(time.time()) - max(0, int(keep_days)) * 86400
        with self._db() as db:
            return db.execute("DELETE FROM usage WHERE ts < ?", (cutoff,)).rowcount

    # Internal calls from Forge agents

    def chat(self, payload: dict, caller: str = "internal") -> dict:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        provider, name = self.route(payload.get("model"))
        body = dict(payload, model=name, stream=False)
        start = time.monotonic()
        status, data = 0, None
        try:
            with httpx.Client(transport=self.transport, timeout=TIMEOUT) as client:
                r = client.post(provider["base_url"] + "/chat/completions", json=body, headers=self._headers(provider))
            status = r.status_code
            if r.status_code >= 400:
                raise RuntimeError(f"{provider['name']} answered with error {r.status_code}: {self._err(r, provider)}")
            try:
                data = r.json()
            except ValueError:
                status = 502
                raise RuntimeError(f"{provider['name']} sent a reply that is not JSON.")
            return data
        except httpx.TimeoutException:
            status = 504
            raise RuntimeError(f"{provider['name']} did not answer in time.")
        except httpx.RequestError:
            status = 502
            raise RuntimeError(f"Could not reach {provider['name']} at {provider['base_url']}. Is it running?")
        finally:
            p, c = _tokens(data)
            self.record_usage(None, caller, provider["name"], name, p, c, status or 502,
                              int((time.monotonic() - start) * 1000))


class _Locked:
    """A fresh SQLite connection used while holding the gateway's lock."""

    def __init__(self, lock, path):
        self.lock, self.path, self.conn = lock, path, None

    def __enter__(self):
        self.lock.acquire()
        try:
            self.conn = sqlite3.connect(self.path, timeout=10)
        except Exception:
            self.lock.release()
            raise
        return self.conn

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self.conn.commit()
            self.conn.close()
        finally:
            self.lock.release()


def _error_text(r):
    """A short provider error message. Provider replies never contain our secrets."""
    try:
        data = r.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("message", ""))[:300]
        if err:
            return str(err)[:300]
    except ValueError:
        pass
    return r.text[:300]


def _oai_error(status, message, kind="invalid_request_error"):
    return JSONResponse({"error": {"message": message, "type": kind, "code": status}}, status_code=status)


def make_v1_router(gw: Gateway) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["gateway"])

    def authorize(request):
        """Check the gateway key and rate. Returns (key, None) or (None, error response)."""
        header = request.headers.get("authorization", "")
        given = header[7:].strip() if header.startswith("Bearer ") else ""
        try:
            key = gw.check_key(given)
        except PermissionError as e:
            return None, _oai_error(401, str(e), "authentication_error")
        try:
            gw.check_rate(key["id"], key["rate_per_min"])
        except OverflowError as e:
            return None, _oai_error(429, str(e), "rate_limit_error")
        return key, None

    async def prepare(request):
        """Auth, parse and route a POST body. Returns (key, provider, name, body, error)."""
        key, err = authorize(request)
        if err:
            return None, None, None, None, err
        try:
            body = await request.json()
        except ValueError:
            return None, None, None, None, _oai_error(400, "Request body must be JSON.")
        if not isinstance(body, dict):
            return None, None, None, None, _oai_error(400, "Request body must be a JSON object.")
        try:
            provider, name = gw.route(body.get("model"))
        except LookupError as e:
            return None, None, None, None, _oai_error(400, str(e))
        if not key["allow_cloud"] and (not provider.get("local") or is_ollama_cloud(name)):
            where = "Ollama cloud models" if provider.get("local") else provider["name"]
            return None, None, None, None, _oai_error(
                403, f"This gateway key may only use local models, not {where}.", "permission_error")
        return key, provider, name, dict(body, model=name), None

    def client():
        return httpx.AsyncClient(transport=gw.async_transport, timeout=TIMEOUT)

    async def forward(request, path):
        """Non-streaming proxy for chat and embeddings."""
        key, provider, name, body, err = await prepare(request)
        if err:
            return err
        start = time.monotonic()
        status, data, out = 502, None, None
        try:
            async with client() as c:
                r = await c.post(provider["base_url"] + path, json=body, headers=gw._headers(provider))
            status = r.status_code
            if r.status_code >= 500:
                out = _oai_error(502, f"{provider['name']} failed with error {r.status_code}: {gw._err(r, provider)}",
                                 "provider_error")
            elif r.status_code >= 400 and gw._leaks(r, provider):
                out = _oai_error(r.status_code, gw._err(r, provider), "provider_error")
            else:
                try:
                    data = r.json()
                except ValueError:
                    data = None
                if data is None:
                    status = 502
                    out = _oai_error(502, f"{provider['name']} sent a reply that is not JSON.", "provider_error")
                else:
                    # Pass provider answers (and its 4xx errors, like unknown model) straight through.
                    out = JSONResponse(data, status_code=r.status_code)
        except httpx.TimeoutException:
            status = 504
            out = _oai_error(504, f"{provider['name']} did not answer in time.", "provider_error")
        except httpx.RequestError:
            out = _oai_error(502, f"Could not reach {provider['name']} at {provider['base_url']}. Is it running?",
                             "provider_error")
        p, c_tokens = _tokens(data)
        gw.record_usage(key["id"], key["name"], provider["name"], name, p, c_tokens, status,
                        int((time.monotonic() - start) * 1000))
        return out

    @router.get("/models")
    async def models(request: Request):
        key, err = authorize(request)
        if err:
            return err
        data = []
        ollama = gw._by_name.get("ollama")
        if ollama:
            try:
                async with httpx.AsyncClient(transport=gw.async_transport, timeout=httpx.Timeout(5.0)) as c:
                    r = await c.get(ollama["base_url"] + "/models")
                if r.status_code == 200:
                    items = r.json().get("data", [])
                    data += [m for m in items if isinstance(m, dict) and m.get("id")
                             and (key["allow_cloud"] or not is_ollama_cloud(str(m["id"])))]
            except (httpx.HTTPError, ValueError, AttributeError):
                pass  # Ollama is not running, list cloud placeholders only
        if key["allow_cloud"]:
            for p in gw.providers_status():
                if not p["local"] and p["ready"]:
                    data.append({"id": p["name"] + "/*", "object": "model", "owned_by": p["name"]})
        return {"object": "list", "data": data}

    @router.post("/chat/completions")
    async def chat_completions(request: Request):
        # Peek at the body to decide between streaming and a plain reply.
        try:
            stream = bool((await request.json()).get("stream"))
        except (ValueError, AttributeError):
            stream = False
        if not stream:
            return await forward(request, "/chat/completions")
        key, provider, name, body, err = await prepare(request)
        if err:
            return err
        start = time.monotonic()
        c = client()
        try:
            req = c.build_request("POST", provider["base_url"] + "/chat/completions", json=body,
                                  headers=gw._headers(provider))
            r = await c.send(req, stream=True)
        except httpx.TimeoutException:
            await c.aclose()
            gw.record_usage(key["id"], key["name"], provider["name"], name, 0, 0, 504,
                            int((time.monotonic() - start) * 1000))
            return _oai_error(504, f"{provider['name']} did not answer in time.", "provider_error")
        except httpx.RequestError:
            await c.aclose()
            gw.record_usage(key["id"], key["name"], provider["name"], name, 0, 0, 502,
                            int((time.monotonic() - start) * 1000))
            return _oai_error(502, f"Could not reach {provider['name']} at {provider['base_url']}. Is it running?",
                              "provider_error")
        if r.status_code >= 400:
            await r.aread()
            await r.aclose()
            await c.aclose()
            gw.record_usage(key["id"], key["name"], provider["name"], name, 0, 0, r.status_code,
                            int((time.monotonic() - start) * 1000))
            if r.status_code >= 500:
                return _oai_error(502, f"{provider['name']} failed with error {r.status_code}: {gw._err(r, provider)}",
                                  "provider_error")
            if gw._leaks(r, provider):
                return _oai_error(r.status_code, gw._err(r, provider), "provider_error")
            return Response(r.content, status_code=r.status_code,
                            media_type=r.headers.get("content-type", "application/json"))

        async def passthrough():
            tokens = [0, 0]
            tail = b""
            status = r.status_code
            try:
                async for chunk in r.aiter_bytes():
                    yield chunk
                    # Look for a usage block in finished SSE lines (sent when the client asks for it).
                    tail += chunk
                    lines = tail.split(b"\n")
                    tail = lines.pop()[-65536:]
                    for line in lines:
                        if line.startswith(b"data:") and b'"usage"' in line:
                            try:
                                p, ct = _tokens(json.loads(line[5:].strip()))
                            except ValueError:
                                continue
                            if p or ct:
                                tokens = [p, ct]
            except httpx.HTTPError:
                status = 502
            finally:
                await r.aclose()
                await c.aclose()
                gw.record_usage(key["id"], key["name"], provider["name"], name, tokens[0], tokens[1], status,
                                int((time.monotonic() - start) * 1000))

        return StreamingResponse(passthrough(), status_code=r.status_code,
                                 media_type=r.headers.get("content-type", "text/event-stream"))

    @router.post("/embeddings")
    async def embeddings(request: Request):
        return await forward(request, "/embeddings")

    return router


class KeyIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    rate_per_min: int = Field(60, ge=1, le=100000)
    allow_cloud: bool = False


def make_admin_router(gw: Gateway, auth_dep) -> APIRouter:
    router = APIRouter(prefix="/api/gateway", tags=["gateway"], dependencies=[Depends(auth_dep)])

    @router.get("/providers")
    def providers():
        return gw.providers_status()

    @router.get("/keys")
    def keys():
        return gw.list_keys()

    @router.post("/keys")
    def new_key(body: KeyIn):
        """Create a gateway key. The full key is in this reply only, so copy it now."""
        try:
            return gw.create_key(body.name, body.rate_per_min, body.allow_cloud)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.delete("/keys/{key_id}")
    def delete_key(key_id: str):
        if not gw.revoke_key(key_id):
            raise HTTPException(404, f"No active key with id {key_id}")
        return {"ok": True}

    @router.get("/usage")
    def usage(since: int = Query(86400, ge=60, le=365 * 86400, description="Seconds back"),
              group_by: str = Query("model", description="model, provider, key or caller")):
        try:
            return gw.usage(since, group_by)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.get("/secrets")
    def list_secrets():
        return gw.secrets.list()

    @router.put("/secrets/{name}")
    def put_secret(name: str, value: str = Body(..., embed=True)):
        try:
            gw.secrets.set(name, value)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True}

    @router.delete("/secrets/{name}")
    def delete_secret(name: str):
        if not gw.secrets.delete(name):
            raise HTTPException(404, f"No secret named {name}")
        return {"ok": True}

    return router
