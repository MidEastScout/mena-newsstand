# Push subscription store + click counter (Cloudflare Worker)

The one small piece of server behind the site. A static GitHub Pages site can't
keep a database, so this Worker stores two things in KV:

1. **Push subscriptions** — who opted in to the top-story alerts. It **never
   sends** notifications — the hourly GitHub Action (`scripts/send_push.py`)
   does that.
2. **Headline click counters** — the "most clicked here" ranking signal for the
   Headlines tab. Counts aggregate across **all visitors** (not per-browser)
   and persist across sessions; each per-day counter self-expires after 30
   days. `scripts/fetch_headlines.py` reads the rolling 7-day aggregate every
   refresh and ranks each outlet's headlines by it.

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

> **Already deployed once?** Adding the click counter needs a **redeploy** —
> just `cd push && npx wrangler deploy` again. Nothing else changes: same KV
> namespace, same URL, no new secrets. Until you redeploy, `/click` returns
> 404, the site silently skips tracking, and the Headlines ranking falls back
> to the cross-outlet-coverage signal.

## Endpoints

| Method & path | Auth | Purpose |
|---------------|------|---------|
| `GET  /vapidPublicKey` | none | Hands the browser the key it needs to subscribe |
| `POST /subscribe`      | none | Store a `PushSubscription` (keyed by endpoint) |
| `POST /unsubscribe`    | none | Remove one (body: `{ "endpoint": "..." }`) |
| `POST /click`          | none | Count one headline click (body: `{ "url", "source"?, "title"? }`) |
| `GET  /clicks?days=7`  | none | Rolling per-article click totals, sorted desc |
| `GET  /subscriptions`  | `Bearer ADMIN_TOKEN` | The sender reads every subscription |
| `POST /prune`          | `Bearer ADMIN_TOKEN` | Delete expired endpoints (body: `{ "endpoints": [...] }`) |

## Bindings (see `wrangler.toml`)

- `SUBS` — KV namespace holding subscriptions (`sub:*`) and click counters (`clk:*`).
- `VAPID_PUBLIC_KEY` — plain var, the base64url application server key.
- `ADMIN_TOKEN` — **secret**, must match the Action's `PUSH_ADMIN_TOKEN`.
- `ALLOW_ORIGIN` — plain var, CORS allow-list (`*` to start, or your site origin).

## Click-counter accuracy, honestly

- **Aggregate & persistent:** yes — counters live server-side in KV, shared by
  every visitor, and survive browser restarts. This is a real cross-visitor
  signal, not per-browser localStorage.
- **Not transactional:** KV has no atomic increment (read → +1 → write), so two
  clicks on the *same article in the same second* can collapse into one count.
  At this site's traffic that means an occasional lost count, never an invented
  one — acceptable for ranking, not for billing.
- **Lightly gameable:** the endpoint is open (like every public counter), but
  the site de-dupes per browser per day, the pipeline only *ranks within an
  outlet* with it, and a manipulated ranking can at worst reorder 5 headlines.
