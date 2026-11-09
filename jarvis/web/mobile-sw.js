/* Cache only the static shell, never API calls, desktop frames or camera media. */
const CACHE = "jarvis-mobile-shell-v1";
const SHELL = ["/mobile.html","/mobile.css","/mobile.js","/app.css","/porcelain.css","/icon.svg"];
self.addEventListener("install",event => {
  event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting()));
});
self.addEventListener("activate",event => {
  event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith("jarvis-mobile-shell-") && k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));
});
self.addEventListener("fetch",event => {
  const url=new URL(event.request.url);
  if (event.request.method!=="GET" || url.origin!==self.location.origin ||
      !SHELL.includes(url.pathname) || url.pathname.startsWith("/api/")) return;
  event.respondWith(fetch(event.request).then(response=>{
    if(response.ok){const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));}
    return response;
  }).catch(()=>caches.match(event.request)));
});
