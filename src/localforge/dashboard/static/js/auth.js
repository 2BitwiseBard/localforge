import { API, apiKey, setApiKey, setCurrentUser, authFetch, showToast } from './api.js';

function showAuthModal(errorMsg = '') {
  return new Promise(resolve => {
    const modal = document.getElementById('auth-modal');
    const input = document.getElementById('auth-key-input');
    const btn   = document.getElementById('auth-submit-btn');
    const err   = document.getElementById('auth-error');
    err.textContent = errorMsg;
    input.value = '';
    modal.hidden = false;
    input.focus();

    function submit() {
      const key = input.value.trim();
      if (!key) { err.textContent = 'Key required.'; return; }
      modal.hidden = true;
      btn.removeEventListener('click', submit);
      input.removeEventListener('keydown', onKey);
      resolve(key);
    }
    function onKey(e) { if (e.key === 'Enter') submit(); }
    btn.addEventListener('click', submit);
    input.addEventListener('keydown', onKey);
  });
}

export async function initUser() {
  let key = apiKey;
  if (!key) {
    key = await showAuthModal();
    if (key) setApiKey(key);
  }
  try {
    const resp = await authFetch(API + '/me');
    if (resp.ok) {
      const user = await resp.json();
      setCurrentUser(user);
      document.getElementById('user-badge').textContent = user.name || user.id;
    } else if (resp.status === 401) {
      setApiKey('');
      const newKey = await showAuthModal('Invalid key — try again.');
      if (newKey) { setApiKey(newKey); return initUser(); }
    }
  } catch (e) {
    document.getElementById('user-badge').textContent = 'offline';
  }
}

export function connectSSE() {
  if (!apiKey) return;
  const es = new EventSource(API + '/events?token=' + encodeURIComponent(apiKey));
  es.onmessage = event => {
    try {
      const data = JSON.parse(event.data);
      if (data.type === 'connected') return;
      showToast(`${data.title}: ${data.body}`);
      if (Notification.permission === 'granted') {
        new Notification(data.title, { body: data.body, icon: '/static/icon-192.svg' });
      }
    } catch (e) {}
  };
  es.onerror = () => { setTimeout(connectSSE, 5000); es.close(); };
}
