# LocalForge — TODO / Improvement Plan

Generated from a thorough codebase review (April 2026).
Organized by priority tier. Check items off as completed.

---

## P0 — Security (Do These First)

- [x] **Hash API keys with bcrypt in config.yaml**
  - auth.py already supports `$2b$` prefix — just hash the keys
  - `python3 -c "import bcrypt; print(bcrypt.hashpw(b'YOUR_KEY', bcrypt.gensalt()).decode())"`
  - Replace plaintext keys in `config.yaml` users section with hashed versions
  - Done: config.yaml users.tyler.api_key uses $2b$12$ bcrypt hash (April 2026)

- [x] **Restrict config.yaml permissions to 0600**
  - Currently `664` (world-readable) — contains API keys
  - Done: `stat -c "%a"` confirms 600 (April 2026)

- [x] **Move API keys out of systemd Environment= directives**
  - Use `EnvironmentFile=%h/.config/environment.d/local-ai.conf` instead
  - Keys in Environment= are visible via `/proc/<pid>/environ` and `journalctl`
  - Done: mcp-gateway.service uses EnvironmentFile, no plaintext keys in Environment= (April 2026)

- [x] **Use `hmac.compare_digest()` for plaintext key comparison**
  - `auth.py:111` — `return provided == stored` is vulnerable to timing attacks
  - One-line fix: `return hmac.compare_digest(provided, stored)`

- [x] **Audit all innerHTML assignments in app.js (105 total)**
  - Replace with `textContent` or DOM methods where content is untrusted
  - Ensure model responses always go through `escapeHtml()` before rendering
  - High-risk paths: chat messages, agent logs, KG entity content, note content
  - renderMarkdown() confirmed safe (calls escapeHtml first). Notes, agents: safe.
  - Fixed: knowledge.js entity type badges, relation types, error messages (April 2026)

- [x] **Add CSP (Content-Security-Policy) header to gateway**
  - Block inline scripts, restrict sources to self
  - Prevents XSS exploitation even if innerHTML slips through

- [x] **Scrub API key from `.claude/settings.local.json` bash permissions**
  - `Bash(export LOCAL_AI_KEY=\"fI2F6k5Tn-...\"` is in the permissions allow list
  - If the repo is ever pushed, the key is exposed in plaintext
  - Remove the specific export permission; the env var is already set via `environment.d`

---

## P1 — Bug Fixes

- [x] **Fix chat.py list mutation during iteration**
  - `messages.pop(i)` inside `for i, m in enumerate(messages)` skips elements
  - Fix: build a new list with list comprehension, or iterate in reverse
  - File: `src/localforge/tools/chat.py` (conversation trimming logic)

- [x] **Validate client.py response structure before key access**
  - `resp.json()["choices"][0]["message"]["content"]` crashes if API returns error
  - Add a helper: `_extract_content(resp) -> str` with proper KeyError/IndexError handling
  - File: `src/localforge/client.py`

- [x] **Fix rate limiter cleanup threshold**
  - `auth.py:61` — cleanup only triggers at >1000 entries, leaks memory until then
  - Add periodic cleanup independent of request volume (or lower threshold to 100)

- [x] **Fix cache key ordering**
  - `cache.py` — `sorted(kwargs.items())` is used but two dicts with same content
    could serialize differently if values contain nested dicts
  - Use `json.dumps(kwargs, sort_keys=True)` for deterministic keys

- [x] **Clear cache on config reload**
  - When `reload_config()` changes generation params, cached responses are stale
  - Add `response_cache.clear()` in reload path

---

## P2 — Code Quality / Maintainability

- [ ] **Replace bare `except Exception` with specific catches**
  - 176 instances across 37 files (51 in routes.py alone)
  - Priority files: `routes.py`, `supervisor.py`, `gateway.py`, `workers/`
  - Catch: `httpx.TimeoutException`, `json.JSONDecodeError`, `KeyError`, `OSError`
  - Let unexpected exceptions propagate to top-level handler

- [x] **Add `asyncio.Lock` to config.py global state mutations**
  - `_config`, `_webui_settings`, `_runtime_overrides` are modified without locks
  - Wrap `reload_config()` and `set_generation_params()` with a module-level lock
  - File: `src/localforge/config.py`
  - Added `_config_lock`, `reload_config_safe()`, and `set_runtime_overrides_safe()`

- [ ] **Deduplicate routes.py by calling MCP tool handlers internally**
  - `api_swap()` reimplements model config resolution from `tools/infrastructure.py`
  - `api_sync_models()` reimplements source path resolution
  - `api_benchmark()`, `api_lora_load()`, `api_index_create()` all re-wrap tool handlers
  - Create shared internal dispatch: `await call_tool(name, args)` -> string result

- [x] **Extract shared utilities**
  - Path validation duplicated 4+ ways across tools (analysis.py, parallel.py, rag.py)
  - System message building duplicated across chat.py, parallel.py, analysis.py
  - Create: `utils.py` with `validate_file_path()`, `build_system_message()`

- [x] **Add tool name collision detection in `tools/__init__.py`**
  - If two modules register the same name, second silently overwrites first
  - Add warning/error on duplicate registration

- [x] **Bound in-memory conversation storage in chat.py**
  - `_conversations` dict grows unbounded — add LRU eviction (max 50 conversations)

- [x] **Add request body size limits to all routes**
  - Only `api_chat()` has `MAX_CHAT_BODY_BYTES` check
  - Add middleware or per-route limit (e.g., 1MB default)
  - Added `RequestBodyLimitMiddleware` to gateway.py (1MB default, 50MB for uploads)

- [x] **Standardize API error responses**
  - Some routes return `{"error": "msg"}`, others `{"status": "error", "error": "msg"}`
  - Pick one format, create `_error_response(msg, status=500)` helper

- [x] **Move SQLite databases out of source tree**
  - `approval_queue.db`, `knowledge.db`, `message_bus.db`, `task_queue.db` live in `src/localforge/`
  - These are runtime data — should be created in `LOCALFORGE_DATA_DIR` (paths.py supports this)
  - Add `*.db`, `*.db-shm`, `*.db-wal` to localforge `.gitignore`

- [x] **Add mock HTTP client fixture to test infrastructure**
  - `conftest.py` is just a sys.path hack — no shared fixtures
  - Create a mock `httpx.AsyncClient` fixture for testing tool handlers without a running backend
  - Blocks confident refactoring of tool modules — bump from P6 to P2

- [ ] **Consolidate `.claude/settings.local.json` bash permissions**
  - 150+ explicit entries, many are one-off commands (unsloth verification one-liners)
  - Consolidate with broader patterns where safe (e.g., `Bash(~/Development/unsloth-env/bin/python:*)`)
  - Reduces maintenance burden and settings file bloat

- [x] **Add `.opencode/` to root `.gitignore`**
  - Contains OpenCode runtime SQLite state — not source code
  - Currently not gitignored

- [x] **Remove empty `agent_state/` directory from source tree**
  - `src/localforge/agent_state/` is empty — either populate or remove
  - If it's meant for runtime state, create it dynamically in the data directory

