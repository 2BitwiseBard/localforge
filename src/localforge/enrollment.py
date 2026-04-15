"""Mesh onboarding — short-lived enrollment tokens + long-lived worker key registry.

Enrollment flow:
  1. Admin hits POST /api/mesh/enrollment-token -> mints a 10-minute bootstrap token.
  2. The one-liner install script embeds the token and the hub URL.
  3. On first boot the worker POSTs /api/mesh/register with {enrollment_token,
     hostname, platform, hardware} and receives a long-lived worker API key.
  4. Subsequent /api/mesh/heartbeat calls authenticate with that key (role=worker,
     scope=mesh).

Worker keys live at ``data_dir()/workers.json`` as bcrypt hashes; the plaintext
is only ever returned once, at registration time, and is never written to disk.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from threading import Lock

from localforge.paths import data_dir

log = logging.getLogger("localforge.enrollment")

# Defaults — tuned so the one-liner can be copy/pasted without the user racing a clock.
ENROLLMENT_TTL_SECONDS = 600           # 10 minutes
WORKER_KEY_BYTES = 32                  # 256-bit random, URL-safe
MAX_ACTIVE_TOKENS = 32                 # defense against runaway mint loops


# ---------------------------------------------------------------------------
# Short-lived enrollment tokens
# ---------------------------------------------------------------------------

class EnrollmentStore:
    """In-memory TTL store for bootstrap tokens. Single-process only."""

    def __init__(self, ttl_seconds: int = ENROLLMENT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._tokens: dict[str, dict] = {}
        self._lock = Lock()

    def mint(self, *, issued_by: str, note: str = "") -> dict:
        """Mint a new enrollment token. Returns {token, expires_at, issued_by}."""
        token = secrets.token_urlsafe(24)
        expires_at = time.time() + self._ttl
        with self._lock:
            self._evict_expired_locked()
            if len(self._tokens) >= MAX_ACTIVE_TOKENS:
                raise RuntimeError("Too many active enrollment tokens; wait for expiry")
            self._tokens[token] = {
                "token": token,
                "issued_by": issued_by,
                "note": note,
                "expires_at": expires_at,
                "issued_at": time.time(),
            }
        log.info("Enrollment token minted by %s (ttl=%ds, note=%r)", issued_by, self._ttl, note)
        return {"token": token, "expires_at": expires_at, "ttl_seconds": self._ttl, "issued_by": issued_by}

    def consume(self, token: str) -> dict | None:
        """Validate + burn a token. Returns the record if valid, None otherwise."""
        with self._lock:
            self._evict_expired_locked()
            record = self._tokens.pop(token, None)
        if record is None:
            return None
        if record["expires_at"] < time.time():
            return None
        return record

    def peek(self, token: str) -> dict | None:
        """Validate without consuming (used by GET /install-script)."""
        with self._lock:
            self._evict_expired_locked()
            record = self._tokens.get(token)
        if record is None or record["expires_at"] < time.time():
            return None
        return dict(record)

    def _evict_expired_locked(self) -> None:
        now = time.time()
        stale = [k for k, r in self._tokens.items() if r["expires_at"] < now]
        for k in stale:
            del self._tokens[k]


# ---------------------------------------------------------------------------
# Long-lived worker keys — bcrypt-hashed on disk
# ---------------------------------------------------------------------------

class WorkerRegistry:
    """File-backed registry of worker API keys.

    Storage layout at ``data_dir()/workers.json``::

        {
          "workers": {
            "<worker_id>": {
              "worker_id": "<worker_id>",
              "hostname": "...",
              "platform": "linux|darwin|win32|android",
              "api_key_hash": "$2b$...",
              "registered_at": 173...,
              "last_seen": 173...,
              "enrolled_by": "<admin_id>",
              "hardware": {...},
              "role": "worker",
              "scopes": ["mesh"]
            }
          }
        }
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (data_dir() / "workers.json")
        self._lock = Lock()
        self._cache: dict | None = None
        self._cache_mtime: float = 0.0

    # -- persistence ---------------------------------------------------------

    def _load(self) -> dict:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return {"workers": {}}
        if self._cache is not None and mtime == self._cache_mtime:
            return self._cache
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log.warning("workers.json unreadable (%s); starting empty", e)
            data = {"workers": {}}
        if "workers" not in data or not isinstance(data["workers"], dict):
            data = {"workers": {}}
        self._cache = data
        self._cache_mtime = mtime
        return data

    def _save(self, data: dict) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)
        try:
            self._cache_mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._cache_mtime = 0.0
        self._cache = data

    # -- API -----------------------------------------------------------------

    def register(self, *, hostname: str, platform: str, hardware: dict,
                 enrolled_by: str) -> tuple[str, str]:
        """Create a new worker entry and return (worker_id, plaintext_api_key).

        The plaintext key is only returned here; the stored form is a bcrypt hash.
        """
        import bcrypt

        plaintext = secrets.token_urlsafe(WORKER_KEY_BYTES)
        hashed = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()

        worker_id = self._mint_worker_id(hostname)
        now = time.time()
        record = {
            "worker_id": worker_id,
            "hostname": hostname,
            "platform": platform,
            "api_key_hash": hashed,
            "registered_at": now,
            "last_seen": now,
            "enrolled_by": enrolled_by,
            "hardware": hardware,
            "role": "worker",
            "scopes": ["mesh"],
        }
        with self._lock:
            data = self._load()
            data["workers"][worker_id] = record
            self._save(data)
        log.info("Worker registered: %s (hostname=%s, platform=%s, by=%s)",
                 worker_id, hostname, platform, enrolled_by)
        return worker_id, plaintext

    def find_by_key(self, token: str) -> dict | None:
        """Return the worker record whose bcrypt hash matches token, else None."""
        try:
            import bcrypt
        except ImportError:
            return None
        with self._lock:
            data = self._load()
            for record in data["workers"].values():
                try:
                    if bcrypt.checkpw(token.encode(), record["api_key_hash"].encode()):
                        return dict(record)
                except (ValueError, TypeError):
                    continue
        return None

    def touch(self, worker_id: str) -> None:
        """Bump last_seen for a worker. Called from /api/mesh/heartbeat."""
        with self._lock:
            data = self._load()
            record = data["workers"].get(worker_id)
            if record is None:
                return
            record["last_seen"] = time.time()
            self._save(data)

    def list_workers(self) -> list[dict]:
        with self._lock:
            data = self._load()
            return [
                {k: v for k, v in r.items() if k != "api_key_hash"}
                for r in data["workers"].values()
            ]

    def revoke(self, worker_id: str) -> bool:
        with self._lock:
            data = self._load()
            if worker_id not in data["workers"]:
                return False
            del data["workers"][worker_id]
            self._save(data)
        log.info("Worker revoked: %s", worker_id)
        return True

    # -- helpers -------------------------------------------------------------

    def _mint_worker_id(self, hostname: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in hostname)[:32] or "worker"
        suffix = secrets.token_hex(3)
        return f"{safe}-{suffix}"


# ---------------------------------------------------------------------------
# Module-level singletons (imported by routes + auth)
# ---------------------------------------------------------------------------

_enrollment_store = EnrollmentStore()
_worker_registry = WorkerRegistry()


def enrollment_store() -> EnrollmentStore:
    return _enrollment_store


def worker_registry() -> WorkerRegistry:
    return _worker_registry
