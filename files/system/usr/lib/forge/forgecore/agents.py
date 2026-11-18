"""AI agents for Forge OS.

An agent is a saved spec (model, prompt, allowed tools) that runs a simple
loop: ask the model, run the tools it asks for, repeat until it answers.
Every tool call goes through the policy check first. Agents never run as
root and can only touch the world through the tools listed here.
Standard library plus FastAPI only.
"""

# Needed because the method named "list" hides the builtin in type hints.
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import uuid

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
LIFECYCLES = ("on-demand", "persistent")
CLOUD_PREFIXES = {"openai", "anthropic", "openrouter"}
READ_MAX = 200 * 1024
WRITE_MAX = 1024 * 1024
WAIT_CHUNK_S = 2.0
SUMMARY_LEN = 200
MAX_TOOL_CALLS_PER_STEP = 8
TOOL_RESULT_MAX = 20000
CONVERSATION_MAX = 400000
RESULT_MAX = 20000
DAY_S = 86400

# name -> (policy action, description, JSON schema properties, required args)
BUILTIN_TOOLS = {
    "get_metrics": ("metrics.read", "Read current system stats (CPU, RAM, GPU, disk).", {}, []),
    "list_models": ("models.list", "List the AI models installed on this machine.", {}, []),
    "get_setting": ("settings.read", "Read one Forge OS setting.",
                    {"key": {"type": "string", "description": "Setting key, for example ai.keep_alive_min"}},
                    ["key"]),
    "set_setting": ("settings.write", "Change one Forge OS setting.",
                    {"key": {"type": "string", "description": "Setting key"},
                     "value": {"description": "New value"}},
                    ["key", "value"]),
    "notify": ("notify", "Show a desktop notification to the user.",
               {"message": {"type": "string", "description": "Text to show"}}, ["message"]),
    "fetch_url": ("web.fetch", "Download a web page (http or https only).",
                  {"url": {"type": "string", "description": "http or https URL"}}, ["url"]),
    "read_file": ("files.read", "Read a text file from the agent workspace (max 200 KB).",
                  {"path": {"type": "string", "description": "Path relative to the workspace"}}, ["path"]),
    "write_file": ("files.write", "Write a text file in the agent workspace (max 1 MB).",
                   {"path": {"type": "string", "description": "Path relative to the workspace"},
                    "content": {"type": "string", "description": "File content"}},
                   ["path", "content"]),
    "run_command": ("command.run", "Run a shell command in the agent workspace.",
                    {"command": {"type": "string", "description": "Command to run"}}, ["command"]),
}
FILE_TOOLS = ("read_file", "write_file")

DEFAULTS = {
    "description": "",
    "system_prompt": "",
    "tools": [],
    "lifecycle": "on-demand",
    "interval_min": None,
    "max_steps": 10,
    "approval_timeout_s": 600,
    "enabled": True,
}
SPEC_KEYS = ("id", "name", "description", "model", "system_prompt", "tools", "lifecycle",
             "interval_min", "max_steps", "approval_timeout_s", "enabled")
# Keys we accept but ignore (they are computed, not stored).
IGNORED_KEYS = ("source", "workspace")


class AgentDisabled(Exception):
    """The agent exists but is turned off."""


