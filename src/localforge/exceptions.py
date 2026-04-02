"""Custom exceptions for LocalForge.

Provides a clear hierarchy instead of broad 'except Exception' catches.
"""


class LocalForgeError(Exception):
    """Base exception for all LocalForge errors."""


class BackendError(LocalForgeError):
    """Error communicating with a backend (text-gen-webui, llama.cpp, etc.)."""


class BackendUnreachableError(BackendError):
    """Backend is not responding at all (connection refused, timeout)."""


class ModelNotLoadedError(BackendError):
    """No model is currently loaded in the backend."""


class ConfigError(LocalForgeError):
    """Configuration file is missing, malformed, or has invalid values."""


class AuthError(LocalForgeError):
    """Authentication or authorization failure."""


class IndexError(LocalForgeError):
    """Error with RAG index operations (create, search, delete)."""


class WorkflowError(LocalForgeError):
    """Error executing a workflow or pipeline."""


class AgentError(LocalForgeError):
    """Error in agent lifecycle or execution."""
