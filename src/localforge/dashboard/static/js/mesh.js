import { API, authFetch, escapeHtml, escapeAttr, showToast } from './api.js';

const PLATFORM_ICONS = {
  linux: '\u{1F427}',
  darwin: '\uF8FF',
  win32: '\u{1FA9F}',
  android: '\u{1F916}',
  unknown: '\u2753',
};

function platformGlyph(p) {
  return PLATFORM_ICONS[p] || PLATFORM_ICONS.unknown;
}

function meshStatusPill(w) {
  if (typeof w.status === 'string' && w.status.startsWith('registered')) {
    return '<span class="status-pill pending">pending</span>';
  }
  if (w.healthy === false) return '<span class="status-pill error">down</span>';
  const age = w.heartbeat_age_s;
  if (typeof age === 'number' && age > 60) return '<span class="status-pill warn">stale</span>';
  return '<span class="status-pill ok">online</span>';
}

function meshStatusClass(w) {
  if (w.offline) return 'offline';
  if (typeof w.heartbeat_age_s === 'number' && w.heartbeat_age_s > 60) return 'stale';
  return 'online';
}

let _meshCache = { workers: [], fetchedAt: 0 };

export async function fetchMeshWorkers() {
  try {
    const data = await authFetch(API + '/mesh/status').then(r => r.json());
    _meshCache = { workers: data.workers || [], fetchedAt: Date.now() };
    return _meshCache.workers;
  } catch (e) {
    return null;
  }
}

function _fmtHeartbeat(w) {
  if (typeof w.heartbeat_age_s === 'number') return `${w.heartbeat_age_s}s ago`;
  if (w.last_seen) return `${Math.round((Date.now() / 1000) - w.last_seen)}s ago`;
  return '\u2014';
}

export async function loadMeshStatus() {
  const el = document.getElementById('mesh-summary');
  if (!el) return;
  const workers = await fetchMeshWorkers();
  if (workers === null) { el.textContent = 'Mesh status unavailable'; return; }
  if (workers.length === 0) {
    el.innerHTML = `<div class="empty-state">
      No workers yet. Open the <a href="#" data-tab="mesh">Mesh</a> tab to enroll one.
    </div>`;
    return;
  }
  const online = workers.filter(w => !w.offline && (typeof w.heartbeat_age_s !== 'number' || w.heartbeat_age_s < 60)).length;
  const chips = workers.slice(0, 6).map(w => {
    const platform = w.platform || (w.capabilities?.platform) || 'unknown';
    const name = w.hostname || w.worker_id || 'unknown';
    return `<span class="mesh-chip" title="${name}">${platformGlyph(platform)} ${name}</span>`;
  }).join('');
  el.innerHTML = `
    <div class="mesh-summary-row">
      <div class="stat-tile">
        <div class="stat-tile-num">${online}/${workers.length}</div>
        <div class="stat-tile-label">workers online</div>
      </div>
      <div class="mesh-chips">${chips}</div>
    </div>`;
}

