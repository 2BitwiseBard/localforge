// Workflow editor — visual DAG editor backed by /api/workflows.
// Self-contained ES module; loaded as type="module" from index.html.
//
// V1 scope (per docs/workflow-editor-plan.md §3, Phase B):
// - Palette + Canvas + Inspector layout
// - Load / list / save / delete workflows (YAML/JSON persisted server-side)
// - Add nodes by clicking the palette
// - Click to select, edit config in inspector, delete selected node
// - Run the current workflow via existing /api/workflows/run
//
// Deferred to V2+:
// - Edge drawing (drag output→input)
// - Live SSE progress animation
// - Loop/parallel containers
// - Drag-to-reposition, zoom/pan, multi-select

const API = window.location.origin + '/api';
const NODE_W = 180;
const NODE_H = 60;
const COL_GAP = 60;
const ROW_GAP = 40;

function authHeaders(extra = {}) {
    const key = sessionStorage.getItem('ai-hub-key') || '';
    return { 'Authorization': `Bearer ${key}`, ...extra };
}

async function api(method, path, body) {
    const opts = { method, headers: authHeaders({ 'Content-Type': 'application/json' }) };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const resp = await fetch(API + path, opts);
    if (!resp.ok) {
        let msg = `${resp.status} ${resp.statusText}`;
        try { const j = await resp.json(); if (j.error) msg = j.error; } catch {}
        throw new Error(msg);
    }
    return resp.json();
}

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// -----------------------------------------------------------------------
// State — the canvas is a thin view over `state.workflow`.
// -----------------------------------------------------------------------

const state = {
    nodeSpecs: {},        // type -> spec
    categories: [],       // [{name, types[]}]
    list: [],             // [{id, name, ...}] from /api/workflows
    workflow: null,       // currently loaded WorkflowDef (mutable)
    selectedId: null,     // selected node id
    dirty: false,
};

function markDirty() {
    state.dirty = true;
    setStatus('Unsaved changes', 'warn');
}

function setStatus(text, kind = '') {
    const el = document.getElementById('wf-status');
    if (!el) return;
    el.textContent = text;
    el.className = 'config-status' + (kind ? ' config-status-' + kind : '');
}

// -----------------------------------------------------------------------
// Layout — simple auto-grid if node.layout.{x,y} isn't set.
// -----------------------------------------------------------------------

function ensureLayout(wf) {
    wf.nodes.forEach((n, i) => {
        n.layout = n.layout || {};
        if (typeof n.layout.x !== 'number' || typeof n.layout.y !== 'number') {
            const col = i % 4;
            const row = Math.floor(i / 4);
            n.layout.x = 40 + col * (NODE_W + COL_GAP);
            n.layout.y = 40 + row * (NODE_H + ROW_GAP);
        }
    });
}

// -----------------------------------------------------------------------
// Rendering
// -----------------------------------------------------------------------

function renderPalette() {
    const el = document.getElementById('wf-palette-body');
    if (!el) return;
    const cats = state.categories.length
        ? state.categories
        : [{ name: 'Nodes', types: Object.keys(state.nodeSpecs) }];
    el.innerHTML = cats.map(cat => `
        <div class="wf-palette-group">
          <div class="wf-palette-group-h">${escapeHtml(cat.name)}</div>
          ${cat.types.map(t => {
              const spec = state.nodeSpecs[t];
              if (!spec) return '';
              return `<button class="wf-palette-btn" data-type="${escapeHtml(t)}"
                        style="--node-color:${escapeHtml(spec.color)}"
                        title="${escapeHtml(spec.description || '')}">
                        <span class="wf-palette-icon">${escapeHtml(spec.icon || '?')}</span>
                        <span class="wf-palette-label">${escapeHtml(spec.label || t)}</span>
                      </button>`;
          }).join('')}
        </div>
    `).join('');
    el.querySelectorAll('.wf-palette-btn').forEach(btn => {
        btn.addEventListener('click', () => addNode(btn.dataset.type));
    });
}

