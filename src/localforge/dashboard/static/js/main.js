import { loadStatus, startStatusRefresh, loadModes, initModeControls, scanDiskModels } from './status.js';
import { loadMeshStatus, loadMeshTab, initMeshClickDelegation, initAddNodeModal } from './mesh.js';
import { initUser, connectSSE } from './auth.js';
import { loadModels, loadGenParams, loadPresets, loadLoras, initLoadParams, loadStartupConfig } from './config.js';
import { loadPhotos } from './media.js';
import { loadIndexes, loadIndexMgmt, initIndexCreate } from './search.js';
import { loadAgents, loadApprovals, initAgentToolbar } from './agents.js';
import { loadNotes } from './notes.js';
import { loadKGStats } from './knowledge.js';
import { loadResearchSessions, loadResearchQueue, initResearch } from './research.js';
import { loadTrainingOverview, loadTrainingDatasets, loadTrainingStatus, loadTrainingLoras, initTraining } from './training.js';
import { authFetch } from './api.js';
import './chat.js';
import './knowledge.js';

// =====================================================================
// Connection status indicator
// =====================================================================
const connDot = document.getElementById('conn-dot');
async function checkConnection() {
  try {
    const r = await fetch('/health', { signal: AbortSignal.timeout(3000) });
    const ok = r.ok;
    connDot.className = 'conn-dot ' + (ok ? 'conn-online' : 'conn-offline');
    connDot.title = ok ? 'Backend online' : 'Backend error';
    return ok;
  } catch {
    connDot.className = 'conn-dot conn-offline';
    connDot.title = 'Backend unreachable';
    return false;
  }
}
checkConnection();
setInterval(checkConnection, 15000);

// =====================================================================
// Tabs
// =====================================================================
function activateTab(tabName) {
  if (!tabName) return;
  document.querySelectorAll('.tab').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tabName);
  });
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  const panel = document.getElementById('tab-' + tabName);
  if (panel) panel.classList.add('active');
  if (tabName === 'search') { loadIndexes(); loadIndexMgmt(); }
  if (tabName === 'knowledge') loadKGStats();
  if (tabName === 'media') loadPhotos();
  if (tabName === 'config') { loadGenParams(); loadPresets(); loadLoras(); loadStartupConfig(); }
  if (tabName === 'research') { loadResearchSessions(); loadResearchQueue(); }
  if (tabName === 'workflows') window.__wfEditor?.onTabOpen();
  if (tabName === 'mesh') loadMeshTab();
  if (tabName === 'training') { loadTrainingOverview(); loadTrainingStatus(); }
  document.getElementById('sidebar')?.classList.remove('open');
  document.getElementById('sidebar-backdrop')?.setAttribute('hidden', '');
  const sheet = document.getElementById('mobile-more-sheet');
  if (sheet) sheet.hidden = true;
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-tab]');
  if (btn) {
    e.preventDefault();
    activateTab(btn.dataset.tab);
    return;
  }
  const moreBtn = e.target.closest('#mobile-more-btn');
  if (moreBtn) {
    const sheet = document.getElementById('mobile-more-sheet');
    if (sheet) sheet.hidden = !sheet.hidden;
    return;
  }
  const collapse = e.target.closest('#sidebar-collapse-btn');
  if (collapse) {
    document.body.classList.toggle('sidebar-collapsed');
    try { localStorage.setItem('sidebar-collapsed', document.body.classList.contains('sidebar-collapsed') ? '1' : '0'); } catch {}
    return;
  }
  const mobileToggle = e.target.closest('#sidebar-mobile-toggle');
  if (mobileToggle) {
    document.getElementById('sidebar')?.classList.toggle('open');
    const bd = document.getElementById('sidebar-backdrop');
    if (bd) bd.hidden = !document.getElementById('sidebar').classList.contains('open');
    return;
  }
  if (e.target.id === 'sidebar-backdrop') {
    document.getElementById('sidebar')?.classList.remove('open');
    e.target.hidden = true;
    return;
  }
});

// Keyboard shortcuts
const _TAB_ORDER = ['status', 'chat', 'search', 'mesh', 'media', 'config', 'agents', 'research', 'workflows', 'training', 'notes', 'knowledge'];
document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, textarea, select, [contenteditable]')) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const idx = '123456789'.indexOf(e.key);
  if (idx >= 0 && idx < _TAB_ORDER.length) { activateTab(_TAB_ORDER[idx]); e.preventDefault(); }
  else if (e.key === '0' && _TAB_ORDER[9]) { activateTab(_TAB_ORDER[9]); e.preventDefault(); }
  else if (e.key === '-' && _TAB_ORDER[10]) { activateTab(_TAB_ORDER[10]); e.preventDefault(); }
});

// Restore sidebar collapsed state
try {
  if (localStorage.getItem('sidebar-collapsed') === '1') {
    document.body.classList.add('sidebar-collapsed');
  }
} catch {}

// Notification permission + Web Push subscription
if ('Notification' in window && 'serviceWorker' in navigator && 'PushManager' in window) {
  function _urlBase64ToUint8Array(b64) {
    const pad = '='.repeat((4 - b64.length % 4) % 4);
    const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
  }

  async function subscribeToPush() {
    try {
      const resp = await fetch('/api/push/vapid-key');
      if (!resp.ok) return;
      const { public_key } = await resp.json();
      if (!public_key) return;
      const reg = await navigator.serviceWorker.ready;
      // Re-register existing subscription (idempotent on server)
      const existing = await reg.pushManager.getSubscription();
      if (existing) {
        await authFetch('/api/push/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(existing.toJSON()),
        }).catch(() => {});
        return;
      }
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: _urlBase64ToUint8Array(public_key),
      });
      await authFetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub.toJSON()),
      }).catch(() => {});
    } catch {
      // Push blocked or unavailable — not fatal
    }
  }

  if (Notification.permission === 'granted') {
    subscribeToPush();
  } else if (Notification.permission === 'default') {
    document.addEventListener('click', async function askNotif() {
      document.removeEventListener('click', askNotif);
      const perm = await Notification.requestPermission();
      if (perm === 'granted') subscribeToPush();
    }, { once: true });
  }
}

// =====================================================================
// Init
// =====================================================================
initModeControls();
document.getElementById('scan-models-btn')?.addEventListener('click', scanDiskModels);
initMeshClickDelegation();
initAgentToolbar();
initIndexCreate();
initResearch();
initTraining();
initLoadParams();
startStatusRefresh();

(async () => {
  await initUser();
  loadStatus();
  loadModels();
  loadAgents();
  loadNotes();
  loadMeshStatus();
  initAddNodeModal();
  loadModes();
  loadApprovals();
  loadTrainingOverview();
  loadTrainingStatus();
  connectSSE();
  setInterval(loadApprovals, 15000);
  setInterval(loadTrainingStatus, 30000);
})();