export async function loadMeshTab() {
  const el = document.getElementById('mesh-workers-table');
  if (!el) return;
  el.innerHTML = '<div class="loading-placeholder">Loading workers&hellip;</div>';
  const workers = await fetchMeshWorkers();
  if (workers === null) { el.textContent = 'Mesh status unavailable'; return; }
  if (workers.length === 0) {
    el.innerHTML = `<div class="empty-state">
      No worker nodes yet. Click <strong>+ Add Node</strong> to enroll one.
    </div>`;
    renderMeshTopology([]);
    return;
  }
  const rows = workers.map(w => {
    const caps = w.capabilities || {};
    const vram = caps.vram_mb ? `${(caps.vram_mb / 1024).toFixed(1)} GB` : '\u2014';
    const tasks = (typeof w.active_tasks === 'number')
      ? `${w.active_tasks}/${(w.stats?.tasks_completed || 0)}`
      : '\u2014';
    const name = w.hostname || w.worker_id || 'unknown';
    const platform = w.platform || caps.platform || 'unknown';
    const nickname = (w.config?.nickname) ? ` <span class="mesh-nickname">(${escapeHtml(w.config.nickname)})</span>` : '';
    return `<tr class="mesh-row" data-worker-id="${encodeURIComponent(w.worker_id)}">
      <td><span class="platform-icon" title="${platform}">${platformGlyph(platform)}</span> ${escapeHtml(name)}${nickname}</td>
      <td>${w.tier || caps.tier || '\u2014'}</td>
      <td>${vram}</td>
      <td>${tasks}</td>
      <td>${_fmtHeartbeat(w)}</td>
      <td class="col-status">${meshStatusPill(w)}</td>
      <td><button class="btn-small mesh-detail-btn" data-worker-id="${encodeURIComponent(w.worker_id)}">Details</button></td>
    </tr>`;
  }).join('');
  el.innerHTML = `<table class="mesh-table mesh-table-full">
    <thead>
      <tr><th>Node</th><th>Tier</th><th>VRAM</th><th>Tasks</th><th>Heartbeat</th><th>Status</th><th></th></tr>
    </thead>
    <tbody>${rows}</tbody>
  </table>`;
  renderMeshTopology(workers);
}

function renderMeshTopology(workers) {
  const svg = document.getElementById('mesh-topology');
  if (!svg) return;
  const cx = 300, cy = 160, rHub = 28;
  if (!workers.length) {
    svg.innerHTML = `<circle cx="${cx}" cy="${cy}" r="${rHub}" class="topo-hub"/>
      <text x="${cx}" y="${cy + 4}" text-anchor="middle" class="topo-hub-label">hub</text>
      <text x="${cx}" y="${cy + 70}" text-anchor="middle" class="topo-empty">No workers connected</text>`;
    return;
  }
  const radius = Math.min(120 + workers.length * 6, 240);
  const count = workers.length;
  const parts = [];
  workers.forEach((w, i) => {
    const angle = (i / count) * Math.PI * 2 - Math.PI / 2;
    const x = cx + Math.cos(angle) * radius;
    const y = cy + Math.sin(angle) * radius;
    const status = meshStatusClass(w);
    parts.push(`<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}" class="topo-edge topo-edge-${status}"/>`);
  });
  parts.push(`<circle cx="${cx}" cy="${cy}" r="${rHub}" class="topo-hub"/>`);
  parts.push(`<text x="${cx}" y="${cy + 4}" text-anchor="middle" class="topo-hub-label">hub</text>`);
  workers.forEach((w, i) => {
    const angle = (i / count) * Math.PI * 2 - Math.PI / 2;
    const x = cx + Math.cos(angle) * radius;
    const y = cy + Math.sin(angle) * radius;
    const status = meshStatusClass(w);
    const name = (w.config?.nickname) || w.hostname || w.worker_id || 'node';
    const platform = w.platform || (w.capabilities?.platform) || 'unknown';
    const short = name.length > 14 ? name.slice(0, 13) + '\u2026' : name;
    parts.push(`<g class="topo-node topo-node-${status}" data-worker-id="${encodeURIComponent(w.worker_id)}">
      <circle cx="${x}" cy="${y}" r="22"/>
      <text x="${x}" y="${y + 4}" text-anchor="middle" class="topo-node-glyph">${platformGlyph(platform)}</text>
      <text x="${x}" y="${y + 38}" text-anchor="middle" class="topo-node-label">${escapeHtml(short)}</text>
    </g>`);
  });
  svg.innerHTML = parts.join('');
}

