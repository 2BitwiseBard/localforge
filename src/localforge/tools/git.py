"""Git context assembly tool."""

import asyncio
import os
from pathlib import Path

from localforge.tools import tool_handler


async def _run_git(*args: str, cwd: str | None = None) -> str:
    """Run a git command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return f"(git error: {stderr.decode().strip()})"
    return stdout.decode().strip()


@tool_handler(
    name="git_context",
    description=(
        "Assemble git context for the current directory: branch, recent commits, "
        "staged/unstaged changes, and optionally blame for specific files. "
        "Returns a pre-formatted context blob ready to include in prompts."
    ),
    schema={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Git repo directory (default: cwd)"},
            "log_count": {"type": "integer", "description": "Number of recent commits to include (default: 5)"},
            "include_diff": {"type": "boolean", "description": "Include staged + unstaged diff (default: true)"},
            "blame_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to include git blame for (optional)",
            },
        },
        "required": [],
    },
)
async def git_context(args: dict) -> str:
    directory = args.get("directory", ".")
    dir_path = Path(os.path.expanduser(directory)).resolve()
    cwd = str(dir_path)

    log_count = args.get("log_count", 5)
    include_diff = args.get("include_diff", True)
    blame_files = args.get("blame_files", [])

    parts = []

    branch = await _run_git("branch", "--show-current", cwd=cwd)
    parts.append(f"Branch: {branch}")

    log_output = await _run_git(
        "log", "--oneline", f"-{log_count}", "--no-decorate",
        cwd=cwd,
    )
    if log_output and not log_output.startswith("(git error"):
        parts.append(f"\nRecent commits:\n{log_output}")

    status = await _run_git("status", "--short", cwd=cwd)
    if status and not status.startswith("(git error"):
        parts.append(f"\nStatus:\n{status}")

    if include_diff:
        staged = await _run_git("diff", "--staged", "--stat", cwd=cwd)
        if staged and not staged.startswith("(git error"):
            parts.append(f"\nStaged changes:\n{staged}")

        unstaged = await _run_git("diff", "--stat", cwd=cwd)
        if unstaged and not unstaged.startswith("(git error"):
            parts.append(f"\nUnstaged changes:\n{unstaged}")

        full_diff = await _run_git("diff", cwd=cwd)
        staged_diff = await _run_git("diff", "--staged", cwd=cwd)
        combined_diff = (staged_diff + "\n" + full_diff).strip()
        if combined_diff and not combined_diff.startswith("(git error"):
            if len(combined_diff) > 10000:
                combined_diff = combined_diff[:10000] + "\n... (truncated)"
            parts.append(f"\nDiff:\n```diff\n{combined_diff}\n```")

    for bf in blame_files[:3]:
        blame = await _run_git("blame", "--line-porcelain", bf, cwd=cwd)
        if blame and not blame.startswith("(git error"):
            summary_lines = []
            for line in blame.splitlines():
                if line.startswith("author "):
                    summary_lines.append(line)
                elif line.startswith("summary "):
                    summary_lines.append(line)
            if summary_lines:
                parts.append(f"\nBlame for {bf}:\n" + "\n".join(summary_lines[:20]))

    return "\n".join(parts)