- [x] **Fix documentation drift across AI-HUB.md, CLAUDE.md, README.md**
  - Tool count: unified to 112 (actual count from `@tool_handler` registrations)
  - Vision models: AI-HUB.md still lists Qwen3-VL, CLAUDE.md lists Gemma4 — update AI-HUB.md when model inventory is confirmed
  - All docs, CONTRIBUTING.md, and architecture.md updated

- [x] **Add Kiro steering files for platform_next**
  - `platform_next/.kiro/` only has a specs dir with one testing plan
  - Add steering files with Rust coding standards from CLAUDE.md
  - Ensures Kiro enforces the same rules as Claude Code
  - Created `platform_next/.kiro/steering/rust-standards.md` with auto-inclusion

---

## P3 — Frontend Improvements

### Code Organization
- [x] **Split app.js into ES modules**
  - Suggested modules: `auth.js`, `status.js`, `chat.js`, `photos.js`, `kg.js`,
    `agents.js`, `config.js`, `training.js`, `utils.js`
  - Use `<script type="module">` — no build step needed
  - Each module manages its own state and cleanup
  - Completed: 17 ES modules in dashboard/static/js/ (April 2026)

- [x] **Add cleanup pattern for tab switches**
  - Clear intervals, revoke blob URLs, remove event listeners when leaving a tab
  - Prevents memory leaks from accumulated timers and blob URLs
  - Audited: no per-tab intervals exist; all intervals in main.js are global (April 2026)

- [x] **Revoke blob URLs in `_blobCache`**
  - `URL.revokeObjectURL()` never called — memory leak after many photo loads
  - Add eviction when cache exceeds threshold (e.g., 50 entries)
  - Added LRU eviction at 50 entries with `URL.revokeObjectURL()` on evict

### Mobile / Responsive
- [x] **Add mobile breakpoints to CSS**
  - `@media (max-width: 768px)` — reduce header/nav height, stack layouts
  - `@media (max-width: 480px)` — single column, larger touch targets (44px min)
  - Reduce sticky header + nav vertical space on small screens
  - Added responsive breakpoints at 768px and 480px

- [x] **Make touch targets 44px minimum**
  - Current buttons are 30-35px — below Apple/Google recommended minimum
  - Affects: nav tabs, icon buttons, chat input buttons, photo cards
  - Added `@media (pointer: coarse)` rule enforcing 44px min on all interactive elements

- [ ] **Handle virtual keyboard on mobile**
  - Chat input `max-height: calc(100dvh - 140px)` breaks when keyboard opens
  - Use `visualViewport` API to adjust layout

### Accessibility
- [x] **Add focus styles to all interactive elements**
  - Many buttons/links have `:hover` but no `:focus` or `:focus-visible`
  - Keyboard users can't see what's selected
  - Added `:focus-visible` outlines for tabs, buttons, inputs, selects, links

- [x] **Fix color contrast (WCAG AA)**
  - `--text-dim: #8b949e` on `--bg: #0d1117` = 3.5:1 ratio (needs 4.5:1)
  - Darken to `#8b949e` -> `#9da5ae` or lighten background
  - Updated `--text-dim` to `#9da5ae` (~4.6:1 contrast ratio)

- [x] **Add `aria-label` to emoji/icon buttons**
  - Buttons with emoji text (magnifying glass, camera, mic) need labels
  - Screen readers just announce "button" without them
  - Added `aria-label` to all icon buttons in index.html

- [x] **Add `prefers-reduced-motion` media query**
  - Pulse animation and transitions should respect user preference
  - `@media (prefers-reduced-motion: reduce) { * { animation: none !important; } }`
  - Added to style.css

- [x] **Add focus trap to modal dialogs**
  - Chat history panel, KG graph overlay — tab focus can escape the modal
  - Add `role="dialog"`, `aria-modal="true"`, focus trap JS
  - Done: auth modal traps Tab between key input and submit button (April 2026)

- [x] **Add supplementary icons to color-only status indicators**
  - Connection dot, status badges use only red/green — fails for colorblind users
  - Add checkmark/X icon alongside color
  - Added `::after` pseudo-elements with ✓/✗ to `.conn-dot` states

### UX Polish
- [x] **Debounce search inputs**
  - Every keypress fires an API request — add 300-500ms debounce
  - Affects: RAG search, photo search, KG search
  - Verified: search inputs already use Enter/click (not input events). Added `debounce()` utility for future use.

- [ ] **Move force-directed graph to a Web Worker**
  - O(n^2) simulation freezes UI above ~30 nodes
  - Alternative: use d3-force or limit visible nodes with pagination

- [ ] **Add undo for destructive operations**
  - Delete chat, delete index, delete note — immediate with no recovery
  - At minimum: confirmation dialog. Better: soft-delete with 30s undo toast

- [x] **Service worker: stale-while-revalidate for read-only endpoints**
  - `/api/status`, `/api/models`, `/api/agents` — cache last response
  - Show cached data immediately, update when network responds
  - Makes PWA useful when backend is briefly down
  - Implemented in sw.js v39: /api/status, /api/models, /api/agents, /api/mesh/status (April 2026)

---

## P4 — Infrastructure / Operations

- [x] **Add `asyncio.wait_for()` timeout to agent.execute()**
  - No timeout wrapping — a hung agent blocks the schedule loop forever
  - Add per-agent configurable timeout (default 1 hour)
  - File: `src/localforge/agents/supervisor.py`
  - Added to both `_run_once()` and `_schedule_inner()`, configurable per-agent via `timeout` key

- [x] **Replace hand-rolled cron parser with `croniter`**
  - Current parser doesn't support weekday expressions, ranges, or lists
  - Falls back to 3600s (1 hour) on parse failure — silent degradation
  - Done: `_schedule_inner` uses croniter for exact next-run times; fallback kept if not installed (April 2026)

- [x] **Add StartLimitInterval to systemd services**
  - Prevents restart spam if service crashes immediately
  - Add to all services: `StartLimitIntervalSec=60`, `StartLimitBurst=3`
  - Added to gateway service example and setup-worker.sh generated service

- [x] **Cache GPU metrics and ps aux status calls**
  - `api_status()` calls `ps aux` + regex on every request
  - `api_metrics()` calls `nvidia-smi` subprocess on every request
  - Add 15-30s TTL cache for both
  - Added `_gpu_metrics_cache` and `_status_cache` with 15s TTL in routes.py

- [ ] **Add structured logging (JSON format)**
  - Current logging is plain text — hard to parse/search/alert on
  - Use `python-json-logger` or manual JSON formatter
  - Add request IDs for tracing through gateway -> tools -> agents

- [ ] **Add pagination to more API endpoints**
  - Only `api_chat_list` has pagination
  - Add to: notes, agents, workflows, indexes, KG entities

- [ ] **Knowledge graph: paginate semantic search**
  - Currently loads ALL embeddings into memory for cosine similarity
  - Add LIMIT/OFFSET or cursor-based pagination
  - Consider: pre-filter by FTS5 before running semantic similarity

- [x] **Add FTS5 rebuild mechanism to knowledge graph**
  - If FTS index gets out of sync (crash during write), no recovery path
  - Add `rebuild_fts_index()` method and expose via MCP tool
  - Added `KnowledgeGraph.rebuild_fts_index()` and `kg_rebuild_fts` MCP tool