// ---------------------------------------------------------------------------
// Worker detail drawer
// ---------------------------------------------------------------------------
async function openMeshNodeDrawer(workerId) {
  const drawer = document.getElementById('mesh-node-drawer');
  const backdrop = document.getElementById('mesh-node-drawer-backdrop');
  const body = document.getElementById('mesh-node-drawer-body');
  if (!drawer || !body) return;
  drawer.hidden = false;
  drawer.setAttribute('aria-hidden', 'false');
  if (backdrop) backdrop.hidden = false;
  body.innerHTML = '<div class="loading-placeholder">Loading worker details&hellip;</div>';
  try {
    const w = await authFetch(`${API}/mesh/workers/${encodeURIComponent(workerId)}`).then(r => {
      if (!r.ok) throw new Error('not found');
      return r.json();
    });
    renderMeshNodeDrawer(w);
  } catch (e) {
    body.innerHTML = `<div class="error-box">Failed to load: ${escapeHtml(e.message || 'unknown error')}</div>`;
  }
}

function closeMeshNodeDrawer() {
  const drawer = document.getElementById('mesh-node-drawer');
  const backdrop = document.getElementById('mesh-node-drawer-backdrop');
  if (drawer) { drawer.hidden = true; drawer.setAttribute('aria-hidden', 'true'); }
  if (backdrop) backdrop.hidden = true;
}

