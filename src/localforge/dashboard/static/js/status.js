import { API, authFetch, authHeaders, escapeHtml, escapeAttr, showToast, statusRow, formatUptime } from './api.js';
import { loadMeshStatus } from './mesh.js';

export function arcGauge(pct, label, value, color) {
  const r = 38, cx = 50, cy = 50, sw = 7;
  const c = 2 * Math.PI * r;
  const dash = c * Math.min(pct, 100) / 100;
  const col = color || (pct > 90 ? 'var(--red,#f85149)' : pct > 70 ? 'var(--yellow,#d29922)' : 'var(--green,#3fb950)');
  return `<div class="arc-gauge">
    <svg viewBox="0 0 100 100">
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--border,#30363d)" stroke-width="${sw}" stroke-linecap="round"
        stroke-dasharray="${c * 0.75} ${c * 0.25}" stroke-dashoffset="${-c * 0.125}" />
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${col}" stroke-width="${sw}" stroke-linecap="round"
        stroke-dasharray="${dash * 0.75} ${c}" stroke-dashoffset="${-c * 0.125}" class="arc-fill" />
    </svg>
    <div class="arc-center">
      <div class="arc-value">${value}</div>
      <div class="arc-label">${label}</div>
    </div>
  </div>`;
}

function renderGPUGauges(metrics) {
  const el = document.getElementById('gpu-gauges');
  let html = '';

  if (metrics.gpu) {
    const g = metrics.gpu;
    const vramPct = Math.round((g.vram_used_mb / g.vram_total_mb) * 100);
    const vramLabel = `${(g.vram_used_mb / 1024).toFixed(1)} / ${(g.vram_total_mb / 1024).toFixed(1)} GB`;
    const tempPct = g.temperature_c ? Math.min(100, Math.round(g.temperature_c)) : 0;
    const tempColor = g.temperature_c > 80 ? 'var(--red)' : g.temperature_c > 65 ? 'var(--yellow)' : undefined;
    html += `
      <div class="gpu-name">${escapeHtml(g.name || 'GPU')}</div>
      <div class="gauge-row">
        ${arcGauge(vramPct, 'VRAM', `${vramPct}%`)}
        ${arcGauge(g.utilization_pct || 0, 'GPU%', `${g.utilization_pct || 0}%`)}
        ${arcGauge(tempPct, 'Temp', g.temperature_c ? `${g.temperature_c}°` : '--', tempColor)}
      </div>
      <div class="vram-detail">${vramLabel} VRAM</div>`;
  } else {
    html += '<p class="param-subtitle" style="margin-bottom:8px;">GPU metrics unavailable (nvidia-smi)</p>';
  }

  if (metrics.ram) {
    const r = metrics.ram;
    const ramColor = r.used_pct > 85 ? 'var(--red)' : r.used_pct > 70 ? 'var(--yellow)' : undefined;
    const cpuPct = metrics.cpu?.utilization_pct || 0;
    html += `
      <div class="gpu-name" style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px;">System</div>
      <div class="gauge-row">
        ${arcGauge(r.used_pct, 'RAM', `${r.used_pct}%`, ramColor)}
        ${arcGauge(cpuPct, 'CPU', `${cpuPct}%`)}
      </div>
      <div class="vram-detail">${r.available_gb} GB free / ${r.total_gb} GB RAM</div>`;
  }

  el.innerHTML = html || '<p class="param-subtitle">Metrics unavailable</p>';
}

