"""Tests for knowledge graph FTS5 rebuild and basic operations."""


import pytest


@pytest.fixture
def kg(tmp_path, monkeypatch):
    """Create a KnowledgeGraph with a temp database."""
    # Patch the migrations import to avoid ModuleNotFoundError in test context
    import localforge.knowledge.graph as _graph_mod
    _orig_get_conn = _graph_mod.KnowledgeGraph._get_conn.__wrapped__ if hasattr(
        _graph_mod.KnowledgeGraph._get_conn, '__wrapped__'
    ) else None

    import sqlite3

    from localforge.knowledge.graph import KnowledgeGraph

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


class TestKGExportImport:
    def test_export_empty(self, kg):
        """Exporting an empty graph returns empty lists."""
        data = kg.export_all()
        assert data == {"entities": [], "relations": []}

    def test_export_roundtrip(self, kg):
        """Entities and relations survive an export→import cycle."""
        id1 = kg.add_entity("alpha", "concept", content="first", embed=False)
        id2 = kg.add_entity("beta", "tool", content="second", embed=False)
        kg.add_relation(id1, id2, "RELATED_TO")

        exported = kg.export_all()
        assert len(exported["entities"]) == 2
        assert len(exported["relations"]) == 1

        # Import into a fresh graph
        import sqlite3

        import localforge.knowledge.graph as _gmod

        db2 = kg.db_path.parent / "test_kg2.db"
        kg2 = _gmod.KnowledgeGraph(db_path=db2)
        conn2 = sqlite3.connect(str(db2))
        conn2.execute("PRAGMA journal_mode=WAL")
        conn2.execute("PRAGMA foreign_keys=ON")
        conn2.executescript(_gmod.SCHEMA)
        conn2.executescript(_gmod.FTS_SCHEMA)
        kg2._conn = conn2

        result = kg2.import_all(exported, merge=False)
        assert result["entities_added"] == 2
        assert result["relations_added"] == 1

        # Verify data
        e = kg2.find_entity("alpha", "concept")
        assert e is not None
        assert e.content == "first"

        e2 = kg2.find_entity("beta", "tool")
        assert e2 is not None
        rels = kg2.get_relations(e.id, direction="from")
        assert len(rels) == 1
        assert rels[0]["relation"] == "RELATED_TO"
        kg2.close()

    def test_import_merge_updates(self, kg):
        """Merge import updates existing entities instead of duplicating."""
        kg.add_entity("gamma", "concept", content="original", embed=False)

        data = {
            "entities": [{"id": 999, "name": "gamma", "type": "concept", "content": "updated"}],
            "relations": [],
        }
        result = kg.import_all(data, merge=True)
        assert result["entities_updated"] == 1
        assert result["entities_added"] == 0

        e = kg.find_entity("gamma", "concept")
        assert e.content == "updated"
