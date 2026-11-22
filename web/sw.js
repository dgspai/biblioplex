// Network first; the last good copy answers when the server errors or is unreachable.
const CACHE = "biblioplex";
const MAX_ENTRIES = 300;
const SHELL = new URL("./", self.location).href;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;
  e.respondWith(networkFirst(req, req.mode === "navigate" ? SHELL : req, e));
});

async function networkFirst(req, key, e) {
  const cache = await caches.open(CACHE);
  let res;
  try {
    res = await fetch(req);
  } catch (err) {
    const hit = await cache.match(key);
    if (hit) return hit;
    throw err;
  }
  if (res.ok && !(res.headers.get("content-type") || "").includes("event-stream")) {
    e.waitUntil(cache.put(key, res.clone()).then(() => trim(cache)));
    return res;
  }
  return (res.status >= 500 && (await cache.match(key))) || res;
}

async function trim(cache) {
  const keys = await cache.keys();
  for (const k of keys.slice(0, Math.max(0, keys.length - MAX_ENTRIES))) await cache.delete(k);
}
