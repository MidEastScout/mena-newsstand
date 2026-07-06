/**
 * The Middle East Scout — push subscription store (Cloudflare Worker).
 *
 * A static GitHub Pages site cannot keep a database, so this tiny Worker is the
 * one piece of server we run. It does exactly three things:
 *
 *   1. Hands the browser the VAPID public key it needs to subscribe.
 *   2. Stores / removes push subscriptions in a KV namespace.
 *   3. Lets the GitHub Action (the sender) read every subscription, and prune
 *      the dead ones, guarded by a bearer token.
 *
 * It NEVER sends the notifications itself — the hourly GitHub Action does that
 * with the VAPID *private* key, which never touches this Worker.
 *
 * Bindings it expects (see wrangler.toml):
 *   - SUBS              KV namespace (stores subscriptions)
 *   - VAPID_PUBLIC_KEY  plain var  (the base64url application server key)
 *   - ADMIN_TOKEN       secret     (bearer for /subscriptions and /prune)
 *   - ALLOW_ORIGIN      plain var  (optional; the site origin for CORS, or "*")
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
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
  const hex = [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return "sub:" + hex;
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
