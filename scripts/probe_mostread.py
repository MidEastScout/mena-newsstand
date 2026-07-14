#!/usr/bin/env python3
"""One-off diagnostic (run on GitHub Actions — the outlet hosts aren't reachable
from dev sandboxes): does any of our outlets expose a usable "most read / most
popular / trending" signal?

For EVERY outlet in fetch_headlines.SOURCES it checks, in order of usefulness:

  1. CANDIDATE FEEDS — common most-read RSS endpoint patterns on the outlet's
     domain (e.g. /rss/mostread, /mostviewed, /?feed=popular), plus whatever the
     outlet's own homepage / RSS-hub page advertises. A hit here is the gold
     signal: a machine-readable ranking by real reader behaviour.

  2. HOMEPAGE MODULES — a "most read" section in the homepage HTML itself
     (English / Arabic / Farsi / Turkish / Hebrew markers). Counts how many
     article links sit next to the marker so we can tell a server-rendered,
     scrapable module apart from a JS-rendered placeholder (unscrapable from a
     feed run).

Prints a human-readable verdict per outlet to the job log; the findings get
baked into fetch_headlines.py as per-outlet `mostread` config. Safe to delete
this file once the answers are baked in.
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests

sys.path.insert(0, str(Path(__file__).parent))
from fetch_headlines import HEADERS, SOURCES, domain_of

TIMEOUT = 10
PAUSE = 0.15

# Common most-read endpoint patterns, tried on the outlet's site root (and on
# its base path when the outlet lives under one, e.g. aa.com.tr/en).
GENERIC_PATHS = [
    "/mostread", "/most-read", "/most_read", "/mostviewed", "/most-viewed",
    "/most-popular", "/mostpopular", "/popular", "/trending",
    "/rss/mostread", "/rss/most-read", "/rss/mostviewed", "/rss/popular",
    "/rss/most-popular", "/feed/mostread", "/feed/popular",
    "/feeds/mostread.xml", "/cms/rss/mostread", "/cms/rss/mostread.xml",
    "/?feed=mostread", "/?feed=popular",
]

# Per-outlet RSS hub / directory pages, scanned for advertised most-read feeds
# (in addition to the homepage).
EXTRA_SCAN_PAGES = {
    "Gulf News": ["https://gulfnews.com/rss"],
    "Al Arabiya": ["https://english.alarabiya.net/tools/rss"],
    "Daily Sabah": ["https://www.dailysabah.com/rss"],
    "Hürriyet Daily News": ["https://www.hurriyetdailynews.com/rss"],
    "N12": ["https://www.mako.co.il/help-rss"],
    "Arab News": ["https://www.arabnews.com/rss.xml"],
}

# "Most read"-ish markers, by script. Matched case-insensitively in raw HTML.
MARKERS = [
    # English
    "most read", "most-read", "mostread", "most popular", "most-popular",
    "most viewed", "most-viewed", "mostviewed", "trending now", "popular now",
    # Arabic
    "الأكثر قراءة", "الاكثر قراءة", "الأكثر مشاهدة", "الاكثر مشاهدة",
    "الأكثر تداول", "الأكثر زيارة", "الأكثر تفاعل",
    # Farsi
    "پربازدید", "پربیننده", "پرمخاطب",
    # Turkish
    "en çok okunan", "çok okunan", "en cok okunan",
    # Hebrew
    "הנצפות ביותר", "הנקראות ביותר", "הכי נצפות", "הכי נקראות", "החמות ביותר",
]

# Link URLs / anchor text that smell like a most-read page or feed.
LINKY_RE = re.compile(
    r"most[-_]?(read|viewed|popular|shared|emailed)|popular|trending|"
    r"cok[-_]?okunan|okunanlar|pribazdid|mostview", re.I)
FEED_LINK_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
ANCHOR_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def get(session, url, referer=None):
    headers = dict(HEADERS)
    headers["Accept"] = "text/html,application/rss+xml,application/xml,*/*"
    if referer:
        headers["Referer"] = referer
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        return r, f"{r.status_code} {r.headers.get('Content-Type', '?').split(';')[0]} {len(r.content)}B"
    except Exception as e:
        return None, f"ERR {type(e).__name__}: {str(e)[:60]}"


def classify(resp, outlet_domain):
    """What did this URL give us? → ('feed', n_entries, titles) |
    ('page', n_article_links, samples) | ('empty', 0, [])."""
    body = resp.content
    feed = feedparser.parse(body)
    if feed.entries:
        titles = [(e.get("title") or "")[:70] for e in feed.entries[:3]]
        return "feed", len(feed.entries), titles
    html = resp.text
    links = article_links(html, resp.url, outlet_domain)
    if len(links) >= 3:
        return "page", len(links), links[:3]
    return "empty", 0, []


def article_links(html, base, outlet_domain):
    """Same-domain links that look like articles (long-ish path)."""
    seen, out = set(), []
    for m in HREF_RE.finditer(html):
        u = urljoin(base, m.group(1))
        p = urlparse(u)
        if outlet_domain not in p.netloc.lower():
            continue
        if len(p.path.strip("/")) < 12:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def scan_page_for_leads(html, base_url, outlet_domain):
    """Harvest (a) advertised RSS/Atom feed URLs, (b) links whose URL or anchor
    text smells like most-read, (c) HTML markers with a same-block article-link
    count (server-rendered vs JS-rendered)."""
    leads, markers = set(), []
    for tag in FEED_LINK_RE.finditer(html):
        m = HREF_RE.search(tag.group(0))
        if m:
            u = urljoin(base_url, m.group(1))
            if LINKY_RE.search(u):
                leads.add(u)
    for m in ANCHOR_RE.finditer(html):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        if LINKY_RE.search(href) or any(mk in text.lower() for mk in
                                        ("most read", "most popular", "most viewed")):
            u = urljoin(base_url, href)
            if outlet_domain in urlparse(u).netloc.lower():
                leads.add(u)
    low = html.lower()
    for mk in MARKERS:
        idx = low.find(mk.lower())
        if idx < 0:
            continue
        window = html[idx: idx + 6000]
        n = len(article_links(window, base_url, outlet_domain))
        markers.append({"marker": mk, "links_nearby": n})
    return sorted(leads), markers


def probe_outlet(meta):
    session = requests.Session()
    source, base = meta["source"], meta["url"].rstrip("/")
    dom = domain_of(base)
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    report = {"source": source, "domain": dom, "feeds": [], "pages": [],
              "markers": [], "homepage": ""}

    # 1. Homepage (+ any RSS hub pages): markers + harvested leads.
    lead_urls = []
    scan_pages = [base + "/"] + EXTRA_SCAN_PAGES.get(source, [])
    for page in scan_pages:
        r, status = get(session, page)
        if page == base + "/":
            report["homepage"] = status
        if not r or r.status_code != 200:
            continue
        leads, markers = scan_page_for_leads(r.text, page, dom)
        lead_urls.extend(leads)
        for mk in markers:
            mk["on"] = page
        report["markers"].extend(markers)
        time.sleep(PAUSE)

    # 2. Candidate endpoints: generic patterns + harvested leads.
    candidates, seen = [], set()
    for path in GENERIC_PATHS:
        for b in {root, base}:
            u = b + path
            if u not in seen:
                seen.add(u)
                candidates.append(u)
    for u in lead_urls[:8]:
        if u not in seen:
            seen.add(u)
            candidates.append(u)

    for u in candidates:
        r, status = get(session, u, referer=base + "/")
        if r is None or r.status_code != 200:
            continue
        kind, n, samples = classify(r, dom)
        if kind == "feed":
            report["feeds"].append({"url": u, "entries": n, "titles": samples,
                                    "status": status})
        elif kind == "page":
            report["pages"].append({"url": u, "links": n, "samples": samples[:2],
                                    "status": status})
        time.sleep(PAUSE)
    return report


def main():
    outlets = [m for outs in SOURCES.values() for m in outs]
    print(f"Probing {len(outlets)} outlets for most-read/trending signals…\n")
    with ThreadPoolExecutor(max_workers=6) as ex:
        reports = list(ex.map(probe_outlet, outlets))

    for rep in reports:
        print(f"── {rep['source']} ({rep['domain']}) — homepage {rep['homepage']}")
        for f in rep["feeds"]:
            print(f"   FEED  {f['url']}  [{f['entries']} entries]")
            for t in f["titles"]:
                print(f"         · {t}")
        for p in rep["pages"]:
            print(f"   PAGE  {p['url']}  [{p['links']} article links]")
            for s in p["samples"]:
                print(f"         · {s}")
        for mk in rep["markers"]:
            state = "server-rendered" if mk["links_nearby"] >= 3 else "JS/empty"
            print(f"   MARK  {mk['marker']!r} on {mk['on']}  "
                  f"[{mk['links_nearby']} links nearby → {state}]")
        if not (rep["feeds"] or rep["pages"] or rep["markers"]):
            print("   — nothing found")
        print()

    n_feed = sum(1 for r in reports if r["feeds"])
    n_page = sum(1 for r in reports if r["pages"])
    n_mark = sum(1 for r in reports if any(m["links_nearby"] >= 3 for m in r["markers"]))
    print(f"SUMMARY: {n_feed} outlets with a candidate FEED, "
          f"{n_page} with a most-read PAGE, "
          f"{n_mark} with a server-rendered homepage module "
          f"(of {len(reports)} probed)")
    out = Path(__file__).parent.parent / "state" / "probe_mostread.json"
    out.write_text(json.dumps(reports, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
