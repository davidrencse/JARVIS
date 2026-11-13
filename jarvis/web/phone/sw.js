// JARVIS Phone service worker. Network first for the app shell, so a new version shows on the next
// launch, with the cached copy when the network can't give a usable answer: down (Tailscale off),
// hung (the main asleep while the phone's Tailscale is up) or a server error (JARVIS restarting,
// the phone link off). The app can then open and say it can't reach JARVIS instead of showing a
// browser error or a blank screen. Pairing and the API (/phone/*) never touch the cache. No push,
// no background sync.
// The phone server replaces __SHELL_VERSION__ with a hash of the shell files, so every release
// gets its own cache and the old one is dropped on activate.
"use strict";

const CACHE = "jarvis-phone-__SHELL_VERSION__";
const PREFIX = "jarvis-phone-";
// A shell request that hasn't answered in this long is answered from the cache, so a hung link
// shows the app (and its own "Can't reach JARVIS" screen) instead of a minute of nothing. Once
// one has hung, the next few seconds' requests (the page's scripts and styles) wait far less.
const NET_WAIT_MS = 3500, HUNG_WAIT_MS = 300, HUNG_FOR_MS = 10000;
let hungUntil = 0;
// Exactly the static files the phone server serves; anything else passes straight through.
const SHELL = [
  "/", "/phone.css", "/phone.js", "/manifest.webmanifest",
  "/icons/icon-192.png", "/icons/icon-512.png", "/icons/apple-touch-icon.png", "/icons/icon.svg",
  "/fonts/geist.woff2", "/fonts/geist-mono.woff2", "/fonts/doto.woff2",
  "/icons.svg", "/theme.js", "/esc.js", "/meters.js",
];

self.addEventListener("install", (event) => {
  // One missing file must not stop the install: each is cached on its own, best effort.
  event.waitUntil(caches.open(CACHE)
    .then((cache) => Promise.all(SHELL.map((url) => cache.add(new Request(url, { cache: "no-cache" })).catch(() => {}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith(PREFIX) && k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === "/phone" || url.pathname.startsWith("/phone/")) return;  // pairing and the API: network only, never cached
  const key = req.mode === "navigate" ? (url.pathname === "/" ? "/" : null) : url.pathname;
  if (!key || !SHELL.includes(key)) return;
  const live = fetchShell(req, key);
  event.respondWith(answer(live, key));
  event.waitUntil(live.catch(() => {}));  // a late answer still refreshes the cache
});

// The network's answer, kept for next time when it's good. A navigation request can't be
// refetched with options, so it goes as is; the rest revalidate past the HTTP cache so a stale
// phone.js never outlives a release.
function fetchShell(req, key) {
  const live = req.mode === "navigate" ? fetch(req) : fetch(req.url, { cache: "no-cache", credentials: "same-origin" });
  return live.then((res) => {
    if (cacheable(res, key)) {
      const copy = res.clone();
      caches.open(CACHE).then((cache) => cache.put(key, copy)).catch(() => {});
    }
    return res;
  });
}

// A good shell answer is cached. An authenticated response is not: a Set-Cookie, a
// no-store or private Cache-Control, or a final URL under /phone/ stays on the network.
function cacheable(res, key) {
  if (!res || !res.ok || res.type !== "basic") return false;
  let path = key;
  try { path = new URL(res.url).pathname; } catch (e) { /* keep the request key */ }
  if (path === "/phone" || path.startsWith("/phone/")) return false;
  if (res.headers.has("Set-Cookie")) return false;
  const control = (res.headers.get("Cache-Control") || "").toLowerCase();
  if (control.includes("no-store") || control.includes("private")) return false;
  return true;
}

function cached(key) {
  return caches.open(CACHE).then((cache) => cache.match(key)).catch(() => undefined);
}

// What the page gets: the network's answer when it's usable; the cached copy when the network
// fails, answers 5xx, or is still silent after NET_WAIT_MS. With nothing cached, whatever the
// network says (an error page beats no page), and a network error only when it says nothing.
function answer(live, key) {
  return new Promise((resolve) => {
    let done = false;
    const give = (res) => { if (!done) { done = true; resolve(res); } };
    const fallback = () => cached(key).then((hit) => { if (hit) give(hit); return hit; });
    const timer = setTimeout(() => { hungUntil = Date.now() + HUNG_FOR_MS; fallback(); },
      Date.now() < hungUntil ? HUNG_WAIT_MS : NET_WAIT_MS);
    live.then((res) => {
      clearTimeout(timer);
      hungUntil = 0;
      if (res && res.status >= 500) return fallback().then((hit) => { if (!hit) give(res); });
      give(res);
    }, () => {
      clearTimeout(timer);
      return fallback().then((hit) => { if (!hit) give(Response.error()); });
    });
  });
}
