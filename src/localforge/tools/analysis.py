"""Code analysis tools."""

import asyncio
import base64
import logging
import os
from pathlib import Path
from typing import Any

from localforge import config as cfg
from localforge.client import _chat_to_backend, chat, check_backend_health, resolve_model, task_type_context
from localforge.tools import tool_handler

log = logging.getLogger("localforge")


@tool_handler(
    name="analyze_code",
    description="Analyze a code snippet for issues, patterns, or improvements using the local model",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to analyze"},
            "query": {"type": "string", "description": "What to look for (e.g. 'error handling gaps', 'performance issues')"},
            "language": {"type": "string", "description": "Language hint (optional, overrides context)"},
        },
        "required": ["code", "query"],
    },
)
async def analyze_code(args: dict) -> str:
    lang = args.get("language", cfg._context.get("language", ""))
    lang_hint = f" ({lang})" if lang else ""
    prompt = (
        f"Analyze the following code{lang_hint} for: {args['query']}\n\n"
        f"Be concise. List specific line numbers and issues.\n\n"
        f"```\n{args['code']}\n```"
    )
    async with task_type_context("code"):
        return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="batch_review",
    description="Review multiple code snippets for a consistent concern. Results returned together.",
    schema={
        "type": "object",
        "properties": {
            "snippets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of code snippets to review",
            },
            "concern": {"type": "string", "description": "What to check each snippet for"},
        },
        "required": ["snippets", "concern"],
    },
)
async def batch_review(args: dict) -> str:
    snippets = args["snippets"]
    concern = args["concern"]

    healthy_backends: list[str] = []
    for name in sorted(cfg._backends, key=lambda n: cfg._backends[n]["priority"]):
        if await check_backend_health(name):
            healthy_backends.append(name)

    if len(healthy_backends) <= 1 or len(snippets) <= 1:
        numbered = "\n\n---\n\n".join(
            f"Snippet {i+1}:\n```\n{s}\n```" for i, s in enumerate(snippets)
        )
        prompt = (
            f"For each snippet below, check for: {concern}\n"
            f"Label each response 'Snippet N:' and be concise.\n\n{numbered}"
        )
        return await chat(prompt, system=cfg.get_system_preamble())

    log.info("Parallel batch_review: %d snippets across %d backends", len(snippets), len(healthy_backends))
    n_backends = len(healthy_backends)
    chunks: list[list[tuple[int, str]]] = [[] for _ in range(n_backends)]
    for i, snippet in enumerate(snippets):
        chunks[i % n_backends].append((i, snippet))

    preamble = cfg.get_system_preamble()
    suffix = cfg.get_system_suffix(cfg.MODEL)
    gen_params = cfg.get_generation_params(cfg.MODEL)

    async def _review_chunk(backend_name: str, chunk: list[tuple[int, str]]) -> list[tuple[int, str]]:
        numbered = "\n\n---\n\n".join(
            f"Snippet {idx+1}:\n```\n{s}\n```" for idx, s in chunk
        )
        prompt = (
            f"For each snippet below, check for: {concern}\n"
            f"Label each response 'Snippet N:' and be concise.\n\n{numbered}"
        )
        effective_system = preamble
        if suffix:
            effective_system = f"{preamble}\n\n{suffix}" if preamble else suffix
        messages: list[dict[str, str]] = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.append({"role": "user", "content": prompt})
        body = {"model": cfg.MODEL or "", "messages": messages, "stream": False, **gen_params}
        url = cfg._backends[backend_name]["url"]
        try:
            result = await _chat_to_backend(url, body)
            return [(chunk[0][0], result)]
        except Exception as e:
            log.warning("Backend %s failed during batch_review: %s", backend_name, e)
            return [(chunk[0][0], f"(Backend {backend_name} failed: {e})")]

    tasks = [
        _review_chunk(healthy_backends[i], chunk)
        for i, chunk in enumerate(chunks)
        if chunk
    ]
    results = await asyncio.gather(*tasks)
    all_results = []
    for result_list in results:
        all_results.extend(result_list)
    all_results.sort(key=lambda x: x[0])
    return "\n\n".join(text for _, text in all_results)


