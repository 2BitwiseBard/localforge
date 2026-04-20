import { API, authFetch, authHeaders, escapeHtml, escapeAttr, showToast, statusRow } from './api.js';
import { loadStatus } from './status.js';
import { initUser } from './auth.js';

// ---------------------------------------------------------------------------
// Model Swap
// ---------------------------------------------------------------------------
const modelSelect = document.getElementById('model-select');

export async function loadModels() {
  try {
    const resp = await authFetch(API + '/models');
    if (resp.status === 401) { await initUser(); return loadModels(); }
    const data = await resp.json();
    const models = data.models || [];
    const current = data.current || '';
    const buildOpts = (placeholder) =>
      `<option value="">${placeholder}</option>` +
      models.map(m => {
        const name = typeof m === 'string' ? m : m.name || m;
        const label = name.replace('.gguf', '').substring(0, 45);
        return `<option value="${name}"${name === current ? ' selected' : ''}>${label}</option>`;
      }).join('');
    modelSelect.innerHTML = buildOpts('-- switch model --');
    const tabSel = document.getElementById('model-tab-select');
    if (tabSel) tabSel.innerHTML = buildOpts('-- select model --');
    updateModelBadge(current);
  } catch (e) { modelSelect.innerHTML = '<option>Error</option>'; }
}

function collectLoadParams() {
  const body = {};
  const intFields = [
    ['ctrl-ctx-size', 'ctx_size'], ['ctrl-gpu-layers', 'gpu_layers'],
    ['ctrl-threads', 'threads'], ['ctrl-threads-batch', 'threads_batch'],
    ['ctrl-batch-size', 'batch_size'], ['ctrl-ubatch-size', 'ubatch_size'],
    ['ctrl-rope-freq-base', 'rope_freq_base'], ['ctrl-parallel', 'parallel'],
    ['ctrl-draft-max', 'draft_max'], ['ctrl-gpu-layers-draft', 'gpu_layers_draft'],
    ['ctrl-ctx-size-draft', 'ctx_size_draft'],
    ['ctrl-ngram-n', 'spec_ngram_size_n'], ['ctrl-ngram-m', 'spec_ngram_size_m'],
    ['ctrl-ngram-hits', 'spec_ngram_min_hits'],
  ];
  for (const [id, key] of intFields) {
    const el = document.getElementById(id);
    if (el && el.value !== '') body[key] = parseInt(el.value);
  }
  const selFields = [
    ['ctrl-cache-type', 'cache_type'], ['ctrl-spec-type', 'spec_type'],
  ];
  for (const [id, key] of selFields) {
    const el = document.getElementById(id);
    if (el && el.value) body[key] = el.value;
  }
  const faEl = document.getElementById('ctrl-flash-attn');
  if (faEl && faEl.value) body.flash_attn = faEl.value === 'true';
  const tsEl = document.getElementById('ctrl-tensor-split');
  if (tsEl && tsEl.value.trim()) body.tensor_split = tsEl.value.trim();
  const dmEl = document.getElementById('ctrl-model-draft');
  if (dmEl && dmEl.value) body.model_draft = dmEl.value;
  return body;
}

modelSelect.addEventListener('change', async () => {
  const model = modelSelect.value;
  if (!model) return;

  try {
    const cfgData = await authFetch(API + '/models/config?model=' + encodeURIComponent(model)).then(r => r.json());
    const cfg = cfgData.config || {};
    if (cfg.ctx_size) document.getElementById('ctrl-ctx-size').value = cfg.ctx_size;
    if (cfg.gpu_layers != null) document.getElementById('ctrl-gpu-layers').value = cfg.gpu_layers;
    if (cfg.flash_attn != null) document.getElementById('ctrl-flash-attn').value = String(cfg.flash_attn);
    if (cfg.cache_type) document.getElementById('ctrl-cache-type').value = cfg.cache_type;
    if (cfg.parallel) document.getElementById('ctrl-parallel').value = cfg.parallel;
    if (cfgData.matched_pattern) {
      showToast(`Config loaded: ${cfgData.matched_pattern} (ctx=${cfg.ctx_size || 'default'}, gpu=${cfg.gpu_layers || 'all'})`);
    }
  } catch (e) {}

  if (!confirm(`Swap to ${model.replace('.gguf', '')}?`)) { modelSelect.value = ''; return; }
  const badge = document.getElementById('model-badge');
  badge.textContent = 'Loading...'; badge.style.background = 'var(--yellow)';
  try {
    const swapBody = { model_name: model, ...collectLoadParams() };
    const resp = await authFetch(API + '/swap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(swapBody),
    });
    const data = await resp.json();
    if (data.error) { showToast('Swap error: ' + data.error, 'error'); }
    else if (data.applied) { showToast(`Loaded: ctx=${data.applied.ctx_size}, gpu=${data.applied.gpu_layers}`); }
  } catch (e) { showToast('Swap error: ' + e.message, 'error'); }
  badge.style.background = ''; loadStatus(); loadModels();
});

