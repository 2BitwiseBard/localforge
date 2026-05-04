"""Tests for path resolution."""

import os
from pathlib import Path
from unittest.mock import patch

import localforge.paths as _paths_mod
from localforge.paths import (
    agent_state_dir,
    data_dir,
    fastembed_cache_dir,
    indexes_dir,
    notes_dir,
    pipelines_dir,
    sessions_dir,
)


def _reset_cache():
    """Reset the cached _DATA_DIR so tests can override the env var."""
    _paths_mod._DATA_DIR = None


def test_default_data_dir():
    """Default data dir is ~/.local/share/localforge/."""
    _reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LOCALFORGE_DATA_DIR", None)
        d = data_dir()
        assert d.name == "localforge"
        assert ".local/share" in str(d) or "localforge" in str(d)


def test_custom_data_dir():
    """LOCALFORGE_DATA_DIR overrides the default."""
    _reset_cache()
    with patch.dict(os.environ, {"LOCALFORGE_DATA_DIR": "/tmp/forge-test"}):
        d = data_dir()
        assert d == Path("/tmp/forge-test")


def test_subdirs_under_data_dir():
    """All subdirectories resolve under data_dir()."""
    _reset_cache()
    with patch.dict(os.environ, {"LOCALFORGE_DATA_DIR": "/tmp/forge-test"}):
        assert str(notes_dir()).startswith("/tmp/forge-test")
        _reset_cache()  # each call may re-cache
    _reset_cache()
    with patch.dict(os.environ, {"LOCALFORGE_DATA_DIR": "/tmp/forge-test"}):
        assert str(indexes_dir()).startswith("/tmp/forge-test")
        _reset_cache()
    _reset_cache()
    with patch.dict(os.environ, {"LOCALFORGE_DATA_DIR": "/tmp/forge-test"}):
        assert str(sessions_dir()).startswith("/tmp/forge-test")
        _reset_cache()
    _reset_cache()
    with patch.dict(os.environ, {"LOCALFORGE_DATA_DIR": "/tmp/forge-test"}):
        assert str(pipelines_dir()).startswith("/tmp/forge-test")
        _reset_cache()
    _reset_cache()
    with patch.dict(os.environ, {"LOCALFORGE_DATA_DIR": "/tmp/forge-test"}):
        assert str(agent_state_dir()).startswith("/tmp/forge-test")
        _reset_cache()


def test_fastembed_cache_dir():
    """Fastembed cache dir exists as a path."""
    d = fastembed_cache_dir()
    assert isinstance(d, Path)
