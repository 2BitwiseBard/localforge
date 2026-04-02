#!/usr/bin/env python3
"""Device worker agent for the AI hub compute mesh.

A lightweight HTTP server that runs on each device in the Tailscale mesh.
Reports hardware capabilities and accepts routed tasks from the MCP gateway.

Usage:
  python device_worker.py                    # Auto-detect, port 8200
  python device_worker.py --port 8200        # Explicit port
  python device_worker.py --hub ai-hub:8100  # Register with hub

Endpoints:
  GET  /health      → capabilities, tier, load
  GET  /status      → detailed status
  POST /task        → execute a task
  POST /task/cancel → cancel running task
"""

import argparse
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from detect import detect, HardwareInfo

log = logging.getLogger("device-worker")

# Task state
_current_task: dict | None = None
_task_history: list[dict] = []
_hw: HardwareInfo | None = None
_start_time: float = 0


def get_hardware() -> HardwareInfo:
    global _hw
    if _hw is None:
        _hw = detect()
    return _hw


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

async def execute_task(payload: dict) -> dict:
    """Execute a routed task based on its type."""
    task_type = payload.get("type", "chat")

    if task_type == "chat":
        return await _task_chat(payload)
    elif task_type == "embeddings":
        return await _task_embeddings(payload)
    elif task_type == "tts":
        return await _task_tts(payload)
    elif task_type == "stt":
        return await _task_stt(payload)
    elif task_type == "classify":
        return await _task_classify(payload)
    else:
        return {"error": f"Unknown task type: {task_type}"}


