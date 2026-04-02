"""Research session persistence for multi-step research workflows.

Uses the same knowledge.db as the KG (separate table).
Tracks sources, findings, credibility scores, and synthesis.
"""

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("research-sessions")

DB_PATH = Path(__file__).parent.parent / "knowledge.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_sessions (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    findings TEXT DEFAULT '[]',
    synthesis TEXT DEFAULT '',
    follow_up_urls TEXT DEFAULT '[]',
    kg_entity_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_research_status ON research_sessions(status, updated_at);
"""

# Domain credibility tiers
HIGH_TRUST_DOMAINS = {
    "arxiv.org", "github.com", "docs.python.org", "docs.rs",
    "developer.mozilla.org", "stackoverflow.com", "en.wikipedia.org",
    "pytorch.org", "huggingface.co", "openai.com", "anthropic.com",
}
LOW_TRUST_DOMAINS = {
    "pinterest.com", "quora.com", "medium.com",
}


def score_source(url: str, content: str = "") -> float:
    """Score a source 0.0-1.0 for credibility.

    Uses domain reputation + content signals.
    """
    score = 0.5

    try:
        domain = urlparse(url).netloc.lower()
        # Strip www.
        if domain.startswith("www."):
            domain = domain[4:]

        # Domain-level signals
        if domain in HIGH_TRUST_DOMAINS:
            score += 0.25
        elif domain in LOW_TRUST_DOMAINS:
            score -= 0.15
        if domain.endswith(".edu") or domain.endswith(".gov") or domain.endswith(".ac.uk"):
            score += 0.2
    except Exception:
        pass

    # Content signals
    if content:
        # Longer content is generally more substantive
        if len(content) > 2000:
            score += 0.1
        elif len(content) < 200:
            score -= 0.1

        # Code blocks suggest technical content
        if "```" in content or "    def " in content or "fn " in content:
            score += 0.05

    return min(1.0, max(0.0, round(score, 2)))


class ResearchSession:
    """Manages research session persistence."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def create(self, question: str) -> str:
        """Create a new research session. Returns session_id."""
        session_id = uuid.uuid4().hex[:12]
        conn = self._get_conn()
        now = time.time()
        conn.execute(
            "INSERT INTO research_sessions (id, question, status, created_at, updated_at) VALUES (?, ?, 'active', ?, ?)",
            (session_id, question, now, now),
        )
        conn.commit()
        log.info(f"Created research session {session_id}: {question[:80]}")
        return session_id

    def add_finding(self, session_id: str, url: str, title: str,
                    excerpt: str, credibility_score: float = 0.5):
        """Add a source finding to a session."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT findings FROM research_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return

        findings = json.loads(row[0])
        findings.append({
            "url": url,
            "title": title,
            "excerpt": excerpt[:1000],
            "credibility": credibility_score,
            "found_at": time.time(),
        })
        conn.execute(
            "UPDATE research_sessions SET findings = ?, updated_at = ? WHERE id = ?",
            (json.dumps(findings), time.time(), session_id),
        )
        conn.commit()

    def update_synthesis(self, session_id: str, synthesis: str):
        """Update the synthesis text for a session."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE research_sessions SET synthesis = ?, updated_at = ? WHERE id = ?",
            (synthesis, time.time(), session_id),
        )
        conn.commit()

    def add_follow_up(self, session_id: str, url: str):
        """Add a follow-up URL to crawl."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT follow_up_urls FROM research_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return

        urls = json.loads(row[0])
        if url not in urls:
            urls.append(url)
            conn.execute(
                "UPDATE research_sessions SET follow_up_urls = ?, updated_at = ? WHERE id = ?",
                (json.dumps(urls), time.time(), session_id),
            )
            conn.commit()

    def get(self, session_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, question, status, created_at, updated_at, findings, synthesis, follow_up_urls, kg_entity_id FROM research_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "question": row[1], "status": row[2],
            "created_at": row[3], "updated_at": row[4],
            "findings": json.loads(row[5]),
            "synthesis": row[6],
            "follow_up_urls": json.loads(row[7]),
            "kg_entity_id": row[8],
        }

    def list_sessions(self, status: Optional[str] = None, limit: int = 50) -> list[dict]:
        conn = self._get_conn()
        if status:
            rows = conn.execute(
                "SELECT id, question, status, created_at, updated_at, findings FROM research_sessions WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, question, status, created_at, updated_at, findings FROM research_sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [
            {
                "id": r[0], "question": r[1], "status": r[2],
                "created_at": r[3], "updated_at": r[4],
                "finding_count": len(json.loads(r[5])),
            }
            for r in rows
        ]

    def complete(self, session_id: str, kg_entity_id: Optional[int] = None):
        conn = self._get_conn()
        conn.execute(
            "UPDATE research_sessions SET status = 'complete', updated_at = ?, kg_entity_id = ? WHERE id = ?",
            (time.time(), kg_entity_id, session_id),
        )
        conn.commit()

    def abandon(self, session_id: str):
        conn = self._get_conn()
        conn.execute(
            "UPDATE research_sessions SET status = 'abandoned', updated_at = ? WHERE id = ?",
            (time.time(), session_id),
        )
        conn.commit()