// Model tab select — pre-fill ctx/gpu/flash/cache from config.yaml on selection, no auto-swap
document.getElementById('model-tab-select')?.addEventListener('change', async () => {
  const model = document.getElementById('model-tab-select').value;
  if (!model) return;
  try {
    const cfgData = await authFetch(API + '/models/config?model=' + encodeURIComponent(model)).then(r => r.json());
    const cfg = cfgData.config || {};
    if (cfg.ctx_size) document.getElementById('ctrl-ctx-size').value = cfg.ctx_size;
    if (cfg.gpu_layers != null) document.getElementById('ctrl-gpu-layers').value = cfg.gpu_layers;
    if (cfg.flash_attn != null) document.getElementById('ctrl-flash-attn').value = String(cfg.flash_attn);
    if (cfg.cache_type) document.getElementById('ctrl-cache-type').value = cfg.cache_type;
    if (cfg.parallel) document.getElementById('ctrl-parallel').value = cfg.parallel;
    if (cfgData.matched_pattern) {
      showToast(`Config: ${cfgData.matched_pattern} — ctx=${cfg.ctx_size || 'default'}, gpu=${cfg.gpu_layers ?? 'all'}`);
    }
  } catch (e) {}
});

document.getElementById('model-load-btn')?.addEventListener('click', async () => {
  const sel = document.getElementById('model-tab-select');
  const st = document.getElementById('model-load-status');
  const btn = document.getElementById('model-load-btn');
  const model = sel?.value;
  if (!model) {
    if (st) { st.textContent = 'Select a model first.'; st.className = 'config-status config-status-error'; }
    return;
  }
  if (!confirm(`Load ${model.replace('.gguf', '')}?`)) return;
  btn.disabled = true; btn.textContent = 'Loading\u2026';
  if (st) { st.textContent = 'Loading\u2026'; st.className = 'config-status'; }
  try {
    const resp = await authFetch(API + '/swap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ model_name: model, ...collectLoadParams() }),
    });
    const data = await resp.json();
    if (data.error) {
      if (st) { st.textContent = data.error; st.className = 'config-status config-status-error'; }
    } else {
      const hint = data.applied ? `ctx=${data.applied.ctx_size}, gpu=${data.applied.gpu_layers}` : '';
      if (st) { st.textContent = hint ? `Loaded (${hint})` : 'Loaded'; st.className = 'config-status config-status-ok'; }
      showToast(`Loaded: ${model.replace('.gguf', '')}`, 'success');
      updateModelBadge(model); loadModels(); loadStatus();
    }
  } catch (e) {
    if (st) { st.textContent = 'Error: ' + e.message; st.className = 'config-status config-status-error'; }
  } finally {
    btn.disabled = false; btn.textContent = 'Load Model';
  }
});

