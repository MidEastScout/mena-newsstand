#!/usr/bin/env python3
"""
WHAT THIS SCRIPT DOES (plain English)
======================================
This script runs automatically on GitHub every 30 minutes. It visits each of
the 16 news outlets listed in SOURCES below, reads their RSS feed (a standard
machine-readable list of recent articles), picks the 5 most recent headlines,
and saves everything to a file called headlines.json in the root of this repo.
The website then reads that file to show you the headlines — no live fetching
happens in the visitor's browser.

HOW TO ADD OR REMOVE AN OUTLET
================================
Find the SOURCES dictionary below. Each outlet is one block that looks like:
    {
        "source": "Outlet Name",
        "country": "Country",
        "lang": "en",          ← language code: "en", "ar", "he", etc.
        "url": "https://...",  ← the outlet's homepage
        "rss": "https://...",  ← the RSS feed URL (findable via the outlet's site)
    },
Add a new block inside the right region (Gulf / Levant / Israel / Pan-Arab),
or delete an existing block to remove it. The region names must match exactly
what's used in index.html.

HOW THE FALLBACK WORKS
========================
Some outlets block automated requests from GitHub's servers (they return a
"403 Forbidden" error). In that case the script automatically tries Google News
as a backup — it searches Google News for recent articles from that same outlet.
If Google News also has nothing fresh, the script shows the newest article it
found anyway rather than leaving the card blank. You don't need to do anything
special to enable this; it happens automatically.

WHAT "JUNK TITLES" MEANS
=========================
Sometimes Google News returns navigation pages ("Contact Us", "Sports", etc.)
instead of real articles. The JUNK_TITLES list below tells the script to skip
those so they never appear on the site.
"""
import copy
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
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
MAX_AGE_DAYS = 4          # drop entries older than this (kills evergreen junk)

# Titles that are navigation/section/tag pages, not articles. Matched
# case-folded against the article's core title (outlet suffix stripped).
# Google News surfaces these for thinly-covered outlets.
JUNK_TITLES = {
    "contact us", "about us", "home", "homepage", "sports", "sport", "opinion",
    "football", "roundup", "magazine", "business", "world", "news", "videos",
    "video", "photos", "gallery", "archive", "subscribe", "advertise", "weather",
    "e-paper", "epaper", "newsletters", "newsletter", "tag", "tags", "live",
    "live blog", "watch", "author", "authors", "more", "latest", "latest news",
    "breaking news", "podcasts", "podcast",
}

GNEWS_LOCALE = {
    "en": ("en-US", "US", "US:en"),
    "ar": ("ar", "EG", "EG:ar"),
    "he": ("he", "IL", "IL:he"),
    "tr": ("tr", "TR", "TR:tr"),
    "fr": ("fr", "FR", "FR:fr"),
}

LANG_TARGETS = {
    "he": "Hebrew",
    "ar": "Arabic",
    "ru": "Russian",
    "zh": "Mandarin Chinese (Simplified)",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
}

# Gemini model used for translation. gemini-2.5-flash has free-tier quota;
# gemini-2.0-flash showed a 0 free-tier limit on some projects.
GEMINI_MODEL = "gemini-2.5-flash"

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


def parse_dt(entry):
    """Return a tz-aware datetime for the entry, or None if unavailable."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def gnews_url(meta: dict) -> str:
    """Google News RSS search scoped to the outlet's domain, last 24h."""
    hl, gl, ceid = GNEWS_LOCALE.get(meta["lang"], GNEWS_LOCALE["en"])
    query = quote_plus(f"site:{domain_of(meta['url'])} when:1d")
    return f"https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"


def core_title(title: str) -> str:
    """Strip the trailing ' - Outlet' / ' | Outlet' that Google News appends.

    Only strips when the tail looks like a publisher name (short, title-cased),
    so real headlines that merely end in '... - comment' are left intact.
    """
    t = title.strip()
    for sep in (" - ", " | ", " – ", " — "):
        idx = t.rfind(sep)
        if idx <= 0:
            continue
        head, tail = t[:idx].strip(), t[idx + len(sep):].strip()
        words = tail.split()
        if head and 1 <= len(words) <= 4 and all(
            w[:1].isupper() for w in words if w[:1].isalpha()
        ):
            return head
    return t