function renderCanvas() {
    const svg = document.getElementById('wf-canvas');
    const empty = document.getElementById('wf-canvas-empty');
    if (!svg) return;
    if (!state.workflow) {
        svg.innerHTML = '';
        if (empty) empty.hidden = false;
        return;
    }
    if (empty) empty.hidden = state.workflow.nodes.length > 0;
    ensureLayout(state.workflow);

    const nodes = state.workflow.nodes;
    const edges = state.workflow.edges || [];

    // Fit viewBox to content
    let maxX = 1200, maxY = 800;
    nodes.forEach(n => {
        maxX = Math.max(maxX, n.layout.x + NODE_W + 40);
        maxY = Math.max(maxY, n.layout.y + NODE_H + 40);
    });
    svg.setAttribute('viewBox', `0 0 ${maxX} ${maxY}`);

    const edgeSvg = edges.map(e => {
        const from = nodes.find(n => n.id === (e.from || e.from_id));
        const to   = nodes.find(n => n.id === (e.to   || e.to_id));
        if (!from || !to) return '';
        const x1 = from.layout.x + NODE_W;
        const y1 = from.layout.y + NODE_H / 2;
        const x2 = to.layout.x;
        const y2 = to.layout.y + NODE_H / 2;
        const mx = (x1 + x2) / 2;
        return `<path class="wf-edge" d="M ${x1} ${y1} C ${mx} ${y1} ${mx} ${y2} ${x2} ${y2}"
                      fill="none" stroke="#6e7681" stroke-width="1.5"/>`;
    }).join('');

    const nodeSvg = nodes.map(n => {
        const spec = state.nodeSpecs[n.type] || { color: '#8b949e', icon: '?', label: n.type };
        const selected = n.id === state.selectedId;
        return `<g class="wf-node ${selected ? 'wf-node-sel' : ''}" data-id="${escapeHtml(n.id)}"
                     transform="translate(${n.layout.x},${n.layout.y})">
                  <rect class="wf-node-box" width="${NODE_W}" height="${NODE_H}" rx="8"
                        fill="${escapeHtml(spec.color)}22" stroke="${escapeHtml(spec.color)}" stroke-width="${selected ? 2.5 : 1.5}"/>
                  <text class="wf-node-icon" x="12" y="${NODE_H/2 + 6}" font-size="18">${escapeHtml(spec.icon || '?')}</text>
                  <text class="wf-node-type" x="38" y="22" font-size="11" fill="#8b949e">${escapeHtml(spec.label || n.type)}</text>
                  <text class="wf-node-id"   x="38" y="42" font-size="13" fill="#c9d1d9">${escapeHtml(n.id)}</text>
                </g>`;
    }).join('');

    svg.innerHTML = edgeSvg + nodeSvg;

    svg.querySelectorAll('.wf-node').forEach(g => {
        g.addEventListener('click', (e) => {
            e.stopPropagation();
            selectNode(g.dataset.id);
        });
    });
    svg.addEventListener('click', (e) => {
        if (e.target === svg) selectNode(null);
    }, { once: true });
}

function renderInspector() {
    const el = document.getElementById('wf-inspector-body');
    if (!el) return;
    if (!state.workflow) {
        el.innerHTML = '<div class="loading-placeholder">Select a workflow.</div>';
        return;
    }
    if (!state.selectedId) {
        el.innerHTML = '<div class="loading-placeholder">Select a node to edit its config.</div>';
        return;
    }
    const node = state.workflow.nodes.find(n => n.id === state.selectedId);
    if (!node) { el.innerHTML = '<div class="error-box">Selected node not found.</div>'; return; }
    const spec = state.nodeSpecs[node.type];
    if (!spec) { el.innerHTML = `<div class="error-box">Unknown node type: ${escapeHtml(node.type)}</div>`; return; }

    const fields = (spec.fields || []).map(f => {
        const val = node.config?.[f.name] ?? f.default ?? '';
        return renderField(f, val);
    }).join('');

    el.innerHTML = `
      <div class="wf-insp-header">
        <span class="wf-insp-icon" style="color:${escapeHtml(spec.color)}">${escapeHtml(spec.icon)}</span>
        <strong>${escapeHtml(spec.label)}</strong>
      </div>
      <div class="wf-insp-row">
        <label class="param-label" for="wf-insp-id">Node ID</label>
        <input id="wf-insp-id" class="param-input" value="${escapeHtml(node.id)}" data-field="__id">
      </div>
      ${fields}
      <div class="wf-insp-actions">
        <button id="wf-insp-delete" class="btn-small">Delete node</button>
      </div>
    `;
    el.querySelectorAll('[data-field]').forEach(input => {
        const handler = () => updateField(node, input);
        input.addEventListener('change', handler);
        input.addEventListener('blur',   handler);
    });
    document.getElementById('wf-insp-delete')?.addEventListener('click', () => deleteNode(node.id));
}

