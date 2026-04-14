# Quick Start

## Prerequisites

- **Python 3.11+**
- **An OpenAI-compatible backend** running locally. Recommended:
  - [text-generation-webui](https://github.com/oobabooga/text-generation-webui) with `--api` flag
  - [llama.cpp server](https://github.com/ggerganov/llama.cpp) with `--host 0.0.0.0`
  - [Ollama](https://ollama.ai) (set backend URL to `http://localhost:11434/v1`)
  - [vLLM](https://github.com/vllm-project/vllm)
- **A GGUF model** loaded in your backend

## Install

```bash
# Core only (MCP server, no gateway/dashboard)
pip install localforge

# With HTTP gateway + dashboard
pip install "localforge[gateway]"

# With everything (embeddings, search, agents, web)
pip install "localforge[all]"

# Development
pip install -e ".[all,dev]"
```

## Configure

```bash
# Copy example config
cp examples/config.yaml.example ~/.config/localforge/config.yaml

# Generate an API key (for gateway authentication)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Edit config.yaml — at minimum, set:
#   backends.local.url: your backend URL
#   gateway.api_keys: your generated key
```

Or set environment variables:

```bash
export LOCALFORGE_BACKEND_URL="http://localhost:5000/v1"
export LOCALFORGE_API_KEY="your-key-here"
```

## Run

### As MCP Server (for Claude Code, IDEs)

```bash
# stdio mode — Claude Code connects to this
localforge
# or: python -m localforge.server
```

Add to Claude Code's MCP settings (`~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "local-model": {
      "command": "localforge",
      "env": {}
    }
  }
}
```

Or for HTTP mode (remote/multi-device):

```json
{
  "mcpServers": {
    "local-model": {
      "url": "http://localhost:8100/mcp/",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

### As HTTP Gateway (for dashboard, CLI, remote access)

```bash
localforge-gateway --port 8100
# or: python -m localforge.gateway --port 8100
```

Then open `http://localhost:8100` for the web dashboard.

## Verify

```bash
# Check that tools are registered
curl http://localhost:8100/health

# Or use the CLI
local health
```

## Next Steps

- [Configuration Guide](configuration.md) — model profiles, modes, characters
- [Multi-Device Setup](multi-device.md) — connect multiple machines
- [Architecture](architecture.md) — how LocalForge works internally
