"""Tests for the message bus backpressure and stats."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from localforge.agents.message_bus import Message, MessageBus


@pytest.fixture
def bus(tmp_path):
    """Create a temporary message bus."""
    db_path = tmp_path / "test_bus.db"
    b = MessageBus(db_path=db_path, history_limit=100)
    yield b
    b.close()


@pytest.mark.asyncio
async def test_publish_and_receive(bus):
    queue = await bus.subscribe("test-sub", queue_size=10)
    msg = Message(sender="tester", topic="test.hello", payload={"data": 1})
    await bus.publish(msg)
    received = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert received.topic == "test.hello"
    assert received.payload["data"] == 1


@pytest.mark.asyncio
async def test_dropped_count_on_full_queue(bus):
    await bus.subscribe("slow-sub", queue_size=2)
    for i in range(5):
        await bus.publish(Message(sender="flood", topic="test.flood", payload={"i": i}))
    stats = bus.stats()
    assert stats["dropped_total"] == 3  # 5 published, queue holds 2, 3 dropped


@pytest.mark.asyncio
async def test_stats_reports_subscribers(bus):
    await bus.subscribe("sub-a")
    await bus.subscribe("sub-b")
    stats = bus.stats()
    assert stats["subscribers"] == 2
    assert "sub-a" in stats["queue_sizes"]
    assert "sub-b" in stats["queue_sizes"]


@pytest.mark.asyncio
async def test_unsubscribe(bus):
    await bus.subscribe("temp-sub")
    await bus.unsubscribe("temp-sub")
    stats = bus.stats()
    assert "temp-sub" not in stats["queue_sizes"]
