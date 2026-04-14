"""Tests for authentication and rate limiting."""

import time

from localforge.auth import _check_key, _check_rate_limit, _rate_buckets


class TestCheckKey:
    def test_plaintext_match(self):
        assert _check_key("my-secret", "my-secret") is True

    def test_plaintext_mismatch(self):
        assert _check_key("wrong", "my-secret") is False

    def test_bcrypt_match(self):
        import bcrypt
        hashed = bcrypt.hashpw(b"test-key-123", bcrypt.gensalt()).decode()
        assert _check_key("test-key-123", hashed) is True

    def test_bcrypt_mismatch(self):
        import bcrypt
        hashed = bcrypt.hashpw(b"correct-key", bcrypt.gensalt()).decode()
        assert _check_key("wrong-key", hashed) is False

    def test_empty_key(self):
        assert _check_key("", "") is True
        assert _check_key("something", "") is False


class TestRateLimit:
    def setup_method(self):
        _rate_buckets.clear()

    def test_allows_normal_traffic(self):
        for _ in range(10):
            assert _check_rate_limit("192.168.1.1") is True

    def test_blocks_after_burst(self):
        # Exhaust the bucket (RATE_LIMIT + RATE_BURST = 80 requests)
        ip = "10.0.0.99"
        allowed = 0
        for _ in range(200):
            if _check_rate_limit(ip):
                allowed += 1
        # Should have allowed ~80 (RATE_LIMIT + RATE_BURST) and blocked the rest
        assert allowed < 200
        assert allowed >= 60  # at least RATE_LIMIT worth

    def test_different_ips_independent(self):
        for _ in range(50):
            _check_rate_limit("ip-a")
        # ip-b should still be fresh
        assert _check_rate_limit("ip-b") is True
