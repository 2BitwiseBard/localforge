import { API, authFetch, authHeaders, escapeHtml, escapeAttr, showToast } from './api.js';

const searchIndex = document.getElementById('search-index');
const searchQuery = document.getElementById('search-query');
const searchBtn = document.getElementById('search-btn');
const searchResults = document.getElementById('search-results');

export async function loadIndexes() {
  try {
    const data = await authFetch(API + '/indexes').then(r => r.json());
    searchIndex.innerHTML = '';
    (data.indexes || []).forEach(n => {
      const o = document.createElement('option'); o.value = n; o.textContent = n; searchIndex.appendChild(o);
    });
    if (!data.indexes?.length) searchIndex.innerHTML = '<option>No indexes</option>';
  } catch (e) { searchIndex.innerHTML = '<option>Error</option>'; }
}

searchBtn.addEventListener('click', doSearch);
searchQuery.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });

async function doSearch() {
  const query = searchQuery.value.trim(), index = searchIndex.value;
  if (!query || !index) return;
  const mode = document.querySelector('input[name="search-mode"]:checked').value;
  searchResults.innerHTML = '<div class="loading">Searching...</div>';
  try {
    const data = await authFetch(API + '/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ query, index_name: index, mode }),
    }).then(r => r.json());
    if (data.error) { searchResults.innerHTML = `<div class="error-msg">${data.error}</div>`; return; }
    const r = data.result || '';
    searchResults.innerHTML = `<pre class="search-result-text">${escapeHtml(typeof r === 'string' ? r : JSON.stringify(r, null, 2))}</pre>`;
  } catch (e) { searchResults.innerHTML = `<div class="error-msg">${e.message}</div>`; }
}

// Index management
export async function loadIndexMgmt() {
  try {
    const data = await authFetch(API + '/indexes').then(r => r.json());
    const el = document.getElementById('index-mgmt-list');
    if (!el) return;
    if (!data.indexes?.length) { el.innerHTML = '<div class="empty-state">No indexes. Create one below.</div>'; return; }
    el.innerHTML = data.indexes.map(name => `
      <div class="index-item">
        <span class="index-name">${escapeHtml(name)}</span>
        <div class="index-actions">
          <button class="btn-small idx-refresh-btn" data-name="${escapeAttr(name)}">Refresh</button>
          <button class="btn-small btn-danger-small idx-delete-btn" data-name="${escapeAttr(name)}">Delete</button>
        </div>
      </div>
    `).join('');
    el.querySelectorAll('.idx-refresh-btn').forEach(btn => {
      btn.addEventListener('click', () => refreshIndex(btn.dataset.name));
    });
    el.querySelectorAll('.idx-delete-btn').forEach(btn => {
      btn.addEventListener('click', () => deleteIndex(btn.dataset.name));
    });
  } catch (e) {}
}

async function refreshIndex(name) {
  showToast('Refreshing ' + name + '...');
  try {
    await authFetch(API + `/indexes/${encodeURIComponent(name)}/refresh`, { method: 'POST' });
    showToast('Refreshed: ' + name);
  } catch (e) { showToast('Refresh failed', 'error'); }
}

async function deleteIndex(name) {
  if (!confirm(`Delete index "${name}"?`)) return;
  try {
    await authFetch(API + `/indexes/${encodeURIComponent(name)}/delete`, { method: 'POST' });
    showToast('Deleted: ' + name); loadIndexMgmt(); loadIndexes();
  } catch (e) { showToast('Delete failed', 'error'); }
}

export function initIndexCreate() {
  document.getElementById('idx-create-btn')?.addEventListener('click', async () => {
    const name = document.getElementById('idx-name').value.trim();
    const directory = document.getElementById('idx-directory').value.trim();
    const glob = document.getElementById('idx-glob').value.trim() || '**/*.*';
    const embed = document.getElementById('idx-embed').checked;
    if (!name || !directory) { showToast('Name and directory required', 'error'); return; }
    showToast('Creating index ' + name + '...');
    try {
      const d = await authFetch(API + '/indexes/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ name, directory, glob_pattern: glob, embed }),
      }).then(r => r.json());
      if (d.error) { showToast('Error: ' + d.error, 'error'); return; }
      showToast('Index created: ' + name);
      document.getElementById('idx-name').value = '';
      document.getElementById('idx-directory').value = '';
      loadIndexMgmt(); loadIndexes();
    } catch (e) { showToast('Create failed', 'error'); }
  });
}
