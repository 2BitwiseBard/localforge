"""LocalForge MCP server — local AI coding station.

Thin entry point: imports all tool modules (which auto-register via
the @tool_handler decorator), wires up the MCP Server app, and runs.

Usage:
    python -m localforge.server          # stdio mode (for Claude Code MCP)
    python -m localforge.gateway         # HTTP mode on :8100 (for remote access)
"""

import asyncio
import logging

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from localforge import config as cfg
from localforge.client import _session_stats
from localforge.exceptions import (
    BackendError,
    BackendUnreachableError,
    ConfigError,
    ModelNotLoadedError,
)
from localforge.log import setup_logging

# Import all tool modules to trigger @tool_handler registration.
# Order doesn't matter — each module independently decorates its handlers.
from localforge.tools import (  # noqa: F401
    _tool_definitions,
    _tool_handlers,
    agents_tools,
    analysis,
    chat,
    compute,
    config_tools,
    context,
    diff,
    filesystem,
    generation,
    git,
    infrastructure,
    knowledge,
    memory,
    orchestration,
    parallel,
    presets,
    rag,
    semantic,
    sessions,
    shell,
    training,
    web,
)

setup_logging()
log = logging.getLogger("localforge")


# ---------------------------------------------------------------------------
# MCP app
# ---------------------------------------------------------------------------
app = Server("local-model")


@app.list_tools()
async def list_tools():
    return _tool_definitions


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    handler = _tool_handlers.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    _session_stats["tool_calls"][name] = _session_stats["tool_calls"].get(name, 0) + 1

    try:
        result = await handler(arguments)
    except BackendUnreachableError as e:
        return _error(str(e))
    except ModelNotLoadedError as e:
        return _error(str(e))
    except ConfigError as e:
        return _error(f"Configuration error: {e}")
    except BackendError as e:
        return _error(f"Backend error: {e}")
    except httpx.ConnectError:
        return _error(
            f"Cannot connect to text-generation-webui at {cfg.TGWUI_BASE}. "
            f"Is it running? Check your backend configuration in config.yaml."
        )
    except httpx.HTTPStatusError as e:
        return _error(f"HTTP error from text-generation-webui: {e}")
    except httpx.ReadTimeout:
        return _error("Request timed out (120s). The model may be overloaded or the prompt too long.")
    except (KeyError, IndexError) as e:
        return _error(f"Unexpected response format: {e}")
    except Exception as e:
        return _error(f"Unexpected error: {type(e).__name__}: {e}")

    return [TextContent(type="text", text=result)]


def _error(msg: str) -> list[TextContent]:
    log.error(msg)
    return [TextContent(type="text", text=f"Error: {msg}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def main_sync():
    """Sync entry point for console_scripts."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
