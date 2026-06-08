#!/usr/bin/env python3
"""Fetches top headlines from 16 MENA outlets via RSS and writes headlines.json.

Many outlets' own RSS feeds return 403 to datacenter IPs (GitHub Actions
runners) behind Cloudflare/Akamai. When an outlet's native feed fails or comes
back empty, we fall back to Google News' RSS, scoped to that outlet's domain —
Google News is not blocked from datacenter IPs, so this rescues most outlets.
Links then point at Google News' redirect, which resolves to the original
article in the browser.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

SOURCES = {
    "Gulf": [
        {
            "source": "Arab News", "country": "Saudi Arabia", "lang": "en",
            "url": "https://www.arabnews.com",
            "rss": "https://www.arabnews.com/cms/rss/section/1.xml",
        },
        {
            "source": "The National", "country": "UAE", "lang": "en",
            "url": "https://www.thenationalnews.com",
            "rss": "https://www.thenationalnews.com/rss.xml",
        },
        {
            "source": "Gulf News", "country": "UAE", "lang": "en",
            "url": "https://gulfnews.com",
            "rss": "https://gulfnews.com/rss",
        },
        {
            "source": "Gulf Times", "country": "Qatar", "lang": "en",
            "url": "https://www.gulf-times.com",
            "rss": "https://www.gulf-times.com/rss",
        },
        {
            "source": "Times of Oman", "country": "Oman", "lang": "en",
            "url": "https://timesofoman.com",
            "rss": "https://timesofoman.com/rss",
        },
    ],
    "Levant": [
        {
            "source": "Jordan Times", "country": "Jordan", "lang": "en",
            "url": "https://www.jordantimes.com",
            "rss": "https://jordantimes.com/feed",
        },
        {
            "source": "L'Orient Today", "country": "Lebanon", "lang": "en",
            "url": "https://today.lorientlejour.com",
            "rss": "https://today.lorientlejour.com/feed",
        },
        {
            "source": "Egypt Independent", "country": "Egypt", "lang": "en",
            "url": "https://egyptindependent.com",
            "rss": "https://egyptindependent.com/feed/",
        },
        {
            "source": "Al-Akhbar", "country": "Lebanon", "lang": "ar",
            "url": "https://al-akhbar.com",
            "rss": "https://al-akhbar.com/rss",
        },
    ],
    "Israel": [
        {
            "source": "Jerusalem Post", "country": "Israel", "lang": "en",
            "url": "https://www.jpost.com",
            "rss": "https://www.jpost.com/rss/rssfeedsfrontpage.aspx",
        },
        {
            "source": "Times of Israel", "country": "Israel", "lang": "en",
            "url": "https://www.timesofisrael.com",
            "rss": "https://www.timesofisrael.com/feed/",
        },
        {
            "source": "Haaretz", "country": "Israel", "lang": "en",
            "url": "https://www.haaretz.com",
            "rss": "https://www.haaretz.com/srv/htz---all-articles",
        },
    ],
    "Pan-Arab": [
        {
            "source": "Al Jazeera", "country": "Qatar", "lang": "en",
            "url": "https://www.aljazeera.com",
            "rss": "https://www.aljazeera.com/xml/rss/all.xml",
        },
        {
            "source": "Middle East Eye", "country": "UK", "lang": "en",
            "url": "https://www.middleeasteye.net",
            "rss": "https://www.middleeasteye.net/rss",
        },
        {
            "source": "Al Arabiya", "country": "UAE", "lang": "en",
            "url": "https://english.alarabiya.net",
            "rss": "https://english.alarabiya.net/tools/rss",
        },
        {
            "source": "The New Arab", "country": "UK", "lang": "en",
            "url": "https://www.newarab.com",
            "rss": "https://www.newarab.com/rss",
        },
    ],
}

HEADLINES_PER_OUTLET = 5
REQUEST_TIMEOUT = 20

# Google News RSS locale per language, so Arabic outlets get Arabic results.
GNEWS_LOCALE = {
    "en": ("en-US", "US", "US:en"),
    "ar": ("ar", "EG", "EG:ar"),
    "he": ("he", "IL", "IL:he"),
    "tr": ("tr", "TR", "TR:tr"),
    "fr": ("fr", "FR", "FR:fr"),
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def parse_date(entry) -> str:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return ""


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def gnews_url(meta: dict) -> str:
    """Google News RSS search scoped to the outlet's domain, last 24h."""
    hl, gl, ceid = GNEWS_LOCALE.get(meta["lang"], GNEWS_LOCALE["en"])
    query = quote_plus(f"site:{domain_of(meta['url'])} when:1d")
    return f"https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"


def parse_feed(session: requests.Session, url: str, referer: str | None):
    """Return a list of {title,url,published} or None on failure/empty."""
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        print(f"      ! {url} -> {exc}", file=sys.stderr)
        return None
    feed = feedparser.parse(resp.content)
    items = [
        {
            "title": e.title.strip(),
            "url": e.get("link") or e.get("id", ""),
            "published": parse_date(e),
        }
        for e in feed.entries[:HEADLINES_PER_OUTLET]
        if e.get("title") and (e.get("link") or e.get("id"))
    ]
    return items or None


def fetch_outlet(session: requests.Session, meta: dict) -> dict:
    result = {
        "source": meta["source"],
        "country": meta["country"],
        "lang": meta["lang"],
        "url": meta["url"],
        "headlines": [],
        "error": None,
    }
    # 1) Try the outlet's own feed (own domain as Referer dodges some blocks).
    items = parse_feed(session, meta["rss"], meta["url"] + "/")
    via = "native"
    # 2) Fall back to Google News scoped to the outlet's domain.
    if not items:
        items = parse_feed(session, gnews_url(meta), "https://news.google.com/")
        via = "google-news"
    if items:
        result["headlines"] = items
        print(f"  + {meta['source']}: {len(items)} headlines ({via})")
    else:
        result["error"] = "no entries"
        print(f"  x {meta['source']}: no entries (native + google-news failed)",
              file=sys.stderr)
    return result


def main():
    session = requests.Session()
    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "regions": {},
    }
    for region, sources in SOURCES.items():
        print(f"\n[{region}]")
        output["regions"][region] = [fetch_outlet(session, s) for s in sources]

    out_path = Path(__file__).parent.parent / "headlines.json"
    out_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    total = sum(
        len(outlet["headlines"])
        for outlets in output["regions"].values()
        for outlet in outlets
    )
    ok = sum(
        1 for outlets in output["regions"].values()
        for outlet in outlets if outlet["headlines"]
    )
    print(f"\nWrote {out_path} — {total} headlines, {ok} outlets live")


if __name__ == "__main__":
    main()
