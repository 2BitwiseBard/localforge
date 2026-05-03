"""Parallel execution tools — fan_out, parallel_file_review, quality_sweep.

When mesh workers are available, tasks are distributed across the mesh.
Otherwise, everything runs on the hub model.
"""

import asyncio
import logging
from typing import Any

from localforge import config as cfg
from localforge.client import _chat_to_backend, resolve_model
from localforge.tools import tool_handler

log = logging.getLogger("localforge")


def _get_mesh_worker_urls() -> list[str]:
    """Return URLs of healthy mesh workers with inference capability."""
    try:
        from localforge.tools.compute import _get_healthy_worker_urls

        return _get_healthy_worker_urls("inference")
    except ImportError:
        return []


async def _chat_to_mesh_or_hub(body: dict, worker_url: str | None = None) -> str:
    """Send a chat request to a mesh worker if available, otherwise to the hub.

    Args:
        body: OpenAI-compatible chat completion request body.
        worker_url: If provided, dispatch to this specific worker.
                    If None, use the hub model.
    """
    if worker_url:
        try:
            import httpx

            # Convert chat body to worker task format
            payload = {
                "type": "chat",
                "messages": body.get("messages", []),
                "max_tokens": body.get("max_tokens", 1024),
                "temperature": body.get("temperature", 0.7),
            }
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(f"{worker_url}/task", json=payload)
                result = resp.json()
                if "error" in result:
                    log.debug("Mesh worker %s failed: %s, falling back to hub", worker_url, result["error"])
                else:
                    return result.get("response", "")
        except Exception as e:
            log.debug("Mesh dispatch to %s failed: %s, falling back to hub", worker_url, e)

    # Fallback to hub
    return await _chat_to_backend(cfg.TGWUI_BASE, body)


async def _local_analyze_one(
    file_path: str, concern: str, preamble: str | None, gen_params: dict[str, Any], worker_url: str | None = None
) -> dict[str, str]:
    """Analyze a single file for a concern. Returns {file, verdict, details}.

    If worker_url is provided, dispatches to that mesh worker instead of the hub.
    """
    from localforge.tools.utils import validate_file_path

    path, err = validate_file_path(file_path)
    if err:
        return {"file": file_path, "verdict": "SKIP", "details": err.replace("Error: ", "")}

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
        result = await _chat_to_mesh_or_hub(body, worker_url=worker_url)
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

    # Get mesh workers for distribution
    mesh_workers = _get_mesh_worker_urls()
    using_mesh = len(mesh_workers) > 0

    async def _run_one(idx: int, prompt: str, worker_url: str | None = None) -> tuple[int, str]:
        messages: list[dict[str, str]] = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": cfg.MODEL or "", "messages": messages, "stream": False, **gen_params}
        try:
            result = await _chat_to_mesh_or_hub(body, worker_url=worker_url)
            return (idx, result)
        except Exception as e:
            return (idx, f"Error: {e}")

    # Distribute across mesh workers (round-robin) if available, else all go to hub
    tasks = []
    for i, p in enumerate(prompts):
        worker = mesh_workers[i % len(mesh_workers)] if mesh_workers else None
        tasks.append(_run_one(i, p, worker_url=worker))

    dispatch_target = f"{len(mesh_workers)} mesh workers" if using_mesh else "hub"
    log.info("fan_out: dispatching %d prompts across %s", len(prompts), dispatch_target)
    results = await asyncio.gather(*tasks)
    results_sorted = sorted(results, key=lambda x: x[0])

    parts = []
    if using_mesh:
        parts.append(f"Distributed across {len(mesh_workers)} mesh workers\n")
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
            "concern": {
                "type": "string",
                "description": "What to check for (e.g. 'error handling', 'security issues')",
            },
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

    mesh_workers = _get_mesh_worker_urls()
    dispatch_target = f"{len(mesh_workers)} mesh workers" if mesh_workers else "hub"
    log.info("parallel_file_review: %d files for '%s' across %s", len(file_paths), concern, dispatch_target)
    tasks = []
    for i, fp in enumerate(file_paths):
        worker = mesh_workers[i % len(mesh_workers)] if mesh_workers else None
        tasks.append(_local_analyze_one(fp, concern, preamble, gen_params, worker_url=worker))
    results = await asyncio.gather(*tasks)

    passes = sum(1 for r in results if r["verdict"] == "PASS")
    fails = sum(1 for r in results if r["verdict"] == "FAIL")
    skips = sum(1 for r in results if r["verdict"] in ("SKIP", "ERROR"))

    lines = [f"Reviewed {len(results)} files for: {concern}", f"Results: {passes} PASS, {fails} FAIL, {skips} SKIP\n"]

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
            "glob_pattern": {
                "type": "string",
                "description": "Glob pattern for files (e.g. '*.rs', '*.py', '**/*.ts')",
            },
            "criterion": {"type": "string", "description": "Quality criterion to check"},
            "max_files": {"type": "integer", "description": "Max files to check (default: 20)"},
        },
        "required": ["directory", "glob_pattern", "criterion"],
    },
)
async def quality_sweep(args: dict) -> str:
    if cfg.MODEL is None:
        cfg.MODEL = await resolve_model()

    from localforge.tools.utils import validate_directory

    directory, err = validate_directory(args["directory"])
    if err:
        return err

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

    mesh_workers = _get_mesh_worker_urls()
    dispatch_target = f"{len(mesh_workers)} mesh workers" if mesh_workers else "hub"
    log.info("quality_sweep: %d files in %s for '%s' across %s", len(files), directory, criterion, dispatch_target)
    tasks = []
    for i, f in enumerate(files):
        worker = mesh_workers[i % len(mesh_workers)] if mesh_workers else None
        tasks.append(_local_analyze_one(str(f), criterion, preamble, gen_params, worker_url=worker))
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