def is_cloud_model(model: str) -> bool:
    return "/" in model and model.split("/", 1)[0].lower() in CLOUD_PREFIXES


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_spec(spec: dict) -> dict:
    """Check a spec and fill in defaults. Raises ValueError with a friendly message."""
    if not isinstance(spec, dict):
        raise ValueError("The agent spec must be a JSON object.")
    unknown = [k for k in spec if k not in SPEC_KEYS and k not in IGNORED_KEYS]
    if unknown:
        raise ValueError(f"Unknown field(s): {', '.join(sorted(unknown))}")
    out = dict(DEFAULTS)
    out.update({k: v for k, v in spec.items() if k in SPEC_KEYS})

    aid = out.get("id")
    if not isinstance(aid, str) or not ID_RE.match(aid):
        raise ValueError("The id must be 2 to 41 characters: lowercase letters, digits and dashes, "
                         "starting with a letter or digit.")
    name = out.get("name", aid)
    if name is None:
        name = aid
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError("The name must be text of 1 to 100 characters.")
    out["name"] = name.strip()
    for key in ("description", "system_prompt"):
        if out[key] is None:
            out[key] = ""
        if not isinstance(out[key], str):
            raise ValueError(f"The {key} must be text.")
    model = out.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Pick a model for the agent, for example qwen3:8b.")
    out["model"] = model.strip()

    tools = out["tools"]
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise ValueError("The tools must be a list of tool names.")
    bad = [t for t in tools if t not in BUILTIN_TOOLS]
    if bad:
        raise ValueError(f"Unknown tool(s): {', '.join(bad)}. Known tools: {', '.join(BUILTIN_TOOLS)}")
    out["tools"] = list(dict.fromkeys(tools))

    if out["lifecycle"] not in LIFECYCLES:
        raise ValueError('The lifecycle must be "on-demand" or "persistent".')
    iv = out["interval_min"]
    if iv is not None and (not _is_int(iv) or iv < 5):
        raise ValueError("The interval must be a whole number of minutes, at least 5, or empty.")
    ms = out["max_steps"]
    if not _is_int(ms) or not 1 <= ms <= 50:
        raise ValueError("The max steps must be a whole number from 1 to 50.")
    at = out["approval_timeout_s"]
    if not _is_int(at) or not 1 <= at <= 86400:
        raise ValueError("The approval timeout must be a whole number of seconds from 1 to 86400.")
    if not isinstance(out["enabled"], bool):
        raise ValueError("Enabled must be true or false.")
    return {k: out[k] for k in SPEC_KEYS}


class AgentStore:
    """Agent specs. System specs ship in the image (read only), user specs are editable.
    A user spec with the same id replaces the system one."""

    def __init__(self, system_dir: str, user_dir: str):
        self.system_dir = system_dir
        self.user_dir = user_dir
        self._lock = threading.Lock()

    def _load_dir(self, path: str, source: str) -> dict:
        out = {}
        try:
            names = sorted(os.listdir(path))
        except OSError:
            return out
        for fn in names:
            if not fn.endswith(".json") or not ID_RE.match(fn[:-5]):
                continue
            try:
                with open(os.path.join(path, fn)) as f:
                    spec = validate_spec(json.load(f))
            except (OSError, ValueError):
                # A broken file should not hide the other agents.
                continue
            if spec["id"] != fn[:-5]:
                continue
            out[spec["id"]] = dict(spec, source=source)
        return out

    def _all(self) -> dict:
        specs = self._load_dir(self.system_dir, "system")
        specs.update(self._load_dir(self.user_dir, "user"))
        return specs

    def list(self) -> list[dict]:
        with self._lock:
            specs = self._all()
        return [specs[k] for k in sorted(specs)]

    def get(self, agent_id) -> dict | None:
        if not isinstance(agent_id, str) or not ID_RE.match(agent_id):
            return None
        with self._lock:
            return self._all().get(agent_id)

    def is_system(self, agent_id) -> bool:
        return os.path.isfile(os.path.join(self.system_dir, f"{agent_id}.json"))

    def save(self, spec: dict) -> dict:
        clean = validate_spec(spec)
        with self._lock:
            os.makedirs(self.user_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=self.user_dir)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(clean, f, indent=2)
                os.replace(tmp, os.path.join(self.user_dir, f"{clean['id']}.json"))
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        return dict(clean, source="user")

    def delete(self, agent_id) -> bool:
        if not isinstance(agent_id, str) or not ID_RE.match(agent_id):
            return False
        with self._lock:
            path = os.path.join(self.user_dir, f"{agent_id}.json")
            if os.path.isfile(path):
                os.unlink(path)
                return True
        if self.is_system(agent_id):
            raise PermissionError("Built in agents cannot be deleted. Turn it off instead.")
        return False


