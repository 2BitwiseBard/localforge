"""Dashboard API routes for the web UI.

Supports multi-user profiles, chat history, photo gallery, notifications,
model management, search/RAG, knowledge graph, voice transcription.
"""

import asyncio
import base64
import io
import json
import os
import time
import uuid
from pathlib import Path
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

import httpx
import yaml

try:
    from localforge.paths import config_path as _config_path, data_dir as _data_dir, notes_dir as _notes_dir
    CONFIG_PATH = _config_path()
    NOTES_DIR = _notes_dir()
    DATA_ROOT = _data_dir()
except ImportError:
    CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
    NOTES_DIR = Path(__file__).parent.parent / "notes"
    DATA_ROOT = Path(__file__).parent.parent

from localforge.config import load_config_cached
from localforge.tools import _tool_handlers


async def _call_tool(name: str, args: dict | None = None) -> str:
    """Invoke a registered MCP tool handler by name and return its string result.

    Raises KeyError if the tool isn't registered. Raises whatever the handler raises.
    Centralizes the dashboard's tool-delegation pattern so routes stay thin.
    """
    handler = _tool_handlers.get(name)
    if handler is None:
        raise KeyError(f"tool not registered: {name}")
    return await handler(args or {})


# Set by gateway.py during lifespan
_supervisor = None

# Push notification subscriptions (in-memory, persisted to disk)
_push_subscriptions: dict[str, list[dict]] = {}
_push_subs_file = DATA_ROOT / "push_subscriptions.json"

# SSE notification clients
_sse_clients: dict[str, list[asyncio.Queue]] = {}

# Runtime generation parameter overrides (applied to all chat requests)
_gen_param_overrides: dict = {}

# Current hub mode and character (dashboard-side state)
_current_mode: dict = {}
_current_character: dict = {}

# TTL caches for expensive subprocess calls (nvidia-smi, ps aux)
_gpu_metrics_cache: tuple[float, dict | None] = (0.0, None)
_status_cache: tuple[float, dict | None] = (0.0, None)
_METRICS_CACHE_TTL = 15.0  # seconds


def _load_config() -> dict:
    return load_config_cached()


def _backend_url() -> str:
    cfg = _load_config()
    return cfg.get("backends", {}).get("local", {}).get("url", "http://localhost:5000/v1")


def _get_user(request: Request) -> dict:
    """Extract user profile from request (set by auth middleware)."""
    return getattr(request.state, "user", {"id": "admin", "name": "Admin", "role": "admin"})


import re as _re

_SAFE_USER_ID = _re.compile(r"^[a-zA-Z0-9_-]+$")


def _user_dir(base: str, user_id: str) -> Path:
    """Get user-scoped directory, creating if needed.

    Validates user_id to prevent path traversal attacks.
    """
    if not _SAFE_USER_ID.match(user_id):
        raise ValueError(f"Invalid user_id: {user_id!r}")
    d = DATA_ROOT / base / user_id
    # Belt-and-suspenders: ensure resolved path stays under DATA_ROOT
    if not str(d.resolve()).startswith(str(DATA_ROOT.resolve())):
        raise ValueError(f"Path traversal detected: {user_id!r}")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Notifications helper
# ---------------------------------------------------------------------------

async def notify_user(user_id: str, title: str, body: str, tag: str = "ai-hub"):
    """Send notification to a user via SSE and Web Push (if subscribed)."""
    queues = _sse_clients.get(user_id, [])
    event = json.dumps({"title": title, "body": body, "tag": tag, "timestamp": time.time()})
    for q in queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass
    # Fire-and-forget web push; don't let failures block the SSE path.
    asyncio.get_event_loop().create_task(
        _send_webpush(user_id, title, body, tag)
    )


async def notify_all(title: str, body: str, tag: str = "ai-hub"):
    """Send notification to all connected users."""
    for user_id in _sse_clients:
        await notify_user(user_id, title, body, tag)


# ---------------------------------------------------------------------------
# Web Push helpers
# ---------------------------------------------------------------------------

_push_subs_loaded: bool = False


def _ensure_push_subs_loaded() -> None:
    global _push_subs_loaded
    if _push_subs_loaded:
        return
    _push_subs_loaded = True
    try:
        data = json.loads(_push_subs_file.read_text())
        _push_subscriptions.update(data)
    except (OSError, json.JSONDecodeError):
        pass


def _save_push_subs() -> None:
    try:
        _push_subs_file.write_text(json.dumps(_push_subscriptions))
    except OSError:
        pass


def _load_vapid_keys() -> tuple[str, str]:
    """Return (public_key_b64url, private_key_pem). Empty strings if not configured."""
    cfg = _load_config()
    gw = cfg.get("gateway", {})
    pub = gw.get("vapid_public_key", "")
    priv_b64 = gw.get("vapid_private_key", "")
    if not pub or not priv_b64:
        return "", ""
    import base64 as _b64
    try:
        # private key stored as base64-encoded PEM
        priv_pem = _b64.urlsafe_b64decode(priv_b64 + "==").decode()
    except Exception:
        priv_pem = priv_b64  # assume already raw PEM
    return pub, priv_pem


async def _send_webpush(user_id: str, title: str, body: str, tag: str) -> None:
    """Deliver a Web Push notification to all push endpoints registered for user_id.

    Runs pywebpush in a thread executor so it doesn't block the event loop.
    Silently degrades when:
      - no push subscriptions for user
      - VAPID keys not in config
      - pywebpush not installed
    Subscriptions that return HTTP 410 (Gone) are automatically purged.
    """
    _ensure_push_subs_loaded()
    subs = _push_subscriptions.get(user_id, [])
    if not subs:
        return
    pub_key, priv_pem = _load_vapid_keys()
    if not pub_key or not priv_pem:
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return

    payload = json.dumps({"title": title, "body": body, "tag": tag})
    to_remove: list[dict] = []

    def _send_one(sub: dict) -> bool:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=priv_pem,
                vapid_claims={"sub": "mailto:localforge@localhost"},
            )
            return True
        except Exception as exc:
            # 410 = subscription expired/unsubscribed → clean up
            resp = getattr(exc, "response", None)
            if resp is not None and getattr(resp, "status_code", None) == 410:
                return False
            return True

    loop = asyncio.get_event_loop()
    results = await asyncio.gather(
        *[loop.run_in_executor(None, _send_one, s) for s in subs],
        return_exceptions=True,
    )
    expired = [s for s, keep in zip(subs, results) if keep is False]
    if expired:
        _push_subscriptions[user_id] = [s for s in subs if s not in expired]
        _save_push_subs()


async def api_push_vapid_key(request: Request) -> JSONResponse:
    """Return the VAPID public key so browsers can set up push subscriptions.

    Listed in PUBLIC_PATHS so it works before the user has authenticated.
    """
    pub, _ = _load_vapid_keys()
    if not pub:
        return JSONResponse({"error": "Push notifications not configured on this hub"}, status_code=503)
    return JSONResponse({"public_key": pub})


async def api_push_subscribe(request: Request) -> JSONResponse:
    """Save a browser push subscription for the current user.

    The browser calls this after navigator.serviceWorker.pushManager.subscribe().
    Body: the PushSubscription JSON (endpoint, keys.p256dh, keys.auth).
    """
    _ensure_push_subs_loaded()
    user = _get_user(request)
    user_id = user.get("id", "admin")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if "endpoint" not in body:
        return JSONResponse({"error": "Missing endpoint in subscription"}, status_code=400)
    subs = _push_subscriptions.setdefault(user_id, [])
    if not any(s.get("endpoint") == body["endpoint"] for s in subs):
        subs.append(body)
        _save_push_subs()
    return JSONResponse({"ok": True, "subscriptions": len(subs)})


# ---------------------------------------------------------------------------
# User info
# ---------------------------------------------------------------------------

async def api_me(request: Request) -> JSONResponse:
    """Return current user profile."""
    user = _get_user(request)
    return JSONResponse(user)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

async def api_status(request: Request) -> JSONResponse:
    backend_url = _backend_url()
    status = {"gateway": "ok", "timestamp": time.time()}

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{backend_url}/internal/model/info")
            if resp.status_code == 200:
                data = resp.json()
                status["model"] = {
                    "name": data.get("model_name", "unknown"),
                    "lora_names": data.get("lora_names", []),
                    "status": "loaded",
                }

            # Query llama-server directly for slot/context info
            # text-gen-webui runs llama-server on api_port + 5
            for llama_port in [5005, 5006, 5007]:
                try:
                    slot_resp = await client.get(
                        f"http://127.0.0.1:{llama_port}/slots", timeout=3
                    )
                    if slot_resp.status_code == 200:
                        slots_data = slot_resp.json()
                        slot_count = len(slots_data)
                        ctx_per_slot = slots_data[0].get("n_ctx", 0) if slots_data else 0
                        active = sum(1 for s in slots_data if s.get("is_processing"))
                        status["slots"] = {
                            "total": slot_count,
                            "active": active,
                            "ctx_per_slot": ctx_per_slot,
                            "ctx_total": ctx_per_slot * slot_count,
                        }
                        break
                except Exception:
                    continue

            # Get llama-server process flags for gpu_layers, batch_size, etc.
            # Cached for 15s since process flags don't change between model swaps
            global _status_cache
            now = time.time()
            if now - _status_cache[0] < _METRICS_CACHE_TTL and _status_cache[1] is not None:
                status["server_config"] = _status_cache[1]
            else:
                try:
                    import re
                    proc = await asyncio.create_subprocess_exec(
                        "ps", "aux",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                    for line in stdout.decode().splitlines():
                        if "llama-server" in line and "--model" in line:
                            server_config = {}
                            for flag, key in [
                                (r"--gpu-layers\s+(\S+)", "gpu_layers"),
                                (r"--ctx-size\s+(\S+)", "ctx_size"),
                                (r"--parallel\s+(\S+)", "parallel"),
                                (r"--batch-size\s+(\S+)", "batch_size"),
                                (r"--flash-attn\s+(\S+)", "flash_attn"),
                            ]:
                                m = re.search(flag, line)
                                if m:
                                    server_config[key] = m.group(1)
                            if server_config:
                                status["server_config"] = server_config
                                _status_cache = (now, server_config)
                            break
                except Exception:
                    pass

    except Exception:
        status["model"] = {"status": "unreachable"}

    return JSONResponse(status)


# ---------------------------------------------------------------------------
# Chat (streaming)
# ---------------------------------------------------------------------------

MAX_CHAT_BODY_BYTES = 1_000_000  # 1 MB max request body


async def api_chat(request: Request) -> StreamingResponse:
    # Reject oversized requests to prevent memory exhaustion
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_CHAT_BODY_BYTES:
        return JSONResponse({"error": "Request body too large"}, status_code=413)

    body = await request.json()

    # Support full conversation history or single prompt (backward compat)
    messages = body.get("messages", [])
    if not messages:
        prompt = body.get("prompt", "")
        if prompt:
            messages = [{"role": "user", "content": prompt}]

    # Prepend system prompt if provided
    system_prompt = body.get("system_prompt", "")
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + [
            m for m in messages if m.get("role") != "system"
        ]

    backend_url = _backend_url()
    cfg = _load_config()
    defaults = cfg.get("defaults", {})
    params = {**defaults, **_gen_param_overrides}

    # Per-request overrides
    for k in ("temperature", "top_p", "top_k", "max_tokens", "min_p",
              "repetition_penalty", "presence_penalty", "frequency_penalty", "seed"):
        if k in body:
            params[k] = body[k]

    async def stream():
        try:
            request_body = {
                "messages": messages,
                "max_tokens": params.get("max_tokens", 4096),
                "stream": True,
            }
            for k in ("temperature", "top_p", "top_k", "min_p",
                       "repetition_penalty", "presence_penalty", "frequency_penalty", "seed"):
                if k in params and params[k] is not None:
                    request_body[k] = params[k]

            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{backend_url}/chat/completions",
                    json=request_body,
                    headers={"Content-Type": "application/json"},
                )
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            yield f"data: [DONE]\n\n"
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield f"data: {json.dumps({'content': content})}\n\n"
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Chat History
# ---------------------------------------------------------------------------

