import { API, authFetch, escapeHtml, escapeAttr, formatBytes } from './api.js';

export async function loadNotes() {
  try {
    const data = await authFetch(API + '/notes').then(r => r.json());
    const el = document.getElementById('notes-list');
    if (!data.notes?.length) { el.textContent = 'No notes saved'; return; }
    el.innerHTML = data.notes.map(n => `<div class="note-item-wrap" data-topic="${escapeAttr(n.topic)}">
      <div class="note-item"><span class="note-topic">${escapeHtml(n.topic)}</span><span class="note-meta">${formatBytes(n.size)} | ${new Date(n.modified * 1000).toLocaleDateString()}</span></div>
      <div class="note-content-panel" style="display:none"></div>
    </div>`).join('');
    el.querySelectorAll('.note-item-wrap').forEach(wrap => {
      const header = wrap.querySelector('.note-item');
      const panel = wrap.querySelector('.note-content-panel');
      header.addEventListener('click', async () => {
        if (panel.style.display !== 'none') { panel.style.display = 'none'; wrap.classList.remove('expanded'); return; }
        panel.innerHTML = '<div class="loading">Loading...</div>';
        panel.style.display = 'block';
        wrap.classList.add('expanded');
        try {
          const d = await authFetch(API + '/notes/' + encodeURIComponent(wrap.dataset.topic)).then(r => r.json());
          if (d.error) { panel.innerHTML = `<div class="error-msg">${escapeHtml(d.error)}</div>`; return; }
          panel.innerHTML = `<pre class="note-content-text">${escapeHtml(d.content)}</pre>`;
        } catch (err) { panel.innerHTML = `<div class="error-msg">${err.message}</div>`; }
      });
    });
  } catch (e) { document.getElementById('notes-list').textContent = 'Failed to load notes'; }
}
