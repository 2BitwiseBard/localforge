import { API, authFetch, authHeaders, escapeHtml, escapeAttr, showToast, statusRow, timeAgo } from './api.js';

export async function loadKGStats() {
  try {
    const data = await authFetch(API + '/kg/stats').then(r => r.json());
    const el = document.getElementById('kg-stats');
    if (data.error) { el.textContent = data.error; return; }
    let html = statusRow('Entities', data.total_entities || 0, 'ok') + statusRow('Relations', data.total_relations || 0, 'ok');
    if (data.entities_by_type) {
      html += '<div style="margin-top:8px">';
      for (const [t, c] of Object.entries(data.entities_by_type)) html += `<span class="type-badge type-${t}">${t}:${c}</span> `;
      html += '</div>';
    }
    el.innerHTML = html;
  } catch (e) { document.getElementById('kg-stats').textContent = 'Failed'; }
}

// Timeline
document.getElementById('kg-timeline-btn')?.addEventListener('click', async () => {
  const panel = document.getElementById('kg-timeline');
  if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  panel.innerHTML = '<div class="loading">Loading timeline...</div>';
  try {
    const data = await authFetch(API + '/kg/timeline?limit=20').then(r => r.json());
    const entries = data.entries || [];
    if (!entries.length) { panel.innerHTML = '<div class="empty-state">No entities yet</div>'; return; }
    panel.innerHTML = '<div class="kg-timeline-list">' + entries.map(e => `
      <div class="kg-timeline-entry">
        <span class="type-badge type-${e.type || 'concept'}">${e.type || 'concept'}</span>
        <span class="kg-tl-name">${escapeHtml(e.name)}</span>
        <span class="kg-tl-time">${timeAgo(e.updated_at)}</span>
        <div class="kg-tl-content">${escapeHtml((e.content || '').substring(0, 120))}</div>
      </div>
    `).join('') + '</div>';
  } catch (err) { panel.innerHTML = '<div class="error-msg">' + err.message + '</div>'; }
});

// Search
document.getElementById('kg-search-btn').addEventListener('click', doKGSearch);
document.getElementById('kg-search-input').addEventListener('keydown', e => { if (e.key === 'Enter') doKGSearch(); });

async function doKGSearch() {
  const query = document.getElementById('kg-search-input').value.trim(); if (!query) return;
  const type = document.getElementById('kg-type-filter').value || undefined;
  const el = document.getElementById('kg-results');
  el.innerHTML = '<div class="loading">Searching...</div>';
  try {
    const data = await authFetch(API + '/kg/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ query, entity_type: type }),
    }).then(r => r.json());
    if (data.error) { el.innerHTML = `<div class="error-msg">${data.error}</div>`; return; }
    if (!data.results?.length) { el.innerHTML = '<div class="empty-state">No entities found</div>'; return; }
    el.innerHTML = data.results.map(e => `
      <div class="entity-card" data-name="${escapeAttr(e.name)}">
        <div class="entity-header">
          <span class="type-badge type-${e.type || 'concept'}">${e.type || 'concept'}</span>
          <span class="entity-name">${escapeHtml(e.name)}</span>
          <div class="entity-actions">
            <button class="btn-small entity-explore-btn" data-name="${escapeAttr(e.name)}" title="Explore in graph">&#9906;</button>
            <button class="btn-small btn-danger-small entity-delete-btn" data-name="${escapeAttr(e.name)}" title="Delete entity">&#10005;</button>
          </div>
        </div>
        <div class="entity-content">${escapeHtml((e.content || '').substring(0, 200))}</div>
        <div class="entity-relations" style="display:none"></div>
      </div>
    `).join('');

    el.querySelectorAll('.entity-card').forEach(card => {
      // Click content to expand relations
      card.querySelector('.entity-content')?.addEventListener('click', async () => {
        const rel = card.querySelector('.entity-relations');
        if (rel.style.display !== 'none') { rel.style.display = 'none'; return; }
        rel.innerHTML = '<div class="loading">Loading...</div>'; rel.style.display = 'block';
        try {
          const ctx = await authFetch(API + '/kg/context', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ name: card.dataset.name }),
          }).then(r => r.json());
          if (ctx.error || !ctx.entity) { rel.innerHTML = ctx.error || 'Not found'; return; }
          const rels = ctx.relations || [];
          rel.innerHTML = rels.length
            ? '<div class="relation-tree">' + rels.map(r => `
                <div class="relation-item">
                  <span class="relation-dir ${r.direction}">${r.direction === 'outgoing' ? '→' : '←'}</span>
                  <span class="relation-type">${r.relation}</span>
                  <span class="relation-target">${escapeHtml(r.entity_name || '')}</span>
                </div>`).join('') + '</div>'
            : '<div class="empty-state">No relations</div>';
        } catch (e) { rel.innerHTML = 'Error: ' + e.message; }
      });

      // Explore in graph
      card.querySelector('.entity-explore-btn')?.addEventListener('click', e => {
        e.stopPropagation();
        document.getElementById('kg-viz-center').value = card.dataset.name;
        document.getElementById('kg-viz-btn').click();
      });

      // Delete entity
      card.querySelector('.entity-delete-btn')?.addEventListener('click', async e => {
        e.stopPropagation();
        if (!confirm(`Delete entity "${card.dataset.name}"? All its relations will also be removed.`)) return;
        try {
          const resp = await authFetch(API + '/kg/entity/' + encodeURIComponent(card.dataset.name), {
            method: 'DELETE', headers: authHeaders(),
          });
          const d = await resp.json();
          if (d.error) { showToast(d.error, 'error'); return; }
          showToast('Deleted: ' + card.dataset.name);
          card.remove();
          loadKGStats();
        } catch (err) { showToast('Delete failed: ' + err.message, 'error'); }
      });
    });
  } catch (e) { el.innerHTML = `<div class="error-msg">${e.message}</div>`; }
}

