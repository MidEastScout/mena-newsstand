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
            "source": "WAFA News", "country": "Palestine", "lang": "en",
            "url": "https://english.wafa.ps",
            "rss": "https://english.wafa.ps/rss.xml",
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
            # IRNA — Iran's official state wire service (English edition).
            # History: Fars News (en) only ever returned its Farsi homepage from
            # a datacenter IP; Tehran Times replaced it but is too thinly indexed
            # — its native feed yielded nothing and Google News returned almost
            # nothing but the e-paper, so its card was a single "pdf" stub.
            # IRNA is a high-volume agency that is very well indexed, so the
            # Google News fallback reliably surfaces real articles even when the
            # native feed is blocked from a datacenter IP.
            #
            # Sports are dropped at the TITLE level (is_offtopic), never via a
            # Google-News body exclusion (which throws away political stories
            # that merely mention a sport) — so no "gn_exclude" here.
            "source": "IRNA", "country": "Iran", "lang": "en",
            "url": "https://en.irna.ir",
            "rss": "https://en.irna.ir/rss",
        },
        {
            "source": "Mehr News", "country": "Iran", "lang": "en",
            "url": "https://en.mehrnews.com",
            "rss": "https://en.mehrnews.com/rss",
        },
    ],
}

HEADLINES_PER_OUTLET = 5
# A much broader per-outlet sample of the outlet's actual recent coverage,
# written to coverage.json for the Trends view. The Headlines tab still shows
# only HEADLINES_PER_OUTLET per outlet; Trends, however, should reflect WHAT
# EACH OUTLET IS ACTUALLY COVERING, not just the handful of cards on the wall —
# so it is computed from up to this many fresh, on-topic items per outlet.
COVERAGE_PER_OUTLET = 40
REQUEST_TIMEOUT = 20
ARTICLE_TIMEOUT = 10      # per-article fetch when enriching a missing description
MAX_AGE_DAYS = 4          # drop entries older than this (kills evergreen junk)
# Google News search window for the fallback. Kept >= MAX_AGE_DAYS so the search
# is never the bottleneck: a thinly-indexed outlet (e.g. Tehran Times) returns
# almost nothing for "when:1d", which starved its card down to a single item.
# fresh_items() still enforces MAX_AGE_DAYS, so widening the query only adds
# candidates for sparse outlets and is neutral for busy ones (newest-first, capped).
GNEWS_WINDOW_DAYS = 7

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
    # Arabic section/navigation labels that arrive as if they were articles.
    "صحة وطب", "صحة", "رياضة", "فن", "فنون", "منوعات", "تكنولوجيا", "سيارات",
    "ثقافة", "مقالات", "الرئيسية", "فيديو", "صور",
    "اقتصاد", "اقتصادية", "أخبار", "سياسة", "دولي", "دوليات", "العالم",
    "محليات", "آراء", "رأي", "الأخبار", "عاجل",
}

# ---------------------------------------------------------------------------
# Relevance filter — this is a MENA geopolitics / security / economy / diplomacy
# desk, NOT a general aggregator. Sports, entertainment, lifestyle and generic
# consumer-tech leak in from outlets' broader feeds and must be dropped, even
# from a MENA-based outlet. Two HIGH-PRECISION signals are used so real news is
# never removed:
#   1. the article URL's section path (e.g. /sport/, /entertainment/, /lifestyle/)
#   2. a whole-word topic term in the headline
# Deliberately avoids ambiguous words that ARE geopolitical: strike, launch,
# race (arms race), league (Arab League), cup/world (alone), actor (state actor),
# tour (diplomatic tour), film (filmed), drone, missile, etc.
OFFTOPIC_PATHS = {
    "sport", "sports", "football", "soccer", "tennis", "cricket", "golf",
    "rugby", "basketball", "motorsport", "formula1", "entertainment", "showbiz",
    "celebrity", "celebrities", "movies", "music", "lifestyle", "fashion",
    "beauty", "recipes", "cooking", "travel", "gaming", "gadgets", "auto",
    "autos", "cars", "horoscope", "horoscopes",
}

