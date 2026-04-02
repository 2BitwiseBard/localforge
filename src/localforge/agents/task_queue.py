"""SQLite-backed priority task queue for agents.

Survives restarts, supports retry with exponential backoff,
parent-child task relationships, and named queues.
"""

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("task-queue")

DB_PATH = Path(__file__).parent.parent / "task_queue.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    queue TEXT NOT NULL DEFAULT 'default',
    priority INTEGER NOT NULL DEFAULT 5,
    payload TEXT NOT NULL,
    result TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    not_before REAL DEFAULT 0,
    agent_id TEXT,
    parent_task_id TEXT,
    batch_id TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    error TEXT,
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_dequeue
    ON tasks(queue, status, not_before, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_batch ON tasks(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
"""


class TaskQueue:
    """Persistent priority task queue backed by SQLite."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def enqueue(self, payload: dict, queue: str = "default",
                priority: int = 5, parent_task_id: Optional[str] = None,
                batch_id: Optional[str] = None,
                max_retries: int = 3) -> str:
        """Add a task. Returns task_id. Priority: 1=highest, 10=lowest."""
        task_id = uuid.uuid4().hex[:16]
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO tasks (id, queue, priority, payload, status, created_at,
                                  parent_task_id, batch_id, max_retries)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (task_id, queue, max(1, min(10, priority)),
             json.dumps(payload), time.time(),
             parent_task_id, batch_id, max_retries),
        )
        conn.commit()
        log.debug(f"Enqueued task {task_id} on queue '{queue}' (priority={priority})")
        return task_id

    def dequeue(self, queue: str = "default",
                agent_id: Optional[str] = None) -> Optional[dict]:
        """Claim the next pending task. Returns task dict or None."""
        conn = self._get_conn()
        now = time.time()
        row = conn.execute(
            """SELECT id, payload, priority, parent_task_id, batch_id, retry_count
               FROM tasks
               WHERE queue = ? AND status = 'pending' AND not_before <= ?
               ORDER BY priority ASC, created_at ASC
               LIMIT 1""",
            (queue, now),
        ).fetchone()
        if not row:
            return None

        task_id = row[0]
        conn.execute(
            "UPDATE tasks SET status = 'running', started_at = ?, agent_id = ? WHERE id = ?",
            (now, agent_id, task_id),
        )
        conn.commit()
        return {
            "id": task_id,
            "payload": json.loads(row[1]),
            "priority": row[2],
            "parent_task_id": row[3],
            "batch_id": row[4],
            "retry_count": row[5],
        }

    def complete(self, task_id: str, result: Optional[dict] = None):
        """Mark a task as completed."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ?, result = ? WHERE id = ?",
            (time.time(), json.dumps(result) if result else None, task_id),
        )
        conn.commit()

    def fail(self, task_id: str, error: str, retry: bool = True):
        """Mark a task as failed, optionally re-enqueue with backoff."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT retry_count, max_retries FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return

        retry_count, max_retries = row
        if retry and retry_count < max_retries:
            # Exponential backoff: 10s, 40s, 90s, ...
            backoff = 10 * ((retry_count + 1) ** 2)
            conn.execute(
                """UPDATE tasks SET status = 'pending', error = ?,
                   retry_count = retry_count + 1,
                   not_before = ?, started_at = NULL, agent_id = NULL
                   WHERE id = ?""",
                (error, time.time() + backoff, task_id),
            )
            log.info(f"Task {task_id} retrying in {backoff}s (attempt {retry_count + 2}/{max_retries})")
        else:
            conn.execute(
                "UPDATE tasks SET status = 'failed', completed_at = ?, error = ? WHERE id = ?",
                (time.time(), error, task_id),
            )
        conn.commit()

    def cancel(self, task_id: str) -> bool:
        """Cancel a pending or running task."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', completed_at = ? WHERE id = ? AND status IN ('pending', 'running')",
            (time.time(), task_id),
        )
        conn.commit()
        return conn.total_changes > 0

    def get_task(self, task_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, queue, priority, payload, result, status, created_at, started_at, completed_at, agent_id, parent_task_id, batch_id, retry_count, max_retries, error FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "queue": row[1], "priority": row[2],
            "payload": json.loads(row[3]), "result": json.loads(row[4]) if row[4] else None,
            "status": row[5], "created_at": row[6], "started_at": row[7],
            "completed_at": row[8], "agent_id": row[9],
            "parent_task_id": row[10], "batch_id": row[11],
            "retry_count": row[12], "max_retries": row[13], "error": row[14],
        }

    def list_tasks(self, queue: Optional[str] = None, status: Optional[str] = None,
                   batch_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        conn = self._get_conn()
        conditions = []
        params = []
        if queue:
            conditions.append("queue = ?")
            params.append(queue)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if batch_id:
            conditions.append("batch_id = ?")
            params.append(batch_id)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        rows = conn.execute(
            f"SELECT id, queue, priority, status, created_at, agent_id, batch_id, error FROM tasks WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()

        return [
            {"id": r[0], "queue": r[1], "priority": r[2], "status": r[3],
             "created_at": r[4], "agent_id": r[5], "batch_id": r[6], "error": r[7]}
            for r in rows
        ]

    def batch_status(self, batch_id: str) -> dict:
        """Get completion stats for a batch."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM tasks WHERE batch_id = ? GROUP BY status",
            (batch_id,),
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        total = sum(counts.values())
        return {
            "batch_id": batch_id,
            "total": total,
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
        }

    def batch_results(self, batch_id: str) -> list[dict]:
        """Get results for all completed tasks in a batch."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, payload, result, status, error FROM tasks WHERE batch_id = ? ORDER BY created_at",
            (batch_id,),
        ).fetchall()
        return [
            {"id": r[0], "payload": json.loads(r[1]),
             "result": json.loads(r[2]) if r[2] else None,
             "status": r[3], "error": r[4]}
            for r in rows
        ]

    def queue_depth(self, queue: str = "default") -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE queue = ? AND status = 'pending'",
            (queue,),
        ).fetchone()
        return row[0] if row else 0

    def cleanup(self, max_age_days: int = 7):
        """Remove completed/failed tasks older than max_age_days."""
        conn = self._get_conn()
        cutoff = time.time() - (max_age_days * 86400)
        conn.execute(
            "DELETE FROM tasks WHERE status IN ('done', 'failed', 'cancelled') AND completed_at < ?",
            (cutoff,),
        )
        conn.commit()
