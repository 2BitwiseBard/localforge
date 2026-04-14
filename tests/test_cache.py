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