function renderField(f, val) {
    const id = `wf-f-${f.name}`;
    const lbl = `<label class="param-label" for="${id}">${escapeHtml(f.label)}${f.required ? ' *' : ''}</label>`;
    const help = f.help ? `<div class="param-subtitle">${escapeHtml(f.help)}</div>` : '';
    let input;
    switch (f.type) {
        case 'textarea':
            input = `<textarea id="${id}" class="param-textarea" rows="3" data-field="${escapeHtml(f.name)}">${escapeHtml(val)}</textarea>`;
            break;
        case 'number':
            input = `<input id="${id}" type="number" class="param-input" value="${escapeHtml(val)}" data-field="${escapeHtml(f.name)}">`;
            break;
        case 'toggle':
            input = `<input id="${id}" type="checkbox" ${val ? 'checked' : ''} data-field="${escapeHtml(f.name)}">`;
            break;
        case 'code':
            input = `<textarea id="${id}" class="param-textarea wf-field-code" rows="2" data-field="${escapeHtml(f.name)}">${escapeHtml(val)}</textarea>`;
            break;
        case 'kvmap': {
            const entries = typeof val === 'object' && val ? Object.entries(val) : [];
            input = `<textarea id="${id}" class="param-textarea wf-field-code" rows="4" placeholder="key: value\nother: {{var}}" data-field="${escapeHtml(f.name)}" data-field-type="kvmap">${escapeHtml(entries.map(([k,v])=>`${k}: ${v}`).join('\n'))}</textarea>`;
            break;
        }
        case 'select':
            // Options resolved at render time if static; dynamic options are kept
            // as free-text in V1 to avoid a second fetch round-trip per field.
            if (Array.isArray(f.options)) {
                input = `<select id="${id}" class="param-select" data-field="${escapeHtml(f.name)}">
                    ${f.options.map(o => `<option value="${escapeHtml(o)}" ${o===val?'selected':''}>${escapeHtml(o)}</option>`).join('')}
                </select>`;
            } else {
                input = `<input id="${id}" class="param-input" value="${escapeHtml(val)}" data-field="${escapeHtml(f.name)}"
                           list="${id}-list" placeholder="${f.options_from ? 'Type or pick…' : ''}">`;
            }
            break;
        default:
            input = `<input id="${id}" class="param-input" value="${escapeHtml(val)}" data-field="${escapeHtml(f.name)}">`;
    }
    return `<div class="wf-insp-row">${lbl}${input}${help}</div>`;
}

function updateField(node, input) {
    const field = input.dataset.field;
    let val;
    if (input.type === 'checkbox')      val = input.checked;
    else if (input.type === 'number')   val = input.value === '' ? null : Number(input.value);
    else                                val = input.value;

    if (field === '__id') {
        const newId = val.trim();
        if (!newId || newId === node.id) return;
        if (state.workflow.nodes.some(n => n.id === newId)) {
            setStatus(`ID "${newId}" already used`, 'error');
            input.value = node.id;
            return;
        }
        // Rewrite edges referencing the old id
        (state.workflow.edges || []).forEach(e => {
            if ((e.from || e.from_id) === node.id) { e.from = newId; delete e.from_id; }
            if ((e.to   || e.to_id)   === node.id) { e.to   = newId; delete e.to_id; }
        });
        node.id = newId;
        state.selectedId = newId;
    } else if (input.dataset.fieldType === 'kvmap') {
        node.config = node.config || {};
        const map = {};
        String(val).split('\n').forEach(line => {
            const idx = line.indexOf(':');
            if (idx < 0) return;
            const k = line.slice(0, idx).trim();
            const v = line.slice(idx + 1).trim();
            if (k) map[k] = v;
        });
        node.config[field] = map;
    } else {
        node.config = node.config || {};
        node.config[field] = val;
    }
    markDirty();
    renderCanvas();
}