async def _task_chat(payload: dict) -> dict:
    """Run inference via local llama.cpp or text-gen-webui."""
    prompt = payload.get("prompt", "")
    max_tokens = payload.get("max_tokens", 1024)

    # Try local text-gen-webui first
    try:
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post("http://localhost:5000/v1/chat/completions", json={
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            })
            data = resp.json()
            return {"response": data["choices"][0]["message"]["content"]}
    except Exception:
        pass

    # Fallback: try llama-cli if available
    import shutil
    llama = shutil.which("llama-cli") or shutil.which("llama.cpp")
    if llama:
        proc = await asyncio.create_subprocess_exec(
            llama, "-p", prompt, "-n", str(max_tokens),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return {"response": stdout.decode()[:max_tokens * 4]}

    return {"error": "No inference backend available"}


async def _task_embeddings(payload: dict) -> dict:
    """Compute embeddings using fastembed."""
    texts = payload.get("texts", [])
    if not texts:
        return {"error": "No texts provided"}
    try:
        from fastembed import TextEmbedding
        model = TextEmbedding("jinaai/jina-embeddings-v2-base-code")
        embeddings = list(model.embed(texts))
        return {"embeddings": [e.tolist() for e in embeddings]}
    except ImportError:
        return {"error": "fastembed not installed"}


async def _task_tts(payload: dict) -> dict:
    """Text-to-speech."""
    text = payload.get("text", "")
    if not text:
        return {"error": "No text provided"}
    import shutil
    if shutil.which("piper"):
        proc = await asyncio.create_subprocess_exec(
            "piper", "--output-raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate(text.encode())
        import base64
        return {"audio_b64": base64.b64encode(stdout).decode(), "format": "raw"}
    return {"error": "No TTS backend available (install piper)"}


async def _task_stt(payload: dict) -> dict:
    """Speech-to-text."""
    return {"error": "STT not yet implemented — use Whisper directly"}


async def _task_classify(payload: dict) -> dict:
    """Simple text classification via the loaded model."""
    text = payload.get("text", "")
    categories = payload.get("categories", [])
    prompt = f"Classify the following text into one of these categories: {', '.join(categories)}\n\nText: {text}\n\nCategory:"
    return await _task_chat({"prompt": prompt, "max_tokens": 50})


# ---------------------------------------------------------------------------
# HTTP server (Starlette)
# ---------------------------------------------------------------------------

def create_app():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(request):
        hw = get_hardware()
        return JSONResponse({
            "status": "ok",
            "tier": hw.tier(),
            "model_name": "",
            "active_tasks": 1 if _current_task else 0,
            "uptime_s": int(time.time() - _start_time),
            "capabilities": hw.to_dict(),
        })

    async def status(request):
        hw = get_hardware()
        return JSONResponse({
            "hardware": hw.to_dict(),
            "tier": hw.tier(),
            "current_task": _current_task,
            "task_history_count": len(_task_history),
            "recent_tasks": _task_history[-10:],
            "uptime_s": int(time.time() - _start_time),
        })

    async def handle_task(request):
        global _current_task
        if _current_task:
            return JSONResponse({"error": "Already processing a task"}, status_code=429)

        body = await request.json()
        task_id = body.get("id", uuid.uuid4().hex[:12])
        _current_task = {"id": task_id, "type": body.get("type", "chat"), "started": time.time()}

        start_time = _current_task["started"]
        try:
            result = await execute_task(body)
            record = {"id": task_id, "type": body.get("type"), "duration": round(time.time() - start_time, 2), "status": "done"}
        except Exception as e:
            result = {"error": str(e)}
            record = {"id": task_id, "type": body.get("type"), "duration": round(time.time() - start_time, 2), "status": "error", "error": str(e)}
        finally:
            _current_task = None
            _task_history.append(record)
            if len(_task_history) > 100:
                _task_history[:] = _task_history[-50:]

        return JSONResponse({"id": task_id, **result})

    async def cancel_task(request):
        global _current_task
        if _current_task:
            _current_task = None
            return JSONResponse({"status": "cancelled"})
        return JSONResponse({"status": "no task running"})

    return Starlette(routes=[
        Route("/health", health),
        Route("/status", status),
        Route("/task", handle_task, methods=["POST"]),
        Route("/task/cancel", cancel_task, methods=["POST"]),
    ])


async def register_with_hub(hub_url: str, worker_port: int):
    """Announce this worker to the hub gateway so it gets added to the mesh."""
    import socket
    hw = get_hardware()
    hostname = socket.gethostname()

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            # Hit the hub health endpoint to confirm connectivity
            resp = await client.get(f"{hub_url}/health")
            if resp.status_code != 200:
                print(f"  Hub not reachable at {hub_url}")
                return

            # The hub will discover us via Tailscale probing on :8200,
            # but we can also announce via the MCP compute_status endpoint.
            # For now, just confirm connectivity.
            print(f"  Hub reachable at {hub_url}")
            print(f"  Worker will be auto-discovered via Tailscale on :{worker_port}")
            print(f"  Hostname: {hostname}, Tier: {hw.tier()}")
    except Exception as e:
        print(f"  Hub registration skipped: {e}")


def main():
    global _start_time
    _start_time = time.time()

    parser = argparse.ArgumentParser(description="AI Hub device worker")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--hub", type=str, default="",
                        help="Hub gateway URL to register with (e.g., ai-hub:8100)")
    args = parser.parse_args()

    # Detect and log hardware
    hw = get_hardware()
    print(f"Device Worker starting on :{args.port}")
    print(f"  Platform: {hw.platform}")
    print(f"  GPU: {hw.gpu_name or 'none'} ({hw.gpu_type})")
    print(f"  VRAM: {hw.vram_mb} MB")
    print(f"  RAM: {hw.ram_mb} MB")
    print(f"  CPU cores: {hw.cpu_cores}")
    print(f"  Tier: {hw.tier()}")
    print(f"  Capabilities: inference={hw.inference}, embeddings={hw.embeddings}, "
          f"tts={hw.tts}, stt={hw.stt}, vision={hw.vision}")

    # Register with hub if specified
    if args.hub:
        hub_url = args.hub if args.hub.startswith("http") else f"http://{args.hub}"
        asyncio.run(register_with_hub(hub_url, args.port))

    import uvicorn
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
