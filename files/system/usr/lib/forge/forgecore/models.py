"""Model library for Forge OS.

Talks to the local Ollama server over its HTTP API and checks whether a
model will fit on this machine's GPU. Standard library only.
"""

# Needed because the method named "list" hides the builtin in type hints.
from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:11434"


class OllamaError(Exception):
    """Any problem talking to Ollama. The message is safe to show to the user."""


def base_url_from_env() -> str:
    """Turn the OLLAMA_HOST env var into a usable http URL."""
    raw = (os.environ.get("OLLAMA_HOST") or "").strip()
    if not raw:
        return DEFAULT_URL
    if "://" not in raw:
        raw = "http://" + raw
    parts = urllib.parse.urlsplit(raw)
    scheme = parts.scheme or "http"
    host = parts.hostname or ""
    # 0.0.0.0 means "listen everywhere", but we must connect to a real address.
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port or 11434
    except ValueError:
        port = 11434
    return f"{scheme}://{host}:{port}"


# Letters, digits, dot, dash, underscore. Must start with a letter or digit.
_PART = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_NAME_RE = re.compile(rf"^{_PART}(/{_PART})*(:[A-Za-z0-9][A-Za-z0-9._-]*)?$")


def normalize_name(name: str) -> str:
    """Check a model name and return a cleaned copy. Raises ValueError if it is unsafe."""
    if not isinstance(name, str):
        raise ValueError("Model name must be text.")
    name = name.strip()
    if not name:
        raise ValueError("Please enter a model name.")
    if len(name) > 200:
        raise ValueError("That model name is too long (200 characters max).")
    if any(ch.isspace() for ch in name):
        raise ValueError("Model names cannot contain spaces.")
    if ".." in name:
        raise ValueError("Model names cannot contain '..'.")
    # Use the short Hugging Face host that Ollama expects.
    lower = name.lower()
    for prefix in ("https://huggingface.co/", "huggingface.co/", "https://hf.co/"):
        if lower.startswith(prefix):
            name = "hf.co/" + name[len(prefix):]
            break
    if not _NAME_RE.match(name):
        raise ValueError(
            "That does not look like a model name. Try something like 'qwen3:8b' "
            "or 'hf.co/user/Model-GGUF:Q4_K_M'."
        )
    return name


def _gb(num_bytes: float) -> float:
    return num_bytes / (1024 ** 3)


def fit(size_bytes: int, vram_total_mb: float | None, ram_total_gb: float | None,
        context_overhead: float = 1.2) -> dict:
    """Estimate where a model of this file size will run."""
    if not size_bytes or size_bytes <= 0 or (vram_total_mb is None and ram_total_gb is None):
        return {"verdict": "unknown", "need_gb": 0.0,
                "message": "Not sure if this model fits: model size or hardware info is missing."}

    need = _gb(size_bytes * context_overhead)
    need_gb = round(need, 1)
    # Leave 10% of VRAM free, and only use half the RAM so the system stays usable.
    vram_gb = vram_total_mb / 1024 if vram_total_mb is not None else None
    gpu_room = vram_gb * 0.9 if vram_gb is not None else 0.0
    ram_room = ram_total_gb * 0.5 if ram_total_gb is not None else 0.0

    if vram_gb is not None:
        if need <= gpu_room:
            verdict = "gpu"
            message = f"Runs fully on your GPU (needs about {need_gb} GB of {vram_gb:.0f} GB VRAM)."
        elif ram_total_gb is not None and need <= gpu_room + ram_room:
            verdict = "partial"
            message = (f"Runs partly on your GPU and partly on the CPU, so it will be slower "
                       f"(needs about {need_gb} GB, you have {vram_gb:.0f} GB VRAM).")
        else:
            verdict = "too_big"
            message = f"Too big for this PC (needs about {need_gb} GB)."
    else:
        if need <= ram_room:
            verdict = "cpu"
            message = (f"No GPU found, so it runs on the CPU and will be slow "
                       f"(needs about {need_gb} GB of {ram_total_gb:.0f} GB RAM).")
        else:
            verdict = "too_big"
            message = f"Too big for this PC (needs about {need_gb} GB)."

    return {"verdict": verdict, "need_gb": need_gb, "message": message}


