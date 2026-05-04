"""Config and generation parameter tools."""

import httpx

from localforge import config as cfg
from localforge.client import reload_webui_params_from_api, resolve_model
from localforge.exceptions import ModelNotLoadedError
from localforge.tools import tool_handler


@tool_handler(
    name="get_generation_params",
    description=(
        "Show the current generation parameters (temperature, max_tokens, top_p, etc.) "
        "and system_suffix for the loaded model. Shows the full merge chain: "
        "webui settings → config defaults → model overrides → runtime overrides."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def get_generation_params_tool(args: dict) -> str:
    if cfg.MODEL is None:
        try:
            cfg.MODEL = await resolve_model()
        except (httpx.HTTPError, ModelNotLoadedError):
            pass

    await reload_webui_params_from_api()

    lines = [f"Model: {cfg.MODEL or '(none loaded)'}"]

    if cfg._webui_preset_name:
        lines.append(f"Active preset: {cfg._webui_preset_name}")

    lines.append("\n--- webui preset/settings ---")
    if cfg._webui_settings:
        for k, v in sorted(cfg._webui_settings.items()):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (empty or not found)")

    lines.append("\n--- config.yaml defaults ---")
    defaults = cfg._config.get("defaults", {})
    if defaults:
        for k, v in sorted(defaults.items()):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")

    matched_pattern = None
    model_overrides = cfg._config.get("models", {})
    if cfg.MODEL:
        for pattern, overrides in model_overrides.items():
            if pattern in cfg.MODEL:
                matched_pattern = pattern
                lines.append(f"\n--- model override (matched: '{pattern}') ---")
                for k, v in sorted(overrides.items()):
                    lines.append(f"  {k}: {v}")
                break
    if not matched_pattern:
        lines.append("\n--- model override ---")
        lines.append("  (no match)")

    if cfg._runtime_overrides:
        lines.append("\n--- runtime overrides ---")
        for k, v in sorted(cfg._runtime_overrides.items()):
            lines.append(f"  {k}: {v}")

    resolved = cfg.get_generation_params(cfg.MODEL)
    suffix = cfg.get_system_suffix(cfg.MODEL)
    lines.append("\n--- resolved (sent to model) ---")
    for k, v in sorted(resolved.items()):
        source = cfg.trace_param_source(k, v, matched_pattern)
        lines.append(f"  {k}: {v}  ({source})")
    lines.append(f"  system_suffix: {suffix!r}")

    return "\n".join(lines)


@tool_handler(
    name="set_generation_params",
    description=(
        "Override generation parameters at runtime. These take highest priority and "
        "persist until cleared or the MCP server restarts. "
        "Set a key to null/empty to clear that override. "
        "Supports: temperature, max_tokens, top_p, top_k, min_p, "
        "repetition_penalty, enable_thinking, mode, system_suffix. "
        "Call with no arguments to clear all runtime overrides."
    ),
    schema={
        "type": "object",
        "properties": {
            "temperature": {"type": "number", "description": "Sampling temperature (0.0-2.0)"},
            "max_tokens": {"type": "integer", "description": "Max output tokens"},
            "top_p": {"type": "number", "description": "Nucleus sampling threshold"},
            "top_k": {"type": "integer", "description": "Top-k sampling"},
            "min_p": {"type": "number", "description": "Min-p sampling threshold"},
            "repetition_penalty": {"type": "number", "description": "Repetition penalty (1.0 = none)"},
            "enable_thinking": {
                "type": "boolean",
                "description": "Enable model thinking/reasoning (true/false). Controls <think> block generation.",
            },
            "mode": {"type": "string", "description": "Chat mode: 'instruct', 'chat', or 'chat-instruct'"},
            "system_suffix": {"type": "string", "description": "System instruction appended to all calls"},
        },
        "required": [],
    },
)
async def set_generation_params_tool(args: dict) -> str:
    if not args:
        cfg._runtime_overrides.clear()
        return "All runtime overrides cleared. Falling back to config.yaml + webui settings."

    allowed = {
        "temperature",
        "max_tokens",
        "top_p",
        "top_k",
        "min_p",
        "repetition_penalty",
        "enable_thinking",
        "mode",
        "system_suffix",
        "seed",
        "custom_token_bans",
        "ban_eos_token",
        "reasoning_effort",
        "prompt_lookup_num_tokens",
        "max_tokens_second",
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
        return "No valid parameters provided."

    return "Runtime overrides updated:\n" + "\n".join(changed)


@tool_handler(
    name="reload_config",
    description=(
        "Reload config.yaml and webui settings.yaml from disk. "
        "Use after editing config.yaml or changing webui settings. "
        "Does NOT clear runtime overrides (use set_generation_params with no args for that)."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def reload_config_tool(args: dict) -> str:
    cfg.reload_config()
    await reload_webui_params_from_api()
    defaults = cfg._config.get("defaults", {})
    model_count = len(cfg._config.get("models", {}))
    webui_count = len(cfg._webui_settings)
    preset_info = f"  preset: {cfg._webui_preset_name or '(none)'}\n" if cfg._webui_preset_name else ""
    return (
        f"Config reloaded.\n"
        f"{preset_info}"
        f"  webui params: {webui_count} keys\n"
        f"  config defaults: {list(defaults.keys())}\n"
        f"  model profiles: {model_count}\n"
        f"  runtime overrides: {len(cfg._runtime_overrides)} (unchanged)"
    )