- [ ] **Config schema validation**
  - No validation of config.yaml structure — missing keys cause runtime errors
  - Add pydantic model or jsonschema validation on load
  - Fail fast with clear error message on invalid config

- [x] **Add `Restart=on-failure` with backoff to systemd services**
  - Current services may restart indefinitely on persistent failures
  - Add `RestartSec=5s` with `StartLimitIntervalSec=60`, `StartLimitBurst=3`
  - Prevents log spam and resource waste on unrecoverable errors
  - Applied to gateway service example and setup-worker.sh

- [x] **Consolidate `_load_config()` implementations**
  - `auth.py` and `config.py` both have their own `_load_config()` that reads config.yaml
  - auth.py has a 30s TTL cache, config.py reads fresh every time
  - Unify into a single config loader in `config.py` that auth.py imports
  - Added `load_config_cached()` to config.py; auth.py, gateway.py, routes.py all import it

---

## P5 — New Features / Nice-to-Have

- [ ] **Dark/light theme toggle**
  - All CSS is dark-mode only — add `prefers-color-scheme` support
  - Store preference in localStorage, add toggle button in header

- [x] **Keyboard shortcuts for dashboard**
  - `1-9` to switch tabs, `/` to focus search, `Ctrl+Enter` to send chat
  - Add `?` to show shortcut help overlay
  - Done: `/` focuses search, `?` toggles overlay, `=` for Knowledge tab, Esc closes overlays (April 2026)

- [ ] **Export/import for knowledge graph**
  - Backup/restore KG as JSON or SQLite dump
  - Useful for migration, sharing, or disaster recovery

- [ ] **Chat message search**
  - Search across all saved conversations by content
  - FTS5 index on chat history

- [ ] **Model comparison view**
  - Side-by-side response comparison between two models
  - Useful for evaluating model quality during swaps

- [ ] **API docs endpoint**
  - Auto-generate OpenAPI spec from route definitions
  - Serve Swagger UI at `/docs`

- [ ] **Health dashboard with uptime history**
  - Track service uptime over time (last 24h, 7d, 30d)
  - Display as simple uptime bars on status tab

- [ ] **Agent output viewer in dashboard**
  - View full output/logs from agent runs (not just last status)
  - Stream agent output in real-time via SSE

- [ ] **Notification preferences**
  - Per-agent notification toggle (some agents are noisy)
  - Quiet hours configuration

- [ ] **Photo/video gallery improvements**
  - Lightbox view for photos (click to enlarge)
  - Video thumbnail generation
  - Drag-and-drop upload
  - Album/folder organization

---

## P6 — Testing

- [ ] **Smoke tests for all 112 tool handlers**
  - Each handler should accept minimal valid args and return a string
  - Mock the HTTP client to avoid needing a running backend
  - Currently: 121 tests across 16 files — good coverage for core/infra, sparse for tools

- [ ] **Integration tests for dashboard routes**
  - Test auth middleware (valid key, invalid key, expired key, rate limit)
  - Test CRUD operations (chat save/load/delete, notes, indexes)
  - Use Starlette TestClient

- [ ] **Frontend tests**
  - At minimum: test `escapeHtml()`, `renderMarkdown()`, `authHeaders()`
  - Playwright/Cypress for E2E: login flow, chat send/receive, tab switching

- [ ] **Load test for concurrent requests**
  - Verify config.py thread safety under load
  - Verify rate limiter correctness under burst traffic
  - Tool: `locust` or `wrk`

---

## Architecture Notes (For Context)

### Codebase Stats (April 2026)
- **Python:** 20,839 lines across 50+ files
- **Frontend:** 2,041 JS + 1,683 CSS + 579 HTML + 73 SW = 4,376 lines
- **Tests:** 162 tests across 19 files
- **Tools:** 112 MCP tools in 21 tool modules
- **Agents:** 6 built-in autonomous agents

### Key Files
| File | Lines | Purpose |
|------|-------|---------|
| `dashboard/routes.py` | 2,234 | Dashboard API (largest single file) |
| `dashboard/static/app.js` | 2,041 | Frontend JS (second largest) |
| `dashboard/static/style.css` | 1,683 | All CSS |
| `tools/infrastructure.py` | 759 | Model management, sync, health |
| `tools/rag.py` | 701 | RAG indexing and search |
| `tools/training.py` | 691 | QLoRA training pipeline |
| `agents/supervisor.py` | 684 | Agent supervision |
| `knowledge/graph.py` | 579 | Knowledge graph (SQLite + FTS5) |
| `tools/orchestration.py` | 464 | Workflows and pipelines |
| `config.py` | 366 | Configuration system |

### What's Working Well
- MCP protocol as unified tool interface
- Config merge chain (webui -> defaults -> model overrides -> runtime)
- Auth middleware with bcrypt support and rate limiting
- Agent supervisor with trust levels and approval gates
- 4-signal semantic search (dense + sparse + ColBERT + reranker)
- SQLite WAL mode for concurrent access
- Systemd hardening (PrivateTmp, NoNewPrivileges, ProtectSystem)

---

## Session Log

### 2026-04-08 — Kiro Review Session

**Completed:**
- [x] P0: `hmac.compare_digest()` timing attack fix in auth.py
- [x] P0: Scrubbed plaintext API key from `.claude/settings.local.json`
- [x] P0: Fixed same timing attack in `device_worker.py` `_check_worker_auth()`
- [x] P2: Added `asyncio.Lock` to config.py (`reload_config_safe()`, `set_runtime_overrides_safe()`)
- [x] P2: Added `.opencode/` to root `.gitignore`
- [x] P2: Fixed documentation drift — tool count unified to 111 across all docs
- [x] Added 8 new TODO items from general codebase review
- [x] Added comprehensive Compute Mesh readiness plan (M0-M5, deep code-level analysis)

**Next session priorities (suggested order):**

1. **P0 — Remaining security items (outside this workspace, ~30 min):**
   - `chmod 600 ~/.claude/mcp-servers/local-model/config.yaml`
   - Hash API keys with bcrypt in config.yaml
   - Move keys out of systemd `Environment=` → `EnvironmentFile=`
   - Add CSP header to gateway.py

2. **M0 — Mesh security before adding nodes (~30 min):**
   - Verify heartbeat endpoint auth
   - Add `_mesh_workers` size bound
   - Add worker task body size limit

3. **M1 — Core mesh functionality (~multi-session):**
   - Model distribution to workers (serve GGUF from gateway)
   - Worker model swap endpoint
   - Model-aware sticky routing
   - Embedding offload to mesh workers
   - Dashboard compute mesh tab

4. **P1 — Bug fixes (~1 hour):**
   - Fix chat.py list mutation during iteration
   - Add `_extract_content()` helper to client.py
   - Fix rate limiter cleanup threshold (lower from 1000 to 100)
   - Clear cache on config reload

5. **P2 — Test infrastructure (~1 hour):**
   - Create mock httpx client fixture in conftest.py
   - Add tool name collision detection in `tools/__init__.py`
   - Move SQLite databases out of source tree

