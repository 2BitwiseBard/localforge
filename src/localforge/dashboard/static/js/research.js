import { API, authFetch, authHeaders, escapeHtml, escapeAttr, showToast, timeAgo } from './api.js';

let _pollTimer = null;

export async function loadResearchSessions() {
  const el = document.getElementById('research-sessions');
  try {
    const data = await authFetch(API + '/research/sessions').then(r => r.json());
    const sessions = data.sessions || [];
    if (!sessions.length) { el.innerHTML = '<div class="empty-state">No research sessions yet. Ask a question above!</div>'; return; }

    el.innerHTML = sessions.map(s => {
      const statusCls = s.status === 'complete' ? 'badge-ok' : s.status === 'active' ? 'badge-warn' : '';
      return `<div class="research-session-card" data-id="${s.id}" data-status="${s.status}">
        <div class="research-session-header">
          <div class="research-question">${escapeHtml(s.question)}</div>
          <button class="btn-small btn-danger-small research-delete-btn" data-id="${s.id}" title="Delete session">&#10005;</button>
        </div>
        <div class="research-meta">
          <span class="badge ${statusCls}">${s.status}</span>
          ${s.finding_count ? `<span>${s.finding_count} sources</span>` : ''}
          <span>${timeAgo(s.updated_at)}</span>
        </div>
      </div>`;
    }).join('');

    el.querySelectorAll('.research-session-card').forEach(card => {
      card.querySelector('.research-question')?.addEventListener('click', () => loadResearchDetail(card.dataset.id));
    });
    el.querySelectorAll('.research-delete-btn').forEach(btn => {
      btn.addEventListener('click', async e => {
        e.stopPropagation();
        if (!confirm('Remove this research session?')) return;
        try {
          await authFetch(API + '/research/sessions/' + btn.dataset.id, { method: 'DELETE', headers: authHeaders() });
          showToast('Session removed');
          loadResearchSessions();
          const detail = document.getElementById('research-detail-card');
          if (detail.style.display !== 'none') detail.style.display = 'none';
        } catch (err) { showToast('Failed: ' + err.message, 'error'); }
      });
    });

    // Poll if any active sessions
    const hasActive = sessions.some(s => s.status === 'active');
    if (hasActive && !_pollTimer) {
      _pollTimer = setInterval(() => {
        loadResearchSessions();
        loadResearchQueue();
      }, 8000);
    } else if (!hasActive && _pollTimer) {
      clearInterval(_pollTimer); _pollTimer = null;
    }
  } catch (e) { el.innerHTML = '<div class="error-msg">' + e.message + '</div>'; }
}

export async function loadResearchQueue() {
  const el = document.getElementById('research-queue-list');
  if (!el) return;
  try {
    const data = await authFetch(API + '/research/queue').then(r => r.json());
    const queries = data.queries || [];
    if (!queries.length) { el.innerHTML = '<div class="empty-state">Queue empty — queued queries run every 15 minutes.</div>'; return; }
    el.innerHTML = '<ul class="research-queue">' + queries.map(q =>
      `<li class="research-queue-item">${escapeHtml(q)}</li>`
    ).join('') + '</ul>';
  } catch (e) { el.innerHTML = '<div class="error-msg">' + e.message + '</div>'; }
}

async function loadResearchDetail(sessionId) {
  const card = document.getElementById('research-detail-card');
  const el = document.getElementById('research-detail');
  card.style.display = 'block';
  el.innerHTML = '<div class="loading">Loading...</div>';
  try {
    const data = await authFetch(API + '/research/sessions/' + sessionId).then(r => r.json());
    if (data.error) { el.innerHTML = '<div class="error-msg">' + data.error + '</div>'; return; }
    document.getElementById('research-detail-title').textContent = data.question;
    let html = '';
    if (data.status === 'active') {
      html += '<div class="research-in-progress"><div class="loading-dot"></div> Research in progress — results will appear when complete.</div>';
    }
    if (data.findings?.length) {
      html += '<h3>Sources</h3><div class="findings-list">';
      data.findings.forEach((f, i) => {
        const credClass = f.credibility >= 0.7 ? 'cred-high' : f.credibility >= 0.4 ? 'cred-med' : 'cred-low';
        html += `<div class="finding-item">
          <div class="finding-header">
            <span class="finding-num">[${i + 1}]</span>
            <a href="${escapeAttr(f.url)}" target="_blank" rel="noopener noreferrer" class="finding-title">${escapeHtml(f.title || f.url)}</a>
            <span class="cred-badge ${credClass}">${Math.round(f.credibility * 100)}%</span>
          </div>
          <div class="finding-excerpt">${escapeHtml((f.excerpt || '').substring(0, 300))}</div>
        </div>`;
      });
      html += '</div>';
    }
    if (data.synthesis) {
      html += '<h3>Synthesis</h3><div class="research-synthesis">' + escapeHtml(data.synthesis) + '</div>';
    }
    el.innerHTML = html || '<div class="empty-state">No findings yet</div>';
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) { el.innerHTML = '<div class="error-msg">' + e.message + '</div>'; }
}

export function initResearch() {
  document.getElementById('research-start-btn')?.addEventListener('click', async () => {
    const q = document.getElementById('research-query').value.trim();
    if (!q) { showToast('Enter a research question', 'error'); return; }
    showToast('Starting research...');
    try {
      const data = await authFetch(API + '/research/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ question: q }),
      }).then(r => r.json());
      if (data.error) { showToast(data.error, 'error'); return; }
      showToast('Research started');
      document.getElementById('research-query').value = '';
      setTimeout(() => { loadResearchSessions(); }, 1500);
    } catch (e) { showToast('Failed: ' + e.message, 'error'); }
  });

  document.getElementById('research-queue-btn')?.addEventListener('click', async () => {
    const q = document.getElementById('research-query').value.trim();
    if (!q) { showToast('Enter a research question', 'error'); return; }
    try {
      await authFetch(API + '/research/queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ query: q }),
      });
      showToast('Added to research queue');
      document.getElementById('research-query').value = '';
      loadResearchQueue();
    } catch (e) { showToast('Failed: ' + e.message, 'error'); }
  });

  document.getElementById('research-sessions-refresh')?.addEventListener('click', loadResearchSessions);
  document.getElementById('research-queue-refresh')?.addEventListener('click', loadResearchQueue);

  document.getElementById('research-detail-close')?.addEventListener('click', () => {
    document.getElementById('research-detail-card').style.display = 'none';
  });

  // Press Enter to research
  document.getElementById('research-query')?.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) document.getElementById('research-start-btn').click();
  });
}
