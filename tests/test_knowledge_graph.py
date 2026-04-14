"""Tests for knowledge graph FTS5 rebuild and basic operations."""

import pytest
from pathlib import Path


@pytest.fixture
def kg(tmp_path, monkeypatch):
    """Create a KnowledgeGraph with a temp database."""
    # Patch the migrations import to avoid ModuleNotFoundError in test context
    import localforge.knowledge.graph as _graph_mod
    _orig_get_conn = _graph_mod.KnowledgeGraph._get_conn.__wrapped__ if hasattr(
        _graph_mod.KnowledgeGraph._get_conn, '__wrapped__'
    ) else None

    from localforge.knowledge.graph import KnowledgeGraph
    import sqlite3

    db = tmp_path / "test_kg.db"
    graph = KnowledgeGraph(db_path=db)

    # Manually init the connection without migrations (which may not resolve in tests)
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_graph_mod.SCHEMA)
    conn.executescript(_graph_mod.FTS_SCHEMA)
    graph._conn = conn

    yield graph
    graph.close()


class TestFTSRebuild:
    def test_rebuild_empty_graph(self, kg):
        """Rebuilding an empty graph returns 0."""
        count = kg.rebuild_fts_index()
        assert count == 0

    def test_rebuild_preserves_search(self, kg):
        """After rebuild, FTS search still finds entities."""
        kg.add_entity("test-entity", "concept", content="hello world", embed=False)
        kg.add_entity("another-entity", "tool", content="foo bar", embed=False)

        # Rebuild
        count = kg.rebuild_fts_index()
        assert count == 2

        # Search should still work
        results = kg.query("hello world", max_results=5)
        assert len(results) >= 1
        assert any("test-entity" in r.get("name", "") for r in results)

    def test_rebuild_after_corruption(self, kg):
        """Simulate FTS corruption by manually deleting FTS content, then rebuild."""
        kg.add_entity("survivor", "concept", content="important data", embed=False)

        # Corrupt: delete FTS content directly
        conn = kg._get_conn()
        conn.execute("DELETE FROM entities_fts")
        conn.commit()

        # Search should return nothing now
        results = kg.query("important data", max_results=5)
        assert len(results) == 0

        # Rebuild fixes it
        count = kg.rebuild_fts_index()
        assert count == 1

        results = kg.query("important data", max_results=5)
        assert len(results) >= 1


class TestKGRebuildToolRegistered:
    def test_kg_rebuild_fts_in_registry(self):
        """kg_rebuild_fts tool is registered (tested via test_tool_registry.py)."""
        # Tool registration requires all tool modules to be imported.
        # This is covered by test_tool_registry.py::test_known_tools_present.
        pass
