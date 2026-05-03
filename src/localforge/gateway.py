"""HTTP gateway for the LocalForge MCP server.

Wraps the MCP Server app in a Starlette/uvicorn HTTP server
using StreamableHTTPSessionManager for MCP-over-HTTP transport.

Serves:
  /health    — public health endpoint (no auth)
  /mcp/      — MCP JSON-RPC endpoint (bearer auth)
  /api/*     — dashboard API (bearer auth)
  /          — web dashboard static files (public)

Usage:
    python -m localforge.gateway                    # HTTP mode on :8100
    python -m localforge.gateway --port 9000        # custom port
    python -m localforge.gateway --host 127.0.0.1   # localhost only
"""

import argparse
import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware as _BaseMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from localforge.auth import BearerAuthMiddleware
from localforge.config import load_config_cached
from localforge.dashboard.routes import dashboard_routes
from localforge.gpu_pool import GPUPool
from localforge.log import setup_logging
from localforge.server import app as mcp_app

log = logging.getLogger("mcp-gateway")

STATIC_DIR = Path(__file__).parent / "dashboard" / "static"
START_TIME = time.time()

# GPU pool instance (shared)
gpu_pool = GPUPool({})

# Agent supervisor (shared, set during lifespan)
agent_supervisor = None


def _load_config() -> dict:
    return load_config_cached()


