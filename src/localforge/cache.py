"""Response cache for LocalForge.

Simple in-memory hash-based cache with TTL eviction.
Avoids redundant model calls for identical prompts within a session.
"""

import hashlib
import time
from typing import Any


class ResponseCache:
    """In-memory response cache with TTL and LRU eviction."""

    def __init__(self, ttl: int = 300, max_size: int = 200):
        self._store: dict[str, tuple[str, float]] = {}
        self._ttl = ttl
        self._max_size = max_size
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(prompt: str, system: str | None, model: str | None, **kwargs: Any) -> str:
        """Generate a cache key from prompt + system + model + gen_params."""
        key_data = f"{model}:{system}:{prompt}:{sorted(kwargs.items())}"
        return hashlib.sha256(key_data.encode()).hexdigest()[:16]

    def get(self, key: str) -> str | None:
        """Get cached response, or None if missing/expired."""
        if key in self._store:
            response, ts = self._store[key]
            if time.time() - ts < self._ttl:
                self.hits += 1
                return response
            else:
                del self._store[key]
        self.misses += 1
        return None

    def put(self, key: str, response: str) -> None:
        """Store a response in cache, evicting oldest if full."""
        self._store[key] = (response, time.time())
        if len(self._store) > self._max_size:
            oldest = min(self._store, key=lambda k: self._store[k][1])
            del self._store[oldest]

    def clear(self) -> None:
        """Clear all cached responses."""
        self._store.clear()
        self.hits = 0
        self.misses = 0

    @property
    def size(self) -> int:
        return len(self._store)

    def stats(self) -> dict[str, Any]:
        """Return cache statistics."""
        total = self.hits + self.misses
        return {
            "size": self.size,
            "max_size": self._max_size,
            "ttl": self._ttl,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": f"{self.hits / total * 100:.1f}%" if total > 0 else "0%",
        }