def is_junk_title(title: str, source: str) -> bool:
    core = core_title(title)
    c = core.casefold()
    if not c:
        return True
    if c in JUNK_TITLES:
        return True
    if c == source.casefold():
        return True
    # Just the outlet name (or name + a couple chars) — a homepage/tagline.
    if source.casefold() in c and len(c) <= len(source) + 3:
        return True
    # Pure issue/section numbers, e.g. Al-Akhbar's "5804".
    if core.replace(" ", "").replace("-", "").isdigit():
        return True
    # Author / tag landing pages: one or two capitalised words, no digits, short
    # — never a real headline (e.g. "Nathaniel Lacsina", "Tricia Gajitos").
    words = core.split()
    if len(words) <= 2 and len(core) < 28 and all(
        w.isalpha() and w[:1].isupper() for w in words
    ):
        return True
    return False


def parse_feed(session: requests.Session, url: str, referer):
    """Return entries as list of dicts sorted newest-first, or None on failure.

    Each dict: {title, url, published(iso str), _dt(datetime or None)}.
    """
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
    items = []
    for e in feed.entries:
        title = (e.get("title") or "").strip()
        link = e.get("link") or e.get("id", "")
        if not title or not link:
            continue
        dt = parse_dt(e)
        items.append({
            "title": title,
            "url": link,
            "published": dt.isoformat() if dt else "",
            "_dt": dt,
        })
    # Newest first; undated entries sink to the bottom.
    floor = datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=lambda x: x["_dt"] or floor, reverse=True)
    return items or None


def fresh_items(items, source: str):
    """Keep only recent, non-junk, dated entries."""
    if not items:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    return [
        it for it in items
        if it["_dt"] and it["_dt"] >= cutoff and not is_junk_title(it["title"], source)
    ]


def strip_internal(items):
    return [{"title": it["title"], "url": it["url"], "published": it["published"]}
            for it in items[:HEADLINES_PER_OUTLET]]


def fetch_outlet(session: requests.Session, meta: dict) -> dict:
    result = {
        "source": meta["source"], "country": meta["country"],
        "lang": meta["lang"], "url": meta["url"],
        "headlines": [], "error": None,
    }
    source = meta["source"]

    # 1) Native feed (own domain as Referer dodges some blocks).
    native = parse_feed(session, meta["rss"], meta["url"] + "/") or []
    items = fresh_items(native, source)
    via = "native"

    # 2) Fall back to Google News only if native has no fresh items.
    gn = []
    if not items:
        gn = parse_feed(session, gnews_url(meta), "https://news.google.com/") or []
        items = fresh_items(gn, source)
        via = "google-news"

    # 3) Last resort: if nothing is "fresh" anywhere, show the newest we have
    #    (still date-sorted, junk removed) rather than an empty card.
    if not items:
        items = [it for it in (native or gn) if not is_junk_title(it["title"], source)]
        via += "/stale"

    if items:
        result["headlines"] = strip_internal(items)
        newest = items[0]["published"][:10] or "undated"
        print(f"  + {source}: {len(result['headlines'])} headlines ({via}, newest {newest})")
    else:
        result["error"] = "no entries"
        print(f"  x {source}: no entries (native + google-news failed)", file=sys.stderr)
    return result


