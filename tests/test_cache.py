"""Tests for the response cache."""

import time

from localforge.cache import ResponseCache


def test_basic_put_and_get():
    cache = ResponseCache(ttl=60, max_entries=10)
    key = cache.make_key("hello", "system", "model")
    cache.put(key, "world")
    assert cache.get(key) == "world"


def test_cache_miss():
    cache = ResponseCache(ttl=60, max_entries=10)
    assert cache.get("nonexistent") is None


def test_ttl_expiration():
    cache = ResponseCache(ttl=0.1, max_entries=10)
    key = cache.make_key("prompt", None, "model")
    cache.put(key, "response")
    assert cache.get(key) == "response"

    time.sleep(0.15)
    assert cache.get(key) is None


def test_max_entries_eviction():
    cache = ResponseCache(ttl=60, max_entries=3)
    for i in range(5):
        cache.put(f"key-{i}", f"val-{i}")

    # Oldest entries should have been evicted
    assert len(cache._store) <= 3


def test_hit_miss_counts():
    cache = ResponseCache(ttl=60, max_entries=10)
    key = "test-key"
    cache.put(key, "val")

    cache.get(key)  # hit
    cache.get(key)  # hit
    cache.get("miss")  # miss

    assert cache.hits >= 2
    assert cache.misses >= 1


def test_clear():
    cache = ResponseCache(ttl=60, max_entries=10)
    cache.put("a", "1")
    cache.put("b", "2")
    cache.clear()
    assert cache.get("a") is None
    assert cache.get("b") is None


def test_make_key_deterministic():
    cache = ResponseCache(ttl=60, max_entries=10)
    k1 = cache.make_key("prompt", "system", "model")
    k2 = cache.make_key("prompt", "system", "model")
    assert k1 == k2


def test_make_key_varies_with_input():
    cache = ResponseCache(ttl=60, max_entries=10)
    k1 = cache.make_key("prompt-a", "system", "model")
    k2 = cache.make_key("prompt-b", "system", "model")
    assert k1 != k2


def test_lru_eviction_order():
    """LRU eviction should remove least-recently-accessed entries first."""
    cache = ResponseCache(ttl=60, max_entries=3)
    cache.put("a", "alpha")
    cache.put("b", "beta")
    cache.put("c", "gamma")

    # Access 'a' to make it recently used
    assert cache.get("a") == "alpha"

    # Adding 'd' should evict 'b' (least recently accessed), not 'a'
    cache.put("d", "delta")
    assert cache.size == 3
    assert cache.get("a") == "alpha"  # still there (recently accessed)
    assert cache.get("b") is None  # evicted (LRU)
    assert cache.get("c") is not None or cache.get("d") is not None  # one of these exists


def test_stats_include_hit_rate():
    cache = ResponseCache(ttl=60, max_entries=10)
    cache.put("k", "v")
    cache.get("k")  # hit
    cache.get("miss")  # miss
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert "50" in stats["hit_rate"]
