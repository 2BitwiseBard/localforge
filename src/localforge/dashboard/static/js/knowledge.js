import { API, authFetch, authHeaders, escapeHtml, escapeAttr, showToast, statusRow } from './api.js';

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
        <div class="entity-header"><span class="type-badge type-${e.type || 'concept'}">${e.type || 'concept'}</span><span class="entity-name">${escapeHtml(e.name)}</span></div>
        <div class="entity-content">${escapeHtml((e.content || '').substring(0, 200))}</div>
        <div class="entity-relations" style="display:none"></div>
      </div>
    `).join('');
    el.querySelectorAll('.entity-card').forEach(card => {
      card.addEventListener('click', async () => {
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
            ? '<div class="relation-tree">' + rels.map(r => `<div class="relation-item"><span class="relation-type">${r.relation_type || r.relation}</span><span class="relation-arrow">&rarr;</span><span class="relation-target">${escapeHtml(r.to_name || r.name || '')}</span></div>`).join('') + '</div>'
            : '<div class="empty-state">No relations</div>';
        } catch (e) { rel.innerHTML = 'Error: ' + e.message; }
      });
    });
  } catch (e) { el.innerHTML = `<div class="error-msg">${e.message}</div>`; }
}

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

// KG Visualization
document.getElementById('kg-viz-btn')?.addEventListener('click', async () => {
  const center = document.getElementById('kg-viz-center').value.trim();
  const canvas = document.getElementById('kg-canvas');
  canvas.style.display = 'block';
  try {
    const url = API + '/kg/graph' + (center ? '?center=' + encodeURIComponent(center) : '');
    const data = await authFetch(url).then(r => r.json());
    if (data.error) { showToast(data.error, 'error'); return; }
    renderKGGraph(canvas, data.nodes || [], data.edges || []);
  } catch (e) { showToast('Graph failed: ' + e.message, 'error'); }
});

function renderKGGraph(canvas, nodes, edges) {
  if (!nodes.length) { canvas.style.display = 'none'; showToast('No nodes to visualize'); return; }
  const ctx = canvas.getContext('2d');
  const W = canvas.width = canvas.clientWidth * (window.devicePixelRatio || 1);
  const H = canvas.height = 500 * (window.devicePixelRatio || 1);
  ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);
  const w = canvas.clientWidth, h = 500;

  const pos = {};
  nodes.forEach(n => { pos[n.id] = { x: w / 2 + (Math.random() - 0.5) * w * 0.6, y: h / 2 + (Math.random() - 0.5) * h * 0.6 }; });

  const colors = {
    concept: '#58a6ff', code_module: '#3fb950', decision: '#d29922', learning: '#bc8cff',
    person: '#f78166', tool: '#8b949e', project: '#79c0ff', task: '#d2a8ff', event: '#ffa657', artifact: '#7ee787',
  };

  for (let iter = 0; iter < 200; iter++) {
    const alpha = 0.1 * (1 - iter / 200);
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = pos[nodes[i].id], b = pos[nodes[j].id];
        let dx = b.x - a.x, dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = 5000 / (dist * dist);
        dx /= dist; dy /= dist;
        a.x -= dx * force * alpha; a.y -= dy * force * alpha;
        b.x += dx * force * alpha; b.y += dy * force * alpha;
      }
    }
    edges.forEach(e => {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) return;
      let dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (dist - 100) * 0.01;
      dx /= dist; dy /= dist;
      a.x += dx * force * alpha; a.y += dy * force * alpha;
      b.x -= dx * force * alpha; b.y -= dy * force * alpha;
    });
    nodes.forEach(n => {
      const p = pos[n.id];
      p.x = Math.max(40, Math.min(w - 40, p.x));
      p.y = Math.max(40, Math.min(h - 40, p.y));
    });
  }

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, w, h);

  edges.forEach(e => {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
    ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1; ctx.stroke();
    ctx.fillStyle = '#484f58'; ctx.font = '9px monospace';
    ctx.fillText(e.relation || '', (a.x + b.x) / 2, (a.y + b.y) / 2);
  });

  nodes.forEach(n => {
    const p = pos[n.id];
    const r = n.depth === 0 ? 12 : 8;
    ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = colors[n.type] || '#8b949e'; ctx.fill();
    ctx.strokeStyle = '#0d1117'; ctx.lineWidth = 2; ctx.stroke();
    ctx.fillStyle = '#e6edf3'; ctx.font = '11px monospace'; ctx.textAlign = 'center';
    ctx.fillText(n.name.substring(0, 20), p.x, p.y + r + 14);
  });
}
