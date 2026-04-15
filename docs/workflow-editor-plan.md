# LocalForge Visual Workflow Editor — Implementation Plan

**Status:** Draft · 2026-04-14
**Owner:** tyler
**Depends on:** Phase 1 mesh substrate (done), Mesh tab (done)

---

## 1. Why build this instead of "just use LangGraph"

LangChain / LangGraph are good at orchestrating LLM calls. LocalForge is a different
shape of system — the thing a workflow editor has to understand is not _just_ LLMs,
it's the whole local stack:

| LocalForge capability                          | Covered by LangGraph? |
|------------------------------------------------|----------------------|
| text-generation-webui slots & swap_model       | No                   |
| 112 MCP tools (RAG, KG, agents, web, git, …)   | Only if you adapt each one |
| Mesh workers on heterogeneous hardware (Win/Mac/Linux/Android) with capability-based routing | No |
| Local image-gen (qwen-image-edit) / video (wan2.2) | No                |
| Approval gates + SSE notifications             | No                   |
| Knowledge-graph-backed memory                  | No                   |
| Multi-user auth + scoped keys                  | No                   |

The right framing: **LocalForge already has a DAG engine** (`workflows/engine.py`,
411 lines, safe-eval'd conditions, parallel nodes, loops, topo execution). The
gap is a visual editor that produces / edits the same YAML this engine already
consumes. We're not reinventing a runtime — we're drawing on a runtime that exists.

If in the future a specific LangGraph feature is genuinely useful (streaming
checkpoints, interruption semantics), we wrap it as a custom node type. We do
not fork our runtime onto theirs.

---

## 2. What exists today

- **`workflows/schema.py`** — `WorkflowDef` / `NodeDef` / `EdgeDef` dataclasses,
  YAML round-trip, `validate()`, topo helpers (`root_nodes`, `get_successors`).
  Node types already defined: `prompt`, `tool`, `parallel`, `condition`, `loop`,
  `set_variable`.
- **`workflows/engine.py`** — async executor, AST-walked safe-eval, variable
  substitution, progress callbacks, parallel branch execution.
- **`tools/orchestration.py`** — `workflow(steps=[...])` MCP tool that already
  executes a minimal linear form.
- **Dashboard Workflows tab** — today shows saved pipelines as a flat list; no
  canvas.

The editor's job is to produce / edit / debug these `WorkflowDef` objects
visually. Everything downstream already works.

---

## 3. Scope of V1 (the thing we actually build first)

**Ship a side-by-side editor / runner:**

1. Canvas with draggable nodes + bezier edges.
2. Palette of node types (existing 6 + 3 new: see §5).
3. Inspector panel on the right that edits the selected node's `config`.
4. Save to `data_dir()/workflows/<id>.yaml`; round-trip through existing schema.
5. Run button that streams progress over SSE into the canvas (node glow +
   per-node timing + last output preview).
6. Share-the-graph: copy-as-YAML, paste-from-YAML, export-as-png.

**Explicitly out of scope for V1** — we'll add these in V2+:

- Subworkflows / composition (a node that embeds another workflow).
- Live-edit-during-run (hot-reload a running workflow).
- Git-backed versioning UI (use `git` on `data_dir()/workflows/` manually for now).
- A visual debugger with step-over / breakpoints.
- Non-developer "no-code" template library.

---

## 4. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Browser (Workflows tab)                                         │
│                                                                  │
│   ┌─────────────┐  ┌───────────────────────┐  ┌──────────────┐  │
│   │  Palette    │  │      Canvas (SVG)     │  │  Inspector   │  │
│   │             │  │                       │  │              │  │
│   │ prompt      │  │   ●─────▶●            │  │  [prompt]    │  │
│   │ tool        │  │          └─▶●         │  │  template:   │  │
│   │ parallel    │  │             └─▶●      │  │  [          ]│  │
│   │ condition   │  │                       │  │  model: □    │  │
│   │ mesh_task ● │  │                       │  │  route_to: ▾ │  │
│   │ image_gen ● │  │                       │  │              │  │
│   │ approval  ● │  │                       │  │              │  │
│   └─────────────┘  └───────────────────────┘  └──────────────┘  │
│           │                  │                       │          │
│           └──── events via SSE: /api/workflows/runs/{id}/events ─┤
└──────────────────────────────────────────────────────────────────┘
                             │
┌────────────────────────────┼─────────────────────────────────────┐
│  ai-hub (localforge)       ▼                                     │
│                                                                  │
│   /api/workflows  GET/POST/PUT/DELETE  ── WorkflowDef YAMLs      │
│   /api/workflows/{id}/run  POST         ── start run             │
│   /api/workflows/runs/{id}/events  SSE  ── live progress         │
│                                                                  │
│   workflows/engine.py  ─────────────────►  tool_registry         │
│                         ─────────────────►  mesh_dispatch        │
│                         ─────────────────►  approval queue       │
└──────────────────────────────────────────────────────────────────┘
```

**Key principle:** the canvas is a thin view over the existing `WorkflowDef`.
Every drag / connect / inspector-edit mutates the in-memory object, which is
serialized on save. No parallel "canvas state" that drifts from the engine's
state. This is the thing that usually goes wrong in DAG editors.

---

## 5. New node types we need (beyond the 6 existing)

### 5a. `mesh_task` — dispatch to a specific worker or capability
```yaml
- id: embed_chunks
  type: mesh_task
  config:
    task: embeddings              # matches WorkerRegistry.allowed_tasks
    prefer_worker: null           # or a specific worker_id
    route: capability             # capability | pinned | round_robin
    input: "{{chunks}}"
    timeout_s: 60
```
Implementation: calls `compute_route` → `mesh_dispatch` (both already MCP tools).

### 5b. `media` — generate image / video
```yaml
- id: thumbnail
  type: media
  config:
    kind: image                   # image | video | edit
    model: qwen-image-edit
    prompt: "{{doc.title}} cover, flat illustration"
    size: 1024x1024
    save_to: photos/{{user}}/generated/
```
Implementation: new `tools/media.py` wrapping ComfyUI / qwen-image-edit / wan2.2.

### 5c. `approval` — pause and wait for human signoff
```yaml
- id: review_before_publish
  type: approval
  config:
    summary: "Approve publishing this digest?"
    payload: "{{digest.text}}"
    timeout_s: 3600
    on_timeout: skip              # skip | fail
```
Implementation: integrates with the existing approval queue (`/api/approvals`).

These three unlock the majority of the "real-world useful" workflows the user
wants: research pipelines that end in a human-reviewed post, agent chains that
generate images, batch embedding jobs that fan out across the mesh.

---

## 6. UI design — the thing on screen

### 6a. Canvas
- **SVG**, not Canvas2D — nodes are clickable, accessible, zoomable via CSS
  transform. Avoid a DAG library (no reaflow / react-flow) to keep the "no
  build step" rule in this codebase.
- Nodes: 160×60 rectangles, rounded, color-coded by type, icon on left,
  truncated label, small status dot in the corner (idle / running / done / err).
- Edges: cubic bezier, drawn from output port (right) to input port (left);
  animated flow dots during a run.
- Pan: drag background. Zoom: ctrl+wheel (desktop) / pinch (touch).
- Selection: click = single, shift-click = multi, lasso with drag in empty space.
- Connect: drag from a node's output port onto another node's input port.
- Right-click / long-press: context menu (delete, duplicate, "run from here").

### 6b. Palette (left)
- Grouped by category: **Core** (prompt, tool, set_variable), **Control**
  (condition, loop, parallel), **Mesh & Media** (mesh_task, media), **Human
  loop** (approval).
- Drag onto canvas to instantiate. Default configs are useful (a `prompt`
  node with `template: "Summarize: {{input}}"` works out of the box).

### 6c. Inspector (right)
- Renders a form per node type using a tiny schema system (see §7).
- Tool-node: tool selector becomes a **searchable list of all 112 MCP tools**
  pulled from `/api/tools` — that's the killer feature.
- Template fields get a variable picker (`{{var}}` autocompletion for all
  upstream-defined variables, determined by tracing ancestor nodes).

### 6d. Run controls (top)
- Play / stop.
- Variables panel (edit workflow-level `variables` before running).
- Live run history: last 10 runs as chips, click to replay results on the
  canvas.

### 6e. Mobile (phone)
- The canvas is honestly not the primary mobile use case. On narrow screens
  we default to a **read-only list view** (node list + status) with a "open on
  desktop to edit" hint. Running existing workflows from the phone still works
  fully.

---

## 7. Per-node form schema

Instead of hand-writing a form component per node type, we define a tiny
`fields` array per type and render it generically:

```python
# localforge/workflows/node_specs.py
NODE_SPECS = {
    "prompt": {
        "icon": "💬",
        "color": "#58a6ff",
        "fields": [
            {"name": "template",   "type": "textarea", "label": "Prompt template", "required": True},
            {"name": "system",     "type": "textarea", "label": "System prompt"},
            {"name": "model",      "type": "select",   "label": "Model", "options_from": "/api/models"},
            {"name": "max_tokens", "type": "number",   "label": "Max tokens", "default": 1024},
        ],
    },
    "tool": {
        "icon": "🔧",
        "color": "#3fb950",
        "fields": [
            {"name": "tool_name",  "type": "select",   "label": "MCP tool", "options_from": "/api/tools", "searchable": True, "required": True},
            {"name": "arguments",  "type": "kvmap",    "label": "Arguments", "help": "Values support {{variable}} substitution"},
        ],
    },
    "mesh_task": { ... },
    # etc.
}
```

The frontend renders a field by `type` (`text`, `textarea`, `number`, `select`,
`kvmap`, `code`, `toggle`). This keeps adding node types cheap — 30 lines per
type, no new frontend component.

---

## 8. Execution + live feedback

Already solved on the engine side (`engine.py` has progress callbacks). We need:

1. A new route `POST /api/workflows/{id}/run` that wraps the engine and assigns
   a `run_id`.
2. A new SSE stream `GET /api/workflows/runs/{run_id}/events` that emits events:
   - `{"type": "node_start",  "node_id": "...", "at": 17...}`
   - `{"type": "node_output", "node_id": "...", "output": "..."}` (truncated to 2 KB)
   - `{"type": "node_end",    "node_id": "...", "status": "ok|err", "ms": 123}`
   - `{"type": "run_end",     "status": "ok|err", "ms": 4567}`
3. The canvas listens, mutates `.status` on each node div, animates edges as
   they "fire", pops the last output as a tooltip on hover.

Storage: `data_dir()/workflow_runs/<run_id>.jsonl` — one event per line, so the
Runs history page can replay.

---

## 9. Mesh-awareness

The `mesh_task` node is the big integration point. Concretely:

1. Inspector queries `/api/mesh/status` on open → populates a "Prefer worker"
   dropdown and shows which workers advertise each capability.
2. When the run fires a `mesh_task`, the engine calls `compute_route` with the
   task + preferences → gets a worker_id → calls `mesh_dispatch(worker_id, …)`.
3. The canvas shows a small platform icon on the node (the OS glyph of the
   worker that handled it) — so at-a-glance you see "this step ran on the
   Windows laptop."
4. Failures (worker offline, capability unavailable) surface as the node's
   error tooltip, not a modal — users should be able to restructure the graph
   without the error blocking them.

---

## 10. File layout — what we actually write

```
src/localforge/
├── workflows/
│   ├── schema.py             (exists — extend NodeDef types: mesh_task, media, approval)
│   ├── engine.py             (exists — add executors for the 3 new types)
│   ├── node_specs.py         (NEW — form schemas, ~200 lines)
│   └── runs.py               (NEW — run_id tracking, SSE event log, ~150 lines)
│
├── dashboard/
│   ├── routes.py             (extend — add /api/workflows + /runs + SSE)
│   └── static/
│       ├── index.html        (extend — replace Workflows tab body with canvas layout)
│       ├── js/
│       │   ├── workflow_canvas.js    (NEW — ~600 lines: SVG, drag, connect, zoom)
│       │   ├── workflow_inspector.js (NEW — ~200 lines: renders NODE_SPECS forms)
│       │   └── workflow_runs.js      (NEW — ~150 lines: SSE subscription + replay)
│       └── style.css         (extend — canvas/palette/inspector styles, ~300 lines)
│
├── tools/
│   └── media.py              (NEW — image/video node impl, ~200 lines;
│                              wraps existing qwen-image-edit + wan2.2 tools)
│
└── data_dir/
    ├── workflows/<id>.yaml   (NEW storage)
    └── workflow_runs/<id>.jsonl (NEW run event logs)
```

**Total**: ~1,800 lines of new code, roughly 60% JS, 40% Python.
This is about 1–2 weeks of focused work.

---

## 11. Build order (phased, 5 phases)

### Phase A — Backend plumbing (2–3 days, no UI)
- Add the 3 new node types (`mesh_task`, `media`, `approval`) to schema + engine.
- Build `node_specs.py` and expose at `/api/workflows/node-specs`.
- Build `runs.py`, wire SSE endpoint.
- Write tests: YAML round-trip for each new node type, engine runs one of each
  end-to-end. **Acceptance:** can run a 4-node workflow (`prompt → tool →
  mesh_task → approval`) via `curl`, see SSE events stream correctly.

### Phase B — Static canvas (2 days)
- Palette + canvas + drag-to-instantiate. **No connections, no inspector yet.**
- Nodes persist to `/api/workflows`.
- **Acceptance:** can drop 5 nodes on canvas, refresh, they're still there.

### Phase C — Connections + inspector (2–3 days)
- Port-to-port edge drawing.
- Generic form renderer for `NODE_SPECS`.
- Variable autocompletion in template fields (trace ancestors).
- **Acceptance:** user can build the Phase-A workflow entirely by drag/click
  and it saves to the exact same YAML format as hand-written.

### Phase D — Run + live feedback (1–2 days)
- Play button → POST run → subscribe SSE → animate nodes.
- Run history chips, replay.
- **Acceptance:** user hits play on a saved workflow, sees nodes glow
  in sequence, per-node timings visible.

### Phase E — Polish (ongoing)
- Multi-select, copy/paste, undo/redo (Ctrl+Z via a simple op-log).
- Export as PNG (SVG.toBlob → canvas → toDataURL).
- Node duplication ("run from here" context menu).
- A small library of **template workflows** shipped in `examples/workflows/`:
  - `research-and-summarize.yaml`
  - `code-review-with-mesh-embeddings.yaml`
  - `daily-digest-with-approval.yaml`
  - `document-to-kg.yaml`

---

## 12. The tricky parts (call them out now)

### 12a. Variable scoping in the UI
Autocompletion for `{{foo.bar}}` in template fields requires tracing ancestors
in the DAG. This is **not hard** — the schema already has `get_predecessors` —
but it needs care when the user has multiple paths converging on a node.
Decision: show variables from all ancestors, but annotate conditional paths
with a small "(if true branch of node X)" hint.

### 12b. Loop nodes visualized
A loop node contains `node_ids`. In the current YAML, those are just IDs —
there's no spatial relationship. For the canvas: we render loops as a
**container rectangle** that the user drops child nodes inside. Save-time we
translate back to `node_ids`. This is the one place canvas state and schema
diverge slightly; document carefully.

### 12c. Parallel fanout widths
A `parallel` node with 10 children looks terrible in a linear layout. We need
auto-layout (Dagre-style) on first open, but once the user drags anything we
respect their positions. Store per-node `x,y` in the YAML under a `layout`
metadata key the engine ignores.

### 12d. Approval nodes pause the run
SSE clients need to know when a run is "suspended awaiting approval" vs
"dead." Emit an explicit `{"type":"run_suspended","reason":"approval","approval_id":"..."}`
event; resume via the existing approval queue UI, which re-emits to the run's
SSE channel.

### 12e. Cross-worker media output is heavy
A mesh worker that generates a 4K image shouldn't POST the bytes back — it
should write to a shared / hub-pulled path. Use the existing photo-save
pattern: worker returns a URI, hub fetches on demand. Don't stream binary
over SSE.

---

## 13. What "done" looks like (acceptance for the whole editor)

A user on their phone can:
1. Open LocalForge, tap Workflows.
2. See a thumbnail of `daily-digest-with-approval` running right now: 3 of 5
   nodes glowing green.
3. See the current node waiting on approval.
4. Tap approve.
5. See the last 2 nodes finish, the run complete, the resulting summary
   appear as a note.

And on desktop the same user can:
1. Drag a new `mesh_task` node onto a blank canvas.
2. Connect a `prompt` node to it.
3. Pick the `mesh_task`'s capability (embeddings) from a dropdown populated
   from the actual mesh workers.
4. Hit play.
5. Watch both nodes animate, see the Windows laptop's icon light up on the
   `mesh_task`, see the output text appear in a tooltip.
6. Save, close, reopen — the workflow is persisted as YAML and runs the same
   way from the CLI (`local call workflow --id xyz`).

---

## 14. Open questions / decisions we defer

- **Undo/redo depth** — 50 steps is probably enough, but memory pressure on
  phones is a concern. Revisit after Phase C.
- **Collaborative editing** — skip for now. Single-user per workflow. If we
  ever add multi-user, use a simple "checkout" lock in the YAML header, not
  operational transform.
- **Typed connections** — should an edge from `embeddings` refuse to connect
  to `prompt.template`? Skip in V1 (let runtime error). V2 can add optional
  type hints in `NODE_SPECS` and validate on connect.
- **Non-Python custom code nodes** — deferred. A `code` node that executes
  user Python is a security / sandboxing project of its own. For now, custom
  behavior goes through `tool` nodes that call a pre-registered MCP tool.

---

## 15. Why not just adopt LangGraph after all

Concretely, adopting LangGraph would mean:
- Re-implementing 112 MCP tools as LangGraph tools.
- Rebuilding mesh-dispatch as a LangGraph-compatible executor.
- Teaching LangGraph about our approval queue, SSE system, auth scopes.
- Shipping a Python dependency with a large blast radius.

…and we'd still have to build the visual editor on top of it, because
LangGraph's "Studio" is cloud-hosted SaaS.

The runtime we already have is ~550 lines. Extending it with 3 node types and
building the editor on top is cheaper than integrating LangGraph **and** safer
to run on a family's devices with zero external dependencies.

This calculus may flip later — when the workflow engine starts needing
checkpointing, time-travel debugging, or human-in-the-loop semantics that we
don't want to maintain ourselves. That's a Phase F conversation, not a V1
blocker.
