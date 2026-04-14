"""SQLite-backed async message bus for agent communication.

Persists all messages to SQLite so inter-agent history survives gateway
restarts.  Real-time delivery still uses asyncio.Queue per subscriber
for in-process speed; the DB is the source of truth for replay/history.
"""

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from localforge.paths import message_bus_db_path

log = logging.getLogger("message-bus")

_DEFAULT_DB = message_bus_db_path()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    sender      TEXT NOT NULL,
    topic       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    recipients  TEXT NOT NULL DEFAULT '[]',
    reply_to    TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_topic    ON messages(topic, created_at);
CREATE INDEX IF NOT EXISTS idx_msg_sender   ON messages(sender, created_at);
CREATE INDEX IF NOT EXISTS idx_msg_created  ON messages(created_at);
"""


@dataclass
class Message:
    """A message on the bus."""
    sender: str
    topic: str
    payload: dict
    recipients: list[str] = field(default_factory=list)  # empty = broadcast
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    reply_to: Optional[str] = None  # message ID this is replying to

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender,
            "topic": self.topic,
            "payload": self.payload,
            "recipients": self.recipients,
            "timestamp": self.timestamp,
            "reply_to": self.reply_to,
        }


class MessageBus:
    """Async message bus with SQLite persistence and topic-based routing."""

    def __init__(self, db_path: Optional[Path] = None, history_limit: int = 2000):
        self._db_path = db_path or _DEFAULT_DB
        self._queues: dict[str, asyncio.Queue] = {}  # subscriber_id -> Queue
        self._topic_handlers: dict[str, list[Callable]] = {}  # topic prefix -> handlers
        self._history_limit = history_limit
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._dropped_count: int = 0

    # -----------------------------------------------------------------------
    # SQLite helpers
    # -----------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
        return self._conn

    def _persist(self, msg: Message):
        """Write a message to the database."""
        try:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR IGNORE INTO messages
                   (id, sender, topic, payload, recipients, reply_to, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id,
                    msg.sender,
                    msg.topic,
                    json.dumps(msg.payload),
                    json.dumps(msg.recipients),
                    msg.reply_to,
                    msg.timestamp,
                ),
            )
            conn.commit()
        except Exception as exc:
            log.error(f"Failed to persist message {msg.id}: {exc}")

    def _prune(self):
        """Keep only the most recent history_limit messages."""
        try:
            conn = self._get_conn()
            conn.execute(
                """DELETE FROM messages WHERE id NOT IN (
                       SELECT id FROM messages ORDER BY created_at DESC LIMIT ?
                   )""",
                (self._history_limit,),
            )
            conn.commit()
        except Exception as exc:
            log.error(f"Failed to prune message history: {exc}")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def stats(self) -> dict:
        """Return bus health metrics."""
        return {
            "subscribers": len(self._queues),
            "queue_sizes": {sid: q.qsize() for sid, q in self._queues.items()},
            "dropped_total": self._dropped_count,
            "topic_handlers": len(self._topic_handlers),
        }

    # -----------------------------------------------------------------------
    # Pub/sub
    # -----------------------------------------------------------------------

    async def subscribe(self, subscriber_id: str, queue_size: int = 500) -> asyncio.Queue:
        """Register a subscriber and return their message queue."""
        async with self._lock:
            if subscriber_id not in self._queues:
                self._queues[subscriber_id] = asyncio.Queue(maxsize=queue_size)
                log.debug(f"Subscribed: {subscriber_id}")
            return self._queues[subscriber_id]

    async def unsubscribe(self, subscriber_id: str):
        """Remove a subscriber."""
        async with self._lock:
            self._queues.pop(subscriber_id, None)
            log.debug(f"Unsubscribed: {subscriber_id}")

    def on_topic(self, topic_prefix: str, handler: Callable):
        """Register a handler for messages matching a topic prefix."""
        self._topic_handlers.setdefault(topic_prefix, []).append(handler)

    async def publish(self, msg: Message):
        """Publish a message to targeted recipients or broadcast.

        The message is persisted to SQLite *and* delivered to in-process queues.
        """
        # Persist first (source of truth)
        self._persist(msg)

        delivered = 0
        async with self._lock:
            targets = list(self._queues.items())

        for sub_id, queue in targets:
            # Skip sender
            if sub_id == msg.sender:
                continue
            # If recipients specified, only deliver to them
            if msg.recipients and sub_id not in msg.recipients:
                continue
            try:
                queue.put_nowait(msg)
                delivered += 1
            except asyncio.QueueFull:
                self._dropped_count += 1
                log.warning(f"Queue full for {sub_id}, dropping message {msg.id} "
                            f"(topic={msg.topic}, total_dropped={self._dropped_count})")

        # Fire topic handlers
        for prefix, handlers in self._topic_handlers.items():
            if msg.topic.startswith(prefix):
                for handler in handlers:
                    try:
                        result = handler(msg)
                        if asyncio.iscoroutine(result):
                            asyncio.create_task(result)
                    except Exception as e:
                        log.error(f"Topic handler error for {prefix}: {e}")

        log.debug(f"Published {msg.topic} from {msg.sender} → {delivered} recipients")

    async def request(self, msg: Message, timeout: float = 30) -> Optional[Message]:
        """Publish a message and wait for a reply (by reply_to matching msg.id)."""
        # Ensure sender is subscribed
        queue = await self.subscribe(msg.sender)
        await self.publish(msg)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                reply = await asyncio.wait_for(queue.get(), timeout=min(1.0, deadline - time.time()))
                if isinstance(reply, Message) and reply.reply_to == msg.id:
                    return reply
                # Put back non-matching messages
                try:
                    queue.put_nowait(reply)
                except asyncio.QueueFull:
                    pass
            except asyncio.TimeoutError:
                continue
        return None

    # -----------------------------------------------------------------------
    # History (reads from SQLite, not in-memory)
    # -----------------------------------------------------------------------

    def get_history(self, topic_prefix: str = "", sender: str = "",
                    limit: int = 50) -> list[dict]:
        """Get recent messages from the persistent store, optionally filtered."""
        try:
            conn = self._get_conn()
            conditions = []
            params: list = []
            if topic_prefix:
                conditions.append("topic LIKE ?")
                params.append(topic_prefix + "%")
            if sender:
                conditions.append("sender = ?")
                params.append(sender)
            where = " AND ".join(conditions) if conditions else "1=1"
            params.append(limit)

            rows = conn.execute(
                f"""SELECT id, sender, topic, payload, recipients, reply_to, created_at
                    FROM messages WHERE {where}
                    ORDER BY created_at DESC LIMIT ?""",
                params,
            ).fetchall()

            return [
                {
                    "id": r[0],
                    "sender": r[1],
                    "topic": r[2],
                    "payload": json.loads(r[3]),
                    "recipients": json.loads(r[4]),
                    "reply_to": r[5],
                    "timestamp": r[6],
                }
                for r in reversed(rows)  # oldest first
            ]
        except Exception as exc:
            log.error(f"Failed to read message history: {exc}")
            return []

    def message_count(self) -> int:
        """Total persisted messages."""
        try:
            conn = self._get_conn()
            row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def cleanup(self, max_age_days: int = 7):
        """Remove messages older than max_age_days."""
        try:
            conn = self._get_conn()
            cutoff = time.time() - (max_age_days * 86400)
            conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            conn.commit()
        except Exception as exc:
            log.error(f"Failed to cleanup old messages: {exc}")

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    @property
    def subscribers(self) -> list[str]:
        return list(self._queues.keys())
