"""Dashboard API routes for the web UI.

Supports multi-user profiles, chat history, photo gallery, notifications,
model management, search/RAG, knowledge graph, voice transcription.
"""

import asyncio
import base64
import io
import json
import time
import uuid
from pathlib import Path
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

import httpx
import yaml

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
NOTES_DIR = Path(__file__).parent.parent / "notes"
DATA_ROOT = Path(__file__).parent.parent

# Set by gateway.py during lifespan
_supervisor = None

# Push notification subscriptions (in-memory, persisted to disk)
_push_subscriptions: dict[str, list[dict]] = {}
_push_subs_file = DATA_ROOT / "push_subscriptions.json"

# SSE notification clients
_sse_clients: dict[str, list[asyncio.Queue]] = {}

# Runtime generation parameter overrides (applied to all chat requests)
_gen_param_overrides: dict = {}


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _backend_url() -> str:
    cfg = _load_config()
    return cfg.get("backends", {}).get("local", {}).get("url", "http://localhost:5000/v1")


def _get_user(request: Request) -> dict:
    """Extract user profile from request (set by auth middleware)."""
    return getattr(request.state, "user", {"id": "admin", "name": "Admin", "role": "admin"})


def _user_dir(base: str, user_id: str) -> Path:
    """Get user-scoped directory, creating if needed."""
    d = DATA_ROOT / base / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Notifications helper
# ---------------------------------------------------------------------------

async def notify_user(user_id: str, title: str, body: str, tag: str = "ai-hub"):
    """Send notification to a user via SSE."""
    queues = _sse_clients.get(user_id, [])
    event = json.dumps({"title": title, "body": body, "tag": tag, "timestamp": time.time()})
    for q in queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def notify_all(title: str, body: str, tag: str = "ai-hub"):
    """Send notification to all connected users."""
    for user_id in _sse_clients:
        await notify_user(user_id, title, body, tag)


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
                        break
            except Exception:
                pass

    except Exception:
        status["model"] = {"status": "unreachable"}

    return JSONResponse(status)


# ---------------------------------------------------------------------------
# Chat (streaming)
# ---------------------------------------------------------------------------

