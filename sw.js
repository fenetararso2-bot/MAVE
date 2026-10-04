/* Offline shell: cache the static files; API calls always go to the network (saved/history live in localStorage). */
var CACHE = "mave-web-v1";
var SHELL = ["./", "index.html", "app.css", "i18n.js", "core.js", "app.js", "favicon.svg", "manifest.webmanifest"];
self.addEventListener("install", function (e) { e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () { return self.skipWaiting(); })); });
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (ks) { return Promise.all(ks.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); })); }).then(function () { return self.clients.claim(); }));
});
self.addEventListener("fetch", function (e) {
  var u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname.indexOf("/api/") === 0 || u.pathname.indexOf("admin") >= 0) return;
  e.respondWith(fetch(e.request).then(function (r) {
    var copy = r.clone(); if (r.ok) caches.open(CACHE).then(function (c) { c.put(e.request, copy); }); return r;
  }).catch(function () { return caches.match(e.request).then(function (m) { return m || caches.match("index.html"); }); }));
});