// Add entity
document.getElementById('kg-add-btn').addEventListener('click', async () => {
  const name = document.getElementById('kg-add-name').value.trim();
  if (!name) { showToast('Name required', 'error'); return; }
  try {
    const data = await authFetch(API + '/kg/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        name,
        entity_type: document.getElementById('kg-add-type').value,
        content: document.getElementById('kg-add-content').value.trim(),
      }),
    }).then(r => r.json());
    if (data.error) showToast('Error: ' + data.error, 'error');
    else {
      document.getElementById('kg-add-name').value = '';
      document.getElementById('kg-add-content').value = '';
      loadKGStats();
      showToast('Entity added');
    }
  } catch (e) { showToast('Error: ' + e.message, 'error'); }
});

// Relate entities
document.getElementById('kg-relate-btn')?.addEventListener('click', async () => {
  const from = document.getElementById('kg-relate-from').value.trim();
  const to = document.getElementById('kg-relate-to').value.trim();
  const rel = document.getElementById('kg-relate-type').value;
  const status = document.getElementById('kg-relate-status');
  if (!from || !to) { showToast('Both entity names required', 'error'); return; }
  status.textContent = 'Creating...'; status.className = 'config-status';
  try {
    const data = await authFetch(API + '/kg/relate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ from_name: from, to_name: to, relation: rel }),
    }).then(r => r.json());
    if (data.error) {
      status.textContent = data.error; status.className = 'config-status config-status-error';
    } else {
      status.textContent = `Created: ${from} ${rel} ${to}`; status.className = 'config-status config-status-ok';
      document.getElementById('kg-relate-from').value = '';
      document.getElementById('kg-relate-to').value = '';
      setTimeout(() => { status.textContent = ''; }, 3000);
    }
  } catch (e) { status.textContent = e.message; status.className = 'config-status config-status-error'; }
});

// KG Visualization
let _kgNodes = [], _kgPos = {};

document.getElementById('kg-viz-btn')?.addEventListener('click', async () => {
  const center = document.getElementById('kg-viz-center').value.trim();
  const canvas = document.getElementById('kg-canvas');
  canvas.style.display = 'block';
  try {
    const url = API + '/kg/graph' + (center ? '?center=' + encodeURIComponent(center) : '');
    const data = await authFetch(url, { method: 'POST' }).then(r => r.json());
    if (data.error) { showToast(data.error, 'error'); return; }
    _kgNodes = data.nodes || [];
    renderKGGraph(canvas, _kgNodes, data.edges || []);
  } catch (e) { showToast('Graph failed: ' + e.message, 'error'); }
});

// Click on canvas to navigate to nearest node
document.getElementById('kg-canvas')?.addEventListener('click', e => {
  if (!_kgNodes.length || !Object.keys(_kgPos).length) return;
  const canvas = e.currentTarget;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cx = (e.clientX - rect.left);
  const cy = (e.clientY - rect.top);
  let closest = null, minDist = Infinity;
  for (const n of _kgNodes) {
    const p = _kgPos[n.id];
    if (!p) continue;
    const dx = p.x - cx, dy = p.y - cy;
    const d = Math.sqrt(dx * dx + dy * dy);
    if (d < minDist) { minDist = d; closest = n; }
  }
  if (closest && minDist < 30) {
    document.getElementById('kg-viz-center').value = closest.name;
    document.getElementById('kg-search-input').value = closest.name;
    doKGSearch();
  }
});

