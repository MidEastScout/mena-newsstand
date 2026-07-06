# Push subscription store (Cloudflare Worker)

The one small piece of server behind the site's web-push notifications. A static
GitHub Pages site can't keep a database, so this Worker stores who opted in.
It **never sends** notifications — the hourly GitHub Action
(`scripts/send_push.py`) does that.

**Full, plain-language setup is in [`../PUSH-NOTIFICATIONS.md`](../PUSH-NOTIFICATIONS.md).**
Quick version:

```bash
cd push
npx wrangler login
npx wrangler kv namespace create SUBS     # paste the id into wrangler.toml
# paste your VAPID public key into wrangler.toml (VAPID_PUBLIC_KEY)
npx wrangler secret put ADMIN_TOKEN       # a long random string
npx wrangler deploy                       # prints the Worker URL
```

## Endpoints

| Method & path | Auth | Purpose |
|---------------|------|---------|
| `GET  /vapidPublicKey` | none | Hands the browser the key it needs to subscribe |
| `POST /subscribe`      | none | Store a `PushSubscription` (keyed by endpoint) |
| `POST /unsubscribe`    | none | Remove one (body: `{ "endpoint": "..." }`) |
| `GET  /subscriptions`  | `Bearer ADMIN_TOKEN` | The sender reads every subscription |
| `POST /prune`          | `Bearer ADMIN_TOKEN` | Delete expired endpoints (body: `{ "endpoints": [...] }`) |

## Bindings (see `wrangler.toml`)

- `SUBS` — KV namespace holding the subscriptions.
- `VAPID_PUBLIC_KEY` — plain var, the base64url application server key.
- `ADMIN_TOKEN` — **secret**, must match the Action's `PUSH_ADMIN_TOKEN`.
- `ALLOW_ORIGIN` — plain var, CORS allow-list (`*` to start, or your site origin).
