// Service worker registration + auto-reload on SW update.
// Loaded as a plain <script src> so it runs even when main.js fails
// (e.g. a module import error during a cache version transition).
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/sw.js').catch(function () {});
  navigator.serviceWorker.addEventListener('message', function (e) {
    if (e.data && e.data.type === 'SW_UPDATED') { location.reload(true); }
  });
}