OFFTOPIC_TERMS = [
    # — sports —
    "football", "footballer", "soccer", "goalkeeper", "midfielder", "bundesliga",
    "la liga", "laliga", "premier league", "champions league", "europa league",
    "world cup", "asian cup", "gulf cup", "afcon", "uefa", "fifa", "olympics",
    "olympic games", "wimbledon", "cricket", "formula one", "grand prix",
    "motogp", "rugby", "nba", "nfl", "ballon d'or", "messi", "ronaldo", "mbappe",
    "neymar", "benzema", "haaland", "transfer window", "penalty shootout",
    "hat-trick", "hat trick", "top scorer", "clean sheet", "man of the match",
    "quarter-final", "semi-final", "quarterfinal", "semifinal",
    # Unambiguous sport names (no geopolitical sense). These previously lived in
    # Tehran Times' Google-News body exclusion; moved here so they're dropped at
    # the headline level for every outlet without starving any feed.
    "volleyball", "basketball", "handball", "futsal", "taekwondo", "weightlifting",
    "wrestling", "gymnastics", "marathon", "asian games", "judo", "karate",
    "athletics meet", "friendly match",
    # — arts / culture venues & ensembles —
    "orchestra", "symphony", "concerto",
    # — entertainment / celebrity —
    "hollywood", "bollywood", "box office", "box-office", "oscars",
    "academy award", "grammy", "golden globe", "film festival", "red carpet",
    "celebrity", "celebrities", "actress", "movie", "movies", "music video",
    "studio album", "rapper", "kardashian", "taylor swift", "beyonce", "netflix",
    "met gala", "reality show", "reality tv", "opera", "ballet", "playwright",
    "philharmonic", "art exhibition", "biennale",
    # — lifestyle / consumer tech —
    "robotaxi", "self-driving", "smartphone", "iphone", "ipad", "playstation",
    "xbox", "nintendo", "smartwatch", "earbuds", "video game", "app store",
    "horoscope", "zodiac", "astrology", "skincare", "makeup", "weight loss",
    "fashion week", "gadget", "gadgets",
    # — lifestyle / service / explainer leakage —
    "air conditioning", "air conditioner", "air conditioners", "how to apply",
    "step-by-step guide", "ultimate guide", "best places to", "things to do in",
    "top 10", "top 5", "sudoku", "crossword", "word search",
]
OFFTOPIC_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in OFFTOPIC_TERMS) + r")\b", re.I)

# Sponsored / advertising entries that RSS feeds mix in as if they were articles.
# Only clear ad markers — NOT a bare "sponsored"/"partnership", which appear in
# real headlines ("Sponsored by years of conflict…", "in partnership with the US").
AD_TITLE_RE = re.compile(
    r"\bsponsored\s+(content|post|article|feature|story|listing)\b"
    r"|\b(advertorial|advertisement)\b"
    r"|\bpaid\s+(content|post|partnership|promotion)\b"
    r"|\b(partnered|branded|promoted)\s+(content|post|story)\b"
    r"|\[\s*(ad|sponsored|promoted|advertisement)\s*\]"
    r"|\(\s*(ad|sponsored|advertisement)\s*\)"
    r"|(?:^|[-|–—:]\s*)sponsored\s*$",          # a trailing "… - Sponsored" tag
    re.I)
# Known ad-network / affiliate redirect hosts that show up in entry links.
AD_DOMAINS = {
    "doubleclick.net", "googleadservices.com", "googlesyndication.com",
    "outbrain.com", "taboola.com", "adnxs.com", "go.skimresources.com",
    "skimresources.com", "awin1.com", "shareasale.com", "linksynergy.com",
    "prf.hn", "anrdoezrs.net", "dpbolvw.net", "jdoqocy.com", "smartadserver.com",
}

# E-paper / PDF / print-edition cards (e.g. "Gulf Times ePaper-June 24, 2026",
# "tehrantimes pdf") — the daily paper download, never an article.
EPAPER_RE = re.compile(
    r"(?i)\b(e-?paper|e-?edition|epaper|print\s+edition|paper\s+edition|"
    r"today'?s\s+paper|digital\s+edition)\b")
# Google News tag / search / archive landing pages surfaced as if articles:
#   "Tag Results for "IWRE" (1 articles)",  "… (12 articles)"
TAG_PAGE_RE = re.compile(
    r"(?i)^(tag|search|topic|category)\s+results?\b"
    r"|\(\s*\d+\s+articles?\s*\)\s*$")

# Arabic-language sports / lifestyle that the English term list can't catch.
# Matched as substrings (Arabic prefixes attach to words), so ONLY unambiguous
# multi-letter terms are listed — deliberately avoiding ones that hide inside
# common geopolitical words: e.g. "هداف" (scorer) ⊂ "استهداف" (targeting),
# "الدوري" ⊂ "الدورية" (patrol), "منتخب" also means "elected", "أبراج"=towers.
OFFTOPIC_AR = [
    "كأس العالم", "المونديال", "كرة القدم", "كرة قدم", "دوري أبطال",
    "ميسي", "رونالدو", "نيمار", "مبابي",                            # sports — players
    # Club names: each is unique and never appears inside a geopolitical word,
    # so they're safe as substrings (Arabic prefixes attach to the front).
    "برشلونة", "ريال مدريد", "ليفربول", "تشيلسي", "مانشستر",
    "يوفنتوس", "يويفا", "الميركاتو", "هاتريك",                       # sports — clubs/terms
    "وصفات", "العناية بالبشرة", "مكياج", "تسريحة",                   # lifestyle
]
OFFTOPIC_AR_RE = re.compile("|".join(re.escape(t) for t in OFFTOPIC_AR))

