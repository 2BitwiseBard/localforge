"""Database schema migration runner.

Each database (knowledge.db, task_queue.db, approval_queue.db) gets a
schema_version table. On connection, the current version is checked and
any pending migrations are run sequentially.

Migration functions are registered per database name.
"""

import logging
import sqlite3
from typing import Callable

log = logging.getLogger("localforge.migrations")

# Registry: {db_name: [(version, description, migrate_fn), ...]}
_MIGRATIONS: dict[str, list[tuple[int, str, Callable[[sqlite3.Connection], None]]]] = {}


def register(db_name: str, version: int, description: str):
    """Decorator to register a migration function."""

    def decorator(fn: Callable[[sqlite3.Connection], None]):
        _MIGRATIONS.setdefault(db_name, []).append((version, description, fn))
        _MIGRATIONS[db_name].sort(key=lambda x: x[0])
        return fn

    return decorator


def run_migrations(conn: sqlite3.Connection, db_name: str) -> int:
    """Run pending migrations for the given database. Returns current version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  version INTEGER NOT NULL DEFAULT 0,"
        "  updated_at TEXT DEFAULT (datetime('now'))"
        ")"
    )
    conn.execute("INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 0)")

    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    current = row[0] if row else 0

    migrations = _MIGRATIONS.get(db_name, [])
    applied = 0
    for version, description, migrate_fn in migrations:
        if version <= current:
            continue
        log.info(f"[{db_name}] Applying migration v{version}: {description}")
        try:
            migrate_fn(conn)
            conn.execute(
                "UPDATE schema_version SET version = ?, updated_at = datetime('now') WHERE id = 1",
                (version,),
            )
            conn.commit()
            applied += 1
        except Exception:
            conn.rollback()
            log.exception(f"[{db_name}] Migration v{version} failed, rolling back")
            raise

    if applied:
        log.info(f"[{db_name}] Applied {applied} migration(s), now at v{current + applied}")
    return current + applied


# --- Knowledge Graph Migrations ---


@register("knowledge", 1, "Initial schema (baseline)")
def _kg_v1(conn: sqlite3.Connection):
    """Baseline — schema already exists, just mark as v1."""
    pass


@register("knowledge", 2, "Add updated_at and compound relation indexes")
def _kg_v2(conn: sqlite3.Connection):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_updated_at ON entities(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_to_type ON relations(to_id, relation_type)")


# --- Task Queue Migrations ---


@register("task_queue", 1, "Initial schema (baseline)")
def _tq_v1(conn: sqlite3.Connection):
    pass


# --- Approval Queue Migrations ---


@register("approval_queue", 1, "Initial schema (baseline)")
def _aq_v1(conn: sqlite3.Connection):
    pass