async def api_chat_list(request: Request) -> JSONResponse:
    """List saved chat conversations for the user (paginated)."""
    user = _get_user(request)
    chat_dir = _user_dir("chats", user["id"])
    page = int(request.query_params.get("page", "1"))
    limit = min(int(request.query_params.get("limit", "30")), 100)
    offset = (page - 1) * limit
    all_files = sorted(chat_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    total = len(all_files)
    chats = []
    for f in all_files[offset:offset + limit]:
        try:
            data = json.loads(f.read_text())
            chats.append({
                "id": f.stem,
                "title": data.get("title", "Untitled"),
                "message_count": len(data.get("messages", [])),
                "created": data.get("created", 0),
                "updated": data.get("updated", 0),
            })
        except Exception:
            continue
    return JSONResponse({"chats": chats, "total": total, "page": page, "has_more": offset + limit < total})


async def api_chat_load(request: Request) -> JSONResponse:
    """Load a saved chat conversation."""
    chat_id = request.path_params.get("chat_id", "")
    user = _get_user(request)
    chat_file = _user_dir("chats", user["id"]) / f"{chat_id}.json"
    if not chat_file.exists():
        return JSONResponse({"error": "Chat not found"}, status_code=404)
    return JSONResponse(json.loads(chat_file.read_text()))


async def api_chat_save(request: Request) -> JSONResponse:
    """Save a chat conversation."""
    user = _get_user(request)
    body = await request.json()
    messages = body.get("messages", [])
    title = body.get("title", "")
    chat_id = body.get("id", str(uuid.uuid4())[:8])

    chat_dir = _user_dir("chats", user["id"])
    chat_file = chat_dir / f"{chat_id}.json"

    # Auto-title from first user message
    if not title and messages:
        for m in messages:
            if m.get("role") == "user":
                title = m.get("content", "")[:60]
                break

    now = time.time()
    data = {
        "id": chat_id,
        "title": title or "Untitled",
        "messages": messages,
        "created": json.loads(chat_file.read_text()).get("created", now) if chat_file.exists() else now,
        "updated": now,
        "user": user["id"],
    }
    chat_file.write_text(json.dumps(data, indent=2))
    return JSONResponse({"id": chat_id, "title": data["title"]})


async def api_chat_delete(request: Request) -> JSONResponse:
    """Delete a saved chat."""
    chat_id = request.path_params.get("chat_id", "")
    user = _get_user(request)
    chat_file = _user_dir("chats", user["id"]) / f"{chat_id}.json"
    if chat_file.exists():
        chat_file.unlink()
    return JSONResponse({"deleted": chat_id})


# ---------------------------------------------------------------------------
# Model management
# ---------------------------------------------------------------------------

async def api_models(request: Request) -> JSONResponse:
    backend_url = _backend_url()
    result = {"current": None, "models": []}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            info_resp = await client.get(f"{backend_url}/internal/model/info")
            if info_resp.status_code == 200:
                result["current"] = info_resp.json().get("model_name")
            list_resp = await client.get(f"{backend_url}/internal/model/list")
            if list_resp.status_code == 200:
                data = list_resp.json()
                result["models"] = data.get("model_names", data) if isinstance(data, dict) else data
    except Exception as e:
        result["error"] = str(e)
    return JSONResponse(result)


async def api_swap(request: Request) -> JSONResponse:
    body = await request.json()
    model_name = body.get("model_name", "")
    if not model_name:
        return JSONResponse({"error": "model_name required"}, status_code=400)

    backend_url = _backend_url()

    # Resolve config.yaml model overrides (ctx_size, gpu_layers, etc.)
    cfg = _load_config()
    model_config: dict = {}
    for pattern, overrides in cfg.get("models", {}).items():
        if pattern in model_name:
            model_config = overrides
            break

    def _resolve(key: str, default=None):
        """Explicit request param > config.yaml model override > default."""
        if key in body:
            return body[key]
        if key in model_config:
            return model_config[key]
        return default

    # Build loading args — resolve each param through the config chain
    load_args: dict = {}
    LOAD_PARAM_KEYS = [
        "ctx_size", "gpu_layers", "threads", "threads_batch",
        "batch_size", "ubatch_size", "cache_type", "flash_attn",
        "rope_freq_base", "tensor_split", "parallel",
        "model_draft", "draft_max", "gpu_layers_draft", "ctx_size_draft",
        "spec_type", "spec_ngram_size_n", "spec_ngram_size_m", "spec_ngram_min_hits",
    ]
    for key in LOAD_PARAM_KEYS:
        val = _resolve(key)
        if val is not None:
            load_args[key] = val

    # Ensure ctx_size and gpu_layers always have safe defaults
    ctx_size = load_args.get("ctx_size", 8192)
    load_args.setdefault("ctx_size", ctx_size)
    load_args.setdefault("gpu_layers", -1)

    payload: dict = {
        "model_name": model_name,
        "args": load_args,
        "settings": {"truncation_length": ctx_size},
    }

    applied = {k: v for k, v in load_args.items() if v is not None}
    config_source = model_config.get("ctx_size") and f" (from config: {list(model_config.keys())})" or ""

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(f"{backend_url}/internal/model/load", json=payload)
            if resp.status_code == 200:
                await notify_all("Model Swapped", f"Now running: {model_name}", "model-swap")
                return JSONResponse({"status": "ok", "model": model_name, "applied": applied})
            return JSONResponse({"error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Search / RAG
# ---------------------------------------------------------------------------

async def api_indexes(request: Request) -> JSONResponse:
    index_dir = DATA_ROOT / "indexes"
    indexes = []
    if index_dir.exists():
        for d in sorted(index_dir.iterdir()):
            if d.is_dir():
                indexes.append(d.name)
    return JSONResponse({"indexes": indexes})


async def api_search(request: Request) -> JSONResponse:
    body = await request.json()
    query = body.get("query", "")
    index_name = body.get("index_name", "")
    mode = body.get("mode", "hybrid")

    if not query or not index_name:
        return JSONResponse({"error": "query and index_name required"}, status_code=400)

    tool_name = "rag_query" if mode == "rag" else "hybrid_search"
    args = (
        {"index_name": index_name, "question": query}
        if mode == "rag"
        else {"index_name": index_name, "query": query}
    )
    try:
        result = await _call_tool(tool_name, args)
        return JSONResponse({"mode": mode, "result": result})
    except KeyError:
        return JSONResponse({"error": "Search tools not available"}, status_code=500)
    except (httpx.HTTPError, OSError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# System metrics
# ---------------------------------------------------------------------------

async def api_metrics(request: Request) -> JSONResponse:
    global _gpu_metrics_cache
    now = time.time()
    if now - _gpu_metrics_cache[0] < _METRICS_CACHE_TTL and _gpu_metrics_cache[1] is not None:
        return JSONResponse(_gpu_metrics_cache[1])

    metrics = {"gpu": None, "backends": []}
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
            "--format=csv,nounits,noheader",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if stdout:
            parts = stdout.decode().strip().split(", ")
            if len(parts) >= 5:
                metrics["gpu"] = {
                    "name": parts[0],
                    "vram_used_mb": int(parts[1]),
                    "vram_total_mb": int(parts[2]),
                    "utilization_pct": int(parts[3]),
                    "temperature_c": int(parts[4]),
                }
    except Exception:
        pass
    _gpu_metrics_cache = (now, metrics)
    return JSONResponse(metrics)


# ---------------------------------------------------------------------------
# Photo Gallery
# ---------------------------------------------------------------------------

async def api_photos_list(request: Request) -> JSONResponse:
    """List photos for the user."""
    user = _get_user(request)
    photo_dir = _user_dir("photos", user["id"])
    photos = []

    for f in sorted(photo_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            meta_file = f.with_suffix(".json")
            meta = {}
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                except Exception:
                    pass
            photos.append({
                "filename": f.name,
                "size": f.stat().st_size,
                "uploaded": f.stat().st_mtime,
                "description": meta.get("description", ""),
                "tags": meta.get("tags", []),
                "thumbnail": f"/api/photos/{f.name}?thumb=1",
                "url": f"/api/photos/{f.name}",
            })

    return JSONResponse({"photos": photos})


async def api_photos_get(request: Request) -> StreamingResponse:
    """Serve a photo file (with optional thumbnail)."""
    user = _get_user(request)
    filename = request.path_params.get("filename", "")
    thumb = request.query_params.get("thumb", "")

    photo_dir = _user_dir("photos", user["id"])
    photo_path = photo_dir / filename

    if not photo_path.exists() or not photo_path.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)

    if thumb:
        # Generate thumbnail
        try:
            from PIL import Image
            img = Image.open(photo_path)
            img.thumbnail((200, 200))
            buf = io.BytesIO()
            fmt = "JPEG" if photo_path.suffix.lower() in (".jpg", ".jpeg") else "PNG"
            img.save(buf, format=fmt)
            buf.seek(0)
            content_type = "image/jpeg" if fmt == "JPEG" else "image/png"
            return StreamingResponse(buf, media_type=content_type)
        except Exception:
            pass  # Fall through to full image

    # Serve full image
    content_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    }.get(photo_path.suffix.lower(), "application/octet-stream")

    return StreamingResponse(open(photo_path, "rb"), media_type=content_type)


async def api_photos_upload(request: Request) -> JSONResponse:
    """Upload a photo and optionally auto-tag with vision model."""
    user = _get_user(request)
    form = await request.form()
    image_file = form.get("image")
    description = form.get("description", "")
    auto_tag = form.get("auto_tag", "true") == "true"

    if not image_file:
        return JSONResponse({"error": "No image"}, status_code=400)

    photo_dir = _user_dir("photos", user["id"])

    # Save file
    ext = Path(image_file.filename or "photo.jpg").suffix or ".jpg"
    photo_id = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
    filename = f"{photo_id}{ext}"
    photo_path = photo_dir / filename

    content = await image_file.read()
    photo_path.write_bytes(content)

    meta = {"description": description, "tags": [], "uploaded_by": user["id"]}

    # Auto-tag with vision model if available
    if auto_tag and not description:
        try:
            backend_url = _backend_url()
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{backend_url}/internal/model/info")
                if resp.status_code == 200:
                    model_name = resp.json().get("model_name", "").lower()
                    if any(kw in model_name for kw in ["vl", "vision"]):
                        # Vision model loaded — analyze
                        b64 = base64.b64encode(content).decode("utf-8")
                        ct = image_file.content_type or "image/jpeg"
                        tag_resp = await httpx.AsyncClient(timeout=60).post(
                            f"{backend_url}/chat/completions",
                            json={
                                "messages": [{
                                    "role": "user",
                                    "content": [
                                        {"type": "image_url", "image_url": {"url": f"data:{ct};base64,{b64}"}},
                                        {"type": "text", "text": "Describe this image in 2-3 sentences. Then list 5-8 tags as comma-separated keywords."},
                                    ],
                                }],
                                "max_tokens": 256,
                                "stream": False,
                            },
                        )
                        if tag_resp.status_code == 200:
                            result = tag_resp.json()
                            text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                            meta["description"] = text.split("\n")[0][:200]
                            # Extract tags
                            if "tags:" in text.lower():
                                tag_line = text.lower().split("tags:")[-1].strip()
                                meta["tags"] = [t.strip() for t in tag_line.split(",")][:10]
        except Exception:
            pass  # Auto-tag is best-effort

    # Save metadata
    meta_path = photo_path.with_suffix(".json")
    meta_path.write_text(json.dumps(meta, indent=2))

    return JSONResponse({"filename": filename, "description": meta.get("description", ""), "tags": meta.get("tags", [])})


async def api_photos_search(request: Request) -> JSONResponse:
    """Search photos by description/tags."""
    body = await request.json()
    query = body.get("query", "").lower()
    user = _get_user(request)
    photo_dir = _user_dir("photos", user["id"])

    results = []
    for f in photo_dir.glob("*.json"):
        try:
            meta = json.loads(f.read_text())
            desc = meta.get("description", "").lower()
            tags = " ".join(meta.get("tags", [])).lower()
            if query in desc or query in tags:
                img_file = f.with_suffix("")  # Remove .json
                # Find the actual image extension
                for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    img_path = f.parent / (f.stem + ext)
                    if img_path.exists():
                        results.append({
                            "filename": img_path.name,
                            "description": meta.get("description", ""),
                            "tags": meta.get("tags", []),
                            "url": f"/api/photos/{img_path.name}",
                            "thumbnail": f"/api/photos/{img_path.name}?thumb=1",
                        })
                        break
        except Exception:
            continue

    return JSONResponse({"results": results})


# ---------------------------------------------------------------------------
# Image upload (vision) — Chat tab
# ---------------------------------------------------------------------------

async def api_upload_image(request: Request) -> StreamingResponse:
    form = await request.form()
    image_file = form.get("image")
    question = form.get("question", "Describe this image in detail.")

    if not image_file:
        async def err():
            yield f"data: {json.dumps({'error': 'No image uploaded'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    image_bytes = await image_file.read()
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    content_type = image_file.content_type or "image/png"
    backend_url = _backend_url()

    is_vision = False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{backend_url}/internal/model/info")
            if resp.status_code == 200:
                model_name = resp.json().get("model_name", "").lower()
                is_vision = any(kw in model_name for kw in ["vl", "vision", "image"])
    except Exception:
        pass

    if not is_vision:
        async def vision_err():
            yield f"data: {json.dumps({'error': 'Current model is not vision-capable. Load a VL model first.'})}\n\n"
        return StreamingResponse(vision_err(), media_type="text/event-stream")

    async def stream():
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{backend_url}/chat/completions",
                    json={
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}},
                                {"type": "text", "text": question},
                            ],
                        }],
                        "max_tokens": 2048,
                        "stream": True,
                    },
                )
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            yield f"data: [DONE]\n\n"
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield f"data: {json.dumps({'content': content})}\n\n"
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Knowledge Graph
# ---------------------------------------------------------------------------

