import { API, authFetch, authHeaders, escapeHtml, escapeAttr, formatBytes, showToast, showUndoToast } from './api.js';

let _allNotes = [];

function _sortNotes(notes, sortBy) {
  const s = [...notes];
  if (sortBy === 'name') s.sort((a, b) => a.topic.localeCompare(b.topic));
  else if (sortBy === 'oldest') s.sort((a, b) => a.modified - b.modified);
  else if (sortBy === 'size') s.sort((a, b) => b.size - a.size);
  else s.sort((a, b) => b.modified - a.modified);
  return s;
}

// Live filter
document.getElementById('notes-filter')?.addEventListener('input', e => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll('#notes-list .note-item-wrap').forEach(wrap => {
    const topic = wrap.dataset.topic?.toLowerCase() || '';
    wrap.style.display = (!q || topic.includes(q)) ? '' : 'none';
  });
});

document.getElementById('notes-sort')?.addEventListener('change', () => loadNotes());

export async function loadNotes() {
  const el = document.getElementById('notes-list');
  if (!el) return;
  try {
    const data = await authFetch(API + '/notes').then(r => r.json());
    _allNotes = data.notes || [];
    if (!_allNotes.length) {
      el.innerHTML = '<div class="empty-state">No notes yet. Create one above.</div>';
      return;
    }
    const sortBy = document.getElementById('notes-sort')?.value || 'newest';
    const data_notes = _sortNotes(_allNotes, sortBy);
    el.innerHTML = data_notes.map(n => `
      <div class="note-item-wrap" data-topic="${escapeAttr(n.topic)}">
        <div class="note-item">
          <span class="note-topic">${escapeHtml(n.topic)}</span>
          <span class="note-meta">${formatBytes(n.size)} &middot; ${new Date(n.modified * 1000).toLocaleDateString()}</span>
          <div class="note-item-actions">
            <button class="btn-small note-edit-btn" data-topic="${escapeAttr(n.topic)}">Edit</button>
            <button class="btn-small btn-danger-small note-delete-btn" data-topic="${escapeAttr(n.topic)}">Delete</button>
          </div>
        </div>
        <div class="note-content-panel" style="display:none"></div>
      </div>`).join('');

    // Add group separators when sorted alphabetically by name
    if (sortBy === 'name') {
      const groups = new Map();
      el.querySelectorAll('.note-item-wrap').forEach(wrap => {
        const topic = wrap.dataset.topic || '';
        const prefix = topic.includes('-') ? topic.split('-')[0] : '_';
        if (!groups.has(prefix)) {
          groups.set(prefix, wrap);
          if (groups.size > 1) {
            const sep = document.createElement('div');
            sep.className = 'note-group-sep';
            sep.textContent = prefix;
            wrap.parentNode.insertBefore(sep, wrap);
          }
        }
      });
    }

    el.querySelectorAll('.note-item-wrap').forEach(wrap => {
      const topic = wrap.dataset.topic;
      const header = wrap.querySelector('.note-item');
      const panel = wrap.querySelector('.note-content-panel');

      // Click topic name to expand/collapse
      header.querySelector('.note-topic').addEventListener('click', async () => {
        if (panel.style.display !== 'none') { panel.style.display = 'none'; wrap.classList.remove('expanded'); return; }
        await _showNoteContent(panel, topic);
        wrap.classList.add('expanded');
      });

      // Edit button — open inline editor
      wrap.querySelector('.note-edit-btn').addEventListener('click', async (e) => {
        e.stopPropagation();
        await _openNoteEditor(wrap, topic);
      });

      // Delete button — immediate removal with 8s undo window
      wrap.querySelector('.note-delete-btn').addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          const resp = await authFetch(API + '/notes/' + encodeURIComponent(topic), { method: 'DELETE', headers: authHeaders() });
          const data = await resp.json();
          if (data.error) { showToast(data.error, 'error'); return; }
          wrap.remove();
          showUndoToast(`Deleted "${topic}"`, async () => {
            try {
              await authFetch(API + '/notes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...authHeaders() },
                body: JSON.stringify({ topic, content: data.content || '' }),
              });
              loadNotes();
              showToast(`Restored "${topic}"`);
            } catch (err) { showToast('Restore failed: ' + err.message, 'error'); }
          });
        } catch (err) { showToast('Delete failed: ' + err.message, 'error'); }
      });
    });
  } catch (e) {
    document.getElementById('notes-list').textContent = 'Failed to load notes';
  }
}