document.getElementById('model-unload-btn')?.addEventListener('click', async () => {
  if (!confirm('Unload current model? This will free VRAM.')) return;
  const btn = document.getElementById('model-unload-btn');
  const st = document.getElementById('model-load-status');
  btn.disabled = true;
  try {
    await authFetch(API + '/model/unload', { method: 'POST' });
    showToast('Model unloaded \u2014 VRAM freed', 'success');
    if (st) { st.textContent = 'Unloaded'; st.className = 'config-status config-status-ok'; }
    updateModelBadge(''); loadStatus(); loadModels();
  } catch (e) {
    showToast('Unload failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
  }
});

function updateModelBadge(name) {
  const el = document.getElementById('model-current-badge');
  if (!el) return;
  el.textContent = name ? name.replace('.gguf', '') : '';
  el.style.display = name ? '' : 'none';
}

// ---------------------------------------------------------------------------
// Generation Parameters
// ---------------------------------------------------------------------------
document.querySelectorAll('.param-row input[type="range"]').forEach(slider => {
  const valEl = slider.closest('.param-row').querySelector('.param-value');
  if (valEl) slider.addEventListener('input', () => { valEl.textContent = slider.value; });
});

export async function loadGenParams() {
  try {
    const data = await authFetch(API + '/generation-params').then(r => r.json());
    const mapping = {
      'param-temp': 'temperature', 'param-top-p': 'top_p', 'param-min-p': 'min_p',
      'param-top-k': 'top_k', 'param-rep-pen': 'repetition_penalty',
      'param-max-tokens': 'max_tokens', 'param-seed': 'seed',
    };
    for (const [elId, key] of Object.entries(mapping)) {
      const el = document.getElementById(elId);
      if (el && data[key] !== undefined && data[key] !== null) el.value = data[key];
    }
    const thinkEl = document.getElementById('param-thinking');
    if (thinkEl && data.enable_thinking != null) thinkEl.checked = !!data.enable_thinking;
    document.querySelectorAll('.param-row input[type="range"]').forEach(s => {
      const v = s.closest('.param-row').querySelector('.param-value');
      if (v) v.textContent = s.value;
    });
  } catch (e) {}
}

document.getElementById('params-apply').addEventListener('click', async () => {
  const st = document.getElementById('params-status');
  st.textContent = 'Applying...'; st.className = 'config-status';
  const params = {
    temperature: parseFloat(document.getElementById('param-temp').value),
    top_p: parseFloat(document.getElementById('param-top-p').value),
    min_p: parseFloat(document.getElementById('param-min-p').value),
    top_k: parseInt(document.getElementById('param-top-k').value),
    repetition_penalty: parseFloat(document.getElementById('param-rep-pen').value),
    max_tokens: parseInt(document.getElementById('param-max-tokens').value),
  };
  const seed = parseInt(document.getElementById('param-seed').value);
  if (seed >= 0) params.seed = seed;
  const thinkEl = document.getElementById('param-thinking');
  if (thinkEl) params.enable_thinking = thinkEl.checked;
  try {
    const d = await authFetch(API + '/generation-params', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(params),
    }).then(r => r.json());
    if (d.error) { st.textContent = d.error; st.className = 'config-status config-status-error'; }
    else { st.textContent = 'Applied!'; st.className = 'config-status config-status-ok'; setTimeout(() => { st.textContent = ''; }, 3000); }
  } catch (e) { st.textContent = 'Error: ' + e.message; st.className = 'config-status config-status-error'; }
});

document.getElementById('params-reset').addEventListener('click', async () => {
  try {
    await authFetch(API + '/generation-params', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ reset: true }),
    });
    loadGenParams(); showToast('Params reset to defaults');
  } catch (e) { showToast('Reset failed', 'error'); }
});

// ---------------------------------------------------------------------------
// Presets
// ---------------------------------------------------------------------------
export async function loadPresets() {
  try {
    const data = await authFetch(API + '/presets').then(r => r.json());
    const el = document.getElementById('preset-select');
    el.innerHTML = '<option value="">-- select preset --</option>';
    (data.presets || []).forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.name;
      let hint = '';
      if (p.temperature != null) hint += ` T=${p.temperature}`;
      if (p.top_p != null) hint += ` P=${p.top_p}`;
      opt.textContent = p.name + (hint ? ` (${hint.trim()})` : '');
      el.appendChild(opt);
    });
  } catch (e) {}
}

document.getElementById('preset-load').addEventListener('click', async () => {
  const name = document.getElementById('preset-select').value;
  if (!name) { showToast('Select a preset', 'error'); return; }
  try {
    const d = await authFetch(API + '/presets/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ preset_name: name }),
    }).then(r => r.json());
    if (d.error) showToast('Error: ' + d.error, 'error');
    else { showToast('Preset loaded: ' + name); loadGenParams(); }
  } catch (e) { showToast('Failed', 'error'); }
});