@tool_handler(
    name="summarize_file",
    description="Generate a structural summary of a source file: types, functions, signatures, etc.",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to summarize"},
            "file_path": {"type": "string", "description": "Optional file path for context"},
        },
        "required": ["code"],
    },
)
async def summarize_file(args: dict) -> str:
    path_note = f" ({args['file_path']})" if args.get("file_path") else ""
    prompt = (
        f"Summarize the structure of this source file{path_note}. List:\n"
        f"- Public types (structs/classes/enums) with field counts\n"
        f"- Interfaces/traits and their methods\n"
        f"- Public methods and functions (with signatures)\n"
        f"- Notable constants, type aliases, or exports\n\n"
        f"Be concise. Use a structured format.\n\n"
        f"```\n{args['code']}\n```"
    )
    async with task_type_context("code"):
        return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="explain_error",
    description="Explain a compiler/linter/runtime error in plain English and suggest a fix",
    schema={
        "type": "object",
        "properties": {
            "error": {"type": "string", "description": "The error message"},
            "context": {"type": "string", "description": "Optional surrounding source code for context"},
        },
        "required": ["error"],
    },
)
async def explain_error(args: dict) -> str:
    context_block = ""
    if args.get("context"):
        context_block = f"\n\nRelevant code:\n```\n{args['context']}\n```"
    prompt = (
        f"Explain this error in plain English, then suggest a concrete fix.\n\n"
        f"Error:\n```\n{args['error']}\n```{context_block}\n\n"
        f"Format: 1) What it means  2) Why it happens  3) How to fix it (with code)"
    )
    async with task_type_context("code"):
        return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="file_qa",
    description=(
        "Read a file from disk and ask the local model a question about it. "
        "Saves you from pasting file contents. Supports text files up to 100KB."
    ),
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the file (absolute or ~ relative)"},
            "question": {"type": "string", "description": "Question to ask about the file"},
            "line_range": {"type": "string", "description": "Optional line range, e.g. '10-50'"},
        },
        "required": ["file_path", "question"],
    },
)
async def file_qa(args: dict) -> str:
    from localforge.tools.utils import validate_file_path

    file_path, err = validate_file_path(args["file_path"])
    if err:
        return err

    content = file_path.read_text(encoding="utf-8", errors="replace")

    if args.get("line_range"):
        lines = content.splitlines()
        try:
            parts = args["line_range"].split("-")
            start = int(parts[0]) - 1
            end = int(parts[1]) if len(parts) > 1 else start + 1
            content = "\n".join(lines[start:end])
        except (ValueError, IndexError):
            return f"Error: invalid line_range '{args['line_range']}'. Use format: '10-50'"

    prompt = (
        f"File: {file_path.name}\n\n"
        f"```\n{content}\n```\n\n"
        f"Question: {args['question']}"
    )
    async with task_type_context("code"):
        return await chat(prompt, system=cfg.get_system_preamble())


@tool_handler(
    name="analyze_image",
    description=(
        "Send an image to the local vision model for analysis. "
        "Requires a vision-capable model (e.g. Qwen3-VL). "
        "Supports PNG, JPG, WEBP up to 10MB."
    ),
    schema={
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Path to the image file"},
            "question": {"type": "string", "description": "Question about the image (default: describe it)"},
        },
        "required": ["image_path"],
    },
)
async def analyze_image(args: dict) -> str:
    if cfg.MODEL is None:
        cfg.MODEL = await resolve_model()

    vision_keywords = ["vl", "vision", "visual"]
    is_vision = any(kw in (cfg.MODEL or "").lower() for kw in vision_keywords)
    if not is_vision:
        return (
            f"Current model ({cfg.MODEL}) is not vision-capable. "
            f"Load a vision model first, e.g.: swap_model(model_name='Qwen3-VL-30B-A3B-Instruct-UD-Q4_K_XL.gguf')"
        )

    from localforge.tools.utils import validate_file_path
    image_path, err = validate_file_path(args["image_path"], max_size=10_000_000)
    if err:
        return err

    suffix = image_path.suffix.lower()
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif"}
    mime_type = mime_map.get(suffix)
    if not mime_type:
        return f"Error: unsupported format '{suffix}'. Use PNG, JPG, or WEBP."

    image_data = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    question = args.get("question", "Describe this image in detail.")

    system = cfg.get_system_preamble()
    sys_suffix = cfg.get_system_suffix(cfg.MODEL)
    effective_system = system
    if sys_suffix:
        effective_system = f"{system}\n\n{sys_suffix}" if system else sys_suffix

    messages: list[dict[str, Any]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}},
            {"type": "text", "text": question},
        ],
    })

    gen_params = cfg.get_generation_params(cfg.MODEL)
    body: dict[str, Any] = {"model": cfg.MODEL, "messages": messages, "stream": False, **gen_params}
    async with task_type_context("vision"):
        return await _chat_to_backend(cfg.TGWUI_BASE, body)


