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
  if (!metrics.gpu) {
    el.innerHTML = '<p class="param-subtitle">GPU metrics unavailable</p>';
    return;
  }
  const g = metrics.gpu;
  const vramPct = Math.round((g.vram_used_mb / g.vram_total_mb) * 100);
  const vramLabel = `${(g.vram_used_mb / 1024).toFixed(1)}/${(g.vram_total_mb / 1024).toFixed(1)} GB`;
  el.innerHTML = `
    <div class="gpu-name">${escapeHtml(g.name || 'GPU')}</div>
    <div class="gauge-row">
      ${arcGauge(vramPct, 'VRAM', `${vramPct}%`)}
      ${arcGauge(g.utilization_pct || 0, 'Util', `${g.utilization_pct || 0}%`)}
      ${arcGauge(
        g.temperature_c ? Math.min(100, Math.round(g.temperature_c / 100 * 100)) : 0,
        'Temp',
        g.temperature_c ? `${g.temperature_c}°` : '--',
        g.temperature_c > 80 ? 'var(--red,#f85149)' : g.temperature_c > 65 ? 'var(--yellow,#d29922)' : undefined
      )}
    </div>
    <div class="vram-detail">${vramLabel}</div>`;
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

    renderGPUGauges(metrics);

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
    if (modesData.current) {
      const m = modesData.modes[modesData.current] || {};
      infoEl.innerHTML = `Mode: <strong>${modesData.current}</strong> &middot; temp=${m.temperature || '?'} &middot; max_tokens=${m.max_tokens || '?'} &middot; model=${(m.prefer_model || ['any'])[0]}`;
    } else {
      infoEl.textContent = 'No mode active — using default settings';
    }
    if (charsData.current) {
      infoEl.innerHTML += ` &middot; Character: <strong>${charsData.current}</strong>`;
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
