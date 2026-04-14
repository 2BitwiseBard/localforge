"""Tool handler registry for LocalForge.

The `tool_handler` decorator registers MCP tools at import time.
Each tool module (context.py, memory.py, etc.) uses this decorator
to register its tools into the shared registry.

To add a new tool:
    1. Create or open a tool module in this package
    2. Decorate your async handler with @tool_handler(name=..., description=..., schema=...)
    3. Import the module in server.py's tool import block

The registry is consumed by server.py to build the MCP tool list.
"""

import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from mcp.types import Tool

log = logging.getLogger("localforge.tools")

# ---------------------------------------------------------------------------
# Shared registry — populated by @tool_handler decorators at import time
# ---------------------------------------------------------------------------
_tool_definitions: list[Tool] = []
_tool_handlers: dict[str, Callable[..., Awaitable[str]]] = {}


def tool_handler(
    name: str,
    description: str,
    schema: dict[str, Any],
):
    """Decorator to register a tool with its MCP definition and async handler."""
    def decorator(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        if name in _tool_handlers:
            log.warning(
                "Tool name collision: '%s' registered by %s, overwriting previous handler %s",
                name, fn.__module__, _tool_handlers[name].__module__,
            )
        _tool_definitions.append(Tool(name=name, description=description, inputSchema=schema))
        _tool_handlers[name] = fn

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            return await fn(*args, **kwargs)
        return wrapper
    return decorator
