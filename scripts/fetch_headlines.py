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
import html
import json
import os
import re
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
        {
            "source": "Al Manar", "country": "Lebanon", "lang": "ar",
            "url": "https://www.almanar.com.lb",
            "rss": "https://www.almanar.com.lb/rss",
        },
        {
            "source": "Quds News", "country": "Palestine", "lang": "ar",
            "url": "https://qudsn.net",
            "rss": "https://qudsn.net/feed",
        },
        {
            "source": "Falastin al-Youm", "country": "Palestine", "lang": "ar",
            "url": "https://paltoday.ps",
            "rss": "https://paltoday.ps/feed",
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
        {
            "source": "Al Mayadeen", "country": "Lebanon", "lang": "ar",
            "url": "https://www.almayadeen.net",
            "rss": "https://www.almayadeen.net/rss.xml",
        },
    ],
    "Iran": [
        {
            "source": "Fars News", "country": "Iran", "lang": "en",
            "url": "https://en.farsnews.ir",
            "rss": "https://en.farsnews.ir/rss.aspx",
        },
        {
            "source": "Tasnim News", "country": "Iran", "lang": "en",
            "url": "https://en.tasnimnews.com",
            "rss": "https://en.tasnimnews.com/en/rss.aspx",
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

# Order regions appear in the Headlines tab (the site renders them in the order
# they're written to headlines.json).
REGION_ORDER = ["Pan-Arab", "Levant", "Gulf", "Israel", "Iran"]

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

# Gemini model used for snippets + translation. flash-lite has a much higher
# free-tier daily request limit than gemini-2.5-flash (which capped at ~20/day
# on this project) — important because each run makes ~8 calls.
GEMINI_MODEL = "gemini-2.5-flash-lite"

# Target length of the per-headline summary, in words.
SNIPPET_WORDS = 50

# Bump this whenever the snippet/translation prompt changes so the content cache
# is invalidated and everything regenerates on the next run.
TRANSLATION_VERSION = "v3-50w"

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


def clean_description(raw: str, title: str) -> str:
    """Turn an RSS summary/description into clean plain text, or '' when it has
    no real content beyond the title.

    Google News RSS stubs are essentially "<a>title</a> <font>source</font>" —
    once the title is stripped, nothing useful remains, so we return '' and the
    snippet step will skip that headline (rather than inventing a summary).
    """
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)      # drop HTML tags
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    leftover = text.replace(title, "").strip()
    if len(leftover) < 40:                    # basically just the title + source
        return ""
    return text[:600]                         # cap what we feed the model


def parse_feed(session: requests.Session, url: str, referer):
    """Return entries as list of dicts sorted newest-first, or None on failure.

    Each dict: {title, url, published(iso str), description(str), _dt(datetime)}.
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
        raw_desc = e.get("summary") or e.get("description") or ""
        items.append({
            "title": title,
            "url": link,
            "published": dt.isoformat() if dt else "",
            "description": clean_description(raw_desc, title),
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
    # "description" is kept temporarily for snippet generation, then removed
    # before the file is written (see translate_all_languages).
    return [{"title": it["title"], "url": it["url"], "published": it["published"],
             "description": it.get("description", "")}
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
    """SHA-256 (truncated) of all titles + descriptions — the cache key for
    snippets and translations. Including descriptions means a snippet refreshes
    if its source text changes, even when the title is identical."""
    parts = [TRANSLATION_VERSION]
    for outlets in regions.values():
        for outlet in outlets:
            for h in outlet.get("headlines", []):
                parts.append(h.get("title", ""))
                parts.append(h.get("description", ""))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _strip_descriptions(regions: dict):
    """Remove the temporary raw 'description' field before the file is written."""
    for outlets in regions.values():
        for outlet in outlets:
            for h in outlet.get("headlines", []):
                h.pop("description", None)


def translate_all_languages(regions: dict, existing_output: dict = None) -> dict:
    """Generate a short English snippet for each headline and translate both the
    title and the snippet into 7 languages, using the Google Gemini API.

    Mutates `regions` (English) in place: adds a "snippet" field to each headline
    and removes the temporary "description" field.

    Returns a dict with keys regions_he/ar/ru/zh/fr/de/es (each a full regions
    copy with translated titles + snippets) plus "titles_hash". If the content
    hasn't changed since the last run, snippets and translations are reused with
    no API calls. Returns {} on failure — non-fatal.

    Uses Gemini's free tier (GEMINI_MODEL), so the workload here costs nothing
    in practice.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  GEMINI_API_KEY not set — skipping snippets/translations", file=sys.stderr)
        _strip_descriptions(regions)
        return {}

    positions, titles, descriptions = [], [], []
    for region, outlets in regions.items():
        for o_idx, outlet in enumerate(outlets):
            for h_idx, h in enumerate(outlet.get("headlines", [])):
                positions.append((region, o_idx, h_idx))
                titles.append(h["title"])
                descriptions.append(h.get("description", ""))

    if not titles:
        _strip_descriptions(regions)
        return {}

    current_hash = _titles_hash(regions)

    # Cache hit: reuse last run's snippets (into the fresh English regions) and
    # its translated copies, with no API calls.
    if existing_output and existing_output.get("titles_hash") == current_hash:
        cached = {k: v for k, v in existing_output.items() if k.startswith("regions_")}
        prev = existing_output.get("regions")
        if len(cached) >= len(LANG_TARGETS) and prev:
            for (region, o_idx, h_idx) in positions:
                try:
                    snip = prev[region][o_idx]["headlines"][h_idx].get("snippet", "")
                except Exception:
                    snip = ""
                regions[region][o_idx]["headlines"][h_idx]["snippet"] = snip
            _strip_descriptions(regions)
            cached["titles_hash"] = current_hash
            print(f"  Content unchanged — reusing snippets + translations (hash {current_hash})")
            return cached

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("  google-genai package not installed — skipping", file=sys.stderr)
        _strip_descriptions(regions)
        return {}

    client = genai.Client(api_key=api_key)
    # Structured output: force a valid JSON array of strings every time, so a
    # stray quote in an RTL headline can't break the whole response.
    str_list_config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=list[str],
    )

    def call_model(prompt: str, expected_len: int):
        """Call Gemini, returning a list[str] of expected_len, or None.
        Retries both transient 429s and wrong-length responses."""
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=GEMINI_MODEL, contents=prompt, config=str_list_config,
                )
                arr = json.loads((resp.text or "").strip())
                if isinstance(arr, list) and len(arr) == expected_len:
                    return arr
                got = len(arr) if isinstance(arr, list) else "?"
                print(f"    unexpected response shape ({got} vs {expected_len})", file=sys.stderr)
                if attempt < 2:
                    time.sleep(2)
                    continue
                return None
            except Exception as exc:
                msg = str(exc)
                if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) and attempt < 2:
                    wait = 6 * (attempt + 1)
                    print(f"    rate-limited — retrying in {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"    model call failed: {exc}", file=sys.stderr)
                return None
        return None

    # ---- Step 1: short English snippets, strictly from the description ----
    snippet_items = [{"title": t, "description": d} for t, d in zip(titles, descriptions)]
    snip_prompt = (
        f"For each news item in the JSON array below, write an informative summary "
        f"of about {SNIPPET_WORDS} words (two to three sentences), in English, that "
        "captures ALL the key facts in its 'description' — keep specific details "
        "like numbers, names, places and quotes. Use ONLY information present in the "
        "'description'; never add anything that isn't there, and do not pad. If the "
        "'description' is empty or only restates the 'title', return an empty string "
        "for that item.\n\n"
        f"Items:\n{json.dumps(snippet_items, ensure_ascii=False)}\n\n"
        f"Return ONLY a JSON array of exactly {len(titles)} strings, same order."
    )
    snip_result = call_model(snip_prompt, len(titles))
    snippets_ok = snip_result is not None
    en_snippets = snip_result or [""] * len(titles)

    # If snippet generation failed, reuse last run's snippets (matched by title)
    # so the site doesn't go blank while we wait for the next run to retry.
    if not snippets_ok and existing_output:
        prev_snip = {}
        for outs in existing_output.get("regions", {}).values():
            for o in outs:
                for h in o.get("headlines", []):
                    if h.get("snippet"):
                        prev_snip[h.get("title", "")] = h["snippet"]
        if prev_snip:
            en_snippets = [prev_snip.get(t, "") for t in titles]
            print(f"  snippet refresh failed — reused {sum(1 for s in en_snippets if s)} prior snippets", file=sys.stderr)

    n_snips = sum(1 for s in en_snippets if s)
    for i, (region, o_idx, h_idx) in enumerate(positions):
        regions[region][o_idx]["headlines"][h_idx]["snippet"] = en_snippets[i]
    _strip_descriptions(regions)
    print(f"  Generated {n_snips}/{len(titles)} English snippets")

    # ---- Step 2: translate title + snippet into each language ----
    # One structured call per language over a flat [title, snippet, title, ...]
    # list keeps us at 7 calls total.
    flat = []
    for t, s in zip(titles, en_snippets):
        flat.append(t)
        flat.append(s)

    result = {}
    fresh = 0
    for lang_code, lang_name in LANG_TARGETS.items():
        prompt = (
            f"Translate each string in the JSON array below to {lang_name}. The "
            "array alternates between a headline and its summary. Translate "
            "naturally and journalistically. Keep empty strings as empty strings. "
            "Do not add notes.\n\n"
            f"Strings:\n{json.dumps(flat, ensure_ascii=False)}\n\n"
            f"Return ONLY a JSON array of exactly {len(flat)} strings, same order."
        )
        translated = call_model(prompt, len(flat))
        if not translated:
            continue
        regions_lang = copy.deepcopy(regions)  # already has snippet, no description
        for i, (region, o_idx, h_idx) in enumerate(positions):
            hl = regions_lang[region][o_idx]["headlines"][h_idx]
            t_title, t_snip = translated[2 * i], translated[2 * i + 1]
            if t_title:
                hl["title"] = t_title
            hl["snippet"] = t_snip or ""
        result[f"regions_{lang_code}"] = regions_lang
        fresh += 1
        print(f"  [{lang_code}] Translated {len(titles)} headlines + snippets")

    # Non-destructive: if a language failed this run (e.g. quota), keep the
    # previous run's copy so its chip never disappears from the site.
    if existing_output:
        for lang_code in LANG_TARGETS:
            key = f"regions_{lang_code}"
            if key not in result and key in existing_output:
                result[key] = existing_output[key]
                print(f"  [{lang_code}] kept previous translation (refresh failed)", file=sys.stderr)

    # Only stamp the content hash when snippets were freshly generated AND every
    # language was refreshed this run, so a partial run never gets cached (which
    # would otherwise freeze empty snippets in place until headlines change).
    if snippets_ok and fresh == len(LANG_TARGETS):
        result["titles_hash"] = current_hash
    return result


def main():
    session = requests.Session()
    output = {"updated": datetime.now(timezone.utc).isoformat(), "regions": {}}
    for region in REGION_ORDER:
        print(f"\n[{region}]")
        output["regions"][region] = [fetch_outlet(session, s) for s in SOURCES[region]]

    out_path = Path(__file__).parent.parent / "headlines.json"
    existing = None
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    print("\n[Snippets + translations]")
    # Adds English snippets to output["regions"], returns translated copies
    # (regions_he/ar/...) plus "titles_hash", and strips raw descriptions.
    translations = translate_all_languages(output["regions"], existing)
    output.update(translations)

    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(o["headlines"]) for outs in output["regions"].values() for o in outs)
    ok = sum(1 for outs in output["regions"].values() for o in outs if o["headlines"])
    print(f"\nWrote {out_path} — {total} headlines, {ok} outlets live")


if __name__ == "__main__":
    main()
