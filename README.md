# LocalForge

Your local AI coding station. MCP server + multi-device compute mesh + autonomous agents.

Bring your own models, keep your data local.

---

## What is this?

LocalForge is a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that turns locally-running language models into a full-featured AI development platform. It connects to any OpenAI-compatible backend (text-generation-webui, llama.cpp, Ollama, vLLM) and exposes **112 tools** for code analysis, RAG search, autonomous agents, knowledge graphs, and more.

```
You / Claude Code / IDE
        |
    [MCP Protocol]
        |
   LocalForge (112 tools)
   ├── Code analysis, review, generation
   ├── RAG with 4-signal retrieval (BM25 + dense + SPLADE + ColBERT)
   ├── Knowledge graph (SQLite + FTS5 + semantic search)
   ├── Autonomous agents (6 built-in, trust-gated)
   ├── Workflow engine (DAG execution)
   ├── Multi-device compute mesh (Tailscale auto-discovery)
   ├── Web dashboard (PWA, 10 tabs)
   └── CLI tool (works from any device)
        |
   [OpenAI API]
        |
   Your local models (GGUF, any size)
```

## Why LocalForge?

| | Ollama | Open WebUI | LocalAI | **LocalForge** |
|---|---|---|---|---|
| Model runner | Yes | No | Yes | Via backend |
| Chat UI | No | Yes | No | Yes (PWA) |
| MCP native | No | No | No | **Yes** |
| RAG search | No | Basic | No | **4-signal fusion** |
| Autonomous agents | No | No | No | **6 built-in** |
| Knowledge graph | No | No | No | **Yes** |
| Multi-device mesh | No | No | No | **Yes** |
| Workflow engine | No | No | No | **Yes** |
| Code-first tools | No | No | No | **112 tools** |

## Quick Start

### Prerequisites

- Python 3.11+
- An OpenAI-compatible backend running (e.g., [text-generation-webui](https://github.com/oobabooga/text-generation-webui) with `--api` flag)
- A GGUF model loaded in your backend

### Install

```bash
# Clone
git clone https://github.com/bitwisebard/localforge.git
cd localforge

# Install (core only)
pip install .

# Or with all features
pip install ".[all]"
```

### Configure

```bash
# Copy example config
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

Add to your Claude Code MCP settings:

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

### 112 MCP Tools

- **Code Analysis:** `analyze_code`, `batch_review`, `summarize_file`, `explain_error`, `file_qa`
- **Code Generation:** `suggest_refactor`, `generate_test_stubs`, `draft_docs`, `translate_code`
- **Diff Review:** `review_diff`, `diff_explain`, `draft_commit_message`
- **RAG Search:** `index_directory`, `rag_query`, `semantic_search`, `hybrid_search`
- **Chat:** `local_chat`, `multi_turn_chat`, `validated_chat`
- **Knowledge Graph:** `kg_add`, `kg_relate`, `kg_query`, `kg_context`, `kg_timeline`
- **Parallel Processing:** `fan_out`, `parallel_file_review`, `quality_sweep`
- **Infrastructure:** `health_check`, `swap_model` (20 loading params), `benchmark`, `slot_info`
- **Compute Mesh:** `compute_status`, `compute_route`, `mesh_dispatch`
- **Training Pipeline:** `train_prepare`, `train_start`, `train_status`, `train_list`, `train_feedback`
- **Context:** `auto_context` (detects project from Cargo.toml, package.json, go.mod, etc.)
- **Model Management:** `sync_models` (auto-discover GGUF files from external drives)
- **And 50+ more** — see [docs/architecture.md](docs/architecture.md) for the full module map

### Autonomous Agents

Trust-gated background agents that run on schedules, file changes, or webhooks:

| Agent | Trust | What it does |
|-------|-------|-------------|
| health-monitor | monitor | Pings services, alerts on failures |
| index-maintainer | safe | Keeps RAG indexes up-to-date |
| code-watcher | safe | Reviews recent git diffs |
| research-agent | safe | Web research on demand |
| news-agent | safe | Scrapes news by topic |
| daily-digest | safe | Aggregates daily summary |

Agents at FULL trust level go through an **approval queue** — destructive actions (model swaps, index deletions) require human approval via the dashboard before executing.

### Multi-Device Compute Mesh

Connect multiple machines into a compute mesh. Workers push heartbeats to the hub, with capability-based routing:

- **Primary (GPU):** 27-35B models for heavy inference
- **Secondary devices:** Embeddings, classification, TTS/STT
- **Phone:** Dashboard access via PWA

Set up a new worker in one command:

```bash
./scripts/setup-worker.sh --hub ai-hub:8100 --key YOUR_KEY
```

### Web Dashboard (PWA)

11-tab dashboard: Status, Chat, Search, Media, Config, Agents, Research, Workflows, Training, Notes, Knowledge Graph.

- Full model loading controls (20 params: context, GPU layers, threads, KV cache, flash attention, speculative decoding)
- Hub mode/character switcher on the Status tab
- Compute mesh monitor with heartbeat health
- Training tab: dataset preparation, run monitor, feedback recorder
- Model sync button: one-click discovery of new models from external drives
- Agent approval queue with approve/deny buttons

### Hub Modes

Switch the entire system's behavior with one command:

```
set_mode("development")  # code-focused, low temp, prefers coder models
set_mode("research")     # thorough, medium temp, prefers dense models
set_mode("creative")     # expansive, high temp
set_mode("review")       # strict, analytical
```

## Architecture

```
localforge/
  src/localforge/
    server.py           # MCP server entry point (~105 lines)
    gateway.py          # HTTP gateway (Starlette + uvicorn)
    config.py           # Config state, generation params, backends
    client.py           # httpx client pool, chat, caching, failover
    auth.py             # Bcrypt auth + token-bucket rate limiting
    cache.py            # Response cache with TTL
    paths.py            # Data dir resolution (LOCALFORGE_DATA_DIR)
    exceptions.py       # Error hierarchy
    log.py              # Structured logging (human + JSON)
    tools/              # 112 tools across 21 modules
    agents/             # Autonomous agents + approval queue
    knowledge/          # SQLite knowledge graph + FTS5
    workflows/          # DAG workflow engine
    workers/            # Compute mesh device workers
    dashboard/          # PWA web interface (11 tabs)
  cli/local             # Remote-capable CLI tool
  scripts/              # setup-worker.sh, systemd templates
  examples/             # Config templates
  docs/                 # Documentation
  tests/                # 75 unit tests
```

## Documentation

- [Quick Start](docs/quickstart.md)
- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Multi-Device Setup](docs/multi-device.md)
- [Agent Development](docs/agent-development.md)
- [API Reference](docs/api-reference.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Apache-2.0 — see [LICENSE](LICENSE).