@tool_handler(
    name="classify_task",
    description=(
        "Classify a task and recommend the best model from your inventory. "
        "Fast heuristic — no model call needed. Tells you which model to load."
    ),
    schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Description of the task"},
        },
        "required": ["task"],
    },
)
async def classify_task(args: dict) -> str:
    task = args["task"].lower()

    categories = {
        "code": {
            "keywords": ["code", "function", "implement", "debug", "refactor", "test",
                         "compile", "build", "fix bug", "write a", "programming", "api",
                         "endpoint", "class", "method", "syntax", "coder", "coding"],
            "models": [
                ("Qwen3-Coder-30B-A3B-Instruct-1M-UD-Q5_K_XL.gguf", "Best code model, 1M ctx, MoE"),
                ("Devstral-Small-2-24B-Instruct-2512-UD-Q5_K_XL.gguf", "Strong code, 24B dense"),
            ],
        },
        "reasoning": {
            "keywords": ["think", "reason", "plan", "architect", "design", "analyze",
                         "complex", "strategy", "trade-off", "compare", "evaluate",
                         "decision", "step by step", "chain of thought"],
            "models": [
                ("Qwen3.5-27B-UD-Q5_K_XL.gguf", "Dense 27B, best multi-step reasoning"),
            ],
        },
        "vision": {
            "keywords": ["image", "picture", "screenshot", "photo", "visual", "diagram",
                         "chart", "ui", "design", "look at", "see", "ocr"],
            "models": [
                ("Qwen3-VL-30B-A3B-Instruct-UD-Q4_K_XL.gguf", "Vision + instruction, MoE"),
            ],
        },
        "quick": {
            "keywords": ["quick", "simple", "short", "fast", "brief", "summary",
                         "tldr", "one-liner", "small", "tiny"],
            "models": [
                ("google_gemma-3n-E4B-it-Q8_0.gguf", "Fast, small footprint"),
                ("Qwen3.5-35B-A3B-UD-Q5_K_XL.gguf", "Primary model, MoE (fast enough)"),
            ],
        },
        "general": {
            "keywords": [],
            "models": [
                ("Qwen3.5-35B-A3B-UD-Q5_K_XL.gguf", "Primary model, good all-rounder"),
            ],
        },
    }

    scores = {cat: sum(1 for kw in info["keywords"] if kw in task)
              for cat, info in categories.items()}
    best_cat = max(scores, key=scores.get) if max(scores.values()) > 0 else "general"
    recommendations = categories[best_cat]["models"]

    current = cfg.MODEL or "(none)"
    current_match = any(m[0] in current for m in recommendations)

    lines = [f"Task type: {best_cat}"]
    if current_match:
        lines.append(f"Current model ({current}) is already a good fit!")
    else:
        lines.append(f"Current model: {current}")
        lines.append("\nRecommended:")
        for model_name, reason in recommendations:
            lines.append(f"  -> {model_name}")
            lines.append(f"     {reason}")
        lines.append(f"\nSwap: swap_model(model_name='{recommendations[0][0]}')")

    return "\n".join(lines)