// ---------------------------------------------------------------------------
// Model Controls
// ---------------------------------------------------------------------------
document.getElementById('model-unload')?.addEventListener('click', async () => {
  if (!confirm('Unload current model? This will free VRAM.')) return;
  try {
    await authFetch(API + '/model/unload', { method: 'POST' });
    showToast('Model unloaded'); loadStatus(); loadModels();
  } catch (e) { showToast('Unload failed: ' + e.message, 'error'); }
});

document.getElementById('run-benchmark').addEventListener('click', async () => {
  const el = document.getElementById('benchmark-result');
  el.textContent = 'Running...'; el.className = 'config-status';
  try {
    const d = await authFetch(API + '/benchmark', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ prompt_length: 'short' }),
    }).then(r => r.json());
    if (d.error) { el.textContent = d.error; el.className = 'config-status config-status-error'; }
    else {
      const text = typeof d.result === 'string' ? d.result : JSON.stringify(d.result);
      el.textContent = text.substring(0, 200); el.className = 'config-status config-status-ok';
    }
  } catch (e) { el.textContent = 'Error'; el.className = 'config-status config-status-error'; }
});

// ---------------------------------------------------------------------------
// Loading Parameters
// ---------------------------------------------------------------------------
async function loadDraftModels() {
  const sel = document.getElementById('ctrl-model-draft');
  if (!sel) return;
  try {
    const data = await authFetch(API + '/models').then(r => r.json());
    const current = sel.value;
    sel.innerHTML = '<option value="">none</option>';
    (data.models || []).forEach(m => {
      const name = typeof m === 'string' ? m : m.name || m;
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name.replace('.gguf', '').substring(0, 45);
      if (name === current) opt.selected = true;
      sel.appendChild(opt);
    });
  } catch (e) {}
}

document.getElementById('load-params-clear').addEventListener('click', () => {
  const numberIds = [
    'ctrl-ctx-size', 'ctrl-gpu-layers', 'ctrl-threads', 'ctrl-threads-batch',
    'ctrl-batch-size', 'ctrl-ubatch-size', 'ctrl-rope-freq-base', 'ctrl-parallel',
    'ctrl-draft-max', 'ctrl-gpu-layers-draft', 'ctrl-ctx-size-draft',
    'ctrl-ngram-n', 'ctrl-ngram-m', 'ctrl-ngram-hits',
  ];
  for (const id of numberIds) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  }
  const selectIds = ['ctrl-cache-type', 'ctrl-flash-attn', 'ctrl-spec-type', 'ctrl-model-draft'];
  for (const id of selectIds) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  }
  const tsEl = document.getElementById('ctrl-tensor-split');
  if (tsEl) tsEl.value = '';
  showToast('Loading parameters cleared');
});

document.getElementById('ctrl-spec-type').addEventListener('change', () => {
  const specType = document.getElementById('ctrl-spec-type').value;
  const draftFields = ['ctrl-model-draft', 'ctrl-draft-max', 'ctrl-gpu-layers-draft', 'ctrl-ctx-size-draft'];
  const ngramFields = ['ctrl-ngram-n', 'ctrl-ngram-m', 'ctrl-ngram-hits'];
  for (const id of draftFields) {
    const row = document.getElementById(id)?.closest('.param-row');
    if (row) row.style.display = specType === 'draft' ? '' : 'none';
  }
  for (const id of ngramFields) {
    const row = document.getElementById(id)?.closest('.param-row');
    if (row) row.style.display = specType === 'ngram' ? '' : 'none';
  }
});

export function initLoadParams() {
  document.getElementById('ctrl-spec-type').dispatchEvent(new Event('change'));
  loadDraftModels();
}

