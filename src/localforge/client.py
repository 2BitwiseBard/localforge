"""HTTP client for LocalForge.

Manages the httpx connection pool, chat completions with retry and caching,
model resolution, and async backend health/failover.
"""

import logging
import time
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from localforge import config as cfg
from localforge.cache import ResponseCache

log = logging.getLogger("localforge")

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)

# ---------------------------------------------------------------------------
# Response cache (replaces inline dict from server.py)
# ---------------------------------------------------------------------------
_cache = ResponseCache(ttl=300, max_size=200)

# ---------------------------------------------------------------------------
# Session statistics
# ---------------------------------------------------------------------------
_session_stats: dict[str, Any] = {
    "total_calls": 0,
    "total_tokens_in_approx": 0,
    "total_tokens_out_approx": 0,
    "tool_calls": {},
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
}


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

async def resolve_model() -> str:
    """Get the currently loaded model name from text-gen-webui's internal API."""
    log.info("Resolving model from %s/model/info", cfg.TGWUI_INTERNAL)
    resp = await _client.get(f"{cfg.TGWUI_INTERNAL}/model/info", timeout=5)
    resp.raise_for_status()
    info = resp.json()
    model_name = info.get("model_name", "")
    if not model_name or model_name == "None":
        raise RuntimeError("No model loaded in text-generation-webui")
    log.info("Resolved model: %s", model_name)
    return model_name


# ---------------------------------------------------------------------------
# Backend health and failover
# ---------------------------------------------------------------------------

async def check_backend_health(name: str) -> bool:
    """Check if a backend is reachable and has a model loaded."""
    info = cfg._backends.get(name)
    if not info:
        return False
    url = info["url"].rstrip("/") + "/internal/health"
    try:
        resp = await _client.get(url, timeout=3)
        resp.raise_for_status()
        cfg._backends[name]["healthy"] = True
        return True
    except Exception:
        cfg._backends[name]["healthy"] = False
        return False


async def select_backend() -> str | None:
    """Select the best available backend. Returns backend name or None."""
    ordered = sorted(cfg._backends.items(), key=lambda x: x[1]["priority"])
    for name, info in ordered:
        if info.get("healthy") is True:
            if name != cfg._active_backend:
                cfg.set_active_backend(name, info["url"])
            return name
    # Nothing known healthy — probe each
    for name, info in ordered:
        if await check_backend_health(name):
            cfg.set_active_backend(name, info["url"])
            return name
    return cfg._active_backend  # fall back to whatever was set


async def fetch_resolved_params_from_api() -> tuple[dict[str, Any], str | None] | None:
    """Fetch resolved generation params from the webui API endpoint.

    Returns (params_dict, preset_name) or None if unavailable.
    """
    try:
        resp = await _client.get(f"{cfg.TGWUI_INTERNAL}/generation-params", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        preset_name = data.get("preset_name")
        raw_params = data.get("params", {})
        params = {k: v for k, v in raw_params.items() if k in cfg.WEBUI_GEN_KEYS}
        return params, preset_name
    except Exception as e:
        log.debug("Could not fetch params from API (falling back to disk): %s", e)
        return None


async def reload_webui_params_from_api() -> None:
    """Refresh webui settings from the live API, updating in-memory state."""
    result = await fetch_resolved_params_from_api()
    if result is not None:
        cfg._webui_settings, cfg._webui_preset_name = result
        log.info("Refreshed webui params from API. preset: %s, keys: %s",
                 cfg._webui_preset_name, list(cfg._webui_settings.keys()))


# ---------------------------------------------------------------------------
# Chat completion with retry, caching, and backend fallback
# ---------------------------------------------------------------------------

async def _chat_to_backend(base_url: str, body: dict[str, Any]) -> str:
    """Send a chat completion to a specific backend URL. Returns response text."""
    resp = await _client.post(f"{base_url}/chat/completions", json=body)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
    reraise=True,
)
async def chat(prompt: str, system: str | None = None, **kwargs: Any) -> str:
    """Send a chat completion request with retry, caching, and backend fallback."""
    if cfg.MODEL is None:
        cfg.MODEL = await resolve_model()

    # Cache check
    use_cache = kwargs.pop("use_cache", True)
    cache_k = None
    if use_cache:
        cache_k = _cache.make_key(prompt, system, cfg.MODEL, **kwargs)
        cached = _cache.get(cache_k)
        if cached is not None:
            log.debug("Cache hit (prompt_len=%d)", len(prompt))
            return cached

    # Track session stats
    _session_stats["total_calls"] += 1
    _session_stats["total_tokens_in_approx"] += len(prompt) // 4

    # Build system message: preamble + system_suffix
    suffix = cfg.get_system_suffix(cfg.MODEL)
    effective_system = system
    if suffix:
        if effective_system:
            effective_system = f"{effective_system}\n\n{suffix}"
        else:
            effective_system = suffix

    messages: list[dict[str, str]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": prompt})

    # Merge generation params: config → kwargs (caller overrides win)
    gen_params = cfg.get_generation_params(cfg.MODEL)
    gen_params.update(kwargs)

    body: dict[str, Any] = {
        "model": cfg.MODEL,
        "messages": messages,
        "stream": False,
        **gen_params,
    }

    log.debug("Chat request: model=%s, prompt_len=%d", cfg.MODEL, len(prompt))

    # Try primary backend
    try:
        result = await _chat_to_backend(cfg.TGWUI_BASE, body)
        log.debug("Chat response: len=%d", len(result))
        _session_stats["total_tokens_out_approx"] += len(result) // 4
        if cache_k:
            _cache.put(cache_k, result)
        return result
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 404):
            log.warning("HTTP %d — clearing stale MODEL for re-resolution", e.response.status_code)
            cfg.MODEL = None
        raise
    except (httpx.ConnectError, httpx.ReadTimeout) as primary_err:
        # Try fallback backends
        if len(cfg._backends) <= 1:
            raise
        cfg._backends.get(cfg._active_backend, {})["healthy"] = False
        ordered = sorted(cfg._backends.items(), key=lambda x: x[1]["priority"])
        for name, info in ordered:
            if name == cfg._active_backend:
                continue
            fallback_url = info["url"]
            log.info("Primary backend failed, trying fallback: %s (%s)", name, fallback_url)
            try:
                result = await _chat_to_backend(fallback_url, body)
                log.info("Fallback %s succeeded (len=%d)", name, len(result))
                info["healthy"] = True
                _session_stats["total_tokens_out_approx"] += len(result) // 4
                if cache_k:
                    _cache.put(cache_k, result)
                return result
            except Exception as fallback_err:
                log.warning("Fallback %s also failed: %s", name, fallback_err)
                info["healthy"] = False
        raise primary_err