export async function loadStatus() {
  try {
    const [health, status, metrics] = await Promise.all([
      fetch('/health').then(r => r.json()),
      authFetch(API + '/status').then(r => r.json()),
      authFetch(API + '/metrics').then(r => r.json()).catch(() => ({})),
    ]);

    const badge = document.getElementById('model-badge');
    const modelName = health.model?.model_name || status.model?.name || '--';
    badge.textContent = modelName.replace('.gguf', '').substring(0, 30);

    const gw = document.getElementById('hp-gateway');
    const be = document.getElementById('hp-backend');
    const ll = document.getElementById('hp-litellm');
    gw.className = 'health-pill ' + (health.status === 'ok' ? 'up' : 'down');
    be.className = 'health-pill ' + (health.model?.status === 'loaded' ? 'up' : 'down');
    ll.className = 'health-pill ' + (health.litellm?.status === 'ok' ? 'up' : health.litellm ? 'down' : 'unknown');

    const mc = document.getElementById('status-model');
    const loras = (health.model?.lora_names || []).join(', ');
    mc.innerHTML = `
      <div class="model-card-name">${escapeHtml(modelName.replace('.gguf', ''))}</div>
      ${status.slots ? `<div class="model-card-detail">
        <span>ctx: ${(status.slots.ctx_total || 0).toLocaleString()}</span>
        <span>slots: ${status.slots.active}/${status.slots.total}</span>
        ${status.server_config?.gpu_layers ? `<span>layers: ${status.server_config.gpu_layers}</span>` : ''}
      </div>` : ''}
      ${loras ? `<div class="model-card-detail"><span>LoRA: ${escapeHtml(loras)}</span></div>` : ''}
      <div class="model-card-detail"><span>uptime: ${formatUptime(health.uptime_seconds)}</span></div>`;

    // Show/hide unload button — text-gen-webui always reports "loaded" so check model_name too
    const modelActions = document.getElementById('status-model-actions');
    const hasModel = health.model?.status === 'loaded' && modelName !== '--' && modelName !== 'None';
    if (modelActions) modelActions.style.display = hasModel ? '' : 'none';

    renderGPUGauges(metrics);
    loadStatusModels();

    const si = document.getElementById('status-info');
    let rows = '';
    if (status.slots) {
      rows += statusRow('Context / Slot', status.slots.ctx_per_slot?.toLocaleString() || '--');
    }
    if (status.server_config) {
      const sc = status.server_config;
      if (sc.batch_size) rows += statusRow('Batch Size', sc.batch_size);
      if (sc.flash_attn) rows += statusRow('Flash Attn', sc.flash_attn);
    }
    rows += statusRow('Backend', health.model?.status || 'unknown',
      health.model?.status === 'loaded' ? 'ok' : 'error');
    si.innerHTML = rows;
  } catch (e) {
    document.getElementById('status-info').textContent = 'Failed to load: ' + e.message;
  }
}

export async function loadStatusModels() {
  const sel = document.getElementById('status-model-select');
  if (!sel) return;
  try {
    const data = await authFetch(API + '/models').then(r => r.json());
    const current = data.current || '';
    sel.innerHTML = '<option value="">-- select model --</option>' +
      (data.models || []).map(m => {
        const name = typeof m === 'string' ? m : m.name || m;
        return `<option value="${name}"${name === current ? ' selected' : ''}>${name.replace('.gguf', '').substring(0, 45)}</option>`;
      }).join('');
  } catch (e) {
    sel.innerHTML = '<option value="">Error loading models</option>';
  }
}

export async function scanDiskModels() {
  const btn = document.getElementById('scan-models-btn');
  const el = document.getElementById('scan-models-result');
  const sel = document.getElementById('status-model-select');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
  if (el) el.style.display = 'none';
  try {
    const data = await authFetch(API + '/models/scan').then(r => r.json());
    const found = data.scanned || [];
    if (!found.length) {
      if (el) { el.textContent = 'No GGUF files found in: ' + (data.dirs_checked || []).join(', '); el.style.display = 'block'; }
      return;
    }
    // Merge scanned files into the model select
    if (sel) {
      const existing = new Set(Array.from(sel.options).map(o => o.value));
      found.forEach(m => {
        if (!existing.has(m.name)) {
          const opt = document.createElement('option');
          opt.value = m.name;
          opt.textContent = `${m.name.replace('.gguf', '').substring(0, 42)} (${m.size_gb}GB)`;
          sel.appendChild(opt);
        }
      });
    }
    if (el) {
      el.innerHTML = `Found <strong>${found.length}</strong> GGUF file${found.length !== 1 ? 's' : ''} on disk. ` +
        found.map(m => `<span style="font-family:monospace;font-size:0.8em;">${escapeHtml(m.name)} <span style="color:#8b949e;">${m.size_gb}GB</span></span>`).join(' &middot; ');
      el.style.display = 'block';
    }
  } catch (e) {
    if (el) { el.textContent = 'Scan failed: ' + e.message; el.style.display = 'block'; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '&#x1F4BE; Scan Disk'; }
  }
}