# Football / match-report scorelines, e.g. "… holding England to 0-0 draw",
# "won 3-1", "goalless draw". A digit-digit pairing next to a result word never
# occurs in geopolitics, so this is high-precision.
SPORT_SCORE_RE = re.compile(
    r"(?i)\b\d{1,2}\s*[-–—]\s*\d{1,2}\s+(draw|win|won|loss|defeat|victory|aggregate)\b"
    r"|\bgoalless\s+draw\b|\bnil[-\s]nil\b|\bfull[-\s]time\b")

# Weather-record FILLER (heat/cold records). Deliberately tight: it matches the
# "record/grips/sweeps" phrasing of a weather story, NOT a geopolitical line that
# merely contains "heatwave" (e.g. "Heatwave strains Iraq's power grid" is kept).
WEATHER_FILLER_RE = re.compile(
    r"(?i)\brecord(?:s|ed)?\s+(?:the\s+)?(?:hottest|coldest|warmest)\b"
    r"|\b(?:hottest|coldest|warmest)\s+\w+\s+(?:ever|on\s+record)\b"
    r"|\bheat\s?wave\s+(?:grips|sweeps|hits|engulfs|bakes|scorches|blankets)\b"
    r"|\bcold\s+snap\b")

# Order regions appear in the Headlines tab (the site renders them in the order
# they're written to headlines.json).
REGION_ORDER = ["Pan-Arab", "Levant", "Gulf", "Israel", "Iran"]

GNEWS_LOCALE = {
    "en": ("en-US", "US", "US:en"),
    "ar": ("ar", "EG", "EG:ar"),
    "he": ("he", "IL", "IL:he"),
    "fa": ("fa", "IR", "IR:fa"),
    "tr": ("tr", "TR", "TR:tr"),
    "fr": ("fr", "FR", "FR:fr"),
}

# Gemini model used for the English snippets. flash-lite has a much higher
# free-tier daily request limit than gemini-2.5-flash. Snippet generation is a
# single batched call per run (only when headlines actually changed).
GEMINI_MODEL = "gemini-2.5-flash-lite"

# Target length of the per-headline summary, in words.
SNIPPET_WORDS = 50

# Bump this whenever the snippet prompt changes so the content cache is
# invalidated and snippets regenerate on the next run.
SNIPPET_VERSION = "v7-en"

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


# Separators Google News (and outlets) use to append a source/publisher label.
TITLE_SEPS = (" - ", " | ", " – ", " — ")
# A bare domain token, e.g. "almayadeen.net" or "paltoday.ps".
DOMAIN_RE = re.compile(r'^[a-z0-9-]+(\.[a-z0-9-]+)+$', re.I)


def clean_title(title: str, source: str, domain: str) -> str:
    """Strip the publisher label Google News bolts onto a headline.

    Google News rewrites titles as "<Outlet> | Real headline - <Outlet>" or
    "Real headline - outlet-domain.tld". We strip a trailing source/domain suffix
    and a leading source prefix so the displayed headline is the actual headline
    (and dedupes correctly). Only strips when the affix matches THIS outlet (its
    name or domain) or is a bare domain — so real headlines are never touched.
    """
    if not title:
        return title
    t = title.strip()
    src_cf = source.casefold()
    src_compact = src_cf.replace(" ", "")
    dom = domain.casefold()
    # Trailing "- <source>" / "- <domain>" (may repeat, e.g. "... - X - X").
    for _ in range(3):
        changed = False
        for sep in TITLE_SEPS:
            idx = t.rfind(sep)
            if idx <= 0:
                continue
            tail = t[idx + len(sep):].strip()
            tcf = tail.casefold()
            if (tcf == dom or DOMAIN_RE.match(tail) or tcf == src_cf
                    or src_cf in tcf or tcf.replace(" ", "") == src_compact):
                t = t[:idx].strip()
                changed = True
                break
        if not changed:
            break
    # Leading "<source> | " prefix (e.g. "Farsnews | Real headline"). Use a
    # prefix match (not substring) so we only strip an actual outlet label.
    for sep in (" | ", " - "):
        idx = t.find(sep)
        if 0 < idx <= len(source) + 4:
            head = t[:idx].strip().casefold().replace(" ", "")
            if head and src_compact.startswith(head):
                t = t[idx + len(sep):].strip()
                break
    return t or title


def gnews_url(meta: dict) -> str:
    """Google News RSS search scoped to the outlet's domain, last 24h. An outlet
    may set 'gn_exclude' to drop whole sections (sports, culture…) that its
    full-site feed would otherwise surface — applied as Google News '-term'
    exclusions, which match the article body, not just the headline."""
    hl, gl, ceid = GNEWS_LOCALE.get(meta["lang"], GNEWS_LOCALE["en"])
    q = f"site:{domain_of(meta['url'])} when:{GNEWS_WINDOW_DAYS}d"
    for term in meta.get("gn_exclude", []):
        q += f" -{term}"
    query = quote_plus(q)
    return f"https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"


