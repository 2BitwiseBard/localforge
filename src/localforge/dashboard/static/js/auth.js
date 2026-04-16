import { API, apiKey, setApiKey, setCurrentUser, authFetch, showToast } from './api.js';

export async function initUser() {
  let key = apiKey;
  if (!key) {
    key = prompt('Enter your AI Hub API key:') || '';
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
      const newKey = prompt('Invalid key. Enter your AI Hub API key:') || '';
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
