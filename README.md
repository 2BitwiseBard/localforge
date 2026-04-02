# LocalForge

Your local AI coding station. MCP server + multi-device compute mesh + autonomous agents.

Bring your own models, keep your data local.

---

## What is this?

LocalForge is a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that turns locally-running language models into a full-featured AI development platform. It connects to any OpenAI-compatible backend (text-generation-webui, llama.cpp, Ollama, vLLM) and exposes **99 tools** for code analysis, RAG search, autonomous agents, knowledge graphs, and more.

```
You / Claude Code / IDE
        |
    [MCP Protocol]
        |
   LocalForge (99 tools)
   ├── Code analysis, review, generation
   ├── RAG with 4-signal retrieval (BM25 + dense + SPLADE + ColBERT)
   ├── Knowledge graph (SQLite + FTS5 + semantic search)
   ├── Autonomous agents (6 built-in, trust-gated)
   ├── Workflow engine (DAG execution)
   ├── Multi-device compute mesh (Tailscale auto-discovery)
   ├── Web dashboard (PWA, 7 tabs)
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
| Code-first tools | No | No | No | **99 tools** |

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

### 99 MCP Tools

- **Code Analysis:** `analyze_code`, `batch_review`, `summarize_file`, `explain_error`, `file_qa`
- **Code Generation:** `suggest_refactor`, `generate_test_stubs`, `draft_docs`, `translate_code`
- **Diff Review:** `review_diff`, `diff_explain`, `draft_commit_message`
- **RAG Search:** `index_directory`, `rag_query`, `semantic_search`, `hybrid_search`
- **Chat:** `local_chat`, `multi_turn_chat`, `validated_chat`
- **Knowledge Graph:** `kg_add`, `kg_relate`, `kg_query`, `kg_context`, `kg_timeline`
- **Parallel Processing:** `fan_out`, `parallel_file_review`, `quality_sweep`
- **Infrastructure:** `health_check`, `swap_model`, `benchmark`, `slot_info`
- **And 60+ more** — see [docs/api-reference.md](docs/api-reference.md)

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

### Multi-Device Mesh

Connect multiple machines running different-sized models. Auto-discovered via Tailscale:

- **Primary (RTX 3080 Ti):** 27-35B models for heavy lifting
- **Secondary laptops:** 7B models for parallel tasks (embeddings, classification)
- **Phone:** Dashboard access via PWA

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
    server.py          # MCP server (99 tools)
    gateway.py         # HTTP gateway (Starlette + uvicorn)
    auth.py            # Bearer token authentication
    gpu_pool.py        # Multi-device routing + Tailscale mesh
    config.yaml        # Your local configuration
    agents/            # Autonomous agent framework
    knowledge/         # SQLite knowledge graph
    workflows/         # DAG workflow engine
    workers/           # Compute mesh device workers
    dashboard/         # PWA web interface
    media/             # Video/image processing
  cli/local            # Remote-capable CLI tool
  examples/            # Config templates, systemd units
  docs/                # Documentation
  tests/               # Test suite
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
