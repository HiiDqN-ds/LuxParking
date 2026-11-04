importScripts("/static/js/staff-queue.js");

const CACHE_NAME = "parkpilot-staff-v3";
const APP_SHELL = [
  "/staff/offline",
  "/static/css/app.css",
  "/static/js/staff-inspection.js",
  "/static/js/staff-pwa.js",
  "/static/js/staff-queue.js",
  "/static/staff/manifest.webmanifest",
  "/static/staff/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key.startsWith("parkpilot-staff-") && key !== CACHE_NAME)
        .map((key) => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (APP_SHELL.includes(url.pathname)) {
    // Network first so edits to CSS/JS show up immediately; the cache is only the offline fallback.
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => caches.match(request))
    );
    return;
  }
  if (url.pathname.startsWith("/staff/")) {
    event.respondWith(fetch(request).catch(async () => {
      const cached = await caches.match("/staff/offline");
      return cached || new Response("Offline. Return when connected to continue.", {
        status: 503,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }));
  }
});

self.addEventListener("sync", (event) => {
  if (event.tag === "inspection-upload") {
    event.waitUntil(self.ParkPilotInspectionQueue.flush());
  }
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SYNC_INSPECTIONS") {
    event.waitUntil(self.ParkPilotInspectionQueue.flush());
  }
});
