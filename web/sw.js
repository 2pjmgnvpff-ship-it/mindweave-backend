// sw.js — minimal offline shell. Caches the app shell + static assets only.
// API traffic (/v1, /api, /auth media URLs) is NEVER cached: always straight to the network.
const CACHE = 'mw-shell-3';
const SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './assets/icon.svg',
  './assets/cupid/muse_cupid_a_listen.webp',
  './assets/cupid/arrow_pin_gold.webp',
  './assets/cupid/arrow_pin_silver.webp',
  './assets/cupid/arrow_pin_crystal.webp',
  './assets/gleb/lake_bench_clean.jpg',
  './styles/base.css',
  './styles/journal.css',
  './styles/dock.css',
  './styles/chat.css',
  './styles/sheets.css',
  './scripts/main.js',
  './scripts/dom.js',
  './scripts/decor.js',
  './scripts/store.js',
  './scripts/format.js',
  './scripts/icons.js',
  './scripts/voice-logic.js',
  './scripts/offline-queue.js',
  './scripts/actions.js',
  './scripts/recorder.js',
  './scripts/api/client.js',
  './scripts/api/sse.js',
  './scripts/api/endpoints.js',
  './scripts/views/toast.js',
  './scripts/views/auth.js',
  './scripts/views/journal.js',
  './scripts/views/journal-items.js',
  './scripts/views/casual.js',
  './scripts/views/dock.js',
  './scripts/views/arrow.js',
  './scripts/views/voice.js',
  './scripts/views/capture.js',
  './scripts/views/chat.js',
  './scripts/views/chat-render.js',
  './scripts/views/settings.js',
  './scripts/views/sheet.js',
  './scripts/views/onboarding.js',
  './scripts/views/questions.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function isApi(url) {
  return url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/') || url.pathname.startsWith('/dev/');
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || isApi(url)) return; // network only, never cached

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match('./index.html').then((r) => r || caches.match('./'))),
    );
    return;
  }

  // static assets: network first (fresh code after deploys), cache fallback when offline
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req)),
  );
});
