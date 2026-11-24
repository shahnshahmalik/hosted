// LabCanvas service worker. It makes the dashboard installable and shows a friendly page when
// the server can't be reached. It deliberately caches nothing else: the dashboard is live data,
// and the server already sets long-lived cache headers on versioned assets.
const CACHE = "labcanvas-offline-v1";
const OFFLINE = ["/offline.html", "/icons/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(OFFLINE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const { request } = e;
  if (request.mode === "navigate") {
    e.respondWith(fetch(request).catch(() => caches.match("/offline.html")));
  } else if (OFFLINE.includes(new URL(request.url).pathname)) {
    e.respondWith(fetch(request).catch(() => caches.match(request)));
  }
});
