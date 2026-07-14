/**
 * The Middle East Scout — push subscription store + headline click counter
 * (Cloudflare Worker).
 *
 * A static GitHub Pages site cannot keep a database, so this tiny Worker is the
 * one piece of server we run. It does exactly four things:
 *
 *   1. Hands the browser the VAPID public key it needs to subscribe.
 *   2. Stores / removes push subscriptions in a KV namespace.
 *   3. Lets the GitHub Action (the sender) read every subscription, and prune
 *      the dead ones, guarded by a bearer token.
 *   4. Counts headline clicks (POST /click) and serves the rolling aggregate
 *      (GET /clicks) — the "most clicked here" ranking signal for the
 *      Headlines tab. Counters aggregate across ALL visitors and persist in
 *      KV; each per-day counter self-expires after CLICK_TTL_DAYS.
 *
 * It NEVER sends the notifications itself — the hourly GitHub Action does that
 * with the VAPID *private* key, which never touches this Worker.
 *
 * Bindings it expects (see wrangler.toml):
 *   - SUBS              KV namespace (stores subscriptions + click counters)
 *   - VAPID_PUBLIC_KEY  plain var  (the base64url application server key)
 *   - ADMIN_TOKEN       secret     (bearer for /subscriptions and /prune)
 *   - ALLOW_ORIGIN      plain var  (optional; the site origin for CORS, or "*")
 *
 * Honest limitation of the click counter: KV has no atomic increment, so an
 * increment is read → +1 → write. Two clicks on the SAME article in the SAME
 * second can collapse into one count (last write wins). At this site's scale
 * that costs at most an occasional single count and never invents clicks, so
 * it is a fine trade for staying on the free tier with zero infrastructure.
 */

const json = (obj, status, origin) =>
  new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { "content-type": "application/json", ...cors(origin) },
  });

function cors(origin) {
  return {
    "access-control-allow-origin": origin || "*",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "access-control-allow-headers": "content-type,authorization",
    "access-control-max-age": "86400",
  };
}

// A fixed-length, opaque KV key derived from the subscription endpoint, so the
// same browser re-subscribing overwrites its own record instead of duplicating.
async function keyFor(endpoint) {
  return "sub:" + (await sha256hex(endpoint));
}

async function sha256hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ── Click counter constants ─────────────────────────────────────────────────
// One KV record per article per UTC day: "clk:<YYYY-MM-DD>:<sha256(url)[:32]>"
// → { n, url, source, title }. Day-bucketing keeps the rolling window honest
// (GET /clicks sums only the requested days) and lets old data expire on its
// own instead of accumulating forever.
const CLICK_TTL_DAYS = 30;        // per-day counters self-delete after this
const CLICK_WINDOW_DEFAULT = 7;   // GET /clicks default rolling window
const CLICK_WINDOW_MAX = 30;
const CLICK_LIST_CAP = 5000;      // safety cap on keys read per aggregation

function utcDay(offset = 0) {
  const d = new Date(Date.now() - offset * 86400000);
  return d.toISOString().slice(0, 10);
}

function allowOrigin(env, request) {
  const configured = (env.ALLOW_ORIGIN || "*").trim();
  if (configured === "*") return "*";
  // Support a comma-separated allow-list; echo the request origin if it matches.
  const origin = request.headers.get("Origin") || "";
  const list = configured.split(",").map((s) => s.trim());
  return list.includes(origin) ? origin : list[0];
}

