#!/usr/bin/env python3
"""Device worker agent for the AI hub compute mesh.

A lightweight HTTP server that runs on each device in the Tailscale mesh.
Reports hardware capabilities, accepts routed tasks from the MCP gateway,
and pushes heartbeats to the hub so the mesh stays up to date.

Usage:
  localforge-worker                           # Auto-detect, port 8200
  localforge-worker --port 8200               # Explicit port
  localforge-worker --hub ai-hub:8100         # Register with hub
  localforge-worker --hub ai-hub:8100 --key   # With API key

Endpoints:
  GET  /health        → capabilities, tier, load, model info
  GET  /status        → detailed status + task history
  POST /task          → execute a task (queued, up to max_concurrent)
  POST /task/cancel   → cancel running task by id
  GET  /metrics       → system resource usage
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
import uuid

from localforge.workers.detect import HardwareInfo, detect

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover — worker dep group
    _HAS_PSUTIL = False

log = logging.getLogger("localforge.worker")

# Task state
_task_queue: asyncio.Queue | None = None
_active_tasks: dict[str, asyncio.Task] = {}  # task_id -> asyncio.Task
_task_results: dict[str, dict] = {}  # task_id -> result
_task_history: list[dict] = []
_hw: HardwareInfo | None = None
_start_time: float = 0
_hub_url: str = ""
_api_key: str = ""
_max_concurrent: int = 2
_task_timeout: int = 180  # seconds
_shutting_down: bool = False
_min_memory_mb: int = 500  # Reject tasks if available RAM below this
_battery_floor: int = 15   # Reject tasks if battery below this % (and not charging)

# Task stats
_stats = {
    "tasks_completed": 0,
    "tasks_failed": 0,
    "tasks_timed_out": 0,
    "tasks_queued": 0,
    "total_duration_s": 0.0,
}

# Backend URL (env-var driven)
_backend_url = os.environ.get("LOCALFORGE_BACKEND_URL", "http://localhost:5000/v1")

# Local llama-server manager (set in main())
_llama_manager: "LlamaServerManager | None" = None


class LlamaServerManager:
    """Manage a local llama-server subprocess for self-hosted inference."""

    def __init__(self, model_path: str, port: int = 5050,
                 gpu_layers: int = -1, ctx_size: int = 0, parallel: int = 1):
        self.model_path = model_path
        self.port = port
        self.gpu_layers = gpu_layers
        self.ctx_size = ctx_size or self._auto_ctx_size()
        self.parallel = parallel
        self._proc: asyncio.subprocess.Process | None = None
        self._crash_count = 0
        self._crash_window_start = 0.0
        self._restart_task: asyncio.Task | None = None
        self._url = f"http://127.0.0.1:{port}"

    @property
    def model_name(self) -> str:
        from pathlib import Path
        return Path(self.model_path).name

    @property
    def url(self) -> str:
        return self._url

    def _auto_ctx_size(self) -> int:
        """Pick a ctx_size that fits in available RAM."""
        avail_mb = 0
        if _HAS_PSUTIL:
            try:
                avail_mb = psutil.virtual_memory().available // (1024 * 1024)
            except Exception:
                pass
        if avail_mb == 0:
            try:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemAvailable:"):
                            avail_mb = int(line.split()[1]) // 1024
                            break
            except Exception:
                pass
        if avail_mb:
            return min(8192, max(2048, (avail_mb - 2000) // 2))
        return 4096

    async def start(self) -> bool:
        """Launch llama-server and wait for it to be healthy."""
        import shutil
        # LOCALFORGE_LLAMA_BIN lets the bootstrapper point at a vendored binary
        # without mutating the service PATH (NSSM's AppEnvironmentExtra
        # replaces PATH rather than prepending, which would break System32).
        llama_bin = os.environ.get("LOCALFORGE_LLAMA_BIN") or shutil.which("llama-server")
        if llama_bin and not os.path.exists(llama_bin):
            log.warning("LOCALFORGE_LLAMA_BIN points at missing path: %s", llama_bin)
            llama_bin = shutil.which("llama-server")
        if not llama_bin:
            log.error("llama-server not found (set LOCALFORGE_LLAMA_BIN or add to PATH)")
            return False

        if not os.path.exists(self.model_path):
            log.error("Model not found: %s", self.model_path)
            return False

        cmd = [
            llama_bin,
            "--model", self.model_path,
            "--port", str(self.port),
            "--host", "127.0.0.1",
            "--ctx-size", str(self.ctx_size),
            "--n-gpu-layers", str(self.gpu_layers),
            "--parallel", str(self.parallel),
        ]
        log.info("Starting llama-server: %s", " ".join(cmd))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Health-poll up to 30s
        for i in range(60):
            if self._proc.returncode is not None:
                stderr = await self._proc.stderr.read(4096) if self._proc.stderr else b""
                log.error("llama-server exited immediately: %s", stderr.decode()[:500])
                return False
            if await self.health_check():
                log.info("llama-server ready on :%d (model: %s, ctx: %d)",
                         self.port, self.model_name, self.ctx_size)
                self._restart_task = asyncio.create_task(self._crash_watcher())
                return True
            await asyncio.sleep(0.5)

        log.error("llama-server failed to become healthy within 30s")
        await self.stop()
        return False

    async def health_check(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self._url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def _crash_watcher(self):
        """Monitor the process and restart on crash with exponential backoff."""
        backoff = 5
        while not _shutting_down:
            await asyncio.sleep(5)
            if self._proc and self._proc.returncode is not None:
                now = time.time()
                # Reset crash window every 5 minutes
                if now - self._crash_window_start > 300:
                    self._crash_count = 0
                    self._crash_window_start = now
                self._crash_count += 1
                if self._crash_count > 3:
                    log.error("llama-server crashed %d times in 5min, giving up", self._crash_count)
                    return
                log.warning("llama-server crashed, restarting in %ds (attempt %d/3)",
                            backoff, self._crash_count)
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
                await self.start()
                return  # new start() creates its own watcher

    async def stop(self):
        if self._restart_task:
            self._restart_task.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._proc.kill()
            log.info("llama-server stopped")


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

    handlers = {
        "chat": _task_chat,
        "embeddings": _task_embeddings,
        "tts": _task_tts,
        "stt": _task_stt,
        "classify": _task_classify,
        "rerank": _task_rerank,
    }
    handler = handlers.get(task_type)
    if handler:
        return await handler(payload)
    return {"error": f"Unknown task type: {task_type}"}


async def _task_chat(payload: dict) -> dict:
    """Run inference via local llama-server, text-gen-webui, or llama-cli fallback."""
    prompt = payload.get("prompt", "")
    max_tokens = payload.get("max_tokens", 1024)
    messages = payload.get("messages", [{"role": "user", "content": prompt}])

    # Priority 1: self-hosted llama-server
    if _llama_manager and await _llama_manager.health_check():
        try:
            import httpx
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(f"{_llama_manager.url}/v1/chat/completions", json={
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": payload.get("temperature", 0.7),
                })
                data = resp.json()
                return {
                    "response": data["choices"][0]["message"]["content"],
                    "tokens": data.get("usage", {}),
                    "backend": "llama-server",
                }
        except Exception as e:
            log.debug("llama-server chat failed: %s", e)

    # Priority 2: external text-gen-webui backend
    try:
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{_backend_url}/chat/completions", json={
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": payload.get("temperature", 0.7),
            })
            data = resp.json()
            return {
                "response": data["choices"][0]["message"]["content"],
                "tokens": data.get("usage", {}),
                "backend": "text-gen-webui",
            }
    except Exception:
        pass

    # Priority 3: llama-cli binary fallback
    import shutil
    llama = shutil.which("llama-cli") or shutil.which("llama.cpp")
    if llama:
        proc = await asyncio.create_subprocess_exec(
            llama, "-p", prompt, "-n", str(max_tokens),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_task_timeout)
        return {"response": stdout.decode()[:max_tokens * 4], "backend": "llama-cli"}

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
    """Text-to-speech via piper."""
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
        stdout, _ = await asyncio.wait_for(proc.communicate(text.encode()), timeout=60)
        import base64
        return {"audio_b64": base64.b64encode(stdout).decode(), "format": "raw"}
    return {"error": "No TTS backend available (install piper)"}


async def _task_stt(payload: dict) -> dict:
    """Speech-to-text via faster-whisper."""
    try:
        from faster_whisper import WhisperModel
        audio_path = payload.get("audio_path", "")
        if not audio_path:
            return {"error": "No audio_path provided"}
        model = WhisperModel("base", compute_type="int8")
        segments, info = model.transcribe(audio_path)
        text = " ".join(s.text for s in segments)
        return {"text": text, "language": info.language}
    except ImportError:
        return {"error": "faster-whisper not installed"}


async def _task_classify(payload: dict) -> dict:
    """Simple text classification via the loaded model."""
    text = payload.get("text", "")
    categories = payload.get("categories", [])
    prompt = (
        f"Classify the following text into one of these categories: "
        f"{', '.join(categories)}\n\nText: {text}\n\nCategory:"
    )
    return await _task_chat({"prompt": prompt, "max_tokens": 50})


async def _task_rerank(payload: dict) -> dict:
    """Cross-encoder reranking."""
    query = payload.get("query", "")
    chunks = payload.get("chunks", [])
    if not query or not chunks:
        return {"error": "query and chunks required"}
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        reranker = TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2")
        scores = list(reranker.rerank(query, chunks))
        ranked = sorted(
            zip(chunks, scores), key=lambda x: x[1], reverse=True
        )
        return {"ranked": [{"text": t, "score": float(s)} for t, s in ranked]}
    except ImportError:
        return {"error": "fastembed not installed"}


# ---------------------------------------------------------------------------
# Task queue worker loop
# ---------------------------------------------------------------------------

async def _task_worker(worker_id: str):
    """Process tasks from the queue."""
    while not _shutting_down:
        try:
            task_id, payload, result_future = await asyncio.wait_for(
                _task_queue.get(), timeout=1.0
            )
        except asyncio.TimeoutError:
            continue

        start_ts = time.time()
        _active_tasks[task_id] = asyncio.current_task()
        log.info("[%s] Processing task %s (type: %s)", worker_id, task_id, payload.get("type"))

        try:
            result = await asyncio.wait_for(execute_task(payload), timeout=_task_timeout)
            duration = round(time.time() - start_ts, 2)
            _stats["tasks_completed"] += 1
            _stats["total_duration_s"] += duration
            record = {"id": task_id, "type": payload.get("type"), "duration": duration, "status": "done"}
            result_future.set_result({"id": task_id, **result, "duration": duration})
        except asyncio.TimeoutError:
            duration = round(time.time() - start_ts, 2)
            _stats["tasks_timed_out"] += 1
            _stats["total_duration_s"] += duration
            record = {"id": task_id, "type": payload.get("type"), "duration": duration, "status": "timeout"}
            log.warning("[%s] Task %s timed out after %ds", worker_id, task_id, _task_timeout)
            result_future.set_result({"id": task_id, "error": f"Task timed out after {_task_timeout}s"})
        except asyncio.CancelledError:
            duration = round(time.time() - start_ts, 2)
            record = {"id": task_id, "type": payload.get("type"), "duration": duration, "status": "cancelled"}
            result_future.set_result({"id": task_id, "error": "Task cancelled"})
            raise
        except Exception as e:
            duration = round(time.time() - start_ts, 2)
            _stats["tasks_failed"] += 1
            _stats["total_duration_s"] += duration
            record = {"id": task_id, "type": payload.get("type"), "duration": duration, "status": "error", "error": str(e)}
            log.error("[%s] Task %s failed: %s", worker_id, task_id, e)
            result_future.set_result({"id": task_id, "error": str(e)})
        finally:
            _active_tasks.pop(task_id, None)
            _task_history.append(record)
            if len(_task_history) > 100:
                _task_history[:] = _task_history[-50:]
            _task_queue.task_done()


# ---------------------------------------------------------------------------
# Heartbeat — push-based registration with the hub
# ---------------------------------------------------------------------------

async def heartbeat_loop(interval: int = 30):
    """Periodically announce this worker to the hub."""
    import socket
    hostname = socket.gethostname()

    while not _shutting_down:
        if _hub_url:
            try:
                import httpx
                hw = get_hardware()
                # Determine model name: prefer llama-server, fall back to external backend
                model_name = ""
                if _llama_manager:
                    model_name = _llama_manager.model_name
                elif _backend_url:
                    try:
                        async with httpx.AsyncClient(timeout=5) as probe:
                            resp = await probe.get(
                                f"{_backend_url.rstrip('/v1')}/v1/internal/model/info"
                            )
                            if resp.status_code == 200:
                                model_name = resp.json().get("model_name", "")
                    except Exception:
                        pass

                payload = {
                    "hostname": hostname,
                    "port": _worker_port,
                    "tier": hw.tier(),
                    "capabilities": hw.to_dict(),
                    "model_name": model_name,
                    "active_tasks": len(_active_tasks),
                    "queued_tasks": _task_queue.qsize() if _task_queue else 0,
                    "max_concurrent": _max_concurrent,
                    "stats": _stats,
                    "uptime_s": int(time.time() - _start_time),
                }
                headers = {}
                if _api_key:
                    headers["Authorization"] = f"Bearer {_api_key}"
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"{_hub_url}/api/mesh/heartbeat",
                        json=payload,
                        headers=headers,
                    )
            except Exception as e:
                log.debug("Heartbeat failed: %s", e)
        await asyncio.sleep(interval)


_worker_port: int = 8200


# ---------------------------------------------------------------------------
# System metrics
# ---------------------------------------------------------------------------

def system_metrics() -> dict:
    """Gather current system resource usage."""
    metrics: dict = {
        "timestamp": time.time(),
        "uptime_s": int(time.time() - _start_time),
    }

    # CPU load — getloadavg is POSIX-only; psutil provides it on Windows too
    # by sampling CPU percent over a rolling window.
    try:
        if hasattr(psutil, "getloadavg") if _HAS_PSUTIL else False:
            load1, load5, load15 = psutil.getloadavg()
        else:
            load1, load5, load15 = os.getloadavg()
        metrics["cpu_load"] = {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)}
    except (OSError, AttributeError):
        # Pure-Windows with old psutil — synthesize from instantaneous CPU%.
        if _HAS_PSUTIL:
            try:
                pct = psutil.cpu_percent(interval=None) / 100 * (os.cpu_count() or 1)
                metrics["cpu_load"] = {"1m": round(pct, 2), "5m": round(pct, 2), "15m": round(pct, 2)}
            except Exception:
                pass

    # Memory — psutil is cross-platform; /proc/meminfo is the Linux/Android fallback
    if _HAS_PSUTIL:
        try:
            vm = psutil.virtual_memory()
            metrics["ram"] = {
                "total_mb": vm.total // (1024 * 1024),
                "available_mb": vm.available // (1024 * 1024),
                "used_pct": round(vm.percent, 1),
            }
        except Exception:
            pass
    if "ram" not in metrics:
        try:
            with open("/proc/meminfo") as f:
                meminfo = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        meminfo[parts[0].rstrip(":")] = int(parts[1])
                total = meminfo.get("MemTotal", 0)
                avail = meminfo.get("MemAvailable", 0)
                if total:
                    metrics["ram"] = {
                        "total_mb": total // 1024,
                        "available_mb": avail // 1024,
                        "used_pct": round((1 - avail / total) * 100, 1),
                    }
        except Exception:
            pass

    # GPU metrics via nvidia-smi
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(", ")
            if len(parts) >= 4:
                metrics["gpu"] = {
                    "utilization_pct": int(parts[0]),
                    "vram_used_mb": int(parts[1]),
                    "vram_total_mb": int(parts[2]),
                    "temperature_c": int(parts[3]),
                }
    except Exception:
        pass

    # macOS memory (final fallback for environments without psutil or /proc)
    if "ram" not in metrics and sys.platform == "darwin":
        try:
            import subprocess
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                total_mb = int(result.stdout.strip()) // (1024 * 1024)
                # vm_stat for available
                vm_result = subprocess.run(
                    ["vm_stat"], capture_output=True, text=True, timeout=5,
                )
                free_pages = 0
                if vm_result.returncode == 0:
                    for line in vm_result.stdout.splitlines():
                        if "Pages free" in line or "Pages inactive" in line:
                            try:
                                free_pages += int(line.split(":")[1].strip().rstrip("."))
                            except (ValueError, IndexError):
                                pass
                avail_mb = (free_pages * 4096) // (1024 * 1024)
                metrics["ram"] = {
                    "total_mb": total_mb,
                    "available_mb": avail_mb,
                    "used_pct": round((1 - avail_mb / total_mb) * 100, 1) if total_mb else 0,
                }
        except Exception:
            pass

    # Battery / thermal from detect
    hw = get_hardware()
    if hw.battery_pct >= 0:
        metrics["battery"] = {
            "pct": hw.battery_pct,
            "charging": hw.battery_charging,
        }
    metrics["thermal_throttled"] = hw.thermal_throttled

    # llama-server status (sync — health_check is done separately in async endpoints)
    if _llama_manager:
        metrics["llama_server"] = {
            "model": _llama_manager.model_name,
            "port": _llama_manager.port,
        }

    return metrics


# ---------------------------------------------------------------------------
# HTTP server (Starlette)
# ---------------------------------------------------------------------------

async def _test_hub_connection(max_attempts: int = 3) -> bool:
    """Verify the hub is reachable before entering the heartbeat loop.

    Sends a test heartbeat to the hub. Returns True if the hub responds
    with 200, False otherwise. Retries up to max_attempts times with 2s delay.
    """
    import socket
    hostname = socket.gethostname()

    for attempt in range(1, max_attempts + 1):
        try:
            import httpx
            hw = get_hardware()
            payload = {
                "hostname": hostname,
                "port": _worker_port,
                "tier": hw.tier(),
                "capabilities": hw.to_dict(),
                "model_name": _llama_manager.model_name if _llama_manager else "",
                "active_tasks": 0,
                "queued_tasks": 0,
                "max_concurrent": _max_concurrent,
                "stats": {},
                "uptime_s": 0,
            }
            headers = {}
            if _api_key:
                headers["Authorization"] = f"Bearer {_api_key}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{_hub_url}/api/mesh/heartbeat",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 200:
                    log.info("Hub connection verified: %s (attempt %d)", _hub_url, attempt)
                    return True
                elif resp.status_code == 401:
                    log.error(
                        "Hub auth failed (401) — check --key or LOCALFORGE_API_KEY. "
                        "Hub: %s", _hub_url
                    )
                    return False  # Don't retry auth failures
                else:
                    log.warning(
                        "Hub returned %d on connection test (attempt %d/%d)",
                        resp.status_code, attempt, max_attempts,
                    )
        except Exception as e:
            log.warning(
                "Hub connection test failed (attempt %d/%d): %s",
                attempt, max_attempts, e,
            )
        if attempt < max_attempts:
            await asyncio.sleep(2)

    log.error("Hub unreachable after %d attempts: %s", max_attempts, _hub_url)
    return False


def create_app():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(request):
        hw = get_hardware()
        return JSONResponse({
            "status": "ok" if not _shutting_down else "draining",
            "tier": hw.tier(),
            "model_name": _llama_manager.model_name if _llama_manager else "",
            "active_tasks": len(_active_tasks),
            "queued_tasks": _task_queue.qsize() if _task_queue else 0,
            "max_concurrent": _max_concurrent,
            "uptime_s": int(time.time() - _start_time),
            "capabilities": hw.to_dict(),
            "stats": _stats,
        })

    async def status(request):
        hw = get_hardware()
        return JSONResponse({
            "hardware": hw.to_dict(),
            "tier": hw.tier(),
            "active_tasks": {tid: {"type": "running"} for tid in _active_tasks},
            "queued_tasks": _task_queue.qsize() if _task_queue else 0,
            "task_history_count": len(_task_history),
            "recent_tasks": _task_history[-10:],
            "uptime_s": int(time.time() - _start_time),
            "stats": _stats,
            "hub": _hub_url or "(standalone)",
            "backend_url": _backend_url,
        })

    def _check_worker_auth(request):
        """Verify Bearer token on mutating endpoints."""
        if not _api_key:
            return True  # No key configured, allow (standalone mode)
        auth = request.headers.get("authorization", "")
        import hmac
        return hmac.compare_digest(auth, f"Bearer {_api_key}")

    async def handle_task(request):
        if not _check_worker_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if _shutting_down:
            return JSONResponse({"error": "Worker is shutting down"}, status_code=503)

        # Memory-pressure check: reject if available RAM < threshold
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        avail_mb = int(line.split()[1]) // 1024
                        if avail_mb < _min_memory_mb:
                            return JSONResponse(
                                {"error": f"Insufficient memory ({avail_mb}MB available, need {_min_memory_mb}MB)"},
                                status_code=503,
                            )
                        break
        except (OSError, ValueError):
            pass  # Non-Linux or parse error — skip check

        # Power-aware check: reject if battery too low and not charging
        hw = get_hardware()
        if 0 <= hw.battery_pct < _battery_floor and not hw.battery_charging:
            return JSONResponse(
                {"error": f"Battery too low ({hw.battery_pct}%, floor={_battery_floor}%)"},
                status_code=503,
            )

        # Reject oversized payloads (10MB max)
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 10 * 1024 * 1024:
            return JSONResponse({"error": "Payload too large"}, status_code=413)

        body = await request.json()
        task_id = body.get("id", uuid.uuid4().hex[:12])

        # Queue the task with a future for the result
        loop = asyncio.get_event_loop()
        result_future = loop.create_future()
        await _task_queue.put((task_id, body, result_future))
        _stats["tasks_queued"] += 1

        # Wait for the result (with timeout)
        try:
            result = await asyncio.wait_for(result_future, timeout=_task_timeout + 10)
            return JSONResponse(result)
        except asyncio.TimeoutError:
            return JSONResponse({"id": task_id, "error": "Queue timeout"}, status_code=504)

    async def cancel_task(request):
        if not _check_worker_auth(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        body = await request.json()
        task_id = body.get("id", "")

        if task_id and task_id in _active_tasks:
            _active_tasks[task_id].cancel()
            return JSONResponse({"status": "cancelled", "task_id": task_id})
        return JSONResponse({"status": "not found", "task_id": task_id}, status_code=404)

    async def metrics_endpoint(request):
        return JSONResponse(system_metrics())

    async def on_startup():
        global _task_queue
        _task_queue = asyncio.Queue(maxsize=_max_concurrent * 4)

        # Start task worker coroutines
        for i in range(_max_concurrent):
            asyncio.create_task(_task_worker(f"worker-{i}"))
        log.info("Started %d task workers (queue max: %d)", _max_concurrent, _task_queue.maxsize)

        if _hub_url:
            # Verify hub is reachable before entering heartbeat loop
            reachable = await _test_hub_connection()
            if reachable:
                asyncio.create_task(heartbeat_loop())
                log.info("Heartbeat started → %s", _hub_url)
            else:
                log.warning(
                    "Hub unreachable at %s — starting heartbeat anyway "
                    "(will retry on each interval)", _hub_url
                )
                asyncio.create_task(heartbeat_loop())

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/status", status),
            Route("/task", handle_task, methods=["POST"]),
            Route("/task/cancel", cancel_task, methods=["POST"]),
            Route("/metrics", metrics_endpoint),
        ],
        on_startup=[on_startup],
    )
    return app


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

async def _graceful_shutdown():
    """Wait for in-flight tasks to finish, stop llama-server, then exit."""
    global _shutting_down
    _shutting_down = True
    log.info("Graceful shutdown: waiting for %d active tasks", len(_active_tasks))
    # Give active tasks up to 30s to finish
    for _ in range(30):
        if not _active_tasks:
            break
        await asyncio.sleep(1)
    if _active_tasks:
        log.warning("Force-killing %d remaining tasks", len(_active_tasks))
        for task in _active_tasks.values():
            task.cancel()
    # Stop llama-server
    if _llama_manager:
        await _llama_manager.stop()


def main():
    global _start_time, _hub_url, _api_key, _worker_port, _max_concurrent, _task_timeout, _backend_url, _min_memory_mb, _battery_floor
    _start_time = time.time()

    parser = argparse.ArgumentParser(description="LocalForge device worker")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--hub", type=str, default="",
                        help="Hub gateway URL (e.g., ai-hub:8100)")
    parser.add_argument("--key", type=str, default="",
                        help="API key for hub authentication")
    parser.add_argument("--heartbeat", type=int, default=30,
                        help="Heartbeat interval in seconds (default: 30)")
    parser.add_argument("--max-concurrent", type=int, default=2,
                        help="Max concurrent tasks (default: 2)")
    parser.add_argument("--task-timeout", type=int, default=180,
                        help="Task timeout in seconds (default: 180)")
    # Self-hosted llama-server
    parser.add_argument("--model", type=str, default="",
                        help="Path to GGUF model for self-hosted llama-server")
    parser.add_argument("--llama-port", type=int, default=5050,
                        help="Port for llama-server (default: 5050)")
    parser.add_argument("--no-llama", action="store_true",
                        help="Disable auto-detection of local models")
    parser.add_argument("--gpu-layers", type=int, default=-1,
                        help="GPU layers for llama-server (-1=auto, 0=CPU only)")
    parser.add_argument("--ctx-size", type=int, default=0,
                        help="Context size for llama-server (0=auto)")
    parser.add_argument("--tls-cert", type=str, default="",
                        help="Path to TLS certificate (enables HTTPS)")
    parser.add_argument("--tls-key", type=str, default="",
                        help="Path to TLS private key")
    parser.add_argument("--min-memory", type=int, default=500,
                        help="Minimum available RAM in MB to accept tasks (default: 500)")
    parser.add_argument("--battery-floor", type=int, default=15,
                        help="Minimum battery %% to accept tasks when not charging (default: 15)")
    parser.add_argument("--platform", type=str, default="auto",
                        choices=["auto", "linux", "darwin", "win32", "android"],
                        help="Override auto-detected platform (default: auto)")
    args = parser.parse_args()

    # Platform override — written to HardwareInfo after detect() runs.
    # Primarily for testing and for devices where autodetect gets confused
    # (e.g., a Chromebook crouton env that looks linux-ish but needs android routing).
    _platform_override = args.platform if args.platform != "auto" else ""

    _worker_port = args.port
    _max_concurrent = args.max_concurrent
    _task_timeout = args.task_timeout
    if args.hub:
        _hub_url = args.hub if args.hub.startswith("http") else f"http://{args.hub}"
    _api_key = args.key or os.environ.get("LOCALFORGE_API_KEY", "")
    _backend_url = os.environ.get("LOCALFORGE_BACKEND_URL", _backend_url)
    _min_memory_mb = args.min_memory
    _battery_floor = args.battery_floor

    # Detect and log hardware
    hw = get_hardware()
    if _platform_override:
        hw.platform = _platform_override
    print(f"LocalForge Worker starting on :{args.port}")
    print(f"  Platform: {hw.platform}{' (override)' if _platform_override else ''}")
    print(f"  GPU: {hw.gpu_name or 'none'} ({hw.gpu_type})")
    print(f"  VRAM: {hw.vram_mb} MB | RAM: {hw.ram_mb} MB | CPU: {hw.cpu_cores} cores")
    print(f"  Tier: {hw.tier()}")
    caps = [k for k in ("inference", "embeddings", "tts", "stt", "vision", "reranking")
            if getattr(hw, k, False)]
    print(f"  Capabilities: {', '.join(caps)}")
    print(f"  Max concurrent: {_max_concurrent} | Timeout: {_task_timeout}s")
    print(f"  Backend: {_backend_url}")

    # Self-hosted llama-server setup
    global _llama_manager
    model_path = args.model
    if not model_path and not args.no_llama:
        # Auto-detect: look for GGUF files in standard locations
        from pathlib import Path
        for search_dir in [
            Path(os.environ.get("INSTALL_DIR", "")) / "models",
            Path.home() / ".ai-hub-worker" / "models",
            Path.cwd() / "models",
        ]:
            if search_dir.is_dir():
                gguf_files = sorted(search_dir.glob("*.gguf"), key=lambda p: p.stat().st_size, reverse=True)
                if gguf_files:
                    model_path = str(gguf_files[0])
                    print(f"  Auto-detected model: {gguf_files[0].name}")
                    break

    if model_path:
        # Auto-detect CPU-only devices and set gpu_layers=0
        gpu_layers = args.gpu_layers
        if gpu_layers == -1:
            hw_check = get_hardware()
            if hw_check.gpu_type == "none":
                gpu_layers = 0
                print("  GPU: none detected — using CPU-only inference (gpu_layers=0)")

        _llama_manager = LlamaServerManager(
            model_path=model_path,
            port=args.llama_port,
            gpu_layers=gpu_layers,
            ctx_size=args.ctx_size,
            parallel=min(args.max_concurrent, 2),
        )
        # Start llama-server synchronously before uvicorn
        started = asyncio.run(_llama_manager.start())
        if started:
            print(f"  llama-server: {_llama_manager.model_name} on :{args.llama_port}")
        else:
            print("  llama-server: FAILED to start (falling back to external backend)")
            _llama_manager = None
    else:
        print("  llama-server: disabled (no model found)")

    if _hub_url:
        print(f"  Hub: {_hub_url} (heartbeat every {args.heartbeat}s)")
    else:
        print("  Hub: standalone (no heartbeat)")

    import uvicorn
    app = create_app()
    uvicorn_kwargs = dict(host=args.host, port=args.port, log_level="info")
    if args.tls_cert and args.tls_key:
        uvicorn_kwargs["ssl_certfile"] = args.tls_cert
        uvicorn_kwargs["ssl_keyfile"] = args.tls_key
        print(f"  TLS: enabled ({args.tls_cert})")
    uvicorn.run(app, **uvicorn_kwargs)


if __name__ == "__main__":
    main()