**Vision model note:** AI-HUB.md and AI-HUB-GUIDE.md still reference Qwen3-VL
for vision. CLAUDE.md references Gemma4. Confirm which models are actually on
disk and update the stale doc accordingly.

---

### 2026-04-08 — Kiro Session #2 (Mesh Routing + Bug Fixes)

**Completed:**
- [x] M1: Wired `gpu_pool.route_request()` into `client.py chat()` — THE critical change
  - Added `contextvars`-based `_task_type_ctx` so tools can set task type without signature changes
  - Added `set_gpu_pool()` / `set_task_type()` API in client.py
  - Gateway lifespan now injects gpu_pool into client.py
  - Fallback logic unified: collects URLs from both config backends and gpu_pool
  - Circuit breaker feedback on success/failure for pool-routed requests
- [x] M1: Worker now probes external backend for model name in heartbeats
  - If no llama-server but `_backend_url` is set, probes `/v1/internal/model/info`
  - Hub can now do model-aware routing for workers using external text-gen-webui
- [x] M0: Added `_mesh_workers` size bound (100 max) + heartbeat body size limit (64KB)
- [x] M0: Added worker task endpoint body size limit (10MB)
- [x] M5: Fixed CPU-only inference — auto-detects `gpu_type == "none"` and sets `gpu_layers=0`
- [x] P1: Fixed chat.py list mutation during iteration (replaced with list comprehension)
- [x] P1: Added `_extract_content()` helper to client.py (handles error payloads, missing keys)
- [x] P1: Lowered rate limiter cleanup threshold from 1000 to 100
- [x] P1: Fixed cache key ordering (json.dumps with sort_keys=True)
- [x] P1: Cache now cleared on config reload via `reload_config_safe()`
- All 121 tests pass

**How mesh routing works now:**
1. Tools optionally call `set_task_type("code")` before `chat()`
2. `chat()` reads `_task_type_ctx` via contextvars
3. If `_gpu_pool` is set, calls `gpu_pool.route_request(task_type)`
4. If pool returns a different URL than primary, routes there
5. On failure, falls back to all known backends (config + pool)
6. Circuit breaker state updated on both success and failure

**Next session priorities:**
1. Have tools set task_type context (code tools → "code", vision → "vision", etc.)
2. M1: Unify discovery + heartbeat into single registry in gpu_pool.py
3. M1: Add model-aware sticky routing (prefer workers with right model loaded)
4. M0: Verify heartbeat endpoint is behind auth middleware (check PUBLIC_PATHS)
5. P2: Create mock httpx client fixture for tool handler testing

### 2026-04-08 — Kiro Session #3 (Task Type Routing + P2 Fixes + Docs)

**Completed:**
- [x] Added `task_type_context()` async context manager to client.py for clean task type hints
- [x] Wired task_type into 12 tool handlers across 4 modules:
  - analysis.py: analyze_code, summarize_file, explain_error, file_qa → "code"
  - analysis.py: analyze_image → "vision"
  - diff.py: review_diff → "code"
  - generation.py: generate_test_stubs, suggest_refactor, translate_code → "code"
  - web.py: deep_research synthesis → "reasoning"
- [x] M0: Verified heartbeat endpoint auth — already behind BearerAuthMiddleware
- [x] P2: Added tool name collision detection in tools/__init__.py (logs warning on duplicate)
- [x] P2: Added LRU eviction to _conversations dict (max 50, evicts least-active)
- [x] Updated architecture.md: new data flow diagram with gpu_pool, task type routing table
- [x] Updated multi-device.md: documented task_type_context usage and routing behavior
- All 121 tests pass

**Next session priorities:**
1. M1: Unify discovery + heartbeat into single registry in gpu_pool.py
2. M1: Add model-aware sticky routing (prefer workers with right model loaded)
3. P2: Create mock httpx client fixture for tool handler testing
4. P2: Move SQLite databases out of source tree
5. P3: Frontend improvements (debounce, mobile breakpoints)

### 2026-04-08 — Kiro Session #4 (DB Paths + CSP + Shared Utils + Devlog)

**Completed:**
- [x] P0: Added CSP header to gateway (Content-Security-Policy, X-Content-Type-Options,
  X-Frame-Options, Referrer-Policy) via SecurityHeadersMiddleware
- [x] P2: Moved all 4 SQLite databases out of source tree to LOCALFORGE_DATA_DIR:
  - knowledge.db, task_queue.db, approval_queue.db, message_bus.db
  - Added `approval_db_path()` and `message_bus_db_path()` to paths.py
  - Fixed graph.py, research_sessions.py, task_queue.py, approval.py, message_bus.py
- [x] P2: Created `tools/utils.py` with shared utilities:
  - `validate_file_path()`, `validate_directory()`, `build_system_message()`,
    `build_chat_body()`, `error_response()`
- [x] P2: Standardized API error response format via `error_response()` helper
- [x] P2: Removed empty `agent_state/` directory, cleaned up supervisor.py fallback
- [x] Created DEVLOG.md in root development directory
- All 121 tests pass

**Next session priorities:**
1. Refactor tools to actually use `utils.py` helpers (reduce duplication)
2. M1: Unify discovery + heartbeat into single registry in gpu_pool.py
3. M1: Model-aware sticky routing
4. P2: Mock httpx client fixture for tool handler testing
5. P3: Frontend improvements

### 2026-04-08 — Kiro Session #5 (Test Infra + Tool Refactoring + Devlog)

**Completed:**
- [x] P2: Created mock httpx client fixture in conftest.py
  - `mock_httpx_client` fixture: configurable chat responses, model info, custom URL responses
  - `tmp_data_dir` fixture: isolated temp data directory for database tests
  - `MockHTTPResponse` class with raise_for_status() support
- [x] P2: Refactored tools to use `utils.py` shared helpers:
  - analysis.py: file_qa, analyze_image now use `validate_file_path()`
  - parallel.py: _local_analyze_one uses `validate_file_path()`, quality_sweep uses `validate_directory()`
- [x] Added 20 new tests:
  - test_client_routing.py: _extract_content, task_type_context, chat with mock
  - test_utils.py: validate_file_path, validate_directory, error_response
  - test_db_paths.py: database path resolution under LOCALFORGE_DATA_DIR
- [x] Created DEVLOG.md in root development directory
- 141 tests passing (up from 121)

**Cumulative session stats (sessions 1-5):**
- P0 completed: 4 of 7 (hmac fix, key scrub, CSP header, worker auth)
- P1 completed: 5 of 5 (all done!)
- P2 completed: 9 of 14
- M0 completed: 3 of 5
- M1 completed: 3 of 10 (routing core, worker model probe, task type context)
- M5 completed: 1 of 9 (CPU-only fix)
- Tests: 121 → 141

**Next session priorities:**
1. M1: Unify discovery + heartbeat into single registry in gpu_pool.py
2. M1: Model-aware sticky routing
3. P2: Replace bare `except Exception` in priority files (routes.py, supervisor.py)
4. P3: Frontend improvements
5. Bring up first physical worker node and test end-to-end

