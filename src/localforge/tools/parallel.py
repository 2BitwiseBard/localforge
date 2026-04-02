"""Parallel execution tools — fan_out, parallel_file_review, quality_sweep."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from localforge import config as cfg
from localforge.client import resolve_model, _chat_to_backend
from localforge.tools import tool_handler

log = logging.getLogger("localforge")


async def _local_analyze_one(file_path: str, concern: str, preamble: str | None,
                              gen_params: dict[str, Any]) -> dict[str, str]:
    """Analyze a single file for a concern. Returns {file, verdict, details}."""
    path = Path(os.path.expanduser(file_path)).resolve()
    home = Path.home().resolve()
    if not (str(path).startswith(str(home)) or str(path).startswith("/tmp")):
        return {"file": file_path, "verdict": "SKIP", "details": "outside allowed paths"}
    if not path.exists():
        return {"file": file_path, "verdict": "SKIP", "details": "file not found"}
    if not path.is_file():
        return {"file": file_path, "verdict": "SKIP", "details": "not a file"}

    size = path.stat().st_size
    if size > 100_000:
        return {"file": file_path, "verdict": "SKIP", "details": f"too large ({size:,} bytes)"}

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"file": file_path, "verdict": "SKIP", "details": str(e)}

    prompt = (
        f"Analyze the following code for: {concern}\n"
        f"File: {path.name}\n\n"
        f"Be concise. List specific issues with line numbers.\n"
        f"If no issues found, say 'No issues found.'\n\n"
        f"```\n{content}\n```"
    )

    suffix = cfg.get_system_suffix(cfg.MODEL)
    effective_system = preamble
    if suffix:
        effective_system = f"{preamble}\n\n{suffix}" if preamble else suffix

    messages: list[dict[str, str]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, Any] = {"model": cfg.MODEL or "", "messages": messages, "stream": False, **gen_params}

    try:
        result = await _chat_to_backend(cfg.TGWUI_BASE, body)
        has_issues = "no issues" not in result.lower()
        return {
            "file": path.name,
            "verdict": "FAIL" if has_issues else "PASS",
            "details": result,
        }
    except Exception as e:
        return {"file": file_path, "verdict": "ERROR", "details": str(e)}


@tool_handler(
    name="fan_out",
    description=(
        "Run multiple prompts through the local model in parallel. "
        "100%% local — zero API costs. Uses asyncio.gather for concurrent execution. "
        "Returns all results labeled by index."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of prompts to run in parallel",
            },
            "system": {"type": "string", "description": "Optional shared system message for all prompts"},
        },
        "required": ["prompts"],
    },
)
async def fan_out(args: dict) -> str:
    if cfg.MODEL is None:
        cfg.MODEL = await resolve_model()

    prompts = args["prompts"]
    if not prompts:
        return "Error: provide at least one prompt"
    if len(prompts) > 20:
        return f"Error: max 20 prompts (got {len(prompts)}). Split into batches."

    system = args.get("system") or cfg.get_system_preamble()
    suffix = cfg.get_system_suffix(cfg.MODEL)
    effective_system = system
    if suffix:
        effective_system = f"{system}\n\n{suffix}" if system else suffix

    gen_params = cfg.get_generation_params(cfg.MODEL)

    async def _run_one(idx: int, prompt: str) -> tuple[int, str]:
        messages: list[dict[str, str]] = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": cfg.MODEL or "", "messages": messages, "stream": False, **gen_params}
        try:
            result = await _chat_to_backend(cfg.TGWUI_BASE, body)
            return (idx, result)
        except Exception as e:
            return (idx, f"Error: {e}")

    log.info("fan_out: dispatching %d prompts in parallel", len(prompts))
    tasks = [_run_one(i, p) for i, p in enumerate(prompts)]
    results = await asyncio.gather(*tasks)
    results_sorted = sorted(results, key=lambda x: x[0])

    parts = []
    for idx, result in results_sorted:
        prompt_preview = prompts[idx][:60] + "..." if len(prompts[idx]) > 60 else prompts[idx]
        parts.append(f"--- [{idx + 1}] {prompt_preview} ---\n{result}")

    return "\n\n".join(parts)


@tool_handler(
    name="parallel_file_review",
    description=(
        "Review multiple files for a specific concern — all in parallel on the local model. "
        "100%% local, zero API costs. Reads each file, analyzes it, returns pass/fail per file."
    ),
    schema={
        "type": "object",
        "properties": {
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of file paths to review",
            },
            "concern": {"type": "string", "description": "What to check for (e.g. 'error handling', 'security issues')"},
        },
        "required": ["file_paths", "concern"],
    },
)
async def parallel_file_review(args: dict) -> str:
    if cfg.MODEL is None:
        cfg.MODEL = await resolve_model()

    file_paths = args["file_paths"]
    concern = args["concern"]

    if not file_paths:
        return "Error: provide at least one file path"
    if len(file_paths) > 30:
        return f"Error: max 30 files (got {len(file_paths)}). Split into batches."

    preamble = cfg.get_system_preamble()
    gen_params = cfg.get_generation_params(cfg.MODEL)

    log.info("parallel_file_review: %d files for '%s'", len(file_paths), concern)
    tasks = [_local_analyze_one(fp, concern, preamble, gen_params) for fp in file_paths]
    results = await asyncio.gather(*tasks)

    passes = sum(1 for r in results if r["verdict"] == "PASS")
    fails = sum(1 for r in results if r["verdict"] == "FAIL")
    skips = sum(1 for r in results if r["verdict"] in ("SKIP", "ERROR"))

    lines = [f"Reviewed {len(results)} files for: {concern}",
             f"Results: {passes} PASS, {fails} FAIL, {skips} SKIP\n"]

    for r in results:
        marker = {"PASS": "ok", "FAIL": "ISSUE", "SKIP": "skip", "ERROR": "err"}.get(r["verdict"], "?")
        lines.append(f"[{marker}] {r['file']}")
        if r["verdict"] == "FAIL":
            for detail_line in r["details"].splitlines()[:5]:
                lines.append(f"     {detail_line}")
            if len(r["details"].splitlines()) > 5:
                lines.append(f"     ... ({len(r['details'].splitlines()) - 5} more lines)")

    return "\n".join(lines)


@tool_handler(
    name="quality_sweep",
    description=(
        "Sweep a directory for a quality criterion. Finds files matching a glob pattern, "
        "analyzes each in parallel on the local model, returns pass/fail summary. "
        "100%% local, zero API costs."
    ),
    schema={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Directory to sweep (absolute or ~ relative)"},
            "glob_pattern": {"type": "string", "description": "Glob pattern for files (e.g. '*.rs', '*.py', '**/*.ts')"},
            "criterion": {"type": "string", "description": "Quality criterion to check"},
            "max_files": {"type": "integer", "description": "Max files to check (default: 20)"},
        },
        "required": ["directory", "glob_pattern", "criterion"],
    },
)
async def quality_sweep(args: dict) -> str:
    if cfg.MODEL is None:
        cfg.MODEL = await resolve_model()

    directory = Path(os.path.expanduser(args["directory"])).resolve()
    home = Path.home().resolve()
    if not (str(directory).startswith(str(home)) or str(directory).startswith("/tmp")):
        return f"Error: directory must be under {home} or /tmp"
    if not directory.exists():
        return f"Error: directory not found: {directory}"

    glob_pattern = args["glob_pattern"]
    criterion = args["criterion"]
    max_files = args.get("max_files", 20)

    files = sorted(directory.glob(glob_pattern))
    files = [f for f in files if f.is_file() and "/." not in str(f)]

    if not files:
        return f"No files matching '{glob_pattern}' in {directory}"

    truncated = len(files) > max_files
    if truncated:
        files = files[:max_files]

    preamble = cfg.get_system_preamble()
    gen_params = cfg.get_generation_params(cfg.MODEL)

    log.info("quality_sweep: %d files in %s for '%s'", len(files), directory, criterion)
    tasks = [_local_analyze_one(str(f), criterion, preamble, gen_params) for f in files]
    results = await asyncio.gather(*tasks)

    passes = sum(1 for r in results if r["verdict"] == "PASS")
    fails = sum(1 for r in results if r["verdict"] == "FAIL")
    skips = sum(1 for r in results if r["verdict"] in ("SKIP", "ERROR"))

    lines = [
        f"Quality sweep: {criterion}",
        f"Directory: {directory}",
        f"Pattern: {glob_pattern} ({len(files)} files{', truncated' if truncated else ''})",
        f"Results: {passes} PASS, {fails} FAIL, {skips} SKIP",
        "",
    ]

    for r in results:
        if r["verdict"] == "FAIL":
            lines.append(f"FAIL {r['file']}")
            for detail_line in r["details"].splitlines()[:3]:
                lines.append(f"     {detail_line}")

    pass_files = [r["file"] for r in results if r["verdict"] == "PASS"]
    if pass_files:
        lines.append(f"\nPASS: {', '.join(pass_files)}")

    skip_files = [f"{r['file']} ({r['details']})" for r in results if r["verdict"] in ("SKIP", "ERROR")]
    if skip_files:
        lines.append(f"\nSKIP: {', '.join(skip_files)}")

    return "\n".join(lines)
