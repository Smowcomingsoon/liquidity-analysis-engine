/**
 * Service worker of the app: notifications only, no cache (a cached page
 * would be two deploys old). A long task launched from the app runs on the
 * server as a job; when it ends the server sends a push here, even with the
 * app closed, and tapping it reopens the app where the task was started
 * (Lorenzo, 01/10). The push carries {title, body, url, tag}; url is
 * /onchain-app.html?job=<id>#<view>.
 */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch { d = { body: event.data?.text() }; }
  event.waitUntil(self.registration.showNotification(d.title || "DEGEN", {
    body: d.body || "",
    tag: d.tag || d.url || "degen",
    icon: "/static/engn/icon-192.png",
    badge: "/static/engn/icon-192.png",
    data: { url: d.url || "/onchain-app.html" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/onchain-app.html", self.location.origin);
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    // An open app window: bring it forward and let it restore the job there
    // (no reload, the page keeps what it had).
    const app = wins.find((w) => new URL(w.url).pathname === url.pathname);
    if (app) {
      try { await app.focus(); } catch { /* focus may be refused, the message still lands */ }
      app.postMessage({ type: "job", job: url.searchParams.get("job"), hash: url.hash.slice(1) });
      return;
    }
    await self.clients.openWindow(url.href);
  })());
});
