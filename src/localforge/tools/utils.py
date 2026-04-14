"""Shared utilities for tool handlers.

Deduplicates common patterns: file path validation, system message building,
and error response formatting.
"""

import os
from pathlib import Path
from typing import Any

from localforge import config as cfg


def validate_file_path(raw_path: str, max_size: int = 100_000) -> tuple[Path, str | None]:
    """Validate and resolve a user-provided file path.

    Returns (resolved_path, error_message). If error_message is not None,
    the path is invalid and the error should be returned to the caller.

    Checks:
      - Path is under $HOME or /tmp
      - Path exists and is a regular file
      - File size is within max_size bytes
    """
    path = Path(os.path.expanduser(raw_path)).resolve()
    home = Path.home().resolve()

    if not (str(path).startswith(str(home)) or str(path).startswith("/tmp")):
        return path, f"Error: file must be under {home} or /tmp"
    if not path.exists():
        return path, f"Error: file not found: {path}"
    if not path.is_file():
        return path, f"Error: not a file: {path}"

    size = path.stat().st_size
    if size > max_size:
        return path, f"Error: file too large ({size:,} bytes, max {max_size // 1000}KB)"

    return path, None


def validate_directory(raw_path: str) -> tuple[Path, str | None]:
    """Validate and resolve a user-provided directory path.

    Returns (resolved_path, error_message).
    """
    path = Path(os.path.expanduser(raw_path)).resolve()
    home = Path.home().resolve()

    if not (str(path).startswith(str(home)) or str(path).startswith("/tmp")):
        return path, f"Error: directory must be under {home} or /tmp"
    if not path.exists():
        return path, f"Error: directory not found: {path}"
    if not path.is_dir():
        return path, f"Error: not a directory: {path}"

    return path, None


def build_system_message(system: str | None = None) -> str | None:
    """Build the effective system message with preamble and model suffix.

    Merges: explicit system message (or preamble) + model-specific suffix.
    """
    effective = system or cfg.get_system_preamble()
    suffix = cfg.get_system_suffix(cfg.MODEL)
    if suffix:
        if effective:
            return f"{effective}\n\n{suffix}"
        return suffix
    return effective


def build_chat_body(
    prompt: str,
    system: str | None = None,
    **extra_params: Any,
) -> dict[str, Any]:
    """Build a complete chat completion request body.

    Handles system message construction, generation param merging,
    and model name inclusion.
    """
    effective_system = build_system_message(system)
    messages: list[dict[str, str]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": prompt})

    gen_params = cfg.get_generation_params(cfg.MODEL)
    gen_params.update(extra_params)

    return {
        "model": cfg.MODEL or "",
        "messages": messages,
        "stream": False,
        **gen_params,
    }


def error_response(msg: str, status: int = 500) -> dict[str, Any]:
    """Standardized API error response format."""
    return {"error": msg, "status": status}