def core_title(title: str) -> str:
    """Strip the trailing ' - Outlet' / ' | Outlet' that Google News appends.

    Only strips when the tail looks like a publisher name (short, title-cased),
    so real headlines that merely end in '... - comment' are left intact.
    """
    t = title.strip()
    for sep in TITLE_SEPS:
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


def _is_namecard(core: str) -> bool:
    """A 'Name - Name' / 'Tagline | Outlet' homepage card, where one side is
    contained in the other (e.g. 'فارس - خبرگزاری فارس', 'Outlet - Outlet News').
    Never a real headline."""
    for sep in TITLE_SEPS:
        if sep in core:
            parts = [p.strip() for p in core.split(sep) if p.strip()]
            if len(parts) >= 2:
                a, b = parts[0], parts[-1]
                if (a in b or b in a) and max(len(a), len(b)) <= 40:
                    return True
    return False


def _is_allcaps_topic(core: str) -> bool:
    """An all-caps section/topic label like 'US-ISRAEL-IRAN WAR' — a Google News
    topic page, not an article. Requires at least one Latin letter so non-Latin
    scripts (no upper/lower case) are never matched."""
    if not any(c.isascii() and c.isalpha() for c in core):
        return False
    if len(core) > 40:                 # don't risk a long all-caps real headline
        return False
    words = core.split()
    return 2 <= len(words) <= 6 and all(
        (not c.isalpha()) or c.isupper() for c in core
    )


# Category words that make up Google News' section "landing page" labels. When
# an outlet's own feed is down, the site:-scoped Google News search sometimes
# returns these navigation pages (e.g. "Latest Business News", "Transportation
# and Aviation News") instead of articles. We treat a Title-Cased string made up
# ENTIRELY of these words (+ connectors) as junk — a real headline always carries
# ordinary lower-case words, so it can never match.
SECTION_WORDS = {
    "news", "headlines", "updates", "update", "coverage", "live", "latest",
    "breaking", "top", "more", "trending", "featured", "features", "feature",
    "business", "economy", "economic", "finance", "financial", "markets",
    "market", "money", "trade", "science", "technology", "tech", "innovation",
    "digital", "transportation", "transport", "aviation", "travel", "tourism",
    "sports", "sport", "football", "world", "politics", "political", "policy",
    "opinion", "opinions", "editorial", "lifestyle", "entertainment", "showbiz",
    "culture", "arts", "art", "health", "education", "environment", "climate",
    "energy", "oil", "gas", "defense", "defence", "security", "military",
    "national", "international", "regional", "local", "analysis", "videos",
    "video", "photos", "photo", "gallery", "weather", "automotive", "auto",
    "cars", "motoring", "realestate", "property", "sections", "section",
}
SECTION_CONNECTORS = {"and", "&", "of", "the", "in", "for", "your", "all", "a", "on"}


def _is_section_label(core: str) -> bool:
    """A Title-Cased category/landing-page label (e.g. 'Latest Business News',
    'Transportation and Aviation News'): every significant word is a section
    name and is capitalised, so it is a navigation page, never an article."""
    if not any(c.isascii() and c.isalpha() for c in core):
        return False                       # non-Latin scripts handled elsewhere
    words = re.findall(r"[A-Za-z&]+", core)
    if not (2 <= len(words) <= 6):
        return False
    sig = [w for w in words if w.lower() not in SECTION_CONNECTORS]
    if len(sig) < 2:                       # need >=2 real category words
        return False
    # Title-Cased AND every significant word is a known section term.
    if not all(w[:1].isupper() for w in sig):
        return False
    return all(w.lower() in SECTION_WORDS for w in sig)


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
    # "- domain.tld" artifacts from Google News when the outlet isn't indexed.
    if re.match(r'^-\s+\S+\.\S+\s*$', core):
        return True
    # Social-media reposts: any title referencing a @handle in parentheses.
    # Covers both auto-generated (@user1234567890) and custom (@m0hamm6d) handles
    # that Google News surfaces as Twitter/X aggregation cards.
    if re.search(r'\(@\w', core):
        return True
    # Titles that are purely punctuation/whitespace with no word characters (e.g. ".").
    if not re.search(r'\w', core):
        return True
    # Homepage 'Name - Name' cards and all-caps topic/section pages that Google
    # News surfaces when an outlet has no fresh article in the search window.
    if _is_namecard(core):
        return True
    if _is_allcaps_topic(core):
        return True
    # E-paper / PDF / print-edition download cards (the daily paper, not news).
    if EPAPER_RE.search(core):
        return True
    # A bare "<outlet> pdf" / "Daily PDF" card — only when the title is just a
    # word or two, so a real headline that mentions a PDF is never dropped.
    if re.search(r"\bpdf\b", c) and len(core.split()) <= 3:
        return True
    # Tag / search / archive landing pages ("Tag Results for …", "… (3 articles)").
    if TAG_PAGE_RE.search(core):
        return True
    # Title-Cased section labels ("Latest Business News", "Transportation and
    # Aviation News") that Google News returns when an outlet's feed is down.
    if _is_section_label(core):
        return True
    return False


