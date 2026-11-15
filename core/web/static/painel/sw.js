/* Only public, fixed assets. No API, media, authenticated HTML or query strings
 * enter Cache Storage. An offline navigation shows a public connection page. */
"use strict";
const PREFIX = "converge-panel-";
const CACHE = PREFIX + __APP_VERSION__;
const ASSETS = [
  "/painel/offline", "/painel/favicon.svg",
  "/painel/assets/pwa.js", "/painel/assets/auth.css",
  "/painel/assets/painel.css", "/painel/assets/painel.js",
  "/painel/assets/admin.js",
  "/painel/assets/attachments.js", "/painel/assets/billing.js", "/painel/assets/inbox.js", "/painel/assets/automacoes.js",
  "/painel/assets/propostas.js",
  "/painel/assets/icon-192.png", "/painel/assets/icon-512.png",
  "/painel/assets/icon-maskable-512.png", "/painel/assets/apple-touch-icon.png"
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys
    .filter(key => key.startsWith(PREFIX) && key !== CACHE)
    .map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener("message", event => {
  // The page asks before offering notifications: an older worker, still
  // active until the person updates, would never show them.
  if (event.data && event.data.type === "CAPABILITIES" && event.ports && event.ports[0]) {
    event.ports[0].postMessage({ push: true });
  }
  if (event.data && event.data.type === "ACTIVATE_UPDATE") {
    event.waitUntil(self.skipWaiting());
  }
  if (event.data && event.data.type === "CLEAR_PUBLIC_CACHE") {
    event.waitUntil(caches.keys().then(keys => Promise.all(keys
      .filter(key => key.startsWith(PREFIX)).map(key => caches.delete(key)))));
  }
});
self.addEventListener("fetch", event => {
  const request = event.request, url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.search ||
      url.pathname.startsWith("/painel/api/")) return;
  if (request.mode === "navigate" && url.pathname.startsWith("/painel/")) {
    event.respondWith(fetch(request).catch(async () =>
      (await caches.match("/painel/offline", { cacheName: CACHE })) ||
      new Response("Sem conexão. Reconecte-se e tente novamente.", {
        status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" }
      })));
    return;
  }
  if (!ASSETS.includes(url.pathname)) return;
  event.respondWith(fetch(request).then(async response => {
    if (response.ok && !response.redirected) {
      const cache = await caches.open(CACHE);
      await cache.put(request, response.clone());
    }
    return response;
  }).catch(async () => (await caches.match(request, { cacheName: CACHE })) || Response.error()));
});

// ── Push notifications (docs/PUSH.md) ───────────────────────────────────
// Only ever somewhere inside the panel, whatever the payload says.
function panelUrl(value) {
  try {
    const url = new URL(typeof value === "string" ? value : "/painel/", self.location.origin);
    if (url.origin === self.location.origin && url.pathname.startsWith("/painel/")) return url;
  } catch (e) {}
  return new URL("/painel/", self.location.origin);
}

// Every push is shown: Safari revokes push from a page that receives one
// without showing it.
self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  const url = panelUrl(data.url);
  const title = typeof data.title === "string" && data.title ? data.title.slice(0, 200) : "Converge";
  event.waitUntil(self.registration.showNotification(title, {
    tag: typeof data.tag === "string" && data.tag ? data.tag.slice(0, 64) : "converge",
    renotify: true,
    icon: "/painel/assets/icon-192.png",
    badge: "/painel/assets/icon-192.png",
    data: { url: url.pathname + url.hash }
  }));
});

// A tap brings the open panel forward and switches it to the conversation
// (no reload: a reply being written elsewhere stays), or opens the panel.
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = panelUrl(event.notification.data && event.notification.data.url).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find(client => {
      try { return new URL(client.url).pathname.startsWith("/painel/"); } catch (e) { return false; }
    });
    if (!open) return self.clients.openWindow(target);
    await open.focus();
    open.postMessage({ type: "OPEN_URL", url: target });
  })());
});