_kg = None

def _get_kg():
    global _kg
    if _kg is None:
        from localforge.knowledge.graph import KnowledgeGraph
        _kg = KnowledgeGraph()
    return _kg


async def api_kg_stats(request: Request) -> JSONResponse:
    try:
        return JSONResponse(_get_kg().stats())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_kg_search(request: Request) -> JSONResponse:
    body = await request.json()
    query = body.get("query", "")
    entity_type = body.get("entity_type")
    if not query:
        return JSONResponse({"error": "query required"}, status_code=400)
    try:
        return JSONResponse({"results": _get_kg().query(query, max_results=20, entity_type=entity_type)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_kg_context(request: Request) -> JSONResponse:
    body = await request.json()
    name = body.get("name", "")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    try:
        return JSONResponse(_get_kg().context(name))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_kg_add(request: Request) -> JSONResponse:
    body = await request.json()
    name = body.get("name", "")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    try:
        entity_id = _get_kg().add_entity(
            name=name, type=body.get("entity_type", "concept"), content=body.get("content", "")
        )
        return JSONResponse({"id": entity_id, "name": name})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

async def api_agents(request: Request) -> JSONResponse:
    if _supervisor:
        return JSONResponse({"agents": _supervisor.list_agents()})
    agents_yaml = Path(__file__).parent.parent / "agents.yaml"
    if agents_yaml.exists():
        with open(agents_yaml) as f:
            cfg = yaml.safe_load(f) or {}
        agents = []
        for agent_id, acfg in cfg.get("agents", {}).items():
            agents.append({
                "id": agent_id, "type": acfg.get("type", agent_id),
                "trust": acfg.get("trust", "monitor"), "schedule": acfg.get("schedule", ""),
                "enabled": acfg.get("enabled", True), "status": "unknown",
                "triggers": [t.get("type") for t in acfg.get("triggers", [])],
            })
        return JSONResponse({"agents": agents})
    return JSONResponse({"agents": []})


async def api_trigger_agent(request: Request) -> JSONResponse:
    agent_id = request.path_params.get("agent_id", "")
    if not _supervisor:
        return JSONResponse({"error": "Agent supervisor not running"}, status_code=503)
    result = await _supervisor.trigger_agent(agent_id, "manual")
    await notify_all("Agent Triggered", f"{agent_id} started", "agent")
    return JSONResponse({"message": result})


async def api_webhook(request: Request) -> JSONResponse:
    agent_id = request.path_params.get("agent_id", "")
    if not _supervisor:
        return JSONResponse({"error": "Agent supervisor not running"}, status_code=503)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    result = await _supervisor.trigger_agent(agent_id, "webhook", payload)
    return JSONResponse({"message": result})


async def api_agent_config(request: Request) -> JSONResponse:
    """Get or update an agent's configuration in agents.yaml."""
    agent_id = request.path_params.get("agent_id", "")
    if not agent_id or "/" in agent_id or ".." in agent_id:
        return JSONResponse({"error": "invalid agent_id"}, status_code=400)

    agents_yaml = Path(__file__).parent.parent / "agents.yaml"

    if request.method == "GET":
        # Return config for this agent
        if not agents_yaml.exists():
            return JSONResponse({"error": "agents.yaml not found"}, status_code=404)
        with open(agents_yaml) as f:
            cfg = yaml.safe_load(f) or {}
        acfg = cfg.get("agents", {}).get(agent_id)
        if not acfg:
            return JSONResponse({"error": f"Agent '{agent_id}' not found"}, status_code=404)
        return JSONResponse({"agent_id": agent_id, "config": acfg})

    # PATCH: update agent config fields
    body = await request.json()
    if not agents_yaml.exists():
        return JSONResponse({"error": "agents.yaml not found"}, status_code=404)

    with open(agents_yaml) as f:
        cfg = yaml.safe_load(f) or {}

    agents = cfg.get("agents", {})
    if agent_id not in agents:
        return JSONResponse({"error": f"Agent '{agent_id}' not found"}, status_code=404)

    acfg = agents[agent_id]
    changed = []

    # Allowed fields to update
    if "enabled" in body:
        acfg["enabled"] = bool(body["enabled"])
        changed.append("enabled")
    if "schedule" in body:
        sched = body["schedule"].strip()
        # Basic validation: must look like a cron expression
        if sched and len(sched.split()) >= 1:
            acfg["schedule"] = sched
            changed.append("schedule")
    if "trust" in body:
        if body["trust"] in ("monitor", "safe", "full"):
            acfg["trust"] = body["trust"]
            changed.append("trust")
    if "config" in body and isinstance(body["config"], dict):
        acfg["config"] = {**acfg.get("config", {}), **body["config"]}
        changed.append("config")

    if not changed:
        return JSONResponse({"error": "No valid fields to update"}, status_code=400)

    # Write back
    agents[agent_id] = acfg
    cfg["agents"] = agents
    with open(agents_yaml, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

    # If supervisor is running, try to apply changes live
    reload_msg = ""
    if _supervisor:
        if "enabled" in body:
            if body["enabled"] and agent_id not in _supervisor._agents:
                await _supervisor.spawn_agent(agent_id, acfg)
                reload_msg = f" | spawned live"
            elif not body["enabled"] and agent_id in _supervisor._agents:
                await _supervisor.stop_agent(agent_id)
                reload_msg = f" | stopped live"
        _supervisor._configs[agent_id] = acfg

    await notify_all("Agent Updated", f"{agent_id}: {', '.join(changed)}{reload_msg}", "agent")
    return JSONResponse({"agent_id": agent_id, "changed": changed, "config": acfg})


async def api_agent_logs(request: Request) -> JSONResponse:
    """Return log entries for a specific agent from its state file."""
    agent_id = request.path_params.get("agent_id", "")
    if not agent_id:
        return JSONResponse({"error": "agent_id required"}, status_code=400)
    # Sanitize
    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        return JSONResponse({"error": "invalid agent_id"}, status_code=400)

    logs = []
    last_run = None
    run_count = 0

    # Try live supervisor state first
    if _supervisor and agent_id in _supervisor._agents:
        agent = _supervisor._agents[agent_id]
        state = getattr(agent, "state", None)
        if state:
            logs = getattr(state, "logs", [])
            last_run = getattr(state, "last_run", None)
            run_count = getattr(state, "run_count", 0)
    else:
        # Fall back to state file on disk
        state_file = DATA_ROOT / "agent_state" / f"{agent_id}.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                logs = state.get("logs", [])
                last_run = state.get("last_run")
                run_count = state.get("run_count", 0)
            except Exception:
                pass

    return JSONResponse({
        "agent_id": agent_id,
        "logs": logs[-50:],  # last 50 entries
        "last_run": last_run,
        "run_count": run_count,
    })


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

async def api_notes(request: Request) -> JSONResponse:
    notes = []
    if NOTES_DIR.exists():
        for f in sorted(NOTES_DIR.iterdir()):
            if f.is_file() and f.suffix in (".txt", ".md", ""):
                notes.append({
                    "topic": f.stem,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                })
    return JSONResponse({"notes": notes})


async def api_note_content(request: Request) -> JSONResponse:
    """Return the content of a single note by topic name."""
    topic = request.path_params.get("topic", "")
    if not topic:
        return JSONResponse({"error": "topic required"}, status_code=400)
    # Sanitize: only allow simple names (no path traversal)
    if "/" in topic or "\\" in topic or ".." in topic:
        return JSONResponse({"error": "invalid topic"}, status_code=400)
    note_path = NOTES_DIR / f"{topic}.md"
    if not note_path.exists():
        note_path = NOTES_DIR / f"{topic}.txt"
    if not note_path.exists():
        note_path = NOTES_DIR / topic
    if not note_path.exists() or not note_path.is_file():
        return JSONResponse({"error": "Note not found"}, status_code=404)
    try:
        content = note_path.read_text(encoding="utf-8", errors="replace")
        return JSONResponse({"topic": topic, "content": content})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Voice: STT transcribe proxy
# ---------------------------------------------------------------------------

async def api_transcribe(request: Request) -> JSONResponse:
    form = await request.form()
    audio_file = form.get("file")
    if not audio_file:
        return JSONResponse({"error": "No audio file"}, status_code=400)

    audio_bytes = await audio_file.read()
    backend_url = _backend_url()

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{backend_url}/audio/transcriptions",
                files={"file": (audio_file.filename or "audio.webm", audio_bytes,
                                audio_file.content_type or "audio/webm")},
                data={"model": "whisper"},
            )
            if resp.status_code == 200:
                return JSONResponse(resp.json())
            return JSONResponse({"error": f"Transcription failed: {resp.status_code}"}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# SSE Notifications
# ---------------------------------------------------------------------------

async def api_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream for real-time notifications."""
    user = _get_user(request)
    user_id = user["id"]
    queue = asyncio.Queue(maxsize=50)

    if user_id not in _sse_clients:
        _sse_clients[user_id] = []
    _sse_clients[user_id].append(queue)

    async def event_stream():
        try:
            # Send initial connected event
            yield f"data: {json.dumps({'type': 'connected', 'user': user_id})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {event}\n\n"
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"  # prevent connection timeout
        except asyncio.CancelledError:
            pass
        finally:
            _sse_clients.get(user_id, []).remove(queue) if queue in _sse_clients.get(user_id, []) else None

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Generation Parameters
# ---------------------------------------------------------------------------

async def api_generation_params(request: Request) -> JSONResponse:
    """Get or update generation parameters applied to chat requests."""
    global _gen_param_overrides

    if request.method == "GET":
        cfg = _load_config()
        params = {**cfg.get("defaults", {}), **_gen_param_overrides}
        # Try backend params too
        backend_url = _backend_url()
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{backend_url}/internal/generation-params")
                if resp.status_code == 200:
                    params["backend"] = resp.json()
        except Exception:
            pass
        return JSONResponse(params)

    # POST: update params
    body = await request.json()
    if body.get("reset"):
        _gen_param_overrides.clear()
        return JSONResponse({"reset": True, "current": _load_config().get("defaults", {})})

    allowed = {"temperature", "top_p", "top_k", "max_tokens", "repetition_penalty",
               "min_p", "presence_penalty", "frequency_penalty", "seed"}
    changed = {}
    for k, v in body.items():
        if k in allowed:
            try:
                _gen_param_overrides[k] = float(v) if k != "seed" else int(v)
                changed[k] = _gen_param_overrides[k]
            except (ValueError, TypeError):
                continue

    if not changed:
        return JSONResponse({"error": "No valid parameters"}, status_code=400)
    await notify_all("Params Updated", ", ".join(f"{k}={v}" for k, v in changed.items()), "config")
    cfg = _load_config()
    return JSONResponse({"updated": changed, "current": {**cfg.get("defaults", {}), **_gen_param_overrides}})


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

async def api_presets(request: Request) -> JSONResponse:
    """List available generation presets with key parameters."""
    webui_dir = Path.home() / "Development" / "text-generation-webui"
    presets = []
    for d in [webui_dir / "presets", webui_dir / "user_data" / "presets"]:
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.suffix in ('.yaml', '.yml'):
                    entry = {"name": f.stem}
                    try:
                        with open(f) as fh:
                            data = yaml.safe_load(fh) or {}
                        for k in ("temperature", "top_p", "top_k", "min_p", "repetition_penalty"):
                            if k in data:
                                entry[k] = data[k]
                    except Exception:
                        pass
                    presets.append(entry)
    return JSONResponse({"presets": presets})


async def api_preset_load(request: Request) -> JSONResponse:
    """Load a preset's parameters as generation overrides."""
    global _gen_param_overrides
    body = await request.json()
    name = body.get("preset_name", "")
    if not name or "/" in name or ".." in name:
        return JSONResponse({"error": "Invalid preset name"}, status_code=400)

    webui_dir = Path.home() / "Development" / "text-generation-webui"
    preset_file = None
    for d in [webui_dir / "user_data" / "presets", webui_dir / "presets"]:
        f = d / f"{name}.yaml"
        if f.exists():
            preset_file = f
            break
    if not preset_file:
        return JSONResponse({"error": f"Preset '{name}' not found"}, status_code=404)

    try:
        with open(preset_file) as fh:
            data = yaml.safe_load(fh) or {}
        allowed = {"temperature", "top_p", "top_k", "min_p", "repetition_penalty",
                    "presence_penalty", "frequency_penalty"}
        applied = {}
        for k in allowed:
            if k in data:
                _gen_param_overrides[k] = data[k]
                applied[k] = data[k]
        await notify_all("Preset Loaded", f"{name}", "config")
        return JSONResponse({"preset": name, "applied": applied})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Model Controls
# ---------------------------------------------------------------------------

async def api_model_unload(request: Request) -> JSONResponse:
    """Unload the current model to free VRAM."""
    backend_url = _backend_url()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{backend_url}/internal/model/load",
                                      json={"model_name": "None"})
            await notify_all("Model Unloaded", "VRAM freed", "model-swap")
            return JSONResponse({"status": "unloaded"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_benchmark(request: Request) -> JSONResponse:
    """Run inference speed benchmark."""
    body = await request.json() if request.method == "POST" else {}
    prompt_length = body.get("prompt_length", "short")
    try:
        result = await _call_tool("benchmark", {"prompt_length": prompt_length})
        return JSONResponse({"result": result})
    except KeyError:
        return JSONResponse({"error": "benchmark not available"}, status_code=500)
    except (httpx.HTTPError, OSError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# LoRA Management
# ---------------------------------------------------------------------------

async def api_loras(request: Request) -> JSONResponse:
    """List available and loaded LoRA adapters."""
    webui_dir = Path.home() / "Development" / "text-generation-webui"
    lora_dir = webui_dir / "user_data" / "loras"
    available = []
    if lora_dir.exists():
        for d in sorted(lora_dir.iterdir()):
            if d.is_dir():
                available.append(d.name)
    loaded = []
    backend_url = _backend_url()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{backend_url}/internal/model/info")
            if resp.status_code == 200:
                loaded = resp.json().get("lora_names", [])
    except Exception:
        pass
    return JSONResponse({"available": available, "loaded": loaded})


async def api_lora_load(request: Request) -> JSONResponse:
    """Load LoRA adapter(s)."""
    body = await request.json()
    names = body.get("lora_names", [])
    if not names:
        return JSONResponse({"error": "lora_names required"}, status_code=400)
    weights = body.get("lora_weights", [1.0] * len(names))
    try:
        result = await _call_tool("load_lora", {"lora_names": names, "lora_weights": weights})
        await notify_all("LoRA Loaded", ", ".join(names), "config")
        return JSONResponse({"result": result})
    except KeyError:
        return JSONResponse({"error": "load_lora not available"}, status_code=500)
    except (httpx.HTTPError, OSError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_lora_unload(request: Request) -> JSONResponse:
    """Unload all LoRA adapters."""
    try:
        result = await _call_tool("unload_loras")
        await notify_all("LoRAs Unloaded", "All adapters removed", "config")
        return JSONResponse({"result": result})
    except KeyError:
        return JSONResponse({"error": "unload_loras not available"}, status_code=500)
    except (httpx.HTTPError, OSError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Index Management
# ---------------------------------------------------------------------------

async def api_index_create(request: Request) -> JSONResponse:
    """Create a new RAG index."""
    body = await request.json()
    name = body.get("name", "")
    directory = body.get("directory", "")
    if not name or not directory:
        return JSONResponse({"error": "name and directory required"}, status_code=400)
    if "/" in name or ".." in name:
        return JSONResponse({"error": "invalid index name"}, status_code=400)
    params = {
        "name": name,
        "directory": directory,
        "glob_pattern": body.get("glob_pattern", "**/*.*"),
    }
    if body.get("embed"):
        params["embed"] = True
    try:
        result = await _call_tool("index_directory", params)
        await notify_all("Index Created", name, "search")
        return JSONResponse({"result": result})
    except KeyError:
        return JSONResponse({"error": "index_directory not available"}, status_code=500)
    except (httpx.HTTPError, OSError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_index_delete(request: Request) -> JSONResponse:
    """Delete a RAG index."""
    name = request.path_params.get("name", "")
    if not name or "/" in name or ".." in name:
        return JSONResponse({"error": "invalid index name"}, status_code=400)
    try:
        result = await _call_tool("delete_index", {"index_name": name})
        await notify_all("Index Deleted", name, "search")
        return JSONResponse({"result": result})
    except KeyError:
        return JSONResponse({"error": "delete_index not available"}, status_code=500)
    except (httpx.HTTPError, OSError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_index_stats(request: Request) -> JSONResponse:
    """Get statistics for a RAG index."""
    name = request.path_params.get("name", "")
    if not name or "/" in name or ".." in name:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    index_dir = DATA_ROOT / "indexes" / name
    if not index_dir.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    stats = {"name": name, "files": 0, "chunks": 0, "has_embeddings": False, "size_bytes": 0}
    try:
        for f in index_dir.rglob("*"):
            if f.is_file():
                stats["size_bytes"] += f.stat().st_size
        if (index_dir / "metadata.json").exists():
            meta = json.loads((index_dir / "metadata.json").read_text())
            stats["files"] = meta.get("file_count", 0)
            stats["chunks"] = meta.get("chunk_count", stats.get("chunks", 0))
            stats["has_embeddings"] = meta.get("has_embeddings", False)
            stats["glob_pattern"] = meta.get("glob_pattern", "")
            stats["directory"] = meta.get("directory", "")
    except Exception:
        pass
    return JSONResponse(stats)


async def api_index_refresh(request: Request) -> JSONResponse:
    """Incrementally update a RAG index."""
    name = request.path_params.get("name", "")
    if not name or "/" in name or ".." in name:
        return JSONResponse({"error": "invalid index name"}, status_code=400)
    try:
        result = await _call_tool("incremental_index", {"index_name": name})
        await notify_all("Index Refreshed", name, "search")
        return JSONResponse({"result": result})
    except KeyError:
        return JSONResponse({"error": "incremental_index not available"}, status_code=500)
    except (httpx.HTTPError, OSError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# Set by gateway.py during lifespan
_message_bus = None
_task_queue = None


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------
async def api_videos_list(request: Request) -> JSONResponse:
    user = _get_user(request)
    video_dir = _user_dir("videos", user["id"])
    if not video_dir.exists():
        return JSONResponse({"videos": []})
    videos = []
    for f in sorted(video_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv", ".avi"}:
            meta_file = video_dir / f"{f.name}.meta.json"
            meta = {}
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                except Exception:
                    pass
            thumb_name = f.stem + ".jpg"
            videos.append({
                "filename": f.name,
                "url": f"/api/videos/{user['id']}/{f.name}",
                "thumbnail": f"/api/videos/{user['id']}/thumbs/{thumb_name}",
                "description": meta.get("description", ""),
                "tags": meta.get("tags", []),
                "duration": meta.get("duration_str", ""),
                "resolution": f"{meta.get('width', '')}x{meta.get('height', '')}" if meta.get("width") else "",
                "uploaded_at": meta.get("uploaded_at", 0),
            })
    return JSONResponse({"videos": videos})


async def api_videos_upload(request: Request) -> JSONResponse:
    user = _get_user(request)
    video_dir = _user_dir("videos", user["id"])
    thumbs_dir = video_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    form = await request.form()
    upload = form.get("video")
    if not upload:
        return JSONResponse({"error": "No video file"}, status_code=400)

    ext = Path(upload.filename).suffix.lower() or ".mp4"
    filename = f"{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}"
    video_path = video_dir / filename

    content = await upload.read()
    video_path.write_bytes(content)

    # Get metadata via ffprobe
    meta = {"uploaded_at": time.time(), "uploaded_by": user["id"]}
    try:
        from media.processor import get_video_metadata, create_video_thumbnail, format_duration
        video_meta = await get_video_metadata(video_path)
        if video_meta:
            meta.update(video_meta)
            meta["duration_str"] = format_duration(video_meta.get("duration", 0))

        # Generate thumbnail
        thumb_path = thumbs_dir / f"{video_path.stem}.jpg"
        await create_video_thumbnail(video_path, thumb_path)
    except Exception as e:
        meta["processing_error"] = str(e)

    # Auto-tag with vision model if requested
    auto_tag = form.get("auto_tag", "false")
    if auto_tag == "true":
        meta["description"] = f"Video: {upload.filename}"

    meta_path = video_dir / f"{filename}.meta.json"
    meta_path.write_text(json.dumps(meta))

    return JSONResponse({"filename": filename, "meta": meta})


async def api_videos_get(request: Request) -> StreamingResponse:
    user_id = request.path_params.get("user_id", "")
    filename = request.path_params.get("filename", "")
    video_dir = DATA_ROOT / "videos" / user_id

    video_path = video_dir / filename
    if not video_path.exists() or not video_path.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)

    from media.processor import content_type_for
    ct = content_type_for(filename)
    file_size = video_path.stat().st_size

    # Support Range requests for video seeking
    range_header = request.headers.get("range")
    if range_header:
        try:
            range_spec = range_header.replace("bytes=", "")
            start_str, end_str = range_spec.split("-")
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else min(start + 10 * 1024 * 1024, file_size - 1)
            end = min(end, file_size - 1)
            length = end - start + 1

            def range_gen():
                with open(video_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            return StreamingResponse(
                range_gen(),
                status_code=206,
                media_type=ct,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                },
            )
        except Exception:
            pass

    # Full file response
    def file_gen():
        with open(video_path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(file_gen(), media_type=ct, headers={
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
    })


async def api_video_thumb(request: Request) -> StreamingResponse:
    user_id = request.path_params.get("user_id", "")
    filename = request.path_params.get("filename", "")
    thumb_path = DATA_ROOT / "videos" / user_id / "thumbs" / filename

    if not thumb_path.exists():
        return JSONResponse({"error": "Thumbnail not found"}, status_code=404)

    return StreamingResponse(
        open(thumb_path, "rb"),
        media_type="image/jpeg",
    )


# ---------------------------------------------------------------------------
# Agent extensions (pause/resume, metrics, tasks, bus)
# ---------------------------------------------------------------------------
async def api_agent_pause(request: Request) -> JSONResponse:
    agent_id = request.path_params.get("agent_id", "")
    if _supervisor and _supervisor.pause_agent(agent_id):
        return JSONResponse({"status": "paused", "agent_id": agent_id})
    return JSONResponse({"error": f"Cannot pause {agent_id}"}, status_code=400)


async def api_agent_resume(request: Request) -> JSONResponse:
    agent_id = request.path_params.get("agent_id", "")
    if _supervisor and _supervisor.resume_agent(agent_id):
        return JSONResponse({"status": "resumed", "agent_id": agent_id})
    return JSONResponse({"error": f"Cannot resume {agent_id}"}, status_code=400)


async def api_agent_metrics(request: Request) -> JSONResponse:
    if not _supervisor:
        return JSONResponse({"error": "Supervisor not running"}, status_code=503)
    return JSONResponse(_supervisor.get_metrics())


async def api_agent_tasks(request: Request) -> JSONResponse:
    if not _task_queue:
        return JSONResponse({"tasks": []})
    return JSONResponse({"tasks": _task_queue.list_tasks(limit=50)})


async def api_agent_bus(request: Request) -> JSONResponse:
    if not _message_bus:
        return JSONResponse({"messages": []})
    return JSONResponse({"messages": _message_bus.get_history(limit=50)})


# ---------------------------------------------------------------------------
# Research sessions
# ---------------------------------------------------------------------------
def _get_research():
    from knowledge.research_sessions import ResearchSession
    return ResearchSession()


async def api_research_sessions(request: Request) -> JSONResponse:
    rs = _get_research()
    return JSONResponse({"sessions": rs.list_sessions()})


async def api_research_session_get(request: Request) -> JSONResponse:
    session_id = request.path_params.get("session_id", "")
    rs = _get_research()
    data = rs.get(session_id)
    if not data:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse(data)


async def api_research_start(request: Request) -> JSONResponse:
    body = await request.json()
    question = body.get("question", "").strip()
    if not question:
        return JSONResponse({"error": "Question required"}, status_code=400)

    rs = _get_research()
    session_id = rs.create(question)

    # Run deep_research in background
    async def _run():
        try:
            result = await _call_tool("deep_research", {
                "question": question,
                "max_sources": 5,
                "save_to_kg": True,
            })
            if isinstance(result, list):
                text = " ".join(
                    item.get("text", "") for item in result
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            else:
                text = str(result)
            rs.update_synthesis(session_id, text)
            rs.complete(session_id)
            await notify_all("Research Complete", question[:50], "research")
        except (KeyError, httpx.HTTPError, OSError, ValueError) as e:
            rs.update_synthesis(session_id, f"Error: {e}")
            rs.complete(session_id)

    asyncio.create_task(_run())
    return JSONResponse({"session_id": session_id, "status": "started"})


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------
def _get_workflow_dir():
    d = DATA_ROOT / "pipelines" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def api_workflows_list(request: Request) -> JSONResponse:
    wf_dir = _get_workflow_dir()
    workflows = []
    for f in sorted(wf_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            workflows.append({
                "id": data.get("id", f.stem),
                "name": data.get("name", f.stem),
                "description": data.get("description", ""),
                "node_count": len(data.get("nodes", [])),
            })
        except Exception:
            continue

    # Also check YAML templates
    templates_dir = Path(__file__).parent.parent / "workflows" / "templates"
    if templates_dir.exists():
        for f in templates_dir.glob("*.yaml"):
            try:
                import yaml as _yaml
                data = _yaml.safe_load(f.read_text())
                workflows.append({
                    "id": data.get("id", f.stem),
                    "name": data.get("name", f.stem) + " (template)",
                    "description": data.get("description", ""),
                    "node_count": len(data.get("nodes", [])),
                })
            except Exception:
                continue

    return JSONResponse({"workflows": workflows})


async def api_workflow_get(request: Request) -> JSONResponse:
    wf_id = request.path_params.get("workflow_id", "")
    wf_path = _get_workflow_dir() / f"{wf_id}.json"
    if wf_path.exists():
        return JSONResponse({"workflow": json.loads(wf_path.read_text())})
    return JSONResponse({"error": "Workflow not found"}, status_code=404)


async def api_workflow_save(request: Request) -> JSONResponse:
    body = await request.json()
    wf_id = body.get("id", uuid.uuid4().hex[:12])
    body["id"] = wf_id
    body.setdefault("name", "Unnamed")
    body["updated_at"] = time.time()
    wf_path = _get_workflow_dir() / f"{wf_id}.json"
    wf_path.write_text(json.dumps(body, indent=2))
    return JSONResponse({"id": wf_id, "status": "saved"})


async def api_workflow_run(request: Request) -> JSONResponse:
    body = await request.json()
    wf_data = body.get("workflow", {})
    initial_input = body.get("initial_input", "")

    from localforge.workflows.schema import WorkflowDef
    from localforge.workflows.engine import WorkflowEngine
    from localforge.client import chat as _chat_fn

    try:
        wf = WorkflowDef.from_dict(wf_data)

        async def chat_fn(prompt, system=""):
            return await _chat_fn(prompt, system=system)

        engine = WorkflowEngine(chat_fn=chat_fn)
        ctx = await asyncio.wait_for(engine.execute(wf, initial_input), timeout=300)
        return JSONResponse({
            "execution_id": ctx.execution_id,
            "status": ctx.status,
        })
    except asyncio.TimeoutError:
        return JSONResponse({"error": "Workflow timed out"}, status_code=504)
    except (ValueError, KeyError, httpx.HTTPError, OSError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_workflow_create(request: Request) -> JSONResponse:
    """POST /api/workflows — create or upsert. Auto-generates id if absent."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "object_required"}, status_code=400)
    wf_id = body.get("id") or uuid.uuid4().hex[:12]
    body["id"] = wf_id
    body.setdefault("name", "Untitled workflow")
    body.setdefault("description", "")
    body.setdefault("nodes", [])
    body.setdefault("edges", [])
    body.setdefault("variables", {})
    body["updated_at"] = time.time()
    body.setdefault("created_at", body["updated_at"])
    wf_path = _get_workflow_dir() / f"{wf_id}.json"
    wf_path.write_text(json.dumps(body, indent=2))
    return JSONResponse({"id": wf_id, "workflow": body})


async def api_workflow_delete(request: Request) -> JSONResponse:
    wf_id = request.path_params.get("workflow_id", "")
    if not wf_id or "/" in wf_id or ".." in wf_id:
        return JSONResponse({"error": "invalid_id"}, status_code=400)
    wf_path = _get_workflow_dir() / f"{wf_id}.json"
    if not wf_path.exists():
        return JSONResponse({"error": "not_found"}, status_code=404)
    wf_path.unlink()
    return JSONResponse({"id": wf_id, "status": "deleted"})


async def api_workflow_node_specs(request: Request) -> JSONResponse:
    """Returns the per-node-type form schemas the visual editor renders."""
    from localforge.workflows.node_specs import NODE_SPECS, categories
    return JSONResponse({"specs": NODE_SPECS, "categories": categories()})


async def api_workflow_executions(request: Request) -> JSONResponse:
    from localforge.workflows.engine import list_executions
    return JSONResponse({"executions": list_executions()})


async def api_workflow_execution_get(request: Request) -> JSONResponse:
    exec_id = request.path_params.get("execution_id", "")
    from localforge.workflows.engine import WorkflowContext
    ctx = WorkflowContext.load(exec_id)
    if not ctx:
        return JSONResponse({"error": "Execution not found"}, status_code=404)
    return JSONResponse(ctx.to_dict())


# ---------------------------------------------------------------------------
# KG Graph visualization
# ---------------------------------------------------------------------------
async def api_kg_graph(request: Request) -> JSONResponse:
    center = request.query_params.get("center", "")
    depth = int(request.query_params.get("depth", "2"))
    from localforge.knowledge.graph import KnowledgeGraph
    try:
        kg = KnowledgeGraph()
        data = kg.get_graph(center=center or None, depth=depth)
        return JSONResponse(data)
    except (OSError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Agent Approval Queue
# ---------------------------------------------------------------------------

# Set by gateway.py (or lazily initialized)
_approval_queue = None


def _get_approval_queue():
    global _approval_queue
    if _approval_queue is None:
        from localforge.agents.approval import ApprovalQueue
        _approval_queue = ApprovalQueue()
    return _approval_queue


async def api_approvals_list(request: Request) -> JSONResponse:
    """List pending approval requests."""
    aq = _get_approval_queue()
    return JSONResponse({
        "pending": aq.list_pending(),
        "recent": aq.list_recent(limit=10),
    })


async def api_approvals_decide(request: Request) -> JSONResponse:
    """Approve or deny a pending request."""
    body = await request.json()
    req_id = body.get("id", "")
    action = body.get("action", "")  # "approve" or "deny"
    if not req_id or action not in ("approve", "deny"):
        return JSONResponse({"error": "id and action (approve/deny) required"}, status_code=400)

    aq = _get_approval_queue()
    user = getattr(request.state, "user", {})
    decided_by = user.get("name", "dashboard")

    if action == "approve":
        ok = aq.approve(req_id, decided_by=decided_by)
    else:
        ok = aq.deny(req_id, decided_by=decided_by)

    if ok:
        await notify_all(
            f"Approval {action}d",
            f"Request {req_id} was {action}d by {decided_by}",
            f"approval-{action}",
        )
    return JSONResponse({"status": "ok" if ok else "not found"})


# ---------------------------------------------------------------------------
# Hub Mode & Character
# ---------------------------------------------------------------------------

async def api_modes(request: Request) -> JSONResponse:
    """List available modes and the current one."""
    config = _load_config()
    modes = config.get("modes", {})
    return JSONResponse({
        "modes": {k: {**v} for k, v in modes.items()},
        "current": _current_mode.get("name", ""),
    })


async def api_characters(request: Request) -> JSONResponse:
    """List available characters and the current one."""
    config = _load_config()
    characters = config.get("characters", {})
    return JSONResponse({
        "characters": {k: {"name": v.get("name", k)} for k, v in characters.items()},
        "current": _current_character.get("name", ""),
    })


async def api_set_mode(request: Request) -> JSONResponse:
    """Set the hub mode (updates generation params accordingly)."""
    global _current_mode
    body = await request.json()
    mode_name = body.get("mode", "")
    config = _load_config()
    modes = config.get("modes", {})

    if not mode_name:
        _current_mode = {}
        _gen_param_overrides.pop("temperature", None)
        _gen_param_overrides.pop("max_tokens", None)
        return JSONResponse({"status": "mode cleared"})

    if mode_name not in modes:
        return JSONResponse({"error": f"Unknown mode: {mode_name}"}, status_code=400)

    mode_cfg = modes[mode_name]
    _current_mode = {"name": mode_name, **mode_cfg}

    # Apply mode's generation param overrides
    if mode_cfg.get("temperature") is not None:
        _gen_param_overrides["temperature"] = mode_cfg["temperature"]
    if mode_cfg.get("max_tokens"):
        _gen_param_overrides["max_tokens"] = mode_cfg["max_tokens"]

    return JSONResponse({
        "status": "ok",
        "mode": mode_name,
        "temperature": mode_cfg.get("temperature"),
        "max_tokens": mode_cfg.get("max_tokens"),
        "prefer_model": mode_cfg.get("prefer_model", []),
    })


async def api_set_character(request: Request) -> JSONResponse:
    """Set the hub character/persona."""
    global _current_character
    body = await request.json()
    char_name = body.get("character", "")
    config = _load_config()
    characters = config.get("characters", {})

    if not char_name or char_name == "default":
        _current_character = {}
        return JSONResponse({"status": "character cleared"})

    if char_name not in characters:
        return JSONResponse({"error": f"Unknown character: {char_name}"}, status_code=400)

    char_cfg = characters[char_name]
    _current_character = {"name": char_name, **char_cfg}

    if char_cfg.get("temperature_override") is not None:
        _gen_param_overrides["temperature"] = char_cfg["temperature_override"]

    return JSONResponse({
        "status": "ok",
        "character": char_name,
        "name": char_cfg.get("name", char_name),
    })


# ---------------------------------------------------------------------------
# Compute Mesh — heartbeat receiver + mesh status
# ---------------------------------------------------------------------------

# In-memory registry of worker nodes — delegated to gpu_pool for unified state.
# The gpu_pool reference is set by gateway.py during lifespan startup.
_gpu_pool_ref = None  # Set by gateway.py

async def api_mesh_heartbeat(request: Request) -> JSONResponse:
    """Receive heartbeat from a worker node and update the mesh registry."""
    from localforge.auth import require_scope
    denied = require_scope(request, "mesh")
    if denied is not None:
        return denied

    # Body size guard — reject oversized payloads
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 65536:  # 64KB max for heartbeat
        return JSONResponse({"error": "Payload too large"}, status_code=413)

    body = await request.json()
    hostname = body.get("hostname", "")
    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)

    # Touch worker registry so last_seen stays fresh.
    user = _get_user(request)
    if user.get("role") == "worker":
        try:
            from localforge.enrollment import worker_registry
            worker_registry().touch(user["id"])
        except Exception:
            pass

    if _gpu_pool_ref is not None:
        key, accepted = _gpu_pool_ref.register_heartbeat(body)
        if not accepted:
            return JSONResponse({"error": "Mesh worker limit reached"}, status_code=503)
        return JSONResponse({"status": "ok", "registered": key})

    # Fallback if gpu_pool not yet initialized (shouldn't happen in practice)
    return JSONResponse({"error": "GPU pool not initialized"}, status_code=503)


async def api_mesh_status(request: Request) -> JSONResponse:
    """Return all registered mesh workers and their status."""
    workers: list[dict] = []
    if _gpu_pool_ref is not None:
        workers = list(_gpu_pool_ref.get_mesh_workers())

    # Merge in workers that have registered but haven't checked in yet.
    try:
        from localforge.enrollment import worker_registry
        seen_ids = {w.get("worker_id") or w.get("hostname") for w in workers}
        for rec in worker_registry().list_workers():
            if rec["worker_id"] in seen_ids or rec["hostname"] in seen_ids:
                continue
            workers.append({
                "worker_id": rec["worker_id"],
                "hostname": rec["hostname"],
                "platform": rec.get("platform"),
                "registered_at": rec.get("registered_at"),
                "last_seen": rec.get("last_seen"),
                "status": "registered (awaiting first heartbeat)",
            })
    except Exception:
        pass

    return JSONResponse({"workers": workers, "count": len(workers)})


# --- Onboarding: enrollment token, install scripts, register ---------------

_PLATFORM_SCRIPTS: dict[str, tuple[str, str]] = {
    # platform -> (script_path_relative_to_repo_root, content_type)
    "linux":   ("scripts/setup-worker.sh",         "text/x-shellscript"),
    "darwin":  ("scripts/setup-worker-darwin.sh",  "text/x-shellscript"),
    "win32":   ("scripts/setup-worker.ps1",        "text/plain"),
    "android": ("scripts/setup-worker-termux.sh",  "text/x-shellscript"),
}


def _repo_root() -> Path:
    """Best-effort locate the installed-source or checkout root."""
    here = Path(__file__).resolve()
    for candidate in [here.parents[3], here.parents[2]]:
        if (candidate / "scripts").is_dir():
            return candidate
    return here.parents[3]


def _install_oneliners(token: str, hub_url: str) -> dict[str, str]:
    """Return per-platform copy/paste install commands with token + hub pre-baked."""
    base = f"{hub_url.rstrip('/')}/api/mesh/install-script"
    return {
        "linux":
            f"curl -fsSL '{base}?platform=linux&token={token}' | "
            f"bash -s -- --hub {hub_url} --token {token}",
        "darwin":
            f"curl -fsSL '{base}?platform=darwin&token={token}' | "
            f"bash -s -- --hub {hub_url} --token {token}",
        "win32":
            # NSSM service registration requires Administrator, so the one-liner
            # downloads the script to a temp file and relaunches it elevated via
            # UAC (Start-Process -Verb RunAs). Server-side templates Hub + Token
            # into the script body so no args need to cross the UAC boundary.
            #
            # Log lives under $env:ProgramData (NOT $env:TEMP) because when UAC
            # prompts for admin credentials (not just consent), the elevated shell
            # runs as a DIFFERENT user whose $env:TEMP resolves to a different
            # directory — the parent can't find the log. ProgramData is the same
            # absolute path for every user on the machine.
            #
            # The elevated script also writes a sentinel file at the very top
            # (before Start-Transcript) so the parent can distinguish:
            #   - sentinel missing      → script never started (UAC denied / GPO block)
            #   - sentinel present, no log → script started but transcript failed
            #   - log present           → script ran (check exit code)
            f"powershell -ExecutionPolicy Bypass -NoProfile -Command "
            f"\"$s=$env:TEMP+'\\localforge-setup.ps1'; "
            f"$d=$env:ProgramData+'\\LocalForge'; "
            f"$log=Join-Path $d 'setup.log'; "
            f"$sentinel=Join-Path $d 'setup.started'; "
            f"New-Item -ItemType Directory -Force -Path $d | Out-Null; "
            f"iwr -useb '{base}?platform=win32&token={token}' -OutFile $s; "
            f"if(Test-Path $log){{Remove-Item -Force $log}}; "
            f"if(Test-Path $sentinel){{Remove-Item -Force $sentinel}}; "
            f"$proc=Start-Process powershell -Verb RunAs -PassThru -Wait "
            f"-ArgumentList '-ExecutionPolicy','Bypass','-NoProfile','-File',$s; "
            f"if(Test-Path $log){{"
            f"Write-Host '--- installer log ---' -ForegroundColor Cyan; "
            f"Get-Content $log | Write-Host; "
            f"Write-Host ('--- exit code: '+$proc.ExitCode+' ---') -ForegroundColor Cyan"
            f"}} elseif(Test-Path $sentinel){{"
            f"Write-Host 'Script started but transcript was never written.' -ForegroundColor Yellow; "
            f"Write-Host ('Sentinel: '+(Get-Content $sentinel -Raw).Trim()) -ForegroundColor Yellow; "
            f"Write-Host ('Exit code: '+$proc.ExitCode) -ForegroundColor Yellow"
            f"}} else{{"
            f"Write-Host 'Elevated shell never ran the script (UAC denied, GPO, or PowerShell blocked).' -ForegroundColor Red; "
            f"Write-Host 'Manual fix: open Admin PowerShell and run:' -ForegroundColor Yellow; "
            f"Write-Host ('  powershell -ExecutionPolicy Bypass -File '+$s) -ForegroundColor Yellow; "
            f"Write-Host ('Exit code: '+$proc.ExitCode) -ForegroundColor Red"
            f"}}\"",
        "android":
            f"curl -fsSL '{base}?platform=android&token={token}' | "
            f"bash -s -- --hub {hub_url} --token {token}",
    }


async def api_mesh_enrollment_token(request: Request) -> JSONResponse:
    """Admin: mint a short-lived enrollment token + return install commands."""
    from localforge.auth import require_role
    denied = require_role(request, "admin")
    if denied is not None:
        return denied

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    note = (body.get("note") or "")[:200]

    user = _get_user(request)
    hub_url = body.get("hub_url") or _default_hub_url(request)

    try:
        from localforge.enrollment import enrollment_store
        token_info = enrollment_store().mint(issued_by=user.get("id", "admin"), note=note)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    return JSONResponse({
        **token_info,
        "hub_url": hub_url,
        "install_commands": _install_oneliners(token_info["token"], hub_url),
    })


def _default_hub_url(request: Request) -> str:
    """Derive the hub URL a worker should call back to."""
    cfg = _load_config()
    configured = cfg.get("gateway", {}).get("public_url")
    if configured:
        return configured.rstrip("/")
    host = request.headers.get("host")
    if host:
        scheme = request.url.scheme or "http"
        return f"{scheme}://{host}"
    return "http://ai-hub:8100"


async def api_mesh_install_script(request: Request) -> JSONResponse | StreamingResponse:
    """Stream the per-platform bootstrapper. Auth via ?token= enrollment token.

    Listed in auth.PUBLIC_PATHS so curl one-liners work without a bearer header.
    """
    from localforge.enrollment import enrollment_store
    from starlette.responses import Response

    platform = (request.query_params.get("platform") or "").lower()
    token = request.query_params.get("token", "")
    if platform not in _PLATFORM_SCRIPTS:
        return JSONResponse(
            {"error": f"Unknown platform. Choose one of: {sorted(_PLATFORM_SCRIPTS)}"},
            status_code=400,
        )
    if not token or enrollment_store().peek(token) is None:
        return JSONResponse({"error": "Invalid or expired enrollment token"}, status_code=401)

    rel, content_type = _PLATFORM_SCRIPTS[platform]
    script_path = _repo_root() / rel
    if not script_path.is_file():
        return JSONResponse(
            {"error": f"Bootstrapper not yet shipped for platform={platform}",
             "expected_path": str(script_path)},
            status_code=404,
        )
    try:
        body = script_path.read_bytes()
    except OSError as e:
        return JSONResponse({"error": f"Failed to read script: {e}"}, status_code=500)

    # Server-side templating: substitute %%LOCALFORGE_HUB_URL%% and
    # %%LOCALFORGE_ENROLLMENT_TOKEN%% placeholders in the script body so
    # the one-liner doesn't have to push args through multiple shell layers.
    hub = _default_hub_url(request).encode()
    tok = token.encode()
    body = body.replace(b"%%LOCALFORGE_HUB_URL%%", hub)
    body = body.replace(b"%%LOCALFORGE_ENROLLMENT_TOKEN%%", tok)

    return Response(
        body,
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{script_path.name}"'},
    )


async def api_mesh_register(request: Request) -> JSONResponse:
    """Exchange an enrollment token + hardware info for a long-lived worker API key.

    Public endpoint — the enrollment token IS the auth. On success returns
    ``{worker_id, api_key, scopes}`` where the api_key is shown exactly once.
    """
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 65536:
        return JSONResponse({"error": "Payload too large"}, status_code=413)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    enrollment_token = body.get("enrollment_token", "")
    hostname = (body.get("hostname") or "").strip()
    platform = (body.get("platform") or "").lower()
    hardware = body.get("hardware") or {}

    if not enrollment_token:
        return JSONResponse({"error": "enrollment_token required"}, status_code=400)
    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)
    if platform not in _PLATFORM_SCRIPTS:
        return JSONResponse({"error": f"platform must be one of {sorted(_PLATFORM_SCRIPTS)}"}, status_code=400)
    if not isinstance(hardware, dict):
        return JSONResponse({"error": "hardware must be an object"}, status_code=400)

    from localforge.enrollment import enrollment_store, worker_registry
    record = enrollment_store().consume(enrollment_token)
    if record is None:
        return JSONResponse({"error": "Invalid or expired enrollment token"}, status_code=401)

    try:
        worker_id, plaintext_key = worker_registry().register(
            hostname=hostname,
            platform=platform,
            hardware=hardware,
            enrolled_by=record.get("issued_by", "admin"),
        )
    except ImportError:
        return JSONResponse({"error": "bcrypt not installed — cannot register workers"}, status_code=500)

    return JSONResponse({
        "status": "registered",
        "worker_id": worker_id,
        "api_key": plaintext_key,
        "role": "worker",
        "scopes": ["mesh"],
        "note": "Store this key securely — it is not recoverable.",
    })


async def api_mesh_workers_list(request: Request) -> JSONResponse:
    """Admin: list registered workers (no plaintext keys)."""
    from localforge.auth import require_role
    denied = require_role(request, "admin")
    if denied is not None:
        return denied
    from localforge.enrollment import worker_registry
    return JSONResponse({"workers": worker_registry().list_workers()})


async def api_mesh_workers_revoke(request: Request) -> JSONResponse:
    """Admin: revoke a worker's API key by worker_id."""
    from localforge.auth import require_role
    denied = require_role(request, "admin")
    if denied is not None:
        return denied
    body = await request.json()
    worker_id = body.get("worker_id", "")
    if not worker_id:
        return JSONResponse({"error": "worker_id required"}, status_code=400)
    from localforge.enrollment import worker_registry
    ok = worker_registry().revoke(worker_id)
    return JSONResponse({"status": "ok" if ok else "not_found"}, status_code=200 if ok else 404)


async def api_mesh_worker_detail(request: Request) -> JSONResponse:
    """Admin: full detail for a single worker (no plaintext key)."""
    from localforge.auth import require_role
    denied = require_role(request, "admin")
    if denied is not None:
        return denied
    worker_id = request.path_params.get("worker_id", "")
    from localforge.enrollment import worker_registry
    record = worker_registry().get_worker(worker_id)
    if record is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(record)


async def api_mesh_worker_config(request: Request) -> JSONResponse:
    """Admin: update per-worker config (nickname, allowed_tasks, etc.)."""
    from localforge.auth import require_role
    denied = require_role(request, "admin")
    if denied is not None:
        return denied
    worker_id = request.path_params.get("worker_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "object_required"}, status_code=400)
    from localforge.enrollment import worker_registry
    ok = worker_registry().update_config(worker_id, body)
    if not ok:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(worker_registry().get_worker(worker_id))


# ---------------------------------------------------------------------------
# Model catalog + per-worker model management
# ---------------------------------------------------------------------------


def _worker_base_url(worker_id: str) -> str | None:
    """Resolve a registered worker_id to the `http://host:port` seen in the
    heartbeat registry. Returns None if the worker has never sent a
    heartbeat (so we can't reach it anyway).
    """
    try:
        from localforge.gpu_pool import pool
    except ImportError:
        return None
    # Registry is keyed by `hostname:port`; heartbeats include the same
    # hostname that `worker_registry` stored at enroll time. Match on that.
    from localforge.enrollment import worker_registry
    record = worker_registry().get_worker(worker_id)
    if record is None:
        return None
    target_hostname = record.get("hostname", "")
    for key, w in pool()._heartbeat_nodes.items():
        if w.get("hostname", "") == target_hostname:
            return f"http://{key}"
    return None


async def api_mesh_models_catalog(request: Request) -> JSONResponse:
    """Return the curated model catalog as JSON.

    Readable by any authenticated user (admin OR worker scope) — needed by
    the dashboard UI to render a picker, and by workers during setup if
    they want to self-select (bootstrappers ship with a baked-in copy, so
    this endpoint is primarily for the UI).
    """
    from localforge.auth import _resolve_user
    user = _resolve_user(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from localforge.models_catalog import catalog_json
    return JSONResponse(catalog_json())


async def api_mesh_worker_models(request: Request) -> JSONResponse:
    """Admin: list GGUFs present on a given worker + what it's currently serving."""
    from localforge.auth import require_role
    denied = require_role(request, "admin")
    if denied is not None:
        return denied
    worker_id = request.path_params.get("worker_id", "")
    base = _worker_base_url(worker_id)
    if base is None:
        return JSONResponse({"error": "worker not reachable (no recent heartbeat)"}, status_code=503)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}/models")
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"worker call failed: {exc}"}, status_code=502)


async def api_mesh_worker_model_download(request: Request) -> JSONResponse:
    """Admin: tell a worker to download a catalog model.

    Body: {"model_id": str}. We look up the catalog entry here (so the
    worker never has to trust a URL the hub client supplied) and forward
    the pinned URL + filename. Streams on the worker side, so we set a
    generous timeout and let the HTTP request block while the worker
    pulls the file.
    """
    from localforge.auth import require_role
    denied = require_role(request, "admin")
    if denied is not None:
        return denied
    worker_id = request.path_params.get("worker_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    model_id = (body.get("model_id") or "").strip()
    if not model_id:
        return JSONResponse({"error": "model_id required"}, status_code=400)

    from localforge.models_catalog import by_id
    model = by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"unknown model_id: {model_id}"}, status_code=404)

    base = _worker_base_url(worker_id)
    if base is None:
        return JSONResponse({"error": "worker not reachable (no recent heartbeat)"}, status_code=503)

    import httpx
    # Generous read timeout — downloads can run minutes on slow links.
    # Worker streams 1 MB chunks, so connection stays busy.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=3600.0,
                                                           write=10.0, pool=10.0)) as client:
            resp = await client.post(f"{base}/models/download", json={
                "url": model["url"],
                "filename": model["filename"],
            })
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"worker call failed: {exc}"}, status_code=502)


async def api_mesh_worker_model_activate(request: Request) -> JSONResponse:
    """Admin: hot-swap the worker's running llama-server to a different GGUF.

    Body: {"filename": str}. The worker resolves the filename inside its
    own models dir (by design — the hub doesn't learn or forward file
    paths). Returns the worker's response directly, which reports the
    new active model name or rollback details.
    """
    from localforge.auth import require_role
    denied = require_role(request, "admin")
    if denied is not None:
        return denied
    worker_id = request.path_params.get("worker_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    filename = (body.get("filename") or "").strip()
    if not filename:
        return JSONResponse({"error": "filename required"}, status_code=400)

    base = _worker_base_url(worker_id)
    if base is None:
        return JSONResponse({"error": "worker not reachable (no recent heartbeat)"}, status_code=503)

    import httpx
    try:
        # Model swap takes ~15-30s for a cold GPU load; allow some headroom.
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=120.0,
                                                           write=10.0, pool=10.0)) as client:
            resp = await client.post(f"{base}/models/activate", json={"filename": filename})
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"worker call failed: {exc}"}, status_code=502)


# ---------------------------------------------------------------------------
# Model Sync
# ---------------------------------------------------------------------------


async def api_sync_models(request: Request) -> JSONResponse:
    """Sync GGUF models from secondary drive to webui models directory."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    try:
        from localforge.tools.infrastructure import sync_models as _sync_models
        result = await _sync_models(body)
        return JSONResponse({"status": "ok", "result": result})
    except ImportError:
        # Fallback for monolith mode — run sync inline
        result = await _sync_models_fallback(body)
        return JSONResponse({"status": "ok", "result": result})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


async def _sync_models_fallback(args: dict) -> str:
    """Inline model sync for monolith mode (no localforge package)."""
    import glob as glob_mod

    clean = args.get("clean", True)
    cfg = _load_config()

    # Auto-detect webui models directory
    webui_root = cfg.get("webui_root") or os.environ.get("LOCALFORGE_WEBUI_ROOT")
    if not webui_root:
        for candidate_path in [
            Path.home() / "Development" / "text-generation-webui",
            Path.home() / "text-generation-webui",
        ]:
            if (candidate_path / "user_data" / "models").exists():
                webui_root = str(candidate_path)
                break
    if not webui_root:
        return "Cannot find text-generation-webui. Set LOCALFORGE_WEBUI_ROOT."

    target_dir = Path(os.path.expanduser(webui_root)) / "user_data" / "models"
    if not target_dir.exists():
        return f"Models directory not found: {target_dir}"

    # Auto-detect source
    source = args.get("source")
    if source:
        source_dir = Path(os.path.expanduser(source))
    else:
        configured = cfg.get("model_source")
        if configured:
            source_dir = Path(os.path.expanduser(configured))
        else:
            source_dir = None
            candidates = [Path("/mnt/models")]
            volume = cfg.get("model_volume")
            if volume:
                candidates.extend(Path(p) for p in sorted(glob_mod.glob(f"/media/*/{volume}*")))
            for cp in candidates:
                if cp.is_dir() and list(cp.glob("*.gguf")):
                    source_dir = cp
                    break
        if not source_dir or not source_dir.is_dir():
            return "No model source found. Set model_source in config.yaml."

    added, removed = [], []
    skipped = 0
    if clean:
        for link in target_dir.glob("*.gguf"):
            if link.is_symlink() and not link.exists():
                link.unlink()
                removed.append(link.name)
    for model_file in sorted(source_dir.glob("*.gguf")):
        target = target_dir / model_file.name
        if target.exists() or target.is_symlink():
            skipped += 1
            continue
        target.symlink_to(model_file)
        added.append(model_file.name)

    total = len(list(target_dir.glob("*.gguf")))
    lines = [f"Sync complete: +{len(added)} new, {skipped} existing, -{len(removed)} broken"]
    lines.append(f"Total models: {total}")
    if added:
        lines.append("\nNew: " + ", ".join(added))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Training Pipeline
# ---------------------------------------------------------------------------


def _import_training():
    """Import training tools, returning None if not available (monolith mode)."""
    try:
        from localforge.tools import training
        return training
    except ImportError:
        return None


async def api_training_list(request: Request) -> JSONResponse:
    """List training datasets, runs, and models."""
    mod = _import_training()
    if not mod:
        return JSONResponse({"status": "ok", "result": "Training tools not installed. Install the localforge package."})
    what = request.query_params.get("what", "all")
    try:
        result = await mod.train_list({"what": what})
        return JSONResponse({"status": "ok", "result": result})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


async def api_training_status(request: Request) -> JSONResponse:
    """Get training run status."""
    mod = _import_training()
    if not mod:
        return JSONResponse({"status": "ok", "result": "Training tools not installed."})
    name = request.query_params.get("name")
    tail = int(request.query_params.get("tail", "20"))
    args = {"tail": tail}
    if name:
        args["name"] = name
    try:
        result = await mod.train_status(args)
        return JSONResponse({"status": "ok", "result": result})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


async def api_training_prepare(request: Request) -> JSONResponse:
    """Prepare a training dataset."""
    mod = _import_training()
    if not mod:
        return JSONResponse({"status": "error", "error": "Training tools not installed."}, status_code=501)
    body = await request.json()
    try:
        result = await mod.train_prepare(body)
        return JSONResponse({"status": "ok", "result": result})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


async def api_training_start(request: Request) -> JSONResponse:
    """Start a training run."""
    mod = _import_training()
    if not mod:
        return JSONResponse({"status": "error", "error": "Training tools not installed."}, status_code=501)
    body = await request.json()
    try:
        result = await mod.train_start(body)
        return JSONResponse({"status": "ok", "result": result})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


async def api_training_feedback(request: Request) -> JSONResponse:
    """Record training feedback."""
    mod = _import_training()
    if not mod:
        return JSONResponse({"status": "error", "error": "Training tools not installed."}, status_code=501)
    body = await request.json()
    try:
        result = await mod.train_feedback(body)
        return JSONResponse({"status": "ok", "result": result})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Model config lookup (for dashboard pre-fill)
# ---------------------------------------------------------------------------

async def api_model_config(request: Request) -> JSONResponse:
    """Return config.yaml model overrides for a given model name."""
    model_name = request.query_params.get("model", "")
    if not model_name:
        return JSONResponse({"config": {}})
    cfg = _load_config()
    for pattern, overrides in cfg.get("models", {}).items():
        if pattern in model_name:
            return JSONResponse({"config": overrides, "matched_pattern": pattern})
    return JSONResponse({"config": {}})


# ---------------------------------------------------------------------------
# Route list for mounting in the gateway
# ---------------------------------------------------------------------------
dashboard_routes = [
    # User
    Route("/me", api_me, methods=["GET"]),
    # Status
    Route("/status", api_status, methods=["GET"]),
    Route("/metrics", api_metrics, methods=["GET"]),
    # Chat
    Route("/chat", api_chat, methods=["POST"]),
    Route("/chats", api_chat_list, methods=["GET"]),
    Route("/chats/save", api_chat_save, methods=["POST"]),
    Route("/chats/{chat_id}", api_chat_load, methods=["GET"]),
    Route("/chats/{chat_id}", api_chat_delete, methods=["DELETE"]),
    # Models
    Route("/models", api_models, methods=["GET"]),
    Route("/swap", api_swap, methods=["POST"]),
    Route("/models/config", api_model_config, methods=["GET"]),
    # Generation params
    Route("/generation-params", api_generation_params, methods=["GET", "POST"]),
    # Presets
    Route("/presets", api_presets, methods=["GET"]),
    Route("/presets/load", api_preset_load, methods=["POST"]),
    # Model controls
    Route("/model/unload", api_model_unload, methods=["POST"]),
    Route("/benchmark", api_benchmark, methods=["POST"]),
    # LoRAs
    Route("/loras", api_loras, methods=["GET"]),
    Route("/loras/load", api_lora_load, methods=["POST"]),
    Route("/loras/unload", api_lora_unload, methods=["POST"]),
    # Search
    Route("/search", api_search, methods=["POST"]),
    Route("/indexes", api_indexes, methods=["GET"]),
    Route("/indexes/create", api_index_create, methods=["POST"]),
    Route("/indexes/{name}/stats", api_index_stats, methods=["GET"]),
    Route("/indexes/{name}/delete", api_index_delete, methods=["POST"]),
    Route("/indexes/{name}/refresh", api_index_refresh, methods=["POST"]),
    # Photos
    Route("/photos", api_photos_list, methods=["GET"]),
    Route("/photos/upload", api_photos_upload, methods=["POST"]),
    Route("/photos/search", api_photos_search, methods=["POST"]),
    Route("/photos/{filename}", api_photos_get, methods=["GET"]),
    # Vision
    Route("/upload-image", api_upload_image, methods=["POST"]),
    # Knowledge Graph
    Route("/kg/stats", api_kg_stats, methods=["GET"]),
    Route("/kg/search", api_kg_search, methods=["POST"]),
    Route("/kg/context", api_kg_context, methods=["POST"]),
    Route("/kg/add", api_kg_add, methods=["POST"]),
    # Agents (static paths first, then parameterized)
    Route("/agents", api_agents, methods=["GET"]),
    Route("/agents/metrics", api_agent_metrics, methods=["GET"]),
    Route("/agents/tasks", api_agent_tasks, methods=["GET"]),
    Route("/agents/bus", api_agent_bus, methods=["GET"]),
    Route("/agents/{agent_id}/trigger", api_trigger_agent, methods=["POST"]),
    Route("/agents/{agent_id}/config", api_agent_config, methods=["GET", "PATCH"]),
    Route("/agents/{agent_id}/logs", api_agent_logs, methods=["GET"]),
    Route("/agents/{agent_id}/pause", api_agent_pause, methods=["POST"]),
    Route("/agents/{agent_id}/resume", api_agent_resume, methods=["POST"]),
    Route("/webhook/{agent_id}", api_webhook, methods=["POST"]),
    # Notes
    Route("/notes", api_notes, methods=["GET"]),
    Route("/notes/{topic}", api_note_content, methods=["GET"]),
    # Voice
    Route("/transcribe", api_transcribe, methods=["POST"]),
    # Notifications + Web Push
    Route("/events", api_events, methods=["GET"]),
    Route("/push/vapid-key", api_push_vapid_key, methods=["GET"]),
    Route("/push/subscribe", api_push_subscribe, methods=["POST"]),
    # Videos
    Route("/videos", api_videos_list, methods=["GET"]),
    Route("/videos/upload", api_videos_upload, methods=["POST"]),
    Route("/videos/{user_id}/{filename}", api_videos_get, methods=["GET"]),
    Route("/videos/{user_id}/thumbs/{filename}", api_video_thumb, methods=["GET"]),
    # Research
    Route("/research/sessions", api_research_sessions, methods=["GET"]),
    Route("/research/sessions/{session_id}", api_research_session_get, methods=["GET"]),
    Route("/research/start", api_research_start, methods=["POST"]),
    # Workflows
    Route("/workflows", api_workflows_list, methods=["GET"]),
    Route("/workflows", api_workflow_create, methods=["POST"]),
    Route("/workflows/node-specs", api_workflow_node_specs, methods=["GET"]),
    Route("/workflows/run", api_workflow_run, methods=["POST"]),
    Route("/workflows/executions", api_workflow_executions, methods=["GET"]),
    Route("/workflows/executions/{execution_id}", api_workflow_execution_get, methods=["GET"]),
    Route("/workflows/{workflow_id}", api_workflow_get, methods=["GET"]),
    Route("/workflows/{workflow_id}", api_workflow_save, methods=["PUT"]),
    Route("/workflows/{workflow_id}", api_workflow_delete, methods=["DELETE"]),
    # KG graph visualization
    Route("/kg/graph", api_kg_graph, methods=["POST"]),
    # Approval queue
    Route("/approvals", api_approvals_list, methods=["GET"]),
    Route("/approvals/decide", api_approvals_decide, methods=["POST"]),
    # Hub mode & character
    Route("/modes", api_modes, methods=["GET"]),
    Route("/modes/set", api_set_mode, methods=["POST"]),
    Route("/characters", api_characters, methods=["GET"]),
    Route("/characters/set", api_set_character, methods=["POST"]),
    # Compute mesh
    Route("/mesh/heartbeat", api_mesh_heartbeat, methods=["POST"]),
    Route("/mesh/status", api_mesh_status, methods=["GET"]),
    Route("/mesh/enrollment-token", api_mesh_enrollment_token, methods=["POST"]),
    Route("/mesh/install-script", api_mesh_install_script, methods=["GET"]),
    Route("/mesh/register", api_mesh_register, methods=["POST"]),
    Route("/mesh/workers", api_mesh_workers_list, methods=["GET"]),
    Route("/mesh/workers/revoke", api_mesh_workers_revoke, methods=["POST"]),
    Route("/mesh/workers/{worker_id}", api_mesh_worker_detail, methods=["GET"]),
    Route("/mesh/workers/{worker_id}/config", api_mesh_worker_config, methods=["POST"]),
    Route("/mesh/models/catalog", api_mesh_models_catalog, methods=["GET"]),
    Route("/mesh/workers/{worker_id}/models", api_mesh_worker_models, methods=["GET"]),
    Route("/mesh/workers/{worker_id}/models/download", api_mesh_worker_model_download, methods=["POST"]),
    Route("/mesh/workers/{worker_id}/models/activate", api_mesh_worker_model_activate, methods=["POST"]),
    # Model sync
    Route("/sync-models", api_sync_models, methods=["POST"]),
    # Training pipeline
    Route("/training", api_training_list, methods=["GET"]),
    Route("/training/status", api_training_status, methods=["GET"]),
    Route("/training/prepare", api_training_prepare, methods=["POST"]),
    Route("/training/start", api_training_start, methods=["POST"]),
    Route("/training/feedback", api_training_feedback, methods=["POST"]),
]