### 2026-04-08 — Kiro Session #6 (Registry Unification + Model Routing + Worker Resilience)

**Completed:**
- [x] M1: Unified discovery + heartbeat into single registry in gpu_pool.py
  - Moved `_mesh_workers` dict from routes.py into `gpu_pool._heartbeat_nodes`
  - Added `register_heartbeat()` and `get_mesh_workers()` methods to GPUPool
  - routes.py heartbeat endpoint now calls `gpu_pool.register_heartbeat()`
  - compute.py `_get_mesh_workers()` reads from gpu_pool directly (no cross-module import)
  - Eliminated the circular import between gpu_pool.py and routes.py
  - gateway.py wires `_gpu_pool_ref` into routes during lifespan
- [x] M1: Added model-aware sticky routing to `route_task()`
  - Workers with the right model loaded get priority (avoids 30-120s model swaps)
  - Configurable via `task_routing.{task_type}.prefer_model` in config.yaml
  - Sort key: model_match > tier_preference > load > thermal/battery penalty
- [x] M5: Added memory-pressure task rejection to worker
  - Checks `/proc/meminfo` MemAvailable before accepting tasks
  - Configurable via `--min-memory 500` CLI arg (default 500MB)
  - Returns 503 with clear error message when memory is low
- [x] M5: Added power-aware task rejection to worker
  - Rejects tasks when battery < floor% and not charging
  - Configurable via `--battery-floor 15` CLI arg (default 15%)
- [x] Rewrote mesh tests to use gpu_pool directly (no routes.py dependency)
  - Added tests: size bound rejection, existing update bypass, health status, stale cleanup
- 145 tests passing (up from 141)

**Cumulative session stats (sessions 1-6):**
- P0 completed: 4 of 7
- P1 completed: 5 of 5 (all done!)
- P2 completed: 9 of 14
- M0 completed: 3 of 5
- M1 completed: 5 of 10 (routing core, worker model probe, task type, registry unification, model-aware routing)
- M5 completed: 3 of 9 (CPU-only fix, memory pressure, power aware)
- Tests: 121 → 145

**Next session priorities:**
1. M1: Retry + circuit breaker for mesh_dispatch
2. M1: Persist mesh worker registry to SQLite (survive gateway restarts)
3. P2: Replace bare `except Exception` in priority files
4. P3: Frontend improvements
5. Bring up first physical worker node and test end-to-end

### 2026-04-08 — Kiro Session #7 (Node-2 Readiness)

**Critical fix: `route_request()` now includes mesh workers.**
Previously, `route_request()` only looked at `_backends` (text-gen-webui on :5000).
A worker on :8200 registered via heartbeat would never get routed to by `chat()`.
This was THE thing that would make a second node sit idle.

**Also fixed: protocol mismatch between backends and workers.**
`chat()` sends OpenAI-compatible `/chat/completions` to backends, but workers
serve `/task` with a different payload format. Added `_chat_to_worker()` and
`_is_worker_url()` to handle the protocol difference transparently.

**Completed:**
- [x] M1: `route_request()` now considers heartbeat workers + Tailscale compute nodes
  - Classifies worker model names to match task types
  - Deduplicates across all three sources (backends, heartbeat, discovery)
- [x] M1: Added `_chat_to_worker()` for mesh worker dispatch protocol
  - Converts OpenAI chat body to worker task format
  - Extracts response from worker's `{response: "..."}` format
- [x] M1: Added `_is_worker_url()` to detect worker vs backend URLs
- [x] M1: Fallback logic now includes healthy heartbeat workers with inference capability
- [x] M1: Fallback dispatch uses correct protocol per URL type
- 145 tests passing

**The mesh is now ready for a second node. Checklist:**
1. Both devices on same Tailscale network
2. Hub running: `localforge-gateway --port 8100`
3. Worker: `localforge-worker --hub ai-hub:8100 --key YOUR_KEY --port 8200`
4. Verify: `curl http://worker-hostname:8200/health` (from hub)
5. Verify: `compute_status` shows the worker in "Mesh Workers (heartbeat)"
6. Test: `mesh_dispatch(task_type="chat", payload={"prompt": "hello"})` → routes to worker
7. Test: regular `local_chat(prompt="hello")` → may route to worker if it's healthier

**Next session priorities:**
1. Actually bring up the second node and test
2. M1: Retry + circuit breaker for mesh_dispatch
3. M1: Persist mesh worker registry to SQLite
4. P2: Replace bare `except Exception` in priority files

### 2026-04-12 — Kiro Session #8 (Infra Hardening + Mesh Reliability)

**Completed:**
- [x] P4: Added `asyncio.wait_for()` timeout to `agent.execute()` in supervisor.py
  - Both `_run_once()` and `_schedule_inner()` now wrap execution with configurable timeout
  - Default 1 hour, per-agent override via `timeout` key in agents.yaml
  - Logs clear error on timeout, saves state, doesn't crash the schedule loop
- [x] P2: Consolidated `_load_config()` implementations across 4 files
  - Added `load_config_cached()` to config.py (30s TTL cache, single source of truth)
  - auth.py now imports from config.py instead of having its own loader
  - gateway.py and routes.py also use the shared loader
  - Eliminated 3 duplicate config loading implementations
- [x] P2: Added `RequestBodyLimitMiddleware` to gateway.py
  - 1MB default for all routes, 50MB for upload routes (photos, videos, transcribe, training)
  - Rejects oversized payloads with 413 before they hit route handlers
  - Covers all routes, not just api_chat
- [x] M1: Added worker connection test on startup (`_test_hub_connection()`)
  - 3 retries with 2s delay, clear error message on auth failure (401)
  - Heartbeat loop still starts even if test fails (worker may come up before hub)
  - Prevents silent failures where worker runs but never registers
- [x] M1: Added `compute_test` MCP tool for end-to-end mesh validation
  - Sends a test prompt to each healthy worker in parallel
  - Reports latency, success/failure, and response preview per worker
  - Like a mesh ping — confirms the full path works after bringing up a new node
- [x] M1: Added retry + circuit breaker to `mesh_dispatch`
  - On failure, tries next candidate worker (up to 3 attempts)
  - Reports success/failure to gpu_pool circuit breakers via `record_failure()`/`record_success()`
  - Added `record_failure(url)` and `record_success(url)` helper methods to GPUPool
- [x] M1: Added routing decision logging to gpu_pool.py
  - Both `route_request()` and `route_task()` now log at DEBUG level
  - Shows: chosen URL, model name, load, source (model-match vs fallback), candidate count
  - Enable with `--log-level DEBUG` on the gateway
- [x] Added 13 new tests (config consolidation, agent timeout, body limits, circuit breaker, routing)
- 158 tests passing (up from 145)

**Cumulative session stats (sessions 1-8):**
- P0 completed: 4 of 7
- P1 completed: 5 of 5 (all done!)
- P2 completed: 12 of 14 (added: request body limits, config consolidation, +prior)
- P4 completed: 1 of 9 (agent timeout)
- M0 completed: 3 of 5
- M1 completed: 10 of 14 (added: dispatch retry, worker connection test, compute_test, routing logging)
- M5 completed: 3 of 9
- Tests: 121 → 158

