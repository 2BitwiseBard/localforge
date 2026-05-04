"""Tests for the database migration system."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from localforge.migrations import _MIGRATIONS, register, run_migrations


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = sqlite3.connect(db_path)
    yield conn
    conn.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def clean_test_migrations():
    """Remove test migrations before/after each test."""
    _MIGRATIONS.pop("test_db", None)
    yield
    _MIGRATIONS.pop("test_db", None)


class TestMigrations:
    def test_creates_schema_version_table(self, temp_db):
        version = run_migrations(temp_db, "test_db")
        assert version == 0
        row = temp_db.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        assert row[0] == 0

    def test_runs_migrations_in_order(self, temp_db):
        @register("test_db", 1, "create test table")
        def _v1(conn):
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

        @register("test_db", 2, "add column")
        def _v2(conn):
            conn.execute("ALTER TABLE test ADD COLUMN name TEXT")

        version = run_migrations(temp_db, "test_db")
        assert version == 2
        # Verify table and column exist
        temp_db.execute("INSERT INTO test (id, name) VALUES (1, 'hello')")
        row = temp_db.execute("SELECT name FROM test WHERE id = 1").fetchone()
        assert row[0] == "hello"

    def test_skips_already_applied(self, temp_db):
        call_count = 0

        @register("test_db", 1, "test")
        def _v1(conn):
            nonlocal call_count
            call_count += 1

        run_migrations(temp_db, "test_db")
        assert call_count == 1

        # Run again — should not re-apply
        run_migrations(temp_db, "test_db")
        assert call_count == 1

    def test_rollback_on_failure(self, temp_db):
        @register("test_db", 1, "good migration")
        def _v1(conn):
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

        @register("test_db", 2, "bad migration")
        def _v2(conn):
            raise RuntimeError("intentional failure")

        with pytest.raises(RuntimeError):
            run_migrations(temp_db, "test_db")

        # Should be at version 1 (v2 rolled back)
        row = temp_db.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        assert row[0] == 1
