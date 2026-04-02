"""Infrastructure, model management, and stats tools."""

import logging
from pathlib import Path
from typing import Any

from localforge import config as cfg
from localforge.client import (
    _client, _cache, _session_stats,
    resolve_model, check_backend_health, chat,
)
from localforge.tools import tool_handler

log = logging.getLogger("localforge")


@tool_handler(
    name="health_check",
    description=(
        "Check text-generation-webui connectivity, loaded model, loader type, and LoRA status. "
        "Quick way to verify everything is working."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def health_check(args: dict) -> str:
    import httpx
    try:
        health_resp = await _client.get(f"{cfg.TGWUI_INTERNAL}/health", timeout=5)
        health_resp.raise_for_status()
    except (httpx.ConnectError, httpx.HTTPStatusError, httpx.ReadTimeout) as e:
        return f"UNHEALTHY: Cannot reach text-generation-webui at {cfg.TGWUI_BASE}\nError: {e}"

    try:
        info_resp = await _client.get(f"{cfg.TGWUI_INTERNAL}/model/info", timeout=5)
        info_resp.raise_for_status()
        info = info_resp.json()
    except Exception as e:
        return f"Connected but cannot get model info: {e}"

    model_name = info.get("model_name", "None")
    loader = info.get("loader", "unknown")
    loras = info.get("lora_names", [])
    lora_status = ", ".join(loras) if loras else "none"

    cfg.MODEL = model_name if model_name and model_name != "None" else None

    preamble = cfg.get_system_preamble()
    ctx_status = "active" if preamble else "generic (no context set)"

    lines = [
        f"Status: HEALTHY",
        f"Endpoint: {cfg.TGWUI_BASE}",
        f"Model: {model_name}",
        f"Loader: {loader}",
        f"LoRAs: {lora_status}",
        f"Context: {ctx_status}",
    ]

    if len(cfg._backends) > 1:
        lines.append(f"\nBackends ({cfg._active_backend} active):")
        for name, info_b in sorted(cfg._backends.items(), key=lambda x: x[1]["priority"]):
            healthy = await check_backend_health(name)
            marker = " <- active" if name == cfg._active_backend else ""
            status_str = "HEALTHY" if healthy else "UNREACHABLE"
            opt = " (optional)" if info_b.get("optional") else ""
            lines.append(f"  {name}: {info_b['url']} [{status_str}]{opt}{marker}")

    return "\n".join(lines)


@tool_handler(
    name="token_count",
    description="Count tokens in text using the currently loaded model's tokenizer",
    schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to count tokens for"},
        },
        "required": ["text"],
    },
)
async def token_count(args: dict) -> str:
    resp = await _client.post(
        f"{cfg.TGWUI_INTERNAL}/token-count",
        json={"text": args["text"]},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    count = data.get("length") or data.get("tokens")
    if count is None:
        return f"Token count unavailable. Response: {data}"
    text_len = len(args["text"])
    return f"Tokens: {count}\nCharacters: {text_len}\nRatio: {text_len / max(int(count), 1):.1f} chars/token"


@tool_handler(
    name="encode_tokens",
    description=(
        "Tokenize text and return the actual token IDs. "
        "Useful for inspecting tokenization, debugging context usage, or checking special tokens."
    ),
    schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to tokenize"},
        },
        "required": ["text"],
    },
)
async def encode_tokens(args: dict) -> str:
    resp = await _client.post(
        f"{cfg.TGWUI_INTERNAL}/encode",
        json={"text": args["text"]},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    tokens = data.get("tokens", [])
    length = data.get("length", len(tokens))
    if len(tokens) > 50:
        preview = f"{tokens[:25]} ... {tokens[-25:]}"
    else:
        preview = str(tokens)
    return f"Tokens ({length}): {preview}"


@tool_handler(
    name="decode_tokens",
    description="Decode token IDs back to text using the loaded model's tokenizer.",
    schema={
        "type": "object",
        "properties": {
            "tokens": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "List of token IDs to decode",
            },
        },
        "required": ["tokens"],
    },
)
async def decode_tokens(args: dict) -> str:
    resp = await _client.post(
        f"{cfg.TGWUI_INTERNAL}/decode",
        json={"tokens": args["tokens"]},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("text", "")
    return f"Decoded ({len(args['tokens'])} tokens): {text}"


@tool_handler(
    name="swap_model",
    description=(
        "List available models or load a specific one without using the webUI. "
        "Call with no arguments to list all models (current model marked). "
        "Call with model_name to load that model."
    ),
    schema={
        "type": "object",
        "properties": {
            "model_name": {"type": "string", "description": "Model filename to load. Omit to list available models."},
            "ctx_size": {"type": "integer", "description": "Context window size in tokens (default: 32768)."},
            "gpu_layers": {"type": "integer", "description": "Number of layers to offload to GPU (-1 = auto/all). Default: -1."},
        },
        "required": [],
    },
)
async def swap_model(args: dict) -> str:
    if not args.get("model_name"):
        resp = await _client.get(f"{cfg.TGWUI_INTERNAL}/model/list", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        available = data.get("model_names", [])

        info_resp = await _client.get(f"{cfg.TGWUI_INTERNAL}/model/info", timeout=5)
        info_resp.raise_for_status()
        current = info_resp.json().get("model_name", "")

        lines = []
        for m in sorted(available):
            marker = " \u2190 loaded" if m == current else ""
            lines.append(f"  {m}{marker}")
        return f"Available models ({len(available)}):\n" + "\n".join(lines)

    model_name = args["model_name"]
    model_config = {}
    for pattern, overrides in cfg._config.get("models", {}).items():
        if pattern in model_name:
            model_config = overrides
            break
    ctx_size = args.get("ctx_size") or model_config.get("ctx_size", 32768)
    gpu_layers = args.get("gpu_layers") if args.get("gpu_layers") is not None else model_config.get("gpu_layers", -1)

    load_request: dict[str, Any] = {
        "model_name": model_name,
        "args": {"ctx_size": ctx_size, "gpu_layers": gpu_layers},
        "settings": {"truncation_length": ctx_size},
    }

    resp = await _client.post(
        f"{cfg.TGWUI_INTERNAL}/model/load",
        json=load_request,
        timeout=180,
    )
    resp.raise_for_status()

    resp_text = resp.text.strip().strip('"')
    if resp_text != "OK":
        return f"Model load may have failed. Response: {resp_text}"

    previous_model = cfg.MODEL
    cfg.MODEL = None
    try:
        cfg.MODEL = await resolve_model()
    except Exception as e:
        cfg.MODEL = previous_model
        return f"Model load request sent but verification failed (still using {cfg.MODEL or 'none'}): {e}"

    try:
        Path("/tmp/claude-local-ctx-state").write_text(f"{cfg.MODEL}:{ctx_size}")
    except OSError:
        pass

    return f"Model loaded: {cfg.MODEL}\nContext size: {ctx_size}\nGPU layers: {gpu_layers}"


@tool_handler(
    name="unload_model",
    description="Unload the current model to free GPU VRAM without loading another.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def unload_model(args: dict) -> str:
    resp = await _client.post(
        f"{cfg.TGWUI_INTERNAL}/model/unload",
        json={},
        timeout=30,
    )
    resp.raise_for_status()
    cfg.MODEL = None
    return "Model unloaded. GPU VRAM freed."


@tool_handler(
    name="stop_generation",
    description="Interrupt a running text generation. Useful if a response is taking too long.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def stop_generation(args: dict) -> str:
    resp = await _client.post(f"{cfg.TGWUI_INTERNAL}/stop-generation", timeout=5)
    resp.raise_for_status()
    return "Generation stopped."


@tool_handler(
    name="warm_model",
    description="Prime the KV cache with a short generation so the first real call is fast.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def warm_model(args: dict) -> str:
    if cfg.MODEL is None:
        cfg.MODEL = await resolve_model()
    await chat("Say OK.", max_tokens=5, use_cache=False)
    return f"Model warmed: {cfg.MODEL}"


@tool_handler(
    name="cache_stats",
    description="Show response cache statistics (hit rate, size). Pass clear=true to flush.",
    schema={
        "type": "object",
        "properties": {
            "clear": {"type": "boolean", "description": "Clear the cache (default: false)"},
        },
        "required": [],
    },
)
async def cache_stats(args: dict) -> str:
    if args.get("clear"):
        _cache.clear()
        return "Response cache cleared."
    stats = _cache.stats()
    return "\n".join(f"  {k}: {v}" for k, v in stats.items())


@tool_handler(
    name="session_stats",
    description="Show cumulative session statistics: token usage, tool call breakdown, uptime.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def session_stats(args: dict) -> str:
    import time
    stats = _session_stats.copy()
    tool_calls = stats.pop("tool_calls", {})

    lines = [f"Session started: {stats.get('started_at', '?')}"]
    lines.append(f"Total calls: {stats.get('total_calls', 0)}")
    lines.append(f"Approx tokens in: {stats.get('total_tokens_in_approx', 0):,}")
    lines.append(f"Approx tokens out: {stats.get('total_tokens_out_approx', 0):,}")

    cache_s = _cache.stats()
    lines.append(f"\nCache: {cache_s['size']}/{cache_s['max_size']} entries, hit rate {cache_s['hit_rate']}")

    if tool_calls:
        lines.append(f"\nTool calls ({sum(tool_calls.values())} total):")
        for name, count in sorted(tool_calls.items(), key=lambda x: -x[1]):
            lines.append(f"  {name}: {count}")

    return "\n".join(lines)


@tool_handler(
    name="benchmark",
    description="Measure generation speed (tok/s) for the loaded model.",
    schema={
        "type": "object",
        "properties": {
            "prompt_length": {"type": "string", "description": "Prompt length: 'short', 'medium', 'long' (default: 'short')"},
        },
        "required": [],
    },
)
async def benchmark(args: dict) -> str:
    import time as _time

    if cfg.MODEL is None:
        cfg.MODEL = await resolve_model()

    length = args.get("prompt_length", "short")
    prompts = {
        "short": "Write a haiku about programming.",
        "medium": "Explain the differences between TCP and UDP in detail, covering reliability, ordering, connection management, and use cases.",
        "long": (
            "Write a comprehensive guide to error handling in Rust. Cover Result, Option, "
            "the ? operator, thiserror, anyhow, custom error types, error propagation, "
            "and best practices for library vs application code. Include code examples."
        ),
    }
    prompt = prompts.get(length, prompts["short"])

    start = _time.time()
    result = await chat(prompt, max_tokens=256, use_cache=False)
    elapsed = _time.time() - start

    # Estimate tokens (rough: 4 chars per token)
    out_tokens = len(result) // 4
    tok_per_sec = out_tokens / elapsed if elapsed > 0 else 0

    return (
        f"Model: {cfg.MODEL}\n"
        f"Prompt: {length} ({len(prompt)} chars)\n"
        f"Output: {len(result)} chars (~{out_tokens} tokens)\n"
        f"Time: {elapsed:.1f}s\n"
        f"Speed: ~{tok_per_sec:.1f} tok/s"
    )


@tool_handler(
    name="list_loras",
    description="List available LoRA adapters.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_loras(args: dict) -> str:
    resp = await _client.get(f"{cfg.TGWUI_INTERNAL}/lora/list", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    loras = data.get("lora_names", [])
    if not loras:
        return "No LoRA adapters found."
    return "Available LoRAs:\n" + "\n".join(f"  {l}" for l in sorted(loras))


@tool_handler(
    name="load_lora",
    description="Load one or more LoRA adapters onto the current model.",
    schema={
        "type": "object",
        "properties": {
            "lora_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "LoRA adapter names to load",
            },
            "lora_weights": {
                "type": "array",
                "items": {"type": "number"},
                "description": "Weight multipliers for each LoRA (default: 1.0 each)",
            },
        },
        "required": ["lora_names"],
    },
)
async def load_lora(args: dict) -> str:
    names = args["lora_names"]
    weights = args.get("lora_weights", [1.0] * len(names))
    resp = await _client.post(
        f"{cfg.TGWUI_INTERNAL}/lora/load",
        json={"lora_names": names, "lora_weights": weights},
        timeout=60,
    )
    resp.raise_for_status()
    return f"LoRA loaded: {', '.join(names)} (weights: {weights})"


@tool_handler(
    name="unload_loras",
    description="Remove all LoRA adapters from the current model.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def unload_loras(args: dict) -> str:
    resp = await _client.post(
        f"{cfg.TGWUI_INTERNAL}/lora/unload",
        json={},
        timeout=30,
    )
    resp.raise_for_status()
    return "All LoRAs unloaded."