**Next session priorities:**
1. Actually bring up the second node and test end-to-end
2. M1: Persist mesh worker registry to SQLite (survive gateway restarts)
3. P2: Replace bare `except Exception` in priority files (routes.py, supervisor.py)
4. M1: Add hub → worker command channel
5. P3: Frontend improvements (debounce, mobile breakpoints)

### 2026-04-12 — Kiro Session #8b (Frontend + KG + Metrics Caching)

**Completed:**
- [x] P4: Cached GPU metrics (`nvidia-smi`) and `ps aux` status calls with 15s TTL
  - `api_metrics()` and `api_status()` server_config no longer spawn subprocesses on every request
- [x] P4: Added FTS5 rebuild mechanism to knowledge graph
  - `KnowledgeGraph.rebuild_fts_index()` drops and recreates FTS table + triggers, repopulates from entities
  - Exposed as `kg_rebuild_fts` MCP tool for recovery after crash-during-write
- [x] P3: Fixed blob URL memory leak — added LRU eviction at 50 entries with `URL.revokeObjectURL()`
- [x] P3: Added `:focus-visible` outlines for all interactive elements (tabs, buttons, inputs, selects, links)
- [x] P3: Fixed color contrast — `--text-dim` updated from `#8b949e` (3.5:1) to `#9da5ae` (~4.6:1)
- [x] P3: Added `aria-label` to all emoji/icon buttons in index.html
- [x] P3: Added `prefers-reduced-motion` media query to disable animations
- [x] P3: Added supplementary ✓/✗ icons to color-only status indicators (colorblind-friendly)
- [x] P3: Added `debounce()` utility to app.js (search inputs already use Enter/click, not input events)
- [x] P2: Created Kiro steering file for platform_next (`rust-standards.md` with auto-inclusion)
- [x] Added 4 new tests (KG FTS rebuild: empty, preserves search, corruption recovery)
- Tool count: 111 → 112 (added `kg_rebuild_fts`)
- 162 tests passing (up from 158)

**Cumulative session stats (sessions 1-8b):**
- P0 completed: 4 of 7
- P1 completed: 5 of 5 (all done!)
- P2 completed: 13 of 14 (added: Kiro steering)
- P3 completed: 7 of ~18 (focus styles, contrast, aria-labels, reduced-motion, status icons, blob cache, debounce)
- P4 completed: 3 of 9 (agent timeout, metrics caching, FTS rebuild)
- M0 completed: 3 of 5
- M1 completed: 10 of 14
- M5 completed: 3 of 9
- Tests: 121 → 162

**Next session priorities:**
1. Bring up second node and test end-to-end
2. M1: Persist mesh worker registry to SQLite
3. P2: Replace bare `except Exception` in priority files
4. M1: Add hub → worker command channel
5. P3: Mobile breakpoints and touch targets

### 2026-04-12 — Kiro Session #8c (Mobile + Systemd + Final Polish)

**Completed:**
- [x] P3: Added mobile responsive breakpoints (768px and 480px)
  - Reduced header/nav padding, stacked layouts, hidden model badge on small screens
- [x] P3: Added 44px minimum touch targets via `@media (pointer: coarse)`
  - Tabs, icon buttons, file labels, chat send, history items, inputs all get 44px min
- [x] P4: Added `StartLimitIntervalSec=60` + `StartLimitBurst=3` to systemd services
  - Gateway example service and setup-worker.sh generated service
- [x] P4: Added `Restart=on-failure` with `RestartSec=5s` backoff
- [x] P4: Added systemd security hardening to both service files
  - `PrivateTmp=true`, `NoNewPrivileges=true`, `ProtectSystem=strict`, `ReadWritePaths=%h`
- 162 tests passing

**Cumulative session stats (all sessions):**
- P0 completed: 4 of 7
- P1 completed: 5 of 5 (all done!)
- P2 completed: 13 of 14
- P3 completed: 9 of ~14 (added: mobile breakpoints, touch targets)
- P4 completed: 5 of 9 (added: systemd limits, restart backoff)
- M0 completed: 3 of 5
- M1 completed: 10 of 14
- M5 completed: 3 of 9
- Tests: 121 → 162

**Next session priorities:**
1. Bring up second node and test end-to-end
2. M1: Persist mesh worker registry to SQLite
3. P2: Replace bare `except Exception` in priority files
4. M1: Add hub → worker command channel

---

## Compute Mesh / Multi-Device Readiness Plan

Deep analysis from 2026-04-08. Every code path traced through gpu_pool.py,
device_worker.py, detect.py, compute.py, client.py, gateway.py, base.py,
message_bus.py, supervisor.py, and routes.py.

### What's Already Solid

- `device_worker.py` (792 lines) is a real, working worker agent — not a stub.
  Task queuing with configurable concurrency, heartbeat push to hub, graceful
  shutdown with drain, `LlamaServerManager` with crash-watching and exponential
  backoff restart, auth on mutating endpoints, system metrics collection.
- `detect.py` covers NVIDIA, Apple Silicon, Adreno (Android), AMD Radeon (both
  macOS system_profiler and Linux DRM/rocm-smi), and Vulkan fallback. Battery
  and thermal awareness. Model recommendations per device class. Tier auto-
  classification based on VRAM/RAM thresholds.
- `gpu_pool.py` has per-backend circuit breakers (5 failures → open, 60s
  cooldown, half-open probe), Tailscale auto-discovery for both text-gen-webui
  (:5000) and workers (:8200), capability-based routing with configurable tier
  preferences per task type, thermal/battery penalty in sort, and heartbeat
  worker integration via lazy import from routes.py.
- `base.py` agent class has `call_mesh()` method — agents can already dispatch
  tasks to mesh workers. Trust-gated tool access, sub-agent spawning, message
  bus integration, approval queue for destructive actions at FULL trust.
- `message_bus.py` is SQLite-backed with persistence, topic-based pub/sub,
  request/reply pattern, history with pruning, and subscriber management.
- The 6 mesh MCP tools cover status, routing preview, direct dispatch, parallel
  fan-out, batch embedding distribution, and model recommendation.
- `client.py` has multi-backend failover with priority ordering — if the primary
  backend goes down, it probes and switches to the next healthy one.

### Critical Gaps Found (Code-Level)

**~~The Big One: `client.py` chat() doesn't use GPU pool routing.~~** FIXED.
`chat()` now calls `gpu_pool.route_request(task_type)` via a contextvars-based
task type hint. All 112 tools are mesh-aware without any tool code changes.
Fallback logic unified across config backends and pool backends with circuit
breaker feedback.

**Heartbeat data is lost on gateway restart.** `_mesh_workers` in `routes.py`
is a plain dict — no persistence. Workers re-register on next heartbeat (30s),
but there's a window where the mesh appears empty.

**Discovery and heartbeat are two separate registries.** `gpu_pool._compute_nodes`
(Tailscale discovery) and `_mesh_workers` (heartbeat push) are separate dicts.
`_get_heartbeat_workers()` bridges them via import from routes.py, creating a
circular-ish dependency. Same device can appear twice with different health.

