"""Preset, grammar, sampling, and prompt preview tools."""

import os
from pathlib import Path
from typing import Any

import yaml

from localforge import config as cfg
from localforge.chunking import BUILTIN_GRAMMARS
from localforge.client import _client, resolve_model
from localforge.tools import tool_handler


def _get_webui_root() -> Path | None:
    """Get the text-generation-webui root directory from config or heuristic."""
    root = cfg._config.get("webui_root", "")
    if root:
        path = Path(os.path.expanduser(root)).resolve()
        if path.exists():
            return path

    settings_path = cfg._config.get("webui_settings", "")
    if settings_path:
        path = Path(os.path.expanduser(settings_path)).resolve()
        webui_root = path.parent.parent
        if webui_root.exists():
            return webui_root

    default = Path.home() / "Development" / "text-generation-webui"
    if default.exists():
        return default

    return None


@tool_handler(
    name="get_logits",
    description=(
        "Get the next-token probability distribution for the given prompt. "
        "Returns the top-50 most likely tokens with their probabilities. "
        "Useful for confidence checking, decision routing, or debugging model behavior."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Prompt to get next-token logits for"},
            "top_n": {"type": "integer", "description": "Number of top tokens to return (default: 50, max: 100)"},
            "use_samplers": {
                "type": "boolean",
                "description": "Apply sampling filters (temp, top_p, etc.) before returning (default: false)",
            },
        },
        "required": ["prompt"],
    },
)
async def get_logits(args: dict) -> str:
    top_n = min(args.get("top_n", 50), 100)
    body: dict[str, Any] = {
        "prompt": args["prompt"],
        "use_samplers": args.get("use_samplers", False),
        "top_logits": top_n,
    }
    resp = await _client.post(f"{cfg.TGWUI_INTERNAL}/logits", json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    logits = data if isinstance(data, list) else data.get("logits", data.get("top_logits", []))

    lines = [f"Top-{len(logits)} next tokens:"]
    for i, entry in enumerate(logits[:top_n]):
        if isinstance(entry, dict):
            token = entry.get("token", entry.get("text", "?"))
            prob = entry.get("probability", entry.get("prob", 0))
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            token, prob = entry[0], entry[1]
        else:
            token, prob = str(entry), 0
        lines.append(f"  {i + 1:3d}. {repr(token):>20s}  {float(prob) * 100:6.2f}%")
    return "\n".join(lines)


@tool_handler(
    name="preview_prompt",
    description=(
        "Render a chat prompt template without generating any text. "
        "Shows the exact formatted prompt that would be sent to the model, "
        "including system messages, template markers, and token count. "
        "Useful for debugging templates and checking context budget."
    ),
    schema={
        "type": "object",
        "properties": {
            "user_message": {"type": "string", "description": "User message to render"},
            "system_message": {
                "type": "string",
                "description": "System message (optional, uses context preamble if not set)",
            },
        },
        "required": ["user_message"],
    },
)
async def preview_prompt(args: dict) -> str:
    if cfg.MODEL is None:
        cfg.MODEL = await resolve_model()

    system = args.get("system_message") or cfg.get_system_preamble()
    suffix = cfg.get_system_suffix(cfg.MODEL)
    effective_system = system
    if suffix:
        effective_system = f"{system}\n\n{suffix}" if system else suffix

    messages: list[dict[str, str]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": args["user_message"]})

    body = {"messages": messages}
    resp = await _client.post(f"{cfg.TGWUI_INTERNAL}/chat-prompt", json=body, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    rendered = data.get("prompt", data.get("text", str(data)))

    try:
        tc_resp = await _client.post(
            f"{cfg.TGWUI_INTERNAL}/token-count",
            json={"text": rendered},
            timeout=5,
        )
        tc_resp.raise_for_status()
        tc_data = tc_resp.json()
        token_count = tc_data.get("length") or tc_data.get("tokens") or "?"
    except Exception:
        token_count = "?"

    return f"Rendered prompt ({token_count} tokens, {len(rendered)} chars):\n{'=' * 60}\n{rendered}\n{'=' * 60}"


@tool_handler(
    name="set_sampling",
    description=(
        "Set advanced sampling parameters at runtime. Extends set_generation_params "
        "with ALL sampling options: mirostat, DRY, XTC, dynamic temperature, seed, "
        "token bans, reasoning_effort, and more. "
        "Call with no arguments to see available parameters."
    ),
    schema={
        "type": "object",
        "properties": {
            "seed": {"type": "integer", "description": "Random seed (-1 = random)"},
            "mirostat_mode": {"type": "integer", "description": "Mirostat mode: 0=disabled, 1=v1, 2=v2"},
            "mirostat_tau": {"type": "number", "description": "Mirostat target entropy (5.0 default)"},
            "mirostat_eta": {"type": "number", "description": "Mirostat learning rate (0.1 default)"},
            "dry_multiplier": {"type": "number", "description": "DRY repetition penalty multiplier (0=disabled)"},
            "dry_allowed_length": {"type": "integer", "description": "DRY minimum repeated sequence length"},
            "dry_base": {"type": "number", "description": "DRY penalty base"},
            "xtc_threshold": {"type": "number", "description": "XTC pruning threshold"},
            "xtc_probability": {"type": "number", "description": "XTC probability of applying"},
            "dynatemp_low": {"type": "number", "description": "Dynamic temperature low bound"},
            "dynatemp_high": {"type": "number", "description": "Dynamic temperature high bound"},
            "dynatemp_exponent": {"type": "number", "description": "Dynamic temperature exponent"},
            "dynamic_temperature": {"type": "boolean", "description": "Enable dynamic temperature"},
            "temperature_last": {"type": "boolean", "description": "Apply temperature after other samplers"},
            "smoothing_factor": {"type": "number", "description": "Quadratic smoothing factor"},
            "smoothing_curve": {"type": "number", "description": "Quadratic smoothing curve"},
            "top_n_sigma": {"type": "number", "description": "Top-n sigma sampling threshold"},
            "custom_token_bans": {"type": "string", "description": "Comma-separated token IDs to ban"},
            "ban_eos_token": {"type": "boolean", "description": "Ban the end-of-sequence token"},
            "reasoning_effort": {"type": "number", "description": "Reasoning effort (0.0-1.0)"},
            "prompt_lookup_num_tokens": {"type": "integer", "description": "Speculative decoding token count"},
            "max_tokens_second": {"type": "integer", "description": "Rate limit output (0=unlimited)"},
            "guidance_scale": {"type": "number", "description": "Classifier-free guidance scale (1.0=disabled)"},
        },
        "required": [],
    },
)
async def set_sampling(args: dict) -> str:
    if not args:
        params_list = [
            "seed, mirostat_mode, mirostat_tau, mirostat_eta,",
            "dry_multiplier, dry_allowed_length, dry_base,",
            "xtc_threshold, xtc_probability,",
            "dynatemp_low, dynatemp_high, dynatemp_exponent, dynamic_temperature,",
            "temperature_last, smoothing_factor, smoothing_curve, top_n_sigma,",
            "custom_token_bans, ban_eos_token, reasoning_effort,",
            "prompt_lookup_num_tokens, max_tokens_second, guidance_scale",
        ]
        return "Available advanced sampling params:\n  " + "\n  ".join(params_list)

    allowed = {
        "seed",
        "mirostat_mode",
        "mirostat_tau",
        "mirostat_eta",
        "dry_multiplier",
        "dry_allowed_length",
        "dry_base",
        "xtc_threshold",
        "xtc_probability",
        "dynatemp_low",
        "dynatemp_high",
        "dynatemp_exponent",
        "dynamic_temperature",
        "temperature_last",
        "smoothing_factor",
        "smoothing_curve",
        "top_n_sigma",
        "custom_token_bans",
        "ban_eos_token",
        "reasoning_effort",
        "prompt_lookup_num_tokens",
        "max_tokens_second",
        "guidance_scale",
    }
    changed = []
    for k, v in args.items():
        if k not in allowed:
            continue
        if v is None or v == "":
            cfg._runtime_overrides.pop(k, None)
            changed.append(f"  {k}: (cleared)")
        else:
            cfg._runtime_overrides[k] = v
            changed.append(f"  {k}: {v}")

    if not changed:
        return "No valid sampling parameters provided."

    return "Advanced sampling overrides updated:\n" + "\n".join(changed)


@tool_handler(
    name="list_presets",
    description=(
        "List all available generation presets with their key parameters. "
        "Presets are stored in the text-generation-webui user_data/presets/ directory."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_presets(args: dict) -> str:
    webui_root = _get_webui_root()
    if webui_root is None:
        return "Error: cannot determine text-generation-webui root. Set webui_root in config.yaml."

    presets_dir = webui_root / "user_data" / "presets"
    if not presets_dir.exists():
        return f"Presets directory not found: {presets_dir}"

    presets = sorted(presets_dir.glob("*.yaml"))
    if not presets:
        return "No presets found."

    lines = [f"Available presets ({len(presets)}):"]
    for p in presets:
        try:
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            key_params = []
            for k in ("temperature", "top_p", "top_k", "min_p", "repetition_penalty"):
                if k in data:
                    key_params.append(f"{k}={data[k]}")
            active = " <- active" if p.stem == cfg._webui_preset_name else ""
            params_str = ", ".join(key_params) if key_params else "(empty)"
            lines.append(f"  {p.stem}: {params_str}{active}")
        except Exception:
            lines.append(f"  {p.stem}: (unreadable)")

    return "\n".join(lines)


@tool_handler(
    name="load_preset",
    description=(
        "Load a preset's parameters as runtime overrides. "
        "This applies the preset's sampling params on top of current settings "
        "without changing the webui's active preset."
    ),
    schema={
        "type": "object",
        "properties": {
            "preset_name": {"type": "string", "description": "Name of the preset to load"},
        },
        "required": ["preset_name"],
    },
)
async def load_preset(args: dict) -> str:
    preset_name = args["preset_name"]
    if ".." in preset_name or "/" in preset_name or "\\" in preset_name:
        return "Error: invalid preset name"

    webui_root = _get_webui_root()
    if webui_root is None:
        return "Error: cannot determine text-generation-webui root. Set webui_root in config.yaml."

    preset_path = webui_root / "user_data" / "presets" / f"{preset_name}.yaml"
    if not preset_path.exists():
        return f"Preset '{preset_name}' not found at {preset_path}"

    try:
        with open(preset_path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        return f"Error reading preset: {e}"

    changed = []
    for k, v in data.items():
        if k in cfg.WEBUI_GEN_KEYS:
            cfg._runtime_overrides[k] = v
            changed.append(f"  {k}: {v}")

    if not changed:
        return f"Preset '{preset_name}' loaded but had no recognized generation params."

    return f"Loaded preset '{preset_name}' as runtime overrides:\n" + "\n".join(changed)


@tool_handler(
    name="list_grammars",
    description=(
        "List available GBNF grammars: built-in ones (json, json_array, boolean) "
        "and any .gbnf files in the text-generation-webui grammars directory."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_grammars(args: dict) -> str:
    lines = ["Built-in grammars:"]
    for name in sorted(BUILTIN_GRAMMARS.keys()):
        preview = BUILTIN_GRAMMARS[name][:60].replace("\n", " ")
        lines.append(f"  {name}: {preview}...")

    webui_root = _get_webui_root()
    if webui_root:
        grammar_dir = webui_root / "user_data" / "grammars"
        if grammar_dir.exists():
            gbnf_files = sorted(grammar_dir.glob("*.gbnf"))
            if gbnf_files:
                lines.append(f"\nOn-disk grammars ({len(gbnf_files)}):")
                for f in gbnf_files:
                    size = f.stat().st_size
                    lines.append(f"  {f.stem} ({size} bytes)")
            else:
                lines.append(f"\nNo .gbnf files in {grammar_dir}")
        else:
            lines.append(f"\nGrammars directory not found: {grammar_dir}")
    else:
        lines.append("\nCannot list on-disk grammars (webui_root unknown)")

    return "\n".join(lines)


@tool_handler(
    name="load_grammar",
    description=(
        "Load a GBNF grammar file from disk into the built-in grammar registry. "
        "Once loaded, it can be used by name in local_chat, structured_output, etc."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name to register the grammar under"},
            "file_path": {"type": "string", "description": "Path to .gbnf file (or use grammar_text)"},
            "grammar_text": {"type": "string", "description": "Raw GBNF grammar text (or use file_path)"},
        },
        "required": ["name"],
    },
)
async def load_grammar(args: dict) -> str:
    name = args["name"]
    if ".." in name or "/" in name:
        return "Error: invalid grammar name"

    if args.get("file_path"):
        path = Path(os.path.expanduser(args["file_path"])).resolve()
        home = Path.home().resolve()
        if not (str(path).startswith(str(home)) or str(path).startswith("/tmp")):
            return f"Error: file must be under {home} or /tmp"
        if not path.exists():
            return f"Error: file not found: {path}"
        grammar_text = path.read_text(encoding="utf-8")
    elif args.get("grammar_text"):
        grammar_text = args["grammar_text"]
    else:
        return "Error: provide either file_path or grammar_text"

    BUILTIN_GRAMMARS[name] = grammar_text
    return (
        f"Grammar '{name}' loaded ({len(grammar_text)} chars). Available: {', '.join(sorted(BUILTIN_GRAMMARS.keys()))}"
    )
