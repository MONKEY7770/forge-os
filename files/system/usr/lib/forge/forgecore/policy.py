"""Permission system for AI agents.

Every agent action gets one of three decisions: allow, ask or deny.
"ask" puts a request in an approval queue that a human answers.
Every decision and answer is written to an audit log.

Decisions come from ACTIONS (the defaults), then the "actions" overrides
in the config file, then the per-agent overrides. Deny always wins, and
an action that is not in ACTIONS is always denied.
"""
# Policy has a method called list, so type hints are kept as text.
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query

ACTIONS = {
    # action id: (description, default decision)
    "metrics.read": ("Read system stats", "allow"),
    "models.list": ("List AI models", "allow"),
    "settings.read": ("Read Forge settings", "allow"),
    "notify": ("Show a desktop notification", "allow"),
    "settings.write": ("Change a Forge setting", "ask"),
    "models.pull": ("Download an AI model", "ask"),
    "models.delete": ("Delete an AI model", "ask"),
    "web.fetch": ("Read a web page", "ask"),
    "ai.cloud": ("Use a paid cloud AI model", "ask"),
    "command.run": ("Run a shell command in a sandbox", "deny"),
    "files.read": ("Read files in the agent workspace", "allow"),
    "files.write": ("Write files in the agent workspace", "ask"),
}
DECISIONS = ("allow", "ask", "deny")
STATUSES = ("pending", "approved", "denied", "expired")

MAX_STR = 2000
HIDDEN = "[hidden]"
# Whole names that hold secrets. A plain "key" (like a setting name) stays visible.
SECRET_NAMES = {"password", "passwd", "secret", "token", "api_key", "apikey", "access_token",
                "refresh_token", "client_secret", "authorization", "private_key"}
SECRET_SUFFIXES = ("_secret", "_token", "_password", "_api_key")

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    decided_by TEXT,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status, created);
CREATE INDEX IF NOT EXISTS approvals_expires ON approvals(status, expires);
CREATE INDEX IF NOT EXISTS approvals_created ON approvals(created);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    decision TEXT NOT NULL,
    details TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