document.getElementById('status-model-load-btn')?.addEventListener('click', async () => {
  const sel = document.getElementById('status-model-select');
  const statusEl = document.getElementById('status-model-load-status');
  const btn = document.getElementById('status-model-load-btn');
  const model = sel?.value;
  if (!model) { if (statusEl) { statusEl.textContent = 'Select a model first.'; statusEl.className = 'config-status config-status-error'; } return; }
  if (!confirm(`Load ${model.replace('.gguf', '')}?`)) return;
  btn.disabled = true; btn.textContent = 'Loading…';
  if (statusEl) { statusEl.textContent = 'Loading…'; statusEl.className = 'config-status'; }
  try {
    const swapBody = { model_name: model };
    const resp = await authFetch(API + '/swap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(swapBody),
    });
    const data = await resp.json();
    if (data.error) {
      if (statusEl) { statusEl.textContent = data.error; statusEl.className = 'config-status config-status-error'; }
      showToast('Swap failed: ' + data.error, 'error');
    } else {
      const hint = data.applied ? `ctx=${data.applied.ctx_size}, gpu=${data.applied.gpu_layers}` : '';
      if (statusEl) { statusEl.textContent = hint ? `Loaded (${hint})` : 'Loaded'; statusEl.className = 'config-status config-status-ok'; }
      showToast(`Loaded: ${model.replace('.gguf', '')}`, 'success');
      await loadStatus();
      await loadStatusModels();
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.className = 'config-status config-status-error'; }
    showToast('Swap error: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Load';
  }
});

document.getElementById('status-unload-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('status-unload-btn');
  if (!confirm('Unload the current model and free VRAM?')) return;
  btn.disabled = true;
  btn.textContent = 'Unloading…';
  try {
    await authFetch(API + '/model/unload', { method: 'POST' });
    showToast('Model unloaded — VRAM freed', 'success');
    await loadStatus();
  } catch (e) {
    showToast('Unload failed: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Unload Model';
  }
});

let _statusInterval = null;
export function startStatusRefresh() {
  if (_statusInterval) return;
  _statusInterval = setInterval(() => {
    const tab = document.querySelector('.tab.active');
    if (tab && tab.dataset.tab === 'status') {
      loadStatus();
      loadMeshStatus();
    }
  }, 30000);
}

export async function loadModes() {
  try {
    const [modesData, charsData] = await Promise.all([
      authFetch(API + '/modes').then(r => r.json()),
      authFetch(API + '/characters').then(r => r.json()),
    ]);

    const modeSel = document.getElementById('mode-select');
    modeSel.innerHTML = '<option value="">(no mode)</option>';
    for (const [key, cfg] of Object.entries(modesData.modes || {})) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = key;
      if (key === modesData.current) opt.selected = true;
      modeSel.appendChild(opt);
    }

    const charSel = document.getElementById('character-select');
    charSel.innerHTML = '<option value="">(no character)</option>';
    for (const [key, cfg] of Object.entries(charsData.characters || {})) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = cfg.name || key;
      if (key === charsData.current) opt.selected = true;
      charSel.appendChild(opt);
    }

    const infoEl = document.getElementById('mode-info');
    const MODE_DESC = {
      development: 'Lower temp, precise — good for code and debugging',
      research: 'Higher context, analytical — good for investigation',
      creative: 'Higher temp, expressive — good for writing',
      review: 'Critical, structured — good for code review',
      ops: 'Concise, action-oriented — good for infra tasks',
      learning: 'Explanatory, patient — good for understanding concepts',
    };
    const CHAR_DESC = {
      'code-reviewer': 'Focused on bugs, correctness, security',
      architect: 'Systems thinking, tradeoffs, design patterns',
      brainstorm: 'Creative, generative, explores possibilities',
      teacher: 'Explains step-by-step, uses analogies',
      devops: 'Infrastructure, reliability, automation',
      security: 'Threat modeling, vulnerabilities, hardening',
    };
    if (modesData.current) {
      const m = modesData.modes[modesData.current] || {};
      const desc = MODE_DESC[modesData.current] || '';
      infoEl.innerHTML = `<strong>${modesData.current}</strong> &middot; temp=${m.temperature || '?'} &middot; max_tokens=${m.max_tokens || '?'}` +
        (desc ? ` <span style="color:#8b949e;">— ${desc}</span>` : '');
    } else {
      infoEl.textContent = 'No mode active — using default generation settings';
    }
    if (charsData.current) {
      const desc = CHAR_DESC[charsData.current] || '';
      infoEl.innerHTML += `<br>Character: <strong>${charsData.current}</strong>` +
        (desc ? ` <span style="color:#8b949e;">— ${desc}</span>` : '');
    }
  } catch (e) {}
}

export function initModeControls() {
  document.getElementById('mode-apply').addEventListener('click', async () => {
    const mode = document.getElementById('mode-select').value;
    const char = document.getElementById('character-select').value;
    try {
      await authFetch(API + '/modes/set', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ mode }),
      });
      if (char !== undefined) {
        await authFetch(API + '/characters/set', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ character: char }),
        });
      }
      showToast(mode ? `Mode: ${mode}` : 'Mode cleared');
      loadModes();
    } catch (e) { showToast('Failed to set mode', 'error'); }
  });
}
