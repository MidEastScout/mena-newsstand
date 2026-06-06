#!/usr/bin/env python3
"""Fetches top headlines from 16 MENA outlets via RSS and writes headlines.json."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

SOURCES = {
    "Gulf": [
        {
            "source": "Arab News", "country": "Saudi Arabia", "lang": "en",
            "url": "https://www.arabnews.com",
            "rss": "https://www.arabnews.com/rss.xml",
        },
        {
            "source": "The National", "country": "UAE", "lang": "en",
            "url": "https://www.thenationalnews.com",
            "rss": "https://www.thenationalnews.com/arc/outboundfeeds/rss/",
        },
        {
            "source": "Gulf News", "country": "UAE", "lang": "en",
            "url": "https://gulfnews.com",
            "rss": "https://gulfnews.com/rss/world",
        },
        {
            "source": "Gulf Times", "country": "Qatar", "lang": "en",
            "url": "https://www.gulf-times.com",
            "rss": "https://www.gulf-times.com/rss",
        },
        {
            "source": "Times of Oman", "country": "Oman", "lang": "en",
            "url": "https://timesofoman.com",
            "rss": "https://timesofoman.com/rss/news",
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
            "rss": "https://today.lorientlejour.com/feed/",
        },
        {
            "source": "Egypt Independent", "country": "Egypt", "lang": "en",
            "url": "https://egyptindependent.com",
            "rss": "https://egyptindependent.com/feed/",
        },
        {
            "source": "Al-Akhbar", "country": "Lebanon", "lang": "ar",
            "url": "https://al-akhbar.com",
            "rss": "https://al-akhbar.com/node/feed",
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
            "rss": "https://www.haaretz.com/cmlink/1.628765",
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
            "rss": "https://english.alarabiya.net/tools/rss/category/news",
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
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


def fetch_outlet(meta: dict) -> dict:
    result = {
        "source": meta["source"],
        "country": meta["country"],
        "lang": meta["lang"],
        "url": meta["url"],
        "headlines": [],
        "error": None,
    }
    try:
        response = requests.get(
            meta["rss"], headers=HEADERS, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        entries = [
            e for e in feed.entries[:HEADLINES_PER_OUTLET]
            if e.get("title") and e.get("link")
        ]
        result["headlines"] = [
            {
                "title": e.title.strip(),
                "url": e.link,
                "published": parse_date(e),
            }
            for e in entries
        ]
        if not result["headlines"]:
            result["error"] = "no entries"
            print(f"  - {meta['source']}: empty feed", file=sys.stderr)
        else:
            print(f"  + {meta['source']}: {len(result['headlines'])} headlines")
    except Exception as exc:
        result["error"] = str(exc)
        print(f"  x {meta['source']}: {exc}", file=sys.stderr)
    return result


def main():
    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "regions": {},
    }
    for region, sources in SOURCES.items():
        print(f"\n[{region}]")
        output["regions"][region] = [fetch_outlet(s) for s in sources]

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
    print(f"\nWrote {out_path} — {total} headlines across 16 outlets")


if __name__ == "__main__":
    main()
