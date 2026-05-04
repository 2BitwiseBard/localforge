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
const _shortcutsOverlay = document.getElementById('shortcuts-overlay');
document.getElementById('shortcuts-close-btn')?.addEventListener('click', () => { _shortcutsOverlay.hidden = true; });

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { _shortcutsOverlay.hidden = true; return; }
  if (e.target.matches('input, textarea, select, [contenteditable]')) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === '?') { _shortcutsOverlay.hidden = !_shortcutsOverlay.hidden; e.preventDefault(); return; }
  if (e.key === '/') {
    activateTab('search');
    const inp = document.getElementById('search-query');
    if (inp) { inp.focus(); inp.select(); }
    e.preventDefault(); return;
  }
  const idx = '123456789'.indexOf(e.key);
  if (idx >= 0 && idx < _TAB_ORDER.length) { activateTab(_TAB_ORDER[idx]); e.preventDefault(); }
  else if (e.key === '0' && _TAB_ORDER[9]) { activateTab(_TAB_ORDER[9]); e.preventDefault(); }
  else if (e.key === '-' && _TAB_ORDER[10]) { activateTab(_TAB_ORDER[10]); e.preventDefault(); }
  else if (e.key === '=' && _TAB_ORDER[11]) { activateTab(_TAB_ORDER[11]); e.preventDefault(); }
});

// Restore sidebar collapsed state
try {
  if (localStorage.getItem('sidebar-collapsed') === '1') {
    document.body.classList.add('sidebar-collapsed');
  }
} catch {}

// Dark/light theme toggle — respect OS preference on first visit
try {
  const saved = localStorage.getItem('theme');
  if (saved === 'light' || (!saved && window.matchMedia('(prefers-color-scheme: light)').matches)) {
    document.body.classList.add('light-theme');
  }
} catch {}
document.getElementById('theme-toggle')?.addEventListener('click', () => {
  const light = document.body.classList.toggle('light-theme');
  try { localStorage.setItem('theme', light ? 'light' : 'dark'); } catch {}
});

// Virtual keyboard: adjust chat container height when software keyboard opens
if (window.visualViewport) {
  const _adjustChat = () => {
    if (!window.matchMedia('(max-width: 480px)').matches) return;
    const el = document.querySelector('#tab-chat .chat-container');
    if (!el) return;
    const hdr = document.querySelector('header')?.offsetHeight ?? 49;
    const nav = document.querySelector('.mobile-nav')?.offsetHeight ?? 56;
    el.style.maxHeight = `${window.visualViewport.height - hdr - nav - 16}px`;
  };
  window.visualViewport.addEventListener('resize', _adjustChat);
}

// Mobile sidebar swipe gesture: swipe right from left edge to open, left to close
(function initSidebarSwipe() {
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (!sidebar) return;

  let startX = 0, startY = 0, tracking = false;
  const EDGE_ZONE = 24;   // px from left edge to start tracking
  const THRESHOLD = 60;   // px swipe distance to trigger

  document.addEventListener('touchstart', e => {
    const t = e.touches[0];
    // Open gesture: start from left edge when sidebar is closed
    if (t.clientX < EDGE_ZONE && !sidebar.classList.contains('open')) {
      startX = t.clientX; startY = t.clientY; tracking = 'open';
    }
    // Close gesture: start anywhere when sidebar is open
    else if (sidebar.classList.contains('open')) {
      startX = t.clientX; startY = t.clientY; tracking = 'close';
    }
  }, { passive: true });

  document.addEventListener('touchend', e => {
    if (!tracking) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - startX;
    const dy = Math.abs(t.clientY - startY);
    // Ignore vertical swipes
    if (dy > Math.abs(dx)) { tracking = false; return; }

    if (tracking === 'open' && dx > THRESHOLD) {
      sidebar.classList.add('open');
      if (backdrop) backdrop.hidden = false;
    } else if (tracking === 'close' && dx < -THRESHOLD) {
      sidebar.classList.remove('open');
      if (backdrop) backdrop.hidden = true;
    }
    tracking = false;
  }, { passive: true });
})();

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
      const data = await resp.json();
      if (data.enabled === false) return;
      const public_key = data.public_key;
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

// PWA install prompt — show a dismissible banner after 30s on mobile
let _deferredInstallPrompt = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  _deferredInstallPrompt = e;
  // Only show on mobile-ish screens after 30s of use
  if (!window.matchMedia('(max-width: 768px)').matches) return;
  setTimeout(() => {
    if (!_deferredInstallPrompt) return;
    // Don't show if already dismissed this session
    try { if (sessionStorage.getItem('pwa-dismissed')) return; } catch {}
    const banner = document.createElement('div');
    banner.id = 'pwa-install-banner';
    banner.style.cssText = 'position:fixed;bottom:0;left:0;right:0;z-index:2000;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--surface);border-top:1px solid var(--border);font-size:0.9rem;color:var(--text)';
    banner.innerHTML = '<span>Install AI Hub for quick access</span><div style="display:flex;gap:8px;flex-shrink:0"><button id="pwa-install-btn" style="padding:6px 14px;border-radius:6px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-size:0.85rem">Install</button><button id="pwa-dismiss-btn" style="padding:6px 10px;border-radius:6px;border:1px solid var(--border);background:none;color:var(--text-dim);cursor:pointer;font-size:0.85rem">Later</button></div>';
    document.body.appendChild(banner);
    document.getElementById('pwa-install-btn').addEventListener('click', async () => {
      banner.remove();
      if (_deferredInstallPrompt) { _deferredInstallPrompt.prompt(); _deferredInstallPrompt = null; }
    });
    document.getElementById('pwa-dismiss-btn').addEventListener('click', () => {
      banner.remove();
      try { sessionStorage.setItem('pwa-dismissed', '1'); } catch {}
    });
  }, 30000);
});

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
  startStatusRefresh();
  setInterval(loadApprovals, 15000);
  setInterval(loadTrainingStatus, 30000);
})();
