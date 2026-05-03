"""Filesystem tools — read, list, glob, grep, write, edit, delete.

All paths are sandboxed to the configured ``tool_workspaces`` (default:
``~/Development``). Resolution uses ``os.path.realpath``, which collapses
symlinks AND ``..`` traversal before the allowlist check.

SAFE-trust tools: fs_read, fs_list, fs_glob, fs_grep
FULL-trust + approval: fs_write, fs_edit, fs_delete
"""

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from localforge.tools import tool_handler
from localforge.tools.utils import validate_workspace_path

log = logging.getLogger("localforge.tools.fs")

MAX_READ_LINES = 2000
MAX_READ_BYTES = 256 * 1024
GLOB_RESULT_CAP = 500
GREP_DEFAULT_MAX = 100


# ---------------------------------------------------------------------------
# fs_read
# ---------------------------------------------------------------------------

@tool_handler(
    name="fs_read",
    description=(
        "Read a text file from a configured workspace. Returns the content "
        "with cat -n style line numbers. Caps output at 2000 lines or 256 KiB. "
        "Use offset/limit to page through larger files."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or ~-prefixed path"},
            "offset": {"type": "integer", "description": "1-based starting line (default 1)"},
            "limit": {"type": "integer", "description": "Max lines (default 2000)"},
        },
        "required": ["path"],
    },
)
async def fs_read(args: dict) -> str:
    raw_path = args.get("path", "")
    offset = max(1, int(args.get("offset", 1) or 1))
    limit = int(args.get("limit", MAX_READ_LINES) or MAX_READ_LINES)
    limit = min(limit, MAX_READ_LINES)

    path, err = validate_workspace_path(raw_path, must_exist=True, must_be_file=True)
    if err:
        return err

    try:
        size = path.stat().st_size
    except OSError as e:
        return f"Error: stat failed: {e}"
    if size > MAX_READ_BYTES:
        return (
            f"Error: file too large ({size:,} bytes, max {MAX_READ_BYTES // 1024} KiB). "
            "Use offset/limit to read in chunks via fs_grep or split externally."
        )

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"Error: read failed: {e}"

    total = len(lines)
    start = offset - 1
    end = min(start + limit, total)
    selected = lines[start:end]

    width = len(str(end if end else 1))
    out: list[str] = []
    for i, line in enumerate(selected, start=offset):
        out.append(f"{i:>{width}}\t{line.rstrip(chr(10))}")

    truncated = end < total
    body = "\n".join(out)
    if truncated:
        body += f"\n... (truncated at line {end} of {total})"
    return body or "(empty file)"


# ---------------------------------------------------------------------------
# fs_list
# ---------------------------------------------------------------------------

@tool_handler(
    name="fs_list",
    description=(
        "List directory entries (non-recursive). Returns one line per entry: "
        "type (f/d/l), size in bytes (- for non-files), name."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path"},
        },
        "required": ["path"],
    },
)
async def fs_list(args: dict) -> str:
    raw_path = args.get("path", "")
    path, err = validate_workspace_path(raw_path, must_exist=True, must_be_dir=True)
    if err:
        return err

    try:
        entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        return f"Error: scandir failed: {e}"

    if not entries:
        return f"(empty directory: {path})"

    lines = [f"Directory: {path}"]
    for entry in entries:
        if entry.is_symlink():
            kind = "l"
        elif entry.is_dir():
            kind = "d"
        elif entry.is_file():
            kind = "f"
        else:
            kind = "?"
        try:
            size = entry.stat(follow_symlinks=False).st_size if kind == "f" else None
        except OSError:
            size = None
        size_str = f"{size:>10,}" if size is not None else " " * 10 + "-"
        lines.append(f"{kind} {size_str}  {entry.name}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# fs_glob
# ---------------------------------------------------------------------------

@tool_handler(
    name="fs_glob",
    description=(
        "Glob a pattern (e.g. '**/*.py') under a workspace root. Returns one "
        "path per line, capped at 500 entries."
    ),
    schema={
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "Directory to glob from"},
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
        },
        "required": ["root", "pattern"],
    },
)
async def fs_glob(args: dict) -> str:
    raw_root = args.get("root", "")
    pattern = args.get("pattern", "")
    if not pattern:
        return "Error: pattern is required"

    root, err = validate_workspace_path(raw_root, must_exist=True, must_be_dir=True)
    if err:
        return err

    try:
        matches: list[Path] = []
        for p in root.glob(pattern):
            matches.append(p)
            if len(matches) > GLOB_RESULT_CAP:
                break
    except (OSError, ValueError) as e:
        return f"Error: glob failed: {e}"

    if not matches:
        return f"(no matches for {pattern!r} under {root})"

    truncated = len(matches) > GLOB_RESULT_CAP
    matches = matches[:GLOB_RESULT_CAP]
    body = "\n".join(str(p) for p in matches)
    if truncated:
        body += f"\n... (truncated at {GLOB_RESULT_CAP} matches)"
    return body


# ---------------------------------------------------------------------------
# fs_grep
# ---------------------------------------------------------------------------

