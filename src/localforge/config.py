"""Configuration system for LocalForge.

Handles config loading, generation parameter resolution, backend management,
context/preamble system, and hub mode/character state.

Merge order for generation params (later wins):
    webui settings → config defaults → model overrides → runtime overrides
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from localforge.paths import config_path, notes_dir

log = logging.getLogger("localforge")

# ---------------------------------------------------------------------------
# Config lock — protects global state during reload/mutation
# ---------------------------------------------------------------------------
_config_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Backend URLs (mutable — updated by _load_backends)
# ---------------------------------------------------------------------------
TGWUI_BASE = os.environ.get("LOCALFORGE_BACKEND_URL", "http://localhost:5000/v1")
TGWUI_INTERNAL = TGWUI_BASE.rstrip("/") + "/internal"
MODEL: str | None = None  # auto-detected on first call via client.resolve_model()

# ---------------------------------------------------------------------------
# Multi-backend state
# ---------------------------------------------------------------------------
_backends: dict[str, dict[str, Any]] = {}  # name -> {url, priority, optional, healthy}
_active_backend: str | None = None

# ---------------------------------------------------------------------------
# Generation config state
# ---------------------------------------------------------------------------
_config: dict[str, Any] = {}
_webui_settings: dict[str, Any] = {}
_runtime_overrides: dict[str, Any] = {}
_webui_preset_name: str | None = None

# Keys recognised as generation params (webui settings.yaml / preset files)
WEBUI_GEN_KEYS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "repetition_penalty",
        "frequency_penalty",
        "presence_penalty",
        "do_sample",
        "enable_thinking",
        "mode",
        "typical_p",
        "tfs",
        "top_a",
        "mirostat_mode",
        "mirostat_tau",
        "mirostat_eta",
        "repetition_penalty_range",
        "encoder_repetition_penalty",
        "no_repeat_ngram_size",
        "penalty_alpha",
        "dynatemp_low",
        "dynatemp_high",
        "dynatemp_exponent",
        "smoothing_factor",
        "smoothing_curve",
        "xtc_threshold",
        "xtc_probability",
        "dry_multiplier",
        "dry_allowed_length",
        "dry_base",
        "top_n_sigma",
        "dynamic_temperature",
        "temperature_last",
        "guidance_scale",
        "seed",
        "custom_token_bans",
        "ban_eos_token",
        "reasoning_effort",
        "prompt_lookup_num_tokens",
        "max_tokens_second",
    }
)

# ---------------------------------------------------------------------------
# Hub mode and character state
# ---------------------------------------------------------------------------
_current_mode: dict = {}  # {"name": "development", ...config from modes section}
_current_character: dict = {}  # {"name": "code-reviewer", ...config from characters section}

# ---------------------------------------------------------------------------
# Context system
# ---------------------------------------------------------------------------
PREAMBLE_REGISTRY: dict[str, str] = {
    "rust/quant-platform": (
        "You are reviewing code for the quant-platform project, a high-performance "
        "algorithmic trading system written in Rust.\n\n"
        "Project rules you MUST enforce:\n"
        "- No unwrap() or expect() in runtime code. Use thiserror for library crates, "
        "anyhow::Result with .context() for application code.\n"
        "- CPU-intensive work (Polars, backtesting, ML) MUST use tokio::task::spawn_blocking.\n"
        "- Crate structure: quant-core (types only, no logic) -> quant-feed (I/O), "
        "quant-strategy (pure signals, no I/O), quant-data (storage/backtest/ML), "
        "quant-exec (orders/risk), quant-ui (TUI), quant-chart (web).\n"
        "- Zero clippy warnings: code must pass cargo clippy --workspace -- -D warnings.\n"
        "- Prefer &[Candle] slices over Vec<Candle> for zero-copy paths.\n"
        "- Use compact_str for small string optimizations where applicable."
    ),
}

_context: dict[str, str] = {}  # mutable session context


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

_KNOWN_TOP_LEVEL = frozenset(
    {
        "backends",
        "webui_root",
        "model_source",
        "webui_settings",
        "defaults",
        "models",
        "gateway",
        "users",
        "news",
        "telegram",
        "gpu_pool",
        "compute_pool",
        "modes",
        "characters",
        "tool_workspaces",
        "shell_deny",
    }
)


def _validate_config(cfg: dict[str, Any]) -> list[str]:
    """Validate config.yaml structure; return list of human-readable warnings."""
    problems: list[str] = []

    for key in cfg:
        if key not in _KNOWN_TOP_LEVEL:
            problems.append(f"Unknown top-level key '{key}' — possible typo?")

    for name, backend in cfg.get("backends", {}).items():
        if not isinstance(backend, dict):
            problems.append(f"backends.{name}: expected a mapping, got {type(backend).__name__}")
            continue
        if "url" not in backend:
            problems.append(f"backends.{name}: missing required field 'url'")
        elif not isinstance(backend.get("url"), str):
            problems.append(f"backends.{name}.url: must be a string")

    gw = cfg.get("gateway", {})
    if gw:
        if "host" not in gw:
            problems.append("gateway: missing 'host'")
        if "port" in gw and not isinstance(gw["port"], int):
            problems.append(f"gateway.port: expected int, got {type(gw['port']).__name__}")

    for uid, user in cfg.get("users", {}).items():
        if not isinstance(user, dict):
            problems.append(f"users.{uid}: expected a mapping")
            continue
        for field in ("name", "api_key"):
            if field not in user:
                problems.append(f"users.{uid}: missing required field '{field}'")

    known_defaults = WEBUI_GEN_KEYS | {"system_suffix", "max_tokens"}
    for key in cfg.get("defaults", {}):
        if key not in known_defaults:
            problems.append(f"defaults.{key}: unrecognised key (not a generation param)")

    return problems


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

# Cached config for hot-path callers (auth middleware, etc.)
_config_cache: tuple[float, dict[str, Any]] = (0.0, {})
_CONFIG_CACHE_TTL = 30.0  # seconds


def _load_config() -> dict[str, Any]:
    """Load config.yaml if it exists (no cache — used by reload_config)."""
    path = config_path()
    if path.exists():
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            log.warning("Failed to load config.yaml: %s", e)
    return {}


def load_config_cached() -> dict[str, Any]:
    """Load config.yaml with a 30s TTL cache.

    Use this for hot paths (auth middleware, health checks) where re-reading
    the file on every request is wasteful. For config reload operations,
    use _load_config() directly.
    """
    global _config_cache
    import time as _time

    now = _time.monotonic()
    if now - _config_cache[0] < _CONFIG_CACHE_TTL and _config_cache[1]:
        return _config_cache[1]
    cfg = _load_config()
    _config_cache = (now, cfg)
    return cfg


def _load_webui_settings_from_disk(config: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Load generation params from text-gen-webui's settings.yaml + active preset file.

    Returns (params_dict, preset_name).
    """
    path_str = config.get("webui_settings", "")
    if not path_str:
        return {}, None
    path = Path(os.path.expanduser(path_str))
    if not path.exists():
        log.info("webui settings not found at %s", path)
        return {}, None
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        log.warning("Failed to load webui settings: %s", e)
        return {}, None

    preset_name = raw.get("preset")
    params = {k: v for k, v in raw.items() if k in WEBUI_GEN_KEYS}

    # Load preset file to get the *real* params (settings.yaml is incomplete)
    if preset_name and preset_name not in ("None", ""):
        if ".." in preset_name or "/" in preset_name or "\\" in preset_name:
            log.warning("Invalid preset name (path component detected): %s", preset_name)
            return params, preset_name
        webui_root = path.parent.parent  # settings.yaml lives in user_data/
        preset_path = webui_root / "user_data" / "presets" / f"{preset_name}.yaml"
        if not preset_path.exists():
            preset_path = path.parent / "presets" / f"{preset_name}.yaml"
        if preset_path.exists():
            try:
                with open(preset_path) as f:
                    preset_data = yaml.safe_load(f) or {}
                for k, v in preset_data.items():
                    if k in WEBUI_GEN_KEYS:
                        params[k] = v
                log.info("Loaded preset '%s' from disk: %s", preset_name, list(preset_data.keys()))
            except Exception as e:
                log.warning("Failed to load preset '%s' from disk: %s", preset_name, e)
        else:
            log.info("Preset file not found: %s", preset_path)

    return params, preset_name


