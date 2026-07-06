# Push notifications — the hourly "top stories" alert

This is a plain-language guide. You do **not** need to be a developer to follow
it, but you will copy-paste a few commands.

## What this gives you

A visitor clicks **🔔 Get alerts** on the site. From then on, roughly **once an
hour — and only when the top stories have actually changed** — their phone or
computer buzzes with the #1 headline ("*and 4 more top stories*"). Tapping it
opens The Middle East Scout.

Nobody is notified unless they opt in, and they can turn it off with the same
button.

## How it fits together

The site is a **static** site (GitHub Pages) with no server, so real browser
push needs two small helpers — both on **free** plans:

| Piece | What it does | Where it runs |
|------|---------------|---------------|
| **Service worker** (`sw.js`) | Shows the notification, opens the site on tap | The visitor's browser (already shipped) |
| **Subscription store** (`push/worker.js`) | Remembers who opted in | A tiny **Cloudflare Worker** (you deploy once) |
| **Sender** (`scripts/send_push.py`) | Every ~hour, pushes the top stories | Your existing **GitHub Action** |

The GitHub Action already runs every ~30 minutes; the sender rides along and
enforces the "hourly, only if changed" rule itself.

> ### ⚠️ One thing to tell iPhone users
> On iPhone/iPad, Apple only allows web-push **after the site is added to the
> Home Screen** (Share → *Add to Home Screen*, then open it from there). The
> button knows this and shows "Add to Home Screen" instead. Android and desktop
> (Chrome, Edge, Firefox) work straight from the button.

Until you finish the setup below, **nothing changes** — the button stays hidden
and the sender quietly does nothing. So you can merge this safely and switch it
on whenever you're ready.

---

## One-time setup (~15 minutes)

### Step 1 — Make your VAPID keys

VAPID is how Apple/Google/Mozilla know a push really came from you. Generate a
matching pair (run this once, on your computer):

```bash
pip install pywebpush          # brings in the crypto it needs
python scripts/gen_vapid_keys.py
```

It prints a **PUBLIC KEY**, a **PRIVATE KEY**, and a reminder to pick a
**SUBJECT** (any contact URL, e.g. `mailto:you@example.com`). Keep this output
open for the next steps.

*(No Python handy? `npx web-push generate-vapid-keys` prints an equivalent
pair.)*

### Step 2 — Deploy the subscription store (Cloudflare Worker)

Cloudflare's free plan is plenty for this.

1. Make a free account at **cloudflare.com**.
2. In a terminal, from the `push/` folder:

   ```bash
   cd push
   npx wrangler login                         # opens your browser to authorize
   npx wrangler kv namespace create SUBS      # prints an id="..."
   ```

3. Open `push/wrangler.toml` and paste in:
   - the KV **id** you just got (replace `REPLACE_WITH_KV_NAMESPACE_ID`), and
   - your **PUBLIC KEY** from Step 1 (replace `REPLACE_WITH_VAPID_PUBLIC_KEY`).

4. Set the admin token (invent a long random string — this guards the list of
   subscribers) and deploy:

   ```bash
   npx wrangler secret put ADMIN_TOKEN        # paste your random string
   npx wrangler deploy                        # prints your Worker URL
   ```

   The Worker URL looks like `https://mena-push.YOUR-SUBDOMAIN.workers.dev`.
   Copy it.

### Step 3 — Tell the site where the Worker is

Edit **`push-config.json`** (in the repo root) and put your Worker URL in:

```json
{ "api": "https://mena-push.YOUR-SUBDOMAIN.workers.dev" }
```

Commit it. Within a refresh cycle the **🔔 Get alerts** button appears on the
live site.

### Step 4 — Give the GitHub Action its secrets

In GitHub: **Settings → Secrets and variables → Actions → New repository
secret**. Add four:

| Secret name | Value |
|-------------|-------|
| `PUSH_API` | your Worker URL (same as `push-config.json`) |
| `PUSH_ADMIN_TOKEN` | the **same** random string you gave `ADMIN_TOKEN` |
| `VAPID_PRIVATE_KEY` | the **PRIVATE KEY** from Step 1 |
| `VAPID_SUBJECT` | e.g. `mailto:you@example.com` |

That's it. The next hourly cycle that finds new top stories will notify every
subscriber.

### Step 5 — Test it

1. Open the live site on a laptop, click **🔔 Get alerts**, allow notifications.
2. In GitHub: **Actions → MENA Newsstand → Run workflow**, tick
   **"Force-send a push notification now"**, run it.
3. You should get a notification within a minute. 🎉

---

## How it behaves

- **Cadence:** at most **once per hour** (`PUSH_MIN_INTERVAL_MIN`, default 60).
- **Only when it matters:** it compares the current top-5 links to the last push
  and **skips if nothing changed** — so quiet news hours stay quiet.
- **Content:** title = the #1 headline; body = "and *N* more top stories"; tap
  opens the site.
- **Self-cleaning:** subscriptions the push service reports as expired are
  pruned automatically.
- **Never blocks the site:** every part is best-effort; if anything is
  misconfigured the sender logs a notice and exits cleanly.

## Costs

All **free**: GitHub Actions (public repo), Cloudflare Workers + KV free tier,
and the browser push services themselves. No credit card required for the
volumes a news site like this generates.

## Turning it off

- **Pause sending:** delete any one of the four GitHub secrets (e.g.
  `VAPID_PRIVATE_KEY`). The sender goes back to doing nothing.
- **Hide the button too:** set `"api": ""` in `push-config.json`.
- **Tear it down:** `cd push && npx wrangler delete`.

## Tuning

- Change the hourly cap by adding a `PUSH_MIN_INTERVAL_MIN` repo **variable**
  (e.g. `120` for every two hours) and passing it through in the workflow, or
  set it inline on the "Send push notifications" step.
- The click-through target defaults to the live GitHub Pages URL; set a
  `SITE_URL` secret to point at a custom domain once you have one.
