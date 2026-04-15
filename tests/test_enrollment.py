"""Tests for the mesh enrollment substrate: short-lived tokens + worker registry."""
from __future__ import annotations

import time

import pytest

from localforge.enrollment import EnrollmentStore, WorkerRegistry


class TestEnrollmentStore:
    def test_mint_and_consume_roundtrip(self):
        store = EnrollmentStore(ttl_seconds=60)
        info = store.mint(issued_by="tyler", note="laptop 1")
        assert info["token"] and info["ttl_seconds"] == 60
        assert info["issued_by"] == "tyler"

        record = store.consume(info["token"])
        assert record is not None
        assert record["issued_by"] == "tyler"
        assert record["note"] == "laptop 1"

    def test_token_is_single_use(self):
        store = EnrollmentStore(ttl_seconds=60)
        info = store.mint(issued_by="admin")
        assert store.consume(info["token"]) is not None
        # Second consume must fail — tokens are burn-on-use.
        assert store.consume(info["token"]) is None

    def test_expired_token_rejected(self, monkeypatch):
        store = EnrollmentStore(ttl_seconds=1)
        info = store.mint(issued_by="admin")
        # Fast-forward past expiry.
        future = time.time() + 10
        monkeypatch.setattr("localforge.enrollment.time.time", lambda: future)
        assert store.consume(info["token"]) is None

    def test_peek_does_not_consume(self):
        store = EnrollmentStore(ttl_seconds=60)
        info = store.mint(issued_by="admin")
        assert store.peek(info["token"]) is not None
        # Consume should still work after peek.
        assert store.consume(info["token"]) is not None

    def test_unknown_token_returns_none(self):
        store = EnrollmentStore()
        assert store.consume("definitely-not-issued") is None
        assert store.peek("definitely-not-issued") is None


class TestWorkerRegistry:
    @pytest.fixture
    def registry(self, tmp_path):
        return WorkerRegistry(path=tmp_path / "workers.json")

    def test_register_returns_fresh_key(self, registry):
        worker_id, key = registry.register(
            hostname="laptop-01",
            platform="linux",
            hardware={"ram_mb": 16000},
            enrolled_by="tyler",
        )
        assert worker_id.startswith("laptop-01-")
        assert len(key) >= 32  # URL-safe token_urlsafe(32) → ≥ 43 chars

    def test_find_by_key_matches_only_correct_key(self, registry):
        _, key = registry.register(
            hostname="box", platform="win32", hardware={}, enrolled_by="tyler"
        )
        assert registry.find_by_key(key) is not None
        assert registry.find_by_key("wrong-key") is None

    def test_hashed_storage_does_not_leak_plaintext(self, registry, tmp_path):
        _, key = registry.register(
            hostname="box", platform="linux", hardware={}, enrolled_by="admin"
        )
        raw = (tmp_path / "workers.json").read_text()
        assert key not in raw, "Plaintext worker key must not be written to disk"
        assert "$2b$" in raw, "Expected a bcrypt hash in the stored record"

    def test_list_workers_excludes_key_hash(self, registry):
        registry.register(hostname="a", platform="linux", hardware={}, enrolled_by="x")
        workers = registry.list_workers()
        assert len(workers) == 1
        assert "api_key_hash" not in workers[0]
        assert workers[0]["hostname"] == "a"

    def test_revoke_removes_worker(self, registry):
        worker_id, key = registry.register(
            hostname="gone", platform="linux", hardware={}, enrolled_by="x"
        )
        assert registry.revoke(worker_id) is True
        assert registry.find_by_key(key) is None
        assert registry.revoke(worker_id) is False

    def test_touch_bumps_last_seen(self, registry):
        worker_id, _ = registry.register(
            hostname="alive", platform="linux", hardware={}, enrolled_by="x"
        )
        original = registry.list_workers()[0]["last_seen"]
        time.sleep(0.01)
        registry.touch(worker_id)
        bumped = registry.list_workers()[0]["last_seen"]
        assert bumped > original
