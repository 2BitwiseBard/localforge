"""Approval gate for agent actions.

When an agent at FULL trust level tries to execute a destructive action,
the request is placed in an approval queue. A human must approve it via
the dashboard before the action proceeds.

Destructive actions: swap_model, unload_model, delete_index, delete_note,
delete_session, save_note (overwrite), set_generation_params.

Features:
  - Priority levels (urgent, normal) with separate TTLs
  - 80%-of-TTL warning notification before auto-deny
  - Audit table logging all decisions
"""

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from localforge.paths import approval_db_path

log = logging.getLogger("approval-gate")

DB_PATH = approval_db_path()

# Tools that require approval at FULL trust level
APPROVAL_REQUIRED = {
    "swap_model",
    "unload_model",
    "delete_index",
    "delete_note",
    "delete_session",
    "set_generation_params",
    "reload_config",
    "fs_write",
    "fs_edit",
    "fs_delete",
    "shell_exec",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    reason TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    decided_at REAL,
    decided_by TEXT,
    ttl_seconds INTEGER DEFAULT 300,
    warning_sent INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS approval_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    action TEXT NOT NULL,
    decided_by TEXT,
    detail TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_priority ON approvals(priority, status);
CREATE INDEX IF NOT EXISTS idx_audit_request ON approval_audit(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON approval_audit(created_at);
"""

# Default TTLs per priority
_DEFAULT_TTLS = {
    "urgent": 120,  # 2 minutes
    "normal": 300,  # 5 minutes
}


class ApprovalQueue:
    """SQLite-backed approval queue for gated agent actions."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        # Pending futures waiting for approval
        self._waiters: dict[str, asyncio.Future] = {}
        # Notification callback (set by supervisor/gateway)
        self._notify_callback: Optional[Callable] = None
        # TTL warning task
        self._warning_task: Optional[asyncio.Task] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            # Migrate: add columns if they don't exist (idempotent)
            try:
                self._conn.execute("ALTER TABLE approvals ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'")
            except sqlite3.OperationalError:
                pass  # column already exists
            try:
                self._conn.execute("ALTER TABLE approvals ADD COLUMN warning_sent INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
        return self._conn

    def close(self):
        if self._warning_task:
            self._warning_task.cancel()
            self._warning_task = None
        if self._conn:
            self._conn.close()
            self._conn = None

    def on_notify(self, callback: Callable):
        """Register a callback for TTL warning notifications.

        Callback signature: callback({"title": str, "body": str, "level": str,
                                       "request_id": str, "agent_id": str, ...})
        """
        self._notify_callback = callback

    def start_warning_loop(self):
        """Start the async loop that fires TTL warnings at 80% expiry."""
        if self._warning_task is None or self._warning_task.done():
            self._warning_task = asyncio.create_task(self._ttl_warning_loop())

    async def _ttl_warning_loop(self):
        """Every 10s, check for pending requests approaching TTL and notify."""
        while True:
            try:
                self._check_ttl_warnings()
            except Exception as exc:
                log.error(f"TTL warning loop error: {exc}")
            await asyncio.sleep(10)

    def _check_ttl_warnings(self):
        """Scan pending requests and send warnings at 80% TTL."""
        conn = self._get_conn()
        now = time.time()
        rows = conn.execute(
            """SELECT id, agent_id, tool_name, created_at, ttl_seconds, priority
               FROM approvals
               WHERE status = 'pending' AND warning_sent = 0""",
        ).fetchall()

        for r in rows:
            req_id, agent_id, tool_name, created_at, ttl, priority = r
            age = now - created_at
            threshold = ttl * 0.8
            if age >= threshold:
                remaining = max(0, int(ttl - age))
                conn.execute(
                    "UPDATE approvals SET warning_sent = 1 WHERE id = ?",
                    (req_id,),
                )
                conn.commit()
                log.warning(f"Approval {req_id} ({agent_id} → {tool_name}) expiring in {remaining}s")
                if self._notify_callback:
                    try:
                        result = self._notify_callback(
                            {
                                "title": f"Approval expiring: {tool_name}",
                                "body": f"Agent '{agent_id}' requested {tool_name}. "
                                f"{remaining}s remaining before auto-deny.",
                                "level": "warning",
                                "request_id": req_id,
                                "agent_id": agent_id,
                                "tool_name": tool_name,
                                "priority": priority,
                                "timestamp": now,
                            }
                        )
                        if asyncio.iscoroutine(result):
                            asyncio.create_task(result)
                    except Exception as exc:
                        log.error(f"TTL notification callback error: {exc}")

    def _audit(
        self, request_id: str, agent_id: str, tool_name: str, action: str, decided_by: str = "", detail: str = ""
    ):
        """Write an audit log entry."""
        try:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO approval_audit
                   (request_id, agent_id, tool_name, action, decided_by, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (request_id, agent_id, tool_name, action, decided_by, detail, time.time()),
            )
            conn.commit()
        except Exception as exc:
            log.error(f"Audit log error: {exc}")

    def request_approval(
        self,
        agent_id: str,
        tool_name: str,
        arguments: dict,
        reason: str = "",
        priority: str = "normal",
        ttl: Optional[int] = None,
    ) -> str:
        """Submit an action for approval. Returns request ID.

        Args:
            priority: "urgent" or "normal"
            ttl: Override TTL in seconds (defaults based on priority)
        """
        if priority not in _DEFAULT_TTLS:
            priority = "normal"
        if ttl is None:
            ttl = _DEFAULT_TTLS[priority]

        req_id = uuid.uuid4().hex[:16]
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO approvals (id, agent_id, tool_name, arguments, reason,
                                      priority, status, created_at, ttl_seconds)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (req_id, agent_id, tool_name, json.dumps(arguments), reason, priority, time.time(), ttl),
        )
        conn.commit()

        self._audit(req_id, agent_id, tool_name, "requested", detail=f"priority={priority}, ttl={ttl}s")

        log.info(f"Approval requested: {req_id} ({agent_id} → {tool_name}, priority={priority}, ttl={ttl}s)")
        return req_id

    async def wait_for_approval(self, req_id: str, timeout: float = 300) -> bool:
        """Wait for a pending request to be approved or denied. Returns True if approved."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._waiters[req_id] = future
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            # Auto-deny on timeout
            self.deny(req_id, decided_by="timeout")
            return False
        finally:
            self._waiters.pop(req_id, None)

    def approve(self, req_id: str, decided_by: str = "dashboard") -> bool:
        """Approve a pending request."""
        conn = self._get_conn()
        # Get details for audit
        row = conn.execute(
            "SELECT agent_id, tool_name FROM approvals WHERE id = ? AND status = 'pending'",
            (req_id,),
        ).fetchone()
        if not row:
            return False

        cur = conn.execute(
            """UPDATE approvals SET status = 'approved', decided_at = ?,
               decided_by = ? WHERE id = ? AND status = 'pending'""",
            (time.time(), decided_by, req_id),
        )
        conn.commit()
        changed = cur.rowcount > 0
        if changed:
            self._audit(req_id, row[0], row[1], "approved", decided_by=decided_by)
            if req_id in self._waiters:
                self._waiters[req_id].set_result(True)
        return changed

    def deny(self, req_id: str, decided_by: str = "dashboard") -> bool:
        """Deny a pending request."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT agent_id, tool_name FROM approvals WHERE id = ? AND status = 'pending'",
            (req_id,),
        ).fetchone()
        if not row:
            return False

        cur = conn.execute(
            """UPDATE approvals SET status = 'denied', decided_at = ?,
               decided_by = ? WHERE id = ? AND status = 'pending'""",
            (time.time(), decided_by, req_id),
        )
        conn.commit()
        changed = cur.rowcount > 0
        if changed:
            self._audit(req_id, row[0], row[1], "denied", decided_by=decided_by)
            if req_id in self._waiters:
                self._waiters[req_id].set_result(False)
        return changed

    def list_pending(self, limit: int = 50) -> list[dict]:
        """List pending approval requests, ordered by priority then age."""
        conn = self._get_conn()
        now = time.time()
        rows = conn.execute(
            """SELECT id, agent_id, tool_name, arguments, reason, created_at,
                      ttl_seconds, priority
               FROM approvals WHERE status = 'pending'
               ORDER BY
                   CASE priority WHEN 'urgent' THEN 0 ELSE 1 END,
                   created_at ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        results = []
        for r in rows:
            age = now - r[5]
            ttl = r[6]
            if age > ttl:
                # Auto-expire
                self.deny(r[0], decided_by="expired")
                continue
            results.append(
                {
                    "id": r[0],
                    "agent_id": r[1],
                    "tool_name": r[2],
                    "arguments": json.loads(r[3]),
                    "reason": r[4],
                    "created_at": r[5],
                    "ttl_seconds": ttl,
                    "remaining_seconds": max(0, int(ttl - age)),
                    "priority": r[7],
                }
            )
        return results

    def list_recent(self, limit: int = 20) -> list[dict]:
        """List recently decided approvals."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT id, agent_id, tool_name, status, created_at, decided_at,
                      decided_by, priority
               FROM approvals WHERE status != 'pending'
               ORDER BY decided_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "agent_id": r[1],
                "tool_name": r[2],
                "status": r[3],
                "created_at": r[4],
                "decided_at": r[5],
                "decided_by": r[6],
                "priority": r[7],
            }
            for r in rows
        ]

    def get_audit_log(self, request_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        """Query the audit trail."""
        conn = self._get_conn()
        if request_id:
            rows = conn.execute(
                """SELECT request_id, agent_id, tool_name, action, decided_by,
                          detail, created_at
                   FROM approval_audit WHERE request_id = ?
                   ORDER BY created_at ASC""",
                (request_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT request_id, agent_id, tool_name, action, decided_by,
                          detail, created_at
                   FROM approval_audit
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "request_id": r[0],
                "agent_id": r[1],
                "tool_name": r[2],
                "action": r[3],
                "decided_by": r[4],
                "detail": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]

    def needs_approval(self, tool_name: str) -> bool:
        """Check if a tool requires approval."""
        return tool_name in APPROVAL_REQUIRED

    def cleanup(self, max_age_days: int = 7):
        """Remove old decided requests and audit entries."""
        conn = self._get_conn()
        cutoff = time.time() - (max_age_days * 86400)
        conn.execute(
            "DELETE FROM approvals WHERE status != 'pending' AND decided_at < ?",
            (cutoff,),
        )
        conn.execute(
            "DELETE FROM approval_audit WHERE created_at < ?",
            (cutoff,),
        )
        conn.commit()
