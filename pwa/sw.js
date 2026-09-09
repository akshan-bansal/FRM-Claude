// Minimal service worker — cache the shell (index.html + manifest) so the
// PWA opens instantly on a return visit and works offline for the empty
// state. API requests are NEVER cached — every /v1 call goes to the
// user's own shim, live.
const CACHE = "tradecard-shell-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Never cache API calls — those must always be live.
  if (url.pathname.startsWith("/v1/") || url.pathname === "/healthz" || url.pathname === "/openapi.json") {
    return;
  }
  // Same-origin shell: cache-first.
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
  }
});
