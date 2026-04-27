// AI Hub Service Worker — stale-while-revalidate for static, network-first for API
const CACHE_VERSION = 43;  // Bump on every static file change
const CACHE_NAME = `ai-hub-v${CACHE_VERSION}`;
const STATIC_ASSETS = [
  '/',
  '/static/style.css',
  '/static/js/sw-bootstrap.js',
  '/static/js/main.js',
  '/static/js/api.js',
  '/static/js/auth.js',
  '/static/js/status.js',
  '/static/js/mesh.js',
  '/static/js/chat.js',
  '/static/js/search.js',
  '/static/js/media.js',
  '/static/js/agents.js',
  '/static/js/notes.js',
  '/static/js/knowledge.js',
  '/static/js/config.js',
  '/static/js/research.js',
  '/static/js/training.js',
  '/static/js/workflows.js',
  '/static/js/workflow_editor.js',
  '/static/js/kg-worker.js',
  '/static/manifest.json',
  '/static/icon-192.svg',
];

// Install: cache all static assets fresh before activating
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// Activate: delete old caches, claim clients, then tell them to reload
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
      .then(() => self.clients.matchAll({ includeUncontrolled: true, type: 'window' }))
      .then(clients => clients.forEach(c => c.postMessage({ type: 'SW_UPDATED', version: CACHE_VERSION })))
  );
});

// Fetch: network-first for HTML navigation, stale-while-revalidate for static assets
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Read-only API endpoints: stale-while-revalidate so the PWA stays useful when
  // the backend is briefly unreachable (model swap, restart, flaky wifi).
  const CACHEABLE_API = ['/api/status', '/api/models', '/api/agents', '/api/mesh/status'];
  if (CACHEABLE_API.some(p => url.pathname === p) && event.request.method === 'GET') {
    event.respondWith(
      caches.open(CACHE_NAME).then(cache =>
        cache.match(event.request).then(cached => {
          const networkFetch = fetch(event.request).then(resp => {
            if (resp.ok) cache.put(event.request, resp.clone());
            return resp;
          }).catch(() => cached);
          return cached || networkFetch;
        })
      )
    );
    return;
  }

  // All other API / MCP / health: always network, never cache
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/mcp') || url.pathname === '/health') {
    return;
  }

  // HTML navigation (the page itself): network-first so we always get fresh HTML
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request)
        .then(response => {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // Static assets (JS, CSS, images): stale-while-revalidate
  event.respondWith(
    caches.match(event.request).then(cached => {
      const networkFetch = fetch(event.request).then(response => {
        if (response.ok && url.pathname.startsWith('/static/')) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => cached);

      return cached || networkFetch;
    })
  );
});

// Listen for page-side acknowledgement (optional)
self.addEventListener('message', event => {
  if (event.data?.type === 'SKIP_WAITING') self.skipWaiting();
});

// Push notifications
self.addEventListener('push', event => {
  const data = event.data ? event.data.json() : { title: 'AI Hub', body: 'Notification' };
  event.waitUntil(
    self.registration.showNotification(data.title || 'AI Hub', {
      body: data.body || '',
      icon: '/static/icon-192.svg',
      badge: '/static/icon-192.svg',
      tag: data.tag || 'ai-hub',
      data: data.url || '/',
    })
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data || '/'));
});