def is_offtopic(title: str, url: str = "") -> bool:
    """True for sports / entertainment / lifestyle / consumer-tech items that
    aren't MENA geopolitics, security, economics or diplomacy. High-precision:
    a story is dropped only if its URL sits under an off-topic section or its
    headline contains a whole-word off-topic term — so real news is never lost."""
    if url:
        path = urlparse(url).path.lower()
        for seg in path.split("/"):
            if not seg:
                continue
            if seg in OFFTOPIC_PATHS:
                return True
            head = re.split(r"[-_.]", seg, 1)[0]      # e.g. "sport-news" -> "sport"
            if head in OFFTOPIC_PATHS:
                return True
        dom = domain_of(url)                          # ad-network / affiliate link
        if dom in AD_DOMAINS or any(dom.endswith("." + a) for a in AD_DOMAINS):
            return True
    if title:
        # Sponsored / advertising content masquerading as a headline.
        if AD_TITLE_RE.search(title):
            return True
        # Arabic-language sports / lifestyle the English term list misses.
        if OFFTOPIC_AR_RE.search(title):
            return True
        # Football/match scorelines and weather-record filler.
        if SPORT_SCORE_RE.search(title):
            return True
        if WEATHER_FILLER_RE.search(title):
            return True
        # Advice / service Q&A columns: "Ask Gulf News: …", "Ask Khaleej Times: …"
        if re.match(r"(?i)^ask\s+[\w.'’ -]{2,24}:", title.strip()):
            return True
        # Arts-venue listings ("Iranshahr Theater to host …", "National Theatre
        # stages …") — but NOT the military "theater of war / operations".
        if re.search(r"(?i)theat(?:er|re)\s+"
                     r"(?:to\s+host|hosts|to\s+stage|stages|festival|production|"
                     r"premieres?|presents)", title):
            return True
        if OFFTOPIC_RE.search(title):
            return True
    return False


# Current office-holders a model may wrongly tag as "former" from stale training
# data — their office title is ALWAYS stripped, regardless of source.
CURRENT_LEADERS = {"trump"}

# "former / ex / current … president | prime minister | premier " before a name.
# Used to delete a stale or invented office title the AI added that the source
# never stated (e.g. "former US president Trump" -> "Trump").
LEADER_TITLE_RE = re.compile(
    r"\b(former|ex|current|outgoing|incoming|sitting)[-\s]+"
    r"(?:(?:us|u\.s\.|american|israeli|iranian|lebanese|egyptian|turkish|saudi|"
    r"french|british|german|russian|ukrainian|palestinian|syrian|iraqi|qatari|"
    r"emirati|jordanian|yemeni|gulf)\s+)?"
    r"(?:president|prime\s+minister|pm|premier)\s+",
    re.I)


def scrub_stale_titles(text: str, source: str = "") -> str:
    """Remove a leader's office title (e.g. 'former US president') from AI-written
    text UNLESS the source material actually used that 'former/current/…' wording —
    so the site never asserts a stale or invented office. Always strips the title
    before a known current office-holder (e.g. Trump). Best-effort and safe: it
    only ever drops an honorific, never a name or a real fact."""
    if not text:
        return text
    src = (source or "").lower()

    def repl(m):
        marker = m.group(1).lower()
        after = m.string[m.end():m.end() + 24].lower()
        if any(after.startswith(n) for n in CURRENT_LEADERS):
            return ""
        return m.group(0) if marker in src else ""

    # Collapse only runs of spaces/tabs left by a removal — NEVER newlines, or
    # the briefing's paragraph breaks would be lost (it splits on blank lines).
    return re.sub(r"[^\S\n]{2,}", " ", LEADER_TITLE_RE.sub(repl, text)).strip()


def fallback_snippet(description: str) -> str:
    """A deterministic ~2-sentence snippet taken straight from the feed's own
    description — no API. Used when the AI snippet is unavailable (e.g. the daily
    Gemini quota is exhausted) so a headline that has real source text still
    expands instead of going blank. Returns '' when there's nothing usable."""
    d = re.sub(r"\s+", " ", (description or "")).strip()
    if len(d) < 40:
        return ""
    out = ""
    for s in re.split(r"(?<=[.!?])\s+", d):
        if not out:
            out = s
        elif len(out) + len(s) + 1 <= 240:
            out += " " + s
        else:
            break
    return out[:300].strip()


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