async def _ripgrep(pattern: str, path: Path, glob: str | None, max_count: int) -> str:
    rg = shutil.which("rg")
    if not rg:
        return ""
    cmd = [rg, "--no-heading", "-n", "--color=never", "-m", str(max_count)]
    if glob:
        cmd += ["--glob", glob]
    cmd += [pattern, str(path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 1:  # no matches
        return f"(no matches for {pattern!r} under {path})"
    if proc.returncode > 1:
        return f"Error: rg failed: {stderr.decode(errors='replace').strip()}"
    return stdout.decode(errors="replace").rstrip()


def _python_grep(pattern: str, path: Path, glob: str | None, max_count: int) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"

    matches: list[str] = []
    iterator = path.rglob(glob) if glob else path.rglob("*")
    for fp in iterator:
        if not fp.is_file():
            continue
        try:
            with fp.open("r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    if regex.search(line):
                        matches.append(f"{fp}:{lineno}:{line.rstrip(chr(10))}")
                        if len(matches) >= max_count:
                            break
        except OSError:
            continue
        if len(matches) >= max_count:
            break

    if not matches:
        return f"(no matches for {pattern!r} under {path})"
    return "\n".join(matches)


@tool_handler(
    name="fs_grep",
    description=(
        "Search a workspace for a regex pattern. Uses ripgrep when available, "
        "falls back to a pure-Python walk. Returns lines as path:lineno:text, "
        "capped at max_count (default 100)."
    ),
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern"},
            "path": {"type": "string", "description": "Directory or file to search"},
            "glob": {"type": "string", "description": "Optional glob filter, e.g. '*.py'"},
            "max_count": {"type": "integer", "description": "Max matches (default 100)"},
        },
        "required": ["pattern", "path"],
    },
)
async def fs_grep(args: dict) -> str:
    pattern = args.get("pattern", "")
    raw_path = args.get("path", "")
    glob = args.get("glob") or None
    max_count = max(1, int(args.get("max_count", GREP_DEFAULT_MAX) or GREP_DEFAULT_MAX))

    if not pattern:
        return "Error: pattern is required"

    path, err = validate_workspace_path(raw_path, must_exist=True)
    if err:
        return err

    rg_result = await _ripgrep(pattern, path, glob, max_count)
    if rg_result:
        return rg_result
    return _python_grep(pattern, path, glob, max_count)


# ---------------------------------------------------------------------------
# fs_write
# ---------------------------------------------------------------------------

@tool_handler(
    name="fs_write",
    description=(
        "Create or overwrite a file inside a workspace. Parent directories "
        "must already exist. Requires approval at FULL trust."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
)
async def fs_write(args: dict) -> str:
    raw_path = args.get("path", "")
    content = args.get("content", "")
    if not isinstance(content, str):
        return "Error: content must be a string"

    path, err = validate_workspace_path(raw_path)
    if err:
        return err
    if path.exists() and not path.is_file():
        return f"Error: not a file: {path}"
    if not path.parent.exists():
        return f"Error: parent directory does not exist: {path.parent}"

    try:
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"Error: write failed: {e}"

    return f"Wrote {len(content)} bytes to {path}"


# ---------------------------------------------------------------------------
# fs_edit
# ---------------------------------------------------------------------------

@tool_handler(
    name="fs_edit",
    description=(
        "Find/replace edit on an existing file. Default mode requires "
        "old_string to occur exactly once. Set replace_all=true to replace "
        "every occurrence. Requires approval at FULL trust."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
        },
        "required": ["path", "old_string", "new_string"],
    },
)
async def fs_edit(args: dict) -> str:
    raw_path = args.get("path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))

    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return "Error: old_string and new_string must be strings"
    if old_string == "":
        return "Error: old_string must be non-empty"
    if old_string == new_string:
        return "Error: old_string and new_string are identical"

    path, err = validate_workspace_path(raw_path, must_exist=True, must_be_file=True)
    if err:
        return err

    try:
        original = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"Error: read failed: {e}"

    occurrences = original.count(old_string)
    if occurrences == 0:
        return f"Error: old_string not found in {path}"
    if occurrences > 1 and not replace_all:
        return (
            f"Error: old_string found {occurrences} times in {path}. "
            "Make it unique or pass replace_all=true."
        )

    if replace_all:
        updated = original.replace(old_string, new_string)
    else:
        updated = original.replace(old_string, new_string, 1)

    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as e:
        return f"Error: write failed: {e}"

    return f"Edited {path}: replaced {occurrences if replace_all else 1} occurrence(s)"


# ---------------------------------------------------------------------------
# fs_delete
# ---------------------------------------------------------------------------

@tool_handler(
    name="fs_delete",
    description=(
        "Delete a single file inside a workspace. Refuses directories. "
        "Requires approval at FULL trust."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
    },
)
async def fs_delete(args: dict) -> str:
    raw_path = args.get("path", "")
    path, err = validate_workspace_path(raw_path, must_exist=True, must_be_file=True)
    if err:
        return err

    try:
        path.unlink()
    except OSError as e:
        return f"Error: unlink failed: {e}"

    return f"Deleted {path}"
