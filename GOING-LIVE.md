# Making the Newsstand a fully independent website

This is a plain-language guide. You do **not** need to be a developer to follow it.

## Where things stand today

Your site is **already a real, public website**. Anyone can open it here:

> https://roiebe23.github.io/mena-newsstand/

It refreshes its own headlines every 30 minutes and its front pages twice a day,
with no computer of yours needing to be on. GitHub does the work for free.

So "going live" is **not** about getting online — you already are. It's about two
upgrades that make it feel like its own product instead of "a GitHub page":

1. **Your own domain name** (e.g. `menanewsstand.com`) instead of the long
   `roiebe23.github.io/...` address.
2. **Looking polished everywhere** — a proper title, an icon in the browser tab,
   a nice preview card when you share the link, and being findable on Google.

Upgrade #2 is **already done** (see below). Upgrade #1 needs a few clicks from
you because it costs a little money and uses accounts only you can log into.

---

## What's already done for you (no action needed)

- **Browser-tab icon** (a little newspaper) — `favicon.svg`
- **Page title & description** — so search engines and shared links read well
- **Share preview** — pasting the link into WhatsApp/Twitter/Slack now shows a
  title, description, and a newspaper cover image (Open Graph / Twitter cards)
- **Installable** — on a phone you can "Add to Home Screen" and it opens like an
  app (`site.webmanifest`)
- **Search-engine files** — `robots.txt` and `sitemap.xml` so Google can index it

---

## What only YOU can do: give it your own domain (about 10–15 minutes)

### Step 1 — Buy a domain name (~$10–15 per year)
Use any registrar. The cheapest, no-nonsense ones:
- **Porkbun** — porkbun.com
- **Cloudflare** — cloudflare.com (at-cost pricing, no upsells)
- (Namecheap, Google/Squarespace Domains also fine)

Pick a name you like, e.g. `menanewsstand.com`. Pay. That's the only cost.

### Step 2 — Point the domain at the site (copy-paste DNS records)
In your registrar's **DNS settings**, add these records.

**For the bare domain (e.g. `menanewsstand.com`)** — add four "A" records, all
with name `@`:

```
A   @   185.199.108.153
A   @   185.199.109.153
A   @   185.199.110.153
A   @   185.199.111.153
```

**For the `www` version** — add one "CNAME" record:

```
CNAME   www   roiebe23.github.io
```

(These four IP addresses are GitHub's official addresses for websites — they do
not change.)

### Step 3 — Turn it on in GitHub (1 minute)
1. Open your repo → **Settings** → **Pages**.
2. Under **Custom domain**, type your domain (e.g. `menanewsstand.com`) → **Save**.
3. Wait until the green check appears, then tick **Enforce HTTPS** (this gives
   you the padlock / `https://`). DNS can take anywhere from a few minutes to a
   few hours to "spread", so if it's not ready immediately, check back later.

That's it — your site now answers at your own domain, still updating itself.

### Step 4 — Tell me the domain, and I'll finish the inside
Once you own the domain, send it to me. I'll update the site's internal links
(the share-preview address, the canonical link, sitemap, robots, and the footer)
to use your new domain instead of the github.io address, and add the small
`CNAME` file GitHub wants in the repo. That part I *can* do.

---

## Optional: hosting it "off GitHub"

If you specifically want it served by something other than GitHub (it still
reads from this same repo), the easiest free options are:
- **Cloudflare Pages** — connect this GitHub repo, it deploys automatically.
- **Netlify** — same idea.

You'd point your domain at them instead of at GitHub in Step 2. There is **no
real benefit** for a site like this — GitHub Pages is free, fast, and already
working — so unless you have a reason, a custom domain on GitHub Pages
(Steps 1–3 above) is the recommended path.

Note: the **data engine** (the scripts that fetch headlines and front pages)
runs on GitHub Actions and is free. Keeping that on GitHub is the simplest setup
no matter where the website itself is hosted.

---

## Cost & effort summary

| Item | Cost | Who | Time |
|---|---|---|---|
| The website itself (hosting) | Free | already done | — |
| Auto-updates (headlines, covers, email) | Free | already done | — |
| Polish (icon, SEO, share preview) | Free | already done | — |
| Domain name | ~$10–15 / year | you | 5 min |
| DNS + turn-on | Free | you | 5–10 min |
| Wire the domain into the site | Free | me, after you buy it | — |