"""


def _is_secret(name):
    name = str(name).lower().replace("-", "_")
    return name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES)


def clean_details(value):
    """Hide secret-looking keys and cut long strings, at any depth."""
    if isinstance(value, dict):
        return {str(k): (HIDDEN if _is_secret(k) else clean_details(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_details(v) for v in value]
    if isinstance(value, str):
        return value[:MAX_STR]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Anything else (bytes, objects) is stored as short text.
    return str(value)[:MAX_STR]


def validate_config(config):
    """Check a config dict and return a clean copy. Raises ValueError."""
    if not isinstance(config, dict):
        raise ValueError("Config must be an object")
    # "error" is what get_config() reports for a broken file, so it is ignored here.
    unknown = set(config) - {"actions", "agents", "error"}
    if unknown:
        raise ValueError(f"Unknown config sections: {', '.join(sorted(unknown))}")

    def check_map(m, where):
        if not isinstance(m, dict):
            raise ValueError(f"{where} must be an object")
        for action, decision in m.items():
            if action not in ACTIONS:
                raise ValueError(f"Unknown action in {where}: {action}")
            if decision not in DECISIONS:
                raise ValueError(f"Bad decision for {action} in {where}: {decision!r} (use allow, ask or deny)")
        return dict(m)

    actions = check_map(config.get("actions", {}), "actions")
    agents_in = config.get("agents", {})
    if not isinstance(agents_in, dict):
        raise ValueError("agents must be an object")
    agents = {}
    for agent_id, m in agents_in.items():
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("Agent ids must be non-empty text")
        agents[agent_id] = check_map(m, f"agents.{agent_id}")
    return {"actions": actions, "agents": agents}


class Policy:
    def __init__(self, db_path: str, config_path: str):
        self.db_path = db_path
        self.config_path = config_path
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
        self._error = None
        self._config = self._load_config()

    # Config

    def _load_config(self):
        try:
            with open(self.config_path) as f:
                return validate_config(json.load(f))
        except FileNotFoundError:
            return {"actions": {}, "agents": {}}
        except (OSError, ValueError) as e:
            # A broken file fails safe: decide() denies everything that is not a plain read.
            self._error = f"Could not load {self.config_path}: {e}"
            return {"actions": {}, "agents": {}}

    def get_config(self) -> dict:
        with self._lock:
            out = json.loads(json.dumps(self._config))
            if self._error:
                out["error"] = self._error
            return out

    def set_config(self, config: dict) -> None:
        clean = validate_config(config)
        parent = os.path.dirname(os.path.abspath(self.config_path))
        os.makedirs(parent, exist_ok=True)
        with self._lock:
            fd, tmp = tempfile.mkstemp(suffix=".json", dir=parent)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(clean, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o644)
                os.replace(tmp, self.config_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._config = clean
            self._error = None

    def decide(self, agent_id: str, action: str) -> str:
        if action not in ACTIONS:
            return "deny"
        with self._lock:
            if self._error:
                return "allow" if ACTIONS[action][1] == "allow" else "deny"
            glob = self._config["actions"].get(action)
            agent = self._config["agents"].get(agent_id, {}).get(action) if agent_id else None
        if "deny" in (glob, agent):
            return "deny"
        return agent or glob or ACTIONS[action][1]

    # Approvals

    def _row(self, r):
        return {"id": r["id"], "agent": r["agent"], "action": r["action"],
                "details": json.loads(r["details"]), "run_id": r["run_id"], "status": r["status"],
                "created": r["created"], "expires": r["expires"],
                "decided_by": r["decided_by"], "decided_at": r["decided_at"]}

    def _expire(self):
        # Caller holds the lock.
        self._db.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND expires < ?",
                         (time.time(),))

    def request(self, agent_id: str, action: str, details: dict, run_id: str | None = None,
                ttl_s: int = 900) -> str:
        aid = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO approvals (id, agent, action, details, run_id, status, created, expires)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (aid, agent_id, action, json.dumps(clean_details(details or {})), run_id, now, now + ttl_s))
        return aid

    def get(self, approval_id: str) -> dict | None:
        with self._lock:
            self._expire()
            r = self._db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return self._row(r) if r else None

    def list(self, status: str | None = "pending", limit: int = 100) -> list[dict]:
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unknown status: {status}")
        with self._lock:
            self._expire()
            if status is None:
                rows = self._db.execute("SELECT * FROM approvals ORDER BY created DESC LIMIT ?",
                                        (limit,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM approvals WHERE status=? ORDER BY created DESC LIMIT ?",
                                        (status, limit)).fetchall()
        return [self._row(r) for r in rows]

    def answer(self, approval_id: str, approve: bool, by: str) -> dict:
        status = "approved" if approve else "denied"
        with self._lock:
            self._expire()
            cur = self._db.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'",
                (status, by, time.time(), approval_id))
            if cur.rowcount != 1:
                r = self._db.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()
                if r is None:
                    raise ValueError(f"Unknown approval: {approval_id}")
                raise ValueError(f"Approval is already {r['status']}")
            r = self._db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        out = self._row(r)
        self.audit(by, out["action"], status,
                   {"approval_id": approval_id, "agent": out["agent"], "run_id": out["run_id"]})
        return out

    def expire(self, approval_id: str, reason: str) -> dict:
        """Give up on a pending approval, for example when the runner stops waiting."""
        with self._lock:
            self._expire()
            cur = self._db.execute(
                "UPDATE approvals SET status='expired', decided_at=? WHERE id=? AND status='pending'",
                (time.time(), approval_id))
            r = self._db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if r is None:
                raise ValueError(f"Unknown approval: {approval_id}")
            if cur.rowcount != 1:
                raise ValueError(f"Approval is already {r['status']}")
        out = self._row(r)
        self.audit(out["agent"], out["action"], "expired",
                   {"approval_id": approval_id, "reason": reason, "run_id": out["run_id"]})
        return out

    def wait(self, approval_id: str, timeout_s: float, poll_s: float = 0.5) -> str:
        end = time.monotonic() + timeout_s
        while True:
            a = self.get(approval_id)
            if a is None:
                raise ValueError(f"Unknown approval: {approval_id}")
            if a["status"] != "pending":
                return a["status"]
            left = end - time.monotonic()
            if left <= 0:
                return "pending"
            time.sleep(min(poll_s, left))

    # Audit

    def audit(self, actor: str, action: str, decision: str, details: dict | None = None,
              result: str | None = None) -> None:
        text = json.dumps(clean_details(details)) if details is not None else None
        if result is not None:
            result = str(result)[:MAX_STR]
        with self._lock:
            self._db.execute(
                "INSERT INTO audit (ts, actor, action, decision, details, result) VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), actor, action, decision, text, result))

    def audit_log(self, limit: int = 200, since_s: int | None = None, actor: str | None = None,
                  action: str | None = None) -> list[dict]:
        where, args = [], []
        if since_s is not None:
            where.append("ts >= ?")
            args.append(time.time() - since_s)
        if actor is not None:
            where.append("actor = ?")
            args.append(actor)
        if action is not None:
            where.append("action = ?")
            args.append(action)
        sql = "SELECT * FROM audit"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
                 "decision": r["decision"],
                 "details": json.loads(r["details"]) if r["details"] is not None else None,
                 "result": r["result"]} for r in rows]

    def prune(self, keep_days: int = 90) -> None:
        """Delete old audit rows and finished approvals. Both deletes use an index, so daily runs are cheap."""
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            self._expire()
            self._db.execute("DELETE FROM audit WHERE ts < ?", (cutoff,))
            # Pending requests are kept; answered and expired ones go.
            self._db.execute("DELETE FROM approvals WHERE status != 'pending' AND created < ?", (cutoff,))

    # Used by the agent runner

    def check(self, agent_id: str, action: str, details: dict, run_id: str | None = None,
              ttl_s: int = 900) -> dict:
        d = self.decide(agent_id, action)
        approval_id = self.request(agent_id, action, details, run_id, ttl_s) if d == "ask" else None
        info = {"details": details, "run_id": run_id}
        if approval_id:
            info["approval_id"] = approval_id
        self.audit(agent_id, action, d, info)
        return {"decision": d, "approval_id": approval_id}

    def close(self):
        with self._lock:
            self._db.close()


def make_router(policy: Policy, auth_dep) -> APIRouter:
    """Routes for the policy, approval queue and audit log. Mount under "/api"."""
    router = APIRouter(tags=["agents"], dependencies=[Depends(auth_dep)])

    @router.get("/policy")
    def get_policy():
        actions = [{"id": a, "description": desc, "default": default, "effective": policy.decide("", a)}
                   for a, (desc, default) in ACTIONS.items()]
        return {"actions": actions, "config": policy.get_config()}

    @router.put("/policy")
    def put_policy(config: dict = Body(..., embed=True)):
        """Replace the policy, for example {"config": {"actions": {"web.fetch": "allow"}}}."""
        try:
            policy.set_config(config)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "config": policy.get_config()}

    @router.get("/approvals")
    def list_approvals(status: str | None = "pending", limit: int = Query(100, ge=1, le=1000)):
        try:
            return policy.list(status or None, limit)
        except ValueError as e:
            raise HTTPException(422, str(e))

    def answer(approval_id, approve, by):
        if policy.get(approval_id) is None:
            raise HTTPException(404, f"Unknown approval: {approval_id}")
        try:
            return policy.answer(approval_id, approve, by or "admin")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @router.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str, by: str | None = Body(default=None, embed=True)):
        return answer(approval_id, True, by)

    @router.post("/approvals/{approval_id}/deny")
    def deny(approval_id: str, by: str | None = Body(default=None, embed=True)):
        return answer(approval_id, False, by)

    @router.get("/audit")
    def get_audit(limit: int = Query(200, ge=1, le=5000),
                  since: int | None = Query(None, ge=1, description="Seconds back"),
                  actor: str | None = None, action: str | None = None):
        return policy.audit_log(limit, since, actor, action)

    return router
