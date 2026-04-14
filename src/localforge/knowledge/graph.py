"""SQLite-backed knowledge graph with semantic search.

Storage: SQLite with FTS5 for full-text search + optional embedding column
for semantic search via the existing fastembed infrastructure.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from localforge.paths import knowledge_db_path

log = logging.getLogger("knowledge-graph")

DB_PATH = knowledge_db_path()

DEFAULT_ENTITY_TYPES = {
    "concept", "code_module", "decision", "learning",
    "person", "tool", "project", "task", "event", "artifact",
}
DEFAULT_RELATION_TYPES = {
    "DEPENDS_ON", "DECIDED_BY", "RELATED_TO", "SUPERSEDES",
    "IMPLEMENTS", "CREATED_BY", "PART_OF", "REFERENCES",
    "CONFLICTS_WITH", "EXTENDS",
}

# Backwards-compatible aliases
ENTITY_TYPES = DEFAULT_ENTITY_TYPES
RELATION_TYPES = DEFAULT_RELATION_TYPES

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    embedding BLOB,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE(from_id, to_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id);
CREATE INDEX IF NOT EXISTS idx_entities_updated_at ON entities(updated_at);
CREATE INDEX IF NOT EXISTS idx_relations_to_type ON relations(to_id, relation_type);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
    name, content, type,
    content=entities,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts(rowid, name, content, type)
    VALUES (new.id, new.name, new.content, new.type);
END;

CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, content, type)
    VALUES ('delete', old.id, old.name, old.content, old.type);
END;

CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, content, type)
    VALUES ('delete', old.id, old.name, old.content, old.type);
    INSERT INTO entities_fts(rowid, name, content, type)
    VALUES (new.id, new.name, new.content, new.type);
END;
"""


@dataclass
class Entity:
    id: int
    name: str
    type: str
    content: str
    created_at: float
    updated_at: float
    metadata: dict

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass
class Relation:
    id: int
    from_id: int
    to_id: int
    relation_type: str
    metadata: dict
    created_at: float


