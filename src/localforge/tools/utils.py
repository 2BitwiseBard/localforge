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


# ---------------------------------------------------------------------------
# Workspace allowlist (used by fs_* and shell_exec tools)
# ---------------------------------------------------------------------------

DEFAULT_WORKSPACES: tuple[str, ...] = ("~/Development",)


def workspace_roots() -> list[Path]:
    """Return the configured workspace roots, realpath-resolved.

    Read from ``tool_workspaces`` in config.yaml. Defaults to ``["~/Development"]``
    when the key is **absent**. An explicitly-empty list disables all fs/shell
    tool access (the validator returns "no tool_workspaces configured").
    """
    if "tool_workspaces" in cfg._config:
        raw = cfg._config["tool_workspaces"]
    else:
        raw = list(DEFAULT_WORKSPACES)
    if not isinstance(raw, list):
        raw = [raw]
    roots: list[Path] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            continue
        try:
            resolved = Path(os.path.realpath(os.path.expanduser(entry)))
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def validate_workspace_path(
    raw_path: str,
    *,
    must_exist: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> tuple[Path, str | None]:
    """Validate that ``raw_path`` resolves inside a configured workspace.

    Uses ``os.path.realpath`` to resolve symlinks AND ``..`` traversal before
    checking the allowlist — neither escape works.

    Returns (resolved_path, error_message). On error, the path may not exist;
    callers should not use it.
    """
    if not raw_path or not isinstance(raw_path, str):
        return Path(), "Error: path is required"

    resolved = Path(os.path.realpath(os.path.expanduser(raw_path)))

    roots = workspace_roots()
    if not roots:
        return resolved, "Error: no tool_workspaces configured"

    in_workspace = False
    for root in roots:
        try:
            resolved.relative_to(root)
            in_workspace = True
            break
        except ValueError:
            continue
    if not in_workspace:
        roots_str = ", ".join(str(r) for r in roots)
        return resolved, f"Error: path outside workspace (allowed roots: {roots_str})"

    if must_exist and not resolved.exists():
        return resolved, f"Error: path not found: {resolved}"
    if must_be_file and resolved.exists() and not resolved.is_file():
        return resolved, f"Error: not a file: {resolved}"
    if must_be_dir and resolved.exists() and not resolved.is_dir():
        return resolved, f"Error: not a directory: {resolved}"

    return resolved, None


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
