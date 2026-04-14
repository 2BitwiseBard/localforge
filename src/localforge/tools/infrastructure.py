"""Infrastructure, model management, and stats tools."""

import logging
import subprocess
from pathlib import Path
from typing import Any

import httpx

from localforge import config as cfg
from localforge.client import (
    _cache,
    _client,
    _session_stats,
    chat,
    check_backend_health,
    resolve_model,
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
    except httpx.HTTPError as e:
        return f"Connected but cannot get model info: {e}"

    model_name = info.get("model_name", "None")
    loader = info.get("loader", "unknown")
    loras = info.get("lora_names", [])
    lora_status = ", ".join(loras) if loras else "none"

    cfg.MODEL = model_name if model_name and model_name != "None" else None

    preamble = cfg.get_system_preamble()
    ctx_status = "active" if preamble else "generic (no context set)"

    lines = [
        "Status: HEALTHY",
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
        "Call with model_name to load. Supports full llama.cpp loading params: "
        "context size, GPU layers, threads, batch size, cache type, flash attention, "
        "rope scaling, speculative decoding (draft model or n-gram), tensor split for multi-GPU."
    ),
    schema={
        "type": "object",
        "properties": {
            "model_name": {"type": "string", "description": "Model filename to load. Omit to list available models."},
            # Core loading params
            "ctx_size": {"type": "integer", "description": "Context window size in tokens (0 = auto from model metadata). Default: from config or 32768."},
            "gpu_layers": {"type": "integer", "description": "Layers to offload to GPU (-1 = all). Default: -1."},
            "threads": {"type": "integer", "description": "CPU threads for generation (0 = auto-detect). Default: 0."},
            "threads_batch": {"type": "integer", "description": "CPU threads for batch/prompt processing (0 = same as threads). Default: 0."},
            "batch_size": {"type": "integer", "description": "Batch size for prompt processing. Higher = faster prefill, more VRAM. Default: 512."},
            "ubatch_size": {"type": "integer", "description": "Micro-batch size (must be <= batch_size). Default: 512."},
            # Memory / cache
            "cache_type": {"type": "string", "enum": ["fp16", "q8_0", "q4_0"], "description": "KV cache quantization. q8_0 halves VRAM for cache, q4_0 quarters it. Default: fp16."},
            "flash_attn": {"type": "boolean", "description": "Enable flash attention (faster, less VRAM for long contexts). Default: depends on webui settings."},
            # Rope / context scaling
            "rope_freq_base": {"type": "number", "description": "RoPE frequency base for context extension (e.g. 1000000 for YaRN). 0 = use model default."},
            # Multi-GPU
            "tensor_split": {"type": "string", "description": "Comma-separated VRAM split ratios for multi-GPU (e.g. '0.7,0.3'). Empty = single GPU."},
            # Parallel slots
            "parallel": {"type": "integer", "description": "Number of parallel generation slots. More slots = more concurrent requests but each gets less context. Default: 1."},
            # Speculative decoding — draft model
            "model_draft": {"type": "string", "description": "Draft model filename for speculative decoding (e.g. 'Qwen3.5-2B-UD-Q8_K_XL.gguf'). Speeds up generation 1.5-2x."},
            "draft_max": {"type": "integer", "description": "Max tokens to speculate per step (default: 16)."},
            "gpu_layers_draft": {"type": "integer", "description": "GPU layers for draft model (-1 = all). Default: -1."},
            "ctx_size_draft": {"type": "integer", "description": "Context size for draft model. Default: 0 (same as main)."},
            # Speculative decoding — n-gram (no draft model needed)
            "spec_type": {"type": "string", "enum": ["none", "lookup"], "description": "Speculation type: 'none' (disabled) or 'lookup' (n-gram, no draft model needed). Default: none."},
            "spec_ngram_size_n": {"type": "integer", "description": "N-gram size for lookup speculation. Default: 4."},
            "spec_ngram_size_m": {"type": "integer", "description": "M-gram context window. Default: 3."},
            "spec_ngram_min_hits": {"type": "integer", "description": "Min n-gram hits to speculate. Default: 2."},
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

    # Resolve config defaults for this model (from config.yaml models section)
    model_config: dict[str, Any] = {}
    for pattern, overrides in cfg._config.get("models", {}).items():
        if pattern in model_name:
            model_config = overrides
            break

    # Build loading args — user params override config overrides override defaults
    def _resolve(key: str, default: Any = None) -> Any:
        """Resolve param: explicit arg > config.yaml model override > default."""
        val = args.get(key)
        if val is not None:
            return val
        val = model_config.get(key)
        if val is not None:
            return val
        return default

    ctx_size = _resolve("ctx_size", 32768)
    gpu_layers = _resolve("gpu_layers", -1)

    load_args: dict[str, Any] = {
        "ctx_size": ctx_size,
        "gpu_layers": gpu_layers,
    }

    # Optional params — only set if provided (avoid overriding webui defaults)
    for key, arg_key in [
        ("threads", "threads"),
        ("threads_batch", "threads_batch"),
        ("batch_size", "batch_size"),
        ("ubatch_size", "ubatch_size"),
        ("cache_type", "cache_type"),
        ("rope_freq_base", "rope_freq_base"),
        ("tensor_split", "tensor_split"),
        ("parallel", "parallel"),
        ("model_draft", "model_draft"),
        ("draft_max", "draft_max"),
        ("gpu_layers_draft", "gpu_layers_draft"),
        ("ctx_size_draft", "ctx_size_draft"),
        ("spec_type", "spec_type"),
        ("spec_ngram_size_n", "spec_ngram_size_n"),
        ("spec_ngram_size_m", "spec_ngram_size_m"),
        ("spec_ngram_min_hits", "spec_ngram_min_hits"),
    ]:
        val = _resolve(arg_key)
        if val is not None:
            load_args[key] = val

    # flash_attn needs special handling (it's a boolean flag)
    flash = _resolve("flash_attn")
    if flash is not None:
        load_args["flash_attn"] = flash

    load_request: dict[str, Any] = {
        "model_name": model_name,
        "args": load_args,
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

    # Build summary of applied params
    summary_parts = [
        f"Model loaded: {cfg.MODEL}",
        f"Context size: {ctx_size}",
        f"GPU layers: {gpu_layers}",
    ]
    extras = {k: v for k, v in load_args.items() if k not in ("ctx_size", "gpu_layers") and v is not None}
    if extras:
        summary_parts.append("Additional params: " + ", ".join(f"{k}={v}" for k, v in extras.items()))

    return "\n".join(summary_parts)


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
    stats = _session_stats.copy()
    tool_calls = stats.pop("tool_calls", {})

    lines = [f"Session started: {stats.get('started_at', '?')}"]
    lines.append(f"Total calls: {stats.get('total_calls', 0)}")
    lines.append(f"Approx tokens in: {stats.get('total_tokens_in_approx', 0):,}")
    lines.append(f"Approx tokens out: {stats.get('total_tokens_out_approx', 0):,}")

    cache_s = _cache.stats()
    lines.append(f"\nCache: {cache_s['entries']}/{cache_s['max_entries']} entries "
                 f"({cache_s['total_bytes'] / 1024:.0f}KB/{cache_s['max_bytes'] / 1024:.0f}KB), "
                 f"hit rate {cache_s['hit_rate']}")

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
    return "Available LoRAs:\n" + "\n".join(f"  {name}" for name in sorted(loras))


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


@tool_handler(
    name="slot_info",
    description=(
        "Show parallel slot count, context per slot, GPU info, and server config. "
        "Useful for understanding parallelism capacity of the current setup."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def slot_info(args: dict) -> str:
    if cfg.MODEL is None:
        try:
            cfg.MODEL = await resolve_model()
        except httpx.HTTPError:
            return "No model loaded."

    try:
        resp = await _client.get(f"{cfg.TGWUI_INTERNAL}/model/info", timeout=5)
        resp.raise_for_status()
        info = resp.json()
    except httpx.HTTPError as e:
        return f"Cannot get model info: {e}"

    model_name = info.get("model_name", "unknown")
    loader = info.get("loader", "unknown")
    loras = info.get("lora_names", [])

    # Query llama-server for slot info
    llama_slots = []
    for llama_port in [5005, 5006, 5007]:
        try:
            slot_resp = await _client.get(
                f"http://127.0.0.1:{llama_port}/slots", timeout=3
            )
            if slot_resp.status_code == 200:
                llama_slots = slot_resp.json()
                break
        except httpx.HTTPError:
            continue

    if llama_slots:
        slot_count = len(llama_slots)
        ctx_per_slot = llama_slots[0].get("n_ctx", "?") if llama_slots else "?"
        total_ctx = ctx_per_slot * slot_count if isinstance(ctx_per_slot, int) else "?"
        active_slots = sum(1 for s in llama_slots if s.get("is_processing", False))
    else:
        slot_count = "unknown"
        ctx_per_slot = "unknown"
        total_ctx = "unknown"
        active_slots = 0

    # GPU info from nvidia-smi
    gpu_info = {}
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            parts = [p.strip() for p in result.stdout.strip().split(",")]
            if len(parts) >= 5:
                gpu_info = {
                    "name": parts[0],
                    "vram_used": f"{int(parts[1]):,} MiB",
                    "vram_total": f"{int(parts[2]):,} MiB",
                    "vram_pct": f"{int(parts[1]) * 100 // int(parts[2])}%",
                    "temp": f"{parts[3]}°C",
                    "util": f"{parts[4]}%",
                }
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        log.debug("nvidia-smi not available or failed")

    # Server process info
    proc_info = {}
    try:
        import re
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if "llama-server" in line and "--model" in line:
                for flag, key in [
                    (r"--gpu-layers\s+(\S+)", "gpu_layers"),
                    (r"--ctx-size\s+(\S+)", "ctx_size"),
                    (r"--parallel\s+(\S+)", "parallel"),
                    (r"--batch-size\s+(\S+)", "batch_size"),
                    (r"--flash-attn\s+(\S+)", "flash_attn"),
                ]:
                    m = re.search(flag, line)
                    if m:
                        proc_info[key] = m.group(1)
                break
    except (subprocess.SubprocessError, FileNotFoundError):
        log.debug("Could not read llama-server process info")

    lines = [
        f"Model: {model_name}",
        f"Loader: {loader}",
        f"LoRAs: {', '.join(loras) if loras else 'none'}",
        "",
        "── Slots ──",
        f"Parallel slots: {slot_count}",
        f"Active / busy: {active_slots}",
        f"Context per slot: {ctx_per_slot:,}" if isinstance(ctx_per_slot, int) else f"Context per slot: {ctx_per_slot}",
        f"Total context: {total_ctx:,}" if isinstance(total_ctx, int) else f"Total context: {total_ctx}",
    ]

    if proc_info:
        lines += ["", "── Server Config ──"]
        for key, label in [("gpu_layers", "GPU layers"), ("ctx_size", "Context size"),
                           ("parallel", "Parallel"), ("batch_size", "Batch size"),
                           ("flash_attn", "Flash attention")]:
            if key in proc_info:
                lines.append(f"{label}: {proc_info[key]}")

    if gpu_info:
        lines += [
            "", "── GPU ──",
            f"Device: {gpu_info['name']}",
            f"VRAM: {gpu_info['vram_used']} / {gpu_info['vram_total']} ({gpu_info['vram_pct']})",
            f"Temperature: {gpu_info['temp']}",
            f"Utilization: {gpu_info['util']}",
        ]

    return "\n".join(lines)


@tool_handler(
    name="sync_models",
    description=(
        "Scan the model source directory for new GGUF files and create symlinks "
        "in text-generation-webui's models directory. Run this after downloading "
        "new models so they appear in the model list. Optionally cleans broken symlinks."
    ),
    schema={
        "type": "object",
        "properties": {
            "clean": {
                "type": "boolean",
                "description": "Also remove broken symlinks (default: true)",
            },
            "source": {
                "type": "string",
                "description": "Source directory containing .gguf files. Default: auto-detect from secondary drive.",
            },
        },
        "required": [],
    },
)
async def sync_models(args: dict) -> str:
    import os

    clean = args.get("clean", True)

    # Resolve webui models directory
    webui_root = cfg._config.get("webui_root") or os.environ.get("LOCALFORGE_WEBUI_ROOT")
    if not webui_root:
        # Auto-detect common locations
        candidates = [
            Path.home() / "Development" / "text-generation-webui",
            Path.home() / "text-generation-webui",
            Path("/opt/text-generation-webui"),
        ]
        for c in candidates:
            if (c / "user_data" / "models").exists():
                webui_root = str(c)
                break

    if not webui_root:
        return (
            "Cannot find text-generation-webui installation.\n"
            "Set LOCALFORGE_WEBUI_ROOT env var or webui_root in config.yaml."
        )

    target_dir = Path(os.path.expanduser(webui_root)) / "user_data" / "models"
    if not target_dir.exists():
        return f"Models directory not found: {target_dir}"

    # Resolve source directory
    source = args.get("source")
    if source:
        source_dir = Path(os.path.expanduser(source))
    else:
        # 1. Check config.yaml model_source
        configured = cfg._config.get("model_source")
        if configured:
            source_dir = Path(os.path.expanduser(configured))
        else:
            # 2. Auto-detect from common mount points
            source_dir = None
            candidates = [Path("/mnt/models")]
            # 3. Legacy: scan /media for volume label
            volume_label = cfg._config.get("model_volume")
            if volume_label:
                import glob as glob_mod
                candidates.extend(Path(p) for p in sorted(glob_mod.glob(f"/media/*/{volume_label}*")))
            for cpath in candidates:
                if cpath.is_dir() and list(cpath.glob("*.gguf")):
                    source_dir = cpath
                    break

        if source_dir is None or not source_dir.is_dir():
            return (
                "No model source directory found.\n"
                "Set model_source in config.yaml or pass source='/path/to/models'."
            )

    # Scan and sync
    added = []
    skipped = 0
    removed = []

    # Clean broken symlinks first
    if clean:
        for link in target_dir.glob("*.gguf"):
            if link.is_symlink() and not link.exists():
                link.unlink()
                removed.append(link.name)

    # Create symlinks for new models
    for model_file in sorted(source_dir.glob("*.gguf")):
        target = target_dir / model_file.name
        if target.exists() or target.is_symlink():
            skipped += 1
            continue
        target.symlink_to(model_file)
        added.append(model_file.name)

    # Summary
    total = len(list(target_dir.glob("*.gguf")))
    lines = [f"Model sync complete ({source_dir.name} → webui models)"]
    lines.append(f"Added: {len(added)} | Skipped: {skipped} | Removed: {len(removed)} broken")
    lines.append(f"Total models available: {total}")

    if added:
        lines.append("\nNew models:")
        for name in added:
            lines.append(f"  + {name}")

    if removed:
        lines.append("\nRemoved broken links:")
        for name in removed:
            lines.append(f"  - {name}")

    if added:
        lines.append("\nNew models are now available in swap_model.")

    return "\n".join(lines)