// -----------------------------------------------------------------------
// Actions
// -----------------------------------------------------------------------

function newId(type) {
    const prefix = type.replace(/[^a-z0-9]/gi, '_').slice(0, 8) || 'node';
    const used = new Set((state.workflow?.nodes || []).map(n => n.id));
    for (let i = 1; i < 9999; i++) {
        const candidate = `${prefix}_${i}`;
        if (!used.has(candidate)) return candidate;
    }
    return prefix + '_' + Math.random().toString(36).slice(2, 8);
}

function addNode(type) {
    if (!state.workflow) { newWorkflow(); }
    const id = newId(type);
    const node = { id, type, config: {}, layout: {} };
    state.workflow.nodes.push(node);
    ensureLayout(state.workflow);
    state.selectedId = id;
    markDirty();
    renderCanvas();
    renderInspector();
}

function selectNode(id) {
    state.selectedId = id;
    renderCanvas();
    renderInspector();
}

function deleteNode(id) {
    if (!state.workflow) return;
    state.workflow.nodes = state.workflow.nodes.filter(n => n.id !== id);
    state.workflow.edges = (state.workflow.edges || []).filter(
        e => (e.from || e.from_id) !== id && (e.to || e.to_id) !== id
    );
    if (state.selectedId === id) state.selectedId = null;
    markDirty();
    renderCanvas();
    renderInspector();
}

function newWorkflow() {
    state.workflow = {
        id: '',
        name: document.getElementById('wf-name')?.value?.trim() || 'Untitled workflow',
        description: '',
        nodes: [],
        edges: [],
        variables: {},
    };
    state.selectedId = null;
    state.dirty = true;
    const sel = document.getElementById('wf-select');
    if (sel) sel.value = '';
    const name = document.getElementById('wf-name');
    if (name) name.value = state.workflow.name;
    renderCanvas();
    renderInspector();
    setStatus('New workflow — save to persist', 'warn');
}

async function loadList() {
    try {
        const data = await api('GET', '/workflows');
        state.list = data.workflows || [];
    } catch (e) {
        state.list = [];
        setStatus('Failed to load list: ' + e.message, 'error');
    }
    const sel = document.getElementById('wf-select');
    if (!sel) return;
    sel.innerHTML = '<option value="">— pick a workflow —</option>' +
        state.list.map(w => `<option value="${escapeHtml(w.id)}">${escapeHtml(w.name)} (${w.node_count||0} nodes)</option>`).join('');
}

async function loadById(id) {
    if (!id) { state.workflow = null; state.selectedId = null; renderCanvas(); renderInspector(); return; }
    try {
        const data = await api('GET', '/workflows/' + encodeURIComponent(id));
        state.workflow = data.workflow;
        state.workflow.nodes = state.workflow.nodes || [];
        state.workflow.edges = state.workflow.edges || [];
        state.selectedId = null;
        state.dirty = false;
        const name = document.getElementById('wf-name');
        if (name) name.value = state.workflow.name || '';
        renderCanvas();
        renderInspector();
        setStatus('', '');
    } catch (e) {
        setStatus('Load failed: ' + e.message, 'error');
    }
}

async function saveCurrent() {
    if (!state.workflow) { setStatus('Nothing to save', 'error'); return; }
    state.workflow.name = document.getElementById('wf-name')?.value?.trim() || state.workflow.name || 'Untitled';
    try {
        const data = state.workflow.id
            ? await api('PUT', '/workflows/' + encodeURIComponent(state.workflow.id), state.workflow)
            : await api('POST', '/workflows', state.workflow);
        if (data.id) state.workflow.id = data.id;
        state.dirty = false;
        await loadList();
        const sel = document.getElementById('wf-select');
        if (sel) sel.value = state.workflow.id;
        setStatus(`Saved (${state.workflow.nodes.length} nodes)`, 'ok');
    } catch (e) {
        setStatus('Save failed: ' + e.message, 'error');
    }
}

