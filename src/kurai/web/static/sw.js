// Kurai's service worker: makes the web app installable (with «Поделиться» from other apps, see the manifest) and
// shows push notifications. No offline cache: Kurai is useful only with the server anyway.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});  // a fetch handler is part of being installable

self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {title: "Kurai", body: ""};
  event.waitUntil(self.registration.showNotification(data.title || "Kurai", {
    body: data.body, icon: "/static/icons/icon-192.png", badge: "/static/icons/icon-192.png", data: {url: data.url || "/"},
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data.url;
  event.waitUntil(self.clients.matchAll({type: "window"}).then((windows) => {
    const open = windows.find((w) => w.url.includes(self.location.origin));
    return open ? open.focus().then((w) => w.navigate(url)) : self.clients.openWindow(url);
  }));
});