// ---------------------------------------------------------------------------
// LoRA Management
// ---------------------------------------------------------------------------
export async function loadLoras() {
  try {
    const data = await authFetch(API + '/loras').then(r => r.json());
    const info = document.getElementById('lora-info');
    const list = document.getElementById('lora-list');
    const loaded = data.loaded || [];
    info.innerHTML = loaded.length
      ? statusRow('Loaded', loaded.join(', '), 'ok')
      : statusRow('Loaded', 'none');
    if (data.available?.length) {
      list.innerHTML = data.available.map(name => {
        const isLoaded = loaded.includes(name);
        return `<div class="lora-item${isLoaded ? ' lora-loaded' : ''}">
          <span>${escapeHtml(name)}</span>
          ${isLoaded
            ? '<span class="badge" style="background:var(--green);font-size:0.65rem">active</span>'
            : `<button class="btn-small lora-load-btn" data-name="${escapeAttr(name)}">Load</button>`}
        </div>`;
      }).join('');
      list.querySelectorAll('.lora-load-btn').forEach(btn => {
        btn.addEventListener('click', () => loadSingleLora(btn.dataset.name));
      });
    } else {
      list.innerHTML = '<div class="empty-state">No LoRA adapters found</div>';
    }
  } catch (e) { document.getElementById('lora-info').textContent = 'Failed to load'; }
}

async function loadSingleLora(name) {
  showToast('Loading LoRA: ' + name + '...');
  try {
    const d = await authFetch(API + '/loras/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ lora_names: [name] }),
    }).then(r => r.json());
    if (d.error) showToast('Error: ' + d.error, 'error');
    else { showToast('LoRA loaded: ' + name); loadLoras(); loadStatus(); }
  } catch (e) { showToast('Failed', 'error'); }
}

document.getElementById('lora-unload-all').addEventListener('click', async () => {
  try {
    await authFetch(API + '/loras/unload', { method: 'POST' });
    showToast('LoRAs unloaded'); loadLoras(); loadStatus();
  } catch (e) { showToast('Failed', 'error'); }
});

document.getElementById('lora-refresh-btn')?.addEventListener('click', () => loadLoras());

// ---------------------------------------------------------------------------
// Model Sync
// ---------------------------------------------------------------------------
document.getElementById('sync-models-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('sync-models-btn');
  const status = document.getElementById('sync-models-status');
  const result = document.getElementById('sync-models-result-raw');
  btn.disabled = true;
  status.textContent = 'Syncing...';
  result.style.display = 'none';
  try {
    const resp = await authFetch('/api/sync-models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clean: true }),
    });
    const data = await resp.json();
    result.textContent = data.result || data.error || 'Done';
    result.style.display = 'block';
    status.textContent = '';
    loadModels();
    showToast('Models synced', 'success');
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Startup Model
// ---------------------------------------------------------------------------
export async function loadStartupConfig() {
  try {
    const data = await authFetch(API + '/config/startup').then(r => r.json());
    const model = data.startup_model || '';
    const chk = document.getElementById('startup-model-enabled');
    const row = document.getElementById('startup-model-row');
    if (chk) chk.checked = !!model;
    if (row) row.style.display = model ? '' : 'none';
    const sel = document.getElementById('startup-model-select');
    if (sel) {
      try {
        const md = await authFetch(API + '/models').then(r => r.json());
        sel.innerHTML = '<option value="">-- select model --</option>' +
          (md.models || []).map(m => {
            const name = typeof m === 'string' ? m : m.name || m;
            return `<option value="${name}"${name === model ? ' selected' : ''}>${name.replace('.gguf', '')}</option>`;
          }).join('');
      } catch {}
    }
  } catch (e) {
    const st = document.getElementById('startup-status');
    if (st) st.textContent = 'Error loading startup config';
  }
}

document.getElementById('startup-model-enabled')?.addEventListener('change', function () {
  const row = document.getElementById('startup-model-row');
  if (row) row.style.display = this.checked ? '' : 'none';
});

document.getElementById('startup-save-btn')?.addEventListener('click', async () => {
  const chk = document.getElementById('startup-model-enabled');
  const sel = document.getElementById('startup-model-select');
  const st = document.getElementById('startup-status');
  const model = chk?.checked ? (sel?.value || '') : '';
  try {
    await authFetch(API + '/config/startup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ startup_model: model }),
    });
    if (st) st.textContent = model ? `Will load ${model.replace('.gguf', '')} on next boot` : 'No auto-load on boot';
    showToast('Startup setting saved', 'success');
  } catch (e) {
    if (st) st.textContent = 'Error: ' + e.message;
  }
});
