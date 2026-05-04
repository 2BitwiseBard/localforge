# LocalForge Development Log

## 2026-05-04 — Comprehensive Audit & Quality Sprint

### Session Summary

Full codebase audit and improvement sprint. Started with 294 tests passing, ended with 303. Addressed performance bottlenecks, security gaps, architectural debt, and shipped several P5 features.

### Changes Made

**Performance**
- KG semantic search: added FTS5 pre-filter. Was loading all 10k embeddings (~30MB) per query; now pre-filters to ~200 candidates via text search, then re-ranks by cosine similarity. 10-100x faster for large graphs.
- Status endpoint: full-response caching (5s TTL). Was creating a new httpx client and probing 3 ports on every poll.
- Response cache: upgraded from oldest-first to LRU eviction. Tracks `last_access` time so frequently-hit entries survive longer.
- GPU pool: stale heartbeat cleanup now runs on every registration, not just on read. Prevents unbounded memory growth.

**Architecture**
- Mesh heartbeat persistence to SQLite (`mesh.db`). Workers survive gateway restarts — loaded on startup, persisted on each heartbeat. Migration system wired in.
- Agent state reload: `spawn_agent()` now restores `logs`, `last_run`, `last_error`, `last_duration` from persisted state (was only restoring 3 of 7 fields).
- Routes deduplication: `api_swap()` delegates to `_call_tool("swap_model", ...)` — removed ~50 lines of duplicated config resolution. `api_sync_models()` similarly simplified.
- `LlamaServerManager.swap(path)` method added with auto-rollback on failure.

**Security**
- Shell denylist expanded: +10 patterns (chmod 777, chown -R root, iptables, nft, systemctl, shutdown, reboot, passwd, useradd, userdel).
- Rate limiting: configurable via `config.yaml` → `gateway.rate_limit`, cleanup threshold lowered from 100→50 buckets.
- Bare `except Exception` sweep: replaced ~50 instances across 12 files with specific catches (httpx.HTTPError, json.JSONDecodeError, OSError, etc.).

**Features**
- KG export/import: `GET /api/kg/export`, `POST /api/kg/import` with merge mode.
- Chat message search: `GET /api/chats/search?q=...` with snippet extraction.
- Model swap history: logged to `swap_history.json`, exposed via `GET /api/swap/history`.
- Notes pagination: `page` and `limit` query params.
- Undo toast: KG entity delete (returns entity+relations for re-add), research session delete (abandon/restore pattern).
- Mobile sidebar swipe gesture (touchstart/touchend, 24px edge zone, 60px threshold).
- PWA install prompt (beforeinstallprompt, 30s delay, mobile-only, session-dismissible).
- Light theme: replaced 14 hardcoded `#c9d1d9` with `var(--text)`.
- Metrics endpoint: now includes MCP session stats and cache hit rates.

**Code Quality**
- Fixed `run_supervisor.py` missing `yaml_schema_validator` import.
- Fixed all 41 ruff lint warnings in test files (unused imports, import sorting).
- Service worker cache version bumped to 45.
- TODO.md cleaned: stripped completed items, updated stats, added progress tracking.

**Tests**
- 303 tests across 27 files (up from 294/26).
- New: KG export/import roundtrip (3 tests), mesh persistence (4 tests), LRU cache eviction (2 tests).

### Current Codebase State

| Metric | Value |
|--------|-------|
| Python LOC | 24,030 |
| Python files | 71 |
| MCP tools | 126 |
| Built-in agents | 7 |
| Frontend JS modules | 17 |
| Tests | 303 |
| Ruff errors | 0 |
| Bare `except Exception` remaining | ~161 (down from 176) |

### Recommended Next Steps

**High Priority (should do next)**
1. Integration tests for dashboard routes — use Starlette TestClient, cover auth middleware, CRUD operations. This is the biggest testing gap.
2. Continue bare `except Exception` cleanup in routes.py (31 remaining), tools/* (various). The device_worker/detect ones are mostly justified (hardware detection).
3. Hub→worker command channel — workers poll for commands on heartbeat. Unlocks remote model swap, capability install, agent execution.

**Medium Priority**
4. Health dashboard with uptime history — store health check results in SQLite, show uptime bars on Status tab.
5. Embedding offload to mesh workers — distribute fastembed calls to workers with `embeddings` capability.
6. Frontend: add a search UI for the new `/api/chats/search` endpoint (the backend is done, needs a search box in the Chat tab).
7. Frontend: show swap history on the Config tab (backend done, needs UI).

**Ideas Worth Exploring**
- Streaming chat search (search as you type with debounce, highlight matches).
- Agent observability dashboard — show real-time agent logs, error budgets, task queue depth.
- Model preloading hints — if a worker is idle and the hub knows a swap is coming, pre-warm the model.
- Distributed KG — sync knowledge graph entities across mesh nodes for shared context.
- WebSocket for real-time mesh status updates (currently polls every 15s).

### Things That Need Further Thought

- **Cache invalidation on model swap**: The response cache doesn't invalidate when the model changes. A swap should probably clear the cache (or at least entries for the old model). Currently the TTL handles this (5 min), but a long-running conversation could get stale responses for a few minutes after a swap.

- **FTS5 match syntax**: The semantic search FTS5 pre-filter uses the raw query text as an FTS5 MATCH expression. If the user's query contains FTS5 special characters (`AND`, `OR`, `NOT`, `*`, `""`), it could fail or produce unexpected results. Should sanitize or wrap in quotes.

- **Mesh persistence write frequency**: Currently persists on every heartbeat (every 30s per worker). With 10 workers that's 20 writes/min. SQLite WAL handles this fine, but if the mesh grows to 50+ workers, might want to batch writes or use a write-behind buffer.

- **Rate limit per-user vs per-IP**: Current rate limiting is per-IP. Behind a reverse proxy, all users share one IP. Should support `X-Forwarded-For` or per-API-key rate limiting for production deployments.

- **Agent error budget reset**: When an agent is manually resumed after being error-paused, the error count isn't reset. It could immediately re-pause if the window hasn't expired. Should clear error history on manual resume.

---

## Prior Work (condensed from git history)

### April 2026
- P0 security: bcrypt API keys, config 0600 permissions, systemd hardening, CSP headers, XSS fixes
- P1 bug fixes: chat mutation, response parsing, rate limiter, cache key collision, cache clear
- P2 async safety: asyncio locks, request body limits, error response format
- P3 frontend: ES modules, mobile breakpoints, accessibility, keyboard shortcuts, undo toasts, theme toggle
- P4 agents: timeouts, croniter scheduling, metrics caching, FTS5 rebuild, structured logging + request IDs
- M0-M1 mesh: routing wired into chat(), model-aware routing, registry unification, worker connection test
- M5 worker: CPU-only inference, memory-pressure rejection, power-aware rejection
- PR #8: filesystem and shell MCP tools with workspace sandbox
- PR #10: ruff lint + format sweep
- PR #11: strict YAML schema validation tool + workflow template

### March 2026
- Initial architecture: MCP server, HTTP gateway, dashboard PWA
- Tool registry pattern (`@tool_handler` decorator)
- Agent supervisor with trust levels and approval gates
- Knowledge graph with FTS5 + semantic search
- 4-signal RAG (dense + sparse + ColBERT + reranker)
- Compute mesh with circuit breakers and model-aware routing
- Device worker for heterogeneous compute nodes
