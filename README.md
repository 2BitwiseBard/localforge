# LocalForge

A small MCP server and HTTP gateway for running locally-hosted language
models. Hub for a multi-device compute mesh, with a PWA dashboard for
model swap, monitoring, and notes.

Bring your own models, keep your data local. Trimming toward a
skeleton-minimal target post-OS-reinstall.

---

## What is this?

LocalForge is a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
server that talks to any OpenAI-compatible backend (text-generation-webui,
llama.cpp, llama-swap, vLLM) and exposes a focused set of tools for chat,
filesystem/shell, web search, git context, and infrastructure management.

```
You / Claude Code / IDE
        |
    [MCP Protocol]
        |
   LocalForge
   ├── Local chat (single-turn + multi-turn)
   ├── Filesystem + shell (sandboxed)
   ├── Web search + fetch
   ├── Git context
   ├── Model swap / health / benchmark
   ├── Compute mesh (Tailscale auto-discovery)
   ├── Web dashboard (PWA, 5 tabs)
   └── CLI tool (works from any device)
        |
   [OpenAI-compatible API]
        |
   Your local models (GGUF, any size)
```

## Quick Start

### Prerequisites

- Python 3.11+
- An OpenAI-compatible backend running (e.g.,
  [text-generation-webui](https://github.com/oobabooga/text-generation-webui)
  with `--api` flag, llama.cpp's `llama-server`, or `llama-swap`)
- A GGUF model loaded in your backend

### Install

```bash
git clone https://github.com/2BitwiseBard/localforge.git
cd localforge

# Core only
pip install .

# Or with optional features (gateway + agents + web + auth + worker)
pip install ".[all]"
```

### Configure

```bash
cp examples/config.yaml.example src/localforge/config.yaml

# Generate an API key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Edit config.yaml — set your API key and backend URL
```

### Run

```bash
# As MCP server (stdio mode — for Claude Code, IDEs)
python -m localforge.server

# As HTTP gateway (for dashboard, CLI, remote access)
python -m localforge.gateway --port 8100
```

### Connect to Claude Code

```json
{
  "local-model": {
    "command": "python",
    "args": ["-m", "localforge.server"],
    "cwd": "/path/to/localforge/src"
  }
}
```

## Features

### MCP tools

A focused tool surface for use by IDE/agent clients. The current set
covers chat (`local_chat`, `multi_turn_chat`), filesystem and shell
(`fs_read`, `fs_write`, `fs_edit`, `fs_list`, `fs_glob`, `fs_grep`,
`shell_exec`), git context (`git_context`), web search and fetch
(`web_search`, `web_fetch`), model management (`swap_model`,
`health_check`, `benchmark`, `sync_models`), modes and characters
(`set_mode`, `set_character`), and a few utilities for sessions and
notes.

Post-reinstall the tool count contracts further toward the
skeleton-minimal target — see `TODO.md`.

### Autonomous agents

Two built-in agents, both running through a trust-gated supervisor:

| Agent | Trust | Default | What it does |
|---|---|---|---|
| health-monitor | monitor | enabled | Pings services every 5 min, alerts on failure |
| index-maintainer | safe | disabled | Refreshes RAG indexes (reserved for future use) |

Earlier code-watcher / research / news / digest agents were retired in
the 2026-05-12 cleanup — see `devlog.md`. The supervisor itself stays
as a skeleton for future use.

### Multi-device compute mesh

Workers register with the hub over Tailscale and push heartbeats with
their hardware capabilities. The hub routes requests by capability and
model availability. Mesh code is present but inactive until a second
machine joins the tailnet — see `~/Development/NEW-OS-PLAN.md` for the
activation arc.

### Web dashboard (PWA)

5 tabs: **Status**, **Mesh**, **Config** (model swap + params),
**Agents**, **Notes**. Installable on phones.

### Hub modes

```
set_mode("development")  # code-focused, low temp
set_mode("research")     # thorough, medium temp
set_mode("creative")     # expansive, high temp
set_mode("review")       # strict, analytical
```

## Architecture

```
localforge/
  src/localforge/
    server.py           MCP server entry point
    gateway.py          HTTP gateway (Starlette + uvicorn)
    config.py           Config state, generation params, backends
    client.py           httpx client pool, chat, caching, failover
    auth.py             Bcrypt auth + token-bucket rate limiting
    cache.py            Response cache with TTL
    paths.py            Data dir resolution (LOCALFORGE_DATA_DIR)
    exceptions.py       Error hierarchy
    log.py              Structured logging (human + JSON)
    enrollment.py       Mesh worker enrollment
    gpu_pool.py         Mesh worker registry + routing
    tools/              MCP tools (chat, filesystem, shell, web, git, …)
    agents/             Supervisor + 2 agents + approval queue
    workers/            Mesh device worker code
    dashboard/          PWA web interface (5 tabs)
  cli/local             Remote-capable CLI tool
  scripts/              setup-worker.sh, systemd templates
  examples/             Config templates
  docs/                 Documentation
  tests/                Unit tests
```

## Documentation

- [Quick Start](docs/quickstart.md)
- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Multi-Device Setup](docs/multi-device.md)
- [Agent Development](docs/agent-development.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).