# ---------------------------------------------------------------------------
# Health endpoint (public, no auth)
# ---------------------------------------------------------------------------
async def health(request: Request) -> JSONResponse:
    """Return gateway health + model status + GPU pool."""
    import httpx

    cfg = _load_config()
    backend_url = cfg.get("backends", {}).get("local", {}).get("url", "http://localhost:5000/v1")
    uptime = int(time.time() - START_TIME)

    model_info = {"status": "unknown"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{backend_url}/internal/model/info")
            if resp.status_code == 200:
                data = resp.json()
                model_info = {
                    "status": "loaded",
                    "model_name": data.get("model_name", "unknown"),
                    "lora_names": data.get("lora_names", []),
                }
    except Exception:
        model_info = {"status": "unreachable"}

    result = {
        "service": "mcp-gateway",
        "status": "ok",
        "uptime_seconds": uptime,
        "model": model_info,
    }

    # Include GPU pool status if active
    pool_status = gpu_pool.status()
    if pool_status:
        result["backends"] = pool_status

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Starlette app with MCP transport + dashboard
# ---------------------------------------------------------------------------
session_manager = StreamableHTTPSessionManager(app=mcp_app, stateless=True)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    global agent_supervisor

    # Initialize GPU pool + compute mesh
    cfg = _load_config()
    gpu_pool._config = cfg.get("gpu_pool", {})
    gpu_pool._routing_rules = gpu_pool._config.get("model_routing", {})
    # Merge compute_pool config so worker discovery + task routing picks it up
    compute_cfg = cfg.get("compute_pool", {})
    if compute_cfg:
        gpu_pool._config.update(compute_cfg)
    gpu_pool.register_from_config(cfg.get("backends", {}))
    await gpu_pool.start()

    # Make GPU pool accessible to compute tools
    from localforge.tools import compute as _compute_tools

    _compute_tools._gpu_pool = gpu_pool

    # Make GPU pool accessible to client.py for mesh-aware routing
    from localforge import client as _client_mod

    _client_mod.set_gpu_pool(gpu_pool)

    # Make GPU pool accessible to dashboard routes for heartbeat registration
    from localforge.dashboard import routes as _dash_routes

    _dash_routes._gpu_pool_ref = gpu_pool

    # Start agent supervisor
    gateway_cfg = cfg.get("gateway", {})
    api_keys = gateway_cfg.get("api_keys", [])
    api_key = api_keys[0] if api_keys else os.environ.get("LOCAL_AI_KEY", "")
    port = gateway_cfg.get("port", 8100)

    try:
        import localforge.agents.code_watcher  # noqa: F401
        import localforge.agents.daily_digest  # noqa: F401

        # Import all agent modules to register them
        import localforge.agents.health_monitor  # noqa: F401
        import localforge.agents.index_maintainer  # noqa: F401
        import localforge.agents.news_agent  # noqa: F401
        import localforge.agents.research_agent  # noqa: F401
        import localforge.agents.yaml_schema_validator  # noqa: F401
        from localforge.agents.supervisor import AgentSupervisor

        gateway_url = os.environ.get(
            "LOCALFORGE_GATEWAY_URL",
            f"http://{gateway_cfg.get('host', '0.0.0.0')}:{port}",
        )
        agent_supervisor = AgentSupervisor(
            gateway_url=gateway_url,
            api_key=api_key,
        )
        await agent_supervisor.start()
        # Make supervisor, bus, and task queue accessible to dashboard routes
        from localforge.dashboard import routes as _routes

        _routes._supervisor = agent_supervisor
        _routes._message_bus = agent_supervisor.bus
        _routes._task_queue = agent_supervisor.task_queue
        # Start approval queue TTL warning loop
        try:
            from localforge.agents.approval import ApprovalQueue
            from localforge.agents.message_bus import Message as _Msg

            _aq = ApprovalQueue()
            _aq.on_notify(
                lambda payload: agent_supervisor.bus.publish(
                    _Msg(sender="approval-gate", topic="agent.notification", payload=payload)
                )
            )
            _aq.start_warning_loop()
            _routes._approval_queue = _aq
        except Exception as exc:
            log.warning(f"Approval queue warning loop failed: {exc}")
        log.info("Agent supervisor started")
    except Exception as e:
        log.warning(f"Agent supervisor failed to start: {e}")

    # Auto-load startup model if configured (off by default)
    import json as _json

    sc_path = Path(os.environ.get("LOCALFORGE_DATA_DIR", ".")) / "startup_config.json"
    if sc_path.exists():
        try:
            sc = _json.loads(sc_path.read_text())
            startup_model = sc.get("startup_model", "").strip()
            if startup_model:
                log.info("Auto-loading startup model: %s", startup_model)
                backend_url = cfg.get("backends", {}).get("local", {}).get("url", "http://localhost:5000/v1")
                model_cfg: dict = {}
                for pat, overrides in cfg.get("models", {}).items():
                    if pat in startup_model:
                        model_cfg = overrides
                        break
                ctx_size = model_cfg.get("ctx_size", 8192)
                load_payload = {
                    "model_name": startup_model,
                    "args": {"ctx_size": ctx_size, "gpu_layers": model_cfg.get("gpu_layers", -1)},
                    "settings": {"truncation_length": ctx_size},
                }
                import httpx as _httpx

                try:
                    async with _httpx.AsyncClient(timeout=180) as _hc:
                        r = await _hc.post(f"{backend_url}/internal/model/load", json=load_payload)
                    if r.status_code == 200:
                        log.info("Startup model loaded: %s", startup_model)
                    else:
                        log.warning("Startup model load returned %s: %s", r.status_code, r.text[:200])
                except Exception as exc:
                    log.warning("Startup model load failed: %s", exc)
        except Exception as exc:
            log.warning("Could not read startup_config.json: %s", exc)

    async with session_manager.run():
        log.info("MCP HTTP gateway started (with dashboard + GPU pool + agents)")
        yield

    log.info("Shutting down gracefully...")
    if agent_supervisor:
        await agent_supervisor.stop()
    await gpu_pool.stop()
    # Checkpoint SQLite WAL files for clean shutdown
    try:
        import sqlite3

        for db_file in Path(os.environ.get("LOCALFORGE_DATA_DIR", ".")).glob("*.db"):
            conn = sqlite3.connect(str(db_file))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
    except Exception as e:
        log.debug(f"WAL checkpoint: {e}")
    log.info("MCP HTTP gateway stopped")


starlette_app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/mcp", app=session_manager.handle_request),
        Mount("/api", routes=dashboard_routes),
        Mount(
            "/static/workers", app=StaticFiles(directory=str(Path(__file__).parent / "workers")), name="worker-files"
        ),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
        Route("/", lambda r: FileResponse(str(STATIC_DIR / "index.html"))),
    ],
    lifespan=lifespan,
)
starlette_app.add_middleware(BearerAuthMiddleware)