function authed(env, request) {
  const got = request.headers.get("Authorization") || "";
  const want = "Bearer " + (env.ADMIN_TOKEN || "");
  // Constant-ish comparison is overkill for a broadcast list, but reject empties.
  return env.ADMIN_TOKEN && got === want;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const origin = allowOrigin(env, request);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors(origin) });
    }

    // ── Public: the browser fetches the key it needs to Push-subscribe. ──────
    if (path === "/vapidPublicKey" && request.method === "GET") {
      return new Response(env.VAPID_PUBLIC_KEY || "", {
        status: env.VAPID_PUBLIC_KEY ? 200 : 503,
        headers: { "content-type": "text/plain", ...cors(origin) },
      });
    }

    // ── Public: store a subscription. ────────────────────────────────────────
    if (path === "/subscribe" && request.method === "POST") {
      let sub;
      try {
        sub = await request.json();
      } catch (e) {
        return json({ error: "invalid json" }, 400, origin);
      }
      if (!sub || !sub.endpoint) return json({ error: "missing endpoint" }, 400, origin);
      const record = { endpoint: sub.endpoint, keys: sub.keys || {}, added: Date.now() };
      // Per-subscriber notification frequency (minutes). Stored so the sender can
      // pace each person independently. Clamped to a sane 15 min .. 1 week window.
      const iv = Number(sub.interval);
      if (Number.isFinite(iv)) record.interval = Math.max(15, Math.min(iv, 10080));
      // Preferred language (matches the reader's site language) so the sender can
      // localise the notification. Only 'he' / 'en' are accepted.
      if (sub.lang === "he" || sub.lang === "en") record.lang = sub.lang;
      await env.SUBS.put(await keyFor(sub.endpoint), JSON.stringify(record));
      return json({ ok: true }, 201, origin);
    }

    // ── Public: remove a subscription (user turned alerts off). ──────────────
    if (path === "/unsubscribe" && request.method === "POST") {
      let body;
      try {
        body = await request.json();
      } catch (e) {
        return json({ error: "invalid json" }, 400, origin);
      }
      if (!body || !body.endpoint) return json({ error: "missing endpoint" }, 400, origin);
      await env.SUBS.delete(await keyFor(body.endpoint));
      return json({ ok: true }, 200, origin);
    }

    // ── Public: count one headline click (the "most clicked here" signal). ───
    // Body: { url, source?, title? }. Sent via navigator.sendBeacon on click;
    // the site also de-dupes per browser per day, so one reader re-opening an
    // article doesn't stack counts.
    if (path === "/click" && request.method === "POST") {
      let body;
      try {
        body = await request.json();
      } catch (e) {
        return json({ error: "invalid json" }, 400, origin);
      }
      const u = typeof body?.url === "string" ? body.url.slice(0, 600) : "";
      if (!/^https?:\/\//i.test(u)) return json({ error: "missing url" }, 400, origin);
      const key = `clk:${utcDay()}:${(await sha256hex(u)).slice(0, 32)}`;
      let rec = null;
      try {
        rec = JSON.parse((await env.SUBS.get(key)) || "null");
      } catch (e) { /* corrupt record → start over */ }
      rec = rec && typeof rec.n === "number" ? rec : { n: 0, url: u };
      rec.n += 1;
      if (typeof body.source === "string") rec.source = body.source.slice(0, 60);
      if (typeof body.title === "string") rec.title = body.title.slice(0, 200);
      await env.SUBS.put(key, JSON.stringify(rec),
        { expirationTtl: CLICK_TTL_DAYS * 86400 });
      return json({ ok: true }, 201, origin);
    }

    // ── Public: rolling click aggregate, summed per article over ?days. ──────
    // Read by the GitHub Action (fetch_headlines.py ranks each outlet's
    // headlines by it) and open to anyone — counts aren't sensitive.
    if (path === "/clicks" && request.method === "GET") {
      const days = Math.max(1, Math.min(
        Number(url.searchParams.get("days")) || CLICK_WINDOW_DEFAULT,
        CLICK_WINDOW_MAX));
      const wanted = new Set();
      for (let i = 0; i < days; i++) wanted.add(utcDay(i));
      const byUrl = new Map();
      let cursor, read = 0;
      do {
        const page = await env.SUBS.list({ prefix: "clk:", cursor });
        for (const k of page.keys) {
          if (read >= CLICK_LIST_CAP) break;
          const day = k.name.slice(4, 14);
          if (!wanted.has(day)) continue;
          read++;
          let rec;
          try {
            rec = JSON.parse((await env.SUBS.get(k.name)) || "null");
          } catch (e) { continue; }
          if (!rec || typeof rec.n !== "number" || !rec.url) continue;
          const cur = byUrl.get(rec.url) || { url: rec.url, n: 0 };
          cur.n += rec.n;
          if (rec.source) cur.source = rec.source;
          if (rec.title) cur.title = rec.title;
          byUrl.set(rec.url, cur);
        }
        cursor = page.list_complete ? undefined : page.cursor;
      } while (cursor && read < CLICK_LIST_CAP);
      const clicks = [...byUrl.values()].sort((a, b) => b.n - a.n);
      return new Response(JSON.stringify({ days, clicks }), {
        status: 200,
        headers: {
          "content-type": "application/json",
          // Cheap CDN caching: rankings refresh at most every ~30 min anyway.
          "cache-control": "public, max-age=300",
          ...cors(origin),
        },
      });
    }

    // ── Protected: the sender reads every subscription. ──────────────────────
    if (path === "/subscriptions" && request.method === "GET") {
      if (!authed(env, request)) return json({ error: "unauthorized" }, 401, origin);
      const out = [];
      let cursor;
      do {
        const page = await env.SUBS.list({ prefix: "sub:", cursor });
        for (const k of page.keys) {
          const v = await env.SUBS.get(k.name);
          if (v) out.push(JSON.parse(v));
        }
        cursor = page.list_complete ? undefined : page.cursor;
      } while (cursor);
      return json({ subscriptions: out }, 200, origin);
    }

    // ── Protected: the sender prunes endpoints Push rejected (410/404). ──────
    if (path === "/prune" && request.method === "POST") {
      if (!authed(env, request)) return json({ error: "unauthorized" }, 401, origin);
      let body;
      try {
        body = await request.json();
      } catch (e) {
        return json({ error: "invalid json" }, 400, origin);
      }
      const endpoints = (body && body.endpoints) || [];
      let removed = 0;
      for (const ep of endpoints) {
        await env.SUBS.delete(await keyFor(ep));
        removed++;
      }
      return json({ ok: true, removed }, 200, origin);
    }

    return json({ error: "not found" }, 404, origin);
  },
};
