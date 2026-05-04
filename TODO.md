# LocalForge — TODO

Last updated: 2026-05-04. Completed items stripped; see git log for history.

---

## Open Items

---

## P2 — Code Quality / Maintainability

- [ ] **Replace bare `except Exception` with specific catches**
  - Down from 176 → 128 specific catches applied; ~161 remain across 36 files
  - Completed: gateway.py, config.py, supervisor.py, base.py, graph.py, config_tools.py, context.py, training.py, routes.py (34 fixed)
  - Remaining: routes.py (31), device_worker.py (18), detect.py (12), telegram_bot.py (8), tools/* (various)
  - Many remaining in device_worker/detect are hardware detection code where broad catches are somewhat justified

- [x] **Deduplicate routes.py by calling MCP tool handlers internally**
  - `api_swap()` now delegates to `_call_tool("swap_model", ...)` — removed ~50 lines of duplicated config resolution
  - `api_sync_models()` now uses `_call_tool("sync_models", ...)` with fallback
  - `api_benchmark()`, `api_lora_load()`, `api_index_create()` already used `_call_tool`

- [ ] **Consolidate `.claude/settings.local.json` bash permissions**
  - 150+ explicit entries, many are one-off commands
  - Consolidate with broader patterns (e.g., `Bash(~/Development/unsloth-env/bin/python:*)`)

---

### P4 — Infrastructure

- [ ] **Add pagination to more API endpoints**
  - Notes: ✓ (page/limit params added)
  - Chat list: ✓ (already had pagination)
  - Still missing: agents, workflows, indexes, KG entities

- [x] **Knowledge graph: paginate semantic search**
  - FTS5 pre-filter: uses text search to find ~200 candidates, then re-ranks by cosine similarity
  - Falls back to brute-force only when FTS returns fewer than max_results candidates
  - 10-100x faster for large graphs (avoids loading all 10k embeddings into memory)

---

### P5 — Features / Polish

**UI / UX**

- [x] **Extend light theme to hardcoded dark colors**
  - KG canvas: done (reads `--bg`, `--border`, `--text`, `--text-dim` via getComputedStyle)
  - Replaced 14 standalone `#c9d1d9` with `var(--text)` in style.css

- [x] **Undo toast for more destructive operations**
  - Notes ✓, KG entity delete ✓ (returns entity+relations, frontend re-adds on undo), research session delete ✓ (abandon/restore pattern)
  - RAG index delete: skipped (directory tree deletion, undo would require temp storage)

- [x] **Export knowledge graph as JSON**
  - `GET /api/kg/export` → `{"entities": [...], "relations": [...]}`
  - `POST /api/kg/import` with `{"entities": [...], "relations": [...], "merge": true}`
  - Merge mode updates existing entities; non-merge clears first

- [x] **Mobile: sidebar swipe gesture**
  - Swipe right from left edge (24px zone) to open sidebar, left to close
  - Uses `touchstart`/`touchend` with 60px threshold, ignores vertical swipes

- [x] **PWA install prompt**
  - Listens for `beforeinstallprompt`, shows dismissible banner after 30s on mobile (<768px)
  - "Install AI Hub" banner with Install/Later buttons, dismissed per session

**Backend / Agents**

- [x] **Persist agent run logs across gateway restarts**
  - `spawn_agent()` now restores `logs`, `last_run`, `last_error`, `last_duration` from state file
  - Was only restoring `data`, `run_count`, `total_duration`

- [x] **Chat message search**
  - `GET /api/chats/search?q=...&limit=20` — scans saved conversations, returns matching messages with snippets
  - File-based search (consistent with existing JSON file storage)

- [x] **Model swap history**
  - Swap events logged to `swap_history.json` with from/to model, timestamp, duration, status
  - `GET /api/swap/history?limit=10` — returns recent swap events
  - Recorded on both success and failure

- [ ] **Health dashboard with uptime history**
  - Store health check results in SQLite: timestamp, service, healthy, latency_ms
  - Status tab: simple uptime bars for each service (last 24h)

---

### P6 — Testing

- [ ] **Smoke tests for all 112 tool handlers**
  - Each handler: minimal valid args → returns a string, no crash
  - Mock the HTTP client (fixture exists in conftest.py)

- [ ] **Integration tests for dashboard routes**
  - Auth middleware (valid key, invalid, rate limit), CRUD (notes, chat, indexes)
  - Use Starlette `TestClient`

- [ ] **Frontend tests**
  - Unit: `escapeHtml()`, `renderMarkdown()`, `showUndoToast()`, `authHeaders()`
  - E2E (Playwright): login flow, chat send/receive, tab switching

- [ ] **Load test for concurrent requests**
  - Config.py thread safety under load, rate limiter correctness under burst
  - Tool: `locust` or `wrk`

---

### Compute Mesh (M-series)

**M1 — Core routing** (most critical)

- [x] **Persist mesh worker registry to SQLite**
  - Table `mesh_nodes` in `mesh.db` with migration system
  - Nodes loaded on GPUPool startup, persisted on each heartbeat
  - Stale entries (>10 min) cleaned on both register and read
  - 4 new tests covering basic registration, stale cleanup, persist/load roundtrip, capacity limits

- [ ] **Hub → worker command channel**
  - Workers poll `GET /api/mesh/commands/{hostname}` on each heartbeat
  - Commands: `swap_model`, `install_capability`, `run_agent`, `shutdown`
  - Unlocks hub-orchestrated model placement and remote agent execution

- [x] **Worker-side model swap endpoint**
  - `LlamaServerManager.swap(path)` method added with auto-rollback on failure
  - `POST /models/activate` endpoint already existed with full swap+rollback logic

- [ ] **Model distribution (serve GGUF from gateway)**
  - `GET /api/models/download/{filename}` — `StreamingResponse` with chunked transfer
  - `GET /api/models/inventory` — list all GGUFs with size, quant, tier

**M2 — Distribution**

- [ ] **Embedding offload to mesh workers**
  - `embeddings.py` lazy-loads fastembed on hub CPU; distribute to workers with `embeddings` capability
  - Fall back to local; wire into `rag.py` and `semantic.py`

- [ ] **Distributed RAG indexing**
  - Hub reads + chunks → fan-out embedding batches to N workers → hub assembles index
  - `mesh_batch_embed` tool already does the fan-out — wire into the indexer

**M3 — Training**

- [ ] **Remote training dispatch**
  - `train_start(dataset="...", target="worker-laptop2")` — hub keeps serving inference
  - Worker runs Unsloth, exports GGUF, pushes back to hub's model directory

**M4 — Dashboard**

- [ ] **Compute Mesh dashboard tab**
  - Device cards: hostname, tier, VRAM/RAM bars, loaded model, heartbeat age, task count
  - Per-device actions: swap model, trigger task, view logs
  - Mesh overview: total VRAM/RAM, task distribution

**M5 — Deployment**

- [ ] **Validate Termux worker deployment end-to-end**
  - Test on actual old Android phone; document minimum Android version, Termux quirks, wake lock

- [ ] **`setup-worker.sh` served from gateway**
  - Move to `localforge/scripts/`, serve at `/static/setup-worker.sh`

- [ ] **Worker self-update mechanism**
  - Version in heartbeat; hub responds with "update available" if mismatch
  - `GET /api/mesh/worker-bundle` returns tarball of worker code

---

### P7 — Ideas & Future Work

- [ ] **Chat search UI** — add search box to Chat tab, wire to existing `/api/chats/search` endpoint
- [ ] **Swap history UI** — show last 5 swaps on Config tab "currently loaded" section (backend done)
- [ ] **Agent observability dashboard** — real-time agent logs, error budgets, task queue depth in Agents tab
- [x] **Cache invalidation on model swap** — response cache cleared when swap_model completes successfully
- [x] **FTS5 query sanitization** — semantic search wraps each word in quotes to prevent FTS5 syntax errors
- [ ] **Per-API-key rate limiting** — support `X-Forwarded-For` and per-key buckets for reverse proxy deployments
- [x] **Agent error budget reset on resume** — error history cleared when manually resuming an error-paused agent
- [ ] **WebSocket mesh status** — replace 15s polling with real-time push for mesh worker status
- [ ] **Model preloading hints** — if hub knows a swap is coming, pre-warm the model on idle workers
- [ ] **Distributed KG sync** — replicate knowledge graph entities across mesh nodes for shared context

---

## Architecture Notes

### Codebase Stats (May 2026)
- **Python:** 24,030 lines, 71 files, 126 MCP tools, 7 built-in agents
- **Frontend:** 17 JS modules + CSS + HTML + service worker
- **Tests:** 303 tests across 27 files

### Key Files
| File | Purpose |
|------|---------|
| `dashboard/routes.py` | Dashboard API (largest single file, ~2,300 lines) |
| `dashboard/static/js/` | 17 ES modules — main.js, auth.js, chat.js, config.js, etc. |
| `dashboard/static/style.css` | All CSS (~2,700 lines with responsive breakpoints) |
| `tools/infrastructure.py` | Model management, sync, health |
| `tools/rag.py` | RAG indexing and search |
| `tools/training.py` | QLoRA training pipeline |
| `agents/supervisor.py` | Agent supervision, croniter scheduling |
| `knowledge/graph.py` | Knowledge graph (SQLite + FTS5 + embeddings) |
| `config.py` | Config loading, validation, backend management |
| `gpu_pool.py` | Compute mesh routing, circuit breakers, discovery |
| `workers/device_worker.py` | Worker node — runs on secondary devices |

### What's Working Well
- MCP protocol as unified tool interface (112 tools)
- Config merge chain (webui → defaults → model overrides → runtime)
- Auth middleware: bcrypt, rate limiting, timing-safe comparison
- Agent supervisor: trust levels, croniter scheduling, error budgets, approval gates
- 4-signal semantic search (dense + sparse + ColBERT + reranker)
- SQLite WAL mode for concurrent access
- Systemd hardening (PrivateTmp, NoNewPrivileges, ProtectSystem)
- Compute mesh: circuit breakers, model-aware routing, task-type context
- Dashboard PWA: 12 tabs, dark/light theme, keyboard shortcuts, undo toasts

### Completed Highlights (all sessions)
- P0: bcrypt API keys, config 0600, systemd EnvironmentFile, CSP header, XSS fixes
- P1: all 5 bug fixes (chat mutation, response parsing, rate limiter, cache key, cache clear)
- P2: asyncio locks, request body limits, error response format, shared utils, DB path migration
- P3: ES modules, mobile breakpoints, touch targets, accessibility, focus trap, undo toasts, theme toggle, keyboard shortcuts, virtual keyboard, Web Worker for KG graph, SW stale-while-revalidate
- P4: agent timeouts, croniter scheduling, metrics caching, FTS5 rebuild, structured logging + request IDs, config validation
- M0-M1: mesh routing wired into chat(), model-aware routing, registry unification, dispatch retry, worker connection test, compute_test tool
- M5: CPU-only inference fix, memory-pressure rejection, power-aware rejection
- Tests: 121 → 294
- P5 (Apr 27): Ctrl+Enter to send chat, prefers-color-scheme on first load, KG canvas theme-aware colors, export chat as Markdown, TODO condensed 1117→205 lines
- PR #8: filesystem and shell MCP tools with workspace sandbox
- PR #10: ruff lint + format sweep, keyboard-shortcuts overlay fix
- PR #11: strict YAML schema validation tool + workflow template
