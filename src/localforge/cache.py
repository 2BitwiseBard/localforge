"""Response cache for LocalForge.

In-memory hash-based cache with TTL, max entry count, max total bytes,
and size-aware eviction.  Large responses are evicted first when the
byte budget is exceeded.
"""

import hashlib
import logging
import time
from typing import Any

log = logging.getLogger("localforge.cache")


def _load_cache_config() -> dict:
    """Read cache settings from config.yaml (if available)."""
    try:
        import yaml

        from localforge.paths import config_path

        with open(config_path()) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("cache", {})
    except Exception:
        return {}


class ResponseCache:
    """In-memory response cache with TTL, count limit, and byte budget."""

    def __init__(
        self,
        ttl: int | None = None,
        max_entries: int | None = None,
        max_bytes: int | None = None,
    ):
        # Load defaults from config, then apply explicit overrides
        ccfg = _load_cache_config()
        self._ttl = ttl if ttl is not None else ccfg.get("ttl", 300)
        self._max_entries = max_entries if max_entries is not None else ccfg.get("max_entries", 200)
        self._max_bytes = max_bytes if max_bytes is not None else ccfg.get("max_bytes", 50_000_000)  # 50MB

        # store: key -> (response, timestamp, byte_size)
        self._store: dict[str, tuple[str, float, int]] = {}
        self._total_bytes = 0
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(prompt: str, system: str | None, model: str | None, **kwargs: Any) -> str:
        """Generate a cache key from prompt + system + model + gen_params."""
        import json as _json

        key_data = f"{model}:{system}:{prompt}:{_json.dumps(kwargs, sort_keys=True, default=str)}"
        return hashlib.sha256(key_data.encode()).hexdigest()[:16]

    def get(self, key: str) -> str | None:
        """Get cached response, or None if missing/expired."""
        if key in self._store:
            response, ts, size = self._store[key]
            if time.time() - ts < self._ttl:
                self.hits += 1
                return response
            else:
                self._total_bytes -= size
                del self._store[key]
        self.misses += 1
        return None

    def put(self, key: str, response: str) -> None:
        """Store a response, evicting entries if limits exceeded."""
        size = len(response.encode("utf-8", errors="replace"))

        # If this single entry exceeds the byte budget, don't cache it
        if size > self._max_bytes:
            log.debug("Response too large to cache (%d bytes)", size)
            return

        # Remove old version if overwriting
        if key in self._store:
            self._total_bytes -= self._store[key][2]

        self._store[key] = (response, time.time(), size)
        self._total_bytes += size

        # Evict expired entries first
        self._evict_expired()

        # Evict by count (oldest first)
        while len(self._store) > self._max_entries:
            self._evict_oldest()

        # Evict by byte budget (largest first)
        while self._total_bytes > self._max_bytes and self._store:
            self._evict_largest()

    def _evict_expired(self) -> None:
        """Remove all expired entries."""
        now = time.time()
        expired = [k for k, (_, ts, _) in self._store.items() if now - ts >= self._ttl]
        for k in expired:
            self._total_bytes -= self._store[k][2]
            del self._store[k]

    def _evict_oldest(self) -> None:
        """Remove the oldest entry."""
        if not self._store:
            return
        oldest = min(self._store, key=lambda k: self._store[k][1])
        self._total_bytes -= self._store[oldest][2]
        del self._store[oldest]

    def _evict_largest(self) -> None:
        """Remove the largest entry (by response byte size)."""
        if not self._store:
            return
        largest = max(self._store, key=lambda k: self._store[k][2])
        self._total_bytes -= self._store[largest][2]
        del self._store[largest]

    def clear(self) -> None:
        """Clear all cached responses."""
        self._store.clear()
        self._total_bytes = 0
        self.hits = 0
        self.misses = 0

    @property
    def size(self) -> int:
        return len(self._store)

    def stats(self) -> dict[str, Any]:
        """Return cache statistics."""
        total = self.hits + self.misses
        return {
            "entries": self.size,
            "max_entries": self._max_entries,
            "total_bytes": self._total_bytes,
            "max_bytes": self._max_bytes,
            "ttl": self._ttl,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": f"{self.hits / total * 100:.1f}%" if total > 0 else "0%",
        }