def _meta_content(html_text: str, key_attr: str, key_val: str) -> str:
    """Extract the content of a <meta> tag identified by key_attr=key_val,
    tolerant of attribute order (content may come before or after the key)."""
    for m in re.finditer(r"<meta\b[^>]*>", html_text, re.I):
        tag = m.group(0)
        if re.search(rf'{key_attr}\s*=\s*["\']{re.escape(key_val)}["\']', tag, re.I):
            cm = re.search(r'content\s*=\s*["\'](.*?)["\']', tag, re.I | re.S)
            if cm:
                return html.unescape(cm.group(1)).strip()
    return ""


def fetch_meta_description(session: requests.Session, url: str) -> str:
    """Fetch an article page and return its og:description / twitter:description /
    meta description — a real one- or two-sentence summary the outlet wrote.

    Used only when the RSS feed gave us nothing to summarise, so every headline
    can get a snippet instead of only the ones whose feed happens to carry a
    description. Best-effort: any failure (block, timeout, no meta) returns ''.
    """
    try:
        r = session.get(url, headers=HEADERS, timeout=ARTICLE_TIMEOUT)
    except Exception:
        return ""
    if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
        return ""
    text = r.text[:200000]   # the <head> is near the top; cap to stay fast
    for key_attr, key_val in (
        ("property", "og:description"),
        ("name", "twitter:description"),
        ("name", "description"),
    ):
        desc = _meta_content(text, key_attr, key_val)
        desc = re.sub(r"\s+", " ", desc).strip()
        if len(desc) >= 40:
            return desc[:600]
    return ""


def enrich_missing_descriptions(session: requests.Session, headlines: list) -> int:
    """For each kept headline lacking a description, fetch the article's meta
    description so the snippet step has real source text. Google News redirect
    links can't be enriched (they point at news.google.com, not the article), so
    they're skipped. Returns how many descriptions were filled in."""
    filled = 0
    for h in headlines:
        if h.get("description"):
            continue
        url = h.get("url", "")
        if not url or "news.google.com" in url:
            continue
        desc = fetch_meta_description(session, url)
        if desc:
            h["description"] = desc
            filled += 1
    return filled


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


def _clean_titles(items, source: str, domain: str):
    """Strip Google News publisher affixes from every entry's title in place."""
    for it in items:
        it["title"] = clean_title(it["title"], source, domain)


def fresh_items(items, source: str):
    """Keep only recent, non-junk, dated entries."""
    if not items:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    return [
        it for it in items
        if it["_dt"] and it["_dt"] >= cutoff
        and not is_junk_title(it["title"], source)
        and not is_offtopic(it["title"], it.get("url", ""))
    ]


def strip_internal(items):
    # "description" is kept temporarily for snippet generation, then removed
    # before the file is written (see generate_snippets).
    return [{"title": it["title"], "url": it["url"], "published": it["published"],
             "description": it.get("description", "")}
            for it in items[:HEADLINES_PER_OUTLET]]


def coverage_items(items):
    """A lightweight, broader slice of the outlet's fresh coverage for trends —
    titles only (no descriptions/snippets, no article fetches). Already filtered
    for freshness, junk and off-topic by fresh_items()."""
    return [{"title": it["title"], "url": it["url"], "published": it["published"]}
            for it in items[:COVERAGE_PER_OUTLET]]


def fetch_outlet(session: requests.Session, meta: dict) -> dict:
    result = {
        "source": meta["source"], "country": meta["country"],
        "lang": meta["lang"], "url": meta["url"],
        "headlines": [], "coverage": [], "error": None,
    }
    source = meta["source"]
    domain = domain_of(meta["url"])

    # 1) Native feed (own domain as Referer dodges some blocks).
    native = parse_feed(session, meta["rss"], meta["url"] + "/") or []
    _clean_titles(native, source, domain)
    items = fresh_items(native, source)
    via = "native"

    # 2) Fall back to Google News only if native has no fresh items.
    gn = []
    if not items:
        gn = parse_feed(session, gnews_url(meta), "https://news.google.com/") or []
        _clean_titles(gn, source, domain)
        items = fresh_items(gn, source)
        via = "google-news"

    # 3) Last resort: if nothing is "fresh" anywhere, show the newest we have
    #    (still date-sorted, junk removed) rather than an empty card.
    if not items:
        items = [it for it in (native or gn)
                 if not is_junk_title(it["title"], source)
                 and not is_offtopic(it["title"], it.get("url", ""))]
        via += "/stale"

    if items:
        result["headlines"] = strip_internal(items)
        # Broader coverage sample for the Trends view (what the outlet is really
        # covering), captured from the SAME already-filtered item list.
        result["coverage"] = coverage_items(items)
        # Fill in missing descriptions from the article pages so every headline
        # can get a snippet, not just the ones whose feed shipped a description.
        filled = enrich_missing_descriptions(session, result["headlines"])
        newest = items[0]["published"][:10] or "undated"
        extra = f", +{filled} desc" if filled else ""
        print(f"  + {source}: {len(result['headlines'])} headlines "
              f"({len(result['coverage'])} in coverage, {via}, newest {newest}{extra})")
    else:
        result["error"] = "no entries"
        print(f"  x {source}: no entries (native + google-news failed)", file=sys.stderr)
    return result


