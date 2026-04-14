# Architecture

## Overview

LocalForge is a modular Python application that bridges local LLM inference with the Model Context Protocol (MCP). It exposes 112 tools for code analysis, RAG search, knowledge management, compute mesh routing, and autonomous agents.

```
Claude Code / IDE / CLI
        |
    [MCP Protocol]          [HTTP + SSE]
        |                       |
   server.py (stdio)     gateway.py (HTTP)
        |                       |
        +-------+-------+------+
                |
          Tool Registry (112 tools)
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
| `server.py` | MCP server entry point. Imports tool modules, registers handlers, runs stdio loop. ~100 lines. |
| `config.py` | Configuration state: backends, generation params, model profiles, modes. Loaded from `config.yaml`. |
| `client.py` | HTTP client pool, `chat()` function with retry/caching/failover/mesh routing, model resolution, session stats. |
| `cache.py` | Hash-based response cache with TTL and LRU eviction. |
| `paths.py` | Resolves all data directories from `LOCALFORGE_DATA_DIR` env var. |
| `exceptions.py` | Exception hierarchy: `LocalForgeError` -> `BackendError`, `ConfigError`, etc. |
| `log.py` | Logging config: human-readable or JSON structured output. |

### Tools (21 modules, 107 handlers)

Each module uses the `@tool_handler` decorator to register tools at import time. No central tool list — the decorator populates a shared registry in `tools/__init__.py`.

| Module | Tools | Purpose |
|--------|-------|---------|
| `context.py` | 8 | set_context, auto_context, check_model, modes, characters |
| `config_tools.py` | 3 | get/set generation params, reload config |
| `chat.py` | 4 | local_chat, multi_turn, text_complete, validated_chat |
| `analysis.py` | 7 | analyze_code, batch_review, file_qa, classify_task |
| `generation.py` | 7 | test stubs, refactor, docs, translate, regex, SQL, structured output |
| `diff.py` | 3 | review_diff, draft_commit_message, diff_explain |
| `infrastructure.py` | 16 | health, swap_model (20 params), benchmark, slot_info, tokens, LoRA, stats, sync_models |
| `parallel.py` | 3 | fan_out, parallel_file_review, quality_sweep |
| `memory.py` | 5 | scratchpad, save/recall/list/delete notes |
| `sessions.py` | 4 | save/load/list/delete conversation sessions |
| `rag.py` | 8 | index_directory, search, rag_query, incremental_index, diff_rag |
| `semantic.py` | 4 | embed_text, semantic_search, hybrid_search, rerank |
| `presets.py` | 7 | logits, preview_prompt, sampling, presets, grammars |
| `orchestration.py` | 5 | auto_route, workflow, pipeline, save/list pipelines |
| `knowledge.py` | 9 | knowledge_base, doc_lookup, KG CRUD + search + timeline |
| `git.py` | 1 | git_context |
| `web.py` | 3 | web_search, web_fetch, deep_research |
| `compute.py` | 3 | compute_status, compute_route, mesh_dispatch |
| `agents_tools.py` | 2 | agent_list, agent_logs |
| `training.py` | 5 | train_prepare, train_start, train_status, train_list, train_feedback |

### Search & Retrieval

Four-signal retrieval with reciprocal rank fusion:

1. **BM25** (built-in, `chunking.py`) — term-frequency keyword search
2. **Dense** (fastembed, `embeddings.py`) — jinaai/jina-embeddings-v2-base-code, 768-dim
3. **SPLADE** (fastembed, `embeddings.py`) — learned sparse vectors for keyword importance
4. **ColBERT** (fastembed, `embeddings.py`) — late-interaction per-token matching

Cross-encoder reranking (Xenova/ms-marco-MiniLM-L-6-v2) re-orders the fused results.

All embedding models run on CPU to avoid competing with GPU inference.

### Gateway (optional, for HTTP/remote access)

| Module | Purpose |
|--------|---------|
| `gateway.py` | Starlette HTTP server wrapping the MCP app. Serves /health, /mcp/, /api/*, /dashboard. |
| `auth.py` | Bearer token middleware with multi-user profile support. |
| `gpu_pool.py` | Auto-discovers backends on Tailscale mesh, health-checks, routes by model type/task type, circuit breakers. Wired into client.py for transparent mesh routing. |
| `dashboard/` | PWA web interface: status, chat, search, photos, agents, notes, knowledge graph. |

### Agents (optional)

| Module | Purpose |
|--------|---------|
| `agents/supervisor.py` | Lifecycle manager: starts agents, manages schedules, file watches, webhooks. |
| `agents/base.py` | Base agent class with trust levels (monitor/safe/full), mesh dispatch, approval gating. |
| `agents/message_bus.py` | In-process pub/sub for agent communication. |
| `agents/task_queue.py` | SQLite-backed priority task queue with retry and batching. |
| `agents/approval.py` | Approval queue for FULL-trust destructive actions. Dashboard approve/deny UI. |
| `agents/*.py` | 6 built-in agents: health, index, code-watcher, research, news, daily-digest. |

### Knowledge Graph

| Module | Purpose |
|--------|---------|
| `knowledge/graph.py` | SQLite + FTS5 full-text search + embedding-based semantic search. Entity types, relations, timeline queries. |

## Data Flow: Tool Call

```
1. Claude Code sends MCP call_tool("local_chat", {prompt: "..."})
2. server.py call_tool() looks up handler in _tool_handlers registry
3. Handler in tools/chat.py calls client.chat()
   - Code-aware tools set task_type context first:
     async with task_type_context("code"):
         result = await chat(prompt)
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

### Task Type Routing

Tools set a task type hint via `task_type_context()` before calling `chat()`.
The GPU pool uses this to route to the best backend:

| Task Type | Tools | Preferred Model |
|-----------|-------|-----------------|
| `code` | analyze_code, review_diff, generate_test_stubs, suggest_refactor, translate_code, file_qa, explain_error, summarize_file | Code-specialized (Qwen3-Coder, Devstral) |
| `vision` | analyze_image | Vision model (Qwen3-VL) |
| `reasoning` | deep_research synthesis | Dense reasoning model (Qwen3.5-27B) |
| `default` | local_chat, all others | Primary model (any) |

Routing is transparent — tools don't need to know about backends. The
`contextvars`-based approach means no signature changes were needed.

## Configuration Resolution

Generation parameters are resolved in layers (later overrides earlier):

```
webui settings.yaml (source of truth for the running model)
  ↓
config.yaml defaults (MCP-specific overrides)
  ↓
config.yaml models.{pattern} (per-model overrides, matched by substring)
  ↓
Hub mode overrides (set_mode("development") → temp=0.3, etc.)
  ↓
Runtime overrides (set_generation_params(temperature=0.5))
```