function renderMeshNodeDrawer(w) {
  const body = document.getElementById('mesh-node-drawer-body');
  const cfg = w.config || {};
  const hw = w.hardware || {};
  const caps = Array.isArray(cfg.allowed_tasks) ? cfg.allowed_tasks : [];
  const capOptions = ['embeddings', 'rerank', 'classification', 'autocomplete', 'llm_inference', 'vision', 'tts', 'stt'];
  const capBoxes = capOptions.map(c => `
    <label class="checkbox-inline">
      <input type="checkbox" name="allowed_tasks" value="${c}" ${caps.includes(c) ? 'checked' : ''}>
      <span>${c}</span>
    </label>`).join('');
  const isPending = typeof w.status === 'string' && w.status.startsWith('registered');
  const pendingBanner = isPending ? `
    <div class="drawer-section drawer-alert">
      <h4>Worker has not connected yet</h4>
      <p>This node registered but has never sent a heartbeat. The service may not be running.</p>
      <details open>
        <summary>Troubleshooting</summary>
        <ul class="troubleshoot-list">
          ${w.platform === 'win32' ? `
            <li>Check the install log: <code>type %TEMP%\\localforge-setup.log</code></li>
            <li>Check service: <code>%LOCALAPPDATA%\\LocalForge\\nssm.exe status LocalForgeWorker</code></li>
            <li>Check error log: <code>type %LOCALAPPDATA%\\LocalForge\\worker.err.log</code></li>
            <li>Restart: <code>%LOCALAPPDATA%\\LocalForge\\nssm.exe restart LocalForgeWorker</code></li>
          ` : w.platform === 'darwin' ? `
            <li>Check service: <code>launchctl print gui/$(id -u)/com.localforge.worker</code></li>
            <li>Check log: <code>cat ~/Library/Application\\ Support/LocalForge/worker.log</code></li>
          ` : `
            <li>Check service: <code>systemctl --user status localforge-worker</code></li>
            <li>Check log: <code>journalctl --user -u localforge-worker -n 50</code></li>
          `}
          <li>Test from the device: <code>curl http://localhost:8200/health</code></li>
        </ul>
      </details>
      <button type="button" class="btn-small" id="mesh-node-reenroll">Re-enroll (new install command)</button>
    </div>` : '';
  body.innerHTML = `
    ${pendingBanner}
    <div class="drawer-section">
      <div class="drawer-meta">
        <div><strong>Worker ID:</strong> <code>${escapeHtml(w.worker_id)}</code></div>
        <div><strong>Hostname:</strong> ${escapeHtml(w.hostname || '\u2014')}</div>
        <div><strong>Platform:</strong> ${platformGlyph(w.platform)} ${escapeHtml(w.platform || '\u2014')}</div>
        <div><strong>Role:</strong> ${escapeHtml(w.role || 'worker')}</div>
        <div><strong>Enrolled by:</strong> ${escapeHtml(w.enrolled_by || '\u2014')}</div>
        <div><strong>Registered:</strong> ${w.registered_at ? new Date(w.registered_at * 1000).toLocaleString() : '\u2014'}</div>
        <div><strong>Last seen:</strong> ${w.last_seen ? new Date(w.last_seen * 1000).toLocaleString() : '\u2014'}</div>
      </div>
      <details class="drawer-hw">
        <summary>Hardware</summary>
        <pre>${escapeHtml(JSON.stringify(hw, null, 2))}</pre>
      </details>
    </div>
    ${!isPending ? '<div class="drawer-section" id="mesh-node-models"><div class="loading-placeholder">Loading models&hellip;</div></div>' : ''}
    <form class="drawer-section" id="mesh-node-config-form" data-worker-id="${encodeURIComponent(w.worker_id)}">
      <h4>Configuration</h4>
      <label class="param-label">Nickname</label>
      <input type="text" name="nickname" maxlength="60" value="${escapeHtml(cfg.nickname || '')}" placeholder="friendly name">

      <label class="param-label">Allowed tasks</label>
      <div class="checkbox-grid">${capBoxes}</div>

      <div class="drawer-grid">
        <div>
          <label class="param-label">Priority <small>(lower = preferred)</small></label>
          <input type="number" name="priority" min="0" max="100" value="${cfg.priority ?? 50}">
        </div>
        <div>
          <label class="param-label">Max concurrent</label>
          <input type="number" name="max_concurrent" min="1" max="16" value="${cfg.max_concurrent ?? 1}">
        </div>
        <div>
          <label class="param-label">Min battery %</label>
          <input type="number" name="min_battery_pct" min="0" max="100" value="${cfg.min_battery_pct ?? 25}">
        </div>
      </div>
      <div class="drawer-actions">
        <button type="submit" class="btn-primary">Save config</button>
        <button type="button" class="btn-small danger" id="mesh-node-revoke">Revoke worker</button>
        <span id="mesh-node-save-status" class="param-subtitle"></span>
      </div>
    </form>`;

  document.getElementById('mesh-node-config-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    await saveMeshNodeConfig(w.worker_id, e.currentTarget);
  });
  document.getElementById('mesh-node-revoke').addEventListener('click', async () => {
    if (!confirm(`Revoke worker "${w.worker_id}"? It will need to re-enroll.`)) return;
    try {
      const r = await authFetch(`${API}/mesh/workers/revoke`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ worker_id: w.worker_id }),
      });
      if (!r.ok) throw new Error(await r.text());
      closeMeshNodeDrawer();
      loadMeshTab();
    } catch (err) {
      alert('Revoke failed: ' + err.message);
    }
  });
  const reenrollBtn = document.getElementById('mesh-node-reenroll');
  if (reenrollBtn) {
    reenrollBtn.addEventListener('click', async () => {
      reenrollBtn.disabled = true;
      reenrollBtn.textContent = 'Minting token\u2026';
      try {
        const r = await authFetch(`${API}/mesh/enrollment-token`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ note: `re-enroll ${w.hostname || w.worker_id}` }),
        });
        if (!r.ok) throw new Error(await r.text());
        const data = await r.json();
        const platform = w.platform || 'linux';
        const cmd = data.install_commands?.[platform] || 'No command available';
        reenrollBtn.replaceWith(Object.assign(document.createElement('div'), {
          className: 'reenroll-result',
          innerHTML: `<label class="param-label">Run this on the device:</label>
            <div class="code-copy-wrap">
              <pre class="code-block">${escapeHtml(cmd)}</pre>
              <button class="btn-small copy-btn" onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent)">Copy</button>
            </div>
            <p class="param-subtitle">Token expires in ${data.ttl_seconds || 600}s. Revoke the old entry first if re-installing.</p>`,
        }));
      } catch (err) {
        reenrollBtn.disabled = false;
        reenrollBtn.textContent = 'Re-enroll (new install command)';
        alert('Failed: ' + err.message);
      }
    });
  }
  if (!isPending) loadWorkerModels(w.worker_id);
}