def _titles_hash(regions: dict) -> str:
    """SHA-256 (truncated) of all headline titles — cache key for translations."""
    parts = []
    for outlets in regions.values():
        for outlet in outlets:
            for h in outlet.get("headlines", []):
                parts.append(h["title"])
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def translate_all_languages(regions: dict, existing_output: dict = None) -> dict:
    """Translate all headlines into 7 languages using the Google Gemini API.

    Returns a dict with keys regions_he, regions_ar, regions_ru, regions_zh,
    regions_fr, regions_de, regions_es.  If the set of headline titles hasn't
    changed since the last run, the cached translations are reused so no API
    call is made.  Returns {} on any failure — non-fatal.

    Uses Gemini's free tier (GEMINI_MODEL), so the workload here costs nothing
    in practice.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  GEMINI_API_KEY not set — skipping translations", file=sys.stderr)
        return {}

    positions = []
    titles = []
    for region, outlets in regions.items():
        for o_idx, outlet in enumerate(outlets):
            for h_idx, h in enumerate(outlet.get("headlines", [])):
                positions.append((region, o_idx, h_idx))
                titles.append(h["title"])

    if not titles:
        return {}

    current_hash = _titles_hash(regions)
    if existing_output and existing_output.get("titles_hash") == current_hash:
        cached = {k: v for k, v in existing_output.items() if k.startswith("regions_")}
        if len(cached) >= len(LANG_TARGETS):
            print(f"  Headlines unchanged — reusing translations (hash {current_hash})")
            return cached

    try:
        from google import genai
    except ImportError:
        print("  google-genai package not installed — skipping translations", file=sys.stderr)
        return {}

    client = genai.Client(api_key=api_key)
    titles_json = json.dumps(titles, ensure_ascii=False)
    result = {}

    for lang_code, lang_name in LANG_TARGETS.items():
        prompt = (
            f"Translate each news headline in the JSON array below to {lang_name}. "
            "Keep translations natural and journalistic — match the register of the "
            "original. Do not add explanations or notes.\n\n"
            f"Headlines:\n{titles_json}\n\n"
            f"Return ONLY a JSON array of {len(titles)} translated strings "
            "(no markdown, no code fences, no explanation), in the same order as "
            "the input."
        )
        # Retry transient rate limits (429) with a short backoff; a few outlets
        # per minute is well within free-tier limits, but bunched calls can trip
        # the per-minute cap.
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                )
                raw = (response.text or "").strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                lang_titles = json.loads(raw)
                if not isinstance(lang_titles, list) or len(lang_titles) != len(titles):
                    print(f"  [{lang_code}] Unexpected response shape — skipping", file=sys.stderr)
                    break
                regions_lang = copy.deepcopy(regions)
                for i, (region, o_idx, h_idx) in enumerate(positions):
                    if i < len(lang_titles) and lang_titles[i]:
                        regions_lang[region][o_idx]["headlines"][h_idx]["title"] = lang_titles[i]
                result[f"regions_{lang_code}"] = regions_lang
                print(f"  [{lang_code}] Translated {len(titles)} headlines")
                break
            except Exception as exc:
                msg = str(exc)
                is_rate_limit = "429" in msg or "RESOURCE_EXHAUSTED" in msg
                if is_rate_limit and attempt < 2:
                    wait = 6 * (attempt + 1)
                    print(f"  [{lang_code}] rate-limited — retrying in {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"  [{lang_code}] Translation failed: {exc}", file=sys.stderr)
                break

    return result


def main():
    session = requests.Session()
    output = {"updated": datetime.now(timezone.utc).isoformat(), "regions": {}}
    for region, sources in SOURCES.items():
        print(f"\n[{region}]")
        output["regions"][region] = [fetch_outlet(session, s) for s in sources]

    out_path = Path(__file__).parent.parent / "headlines.json"
    existing = None
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    print("\n[Translations]")
    translations = translate_all_languages(output["regions"], existing)
    output.update(translations)
    if translations:
        output["titles_hash"] = _titles_hash(output["regions"])

    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(o["headlines"]) for outs in output["regions"].values() for o in outs)
    ok = sum(1 for outs in output["regions"].values() for o in outs if o["headlines"])
    print(f"\nWrote {out_path} — {total} headlines, {ok} outlets live")


if __name__ == "__main__":
    main()