async function deleteCurrent() {
    if (!state.workflow?.id) { setStatus('Nothing saved to delete', 'error'); return; }
    if (!confirm(`Delete workflow "${state.workflow.name}"? This cannot be undone.`)) return;
    try {
        await api('DELETE', '/workflows/' + encodeURIComponent(state.workflow.id));
        state.workflow = null;
        state.selectedId = null;
        await loadList();
        renderCanvas();
        renderInspector();
        setStatus('Deleted', 'ok');
    } catch (e) {
        setStatus('Delete failed: ' + e.message, 'error');
    }
}

async function runCurrent() {
    if (!state.workflow) { setStatus('Nothing to run', 'error'); return; }
    if (state.dirty) { setStatus('Save before running', 'warn'); return; }
    setStatus('Starting run…', '');
    try {
        const data = await api('POST', '/workflows/run', { workflow: state.workflow, initial_input: '' });
        if (data.error) throw new Error(data.error);
        setStatus(`Run ${data.execution_id}: ${data.status}`, data.status === 'completed' ? 'ok' : 'warn');
    } catch (e) {
        setStatus('Run failed: ' + e.message, 'error');
    }
}

async function showExecutions() {
    const card = document.getElementById('wf-execution-card');
    const detail = document.getElementById('wf-execution-detail');
    if (!card || !detail) return;
    card.style.display = 'block';
    detail.innerHTML = '<div class="loading-placeholder">Loading…</div>';
    try {
        const data = await api('GET', '/workflows/executions');
        const execs = data.executions || [];
        if (!execs.length) {
            detail.innerHTML = '<div class="empty-state">No executions yet.</div>';
            return;
        }
        detail.innerHTML = execs.map(e => `
          <div class="exec-item exec-${escapeHtml(e.status)}">
            <span class="exec-id">${escapeHtml(e.execution_id)}</span>
            <span class="badge badge-${escapeHtml(e.status)}">${escapeHtml(e.status)}</span>
            <span>${e.node_count || 0} nodes</span>
          </div>`).join('');
    } catch (err) {
        detail.innerHTML = `<div class="error-box">${escapeHtml(err.message)}</div>`;
    }
}

// -----------------------------------------------------------------------
// Wire up
// -----------------------------------------------------------------------

async function init() {
    // Node specs first — palette needs them before canvas can render.
    try {
        const data = await api('GET', '/workflows/node-specs');
        state.nodeSpecs = data.specs || {};
        state.categories = data.categories || [];
    } catch (e) {
        setStatus('Could not load node specs: ' + e.message, 'error');
    }
    renderPalette();
    await loadList();
    renderCanvas();
    renderInspector();

    document.getElementById('wf-new-btn')?.addEventListener('click', newWorkflow);
    document.getElementById('wf-save-btn')?.addEventListener('click', saveCurrent);
    document.getElementById('wf-delete-btn')?.addEventListener('click', deleteCurrent);
    document.getElementById('wf-run-btn')?.addEventListener('click', runCurrent);
    document.getElementById('wf-executions-btn')?.addEventListener('click', showExecutions);
    document.getElementById('wf-executions-close')?.addEventListener('click', () => {
        const card = document.getElementById('wf-execution-card');
        if (card) card.style.display = 'none';
    });
    document.getElementById('wf-select')?.addEventListener('change', (e) => loadById(e.target.value));
    document.getElementById('wf-name')?.addEventListener('change', () => {
        if (state.workflow) { state.workflow.name = document.getElementById('wf-name').value; markDirty(); }
    });
}

// Expose one hook for app.js to call when the tab becomes active.
// (Avoids running the fetch on page load if the user never opens the tab.)
let initialized = false;
window.__wfEditor = {
    onTabOpen() {
        if (!initialized) { initialized = true; init(); }
    },
};
