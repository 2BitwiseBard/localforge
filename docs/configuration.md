# Configuration

LocalForge reads configuration from `config.yaml`. The search order is:

1. `$LOCALFORGE_CONFIG` (env var, if set)
2. `~/.config/localforge/config.yaml`
3. `./config.yaml` (relative to working directory)

See `examples/config.yaml.example` for a fully documented template.

## Environment Variables

These override config.yaml values:

| Variable | Purpose | Default |
|----------|---------|---------|
| `LOCALFORGE_CONFIG` | Config file path | (search order above) |
| `LOCALFORGE_DATA_DIR` | Data directory (notes, indexes, sessions) | `~/.local/share/localforge/` |
| `LOCALFORGE_BACKEND_URL` | Primary backend URL | `http://localhost:5000/v1` |
| `LOCALFORGE_API_KEY` | Gateway API key | (none) |
| `LOCALFORGE_GATEWAY_HOST` | Gateway bind host | `0.0.0.0` |
| `LOCALFORGE_GATEWAY_PORT` | Gateway bind port | `8100` |
| `LOCALFORGE_WEBUI_ROOT` | text-generation-webui installation path | (auto-detected) |

## Backend Configuration

```yaml
backends:
  local:
    url: http://localhost:5000/v1
    priority: 1
  remote:
    url: http://second-laptop:5000/v1
    priority: 2
    optional: true
```

Multiple backends enable automatic failover. The server tries backends in priority order and marks unhealthy ones for retry later.

## Generation Parameters

Parameters are resolved in layers:

1. **webui settings.yaml** — whatever your text-gen-webui has configured
2. **config.yaml `defaults`** — MCP-specific overrides
3. **config.yaml `models.{pattern}`** — per-model overrides (substring match)
4. **Hub mode** — applied when you call `set_mode("development")`
5. **Runtime** — applied when you call `set_generation_params(temperature=0.5)`

```yaml
defaults:
  max_tokens: 4096
  enable_thinking: false
  system_suffix: "Be concise and direct."

models:
  Qwen3-Coder:
    max_tokens: 8192
    system_suffix: "Output code directly."
  Qwen3-VL:
    ctx_size: 16384
```

## Hub Modes

Modes change the system's behavior globally — temperature, system suffix, preferred model:

```yaml
modes:
  development:
    temperature: 0.3
    prefer_model: ["Qwen3-Coder", "Devstral"]
    system_suffix: "Output code directly."
    max_tokens: 8192
    auto_swap: true
  research:
    temperature: 0.5
    prefer_model: ["Qwen3.5-27B"]
    system_suffix: "Be thorough. Cite sources."
```

Activate with `set_mode("development")`.

## Characters

Characters add persona-specific system prompts:

```yaml
characters:
  code-reviewer:
    name: "Code Reviewer"
    system_prompt: "You are a senior code reviewer..."
    temperature_override: 0.2
```

Activate with `set_character("code-reviewer")`.

## Multi-User Profiles

Each API key maps to a user profile with isolated data:

```yaml
users:
  admin:
    name: "Admin"
    api_key: "${LOCALFORGE_API_KEY}"
    role: admin
    default_mode: development
```

## Model Loading Parameters

When swapping models (via `swap_model` tool or dashboard), you can specify loading parameters:

| Parameter | Description | Example |
|-----------|-------------|---------|
| `ctx_size` | Context window size (tokens) | `32768` |
| `gpu_layers` | GPU layers (-1 = all) | `-1` |
| `threads` | CPU threads | `8` |
| `threads_batch` | Batch processing threads | `8` |
| `batch_size` | Batch size | `512` |
| `ubatch_size` | Micro-batch size | `512` |
| `cache_type` | KV cache quantization | `fp16`, `q8_0`, `q4_0` |
| `flash_attn` | Flash attention | `true` |
| `rope_freq_base` | RoPE frequency base | `10000` |
| `tensor_split` | Multi-GPU tensor split | `"0.7,0.3"` |
| `parallel` | Parallel inference slots | `2` |
| `model_draft` | Speculative decoding draft model | `"Qwen3.5-2B.gguf"` |
| `draft_max` | Max draft tokens | `16` |
| `spec_type` | Speculation type | `"draft"`, `"ngram"` |

These can be set per-model in config.yaml under the `models` section, or overridden at swap time.

## Compute Mesh

```yaml
compute_pool:
  discovery_interval: 60
  health_check_interval: 30
  auto_discover: true
  worker_port: 8200
  task_routing:
    inference: {prefer_tier: [gpu-primary, gpu-secondary], min_vram: 4000}
    embeddings: {prefer_tier: [cpu-capable, lightweight]}
    tts: {prefer_tier: [cpu-capable, lightweight]}
```

See [Multi-Device Setup](multi-device.md) for adding workers to the mesh.

## Agent Configuration

See `examples/agents.yaml.example` for agent setup (schedules, trust levels, triggers).

## Filesystem & Shell Tools

The `fs_*` and `shell_exec` tools let agents (and any MCP client of the gateway) read, edit, and run commands inside an allowlisted set of directories.

```yaml
# Directories that fs_* and shell_exec are allowed to touch.
# Defaults to ["~/Development"] when omitted. An explicitly-empty list
# disables these tools.
tool_workspaces:
  - ~/Development
  - ~/scratch

# Extra regex patterns that block shell_exec before approval is even
# requested. These extend the built-in defaults (sudo, rm -rf /, curl|bash,
# fork bomb, dd to a block device, mkfs, etc.). Patterns are passed to
# re.search against the raw command string.
shell_deny:
  - "\\bnpm\\s+publish\\b"
  - "\\bgit\\s+push\\s+.*--force\\b"
```

**Security model:**

- **Workspace sandbox** — every path is resolved with `os.path.realpath` (collapses `..` AND symlinks) and must live inside a configured root. `..` traversal and symlink escapes both fail.
- **Trust gating** — agents at SAFE trust can call `fs_read` / `fs_list` / `fs_glob` / `fs_grep`. `fs_write` / `fs_edit` / `fs_delete` / `shell_exec` are FULL-trust only and route through the approval queue (`agents/approval.py`).
- **Shell denylist** — matches happen *before* any approval prompt; a banned pattern can never reach the human reviewer or the shell.
- **Output caps** — `fs_read` caps at 2000 lines / 256 KiB; `shell_exec` truncates combined stdout+stderr at 4000 chars; `shell_exec` defaults to a 30 s timeout (max 300 s).

**Caveat:** the approval gate runs inside `BaseAgent.call_tool`. CLIs and external MCP clients (e.g. `cli/local`) bypass it and can call destructive tools directly — keep workspace roots tight and the operator's API key secret. A gateway-level approval gate is tracked as future work.