class OllamaClient:
    """Small client for the Ollama HTTP API."""

    def __init__(self, base_url: str = DEFAULT_URL, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    # Shared request helper. Returns the raw response body as bytes.
    def _request(self, method: str, path: str, body: dict | None = None) -> bytes:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise OllamaError(_error_text(e.read(), e.code)) from None
        except (urllib.error.URLError, OSError):
            raise OllamaError(f"Ollama is not running (could not reach {self.base_url}).") from None

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
        raw = self._request(method, path, body)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise OllamaError("Ollama sent a reply we could not read.") from None

    def available(self) -> bool:
        try:
            self._request("GET", "/api/version")
            return True
        except OllamaError:
            return False

    def version(self) -> str:
        return str(self._json("GET", "/api/version").get("version", ""))

    def list(self) -> list[dict]:
        models = []
        for m in self._json("GET", "/api/tags").get("models") or []:
            details = m.get("details") or {}
            models.append({
                "name": m.get("name") or m.get("model", ""),
                "size_bytes": m.get("size", 0),
                "family": details.get("family", ""),
                "parameter_size": details.get("parameter_size", ""),
                "quantization": details.get("quantization_level", ""),
                "modified_at": m.get("modified_at", ""),
            })
        return models

    def loaded(self) -> list[dict]:
        models = []
        for m in self._json("GET", "/api/ps").get("models") or []:
            models.append({
                "name": m.get("name") or m.get("model", ""),
                "size_bytes": m.get("size", 0),
                "size_vram_bytes": m.get("size_vram", 0),
                "expires_at": m.get("expires_at", ""),
            })
        return models

    def show(self, name: str) -> dict:
        return self._json("POST", "/api/show", {"model": name})

    def pull(self, name: str, guard=None):
        """Download a model. Yields progress dicts as Ollama sends them.

        guard (optional, a forgecore.storage.PullGuard) sees every progress line. When it says
        "error" (the model does not fit in the room left for AI models) the download stops with
        that message and the connection is closed; a "warning" is passed on as {"warning": text}."""
        parts = urllib.parse.urlsplit(self.base_url)
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parts.hostname, parts.port, timeout=self.timeout_s)
        body = json.dumps({"model": name, "stream": True})
        try:
            try:
                conn.connect()
                # Downloads can take a long time, so no read timeout once connected.
                conn.sock.settimeout(None)
                conn.request("POST", "/api/pull", body=body,
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
            except OSError:
                raise OllamaError(f"Ollama is not running (could not reach {self.base_url}).") from None

            if resp.status >= 400:
                raise OllamaError(_error_text(resp.read(), resp.status))

            for line in resp:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "error" in item:
                    raise OllamaError(f"Download failed: {item['error']}")
                verdict = guard.observe(item) if guard is not None else None
                if verdict and verdict[0] == "error":
                    raise OllamaError(verdict[1])
                if verdict and verdict[0] == "warning":
                    yield {"warning": verdict[1]}
                total, done = item.get("total"), item.get("completed")
                if total and done is not None:
                    item["percent"] = max(0, min(100, int(done * 100 / total)))
                yield item
        finally:
            conn.close()

    def delete(self, name: str) -> None:
        self._request("DELETE", "/api/delete", {"model": name})

    def unload(self, name: str) -> None:
        # keep_alive 0 tells Ollama to free the model from memory right away.
        self._request("POST", "/api/generate", {"model": name, "keep_alive": 0})


def _error_text(raw: bytes, status: int) -> str:
    """Pull the "error" field out of an Ollama error reply if there is one."""
    try:
        msg = json.loads(raw).get("error")
    except (json.JSONDecodeError, AttributeError, TypeError):
        msg = None
    return f"Ollama error: {msg}" if msg else f"Ollama returned HTTP {status}."