**~~Worker doesn't report external backend model.~~** FIXED.
Heartbeat now probes `_backend_url` for model info when no llama-server is running.

**No hub → worker command channel.** Communication is one-way: workers push
heartbeats, hub routes tasks. Hub can't tell a worker "swap model" or "run agent".

**`mesh_dispatch` has no retry/cache/circuit breaker.** Raw httpx POST, single
attempt. Compare with `client.py` which has tenacity retry, caching, and failover.

### Gaps and Improvements Needed

#### M0 — Must Fix Before Adding Nodes

- [x] **Worker auth uses bare `==` comparison (same timing attack as auth.py)**
  - `device_worker.py` `_check_worker_auth()` does `auth == f"Bearer {_api_key}"`
  - Fix: use `hmac.compare_digest()`
  - File: `src/localforge/workers/device_worker.py`

- [x] **Heartbeat endpoint has no auth check**
  - `api_mesh_heartbeat` in `routes.py` accepts any POST — no bearer token validation
  - A rogue device on the Tailscale network could register fake workers
  - VERIFIED: heartbeat is in `dashboard_routes` mounted at `/api`, which goes through
    `BearerAuthMiddleware`. Not in `PUBLIC_PATHS`. Auth is already enforced.

- [x] **`_mesh_workers` dict has no size bound**
  - Stale entries are cleaned after 10 min, but a flood of fake heartbeats could grow it
  - Add a max size (e.g., 100 workers) and reject new registrations beyond that

- [x] **Worker task endpoint has no request body size limit**
  - `handle_task` reads `await request.json()` with no size check
  - A malicious payload could OOM the worker
  - Add `content-length` check (e.g., 10MB max)

- [ ] **No TLS between hub and workers**
  - Tailscale encrypts the tunnel, so this is low-risk on a private tailnet
  - But if you ever expose workers on a LAN or WAN, add mTLS
  - The `--tls-cert` / `--tls-key` args exist but aren't documented in setup guides

#### M1 — Core: Make the Mesh Actually Route Inference

- [x] **Wire `gpu_pool.route_request()` into `client.py` chat()**
  - THE critical change. When `chat()` is called:
    1. Check if GPU pool has multiple healthy backends
    2. If the current tool call has a task_type hint (code, vision, reasoning),
       call `gpu_pool.route_request(task_type)` to pick the best backend
    3. Use that URL instead of `cfg.TGWUI_BASE`
    4. On failure, fall back to current failover logic
  - This makes ALL 112 tools mesh-aware without changing any tool code
  - Files: `client.py`, `gpu_pool.py`
  - Requires: passing task_type context through the call chain (add optional
    param to `chat()`, tools pass it based on their nature)

- [x] **Unify discovery and heartbeat into a single node registry**
  - Move `_mesh_workers` from `routes.py` into `gpu_pool.py` as a proper
    `_heartbeat_nodes: dict[str, ComputeNode]` alongside `_compute_nodes`
  - `gpu_pool` becomes the single source of truth for all mesh state
  - `routes.py` heartbeat endpoint calls `gpu_pool.register_heartbeat()`
  - Eliminates the circular import and duplicate registries
  - `compute_status` tool already calls both — just merge the backing stores

- [ ] **Persist mesh worker registry to SQLite**
  - On gateway restart, reload last-known workers from DB
  - Mark them as "stale" until next heartbeat confirms they're alive
  - Agents that run on startup can see the mesh immediately
  - Table: `mesh_nodes(key, hostname, port, tier, capabilities_json, model_name,
    last_heartbeat, healthy)`

- [x] **Add model-aware routing (sticky by loaded model)**
  - `route_task()` already sorts by tier preference, but ignores `model_name`
  - Change: if a worker has the right model loaded, prefer it (even if higher load)
  - Avoids model swaps (30-120 seconds each)
  - Add `model_preference` to routing: exact match > same family > any

- [ ] **Add hub → worker command channel**
  - Workers poll `GET /api/mesh/commands/{hostname}` on each heartbeat
  - Hub queues commands: `swap_model`, `install_capability`, `update_code`,
    `run_agent`, `shutdown`
  - Worker executes and reports result on next heartbeat
  - This unlocks: hub-orchestrated model placement, remote agent execution,
    capability provisioning

- [ ] **Add worker-side model swap endpoint**
  - `POST /model/swap` on the worker: stops llama-server, loads new GGUF, restarts
  - `LlamaServerManager` already has `stop()` and `start()` — add `swap(path)`
  - Hub calls this via the command channel or direct HTTP

- [ ] **Add model distribution (serve GGUF from gateway)**
  - `GET /api/models/download/{filename}` — streams GGUF from the model directory
  - Use `StreamingResponse` with chunked transfer for large files (5-20GB)
  - Add: `GET /api/models/inventory` listing all GGUFs with size, quant, tier

- [x] **Add retry + circuit breaker to `mesh_dispatch`**
  - Currently a single httpx POST with no retry
  - Wrap with tenacity retry (2 attempts), circuit breaker per worker
  - On failure, try next-best worker from routing
  - Share circuit breaker state with `gpu_pool`
  - Added failover to next candidate on error, gpu_pool.record_failure/record_success

- [x] **Make worker probe its own backend for model info**
  - If worker uses `_backend_url` (external text-gen-webui), probe it on startup
    and periodically for model name
  - Include in heartbeat so hub can do model-aware routing

- [x] **Add worker connection test on startup**
  - Worker should verify it can reach the hub before entering the heartbeat loop
  - `POST /api/mesh/heartbeat` with a test payload, check for 200
  - Print clear error if hub is unreachable or auth fails
  - Prevents silent failures where worker runs but never registers
  - Added `_test_hub_connection()` with 3 retries, clear auth failure message

- [x] **Add routing decision logging**
  - Log which backend/worker was chosen and why (task_type, model match, load)
  - Helps debug "why did this go to the hub instead of the worker?"
  - Add `--verbose-routing` flag or log at DEBUG level
  - Added DEBUG-level logging to both `route_request()` and `route_task()`

- [x] **Add `compute_test` MCP tool for end-to-end mesh validation**
  - Sends a simple chat task to each healthy worker, reports latency + success
  - Like a mesh ping — confirms the full path works before relying on it
  - Useful after bringing up a new node

#### M2 — Embedding, RAG, KG, and Agent Distribution

- [ ] **Add embedding offload to mesh**
  - `embeddings.py` lazy-loads fastembed models on the hub CPU
  - Add `embed_texts_distributed()` that checks for healthy mesh workers with
    `embeddings` capability, splits texts, dispatches via `/task`, collects results
  - Fall back to local if no workers available
  - Wire into `rag.py` index building and `semantic.py` search

- [ ] **Add distributed RAG indexing pipeline**
  - Current `index_directory`: read files → chunk → embed → store (all on hub)
  - Distributed: hub reads + chunks → fan out embedding batches to N workers →
    hub assembles BM25 + vector index
  - `mesh_batch_embed` tool already does the fan-out — wire it into the indexer
  - Estimated speedup: linear with number of embedding workers