class Tools:
    """Registry of tools agents can use. File tools live here, the rest come from handlers."""

    def __init__(self, handlers: dict, workspace_root: str):
        self.handlers = dict(handlers or {})
        self.workspace_root = workspace_root

    def available(self) -> list[str]:
        return [n for n in BUILTIN_TOOLS if n in FILE_TOOLS or callable(self.handlers.get(n))]

    def action_for(self, name: str) -> str:
        return BUILTIN_TOOLS[name][0] if name in BUILTIN_TOOLS else "tool.unknown"

    def describe(self) -> list[dict]:
        return [{"name": n, "action": BUILTIN_TOOLS[n][0], "description": BUILTIN_TOOLS[n][1]}
                for n in self.available()]

    def schemas(self, names: list[str]) -> list[dict]:
        avail = set(self.available())
        out = []
        for n in dict.fromkeys(names or []):
            if n not in avail:
                continue
            _, desc, props, req = BUILTIN_TOOLS[n]
            out.append({"type": "function", "function": {
                "name": n, "description": desc,
                "parameters": {"type": "object", "properties": props, "required": req}}})
        return out

    def workspace_for(self, agent: dict) -> str:
        # Always derived from the id, never taken from the spec.
        return os.path.join(self.workspace_root, agent["id"])

    def _safe_path(self, agent: dict, path) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non empty string")
        if "\x00" in path:
            raise ValueError("path contains a null byte")
        if os.path.isabs(path) or path.startswith("~"):
            raise ValueError("path must be relative to the workspace")
        if ".." in path.replace("\\", "/").split("/"):
            raise ValueError("path must not contain ..")
        ws = self.workspace_for(agent)
        os.makedirs(ws, exist_ok=True)
        real_ws = os.path.realpath(ws)
        full = os.path.realpath(os.path.join(real_ws, path))
        if full == real_ws or not full.startswith(real_ws + os.sep):
            raise ValueError("path is outside the workspace")
        return full

    def _read_file(self, agent, args):
        full = self._safe_path(agent, args.get("path"))
        if not os.path.isfile(full):
            raise ValueError("file not found")
        if os.path.getsize(full) > READ_MAX:
            raise ValueError(f"file is larger than {READ_MAX // 1024} KB")
        with open(full, "rb") as f:
            data = f.read(READ_MAX + 1)
        if len(data) > READ_MAX:
            raise ValueError(f"file is larger than {READ_MAX // 1024} KB")
        return data.decode("utf-8", errors="replace")

    def _write_file(self, agent, args):
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        data = content.encode("utf-8")
        if len(data) > WRITE_MAX:
            raise ValueError(f"content is larger than {WRITE_MAX // 1024 // 1024} MB")
        full = self._safe_path(agent, args.get("path"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        # Check again after making folders, then refuse to follow a symlink at the end.
        full = self._safe_path(agent, args.get("path"))
        fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return {"ok": True, "bytes": len(data)}

    def call(self, name: str, args: dict, agent: dict) -> str:
        try:
            if name not in self.available():
                return f"Error: unknown or unavailable tool {name}"
            if not isinstance(args, dict):
                return "Error: arguments must be a JSON object"
            props, req = BUILTIN_TOOLS[name][2], BUILTIN_TOOLS[name][3]
            missing = [r for r in req if r not in args]
            if missing:
                return f"Error: missing argument(s): {', '.join(missing)}"
            clean = {k: v for k, v in args.items() if k in props}
            if name == "read_file":
                result = self._read_file(agent, clean)
            elif name == "write_file":
                result = self._write_file(agent, clean)
            else:
                if name == "fetch_url":
                    url = clean.get("url")
                    parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
                    if not parts or parts.scheme.lower() not in ("http", "https") or not parts.netloc:
                        return "Error: only http and https URLs are allowed"
                result = self.handlers[name](**clean)
            if isinstance(result, str):
                return result
            return json.dumps(result, default=str)
        except Exception as e:  # tools never raise into the run loop
            return f"Error: {e}"


def _short(text, n=SUMMARY_LEN) -> str:
    text = "" if text is None else str(text)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


class Runner:
    """Runs agents and keeps a history of runs in SQLite."""

    ACTIVE = ("queued", "running", "waiting_approval")

    def __init__(self, store: AgentStore, tools: Tools, policy, chat, db_path: str, max_parallel: int = 2,
                 is_local=None):
        self.store = store
        # is_local(model) -> bool, from the gateway. Without it we guess from the model name.
        self.is_local = is_local
        self.tools = tools
        self.policy = policy
        self.chat = chat
        self._sem = threading.BoundedSemaphore(max(1, int(max_parallel)))
        self._lock = threading.Lock()
        self._cancel = set()
        self._sched_stop = threading.Event()
        self._sched_thread = None
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, agent TEXT, task TEXT, trigger TEXT, status TEXT,
                steps TEXT, result TEXT, error TEXT, started REAL, ended REAL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS runs_agent ON runs(agent, started)")
            self._db.execute("CREATE TABLE IF NOT EXISTS schedule (agent TEXT PRIMARY KEY, last REAL)")
            # Runs cut off by a restart can never finish.
            self._db.execute("UPDATE runs SET status='failed', error='Forge Core restarted', ended=? "
                             "WHERE status IN ('queued','running','waiting_approval')", (time.time(),))
            self._db.commit()

    # Database helpers

    def _row(self, r) -> dict:
        return {"id": r[0], "agent": r[1], "task": r[2], "trigger": r[3], "status": r[4],
                "steps": json.loads(r[5] or "[]"), "result": r[6], "error": r[7],
                "started": r[8], "ended": r[9]}

    def _update(self, run_id, **fields):
        if "steps" in fields:
            fields["steps"] = json.dumps(fields["steps"], default=str)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))
            self._db.commit()

    def get_run(self, run_id) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM runs WHERE id=?", (str(run_id),)).fetchone()
        return self._row(r) if r else None

    def list_runs(self, agent_id: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            if agent_id:
                rows = self._db.execute("SELECT * FROM runs WHERE agent=? ORDER BY started DESC LIMIT ?",
                                        (agent_id, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def _has_active(self, agent_id) -> bool:
        with self._lock:
            r = self._db.execute("SELECT 1 FROM runs WHERE agent=? AND status IN (?,?,?) LIMIT 1",
                                 (agent_id, *self.ACTIVE)).fetchone()
        return r is not None

    # Starting runs

    def _create(self, agent_id, task, trigger) -> tuple[str, dict]:
        spec = self.store.get(agent_id)
        if spec is None:
            raise KeyError(f"Unknown agent: {agent_id}")
        if not spec.get("enabled"):
            raise AgentDisabled(f"Agent {agent_id} is turned off.")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Give the agent a task.")
        run_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (run_id, agent_id, task, trigger, "queued", "[]", None, None, time.time(), None))
            self._db.commit()
        return run_id, spec

    def start_run(self, agent_id: str, task: str, trigger: str = "manual") -> str:
        run_id, spec = self._create(agent_id, task, trigger)
        threading.Thread(target=self._execute, args=(run_id, spec, task), daemon=True,
                         name=f"agent-{agent_id}").start()
        return run_id

    def run_sync(self, agent_id: str, task: str, trigger: str = "manual") -> dict:
        run_id, spec = self._create(agent_id, task, trigger)
        self._execute(run_id, spec, task)
        return self.get_run(run_id)

    def cancel(self, run_id) -> bool:
        run = self.get_run(run_id)
        if not run or run["status"] not in self.ACTIVE:
            return False
        with self._lock:
            self._cancel.add(run_id)
        return True

    def _cancelled(self, run_id) -> bool:
        with self._lock:
            return run_id in self._cancel

    def _finish(self, run_id, status, steps, result=None, error=None, agent_id=None):
        if isinstance(result, str) and len(result) > RESULT_MAX:
            result = result[:RESULT_MAX] + "\n[truncated]"
        self._update(run_id, status=status, steps=steps, result=result, error=error, ended=time.time())
        with self._lock:
            self._cancel.discard(run_id)
        try:
            self.policy.audit(f"agent:{agent_id}", "agent.run", status,
                              details={"run_id": run_id}, result=_short(error or result))
        except Exception:
            pass

    def _execute(self, run_id, spec, task):
        steps = []
        if self._cancelled(run_id):
            self._finish(run_id, "cancelled", steps, error="Cancelled", agent_id=spec["id"])
            return
        with self._sem:
            if self._cancelled(run_id):
                self._finish(run_id, "cancelled", steps, error="Cancelled", agent_id=spec["id"])
                return
            self._update(run_id, status="running")
            try:
                status, result, error = self._loop(run_id, spec, task, steps)
            except Exception as e:
                status, result, error = "failed", None, f"{type(e).__name__}: {e}"
            self._finish(run_id, status, steps, result, error, agent_id=spec["id"])

    # Policy helpers

    def _needs_cloud_ok(self, model) -> bool:
        if self.is_local is not None:
            try:
                return not self.is_local(model)
            except Exception:
                return True  # unsure means ask
        return is_cloud_model(model)

    def _check(self, agent_id, action, details, run_id, ttl_s) -> tuple[str, str | None]:
        try:
            r = self.policy.check(agent_id, action, details, run_id=run_id, ttl_s=ttl_s) or {}
            decision = r.get("decision")
            return (decision if decision in ("allow", "ask", "deny") else "deny"), r.get("approval_id")
        except Exception:
            # If the policy cannot answer, the safe answer is no.
            return "deny", None

    def _wait(self, run_id, approval_id, timeout_s) -> str:
        """Wait for the user, in short chunks so cancel still works. Returns approved, denied or expired."""
        if not approval_id:
            return "expired"
        self._update(run_id, status="waiting_approval")
        deadline = time.monotonic() + timeout_s
        answer, reason = "expired", "timeout"
        try:
            while True:
                left = deadline - time.monotonic()
                if self._cancelled(run_id):
                    reason = "cancelled"
                    break
                if left <= 0:
                    break
                try:
                    r = self.policy.wait(approval_id, min(WAIT_CHUNK_S, left))
                except Exception:
                    reason = "error"
                    break
                if r in ("approved", "denied", "expired"):
                    answer = r
                    break
        except BaseException:
            reason = "error"
            raise
        finally:
            if answer not in ("approved", "denied"):
                # Tell the policy we stopped waiting, so the request does not hang around.
                expire = getattr(self.policy, "expire", None)
                if callable(expire):
                    try:
                        expire(approval_id, reason)
                    except Exception:
                        pass
            self._update(run_id, status="running")
        return answer

    def _gate(self, run_id, agent_id, action, details, timeout_s) -> tuple[bool, str, str | None]:
        """Ask the policy. Returns (allowed, decision label, message if blocked)."""
        decision, approval_id = self._check(agent_id, action, details, run_id, timeout_s)
        if decision == "allow":
            return True, "allow", None
        if decision == "deny":
            return False, "deny", "Not allowed by policy"
        answer = self._wait(run_id, approval_id, timeout_s)
        if answer == "approved":
            return True, "ask:approved", None
        if answer == "denied":
            return False, "ask:denied", "Denied by the user"
        return False, "ask:expired", "No answer in time"

    # The agent loop

    def _loop(self, run_id, spec, task, steps):
        agent_id, model = spec["id"], spec["model"]
        timeout_s = spec["approval_timeout_s"]

        if self._needs_cloud_ok(model):
            ok, label, msg = self._gate(run_id, agent_id, "ai.cloud", {"model": model}, timeout_s)
            steps.append({"type": "policy", "name": "ai.cloud", "summary": f"Use cloud model {model}",
                          "decision": label, "ts": time.time()})
            self._update(run_id, steps=steps)
            if not ok:
                return "failed", None, f"Cloud model {model} not allowed: {msg}"

        messages = []
        if spec.get("system_prompt"):
            messages.append({"role": "system", "content": spec["system_prompt"]})
        messages.append({"role": "user", "content": task})
        tool_schemas = self.tools.schemas(spec.get("tools", []))
        offered = {t["function"]["name"] for t in tool_schemas}
        last_text = ""

        for _ in range(spec["max_steps"]):
            if self._cancelled(run_id):
                return "cancelled", None, "Cancelled"
            payload = {"model": model, "messages": messages}
            if tool_schemas:
                payload["tools"] = tool_schemas
            response = self.chat(payload, caller=f"agent:{agent_id}")
            try:
                msg = dict(response["choices"][0]["message"])
            except (KeyError, IndexError, TypeError):
                return "failed", None, "The model returned an answer Forge could not read."
            msg["role"] = "assistant"
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            content = msg.get("content") or ""
            if content:
                last_text = content
            names = [((c.get("function") or {}).get("name") or "?") for c in calls]
            steps.append({"type": "model", "name": model, "decision": None, "ts": time.time(),
                          "summary": _short(content) if not calls else "Asked for tools: " + ", ".join(names)})
            if not calls:
                return "done", content, None

            for n, call in enumerate(calls):
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                call_id = call.get("id") or uuid.uuid4().hex
                raw = fn.get("arguments") or "{}"
                decision = None
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    args = None
                if n >= MAX_TOOL_CALLS_PER_STEP:
                    result = "Skipped: too many tool calls in one step"
                elif self._cancelled(run_id):
                    result = "Cancelled"
                elif name not in offered:
                    result = f"Error: tool {name} is not available to this agent"
                elif not isinstance(args, dict):
                    result = "Error: tool arguments are not a valid JSON object"
                else:
                    action = self.tools.action_for(name)
                    ok, decision, blocked = self._gate(run_id, agent_id, action,
                                                       {"tool": name, "args": args}, timeout_s)
                    result = self.tools.call(name, args, spec) if ok else blocked
                    try:
                        self.policy.audit(f"agent:{agent_id}", action, decision,
                                          details={"tool": name, "run_id": run_id}, result=_short(result))
                    except Exception:
                        pass
                if not isinstance(result, str):
                    result = str(result)
                if len(result) > TOOL_RESULT_MAX:
                    result = result[:TOOL_RESULT_MAX] + "\n[truncated]"
                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                steps.append({"type": "tool", "name": name, "decision": decision, "ts": time.time(),
                              "summary": _short(f"{raw} -> {result}")})
            # One write per model step keeps the database quiet.
            self._update(run_id, steps=steps)
            if self._cancelled(run_id):
                return "cancelled", None, "Cancelled"
            if len(json.dumps(messages, default=str)) > CONVERSATION_MAX:
                note = "Stopped because the conversation got too long."
                return "done", (f"{note}\n\n{last_text}" if last_text else note), None

        note = f"Stopped after {spec['max_steps']} steps without a final answer."
        return "done", (f"{note}\n\n{last_text}" if last_text else note), None

    # Scheduling

    def tick(self, now: float | None = None) -> list[str]:
        """Start runs for enabled agents with an interval that are due. Returns started run ids."""
        now = time.time() if now is None else now
        started = []
        for spec in self.store.list():
            iv = spec.get("interval_min")
            if not spec.get("enabled") or not iv:
                continue
            with self._lock:
                r = self._db.execute("SELECT last FROM schedule WHERE agent=?", (spec["id"],)).fetchone()
            last = r[0] if r else None
            if last is not None and now - last < iv * 60:
                continue
            if self._has_active(spec["id"]):
                continue
            try:
                run_id = self.start_run(spec["id"], "Scheduled check. Do your regular task.", "schedule")
            except (KeyError, AgentDisabled, ValueError):
                continue
            with self._lock:
                self._db.execute("INSERT OR REPLACE INTO schedule VALUES (?,?)", (spec["id"], now))
                self._db.commit()
            started.append(run_id)
        return started

    def prune(self, keep_days: int = 30, now: float | None = None) -> int:
        """Delete finished runs older than keep_days. Returns how many were deleted."""
        now = time.time() if now is None else now
        cutoff = now - keep_days * DAY_S
        with self._lock:
            cur = self._db.execute("DELETE FROM runs WHERE status NOT IN (?,?,?) AND COALESCE(ended, started) < ?",
                                   (*self.ACTIVE, cutoff))
            self._db.commit()
        return cur.rowcount

    def start_scheduler(self, every_s: float = 60, daily=None):
        if self._sched_thread and self._sched_thread.is_alive():
            return
        self._sched_stop.clear()
        daily_jobs = [self.prune] + list(daily or [])

        def loop():
            last_daily = None
            while not self._sched_stop.is_set():
                try:
                    self.tick()
                except Exception:
                    pass
                if last_daily is None or time.monotonic() - last_daily >= DAY_S:
                    last_daily = time.monotonic()
                    for job in daily_jobs:
                        try:
                            job()
                        except Exception:
                            pass
                self._sched_stop.wait(every_s)

        self._sched_thread = threading.Thread(target=loop, daemon=True, name="agent-scheduler")
        self._sched_thread.start()

    def stop_scheduler(self):
        self._sched_stop.set()
        if self._sched_thread:
            self._sched_thread.join(timeout=5)
            self._sched_thread = None


def make_router(store: AgentStore, runner: Runner, tools: Tools, auth_dep):
    """API routes for agents. Paths already start with /api."""
    from fastapi import APIRouter, Body, Depends, HTTPException, Query

    router = APIRouter(prefix="/api", tags=["agents"], dependencies=[Depends(auth_dep)])

    def with_workspace(spec):
        return dict(spec, workspace=tools.workspace_for(spec))

    @router.get("/agents")
    def list_agents():
        return [with_workspace(s) for s in store.list()]

    @router.get("/agents/{agent_id}")
    def get_agent(agent_id: str):
        spec = store.get(agent_id)
        if not spec:
            raise HTTPException(404, f"Unknown agent: {agent_id}")
        return with_workspace(spec)

    @router.put("/agents/{agent_id}")
    def put_agent(agent_id: str, spec: dict = Body(...)):
        """Create or change an agent. The body is the full spec and its id must match the URL."""
        if spec.get("id", agent_id) != agent_id:
            raise HTTPException(422, "The id in the body does not match the URL.")
        try:
            saved = store.save(dict(spec, id=agent_id))
        except ValueError as e:
            raise HTTPException(422, str(e))
        return with_workspace(saved)

    @router.delete("/agents/{agent_id}")
    def delete_agent(agent_id: str):
        try:
            ok = store.delete(agent_id)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        if not ok:
            raise HTTPException(404, f"Unknown agent: {agent_id}")
        return {"ok": True}

    @router.post("/agents/{agent_id}/run")
    def run_agent(agent_id: str, task: str = Body(..., embed=True, min_length=1, max_length=20000)):
        try:
            return {"run_id": runner.start_run(agent_id, task, "manual")}
        except KeyError:
            raise HTTPException(404, f"Unknown agent: {agent_id}")
        except AgentDisabled as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.get("/runs")
    def list_runs(agent: str | None = None, limit: int = Query(50, ge=1, le=500)):
        return runner.list_runs(agent, limit)

    @router.get("/runs/{run_id}")
    def get_run(run_id: str):
        run = runner.get_run(run_id)
        if not run:
            raise HTTPException(404, "Unknown run")
        return run

    @router.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        if not runner.get_run(run_id):
            raise HTTPException(404, "Unknown run")
        return {"ok": runner.cancel(run_id)}

    @router.get("/tools")
    def list_tools():
        return tools.describe()

    return router
