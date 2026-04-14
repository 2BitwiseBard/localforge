# Contributing to LocalForge

Thanks for your interest in contributing! This document covers the basics.

## Development Setup

```bash
git clone https://github.com/bitwisebard/localforge.git
cd localforge

# Install with dev dependencies
pip install -e ".[all,dev]"

# Run tests
pytest

# Lint
ruff check src/
ruff format --check src/
```

## Project Structure

```
src/localforge/
  server.py          # MCP server entry point (~100 lines)
  gateway.py         # HTTP gateway (Starlette + uvicorn)
  config.py          # Configuration loading and state
  client.py          # HTTP client, chat completion, caching
  cache.py           # Response cache
  chunking.py        # BM25 + tree-sitter code chunking
  embeddings.py      # Dense/sparse/ColBERT embedding models
  exceptions.py      # Exception hierarchy
  log.py             # Structured logging (human + JSON)
  paths.py           # Data directory resolution
  auth.py            # Bearer token authentication
  gpu_pool.py        # Multi-device GPU routing
  tools/             # 112 MCP tool handlers (21 modules)
  agents/            # Autonomous agent framework
  knowledge/         # SQLite knowledge graph
  workflows/         # DAG workflow engine
  workers/           # Compute mesh device workers
  dashboard/         # PWA web dashboard
  media/             # Media processing
```

## Adding a New Tool

1. Pick or create a module in `src/localforge/tools/`
2. Decorate your handler:

```python
from localforge.tools import tool_handler

@tool_handler(
    name="my_tool",
    description="What this tool does",
    schema={
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "The input"},
        },
        "required": ["input"],
    },
)
async def my_tool(args: dict) -> str:
    return f"Result: {args['input']}"
```

3. Import your module in `server.py`'s import block
4. The tool is automatically registered and available via MCP

## Code Style

- **Formatter:** ruff format (line length 120)
- **Linter:** ruff check (E, F, W, I rules)
- **Type hints:** Use them for function signatures. `dict`, `list`, `str | None` style (not `Optional`).
- **Logging:** Use `logging.getLogger("localforge")` or `logging.getLogger("localforge.module")`
- **Exceptions:** Use the hierarchy in `exceptions.py`, not bare `Exception`

## Testing

```bash
# All tests
pytest

# Specific module
pytest tests/test_chunking.py

# With coverage
pytest --cov=localforge
```

Tests should not require a running backend. Mock `httpx` responses for anything that hits the API.

## Pull Requests

- Keep PRs focused — one feature or fix per PR
- Include tests for new tools or behavior changes
- Run `ruff check` and `ruff format --check` before submitting
- Update the tool count in README.md if you add/remove tools

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 license.