- [ ] **Add index replication to workers**
  - Workers doing RAG search need the index locally
  - Add `POST /index/push` on worker that receives a serialized index
  - Enables: RAG queries routed to the nearest worker with the index

- [ ] **Add KG backup/replication**
  - `knowledge/graph.py` is SQLite — easy to backup
  - Add periodic `VACUUM INTO` to a backup path, push to a cpu-capable worker
  - Restore path: worker serves backup, hub downloads on recovery

- [ ] **Add `run_on` config to agents.yaml**
  - Currently all agents run in the hub's supervisor process
  - Add: `run_on: "local"` (default), `run_on: "worker-laptop2"`,
    `run_on: "any-gpu-secondary"`, `run_on: "any-cpu-capable"`
  - Supervisor resolves target from mesh registry, dispatches via command channel
  - Agent's `call_tool()` still goes through the hub gateway (auth + routing)

- [ ] **Add remote agent execution protocol**
  - Worker receives `{"type": "run_agent", "agent_type": "research-agent", ...}`
  - Worker imports agent class, creates instance, calls `execute()`
  - Agent calls tools via hub gateway URL (already how `base.py` works)
  - Results flow back via heartbeat stats or direct HTTP callback

- [ ] **Add mesh-aware sub-agent spawning**
  - `base.py` `spawn_child()` currently spawns on the hub only
  - Add: `spawn_child(run_on="any-gpu-secondary")` to spawn on a worker
  - Enables: research-agent spawns sub-agents on different workers for parallel
    web research, each using a different model

- [ ] **Add mesh event bus (cross-device pub/sub)**
  - Current message bus is in-process (asyncio queues) — hub only
  - For cross-device coordination: lightweight pub/sub over HTTP
  - Workers subscribe to topics, hub broadcasts events
  - Enables: worker reacts to "model swapped on hub" by updating routing prefs

#### M3 — Training & ML Distribution

- [ ] **Add remote training dispatch**
  - `train_start` currently requires unloading the hub's model (blocks inference)
  - Add `target` param: `train_start(dataset="...", target="worker-laptop2")`
  - Hub sends dataset + config to worker, worker runs Unsloth, exports GGUF
  - Worker pushes GGUF back to hub's model directory
  - Hub keeps serving inference the entire time

- [ ] **Add LoRA adapter distribution**
  - After training, push LoRA adapter to gpu-secondary workers
  - Workers load base model + LoRA via llama-server `--lora` flag
  - Enables project-specific inference on secondary nodes

- [ ] **Add continuous training data collection from mesh**
  - Workers doing inference log prompt/response pairs locally
  - Hub periodically collects via `GET /training/feedback` on workers
  - Merges into central `feedback.jsonl` for `train_prepare(mode='from-feedback')`

- [ ] **Add model evaluation across mesh**
  - After training, dispatch eval tasks to a gpu-secondary
  - Compare base vs fine-tuned on a standard test set
  - Report: perplexity, task accuracy, latency, VRAM usage

- [ ] **Add speculative decoding across devices**
  - Draft model on lightweight/gpu-secondary, verification on gpu-primary
  - Requires custom integration with llama.cpp `--draft-model` over network
  - Tailscale LAN latency (~1-5ms) makes this viable

#### M4 — Dashboard & Admin Experience

- [ ] **Add Compute Mesh dashboard tab**
  - Device cards: hostname, tier badge, VRAM/RAM bars, loaded model, capability
    icons, heartbeat age, task count, health indicator
  - Per-device actions: swap model (dropdown), trigger task, view logs, restart
  - Mesh overview: total VRAM, total RAM, total inference capacity, task distribution
  - Health timeline: sparkline per node showing uptime over last 24h

- [ ] **Add admin mesh management view**
  - Shows: all registered workers (including stale), discovery log, circuit breaker
    states, routing decisions log, command queue
  - Actions: force-remove worker, reset circuit breaker, push model, push update,
    approve/deny worker registration
  - Config editor: model routing rules, tier preferences, discovery intervals

- [ ] **Add user-facing "My Devices" view**
  - For non-admin users (wife, guests): shows their connected devices
  - Simple status: "Your phone is connected", "AI is available"
  - No admin controls, just status and basic chat access

- [ ] **Add real-time mesh event stream**
  - SSE endpoint `/api/mesh/events` that streams: worker joins/leaves, model
    swaps, task dispatches, health changes
  - Dashboard subscribes for live updates without polling

- [ ] **Add notification routing to user devices**
  - When an agent completes or an alert fires, push to the user's phone
  - Use SSE (already exists) + optional Telegram/push notification
  - Per-user notification preferences: which agents, which severity levels

- [ ] **Add worker log aggregation**
  - `GET /logs` endpoint on workers, hub aggregates via new tool
  - Dashboard shows per-worker log viewer with tail -f style streaming

- [ ] **Add health history / uptime tracking per node**
  - Store health check results in SQLite: timestamp, node, healthy, latency_ms
  - Dashboard shows uptime bars per node (last 24h, 7d)

#### M5 — Old Hardware & Deployment

- [ ] **Document minimum specs per tier with real examples**
  - lightweight (4GB RAM): old Android phones, Raspberry Pi 4
  - cpu-capable (8GB+ RAM, 4+ cores): old MacBooks, iMacs, Intel laptops
  - gpu-secondary (4GB+ VRAM): gaming laptops GTX 1060+, Apple Silicon Macs
  - Add to multi-device.md with setup time estimates

- [x] **Fix CPU-only inference in LlamaServerManager**
  - `gpu_layers` defaults to `-1` (all GPU) — crashes on CPU-only devices
  - Fix: if `detect().gpu_type == "none"`, set `gpu_layers=0` automatically
  - File: `device_worker.py` main() where `LlamaServerManager` is created

- [x] **Add memory-pressure task rejection**
  - Before accepting a task, check `/proc/meminfo` MemAvailable
  - If < 500MB (configurable), return 503 with "insufficient memory"

- [x] **Add power-aware task acceptance**
  - If battery < 15% and not charging, stop accepting tasks (return 503)
  - Configurable via `--battery-floor 15` CLI arg

- [ ] **Validate Termux worker deployment end-to-end**
  - Test on actual old Android phone
  - Document: minimum Android version, Termux quirks, wake lock setup

- [ ] **Add `setup-worker.sh` to the localforge package**
  - Move from `~/Development/scripts/` to `localforge/scripts/`
  - Serve via gateway at `/static/setup-worker.sh`
  - Handle: platform detection, venv, deps, service creation, model download,
    hub registration test, firewall hint

- [ ] **Add worker self-update mechanism**
  - Version field in heartbeat, hub responds with "update available" if mismatch
  - `GET /api/mesh/worker-bundle` returns tarball of worker code
  - Worker extracts, restarts itself

- [ ] **Add automatic model placement optimization**
  - Given N devices with different VRAM, and M models to keep warm:
  - Solve: which model on which device to minimize swap latency
  - Simple heuristic: code model on gpu-secondary-1, reasoning on gpu-secondary-2

- [ ] **Add worker capability auto-install**
  - `--install-capabilities` flag that auto-installs fastembed, piper, etc.
  - Or: hub pushes a "capability request" via command channel

