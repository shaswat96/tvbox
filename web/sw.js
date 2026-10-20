// Minimal service worker so Chrome treats the remote as an installable app.
// Everything goes straight to the network: the remote is useless offline anyway.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
