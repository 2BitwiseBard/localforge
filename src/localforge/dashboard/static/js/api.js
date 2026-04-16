export const API = window.location.origin + '/api';

export let apiKey = sessionStorage.getItem('ai-hub-key') || '';
export let currentUser = null;

export function setApiKey(key) {
  apiKey = key;
  if (key) sessionStorage.setItem('ai-hub-key', key);
  else sessionStorage.removeItem('ai-hub-key');
}

export function setCurrentUser(user) {
  currentUser = user;
}

export function authHeaders(extra = {}) {
  return { 'Authorization': `Bearer ${apiKey}`, ...extra };
}

export async function authFetch(url, opts = {}) {
  opts.headers = { ...authHeaders(), ...(opts.headers || {}) };
  return fetch(url, opts);
}

export const apiFetch = authFetch;

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

export function escapeAttr(s) {
  return (s || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function showToast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => { toast.classList.add('toast-fade'); setTimeout(() => toast.remove(), 500); }, 4000);
}

export function debounce(fn, ms = 300) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function timeAgo(ts) {
  if (!ts) return 'never';
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

export function formatBytes(b) {
  return b < 1024 ? b + 'B' : (b / 1024).toFixed(1) + 'KB';
}

export function formatUptime(s) {
  if (!s) return '--';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function statusRow(label, value, cls) {
  const c = cls ? ' status-' + cls : '';
  return `<div class="status-row"><span class="status-label">${label}</span><span class="status-value${c}">${value || '--'}</span></div>`;
}

export function renderMarkdown(text) {
  if (!text) return '';
  let html = escapeHtml(text);
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
  html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  html = html.replace(/(?<!\n)\n(?!\n)/g, '<br>');
  html = html.replace(/\n\n+/g, '<br><br>');
  return html;
}
