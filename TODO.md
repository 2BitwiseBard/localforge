# LocalForge — TODO

Last updated: 2026-05-04. Completed items stripped; see git log for history.

---

## Open Items

---

## P2 — Code Quality / Maintainability

- [ ] **Replace bare `except Exception` with specific catches**
  - 176 instances across 37 files (51 in routes.py, many in supervisor.py, gateway.py)
  - Target: `httpx.TimeoutException`, `json.JSONDecodeError`, `KeyError`, `OSError`
  - Let unexpected exceptions reach the top-level handler for proper logging

- [ ] **Deduplicate routes.py by calling MCP tool handlers internally**
  - `api_swap()`, `api_sync_models()`, `api_benchmark()`, `api_lora_load()`, `api_index_create()` all re-wrap tool handlers
  - Create shared internal dispatch: `await call_tool(name, args) -> str`

- [ ] **Consolidate `.claude/settings.local.json` bash permissions**
  - 150+ explicit entries, many are one-off commands
  - Consolidate with broader patterns (e.g., `Bash(~/Development/unsloth-env/bin/python:*)`)

---

### P4 — Infrastructure

- [ ] **Add pagination to more API endpoints**
  - Only `api_chat_list` has pagination; add to: notes, agents, workflows, indexes, KG entities

- [ ] **Knowledge graph: paginate semantic search**
  - Loads ALL embeddings into memory for cosine similarity — pre-filter with FTS5, then limit results

---

### P5 — Features / Polish

**UI / UX**

- [ ] **Extend light theme to hardcoded dark colors** (partial ✓)
  - KG canvas: done (reads `--bg`, `--border`, `--text`, `--text-dim` via getComputedStyle)
  - Remaining: `color: #c9d1d9` / `background: var(--bg)` inline styles in training log, code blocks

- [ ] **Undo toast for more destructive operations**
  - Notes ✓ (done). Still missing: KG entity delete, RAG index delete, research session delete
  - Same pattern: return content in DELETE response, `showUndoToast()` in the handler

- [ ] **Export knowledge graph as JSON**
  - `GET /api/kg/export` → `{"entities": [...], "relations": [...]}`
  - Import endpoint: `POST /api/kg/import` for migration/backup

- [ ] **Mobile: sidebar swipe gesture**
  - Swipe right from left edge to open sidebar, left to close
  - Use `touchstart`/`touchmove`/`touchend` with a 20px start zone

- [ ] **PWA install prompt**
  - Listen for `beforeinstallprompt`, show a dismissible banner after 30s of use
  - "Install AI Hub" banner at bottom of page on mobile

**Backend / Agents**

- [ ] **Persist agent run logs across gateway restarts**
  - `agent.state.logs` is in-memory only; cleared on each restart
  - Write logs to `agent_state/<id>.json` (already done for other state) — already there, just not re-loaded into the in-memory log list on spawn

- [ ] **Chat message search**
  - Full-text search across saved conversations
  - FTS5 index on chat history SQLite table; expose via `/api/chats/search`

- [ ] **Model swap history**
  - Log swap events (from, to, timestamp, duration) to a small SQLite table
  - Show last 5 swaps on the Config tab "currently loaded" section

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

- [ ] **Persist mesh worker registry to SQLite**
  - Workers re-register on next heartbeat (30s window), but mesh appears empty after gateway restart
  - Table: `mesh_nodes(key, hostname, port, tier, capabilities_json, model_name, last_heartbeat, healthy)`

- [ ] **Hub → worker command channel**
  - Workers poll `GET /api/mesh/commands/{hostname}` on each heartbeat
  - Commands: `swap_model`, `install_capability`, `run_agent`, `shutdown`
  - Unlocks hub-orchestrated model placement and remote agent execution

- [ ] **Worker-side model swap endpoint**
  - `POST /model/swap` on the worker: stops llama-server, loads new GGUF, restarts
  - `LlamaServerManager` already has `stop()` and `start()` — add `swap(path)`

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

## Architecture Notes

### Codebase Stats (May 2026)
- **Python:** 23,538 lines, 71 files, 126 MCP tools, 7 built-in agents
- **Frontend:** 17 JS modules + CSS + HTML + service worker
- **Tests:** 294 tests across 26 files

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
