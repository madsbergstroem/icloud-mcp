// Service worker of the icloud-mcp dashboard. It lets browsers install the dashboard as an app and shows the server's
// push notes. It caches nothing: every page and every request goes to the server as before, so nothing from your mailbox
// is stored by the browser.
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });

// A note says only what kind of item waits (push.py); tapping it opens that item in the app.
self.addEventListener("push", function (e) {
  var note = {};
  try { note = e.data ? e.data.json() : {}; } catch (err) { note = {}; }
  e.waitUntil(self.registration.showNotification(note.title || "iCloud connector", {
    body: note.body || "Something is waiting for your approval.", icon: "/icon-192.png", badge: "/icon-192.png",
    tag: note.open || "icloud-mcp", data: { open: note.open || "" }
  }));
});

self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  var open = (e.notification.data || {}).open || "";
  var url = "/dashboard" + (/^[a-z]+:[A-Za-z0-9_-]+$/.test(open) ? "#/item/" + open : "");
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (new URL(c.url).pathname.indexOf("/dashboard") === 0 && "focus" in c) {
        c.postMessage({ open: open });
        return c.focus();
      }
    }
    return self.clients.openWindow(url);
  }));
});
