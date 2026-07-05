#!/usr/bin/env python3
"""Targeted probe (run on GitHub Actions — these hosts aren't reachable from
dev sandboxes). Two jobs, one run:

A. DEAD COVERS — the manifest shows the_national / gulf_news / wsj / usa_today
   frozen for 2+ days. For each, test every candidate source (current + likely
   alternates) across the last 6 days, so we can tell a genuinely dead source
   apart from a no-print day (WSJ has no Sunday paper; USA Today none on
   weekends) and bake a working alternate into fetch_frontpages.py.

B. ISRAELI CHANNEL FEEDS — feasibility check for adding Kan 11, N12 (Mako /
   Channel 12) and Channel 13 headlines. Tests candidate RSS endpoints and
   scrapes each site's pages for advertised feeds; reports item counts, latest
   pubDate and sample (Hebrew) titles.

Writes state/probe_sources.json (committed by the workflow) and prints a
human-readable report to the job log.
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 20
MIN_BYTES = 12000
PAUSE = 0.2

today = datetime.now(timezone.utc).date()
DAYS = [today - timedelta(days=k) for k in range(6)]   # today .. today-5


def get(session, url, as_image=False, referer=None):
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,he;q=0.8"}
    if referer:
        headers["Referer"] = referer
    headers["Accept"] = ("image/avif,image/webp,image/*,*/*" if as_image
                         else "text/html,application/xhtml+xml,application/xml,*/*")
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as e:
        return None, f"ERR {type(e).__name__}: {str(e)[:70]}"
    ct = r.headers.get("Content-Type", "")
    if as_image:
        ok = (r.status_code == 200 and ct.startswith("image/")
              and len(r.content) >= MIN_BYTES)
        return (r if ok else None), f"{r.status_code} {ct or '?'} {len(r.content)}B"
    return (r if r.status_code == 200 else None), f"{r.status_code} {ct or '?'} {len(r.content)}B"


# ---------------------------------------------------------------------------
# A. Dead covers
# ---------------------------------------------------------------------------
# Per paper: candidate (label, url_template) — {d} is a date.
COVER_CANDIDATES = {
    "the_national": [
        ("ff UAE_TN",        lambda d: f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/UAE_TN.jpg"),
        ("kiosko asi",       lambda d: f"https://img.kiosko.net/{d:%Y/%m/%d}/asi/the_national.750.jpg"),
        ("kiosko ae",        lambda d: f"https://img.kiosko.net/{d:%Y/%m/%d}/ae/the_national.750.jpg"),
    ],
    "gulf_news": [
        ("ff UAE_GN",        lambda d: f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/UAE_GN.jpg"),
        ("kiosko asi",       lambda d: f"https://img.kiosko.net/{d:%Y/%m/%d}/asi/gulf_news.750.jpg"),
        ("kiosko ae",        lambda d: f"https://img.kiosko.net/{d:%Y/%m/%d}/ae/gulf_news.750.jpg"),
    ],
    "wsj": [
        ("ff WSJ",           lambda d: f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/WSJ.jpg"),
        ("ff NY_WSJ",        lambda d: f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/NY_WSJ.jpg"),
        ("kiosko us",        lambda d: f"https://img.kiosko.net/{d:%Y/%m/%d}/us/wsj.750.jpg"),
    ],
    "usa_today": [
        ("ff USAT",          lambda d: f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/USAT.jpg"),
        ("ff VA_USAT",       lambda d: f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/VA_USAT.jpg"),
        ("kiosko us",        lambda d: f"https://img.kiosko.net/{d:%Y/%m/%d}/us/usa_today.750.jpg"),
    ],
    # control — a paper that refreshes fine via FF, to separate "FF is broken"
    # from "these specific codes are dead".
    "kuwait_times (control)": [
        ("ff KUW_KT",        lambda d: f"https://cdn.freedomforum.org/dfp/jpg{d.day}/lg/KUW_KT.jpg"),
    ],
}

REFS = {"kiosko.net": "https://en.kiosko.net/",
        "freedomforum.org": "https://www.freedomforum.org/todaysfrontpages/"}


def ref_for(url):
    for host, ref in REFS.items():
        if host in url:
            return ref
    return None


def probe_covers(session):
    print("\n" + "=" * 70)
    print("A. DEAD-COVER CANDIDATES — ok/miss per source per day")
    print("=" * 70)
    out = {}
    for paper, cands in COVER_CANDIDATES.items():
        print(f"\n--- {paper} ---")
        out[paper] = []
        for label, mk in cands:
            row = {"source": label, "days": {}}
            hits = []
            for d in DAYS:
                url = mk(d)
                r, info = get(session, url, as_image=True, referer=ref_for(url))
                row["days"][d.isoformat()] = {"ok": bool(r), "info": info, "url": url}
                if r:
                    hits.append(d.isoformat())
                time.sleep(PAUSE)
            marks = " ".join(("OK  " if row["days"][d.isoformat()]["ok"] else "-   ")
                             for d in DAYS)
            print(f"  {label:14s} [{' '.join(d.strftime('%d') for d in DAYS)}]  {marks}"
                  f"   newest hit: {hits[0] if hits else 'NONE'}")
            out[paper].append(row)
    return out


# ---------------------------------------------------------------------------
# B. Israeli channel feeds
# ---------------------------------------------------------------------------
FEED_CANDIDATES = [
    # (channel, label, url)
    ("kan11", "rss index",        "https://www.kan.org.il/rss/"),
    ("kan11", "homepage",         "https://www.kan.org.il/"),
    ("kan11", "news lobby",       "https://www.kan.org.il/lobby/kan-news/"),
    ("n12",   "rss index page",   "https://www.mako.co.il/help-rss"),
    ("n12",   "news-israel",      "https://rcs.mako.co.il/rss/news-israel.xml"),
    ("n12",   "news-military",    "https://rcs.mako.co.il/rss/news-military.xml"),
    ("n12",   "news-world",       "https://rcs.mako.co.il/rss/news-world.xml"),
    ("n12",   "news-politics",    "https://rcs.mako.co.il/rss/news-politics.xml"),
    ("ch13",  "wp feed",          "https://13tv.co.il/feed/"),
    ("ch13",  "news wp feed",     "https://13tv.co.il/news/feed/"),
    ("ch13",  "13news feed",      "https://13news.co.il/feed/"),
    ("ch13",  "homepage",         "https://13tv.co.il/"),
]

FEED_LINK_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
RSS_URL_RE = re.compile(r'https?://[^\s"\'<>()]+(?:rss|feed)[^\s"\'<>()]*', re.I)


def summarize_feed(text):
    """Light-touch RSS/Atom summary: item count, newest date, sample titles."""
    try:
        import feedparser
        f = feedparser.parse(text)
        if not f.entries:
            return None
        titles = [e.get("title", "") for e in f.entries[:3]]
        newest = ""
        for e in f.entries:
            for k in ("published", "updated"):
                if e.get(k):
                    newest = max(newest, e[k]) if newest else e[k]
                    break
        return {"items": len(f.entries), "newest": newest, "sample_titles": titles}
    except Exception as e:
        return {"parse_error": f"{type(e).__name__}: {e}"}


def probe_feeds(session):
    print("\n" + "=" * 70)
    print("B. ISRAELI CHANNEL FEEDS — Kan 11 / N12 (Mako) / Channel 13")
    print("=" * 70)
    out = []
    for channel, label, url in FEED_CANDIDATES:
        r, info = get(session, url)
        entry = {"channel": channel, "label": label, "url": url, "info": info}
        print(f"\n  [{channel}] {label}: {url}\n      -> {info}")
        if r is not None:
            body = r.text
            ct = r.headers.get("Content-Type", "")
            looks_xml = ("xml" in ct or body.lstrip()[:200].startswith("<?xml")
                         or "<rss" in body[:2000] or "<feed" in body[:2000])
            if looks_xml:
                s = summarize_feed(body)
                entry["feed"] = s
                if s and s.get("items"):
                    print(f"      FEED OK: {s['items']} items, newest {s.get('newest','?')}")
                    for t in s.get("sample_titles", []):
                        print(f"        · {t[:80]}")
            else:
                # HTML page: harvest advertised feed links for follow-up.
                links = []
                for tag in FEED_LINK_RE.findall(body):
                    m = HREF_RE.search(tag)
                    if m:
                        links.append(m.group(1))
                links += RSS_URL_RE.findall(body)
                links = list(dict.fromkeys(links))[:15]
                entry["advertised_feeds"] = links
                if links:
                    print(f"      advertised feeds ({len(links)}):")
                    for l in links[:10]:
                        print(f"        · {l[:100]}")
        out.append(entry)
        time.sleep(PAUSE)

    # Second pass: fetch any advertised feed URLs we discovered on HTML pages.
    print("\n  --- second pass: discovered feed URLs ---")
    seen = {e["url"] for e in out}
    for e in list(out):
        for l in e.get("advertised_feeds", []) or []:
            if not l.startswith("http"):
                base = re.match(r"https?://[^/]+", e["url"]).group(0)
                l = base + (l if l.startswith("/") else "/" + l)
            if l in seen or len(seen) > 60:
                continue
            seen.add(l)
            r, info = get(session, l)
            sub = {"channel": e["channel"], "label": "discovered", "url": l, "info": info}
            if r is not None and ("xml" in r.headers.get("Content-Type", "")
                                  or "<rss" in r.text[:2000] or "<feed" in r.text[:2000]):
                s = summarize_feed(r.text)
                sub["feed"] = s
                if s and s.get("items"):
                    print(f"  [{e['channel']}] {l[:80]}\n      FEED OK: {s['items']} items, "
                          f"newest {s.get('newest','?')}")
                    for t in s.get("sample_titles", [])[:2]:
                        print(f"        · {t[:80]}")
            out.append(sub)
            time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
# C. Follow-ups from the first probe run: (1) what does Kan's newsflash XML
#    actually look like (schema sample)? (2) does the fetcher's existing
#    Google-News fallback carry each channel's site in the Hebrew locale?
#    The GNews URLs mirror gnews_url() in fetch_headlines.py exactly.
# ---------------------------------------------------------------------------
from urllib.parse import quote_plus

GNEWS_SITES = ["13tv.co.il", "kan.org.il", "mako.co.il"]


def probe_followups(session):
    print("\n" + "=" * 70)
    print("C. FOLLOW-UPS — Kan newsflash schema + Google News hebrew fallback")
    print("=" * 70)
    out = {}

    url = "https://www.kan.org.il/api/newsflash/v2/Newsflash"
    r, info = get(session, url)
    entry = {"url": url, "info": info}
    print(f"\n  Kan newsflash API: {info}")
    if r is not None:
        entry["raw_head"] = r.text[:3000]
        print("  --- first 1500 chars ---")
        print(r.text[:1500])
        s = summarize_feed(r.text)
        entry["feedparse"] = s
        if s and s.get("items"):
            print(f"  feedparser: {s['items']} items, newest {s.get('newest','?')}")
            for t in s.get("sample_titles", [])[:3]:
                print(f"    · {t[:80]}")
    out["kan_newsflash"] = entry

    out["gnews"] = []
    for site in GNEWS_SITES:
        q = quote_plus(f"site:{site} when:2d")
        gurl = f"https://news.google.com/rss/search?q={q}&hl=he&gl=IL&ceid=IL:he"
        r, info = get(session, gurl)
        entry = {"site": site, "url": gurl, "info": info}
        print(f"\n  GNews he site:{site} -> {info}")
        if r is not None:
            s = summarize_feed(r.text)
            entry["feed"] = s
            if s and s.get("items"):
                print(f"      {s['items']} items, newest {s.get('newest','?')}")
                for t in s.get("sample_titles", [])[:3]:
                    print(f"        · {t[:80]}")
        out["gnews"].append(entry)
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
# D. Final follow-ups: (1) Channel 13 sitemaps — GNews stopped indexing
#    13tv.co.il in 2021 and there's no RSS, but WordPress sites list sitemaps in
#    robots.txt and often expose a news sitemap with the last 48h of articles.
#    (2) Kan newsflash parse variants — the API declares encoding="utf-16",
#    which can defeat feedparser on raw bytes; test the exact variants
#    fetch_headlines.parse_feed could use so the winner is known in advance.
# ---------------------------------------------------------------------------
SITEMAP_CANDIDATES = [
    "https://13tv.co.il/robots.txt",
    "https://13tv.co.il/sitemap_index.xml",
    "https://13tv.co.il/news-sitemap.xml",
    "https://13tv.co.il/sitemap.xml",
    "https://13tv.co.il/wp-sitemap.xml",
]


def probe_final(session):
    print("\n" + "=" * 70)
    print("D. FINAL — Channel 13 sitemaps + Kan parse variants")
    print("=" * 70)
    out = {"ch13_sitemaps": [], "kan_parse": {}}

    for url in SITEMAP_CANDIDATES:
        r, info = get(session, url)
        entry = {"url": url, "info": info}
        print(f"\n  {url}\n      -> {info}")
        if r is not None:
            body = r.text
            if url.endswith("robots.txt"):
                maps = re.findall(r"(?im)^sitemap:\s*(\S+)", body)
                entry["sitemaps"] = maps
                print(f"      robots.txt sitemaps: {maps}")
                for m in maps[:5]:
                    r2, info2 = get(session, m)
                    print(f"        {m} -> {info2}")
                    if r2 is not None:
                        locs = re.findall(r"<loc>([^<]+)</loc>", r2.text)[:8]
                        dates = re.findall(
                            r"<(?:news:publication_date|lastmod)>([^<]+)<", r2.text)[:4]
                        out["ch13_sitemaps"].append(
                            {"url": m, "info": info2, "locs": locs, "dates": dates})
                        print(f"          locs: {len(locs)} (first: {locs[0][:70] if locs else '-'})")
                        print(f"          dates: {dates[:3]}")
                    time.sleep(PAUSE)
            else:
                locs = re.findall(r"<loc>([^<]+)</loc>", body)[:8]
                dates = re.findall(r"<(?:news:publication_date|lastmod)>([^<]+)<", body)[:4]
                entry["locs"], entry["dates"] = locs, dates
                if locs:
                    print(f"      locs: {len(locs)} (first: {locs[0][:70]})  dates: {dates[:3]}")
                if "news_sitemap" in url:
                    # Nail the exact Google-news-sitemap schema before writing the
                    # parser: unique tag names + the first two <url> blocks raw.
                    tags = sorted(set(re.findall(r"<(/?[a-zA-Z0-9:_-]+)", body)))
                    entry["tag_names"] = tags
                    blocks = re.findall(r"<url>.*?</url>", body, re.S)[:2]
                    entry["raw_url_blocks"] = [b[:900] for b in blocks]
                    print(f"      tag names: {tags}")
                    for b in blocks:
                        print("      --- <url> block ---")
                        print("      " + b[:900].replace("\n", "\n      "))
        out["ch13_sitemaps"].append(entry)
        time.sleep(PAUSE)

    # Kan parse variants
    import feedparser
    url = "https://www.kan.org.il/api/newsflash/v2/Newsflash"
    headers = {"User-Agent": UA}
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT)
        variants = {
            "bytes": r.content,
            "text": r.text,
            "text_decl_stripped": re.sub(r"^\s*<\?xml[^>]*\?>", "", r.text, count=1),
        }
        for name, payload in variants.items():
            try:
                f = feedparser.parse(payload)
                n = len(f.entries)
                t0 = f.entries[0].get("title", "")[:60] if n else ""
                out["kan_parse"][name] = {"entries": n, "first_title": t0,
                                          "bozo": str(getattr(f, "bozo_exception", ""))[:90]}
                print(f"\n  kan variant {name:20s}: {n} entries  {t0}")
            except Exception as e:
                out["kan_parse"][name] = {"error": f"{type(e).__name__}: {e}"}
                print(f"\n  kan variant {name:20s}: ERROR {e}")
    except Exception as e:
        out["kan_parse"]["fetch_error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# E. Exercise the REAL fetch_headlines parsers against the three Israeli
#    channels on the runner — the definitive integration test before the live
#    pipeline touches them: Kan via parse_feed (declaration-strip retry), N12
#    via merged parse_feed, Channel 13 via parse_news_sitemap.
# ---------------------------------------------------------------------------
def probe_israel_parsers(session):
    print("\n" + "=" * 70)
    print("E. REAL PARSERS — fetch_headlines against Kan / N12 / Channel 13")
    print("=" * 70)
    out = {}
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import fetch_headlines as fh
    except Exception as e:
        print(f"  could not import fetch_headlines: {e}")
        return {"import_error": str(e)}

    cases = [
        ("kan11 (parse_feed)", fh.parse_feed,
         "https://www.kan.org.il/api/newsflash/v2/Newsflash"),
        ("n12 news-israel (parse_feed)", fh.parse_feed,
         "https://rcs.mako.co.il/rss/news-israel.xml"),
        ("n12 news-world (parse_feed)", fh.parse_feed,
         "https://rcs.mako.co.il/rss/news-world.xml"),
        ("ch13 (parse_news_sitemap)", fh.parse_news_sitemap,
         "https://13tv.co.il/Services/sitemapGenerator/xmls/news_sitemap.xml"),
    ]
    for name, fn, url in cases:
        try:
            items = fn(session, url, None) or []
        except Exception as e:
            out[name] = {"error": f"{type(e).__name__}: {e}"}
            print(f"\n  {name}: ERROR {e}")
            continue
        sample = [{"title": it["title"][:70], "published": it["published"],
                   "url": it["url"][:60]} for it in items[:4]]
        out[name] = {"count": len(items), "sample": sample}
        print(f"\n  {name}: {len(items)} items")
        for s in sample:
            print(f"    · [{s['published'][:16]}] {s['title']}")
    return out


def main():
    session = requests.Session()
    report = {"ran": datetime.now(timezone.utc).isoformat(), "date": today.isoformat()}
    try:
        report["covers"] = probe_covers(session)
    except Exception as e:
        print(f"!! covers probe crashed: {type(e).__name__}: {e}", file=sys.stderr)
        report["covers"] = {"error": str(e)}
    try:
        report["israeli_feeds"] = probe_feeds(session)
    except Exception as e:
        print(f"!! feeds probe crashed: {type(e).__name__}: {e}", file=sys.stderr)
        report["israeli_feeds"] = {"error": str(e)}
    try:
        report["followups"] = probe_followups(session)
    except Exception as e:
        print(f"!! follow-up probe crashed: {type(e).__name__}: {e}", file=sys.stderr)
        report["followups"] = {"error": str(e)}
    try:
        report["final"] = probe_final(session)
    except Exception as e:
        print(f"!! final probe crashed: {type(e).__name__}: {e}", file=sys.stderr)
        report["final"] = {"error": str(e)}
    try:
        report["israel_parsers"] = probe_israel_parsers(session)
    except Exception as e:
        print(f"!! israel-parser probe crashed: {type(e).__name__}: {e}", file=sys.stderr)
        report["israel_parsers"] = {"error": str(e)}

    out_path = Path(__file__).parent.parent / "state" / "probe_sources.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
