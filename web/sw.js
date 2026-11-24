// sw.js — V2 minimal offline shell. Caches app shell & static assets only.
// API traffic (/v1, /api, /dev) is NEVER cached: always straight to network.
const CACHE = 'mw-shell-7';
const SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './assets/icon.svg',
  './styles/tokens.css',
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
  './scripts/native-bridge.js',
  './scripts/voice-logic.js',
  './scripts/voice-fsm.js',
  './scripts/offline-queue.js',
  './scripts/actions.js',
  './scripts/recorder.js',
  './scripts/journal-scrubber.js',
  './scripts/lib/solar-terms.js',
  './scripts/lib/spring-waapi.js',
  './scripts/lib/virtual-river.js',
  './scripts/lib/swipe-arbiter.js',
  './scripts/lib/journal-pinch.js',
  './scripts/lib/ime-guard.js',
  './scripts/lib/orb.js',
  './scripts/api/client.js',
  './scripts/api/sse.js',
  './scripts/api/endpoints.js',
  './scripts/views/toast.js',
  './scripts/views/auth.js',
  './scripts/views/journal.js',
  './scripts/views/journal-items.js',
  './scripts/views/vellum-pocket.js',
  './scripts/views/peek-date.js',
  './scripts/views/people-sheet.js',
  './scripts/views/archive-history-sheet.js',
  './scripts/views/capture-receipt.js',
  './scripts/views/dock.js',
  './scripts/views/arrow.js',
  './scripts/views/voice.js',
  './scripts/views/capture.js',
  './scripts/views/chat.js',
  './scripts/views/chat-render.js',
  './scripts/views/chat-drawer.js',
  './scripts/views/chat-voice.js',
  './scripts/views/settings.js',
  './scripts/views/data-rights-sheet.js',
  './scripts/views/crisis.js',
  './scripts/views/sheet.js',
  './scripts/views/onboarding.js',
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
  if (url.origin !== self.location.origin || isApi(url)) return;

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match('./index.html').then((r) => r || caches.match('./'))),
    );
    return;
  }

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
