// SBX WMSLite — operator PWA service worker.
// App-shell cache + offline navigation fallback. Data lives in IndexedDB
// (managed by the page), so the SW only caches the shell + static assets.
const CACHE = "wmslite-v1";
const SHELL = [
	"/wms-loading",
	"/assets/sbx_wmslite/css/stackbox.css",
	"/assets/sbx_wmslite/brand/stackbox-logo.svg",
	"/assets/sbx_wmslite/icons/icon-192.png",
];

self.addEventListener("install", (e) => {
	e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
	e.waitUntil(
		caches.keys().then((keys) =>
			Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
		).then(() => self.clients.claim())
	);
});

self.addEventListener("fetch", (e) => {
	const req = e.request;
	if (req.method !== "GET") return; // never cache API POSTs
	const url = new URL(req.url);
	// API calls: network-first, no fallback (page handles offline via IndexedDB).
	if (url.pathname.startsWith("/api/")) return;

	// Navigations: network-first, fall back to cached shell.
	if (req.mode === "navigate") {
		e.respondWith(fetch(req).catch(() => caches.match("/wms-loading")));
		return;
	}
	// Static assets: cache-first.
	e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
		const copy = res.clone();
		caches.open(CACHE).then((c) => c.put(req, copy));
		return res;
	}).catch(() => hit)));
});
