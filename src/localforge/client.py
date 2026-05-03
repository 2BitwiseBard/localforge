"""HTTP client for LocalForge.

Manages the httpx connection pool, chat completions with retry and caching,
model resolution, async backend health/failover, and GPU pool mesh routing.
"""

import contextvars
import logging
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from localforge import config as cfg
from localforge.cache import ResponseCache
from localforge.exceptions import BackendUnreachableError, ModelNotLoadedError

log = logging.getLogger("localforge")

# ---------------------------------------------------------------------------
# Task type context — tools set this before calling chat() so routing
# can pick the best backend without changing every tool's signature.
# ---------------------------------------------------------------------------
_task_type_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("_task_type_ctx", default="default")

# GPU pool reference — set by gateway.py during lifespan startup
_gpu_pool = None


def set_gpu_pool(pool) -> None:
    """Set the GPU pool reference for mesh-aware routing."""
    global _gpu_pool
    _gpu_pool = pool


def set_task_type(task_type: str) -> contextvars.Token:
    """Set the task type hint for the current async context.

    Tools call this before chat() so the router can pick the best backend.
    Returns a token that can be used to reset the value.

    Example:
        token = set_task_type("code")
        try:
            result = await chat(prompt)
        finally:
            _task_type_ctx.reset(token)
    """
    return _task_type_ctx.set(task_type)


class task_type_context:
    """Context manager for setting task type around chat() calls.

    Usage:
        async with task_type_context("code"):
            result = await chat(prompt)
    """

    __slots__ = ("_token",)

    def __init__(self, task_type: str):
        self._token = _task_type_ctx.set(task_type)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        _task_type_ctx.reset(self._token)
        return False


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
_cache = ResponseCache()  # reads ttl/max_entries/max_bytes from config.yaml

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
        raise ModelNotLoadedError("No model loaded in text-generation-webui")
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
    except (httpx.HTTPError, OSError):
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
    except (httpx.HTTPError, OSError, KeyError, ValueError) as e:
        log.debug("Could not fetch params from API (falling back to disk): %s", e)
        return None


async def reload_webui_params_from_api() -> None:
    """Refresh webui settings from the live API, updating in-memory state."""
    result = await fetch_resolved_params_from_api()
    if result is not None:
        cfg._webui_settings, cfg._webui_preset_name = result
        log.info(
            "Refreshed webui params from API. preset: %s, keys: %s",
            cfg._webui_preset_name,
            list(cfg._webui_settings.keys()),
        )


# ---------------------------------------------------------------------------
# Chat completion with retry, caching, and backend fallback
# ---------------------------------------------------------------------------


async def _chat_to_backend(base_url: str, body: dict[str, Any]) -> str:
    """Send a chat completion to a specific backend URL. Returns response text."""
    resp = await _client.post(f"{base_url}/chat/completions", json=body)
    resp.raise_for_status()
    return _extract_content(resp.json())


async def _chat_to_worker(worker_url: str, body: dict[str, Any]) -> str:
    """Send a chat task to a mesh worker's /task endpoint. Returns response text.

    Workers use a different payload format than OpenAI-compatible backends:
    they accept {type: "chat", messages: [...], max_tokens: N, temperature: T}
    and return {response: "...", tokens: {...}, backend: "..."}.
    """
    payload = {
        "type": "chat",
        "messages": body.get("messages", []),
        "max_tokens": body.get("max_tokens", 1024),
        "temperature": body.get("temperature", 0.7),
    }
    resp = await _client.post(
        f"{worker_url}/task", json=payload, timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10)
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise ValueError(f"Worker error: {data['error']}")
    return data.get("response", "")


def _is_worker_url(url: str) -> bool:
    """Check if a URL points to a mesh worker (port 8200) vs a text-gen-webui backend."""
    # Workers typically run on :8200, backends on :5000
    # Also check if the URL is in the heartbeat registry
    if _gpu_pool is not None:
        for key in _gpu_pool._heartbeat_nodes:
            if key in url:
                return True
    # Heuristic: port 8200 is a worker
    return ":8200" in url


