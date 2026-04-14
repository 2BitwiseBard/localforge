// AI Hub Service Worker — stale-while-revalidate for static, network-first for API
const CACHE_VERSION = 3;  // Bump on every static file change
const CACHE_NAME = `ai-hub-v${CACHE_VERSION}`;
const STATIC_ASSETS = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/manifest.json',
  '/static/icon-192.svg',
];

// Install: cache static assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

// Activate: clean old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: stale-while-revalidate for static, network-only for API
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // API calls: always network
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/mcp') || url.pathname === '/health') {
    return;
  }

  // Static assets: serve cached immediately, update cache in background
  event.respondWith(
    caches.match(event.request).then(cached => {
      const networkFetch = fetch(event.request).then(response => {
        if (response.ok && (url.pathname.startsWith('/static/') || url.pathname === '/')) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => cached);  // offline fallback to cache

      return cached || networkFetch;
    })
  );
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
