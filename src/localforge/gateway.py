#!/usr/bin/env python3
"""HTTP gateway for the local-model MCP server.

Wraps the existing MCP Server app in a Starlette/uvicorn HTTP server
using StreamableHTTPSessionManager for MCP-over-HTTP transport.

Serves:
  /health    — public health endpoint (no auth)
  /mcp/      — MCP JSON-RPC endpoint (bearer auth)
  /api/*     — dashboard API (bearer auth)
  /          — web dashboard static files (public)

Usage:
    python3 gateway.py                    # HTTP mode on :8100
    python3 gateway.py --port 9000        # custom port
    python3 gateway.py --host 127.0.0.1   # localhost only

The existing stdio mode is still available via:
    python3 server.py                     # unchanged
"""

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path
from typing import AsyncIterator

import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

# Ensure the server module is importable
sys.path.insert(0, str(Path(__file__).parent))
from server import app as mcp_app  # noqa: E402
from auth import BearerAuthMiddleware  # noqa: E402
from dashboard.routes import dashboard_routes  # noqa: E402
from gpu_pool import GPUPool  # noqa: E402

log = logging.getLogger("mcp-gateway")

CONFIG_PATH = Path(__file__).parent / "config.yaml"
STATIC_DIR = Path(__file__).parent / "dashboard" / "static"
START_TIME = time.time()

# GPU pool instance (shared)
gpu_pool = GPUPool(_cfg := {})

# Agent supervisor (shared, set during lifespan)
agent_supervisor = None


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


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

    # Make GPU pool accessible to server.py tools
    try:
        import server as _server
        _server._gpu_pool = gpu_pool
    except Exception:
        pass

    # Start agent supervisor
    gateway_cfg = cfg.get("gateway", {})
    api_keys = gateway_cfg.get("api_keys", [])
    api_key = api_keys[0] if api_keys else ""
    port = gateway_cfg.get("port", 8100)

    try:
        from agents.supervisor import AgentSupervisor
        # Import all agent modules to register them
        import agents.health_monitor  # noqa: F401
        import agents.index_maintainer  # noqa: F401
        import agents.code_watcher  # noqa: F401
        import agents.research_agent  # noqa: F401
        import agents.news_agent  # noqa: F401
        import agents.daily_digest  # noqa: F401

        agent_supervisor = AgentSupervisor(
            gateway_url=f"http://localhost:{port}",
            api_key=api_key,
        )
        await agent_supervisor.start()
        # Make supervisor, bus, and task queue accessible to dashboard routes
        import dashboard.routes as _routes
        _routes._supervisor = agent_supervisor
        _routes._message_bus = agent_supervisor.bus
        _routes._task_queue = agent_supervisor.task_queue
        log.info("Agent supervisor started")
    except Exception as e:
        log.warning(f"Agent supervisor failed to start: {e}")

    async with session_manager.run():
        log.info("MCP HTTP gateway started (with dashboard + GPU pool + agents)")
        yield

    if agent_supervisor:
        await agent_supervisor.stop()
    await gpu_pool.stop()
    log.info("MCP HTTP gateway stopped")


starlette_app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/mcp", app=session_manager.handle_request),
        Mount("/api", routes=dashboard_routes),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
        Route("/", lambda r: FileResponse(str(STATIC_DIR / "index.html"))),
    ],
    lifespan=lifespan,
)
starlette_app.add_middleware(BearerAuthMiddleware)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def run_http(host: str = "0.0.0.0", port: int = 8100):
    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


def main():
    parser = argparse.ArgumentParser(description="MCP HTTP Gateway")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8100, help="Port (default: 8100)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run_http(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