def _extract_content(data: Any) -> str:
    """Safely extract assistant message content from a chat completion response.

    Handles malformed responses, error payloads, and missing keys gracefully.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected response type: {type(data).__name__}")

    # Check for API error responses
    if "error" in data:
        err = data["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise ValueError(f"Backend returned error: {msg}")

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"Malformed chat completion response (missing choices/message/content): {e}") from e


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

    # Determine target backend URL via GPU pool routing (if available)
    task_type = _task_type_ctx.get()
    target_url = cfg.TGWUI_BASE
    routed_via_pool = False

    if _gpu_pool is not None:
        pool_url = _gpu_pool.route_request(task_type)
        if pool_url and pool_url != cfg.TGWUI_BASE:
            log.debug("GPU pool routed task_type=%s to %s", task_type, pool_url)
            target_url = pool_url
            routed_via_pool = True

    # Try the target backend (pool-routed or primary)
    try:
        # Use worker dispatch for mesh workers, backend dispatch for text-gen-webui
        if routed_via_pool and _is_worker_url(target_url):
            result = await _chat_to_worker(target_url, body)
            log.debug("Chat response: len=%d (via worker %s)", len(result), target_url)
        else:
            result = await _chat_to_backend(target_url, body)
            log.debug("Chat response: len=%d (via %s)", len(result), "pool" if routed_via_pool else "primary")
        _session_stats["total_tokens_out_approx"] += len(result) // 4
        if cache_k:
            _cache.put(cache_k, result)
        # Report success to pool circuit breaker
        if routed_via_pool and _gpu_pool is not None:
            backend = _gpu_pool.get_backend_by_url(target_url)
            if backend:
                backend.circuit.record_success()
        return result
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 404):
            log.warning("HTTP %d — clearing stale MODEL for re-resolution", e.response.status_code)
            cfg.MODEL = None
        raise
    except (httpx.ConnectError, httpx.ReadTimeout) as primary_err:
        # Report failure to pool circuit breaker
        if routed_via_pool and _gpu_pool is not None:
            backend = _gpu_pool.get_backend_by_url(target_url)
            if backend:
                backend.circuit.record_failure()

        # Fall back: try other backends from config + pool
        fallback_urls = _collect_fallback_urls(target_url)
        if not fallback_urls:
            raise BackendUnreachableError(
                f"Cannot connect to backend at {target_url}. "
                f"Is it running? Check your backend configuration in config.yaml."
            ) from primary_err

        for name, fallback_url in fallback_urls:
            log.info("Backend %s failed, trying fallback: %s (%s)", target_url, name, fallback_url)
            try:
                if _is_worker_url(fallback_url):
                    result = await _chat_to_worker(fallback_url, body)
                else:
                    result = await _chat_to_backend(fallback_url, body)
                log.info("Fallback %s succeeded (len=%d)", name, len(result))
                _session_stats["total_tokens_out_approx"] += len(result) // 4
                if cache_k:
                    _cache.put(cache_k, result)
                # Update pool circuit breaker on fallback success
                if _gpu_pool is not None:
                    fb_backend = _gpu_pool.get_backend_by_url(fallback_url)
                    if fb_backend:
                        fb_backend.circuit.record_success()
                return result
            except (httpx.HTTPError, OSError) as fallback_err:
                log.warning("Fallback %s also failed: %s", name, fallback_err)
                if name in cfg._backends:
                    cfg._backends[name]["healthy"] = False
                if _gpu_pool is not None:
                    fb_backend = _gpu_pool.get_backend_by_url(fallback_url)
                    if fb_backend:
                        fb_backend.circuit.record_failure()

        raise BackendUnreachableError(f"All backends unreachable. Target: {target_url}") from primary_err


def _collect_fallback_urls(failed_url: str) -> list[tuple[str, str]]:
    """Gather fallback backend URLs from config + GPU pool, excluding the failed URL."""
    seen = {failed_url.rstrip("/")}
    fallbacks: list[tuple[str, str]] = []

    # Config backends (ordered by priority)
    ordered = sorted(cfg._backends.items(), key=lambda x: x[1]["priority"])
    for name, info in ordered:
        url = info["url"].rstrip("/")
        if url in seen:
            continue
        seen.add(url)
        fallbacks.append((name, info["url"]))

    # GPU pool backends (healthy ones not already in the list)
    if _gpu_pool is not None:
        for b_status in _gpu_pool.status():
            url = b_status["url"].rstrip("/")
            if url in seen:
                continue
            if b_status["healthy"]:
                seen.add(url)
                fallbacks.append((b_status["name"], b_status["url"]))

        # Heartbeat-registered mesh workers with inference capability
        for w in _gpu_pool.get_mesh_workers():
            if not w.get("healthy"):
                continue
            caps = w.get("capabilities", {})
            if not caps.get("inference"):
                continue
            url = f"http://{w['key']}"
            if url.rstrip("/") in seen:
                continue
            seen.add(url.rstrip("/"))
            fallbacks.append((f"worker-{w['key']}", url))

    return fallbacks
