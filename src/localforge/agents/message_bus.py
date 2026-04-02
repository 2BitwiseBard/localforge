"""In-process async message bus for agent communication.

Simple pub/sub using asyncio.Queue per subscriber. No persistence —
agents that need reliable delivery should use the TaskQueue instead.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("message-bus")


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
    """Async in-process message bus with topic-based routing."""

    def __init__(self, history_limit: int = 500):
        self._queues: dict[str, asyncio.Queue] = {}  # subscriber_id -> Queue
        self._topic_handlers: dict[str, list[Callable]] = {}  # topic prefix -> handlers
        self._history: list[Message] = []
        self._history_limit = history_limit
        self._lock = asyncio.Lock()

    async def subscribe(self, subscriber_id: str, queue_size: int = 100) -> asyncio.Queue:
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
        """Publish a message to targeted recipients or broadcast."""
        # Store in history
        self._history.append(msg)
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit:]

        delivered = 0
        async with self._lock:
            targets = self._queues.items()

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
                log.warning(f"Queue full for {sub_id}, dropping message {msg.id}")

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

    def get_history(self, topic_prefix: str = "", sender: str = "",
                    limit: int = 50) -> list[dict]:
        """Get recent messages, optionally filtered."""
        msgs = self._history
        if topic_prefix:
            msgs = [m for m in msgs if m.topic.startswith(topic_prefix)]
        if sender:
            msgs = [m for m in msgs if m.sender == sender]
        return [m.to_dict() for m in msgs[-limit:]]

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    @property
    def subscribers(self) -> list[str]:
        return list(self._queues.keys())