async def api_chat(request: Request) -> StreamingResponse:
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
    """List saved chat conversations for the user."""
    user = _get_user(request)
    chat_dir = _user_dir("chats", user["id"])
    chats = []
    for f in sorted(chat_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
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
    return JSONResponse({"chats": chats})


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
    payload = {"model_name": model_name}
    if "ctx_size" in body:
        payload["args"] = {"n_ctx": body["ctx_size"]}
    if "gpu_layers" in body:
        payload["args"] = payload.get("args", {})
        payload["args"]["n_gpu_layers"] = body["gpu_layers"]

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(f"{backend_url}/internal/model/load", json=payload)
            result = resp.json() if resp.status_code == 200 else {"error": resp.text}
            # Notify users
            await notify_all("Model Swapped", f"Now running: {model_name}", "model-swap")
            return JSONResponse(result)
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

    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from server import _tool_handlers

        if mode == "rag":
            handler = _tool_handlers.get("rag_query")
            if handler:
                result = await handler({"index_name": index_name, "question": query})
                return JSONResponse({"mode": "rag", "result": result})
        else:
            handler = _tool_handlers.get("hybrid_search")
            if handler:
                result = await handler({"index_name": index_name, "query": query})
                return JSONResponse({"mode": "hybrid", "result": result})

        return JSONResponse({"error": "Search tools not available"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# System metrics
# ---------------------------------------------------------------------------

async def api_metrics(request: Request) -> JSONResponse:
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
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from knowledge.graph import KnowledgeGraph
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
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from server import _tool_handlers
        handler = _tool_handlers.get("benchmark")
        if handler:
            result = await handler({"prompt_length": prompt_length})
            return JSONResponse({"result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"error": "benchmark not available"}, status_code=500)


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
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from server import _tool_handlers
        handler = _tool_handlers.get("load_lora")
        if handler:
            weights = body.get("lora_weights", [1.0] * len(names))
            result = await handler({"lora_names": names, "lora_weights": weights})
            await notify_all("LoRA Loaded", ", ".join(names), "config")
            return JSONResponse({"result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"error": "load_lora not available"}, status_code=500)


async def api_lora_unload(request: Request) -> JSONResponse:
    """Unload all LoRA adapters."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from server import _tool_handlers
        handler = _tool_handlers.get("unload_loras")
        if handler:
            result = await handler({})
            await notify_all("LoRAs Unloaded", "All adapters removed", "config")
            return JSONResponse({"result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"error": "unload_loras not available"}, status_code=500)


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
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from server import _tool_handlers
        handler = _tool_handlers.get("index_directory")
        if handler:
            params = {
                "name": name,
                "directory": directory,
                "glob_pattern": body.get("glob_pattern", "**/*.*"),
            }
            if body.get("embed"):
                params["embed"] = True
            result = await handler(params)
            await notify_all("Index Created", name, "search")
            return JSONResponse({"result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"error": "index_directory not available"}, status_code=500)


async def api_index_delete(request: Request) -> JSONResponse:
    """Delete a RAG index."""
    name = request.path_params.get("name", "")
    if not name or "/" in name or ".." in name:
        return JSONResponse({"error": "invalid index name"}, status_code=400)
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from server import _tool_handlers
        handler = _tool_handlers.get("delete_index")
        if handler:
            result = await handler({"index_name": name})
            await notify_all("Index Deleted", name, "search")
            return JSONResponse({"result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"error": "delete_index not available"}, status_code=500)


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
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from server import _tool_handlers
        handler = _tool_handlers.get("incremental_index")
        if handler:
            result = await handler({"index_name": name})
            await notify_all("Index Refreshed", name, "search")
            return JSONResponse({"result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"error": "incremental_index not available"}, status_code=500)


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
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from server import _tool_handlers
            handler = _tool_handlers.get("deep_research")
            if handler:
                result = await handler({
                    "question": question,
                    "max_sources": 5,
                    "save_to_kg": True,
                })
                # Extract synthesis from result
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
        except Exception as e:
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

    try:
        from workflows.schema import WorkflowDef
        from workflows.engine import WorkflowEngine
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from server import chat as _chat_fn

        wf = WorkflowDef.from_dict(wf_data)

        async def chat_fn(prompt, system=""):
            return await _chat_fn(prompt, system=system)

        engine = WorkflowEngine(chat_fn=chat_fn)

        async def _run():
            return await engine.execute(wf, initial_input)

        ctx = await asyncio.wait_for(_run(), timeout=300)
        return JSONResponse({
            "execution_id": ctx.execution_id,
            "status": ctx.status,
        })
    except asyncio.TimeoutError:
        return JSONResponse({"error": "Workflow timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_workflow_executions(request: Request) -> JSONResponse:
    from workflows.engine import list_executions
    return JSONResponse({"executions": list_executions()})


async def api_workflow_execution_get(request: Request) -> JSONResponse:
    exec_id = request.path_params.get("execution_id", "")
    from workflows.engine import WorkflowContext
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
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from knowledge.graph import KnowledgeGraph
        kg = KnowledgeGraph()
        data = kg.get_graph(center=center or None, depth=depth)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
    # Notifications
    Route("/events", api_events, methods=["GET"]),
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
    Route("/workflows/run", api_workflow_run, methods=["POST"]),
    Route("/workflows/executions", api_workflow_executions, methods=["GET"]),
    Route("/workflows/executions/{execution_id}", api_workflow_execution_get, methods=["GET"]),
    Route("/workflows/{workflow_id}", api_workflow_get, methods=["GET"]),
    Route("/workflows/{workflow_id}", api_workflow_save, methods=["PUT"]),
    # KG graph visualization
    Route("/kg/graph", api_kg_graph, methods=["POST"]),
]