# ---------------------------------------------------------------------------
# Security headers middleware (CSP, etc.)
# ---------------------------------------------------------------------------
class SecurityHeadersMiddleware(_BaseMiddleware):
    """Add Content-Security-Policy and other security headers to all responses."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # CSP: block inline scripts, restrict sources to self + data URIs for images
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


# Default body size limit: 1 MB for most routes, overridden per-route where needed
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB
# Routes that need larger bodies (file uploads, photos, training data)
LARGE_BODY_PATHS = {
    "/api/photos/upload",
    "/api/videos/upload",
    "/api/upload-image",
    "/api/transcribe",
    "/api/training/start",
    "/api/training/prepare",
}
LARGE_BODY_LIMIT = 50 * 1024 * 1024  # 50 MB for uploads


class RequestBodyLimitMiddleware(_BaseMiddleware):
    """Reject requests with bodies exceeding the size limit.

    Prevents OOM from oversized payloads. Upload routes get a higher limit.
    """

    async def dispatch(self, request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
            except ValueError:
                return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)

            path = request.url.path.rstrip("/")
            limit = LARGE_BODY_LIMIT if path in LARGE_BODY_PATHS else MAX_BODY_BYTES
            if size > limit:
                return JSONResponse(
                    {"error": f"Request body too large ({size:,} bytes, max {limit:,})"},
                    status_code=413,
                )
        return await call_next(request)


class RequestIDMiddleware(_BaseMiddleware):
    """Attach a unique request ID to every request for distributed tracing.

    Reads X-Request-ID from incoming headers (for client-side correlation) or
    generates a new UUID4. Sets the ID in the ContextVar so JSON log lines
    automatically include it, and echoes it in the response header.
    """

    async def dispatch(self, request, call_next):
        import uuid

        from localforge.log import set_request_id

        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        set_request_id(rid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


starlette_app.add_middleware(SecurityHeadersMiddleware)
starlette_app.add_middleware(RequestBodyLimitMiddleware)
starlette_app.add_middleware(RequestIDMiddleware)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def run_http(
    host: str = "0.0.0.0",
    port: int = 8100,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
):
    kwargs: dict = {"host": host, "port": port, "log_level": "info"}
    if ssl_certfile and ssl_keyfile:
        kwargs["ssl_certfile"] = ssl_certfile
        kwargs["ssl_keyfile"] = ssl_keyfile
    config = uvicorn.Config(starlette_app, **kwargs)
    server = uvicorn.Server(config)
    await server.serve()


def main():
    import os

    parser = argparse.ArgumentParser(description="MCP HTTP Gateway")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8100, help="Port (default: 8100)")
    parser.add_argument(
        "--log-format", choices=["human", "json"], default="human", help="Log output format (default: human)"
    )
    parser.add_argument("--log-level", default="INFO", help="Log level (default: INFO)")
    parser.add_argument(
        "--ssl-certfile",
        default=os.environ.get("LOCALFORGE_SSL_CERTFILE"),
        help="TLS cert file (enables HTTPS). Env: LOCALFORGE_SSL_CERTFILE",
    )
    parser.add_argument(
        "--ssl-keyfile",
        default=os.environ.get("LOCALFORGE_SSL_KEYFILE"),
        help="TLS key file (enables HTTPS). Env: LOCALFORGE_SSL_KEYFILE",
    )
    args = parser.parse_args()

    setup_logging(fmt=args.log_format, level=args.log_level)
    asyncio.run(
        run_http(
            host=args.host,
            port=args.port,
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
        )
    )


if __name__ == "__main__":
    main()
