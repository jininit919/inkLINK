const CACHE = 'inklink-v3';
const PRECACHE = [
  '/',
  '/manifest.json',
  '/favicon.svg',
  'https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:ital,wght@0,300;0,400;1,300&display=swap',
];

// Install — pre-cache shell
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

// Activate — clean old caches
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

// Fetch — network first, cache fallback
self.addEventListener('fetch', e => {
  const { request } = e;
  const url = new URL(request.url);

  // Skip non-GET, cross-origin API calls, uploads, auth
  if (request.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/uploads/')) return;

  e.respondWith(
    fetch(request)
      .then(res => {
        // Cache successful HTML/CSS/JS/font responses
        if (res.ok && (
          request.destination === 'document' ||
          request.destination === 'script' ||
          request.destination === 'style' ||
          request.destination === 'font' ||
          request.destination === 'image'
        )) {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(request, clone));
        }
        return res;
      })
      .catch(() => caches.match(request).then(r => r || caches.match('/')))
  );
});