async function _showNoteContent(panel, topic) {
  panel.innerHTML = '<div class="loading">Loading...</div>';
  panel.style.display = 'block';
  try {
    const d = await authFetch(API + '/notes/' + encodeURIComponent(topic)).then(r => r.json());
    if (d.error) { panel.innerHTML = `<div class="error-msg">${escapeHtml(d.error)}</div>`; return; }
    panel.innerHTML = `<pre class="note-content-text">${escapeHtml(d.content)}</pre>`;
  } catch (err) { panel.innerHTML = `<div class="error-msg">${err.message}</div>`; }
}

async function _openNoteEditor(wrap, topic) {
  const panel = wrap.querySelector('.note-content-panel');
  panel.style.display = 'block';
  wrap.classList.add('expanded');
  panel.innerHTML = '<div class="loading">Loading...</div>';
  try {
    const d = await authFetch(API + '/notes/' + encodeURIComponent(topic)).then(r => r.json());
    const content = d.content || '';
    panel.innerHTML = `
      <textarea class="note-editor-textarea">${escapeHtml(content)}</textarea>
      <div class="param-actions" style="margin-top:6px;">
        <button class="btn-primary note-save-btn">Save</button>
        <button class="btn-secondary note-cancel-btn">Cancel</button>
        <span class="config-status note-save-status"></span>
      </div>`;
    panel.querySelector('.note-cancel-btn').addEventListener('click', () => {
      panel.style.display = 'none'; wrap.classList.remove('expanded');
    });
    panel.querySelector('.note-save-btn').addEventListener('click', async () => {
      const newContent = panel.querySelector('.note-editor-textarea').value;
      const st = panel.querySelector('.note-save-status');
      st.textContent = 'Saving…'; st.className = 'config-status';
      try {
        await authFetch(API + '/notes', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ topic, content: newContent }),
        });
        st.textContent = 'Saved'; st.className = 'config-status config-status-ok';
        showToast('Note saved');
        setTimeout(() => loadNotes(), 500);
      } catch (err) { st.textContent = 'Error: ' + err.message; st.className = 'config-status config-status-error'; }
    });
  } catch (err) { panel.innerHTML = `<div class="error-msg">${err.message}</div>`; }
}

// New note creation
document.getElementById('notes-new-btn')?.addEventListener('click', () => {
  const form = document.getElementById('notes-new-form');
  if (form) form.style.display = form.style.display === 'none' ? '' : 'none';
});

document.getElementById('notes-create-btn')?.addEventListener('click', async () => {
  const topicEl = document.getElementById('notes-new-topic');
  const contentEl = document.getElementById('notes-new-content');
  const st = document.getElementById('notes-create-status');
  const topic = topicEl?.value.trim();
  const content = contentEl?.value || '';
  if (!topic) { if (st) { st.textContent = 'Topic required'; st.className = 'config-status config-status-error'; } return; }
  try {
    await authFetch(API + '/notes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ topic, content }),
    });
    showToast('Note created: ' + topic);
    if (topicEl) topicEl.value = '';
    if (contentEl) contentEl.value = '';
    if (st) st.textContent = '';
    const form = document.getElementById('notes-new-form');
    if (form) form.style.display = 'none';
    loadNotes();
  } catch (err) {
    if (st) { st.textContent = 'Error: ' + err.message; st.className = 'config-status config-status-error'; }
  }
});
