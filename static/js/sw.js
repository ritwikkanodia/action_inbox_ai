// Service worker for the Self-driving Inbox PWA.
//
// Deliberately conservative: only same-origin /static/ assets and the offline
// page are cached. Todo HTML and every API response are user-specific and
// auth-gated, so they always go to the network — a cached copy could be shown
// to the wrong session or long after it went stale.

// Bump on every change to a precached asset. The fetch handler below is
// cache-first, so a stale entry keeps being served until the cache is renamed
// and `activate` drops the old one — an edit to app.js or app.css that forgets
// this ships a frontend the browser never runs.
const VERSION = 'v13';
const CACHE = `self-driving-inbox-${VERSION}`;
const OFFLINE_URL = '/offline';

const PRECACHE = [
  OFFLINE_URL,
  '/static/css/app.css',
  '/static/js/app.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-maskable-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // addAll is atomic; a single 404 would leave us with no cache at all.
      .then((cache) => Promise.all(
        PRECACHE.map((url) => cache.add(url).catch(() => null))
      ))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Page loads: always network, fall back to the offline shell.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }

  // Static assets: serve from cache, refresh in the background.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        const network = fetch(request).then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        }).catch(() => cached);
        return cached || network;
      })
    );
  }
});

// ---------------- Push notifications ----------------
// The poller sends {todo_id, title, body, url, actions:[{index,label}]}. Only
// the label travels; a button click sends the index back and the server runs
// the instruction it cached, so a payload can't put words in the agent's mouth.

// Tell open pages what the push handler did, so a stalled delivery can be
// told apart from a failed showNotification without chrome:// internals.
function report(stage, detail) {
  return self.clients.matchAll({ type: 'window' })
    .then((wins) => wins.forEach((w) => w.postMessage({ type: 'push-debug', stage, detail })))
    .catch(() => null);
}

self.addEventListener('push', (event) => {
  let payload = null;
  try { payload = event.data ? event.data.json() : null; } catch (e) { payload = null; }
  event.waitUntil(report('received', payload && payload.title));
  if (!payload || typeof payload !== 'object') {
    payload = { title: 'New todo', body: '', url: '/', actions: [] };
  }
  const maxActions = Number.isInteger(Notification.maxActions) ? Notification.maxActions : 2;
  const actions = (payload.actions || []).slice(0, maxActions).map((a) => ({
    action: `run:${a.index}`,
    title: a.label,
  }));
  event.waitUntil(self.registration.showNotification(payload.title || 'New todo', {
    body: payload.body || '',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    // Same todo re-pushed replaces its banner instead of stacking a second one.
    tag: payload.todo_id || 'action-inbox',
    // ...but a replacement must still alert; without this macOS updates the
    // entry in Notification Centre silently and nothing appears on screen.
    renotify: true,
    // A banner whose point is its buttons must not slide away after 5s. On
    // macOS this only takes effect in Chrome's "Alerts" style; "Banners"
    // still auto-dismiss and hide the buttons behind a hover chevron.
    requireInteraction: actions.length > 0,
    data: payload,
    actions,
  }).then(() => report('shown', payload.title), (e) => report('error', String(e))));
});

self.addEventListener('notificationclick', (event) => {
  const payload = event.notification.data || {};
  event.notification.close();
  const target = new URL(payload.url || '/', self.location.origin).href;
  const m = /^run:(\d+)$/.exec(event.action || '');

  const startRun = (m && payload.todo_id)
    ? fetch(`/todos/${encodeURIComponent(payload.todo_id)}/ask-ai`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_index: Number(m[1]) }),
      }).catch(() => null)
    : Promise.resolve(null);

  // Whether or not the run started (401 when the session expired, 400 when
  // the cache changed), bring the todo in front of the user: an open window
  // is navigated and focused, otherwise a new one is opened.
  const show = self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    .then((wins) => {
      const win = wins.find((w) => new URL(w.url).origin === self.location.origin);
      if (win) return win.navigate(target).then((w) => (w || win).focus()).catch(() => win.focus());
      return self.clients.openWindow(target);
    });

  event.waitUntil(startRun.then(() => show));
});
