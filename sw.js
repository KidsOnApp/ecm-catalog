// Nordicgrund service worker
//  - pagina e file statici: risposta immediata dalla copia locale, aggiornata in secondo piano
//    (una nuova versione del sito compare alla apertura successiva)
//  - copertine (Supabase Storage): copia locale, ricontrollata dopo 30 giorni
//  - tutto il resto (dati Supabase, MusicBrainz, ...) passa direttamente dalla rete
const SHELL = 'ng-shell-v1';
const COVERS = 'ng-covers-v1';
const STATIC = ['concerts_match.js', 'manifest.json', 'icon-192.png', 'icon-512.png', 'apple-touch-icon.png'];
const COVER_PATH = '/storage/v1/object/public/covers/';
const COVER_MAX_AGE = 30 * 24 * 3600 * 1000;

self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k.startsWith('ng-') && k !== SHELL && k !== COVERS).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

async function staleWhileRevalidate(cacheName, key, request) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(key);
  const fresh = fetch(request, { cache: 'no-cache' }).then(r => {
    if (r && r.ok) cache.put(key, r.clone());
    return r;
  });
  if (hit) { fresh.catch(() => {}); return hit; }
  return fresh;
}

async function fetchCover(url) {
  const r = await fetch(url, { mode: 'cors', credentials: 'omit' });
  if (!r.ok) return r;
  const blob = await r.blob();
  const headers = new Headers({ 'Content-Type': r.headers.get('Content-Type') || 'image/jpeg', 'x-cached-at': String(Date.now()) });
  const cache = await caches.open(COVERS);
  await cache.put(url, new Response(blob, { headers }));
  return new Response(blob, { headers });
}

async function cover(url) {
  const cache = await caches.open(COVERS);
  const hit = await cache.match(url);
  if (hit) {
    const at = parseInt(hit.headers.get('x-cached-at') || '0', 10);
    if (Date.now() - at > COVER_MAX_AGE) fetchCover(url).catch(() => {});
    return hit;
  }
  try { return await fetchCover(url); } catch (e) { return fetch(url); }
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.pathname.includes(COVER_PATH)) {
    e.respondWith(cover(url.origin + url.pathname));
    return;
  }
  if (url.origin !== self.location.origin) return;
  if (req.mode === 'navigate') {
    // i parametri (?v=..., ritorno dal login) non cambiano la pagina servita
    e.respondWith(staleWhileRevalidate(SHELL, '/', new Request('/')).catch(() => fetch(req)));
    return;
  }
  const name = url.pathname.replace(/^\//, '');
  if (STATIC.includes(name) && !url.search) {
    e.respondWith(staleWhileRevalidate(SHELL, url.pathname, req));
  }
});