def _load_webui_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Load generation params — try disk settings.yaml + preset."""
    params, _ = _load_webui_settings_from_disk(config)
    return params


# ---------------------------------------------------------------------------
# Backend management
# ---------------------------------------------------------------------------


def _load_backends(config: dict[str, Any]) -> None:
    """Load backend configuration from config.yaml."""
    global _backends, _active_backend, TGWUI_BASE, TGWUI_INTERNAL
    backends_cfg = config.get("backends", {})
    if not backends_cfg:
        _backends = {"local": {"url": TGWUI_BASE, "priority": 1, "optional": False, "healthy": None}}
        _active_backend = "local"
        return

    _backends = {}
    for name, cfg in backends_cfg.items():
        url = cfg.get("url", "")
        if not url:
            continue
        _backends[name] = {
            "url": url,
            "priority": cfg.get("priority", 99),
            "optional": cfg.get("optional", False),
            "healthy": None,
        }

    if _backends:
        _active_backend = min(_backends, key=lambda n: _backends[n]["priority"])
        TGWUI_BASE = _backends[_active_backend]["url"]
        TGWUI_INTERNAL = TGWUI_BASE.rstrip("/") + "/internal"
        log.info("Active backend: %s (%s)", _active_backend, TGWUI_BASE)


def set_active_backend(name: str, url: str) -> None:
    """Update the active backend (called by client.py during failover)."""
    global _active_backend, TGWUI_BASE, TGWUI_INTERNAL
    _active_backend = name
    TGWUI_BASE = url
    TGWUI_INTERNAL = url.rstrip("/") + "/internal"
    log.info("Switched to backend: %s (%s)", name, TGWUI_BASE)


# ---------------------------------------------------------------------------
# Config reload
# ---------------------------------------------------------------------------


def reload_config() -> None:
    """Reload config and webui settings from disk.

    NOTE: For async callers, use reload_config_safe() which acquires the lock.
    This sync version exists for the module-level init call.
    """
    global _config, _webui_settings, _webui_preset_name
    _config = _load_config()

    for problem in _validate_config(_config):
        log.warning("config.yaml: %s", problem)

    _webui_settings = _load_webui_settings(_config)

    path_str = _config.get("webui_settings", "")
    if path_str:
        path = Path(os.path.expanduser(path_str))
        if path.exists():
            try:
                with open(path) as f:
                    raw = yaml.safe_load(f) or {}
                _webui_preset_name = raw.get("preset")
            except Exception:
                pass

    _load_backends(_config)
    log.info(
        "Config loaded. preset: %s, webui params: %s, config defaults: %s",
        _webui_preset_name,
        list(_webui_settings.keys()),
        list(_config.get("defaults", {}).keys()),
    )


async def reload_config_safe() -> None:
    """Async-safe config reload — acquires lock to prevent partial state reads."""
    async with _config_lock:
        reload_config()
    # Clear response cache since generation params may have changed
    try:
        from localforge.client import _cache

        _cache.clear()
        log.info("Response cache cleared after config reload")
    except ImportError:
        pass


async def set_runtime_overrides_safe(overrides: dict[str, Any]) -> None:
    """Async-safe runtime override update — acquires lock."""
    async with _config_lock:
        _runtime_overrides.update(overrides)


# ---------------------------------------------------------------------------
# Generation parameter resolution
# ---------------------------------------------------------------------------


def get_generation_params(model_name: str | None = None) -> dict[str, Any]:
    """Resolve generation params for the current model.

    Merge order (later wins): webui_settings → config defaults → model match → runtime
    """
    params: dict[str, Any] = {}

    # 1. webui settings baseline
    params.update(_webui_settings)

    # 2. config.yaml defaults
    defaults = _config.get("defaults", {})
    for k, v in defaults.items():
        if k != "system_suffix":
            params[k] = v

    # 3. model-specific overrides
    model_overrides = _config.get("models", {})
    if model_name:
        for pattern, overrides in model_overrides.items():
            if pattern in model_name:
                for k, v in overrides.items():
                    if k != "system_suffix":
                        params[k] = v
                break  # first match wins

    # 4. runtime overrides
    for k, v in _runtime_overrides.items():
        if k != "system_suffix":
            params[k] = v

    # Remove keys that are config-only (not sent to the API)
    params.pop("ctx_size", None)
    params.pop("gpu_layers", None)

    return params


def get_system_suffix(model_name: str | None = None) -> str:
    """Get the system_suffix for the current model (merged config)."""
    suffix = _config.get("defaults", {}).get("system_suffix", "")

    model_overrides = _config.get("models", {})
    if model_name:
        for pattern, overrides in model_overrides.items():
            if pattern in model_name:
                if "system_suffix" in overrides:
                    suffix = overrides["system_suffix"]
                break

    if "system_suffix" in _runtime_overrides:
        suffix = _runtime_overrides["system_suffix"]

    return suffix


def trace_param_source(key: str, value: Any, matched_pattern: str | None) -> str:
    """Determine which layer a resolved param value came from (for display)."""
    if key in _runtime_overrides:
        return "runtime"
    if matched_pattern:
        model_overrides = _config.get("models", {}).get(matched_pattern, {})
        if key in model_overrides:
            return f"model:{matched_pattern}"
    defaults = _config.get("defaults", {})
    if key in defaults:
        return "config"
    if key in _webui_settings:
        return "preset" if _webui_preset_name else "webui"
    return "default"


# ---------------------------------------------------------------------------
# Context and preamble system
# ---------------------------------------------------------------------------


def get_system_preamble() -> str | None:
    """Build system preamble from current context, character, and mode."""
    parts = []

    if _current_character and _current_character.get("system_prompt"):
        parts.append(_current_character["system_prompt"].strip())

    if _context:
        lang = _context.get("language", "")
        project = _context.get("project", "")
        rules = _context.get("rules", "")

        key = f"{lang}/{project}".lower().strip("/")
        if key in PREAMBLE_REGISTRY:
            parts.append(PREAMBLE_REGISTRY[key])
        else:
            ctx_parts = []
            if lang:
                ctx_parts.append(f"Language: {lang}")
            if project:
                ctx_parts.append(f"Project: {project}")
            if rules:
                ctx_parts.append(f"Rules:\n{rules}")
            if ctx_parts:
                parts.append("Context for this session:\n" + "\n".join(ctx_parts))

    if not parts:
        return None
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def sanitize_topic(raw: str) -> str:
    """Sanitize a note topic to a safe filename component."""
    topic = raw.strip().replace("/", "-").replace("\\", "-").replace(" ", "-")
    topic = re.sub(r"\.{2,}", ".", topic)
    topic = re.sub(r"[^\w\-.]", "", topic)
    if not topic:
        topic = "unnamed"
    return topic


def safe_note_path(topic: str) -> Path | None:
    """Return a safe path within the notes directory, or None if traversal detected."""
    nd = notes_dir()
    path = (nd / f"{topic}.md").resolve()
    if not str(path).startswith(str(nd.resolve())):
        return None
    return path


# ---------------------------------------------------------------------------
# Initialise on import
# ---------------------------------------------------------------------------
reload_config()
