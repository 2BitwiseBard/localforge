"""Tests for the exception hierarchy."""

import pytest

from localforge.exceptions import (
    LocalForgeError,
    BackendError,
    BackendUnreachableError,
    ModelNotLoadedError,
    ConfigError,
    AuthError,
    WorkflowError,
    AgentError,
)


def test_hierarchy():
    """All custom exceptions inherit from LocalForgeError."""
    assert issubclass(BackendError, LocalForgeError)
    assert issubclass(BackendUnreachableError, BackendError)
    assert issubclass(ModelNotLoadedError, BackendError)
    assert issubclass(ConfigError, LocalForgeError)
    assert issubclass(AuthError, LocalForgeError)
    assert issubclass(WorkflowError, LocalForgeError)
    assert issubclass(AgentError, LocalForgeError)


def test_catch_base():
    """Catching LocalForgeError catches all subclasses."""
    with pytest.raises(LocalForgeError):
        raise BackendUnreachableError("gone")


def test_catch_backend():
    """Catching BackendError catches unreachable and model-not-loaded."""
    with pytest.raises(BackendError):
        raise ModelNotLoadedError("no model")


def test_message():
    e = BackendUnreachableError("Cannot connect to localhost:5000")
    assert "localhost:5000" in str(e)