def _titles_hash(regions: dict) -> str:
    """SHA-256 (truncated) of all titles + descriptions — the cache key for
    snippets and translations. Including descriptions means a snippet refreshes
    if its source text changes, even when the title is identical."""
    parts = [SNIPPET_VERSION]
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


def generate_snippets(regions: dict, existing_output: dict = None) -> dict:
    """Generate a short English snippet for each headline, using the Gemini API.

    Mutates `regions` in place: adds a "snippet" field to each headline and
    removes the temporary "description" field. Returns {"titles_hash": ...} when
    snippets were produced (so the next run can skip unchanged content), or {} on
    skip/failure — always non-fatal.

    The site is English-only, so no translations are generated; this is a single
    batched Gemini call per run, and none at all when the headlines are unchanged.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  GEMINI_API_KEY not set — skipping snippets", file=sys.stderr)
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

    # Cache hit: reuse last run's snippets, with no API call.
    if existing_output and existing_output.get("titles_hash") == current_hash:
        prev = existing_output.get("regions")
        if prev:
            for i, (region, o_idx, h_idx) in enumerate(positions):
                try:
                    snip = prev[region][o_idx]["headlines"][h_idx].get("snippet", "")
                except Exception:
                    snip = ""
                # Scrub on reuse too, so a previously-stored stale title (e.g.
                # "former president Trump") is corrected even on a cache hit.
                regions[region][o_idx]["headlines"][h_idx]["snippet"] = \
                    scrub_stale_titles(snip, f"{titles[i]} {descriptions[i]}")
            _strip_descriptions(regions)
            print(f"  Content unchanged — reusing snippets (hash {current_hash})")
            return {"titles_hash": current_hash}

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

    # ---- Short English snippets — EVERY headline gets one, sustainably ----
    # When the feed gave us a real description we summarise it; when it gave us
    # nothing (e.g. the Google-News fallback, whose opaque links can't be
    # enriched) we expand the headline itself with safe, widely-known context.
    # That makes the site's expansions CONSISTENT — instead of present for the
    # outlets on a working native feed and blank for the ones on Google News.
    today_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    snippet_items = [{"title": t, "description": d} for t, d in zip(titles, descriptions)]
    snip_prompt = (
        f"Today's date is {today_str}; treat it as the present. Do NOT use outside "
        "or training knowledge for time-sensitive facts (such as who currently "
        "holds any office).\n\n"
        "For each news item in the JSON array below, write a concise, informative "
        f"summary of two to three sentences (about {SNIPPET_WORDS} words) in English.\n"
        "Rules for EVERY item:\n"
        "1. If 'description' has real content, summarise it faithfully, keeping its "
        "specific facts (numbers, names, places, quotes). Use ONLY what 'description' "
        "states.\n"
        "2. If 'description' is empty or merely repeats 'title', expand the 'title': "
        "restate it as a clear sentence and add only TIMELESS context — e.g. which "
        "country a city is in, or what an organisation broadly is. Do NOT add who "
        "currently leads or holds any position.\n"
        "3. CRITICAL — do not add, infer, or change anyone's title, office, role or "
        "status. Never write 'president', 'former', 'current', 'ex-', 'prime "
        "minister', 'minister', etc. for a person unless that exact word already "
        "appears in the source text. Refer to each person EXACTLY as the source does "
        "(if it says 'Trump', write 'Trump', never 'former president Trump').\n"
        "4. NEVER invent specifics that are not given — no made-up numbers, dates, "
        "casualties, quotes, outcomes or events. If a detail isn't available, omit "
        "it; do not speculate or pad.\n"
        "5. Always return a non-empty string for every item.\n\n"
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

    for i, (region, o_idx, h_idx) in enumerate(positions):
        hl = regions[region][o_idx]["headlines"][h_idx]
        # Never wipe a good snippet with an empty refresh: if this run produced
        # no snippet for a headline that already had one (e.g. a carried-over
        # stale headline, or a description that briefly vanished), keep the old one.
        snip = en_snippets[i] or hl.get("snippet", "")
        if not snip:
            # AI unavailable (e.g. quota) and no prior snippet — expand from the
            # feed's own description so the headline still gets a summary.
            snip = fallback_snippet(descriptions[i])
        # Deterministic backstop: strip any leader office title the model added
        # that the source never stated (e.g. the recurring "former president
        # Trump"). Applied to reused snippets too, so old bad text is corrected.
        hl["snippet"] = scrub_stale_titles(snip, f"{titles[i]} {descriptions[i]}")
    n_snips = sum(1 for (region, o_idx, h_idx) in positions
                  if regions[region][o_idx]["headlines"][h_idx].get("snippet"))
    _strip_descriptions(regions)
    print(f"  Generated {n_snips}/{len(titles)} English snippets")

    # Record the hash only when snippets were freshly generated, so the next run
    # can skip the API call for unchanged headlines.
    return {"titles_hash": current_hash} if snippets_ok else {}


# How long a failing outlet keeps showing its last good headlines before the
# card finally reads "Feed unavailable". Long enough to ride out a bad night of
# blocks/outages, short enough that nothing visibly stale lingers for days.
CARRY_FORWARD_HOURS = 18


def apply_carry_forward(output: dict, existing: dict) -> int:
    """Keep the previous run's headlines for any outlet that returned nothing
    this run, so a transient block/outage doesn't blank its card ("Feed
    unavailable today"). Mirrors the snippet/translation reuse-on-failure logic.

    Carried outlets are tagged stale=True (with stale_since = when the data was
    actually fresh) and stop carrying once that data is older than
    CARRY_FORWARD_HOURS. Mutates `output` in place; returns how many were carried.
    """
    if not existing:
        return 0
    now = datetime.now(timezone.utc)
    prev_updated = existing.get("updated", "")
    prev_regions = existing.get("regions", {})
    carried = 0
    for region, outs in output["regions"].items():
        prev_by_source = {o["source"]: o for o in prev_regions.get(region, [])}
        for o in outs:
            if o.get("headlines"):
                continue
            prev = prev_by_source.get(o["source"])
            if not prev or not prev.get("headlines"):
                continue
            stale_since = prev.get("stale_since") or prev_updated
            try:
                age_h = (now - datetime.fromisoformat(stale_since)).total_seconds() / 3600
            except Exception:
                continue
            if age_h > CARRY_FORWARD_HOURS:
                continue
            o["headlines"] = copy.deepcopy(prev["headlines"])
            o["error"] = None
            o["stale"] = True
            o["stale_since"] = stale_since
            carried += 1
            print(f"  ~ {o['source']}: carried {len(o['headlines'])} prior headlines "
                  f"(feed down, {age_h:.1f}h old)", file=sys.stderr)
    return carried


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

    # Backfill any outlet that came back empty with its last-known-good headlines
    # before snippets run, so carried cards stay fully populated.
    carried = apply_carry_forward(output, existing)
    if carried:
        print(f"\n[Carry-forward] kept {carried} outlet(s) from the previous run")

    print("\n[Snippets]")
    # Adds English snippets to output["regions"], strips raw descriptions, and
    # returns {"titles_hash": ...} so unchanged runs can skip the API call.
    snippet_meta = generate_snippets(output["regions"], existing)
    output.update(snippet_meta)

    # Split the broad per-outlet coverage out into its own file BEFORE writing
    # headlines.json, so the Headlines tab's download stays small (5/outlet) while
    # Trends can read what each outlet is actually covering (up to 40/outlet).
    cov_path = Path(__file__).parent.parent / "coverage.json"

    def _outlet_coverage(o):
        # Prefer this run's broad sample; if the feed was down and we carried its
        # previous headlines forward, fall back to those so Trends still reflects
        # what the outlet is showing rather than dropping it to zero.
        cov = o.get("coverage") or []
        if not cov:
            cov = [{"title": h.get("title", ""), "url": h.get("url", ""),
                    "published": h.get("published", "")}
                   for h in o.get("headlines", [])]
        return cov

    coverage_out = {
        "updated": output["updated"],
        "regions": {
            region: [
                {"source": o["source"], "country": o.get("country", ""),
                 "lang": o.get("lang", ""), "url": o.get("url", ""),
                 "coverage": _outlet_coverage(o)}
                for o in outs
            ]
            for region, outs in output["regions"].items()
        },
    }
    cov_total = sum(len(o["coverage"]) for outs in coverage_out["regions"].values()
                    for o in outs)
    # Remove the bulky coverage list from the headlines payload.
    for outs in output["regions"].values():
        for o in outs:
            o.pop("coverage", None)

    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    cov_path.write_text(json.dumps(coverage_out, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(o["headlines"]) for outs in output["regions"].values() for o in outs)
    ok = sum(1 for outs in output["regions"].values() for o in outs if o["headlines"])
    stale = sum(1 for outs in output["regions"].values() for o in outs if o.get("stale"))
    down = sum(1 for outs in output["regions"].values() for o in outs if not o["headlines"])
    print(f"\nWrote {out_path} — {total} headlines, {ok} outlets live"
          f"{f' ({stale} carried-forward)' if stale else ''}"
          f"{f', {down} still down' if down else ''}")
    print(f"Wrote {cov_path} — {cov_total} coverage items across {ok} outlets "
          f"(for Trends)")


if __name__ == "__main__":
    main()
