"""Tests for consolidated config loading (auth.py + config.py + gateway.py + routes.py)."""

import time

from localforge.config import _load_config, load_config_cached, _config_cache


class TestLoadConfigCached:
    def test_returns_dict(self):
        """load_config_cached always returns a dict (even if config.yaml missing)."""
        result = load_config_cached()
        assert isinstance(result, dict)

    def test_cache_hit(self, monkeypatch):
        """Second call within TTL returns cached value without re-reading."""
        import localforge.config as cfg

        # Prime the cache
        cfg._config_cache = (time.monotonic(), {"test": "cached"})
        result = load_config_cached()
        assert result == {"test": "cached"}

    def test_cache_expired(self, monkeypatch):
        """Expired cache triggers a fresh read."""
        import localforge.config as cfg

        # Set cache to expired (far in the past)
        cfg._config_cache = (0.0, {"stale": True})
        result = load_config_cached()
        # Should get a fresh read (which may be empty if no config.yaml)
        assert isinstance(result, dict)
        # Cache should be updated
        assert cfg._config_cache[0] > 0.0

    def test_auth_uses_shared_loader(self):
        """auth.py's _load_config is the same as config.py's load_config_cached."""
        from localforge.auth import _load_config as auth_load
        assert auth_load is load_config_cached


class TestAgentTimeout:
    def test_default_timeout_attribute(self):
        """AgentSupervisor has a default timeout of 3600s."""
        from localforge.agents.supervisor import AgentSupervisor
        sup = AgentSupervisor(gateway_url="http://localhost:8100", api_key="test")
        assert sup._default_agent_timeout == 3600


class TestRequestBodyLimitMiddleware:
    def test_large_body_paths_defined(self):
        """Upload routes are in the LARGE_BODY_PATHS set."""
        from localforge.gateway import LARGE_BODY_PATHS, MAX_BODY_BYTES, LARGE_BODY_LIMIT
        assert "/api/photos/upload" in LARGE_BODY_PATHS
        assert "/api/videos/upload" in LARGE_BODY_PATHS
        assert MAX_BODY_BYTES == 1 * 1024 * 1024
        assert LARGE_BODY_LIMIT == 50 * 1024 * 1024