function renderKGGraph(canvas, nodes, edges) {
  if (!nodes.length) { canvas.style.display = 'none'; showToast('No nodes to visualize'); return; }
  const ctx = canvas.getContext('2d');
  const W = canvas.width = canvas.clientWidth * (window.devicePixelRatio || 1);
  const H = canvas.height = 500 * (window.devicePixelRatio || 1);
  ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);
  const w = canvas.clientWidth, h = 500;

  _kgPos = {};
  nodes.forEach(n => { _kgPos[n.id] = { x: w / 2 + (Math.random() - 0.5) * w * 0.6, y: h / 2 + (Math.random() - 0.5) * h * 0.6 }; });

  const colors = {
    concept: '#58a6ff', code_module: '#3fb950', decision: '#d29922', learning: '#bc8cff',
    person: '#f78166', tool: '#8b949e', project: '#79c0ff', task: '#d2a8ff', event: '#ffa657', artifact: '#7ee787',
  };

  // Force-directed layout
  for (let iter = 0; iter < 200; iter++) {
    const alpha = 0.12 * (1 - iter / 200);
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = _kgPos[nodes[i].id], b = _kgPos[nodes[j].id];
        let dx = b.x - a.x, dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = 6000 / (dist * dist);
        dx /= dist; dy /= dist;
        a.x -= dx * force * alpha; a.y -= dy * force * alpha;
        b.x += dx * force * alpha; b.y += dy * force * alpha;
      }
    }
    edges.forEach(e => {
      const a = _kgPos[e.from], b = _kgPos[e.to];
      if (!a || !b) return;
      let dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (dist - 120) * 0.01;
      dx /= dist; dy /= dist;
      a.x += dx * force * alpha; a.y += dy * force * alpha;
      b.x -= dx * force * alpha; b.y -= dy * force * alpha;
    });
    nodes.forEach(n => {
      const p = _kgPos[n.id];
      p.x = Math.max(50, Math.min(w - 50, p.x));
      p.y = Math.max(50, Math.min(h - 50, p.y));
    });
  }

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, w, h);

  // Edges with arrows
  edges.forEach(e => {
    const a = _kgPos[e.from], b = _kgPos[e.to];
    if (!a || !b) return;
    const angle = Math.atan2(b.y - a.y, b.x - a.x);
    const r = 10;
    const ex = b.x - Math.cos(angle) * r, ey = b.y - Math.sin(angle) * r;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(ex, ey);
    ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1.5; ctx.stroke();
    // Arrowhead
    ctx.beginPath();
    ctx.moveTo(ex, ey);
    ctx.lineTo(ex - 7 * Math.cos(angle - 0.4), ey - 7 * Math.sin(angle - 0.4));
    ctx.lineTo(ex - 7 * Math.cos(angle + 0.4), ey - 7 * Math.sin(angle + 0.4));
    ctx.closePath(); ctx.fillStyle = '#30363d'; ctx.fill();
    // Label
    if (e.relation) {
      ctx.fillStyle = '#6e7681'; ctx.font = '9px monospace'; ctx.textAlign = 'center';
      ctx.fillText(e.relation, (a.x + b.x) / 2, (a.y + b.y) / 2 - 4);
    }
  });

  // Nodes
  nodes.forEach(n => {
    const p = _kgPos[n.id];
    const r = n.depth === 0 ? 14 : 9;
    ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = colors[n.type] || '#8b949e'; ctx.fill();
    ctx.strokeStyle = '#0d1117'; ctx.lineWidth = 2; ctx.stroke();
    ctx.fillStyle = '#e6edf3'; ctx.font = `${n.depth === 0 ? 12 : 10}px monospace`; ctx.textAlign = 'center';
    ctx.fillText(n.name.substring(0, 18), p.x, p.y + r + 13);
  });

  // Legend
  const legendTypes = [...new Set(nodes.map(n => n.type))].slice(0, 6);
  ctx.font = '10px monospace'; ctx.textAlign = 'left';
  legendTypes.forEach((t, i) => {
    ctx.fillStyle = colors[t] || '#8b949e';
    ctx.fillRect(8, 8 + i * 16, 10, 10);
    ctx.fillStyle = '#8b949e';
    ctx.fillText(t, 22, 17 + i * 16);
  });
}