class KnowledgeGraph:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        self._embed_fn = None  # Lazy-loaded

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.executescript(FTS_SCHEMA)
            from .migrations import run_migrations
            run_migrations(self._conn, "knowledge")
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- Embedding (lazy) ---

    def _get_embed_fn(self):
        """Lazy-load the embedding function from the MCP server's fastembed."""
        if self._embed_fn is None:
            try:
                from fastembed import TextEmbedding
                model = TextEmbedding("jinaai/jina-embeddings-v2-base-code")
                self._embed_fn = lambda text: list(model.embed([text]))[0].tolist()
            except Exception as e:
                log.warning(f"Embedding not available: {e}")
                self._embed_fn = lambda text: None
        return self._embed_fn

    def _embed(self, text: str) -> Optional[bytes]:
        embed_fn = self._get_embed_fn()
        vec = embed_fn(text)
        if vec is None:
            return None
        return json.dumps(vec).encode()

    # --- Entities ---

    def add_entity(self, name: str, type: str, content: str = "",
                   metadata: Optional[dict] = None, embed: bool = True) -> int:
        """Add or update an entity. Returns entity ID."""
        if type not in ENTITY_TYPES:
            raise ValueError(f"Unknown entity type: {type}. Valid: {ENTITY_TYPES}")

        conn = self._get_conn()
        now = time.time()
        meta_json = json.dumps(metadata or {})

        # Check for existing
        row = conn.execute(
            "SELECT id FROM entities WHERE name = ? AND type = ?",
            (name, type)
        ).fetchone()

        embedding = self._embed(f"{name} {content}") if embed else None

        if row:
            entity_id = row[0]
            conn.execute(
                "UPDATE entities SET content = ?, embedding = ?, updated_at = ?, metadata = ? WHERE id = ?",
                (content, embedding, now, meta_json, entity_id),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO entities (name, type, content, embedding, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, type, content, embedding, now, now, meta_json),
            )
            entity_id = cursor.lastrowid

        conn.commit()
        log.info(f"Entity {'updated' if row else 'added'}: {name} ({type}) id={entity_id}")
        return entity_id

    def get_entity(self, entity_id: int) -> Optional[Entity]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, name, type, content, created_at, updated_at, metadata FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if not row:
            return None
        return Entity(
            id=row[0], name=row[1], type=row[2], content=row[3],
            created_at=row[4], updated_at=row[5], metadata=json.loads(row[6] or "{}"),
        )

    def find_entity(self, name: str, type: Optional[str] = None) -> Optional[Entity]:
        conn = self._get_conn()
        if type:
            row = conn.execute(
                "SELECT id, name, type, content, created_at, updated_at, metadata FROM entities WHERE name = ? AND type = ?",
                (name, type),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name, type, content, created_at, updated_at, metadata FROM entities WHERE name = ?",
                (name,),
            ).fetchone()
        if not row:
            return None
        return Entity(
            id=row[0], name=row[1], type=row[2], content=row[3],
            created_at=row[4], updated_at=row[5], metadata=json.loads(row[6] or "{}"),
        )

    def delete_entity(self, entity_id: int) -> bool:
        conn = self._get_conn()
        conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        conn.commit()
        return conn.total_changes > 0

    # --- Relations ---

    def add_relation(self, from_id: int, to_id: int, relation_type: str,
                     metadata: Optional[dict] = None) -> int:
        if relation_type not in RELATION_TYPES:
            raise ValueError(f"Unknown relation type: {relation_type}. Valid: {RELATION_TYPES}")

        conn = self._get_conn()
        now = time.time()
        meta_json = json.dumps(metadata or {})

        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO relations (from_id, to_id, relation_type, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
                (from_id, to_id, relation_type, meta_json, now),
            )
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            log.warning(f"Relation already exists: {from_id} -{relation_type}-> {to_id}")
            return 0

    def get_relations(self, entity_id: int, direction: str = "both") -> list[dict]:
        """Get relations for an entity. direction: 'from', 'to', or 'both'."""
        conn = self._get_conn()
        results = []

        if direction in ("from", "both"):
            rows = conn.execute(
                """SELECT r.relation_type, e.id, e.name, e.type, r.metadata
                   FROM relations r JOIN entities e ON r.to_id = e.id
                   WHERE r.from_id = ?""",
                (entity_id,),
            ).fetchall()
            for row in rows:
                results.append({
                    "direction": "outgoing",
                    "relation": row[0],
                    "entity_id": row[1],
                    "entity_name": row[2],
                    "entity_type": row[3],
                    "metadata": json.loads(row[4] or "{}"),
                })

        if direction in ("to", "both"):
            rows = conn.execute(
                """SELECT r.relation_type, e.id, e.name, e.type, r.metadata
                   FROM relations r JOIN entities e ON r.from_id = e.id
                   WHERE r.to_id = ?""",
                (entity_id,),
            ).fetchall()
            for row in rows:
                results.append({
                    "direction": "incoming",
                    "relation": row[0],
                    "entity_id": row[1],
                    "entity_name": row[2],
                    "entity_type": row[3],
                    "metadata": json.loads(row[4] or "{}"),
                })

        return results

    # --- Search ---

    def query(self, text: str, max_results: int = 10, entity_type: Optional[str] = None) -> list[dict]:
        """Full-text search across entities."""
        conn = self._get_conn()

        if entity_type:
            rows = conn.execute(
                """SELECT e.id, e.name, e.type, e.content, e.updated_at, rank
                   FROM entities_fts f
                   JOIN entities e ON f.rowid = e.id
                   WHERE entities_fts MATCH ? AND e.type = ?
                   ORDER BY rank LIMIT ?""",
                (text, entity_type, max_results),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT e.id, e.name, e.type, e.content, e.updated_at, rank
                   FROM entities_fts f
                   JOIN entities e ON f.rowid = e.id
                   WHERE entities_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (text, max_results),
            ).fetchall()

        return [
            {
                "id": row[0], "name": row[1], "type": row[2],
                "content": row[3][:300], "updated_at": row[4],
                "score": -row[5],  # FTS5 rank is negative
            }
            for row in rows
        ]

    def semantic_search(self, text: str, max_results: int = 10) -> list[dict]:
        """Semantic search using embeddings (numpy-accelerated cosine similarity)."""
        query_embedding = self._get_embed_fn()(text)
        if query_embedding is None:
            return self.query(text, max_results)  # fallback to FTS

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, name, type, content, embedding, updated_at FROM entities "
            "WHERE embedding IS NOT NULL LIMIT 10000"
        ).fetchall()

        if not rows:
            return []

        try:
            import numpy as np

            ids = []
            embeddings = []
            meta = []
            for row in rows:
                try:
                    vec = json.loads(row[4])
                    ids.append(row[0])
                    embeddings.append(vec)
                    meta.append((row[1], row[2], row[3], row[5]))
                except Exception:
                    continue

            if not embeddings:
                return []

            matrix = np.array(embeddings, dtype=np.float32)
            q = np.array(query_embedding, dtype=np.float32)

            # Batch cosine similarity via matrix multiply
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            matrix_norm = matrix / (norms + 1e-9)
            q_norm = q / (np.linalg.norm(q) + 1e-9)
            scores = matrix_norm @ q_norm

            top_indices = np.argsort(scores)[::-1][:max_results]
            return [
                {
                    "id": ids[i], "name": meta[i][0], "type": meta[i][1],
                    "content": meta[i][2][:300], "updated_at": meta[i][3],
                    "score": float(scores[i]),
                }
                for i in top_indices
            ]

        except ImportError:
            # Fallback: pure Python (slower)
            import math
            scored = []
            for row in rows:
                try:
                    stored = json.loads(row[4])
                    dot = sum(a * b for a, b in zip(query_embedding, stored))
                    norm_q = math.sqrt(sum(a * a for a in query_embedding))
                    norm_s = math.sqrt(sum(a * a for a in stored))
                    sim = dot / (norm_q * norm_s) if norm_q and norm_s else 0
                    scored.append({
                        "id": row[0], "name": row[1], "type": row[2],
                        "content": row[3][:300], "updated_at": row[5],
                        "score": sim,
                    })
                except Exception:
                    continue
            scored.sort(key=lambda x: x["score"], reverse=True)
            return scored[:max_results]

    # --- Graph traversal ---

    def traverse(self, start_id: int, relation_type: Optional[str] = None,
                 depth: int = 2) -> list[dict]:
        """BFS traversal from an entity."""
        conn = self._get_conn()
        visited = set()
        queue = [(start_id, 0)]
        results = []

        while queue:
            entity_id, d = queue.pop(0)
            if entity_id in visited or d > depth:
                continue
            visited.add(entity_id)

            entity = self.get_entity(entity_id)
            if entity:
                results.append({
                    **entity.to_dict(),
                    "depth": d,
                })

            # Get outgoing relations
            if relation_type:
                rows = conn.execute(
                    "SELECT r.to_id FROM relations r WHERE r.from_id = ? AND r.relation_type = ?",
                    (entity_id, relation_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT r.to_id FROM relations r WHERE r.from_id = ?",
                    (entity_id,),
                ).fetchall()

            for row in rows:
                if row[0] not in visited:
                    queue.append((row[0], d + 1))

        return results

    # --- Temporal ---

    def timeline(self, since: Optional[float] = None, limit: int = 50) -> list[dict]:
        """Get entities ordered by update time."""
        conn = self._get_conn()
        since = since or 0
        rows = conn.execute(
            "SELECT id, name, type, content, created_at, updated_at FROM entities WHERE updated_at > ? ORDER BY updated_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()

        return [
            {
                "id": row[0], "name": row[1], "type": row[2],
                "content": row[3][:200], "created_at": row[4], "updated_at": row[5],
            }
            for row in rows
        ]

    # --- Context ---

    def context(self, name: str, max_depth: int = 2) -> dict:
        """Full context for a topic: entity + relations + related entities."""
        entity = self.find_entity(name)
        if not entity:
            # Try FTS
            results = self.query(name, max_results=1)
            if results:
                entity = self.get_entity(results[0]["id"])

        if not entity:
            return {"error": f"No entity found for '{name}'"}

        relations = self.get_relations(entity.id)
        graph = self.traverse(entity.id, depth=max_depth)

        return {
            "entity": entity.to_dict(),
            "relations": relations,
            "graph": graph,
        }

    # --- Graph visualization ---

    def get_graph(self, center: Optional[str] = None, depth: int = 2,
                  limit: int = 100) -> dict:
        """Return nodes + edges for visualization.

        If center is given, returns the subgraph around that entity.
        Otherwise returns the most recent entities.
        """
        conn = self._get_conn()

        if center:
            entity = self.find_entity(center)
            if not entity:
                results = self.query(center, max_results=1)
                if results:
                    entity = self.get_entity(results[0]["id"])
            if not entity:
                return {"nodes": [], "edges": []}

            graph = self.traverse(entity.id, depth=depth)
            entity_ids = {g["id"] for g in graph}
        else:
            rows = conn.execute(
                "SELECT id FROM entities ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            entity_ids = {r[0] for r in rows}
            graph = []
            for eid in entity_ids:
                e = self.get_entity(eid)
                if e:
                    graph.append({**e.to_dict(), "depth": 0})

        nodes = [
            {"id": g["id"], "name": g["name"], "type": g["type"],
             "depth": g.get("depth", 0)}
            for g in graph
        ]

        # Get all edges between these entities
        if entity_ids:
            placeholders = ",".join("?" * len(entity_ids))
            edge_rows = conn.execute(
                f"SELECT from_id, to_id, relation_type FROM relations WHERE from_id IN ({placeholders}) AND to_id IN ({placeholders})",
                list(entity_ids) + list(entity_ids),
            ).fetchall()
            edges = [
                {"from": r[0], "to": r[1], "relation": r[2]}
                for r in edge_rows
            ]
        else:
            edges = []

        return {"nodes": nodes, "edges": edges}

    # --- Bulk import ---

    def import_notes(self, notes_dir: Path) -> int:
        """Import existing notes as entities."""
        if not notes_dir.exists():
            return 0

        count = 0
        for f in notes_dir.iterdir():
            if f.is_file():
                content = f.read_text()
                self.add_entity(
                    name=f.stem,
                    type="learning",
                    content=content,
                    metadata={"source": "notes", "file": str(f)},
                    embed=True,
                )
                count += 1
        return count

    # --- Stats ---

    def stats(self) -> dict:
        conn = self._get_conn()
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        relation_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]

        type_counts = {}
        for row in conn.execute("SELECT type, COUNT(*) FROM entities GROUP BY type"):
            type_counts[row[0]] = row[1]

        return {
            "total_entities": entity_count,
            "total_relations": relation_count,
            "entities_by_type": type_counts,
        }

    def rebuild_fts_index(self) -> int:
        """Rebuild the FTS5 index from scratch.

        Use this if the FTS index gets out of sync with the entities table
        (e.g., after a crash during a write). Returns the number of entities
        re-indexed.
        """
        conn = self._get_conn()
        # Drop and recreate the FTS table + triggers
        conn.execute("DROP TABLE IF EXISTS entities_fts")
        conn.execute("DROP TRIGGER IF EXISTS entities_ai")
        conn.execute("DROP TRIGGER IF EXISTS entities_ad")
        conn.execute("DROP TRIGGER IF EXISTS entities_au")
        conn.executescript(FTS_SCHEMA)
        # Repopulate from entities table
        conn.execute(
            "INSERT INTO entities_fts(rowid, name, content, type) "
            "SELECT id, name, content, type FROM entities"
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        log.info("FTS5 index rebuilt: %d entities re-indexed", count)
        return count
