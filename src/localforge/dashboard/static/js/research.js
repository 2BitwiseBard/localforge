import { API, authFetch, authHeaders, escapeHtml, escapeAttr, showToast, timeAgo } from './api.js';

export async function loadResearchSessions() {
  const el = document.getElementById('research-sessions');
  try {
    const data = await authFetch(API + '/research/sessions').then(r => r.json());
    const sessions = data.sessions || [];
    if (!sessions.length) { el.innerHTML = '<div class="empty-state">No research sessions. Start one above!</div>'; return; }
    el.innerHTML = sessions.map(s => `
      <div class="research-session-card" data-id="${s.id}">
        <div class="research-question">${escapeHtml(s.question)}</div>
        <div class="research-meta">
          <span class="badge badge-${s.status}">${s.status}</span>
          <span>${s.finding_count} sources</span>
          <span>${timeAgo(s.updated_at)}</span>
        </div>
      </div>
    `).join('');
    el.querySelectorAll('.research-session-card').forEach(card => {
      card.addEventListener('click', () => loadResearchDetail(card.dataset.id));
    });
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
    if (data.findings?.length) {
      html += '<h3>Sources</h3><div class="findings-list">';
      data.findings.forEach((f, i) => {
        const credClass = f.credibility >= 0.7 ? 'cred-high' : f.credibility >= 0.4 ? 'cred-med' : 'cred-low';
        html += `<div class="finding-item">
          <div class="finding-header">
            <span class="finding-num">[${i + 1}]</span>
            <a href="${escapeAttr(f.url)}" target="_blank" class="finding-title">${escapeHtml(f.title || f.url)}</a>
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
      showToast('Research started: ' + data.session_id);
      document.getElementById('research-query').value = '';
      setTimeout(loadResearchSessions, 2000);
    } catch (e) { showToast('Failed: ' + e.message, 'error'); }
  });
}
