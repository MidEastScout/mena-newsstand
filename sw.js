/* The Middle East Scout — service worker.
 *
 * Its ONLY job is web-push: receive a pushed payload and show a notification,
 * then open the site when the user taps it. It deliberately does NOT cache the
 * site (no offline layer) so it can never serve a stale index.html — the whole
 * product is "live", and the data files must always be fetched fresh from the
 * network. Keeping the worker push-only means shipping a new index.html never
 * needs a cache-busting dance here.
 *
 * Payload shape (sent by scripts/send_push.py):
 *   { "title": "<lead headline> · <N> outlets",
 *     "body":  "2. <headline> · <N> outlets\n3. <headline> · <N> outlets\n…",
 *     "url":   "https://mideastscout.github.io/mena-newsstand/",
 *     "tag":   "mena-top-stories",
 *     "stories": [ { "title": "...", "url": "...", "outlets": N }, ... ] }
 */

// Activate a freshly deployed worker immediately instead of waiting for every
// tab to close, so a bug fix here reaches users on their next visit.
self.addEventListener("install", (event) => {
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    // A non-JSON push (shouldn't happen from our sender) still shows something.
    data = { title: "The Middle East Scout", body: event.data ? event.data.text() : "" };
  }

  const title = data.title || "The Middle East Scout";
  const body = data.body || "New top stories across the Middle East";
  const url = data.url || "https://mideastscout.github.io/mena-newsstand/";

  const options = {
    body,
    // Language + direction so Hebrew notifications render right-to-left.
    lang: data.lang || "en",
    dir: data.dir || "auto",
    // SVG favicon renders as the notification icon in Chromium; other engines
    // fall back to the site icon. Kept relative to the worker's scope.
    icon: "favicon.svg",
    badge: "favicon.svg",
    // Collapse repeat editions onto one notification instead of stacking five
    // "top stories" cards over a morning.
    tag: data.tag || "mena-top-stories",
    renotify: true,
    // Stash the click-through target (and the story list, for future use) so
    // notificationclick can route without another network call.
    data: { url, stories: data.stories || [] },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) ||
    "https://mideastscout.github.io/mena-newsstand/";

  // Focus an already-open Scout tab if there is one; otherwise open a new one.
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          // Same-origin tab already open → just focus and navigate it.
          if ("focus" in client) {
            try {
              const u = new URL(client.url);
              const t = new URL(target);
              if (u.origin === t.origin) {
                client.navigate(target);
                return client.focus();
              }
            } catch (e) {
              /* fall through to openWindow */
            }
          }
        }
        if (self.clients.openWindow) return self.clients.openWindow(target);
      })
  );
});