async function saveMeshNodeConfig(workerId, form) {
  const fd = new FormData(form);
  const allowed = Array.from(form.querySelectorAll('input[name="allowed_tasks"]:checked')).map(i => i.value);
  const payload = {
    nickname: (fd.get('nickname') || '').toString().trim() || null,
    allowed_tasks: allowed,
    priority: Number(fd.get('priority')),
    max_concurrent: Number(fd.get('max_concurrent')),
    min_battery_pct: Number(fd.get('min_battery_pct')),
  };
  if (!payload.nickname) delete payload.nickname;
  const status = document.getElementById('mesh-node-save-status');
  status.textContent = 'Saving\u2026';
  try {
    const r = await authFetch(`${API}/mesh/workers/${encodeURIComponent(workerId)}/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(await r.text());
    status.textContent = 'Saved.';
    setTimeout(() => status.textContent = '', 2000);
    loadMeshTab();
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  }
}

// ---------------------------------------------------------------------------
// Worker model management
// ---------------------------------------------------------------------------
let _catalogCache = null;

async function fetchCatalog() {
  if (_catalogCache) return _catalogCache;
  const r = await authFetch(`${API}/mesh/models/catalog`);
  if (!r.ok) return null;
  _catalogCache = await r.json();
  return _catalogCache;
}

async function loadWorkerModels(workerId) {
  const container = document.getElementById('mesh-node-models');
  if (!container) return;
  try {
    const [modelsResp, catalog] = await Promise.all([
      authFetch(`${API}/mesh/workers/${encodeURIComponent(workerId)}/models`).then(r => r.ok ? r.json() : null),
      fetchCatalog(),
    ]);
    if (!modelsResp) {
      container.innerHTML = '<h4>Models</h4><p class="param-subtitle">Could not reach worker \u2014 it may still be starting up.</p>';
      return;
    }
    renderWorkerModels(container, workerId, modelsResp, catalog);
  } catch (err) {
    container.innerHTML = `<h4>Models</h4><p class="param-subtitle">Error: ${escapeHtml(err.message)}</p>`;
  }
}

function renderWorkerModels(container, workerId, data, catalog) {
  const active = data.active?.model_name || '';
  const models = data.files || [];
  const catalogModels = catalog?.models || [];
  const onDisk = new Set(models.map(m => m.filename || m));

  const modelRows = models.map(m => {
    const name = typeof m === 'string' ? m : (m.filename || m.name || '');
    const size = m.size_gb ? `${m.size_gb.toFixed(1)} GB` : '';
    const isActive = name === active;
    return `<div class="model-row ${isActive ? 'model-active' : ''}">
      <div class="model-info">
        <span class="model-name">${escapeHtml(name.replace('.gguf', ''))}</span>
        ${size ? `<span class="model-size">${size}</span>` : ''}
        ${isActive ? '<span class="status-pill ok">active</span>' : ''}
      </div>
      ${!isActive ? `<button class="btn-small model-activate-btn" data-filename="${encodeURIComponent(name)}">Activate</button>` : ''}
    </div>`;
  });

  const catalogRows = catalogModels.map(m => {
    const have = onDisk.has(m.filename);
    return `<div class="model-row catalog-row">
      <div class="model-info">
        <span class="model-name">${escapeHtml(m.name)}</span>
        <span class="model-size">${m.size_gb} GB</span>
        <span class="model-tier">${m.tier}</span>
        ${m.tags?.includes('moe') ? '<span class="model-tag">MoE</span>' : ''}
        ${m.tags?.includes('code') ? '<span class="model-tag">code</span>' : ''}
      </div>
      ${have
        ? '<span class="param-subtitle">on disk</span>'
        : `<button class="btn-small model-download-btn" data-model-id="${encodeURIComponent(m.id)}" data-model-name="${escapeHtml(m.name)}"
             data-size="${m.size_gb}">Download</button>`}
    </div>`;
  });

  container.innerHTML = `
    <h4>Models</h4>
    ${active
      ? `<div class="model-active-banner">Running: <strong>${escapeHtml(active.replace('.gguf', ''))}</strong></div>`
      : '<div class="model-active-banner warn">No model loaded</div>'}
    ${models.length ? `<div class="model-list">${modelRows.join('')}</div>` : '<p class="param-subtitle">No GGUFs on this worker yet.</p>'}
    <details class="catalog-browser">
      <summary>Browse catalog (${catalogModels.length} models)</summary>
      <div class="catalog-list">${catalogRows.join('')}</div>
    </details>
    <span id="model-action-status" class="param-subtitle"></span>`;

  container.querySelectorAll('.model-activate-btn').forEach(btn => {
    btn.addEventListener('click', () => activateWorkerModel(workerId, decodeURIComponent(btn.dataset.filename)));
  });
  container.querySelectorAll('.model-download-btn').forEach(btn => {
    btn.addEventListener('click', () => downloadWorkerModel(workerId, decodeURIComponent(btn.dataset.modelId), btn));
  });
}

async function activateWorkerModel(workerId, filename) {
  const status = document.getElementById('model-action-status');
  if (status) status.textContent = `Activating ${filename}\u2026`;
  try {
    const r = await authFetch(`${API}/mesh/workers/${encodeURIComponent(workerId)}/models/activate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Activate failed');
    if (status) status.textContent = `Activated: ${data.model || filename}`;
    setTimeout(() => loadWorkerModels(workerId), 1000);
  } catch (err) {
    if (status) status.textContent = `Failed: ${err.message}`;
  }
}

async function downloadWorkerModel(workerId, modelId, btn) {
  const status = document.getElementById('model-action-status');
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Downloading\u2026';
  if (status) status.textContent = `Downloading ${modelId}\u2026 (this may take several minutes)`;
  try {
    const r = await authFetch(`${API}/mesh/workers/${encodeURIComponent(workerId)}/models/download`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: modelId }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Download failed');
    btn.textContent = 'Done';
    if (status) status.textContent = `Downloaded: ${data.filename || modelId}`;
    setTimeout(() => loadWorkerModels(workerId), 1000);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = origText;
    if (status) status.textContent = `Download failed: ${err.message}`;
  }
}

// ---------------------------------------------------------------------------
// Click delegation for mesh table, topology, drawer
// ---------------------------------------------------------------------------
export function initMeshClickDelegation() {
  document.addEventListener('click', (e) => {
    const detailBtn = e.target.closest('.mesh-detail-btn');
    if (detailBtn) {
      openMeshNodeDrawer(decodeURIComponent(detailBtn.dataset.workerId));
      return;
    }
    const row = e.target.closest('.mesh-row');
    if (row && !e.target.closest('button')) {
      openMeshNodeDrawer(decodeURIComponent(row.dataset.workerId));
      return;
    }
    const topoNode = e.target.closest('.topo-node[data-worker-id]');
    if (topoNode) {
      openMeshNodeDrawer(decodeURIComponent(topoNode.dataset.workerId));
      return;
    }
    if (e.target.id === 'mesh-node-drawer-close' || e.target.id === 'mesh-node-drawer-backdrop') {
      closeMeshNodeDrawer();
      return;
    }
    if (e.target.id === 'mesh-refresh-btn') {
      loadMeshTab();
      return;
    }
  });
}

// ---------------------------------------------------------------------------
// Add Node modal
// ---------------------------------------------------------------------------
let _enrollmentTokenData = null;
let _selectedPlatform = 'linux';
let _enrollmentCountdownTimer = null;

function _autoDetectPlatform() {
  const ua = (navigator.userAgent || '').toLowerCase();
  if (ua.includes('windows')) return 'win32';
  if (ua.includes('mac os') || ua.includes('macintosh')) return 'darwin';
  if (ua.includes('android')) return 'android';
  return 'linux';
}

function _updateExpiryCountdown() {
  const expiryEl = document.getElementById('enrollment-expiry');
  if (!expiryEl || !_enrollmentTokenData) return;
  const secs = Math.max(0, Math.floor(_enrollmentTokenData.expires_at - Date.now() / 1000));
  if (secs <= 0) {
    expiryEl.innerHTML = `<span class="expiry-expired">Token expired \u2014 click "Mint another" below.</span>`;
    if (_enrollmentCountdownTimer) { clearInterval(_enrollmentCountdownTimer); _enrollmentCountdownTimer = null; }
    return;
  }
  const mins = Math.floor(secs / 60);
  const rem = secs % 60;
  const cls = secs < 60 ? 'expiry-soon' : '';
  expiryEl.innerHTML = `<span class="${cls}">Expires in ${mins}:${String(rem).padStart(2, '0')}</span> \u00B7 single-use token`;
}

function _setAddNodeCommand() {
  const pre = document.getElementById('enrollment-command');
  const copyBtn = document.getElementById('copy-enrollment-cmd');
  const expiryEl = document.getElementById('enrollment-expiry');
  if (!_enrollmentTokenData) {
    pre.innerHTML = '<em>Auto-generating enrollment command&hellip;</em>';
    copyBtn.disabled = true;
    if (expiryEl) expiryEl.textContent = '';
    return;
  }
  const cmd = (_enrollmentTokenData.install_commands || {})[_selectedPlatform] || '(no command available)';
  pre.textContent = cmd;
  copyBtn.disabled = false;
  _updateExpiryCountdown();
  if (_enrollmentCountdownTimer) clearInterval(_enrollmentCountdownTimer);
  _enrollmentCountdownTimer = setInterval(_updateExpiryCountdown, 1000);
}

function _populateModelSelect(catalog) {
  const sel = document.getElementById('enrollment-model-select');
  const hint = document.getElementById('enrollment-model-hint');
  if (!sel || !catalog) return;

  const vramGb = parseFloat(document.getElementById('enrollment-vram-input')?.value || '4');
  const ramGb  = parseFloat(document.getElementById('enrollment-ram-input')?.value || '0');
  const vramMb = Math.round(vramGb * 1024);
  const ramMb  = Math.round(ramGb * 1024);

  const models = (catalog.models || []).slice().sort((a, b) => (b.min_vram_mb || 0) - (a.min_vram_mb || 0));

  const prevId = sel.value;
  sel.innerHTML = '<option value="">-- auto-select by VRAM --</option>';
  let autoId = '';

  models.forEach(m => {
    const needsMb = (m.min_vram_mb || 0);
    const fitsVram = needsMb <= vramMb;
    const fitsOffload = !fitsVram && needsMb <= vramMb + ramMb;
    if (!fitsVram && !fitsOffload) return;

    const opt = document.createElement('option');
    opt.value = m.id;
    const sizeStr = m.size_gb ? ` ${m.size_gb}GB` : '';
    const offload = fitsOffload ? ' [RAM offload]' : '';
    opt.textContent = `${m.name}${sizeStr}${offload}`;
    if (fitsOffload) opt.style.color = 'var(--yellow, #d29922)';
    sel.appendChild(opt);

    if (fitsVram && !autoId) autoId = m.id;
  });

  sel.value = prevId || autoId;
  _updateModelHint(catalog);
}

function _updateModelHint(catalog) {
  const hint = document.getElementById('enrollment-model-hint');
  const sel = document.getElementById('enrollment-model-select');
  if (!hint || !sel) return;
  if (!sel.value) {
    hint.textContent = 'Auto: installer will pick the best fit based on detected VRAM.';
    return;
  }
  const models = catalog?.models || [];
  const m = models.find(x => x.id === sel.value);
  if (!m) { hint.textContent = ''; return; }
  const tags = (m.tags || []).join(', ');
  const offload = sel.options[sel.selectedIndex]?.textContent?.includes('RAM offload') ? ' (requires RAM offloading)' : '';
  hint.textContent = `${m.name} — ${m.size_gb || '?'}GB${offload}${tags ? ' | ' + tags : ''}`;
}

let _enrollmentCatalog = null;

async function _loadEnrollmentCatalog() {
  if (_enrollmentCatalog) {
    _populateModelSelect(_enrollmentCatalog);
    return;
  }
  try {
    const r = await authFetch(`${API}/mesh/models/catalog`);
    if (r.ok) {
      _enrollmentCatalog = await r.json();
      _populateModelSelect(_enrollmentCatalog);
    }
  } catch {}
}

function openAddNodeModal() {
  const modal = document.getElementById('add-node-modal');
  modal.hidden = false;
  const detected = _autoDetectPlatform();
  _selectedPlatform = detected;
  document.querySelectorAll('.platform-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.platform === detected);
  });
  _loadEnrollmentCatalog();
  if (!_enrollmentTokenData || _enrollmentTokenData.expires_at < Date.now() / 1000 + 30) {
    _enrollmentTokenData = null;
    _setAddNodeCommand();
  } else {
    _setAddNodeCommand();
  }
}

