# Architecture

## Overview

LocalForge is a modular Python application that bridges local LLM
inference with the Model Context Protocol (MCP). It exposes a focused
tool surface for code analysis, web search, model management, and
compute mesh routing.

The 2026-05-12 cleanup retired the knowledge graph, workflow engine,
RAG/semantic stack, training subsystem, media handling, and several
agents. The system is being trimmed toward a skeleton-minimal target —
see `~/Development/NEW-OS-PLAN.md`.

```
Claude Code / IDE / CLI
        |
    [MCP Protocol]          [HTTP + SSE]
        |                       |
   server.py (stdio)     gateway.py (HTTP)
        |                       |
        +-------+-------+------+
                |
          Tool Registry
          tools/__init__.py
                |
    +-----------+-----------+
    |           |           |
 config.py  client.py  paths.py
    |           |
    |     [httpx pool]
    |           |
    |     gpu_pool.py ← route_request(task_type)
    |           |
    +-----[Backends]----+----[Mesh Workers]
          (OpenAI API)       (device_worker)
```

## Module Map

### Core (always required)

| Module | Purpose |
|--------|---------|
| `server.py` | MCP server entry point. Imports tool modules, registers handlers, runs stdio loop. |
| `config.py` | Configuration state: backends, generation params, model profiles, modes. Loaded from `config.yaml`. |
| `client.py` | HTTP client pool, `chat()` function with retry/caching/failover/mesh routing, model resolution, session stats. |
| `cache.py` | Hash-based response cache with TTL and LRU eviction. |
| `paths.py` | Resolves all data directories from `LOCALFORGE_DATA_DIR` env var. |
| `exceptions.py` | Exception hierarchy: `LocalForgeError` -> `BackendError`, `ConfigError`, etc. |
| `log.py` | Logging config: human-readable or JSON structured output. |
| `migrations.py` | SQLite migration runner for mesh/agent state. |

### Tools

Each module uses the `@tool_handler` decorator to register tools at
import time. No central tool list — the decorator populates a shared
registry in `tools/__init__.py`.

| Module | Purpose |
|--------|---------|
| `context.py` | set_context, auto_context, check_model, modes, characters |
| `config_tools.py` | get/set generation params, reload config |
| `chat.py` | local_chat, multi_turn, text_complete, validated_chat |
| `infrastructure.py` | health, swap_model, benchmark, slot_info, tokens, LoRA, stats, sync_models |
| `memory.py` | scratchpad, save/recall/list/delete notes |
| `sessions.py` | save/load/list/delete conversation sessions |
| `git.py` | git_context |
| `web.py` | web_search, web_fetch, deep_research |
| `filesystem.py` | sandboxed fs_read/write/edit/list/glob/grep |
| `shell.py` | sandboxed shell_exec |
| `agents_tools.py` | agent_list, agent_logs |

### Gateway (optional, for HTTP/remote access)

| Module | Purpose |
|--------|---------|
| `gateway.py` | Starlette HTTP server wrapping the MCP app. Serves `/health`, `/mcp/`, `/api/*`, `/dashboard`. |
| `auth.py` | Bearer token middleware with multi-user profile support. |
| `gpu_pool.py` | Auto-discovers backends on Tailscale mesh, health-checks, routes by model type/task type, circuit breakers. Wired into `client.py` for transparent mesh routing. |
| `enrollment.py` | Worker enrollment tokens + install-script generation. |
| `dashboard/` | PWA web interface (5 tabs: Status, Mesh, Config, Agents, Notes). |

### Agents (optional)

| Module | Purpose |
|--------|---------|
| `agents/supervisor.py` | Lifecycle manager: starts agents, manages schedules, file watches, webhooks. |
| `agents/base.py` | Base agent class with trust levels (monitor/safe/full), mesh dispatch, approval gating. |
| `agents/message_bus.py` | In-process pub/sub for agent communication. |
| `agents/task_queue.py` | SQLite-backed priority task queue with retry and batching. |
| `agents/approval.py` | Approval queue for FULL-trust destructive actions. |
| `agents/health_monitor.py` | Pings services on schedule, alerts on failure. |
| `agents/index_maintainer.py` | Reserved for future RAG re-integration (disabled). |

### Workers (mesh)

| Module | Purpose |
|--------|---------|
| `workers/device_worker.py` | Standalone HTTP server that runs on secondary machines. Handles enrollment, heartbeats, inference, model management. |
| `workers/detect.py` | Hardware/capability detection. |

## Data Flow: Tool Call

```
1. Claude Code sends MCP call_tool("local_chat", {prompt: "..."})
2. server.py call_tool() looks up handler in _tool_handlers registry
3. Handler in tools/chat.py calls client.chat()
4. client.chat():
   a. Check response cache → return if hit
   b. Resolve model name if not cached
   c. Build messages array with system prompt + suffix
   d. Merge generation params (webui → config → model → runtime)
   e. Read task_type from contextvars (set by calling tool)
   f. If GPU pool is available, call gpu_pool.route_request(task_type)
      → picks best backend by model type match + load balancing
   g. POST to routed backend /chat/completions
   h. On failure: try fallback backends (config + pool, priority order)
   i. Update circuit breaker state on success/failure
   j. Cache response, update session stats
5. Handler formats result string
6. server.py wraps in TextContent, returns to MCP transport
```

### Task type routing

Tools set a task type hint via `task_type_context()` before calling
`chat()`. The GPU pool uses this to route to the best backend.

| Task type | Preferred model |
|---|---|
| `code` | Code-specialized (Qwen3-Coder, Devstral) |
| `vision` | Vision model (Gemma 4 26B) |
| `reasoning` | Dense reasoning model (Qwen3.6-27B) |
| `default` | Primary model (any) |

Routing is transparent — tools don't need to know about backends. The
`contextvars`-based approach means no signature changes were needed.

## Configuration resolution

Generation parameters are resolved in layers (later overrides earlier):

```
webui settings.yaml (source of truth for the running model — to be
                     replaced by llama-swap config post-reinstall)
  ↓
config.yaml defaults (MCP-specific overrides)
  ↓
config.yaml models.{pattern} (per-model overrides, matched by substring)
  ↓
Hub mode overrides (set_mode("development") → temp=0.3, etc.)
  ↓
Runtime overrides (set_generation_params(temperature=0.5))
```
