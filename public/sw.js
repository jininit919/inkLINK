/**
 * InkLink service worker
 *
 * Strategy:
 *   - HTML / documents: network-first (freshness matters — auth, feed data)
 *   - Static assets (CSS/JS/fonts/images): stale-while-revalidate — serve
 *     from cache immediately, refresh in background. Perceived load is instant
 *     after the first visit.
 *   - API + uploads: pass through (no SW involvement)
 */

const CACHE = 'inklink-v10';
const PRECACHE = [
  '/',
  '/theme.css',
  '/manifest.json',
  '/favicon.svg',
  '/img/inklink-logo.png',
  '/fonts/Bristol.otf',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.map(k => caches.delete(k))))
      .then(() => caches.open(CACHE))
      .then(c => c.addAll(PRECACHE).catch(() => {})) // don't fail install if one asset 404s
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Push notifications
self.addEventListener('push', e => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch {}
  const title   = data.title || 'InkLink';
  const options = {
    body:    data.body || '',
    icon:    '/icons/icon-192.png',
    badge:   '/icons/icon-192.png',
    data:    { url: data.url || '/' },
    vibrate: [100, 50, 100],
  };
  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = e.notification.data?.url || '/';
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) {
      if (c.url.includes(self.location.origin) && 'focus' in c) {
        c.navigate(url);
        return c.focus();
      }
    }
    return clients.openWindow(url);
  }));
});

function isStaticAsset(request) {
  const d = request.destination;
  return d === 'script' || d === 'style' || d === 'font' || d === 'image';
}

self.addEventListener('fetch', e => {
  const { request } = e;
  const url = new URL(request.url);

  if (request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/uploads/')) return;

  // Static assets: stale-while-revalidate — instant cache hit, refresh in bg
  if (isStaticAsset(request)) {
    e.respondWith(
      caches.open(CACHE).then(cache =>
        cache.match(request).then(cached => {
          const fetchPromise = fetch(request).then(res => {
            if (res && res.ok) cache.put(request, res.clone());
            return res;
          }).catch(() => cached);
          return cached || fetchPromise;
        })
      )
    );
    return;
  }

  // HTML/documents: network-first, cache fallback (freshness > staleness)
  if (request.destination === 'document') {
    e.respondWith(
      fetch(request)
        .then(res => {
          if (res && res.ok) {
            const clone = res.clone();
            caches.open(CACHE).then(c => c.put(request, clone));
          }
          return res;
        })
        .catch(() => caches.match(request).then(r => r || caches.match('/')))
    );
    return;
  }

  // Everything else: pass through
});