function closeAddNodeModal() {
  document.getElementById('add-node-modal').hidden = true;
  if (_enrollmentCountdownTimer) { clearInterval(_enrollmentCountdownTimer); _enrollmentCountdownTimer = null; }
}

async function mintEnrollmentToken() {
  const noteInput = document.getElementById('enrollment-note-input');
  const modelSel = document.getElementById('enrollment-model-select');
  const btn = document.getElementById('mint-token-btn');
  btn.disabled = true;
  btn.textContent = 'Minting...';
  try {
    const body = { note: (noteInput.value || '').trim() };
    if (modelSel?.value) body.model_id = modelSel.value;
    const resp = await authFetch(API + '/mesh/enrollment-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: 'unknown' }));
      alert('Failed to mint token: ' + (err.error || resp.status));
      return;
    }
    _enrollmentTokenData = await resp.json();
    _setAddNodeCommand();
  } finally {
    btn.disabled = false;
    btn.textContent = 'Mint another';
  }
}

export function initAddNodeModal() {
  document.querySelectorAll('#add-node-btn, #status-add-node-btn').forEach(b =>
    b.addEventListener('click', openAddNodeModal)
  );
  document.getElementById('add-node-close').addEventListener('click', closeAddNodeModal);
  document.getElementById('add-node-modal').addEventListener('click', (e) => {
    if (e.target.id === 'add-node-modal') closeAddNodeModal();
  });
  document.addEventListener('keydown', (e) => {
    const modal = document.getElementById('add-node-modal');
    if (!modal.hidden && e.key === 'Escape') closeAddNodeModal();
  });
  document.querySelectorAll('.platform-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.platform-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      _selectedPlatform = tab.dataset.platform;
      _setAddNodeCommand();
    });
  });
  document.getElementById('mint-token-btn').addEventListener('click', mintEnrollmentToken);

  const vramInput = document.getElementById('enrollment-vram-input');
  const ramInput  = document.getElementById('enrollment-ram-input');
  const modelSel  = document.getElementById('enrollment-model-select');
  if (vramInput) vramInput.addEventListener('input', () => _populateModelSelect(_enrollmentCatalog));
  if (ramInput)  ramInput.addEventListener('input',  () => _populateModelSelect(_enrollmentCatalog));
  if (modelSel)  modelSel.addEventListener('change', () => _updateModelHint(_enrollmentCatalog));
  document.getElementById('copy-enrollment-cmd').addEventListener('click', async () => {
    const cmd = document.getElementById('enrollment-command').textContent;
    try {
      await navigator.clipboard.writeText(cmd);
      const btn = document.getElementById('copy-enrollment-cmd');
      const orig = btn.textContent;
      btn.textContent = 'Copied \u2713';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    } catch {
      alert('Copy failed \u2014 select and copy manually.');
    }
  });
}
