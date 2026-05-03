"""shell_exec — run a shell command inside a workspace.

FULL-trust + approval queue. A configurable denylist rejects dangerous patterns
before any approval prompt is surfaced (the human shouldn't be asked to approve
``sudo`` or fork bombs).
"""

import asyncio
import logging
import re

from localforge import config as cfg
from localforge.tools import tool_handler
from localforge.tools.utils import validate_workspace_path

log = logging.getLogger("localforge.tools.shell")

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
OUTPUT_TRUNCATE = 4000

SHELL_DENY_DEFAULTS: tuple[str, ...] = (
    r"\brm\s+-rf\s+/(?!\w)",                       # rm -rf / and rm -rf /<space>
    r"\bsudo\b",                                   # any sudo
    r":\s*\(\s*\)\s*\{.*:\s*\|\s*:\s*&.*\}",       # fork bomb
    r"curl\s+[^|]*\|\s*(bash|sh)\b",               # curl ... | bash
    r"wget\s+[^|]*\|\s*(bash|sh)\b",               # wget ... | bash
    r"\bdd\s+[^&|;]*\bof=/dev/(sd|nvme|hd)",       # dd to a block device
    r">\s*/dev/(sd|nvme|hd)",                      # redirect to a block device
    r"\bmkfs(\.\w+)?\b",                           # filesystem creation
)


def _denylist() -> list[str]:
    extra = cfg._config.get("shell_deny") or []
    if not isinstance(extra, list):
        extra = [extra]
    return [p for p in (*SHELL_DENY_DEFAULTS, *extra) if isinstance(p, str) and p]


def _check_deny(command: str) -> str | None:
    """Return the first deny-pattern that matches, or None."""
    for pattern in _denylist():
        try:
            if re.search(pattern, command):
                return pattern
        except re.error:
            log.warning("Invalid shell_deny pattern (skipped): %s", pattern)
    return None


def _truncate(text: str) -> str:
    if len(text) <= OUTPUT_TRUNCATE:
        return text
    return text[:OUTPUT_TRUNCATE] + f"\n... (truncated at {OUTPUT_TRUNCATE} chars)"


@tool_handler(
    name="shell_exec",
    description=(
        "Run a shell command via /bin/bash -c inside a workspace directory. "
        "Captures stdout, stderr, and exit code (combined output truncated at "
        "4000 chars). Requires approval at FULL trust. A denylist rejects "
        "obvious-destructive patterns (sudo, rm -rf /, curl|bash, mkfs, etc.) "
        "before approval is requested."
    ),
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Bash command line"},
            "cwd": {"type": "string", "description": "Working directory (must be inside a workspace)"},
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT})",
            },
        },
        "required": ["command", "cwd"],
    },
)
async def shell_exec(args: dict) -> str:
    command = args.get("command", "")
    raw_cwd = args.get("cwd", "")
    timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
    timeout = max(1, min(timeout, MAX_TIMEOUT))

    if not isinstance(command, str) or not command.strip():
        return "Error: command is required"

    matched = _check_deny(command)
    if matched:
        log.warning("shell_exec rejected by denylist (%s): %s", matched, command)
        return f"Rejected by shell_deny pattern: {matched}"

    cwd, err = validate_workspace_path(raw_cwd, must_exist=True, must_be_dir=True)
    if err:
        return err

    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return f"Error: failed to spawn /bin/bash: {e}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
        return f"Error: command timed out after {timeout}s"

    out = stdout.decode("utf-8", errors="replace")
    err_text = stderr.decode("utf-8", errors="replace")

    parts = [f"$ {command}", f"(cwd={cwd}, exit={proc.returncode})"]
    if out:
        parts.append(f"stdout:\n{out.rstrip()}")
    if err_text:
        parts.append(f"stderr:\n{err_text.rstrip()}")
    if not out and not err_text:
        parts.append("(no output)")
    return _truncate("\n".join(parts))
