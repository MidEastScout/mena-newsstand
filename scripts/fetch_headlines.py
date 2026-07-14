#!/usr/bin/env python3
"""
WHAT THIS SCRIPT DOES (plain English)
======================================
This script runs automatically on GitHub every 30 minutes. It visits each of
the 27 news outlets listed in SOURCES below, reads their RSS feed (a standard
machine-readable list of recent articles), picks the 5 most SIGNIFICANT
headlines per outlet, and saves everything to a file called headlines.json in
the root of this repo. The website then reads that file to show you the
headlines — no live fetching happens in the visitor's browser.

HOW "MOST SIGNIFICANT" IS DECIDED (per outlet, best signal available)
======================================================================
We don't have any outlet's internal traffic analytics, so each outlet's top 5
is ranked by the best available real proxy, in this priority order:
  1. "most_read"  — the outlet's own public most-read/trending feed, where one
                    exists (configured per outlet via the "mostread" key below;
                    found by scripts/probe_mostread.py). This reflects real
                    reader behaviour ON THAT OUTLET.
  2. "clicks"     — what readers of THIS site clicked over the last 7 days.
                    Clicks are counted by the same Cloudflare Worker that
                    stores push subscriptions (push/worker.js), aggregated
                    across all visitors, and fetched here via PUSH_API.
  3. "coverage"   — cross-outlet breadth: an outlet's headline ranks higher
                    when the same story is also carried by many OTHER outlets
                    (the same idea as the Top-5 story clustering).
  4. "latest"     — plain recency, when none of the above has data yet.
The method used is written to headlines.json as each outlet's "rank_method",
and the site shows it as a small tag so readers know what the ranking means.

HOW TO ADD OR REMOVE AN OUTLET
================================
Find the SOURCES dictionary below. Each outlet is one block that looks like:
    {
        "source": "Outlet Name",
        "country": "Country",
        "lang": "en",          ← language code: "en", "ar", "he", etc.
        "url": "https://...",  ← the outlet's homepage
        "rss": "https://...",  ← the RSS feed URL (findable via the outlet's site)
        "mostread": ...,       ← OPTIONAL most-read source (see below)
    },
Add a new block inside the right region (Gulf / Levant / Israel / Pan-Arab),
or delete an existing block to remove it. The region names must match exactly
what's used in index.html.

The optional "mostread" key points at the outlet's public most-read/trending
signal (probed 2026-07-14 by scripts/probe_mostread.py — rerun it to re-check):
    "mostread": "https://…/rss/mostread"            ← a real most-read RSS feed
    "mostread": {"page": "https://…/", "marker": "most read"}
        ← a server-rendered homepage module: the article links following the
          marker text, scraped in on-page order (that order IS the ranking)
    "mostread": {"page": "https://…/trending", "link_re": "/trending/."}
        ← a dedicated trending SECTION: links matching link_re, page order
Outlets without a usable signal simply omit the key and fall back to on-site
clicks → cross-outlet coverage → recency (see rank_and_select).

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
            "source": "Gulf News", "country": "UAE", "lang": "en",
            "url": "https://gulfnews.com",
            "rss": "https://gulfnews.com/rss",
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
            # Server-rendered "Most read" homepage module (probe 2026-07-14).
            "mostread": {"page": "https://today.lorientlejour.com/",
                         "marker": "most read"},
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
            # Server-rendered "الأكثر قراءة" (most read) homepage module.
            "mostread": {"page": "https://www.almanar.com.lb/",
                         "marker": "الأكثر قراءة"},
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
        {
            # SANA — Syria's official state news agency (English edition). Like
            # the other state wires (IRNA, Anadolu) it may block a datacenter IP
            # on its native feed; the Google News fallback (site:sana.sy, English
            # locale) then surfaces real articles.
            "source": "SANA", "country": "Syria", "lang": "en",
            "url": "https://sana.sy/en",
            "rss": "https://sana.sy/en/?feed=rss2",
        },
        {
            # Syria Direct — independent, English-language Syria-focused outlet.
            "source": "Syria Direct", "country": "Syria", "lang": "en",
            "url": "https://syriadirect.org",
            "rss": "https://syriadirect.org/feed/",
        },
    ],
    "Pan-Arab": [
        {
            "source": "Al Jazeera", "country": "Qatar", "lang": "en",
            "url": "https://www.aljazeera.com",
            "rss": "https://www.aljazeera.com/xml/rss/all.xml",
            # Server-rendered "Most popular" homepage module.
            "mostread": {"page": "https://www.aljazeera.com/",
                         "marker": "most popular"},
        },
        {
            "source": "Middle East Eye", "country": "UK", "lang": "en",
            "url": "https://www.middleeasteye.net",
            "rss": "https://www.middleeasteye.net/rss",
            # Dedicated /trending section; its articles live under /trending/.
            "mostread": {"page": "https://www.middleeasteye.net/trending",
                         "link_re": r"/trending/."},
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
            # Server-rendered "Most Viewed" homepage module (small — the probe
            # saw ~3 links; whatever it yields leads, the rest fills by rank).
            "mostread": {"page": "https://www.newarab.com/",
                         "marker": "most viewed"},
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
            # Server-rendered "Most Viewed" homepage module.
            "mostread": {"page": "https://en.mehrnews.com/",
                         "marker": "most viewed"},
        },
        {
            # Iran International — London-based Persian-language broadcaster with a
            # high-volume, very well indexed English edition (iranintl.com/en). Like
            # IRNA above, its native feed is often blocked from a datacenter IP, but
            # the Google News fallback (site:iranintl.com, English locale) reliably
            # surfaces real articles. Adds an outside-Iran editorial voice alongside
            # the state wires IRNA and Mehr.
            "source": "Iran International", "country": "Iran", "lang": "en",
            "url": "https://www.iranintl.com/en",
            "rss": "https://www.iranintl.com/en/rss",
            # Server-rendered "Most Viewed" module on the /en homepage (the
            # /en/mostread-style paths are Next.js soft-404s — do not use).
            "mostread": {"page": "https://www.iranintl.com/en/",
                         "marker": "most viewed"},
        },
    ],
    "Turkey": [
        {
            # Anadolu Agency — Türkiye's official state news wire (English
            # edition). High-volume and very well indexed, so the Google News
            # fallback reliably surfaces real articles even if the native feed
            # is blocked from a datacenter IP (same pattern as IRNA above).
            "source": "Anadolu Agency", "country": "Turkey", "lang": "en",
            "url": "https://www.aa.com.tr/en",
            "rss": "https://www.aa.com.tr/en/rss/default?cat=guncel",
        },
        {
            "source": "Daily Sabah", "country": "Turkey", "lang": "en",
            "url": "https://www.dailysabah.com",
            "rss": "https://www.dailysabah.com/rss",
            # The one outlet with a REAL most-read RSS feed (50 ranked entries).
            "mostread": "https://www.dailysabah.com/rss/mostread",
        },
        {
            "source": "Hürriyet Daily News", "country": "Turkey", "lang": "en",
            "url": "https://www.hurriyetdailynews.com",
            "rss": "https://www.hurriyetdailynews.com/rss",
            # Server-rendered "MOST POPULAR" homepage module. (Its
            # /rss/popular URL is a keyword-SEARCH feed for the word
            # "popular", not a most-read feed — do not use.)
            "mostread": {"page": "https://www.hurriyetdailynews.com/",
                         "marker": "most popular"},
        },
        {
            "source": "TRT World", "country": "Turkey", "lang": "en",
            "url": "https://www.trtworld.com",
            "rss": "https://www.trtworld.com/rss",
        },
    ],
    "Yemen": [
        {
            # Al-Masirah — the Ansar Allah (Houthi) satellite channel's news
            # site. lang "ar": the Arabic headline is kept and translated to
            # Hebrew for HE mode, with an English snippet generated like every
            # other Arabic outlet (Al Manar, Al Mayadeen). If the native feed is
            # blocked from a datacenter IP, the Google News fallback
            # (site:almasirah.net.ye, ar locale) surfaces recent articles.
            "source": "Al-Masirah", "country": "Yemen", "lang": "ar",
            "url": "https://www.almasirah.net.ye",
            "rss": "https://www.almasirah.net.ye/rss",
            # The feed bolts the site's Arabic name + Hijri/Gregorian date stamps
            # onto titles and floods the wire with non-news: TV-programme episodes
            # and teasers (pipe-delimited), bare site-label cards, ideological
            # columns and op-eds. Real reporting is a plain declarative sentence
            # with none of those markers — so drop the markers, not the news.
            "strip_affixes": ["المسيرة نت"],
            "drop_patterns": [
                r"\|",                            # programme / teaser / "special coverage" formats
                r"^\s*المسيرة نت\s*[-–—|]?\s*$",   # bare site-label cards
                r"تغطية خاصة",                     # "special coverage" opinion roundups
                r"مقامة",                          # maqāma eulogies
                r"ثقافة الغدير",                   # recurring ideological column
                r"!!",                            # op-eds
                r"\.\.\s*$", r"…\s*$",            # bare teasers
            ],
        },
    ],
    # Israeli broadcast channels. lang "he" — their headlines stay in the
    # original Hebrew everywhere on the site (never translated to English); the
    # English snippet + EN↔HE toggle work as for every other outlet. Sources,
    # probed 2026-07-05 from a datacenter IP (state/probe_sources.json):
    #   • Kan 11  — the newsflash API is a real RSS 2.0 feed, but it declares a
    #     bogus encoding="utf-16"; parse_feed strips the declaration and retries.
    #   • N12     — Mako ships per-section RSS; we merge domestic + military +
    #     world so the card stays fresh even when one section's feed lags.
    #   • Ch. 13  — no RSS at all (Google stopped indexing it in 2021); its
    #     Google-news sitemap carries the last ~48h of articles with Hebrew
    #     titles + timestamps, parsed by parse_news_sitemap.
    "Israel": [
        {
            "source": "Kan 11", "country": "Israel", "lang": "he",
            "url": "https://www.kan.org.il",
            "rss": "https://www.kan.org.il/api/newsflash/v2/Newsflash",
        },
        {
            "source": "N12", "country": "Israel", "lang": "he",
            "url": "https://www.mako.co.il",
            "rss": [
                "https://rcs.mako.co.il/rss/news-israel.xml",
                "https://rcs.mako.co.il/rss/news-military.xml",
                "https://rcs.mako.co.il/rss/news-world.xml",
            ],
        },
        {
            "source": "Channel 13", "country": "Israel", "lang": "he",
            "url": "https://13tv.co.il",
            "rss": None,
            "sitemap": "https://13tv.co.il/Services/sitemapGenerator/xmls/news_sitemap.xml",
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

# Hebrew sports / entertainment / lifestyle for the Israeli channels. Like the
# Arabic list, matched as substrings, so only unambiguous multi-letter terms
# that never hide inside a geopolitical word are listed.
OFFTOPIC_HE = [
    "כדורגל", "כדורסל", "כדוריד", "ליגת האלופות", "ליגת העל", "מכבי תל אביב",
    "הפועל תל אביב", "מכבי חיפה", "משחקי הליגה",                  # sports
    "אירוויזיון", "האח הגדול", "ריאליטי", "רכילות", "פרשת השבוע",
    "מתכונים", "אופנה", "בישול", "הורוסקופ", "אסטרולוגיה",         # entertainment / lifestyle
]
OFFTOPIC_HE_RE = re.compile("|".join(re.escape(t) for t in OFFTOPIC_HE))

# TRACKED-but-off-the-wall topics. These are NOT MENA geopolitics, so they are
# kept off the Headlines wall — but they ARE major media stories worth measuring,
# so they must survive the off-topic filter and reach the broad coverage sample
# that feeds the Pulse and Trends views. Currently: the football World Cup
# (English + the Arabic "كأس العالم" / colloquial "المونديال"). Everything else
# in OFFTOPIC_* stays fully filtered out of every view.
TRACKED_OFFTOPIC_RE = re.compile(
    r"\bworld\s*cup\b|كأس العالم|المونديال|מונדיאל|גביע העולם", re.I)

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
# they're written to headlines.json). main() only fetches regions listed here,
# so this MUST cover every region in SOURCES — the assert makes a new region
# added to SOURCES fail loudly instead of being silently skipped.
REGION_ORDER = ["Israel", "Pan-Arab", "Iran", "Levant", "Gulf", "Yemen", "Turkey"]
assert set(REGION_ORDER) == set(SOURCES), (
    f"REGION_ORDER {sorted(REGION_ORDER)} must match SOURCES regions "
    f"{sorted(SOURCES)}")

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
SNIPPET_VERSION = "v8-he"

# ---- Azure Translator (free tier) — the Hebrew engine for the wall/pulse ----
# Gemini's free tier is only ~20 requests/day — far too little to translate the
# whole wall alongside the English snippets. Azure Translator's free tier gives
# 2,000,000 characters/month and supports Hebrew, so it powers the bulk
# translation. Requires two repo secrets, passed through by the workflow:
#   AZURE_TRANSLATOR_KEY     – the resource key
#   AZURE_TRANSLATOR_REGION  – the resource region (e.g. "westeurope"; "global"
#                              works for a global resource)
AZURE_ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"

# Per-item Hebrew cache: {"t": {en_title: he}, "s": {en_snippet: he}}. Only text
# we have NEVER translated is sent to Azure, so steady-state usage is the handful
# of genuinely new headlines per run — not the whole wall every time (which would
# burn the 2M/month budget in a day). FIFO-capped so the file stays bounded.
HE_CACHE_PATH = Path(__file__).parent.parent / "state" / "headlines_he.json"
HE_CACHE_CAP = 4000


def azure_translate(texts, to_lang="he"):
    """Translate a list of strings via Azure Translator, preserving order.

    Returns a list the same length as `texts`, or None when the key is missing
    or every retry failed (callers then keep whatever Hebrew they already have).
    Empty inputs come back empty and cost nothing against the quota."""
    key = os.environ.get("AZURE_TRANSLATOR_KEY")
    if not key or not texts:
        return None
    region = os.environ.get("AZURE_TRANSLATOR_REGION", "global")
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Ocp-Apim-Subscription-Region": region,
        "Content-Type": "application/json",
    }
    params = {"api-version": "3.0", "to": to_lang}
    out = []
    i = 0
    # Azure caps a request at 1000 array elements and 50,000 characters; chunk
    # under both, with headroom.
    while i < len(texts):
        chunk, chars = [], 0
        while i < len(texts) and len(chunk) < 900 and chars < 45000:
            t = texts[i] or ""
            chunk.append(t)
            chars += len(t) + 1
            i += 1
        body = [{"Text": t} for t in chunk]
        for attempt in range(3):
            try:
                r = requests.post(AZURE_ENDPOINT, params=params, headers=headers,
                                  json=body, timeout=30)
                if r.status_code == 429 and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                r.raise_for_status()
                out.extend(item["translations"][0]["text"] for item in r.json())
                break
            except Exception as exc:
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                print(f"  Azure translate failed: {exc}", file=sys.stderr)
                return None
    return out if len(out) == len(texts) else None


def _load_he_cache():
    try:
        d = json.loads(HE_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("t", {})
    d.setdefault("s", {})
    return d


def _save_he_cache(cache):
    try:
        HE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        HE_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    except Exception as exc:
        print(f"  could not save Hebrew cache ({exc})", file=sys.stderr)


def translate_he_cached(texts, bucket):
    """Map each string in `texts` to Hebrew, calling Azure only for strings not
    already in `bucket` (mutated in place, FIFO-pruned). Returns a list aligned
    to `texts`; cache misses that Azure couldn't fill come back '' so the site
    falls back to English for exactly those items."""
    seen, need = set(), []
    for t in texts:
        if t and t not in bucket and t not in seen:
            seen.add(t)
            need.append(t)
    if need:
        got = azure_translate(need)
        if got:
            for src, he in zip(need, got):
                if he:
                    bucket[src] = he
            if len(bucket) > HE_CACHE_CAP:
                for k in list(bucket)[:len(bucket) - HE_CACHE_CAP]:
                    del bucket[k]
    return [bucket.get(t or "", "") for t in texts]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
    # No "br": brotli isn't in requirements, so advertising it makes servers that
    # honour it (e.g. Kan's newsflash API) return undecodable bytes → an empty
    # feed. gzip/deflate are decoded natively by requests.
    "Accept-Encoding": "gzip, deflate",
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
# An embedded Hijri/Gregorian date stamp some Arabic feeds bolt into the title,
# e.g. "21‏-01‏-1448هـ 06‏-07‏-2026م" — digits (Latin or Arabic-Indic) joined by
# dashes/slashes and closed by an era marker (هـ = Hijri, م = Gregorian). RTL/LRM
# marks between the pieces are tolerated. Never part of an actual headline.
_EMBEDDED_DATE_RE = re.compile(
    r"\s*[‎‏]?[\d٠-٩]{1,4}"
    r"(?:[‎‏]?[-‐-―/.][‎‏]?[\d٠-٩]{1,4}){1,2}"
    r"[‎‏]?\s*(?:هـ|م)\b")


def clean_title(title: str, source: str, domain: str, extra_affixes=None) -> str:
    """Strip the publisher label Google News bolts onto a headline.

    Google News rewrites titles as "<Outlet> | Real headline - <Outlet>" or
    "Real headline - outlet-domain.tld". We strip a trailing source/domain suffix
    and a leading source prefix so the displayed headline is the actual headline
    (and dedupes correctly). Only strips when the affix matches THIS outlet (its
    name or domain) or is a bare domain — so real headlines are never touched.

    extra_affixes: outlet-declared publisher/section labels in the outlet's own
    language that the English source-name match above can't catch (e.g. Arabic
    "المسيرة نت"). Each is stripped as a leading or trailing affix (with a
    separator). Embedded date stamps are stripped for every outlet.
    """
    if not title:
        return title
    t = _EMBEDDED_DATE_RE.sub(" ", title).strip()
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
    # Outlet-declared native-language affixes (leading or trailing, with a
    # separator), e.g. Al-Masirah's "المسيرة نت" that the English name can't match.
    for aff in (extra_affixes or []):
        a = aff.strip()
        if not a:
            continue
        for sep in TITLE_SEPS:
            if t.endswith(sep + a):
                t = t[: -len(sep + a)].strip()
            if t.startswith(a + sep):
                t = t[len(a + sep):].strip()
    t = re.sub(r"\s{2,}", " ", t).strip()
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


# ── Google News redirect resolution ─────────────────────────────────────────
# Outlets whose native feed fails fall back to Google News, whose RSS <link>s are
# opaque `news.google.com/rss/articles/CBMi…` redirects rather than real article
# URLs. Some of those redirects are dead (removed articles, regional consent
# walls, the newer opaque format that never resolves for some clients) — which is
# why a few headlines "lead to a website that does not work". We turn each one
# into the publisher's real article URL at fetch time, so a click opens the
# actual story with no Google/JS/consent hop in between. If resolution fails we
# keep the original Google link (which usually still redirects in a browser), so
# a failure never drops or worsens a headline.
GNEWS_ART_RE = re.compile(r'news\.google\.com/rss/articles/([^?/#]+)', re.I)
GNEWS_RESOLVE_BUDGET = 90      # cap NEW network resolutions per run
GNEWS_RESOLVE_TIMEOUT = 12     # seconds; shorter than feed reads so we don't hang
_gnews_cache = {}              # in-run memo: google url -> resolved url (or "")
_gnews_budget_left = GNEWS_RESOLVE_BUDGET


def _gnews_real_url(session: requests.Session, art_url: str):
    """Resolve one news.google.com/rss/articles/… redirect to the publisher's
    real URL via Google's batchexecute RPC. Returns the URL or None."""
    m = GNEWS_ART_RE.search(art_url)
    if not m:
        return None
    art_id = m.group(1)
    ua = {"User-Agent": HEADERS["User-Agent"], "Accept-Language": "en-US,en;q=0.9"}
    # 1) Fetch the article shell to read this article's signature + timestamp.
    r = session.get(f"https://news.google.com/rss/articles/{art_id}",
                    headers={**ua, "Accept": "text/html,*/*"},
                    timeout=GNEWS_RESOLVE_TIMEOUT)
    r.raise_for_status()
    doc = r.text
    sg = re.search(r'data-n-a-sg="([^"]+)"', doc)
    ts = re.search(r'data-n-a-ts="([^"]+)"', doc)
    if not (sg and ts):
        return None
    # 2) Exchange (id, ts, sg) for the destination URL.
    inner = json.dumps([
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
          None, None, None, None, None, 0, 1],
         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        art_id, int(ts.group(1)), sg.group(1),
    ])
    freq = json.dumps([[["Fbv4je", inner, None, "generic"]]])
    r = session.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={**ua, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data={"f.req": freq}, timeout=GNEWS_RESOLVE_TIMEOUT)
    r.raise_for_status()
    # The resolved URL is embedded as a JSON-escaped string; unescape first (so
    # \/ path slashes and \" quotes are restored). Prefer the value right after
    # the "garturlres" marker (the exact article URL); only if that's missing
    # fall back to the first non-Google URL — robust to Google's envelope shape.
    text = (r.text.replace('\\/', '/').replace('\\u003d', '=')
            .replace('\\u0026', '&').replace('\\u003f', '?').replace('\\"', '"'))
    m = re.search(r'garturlres"\s*,\s*"(https?://[^"\\]+)"', text)
    if m and "google.com" not in m.group(1):
        return m.group(1)
    for u in re.findall(r'https?://[^\s"\\]+', text):
        if "google.com" not in u and "gstatic.com" not in u:
            return u
    return None


def resolve_gnews_url(session: requests.Session, url: str) -> str:
    """Best-effort: return the real article URL behind a Google News redirect.
    Non-Google URLs pass straight through. On any failure (or once the per-run
    budget is spent) the original URL is returned, so this can only ever improve
    a link, never break one."""
    global _gnews_budget_left
    if not url or "news.google.com/rss/articles/" not in url:
        return url
    if url in _gnews_cache:
        return _gnews_cache[url] or url
    if _gnews_budget_left <= 0:
        return url
    _gnews_budget_left -= 1
    try:
        real = _gnews_real_url(session, url)
    except Exception as exc:
        print(f"      ! gnews resolve failed: {str(exc)[:80]}", file=sys.stderr)
        real = None
    _gnews_cache[url] = real or ""      # memo within this run (successes only reused)
    return real or url


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
    # Tracked topics (World Cup) are off the Headlines wall but counted by Pulse
    # and Trends, so they must NOT be treated as off-topic here — they need to
    # survive into the broad coverage sample. They're removed from the displayed
    # headlines separately in fetch_outlet().
    if title and TRACKED_OFFTOPIC_RE.search(title):
        return False
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
        # Hebrew-language sports / lifestyle (the Israeli channels).
        if OFFTOPIC_HE_RE.search(title):
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


def parse_feed(session: requests.Session, url: str, referer, sort=True):
    """Return entries as list of dicts sorted newest-first, or None on failure.

    Each dict: {title, url, published(iso str), description(str), _dt(datetime)}.
    sort=False keeps the feed's own order — a most-read feed's order IS its
    ranking, so re-sorting it by date would destroy the signal.
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
    if not feed.entries:
        # Some feeds ship a wrong XML encoding declaration (e.g. Kan's newsflash
        # API declares encoding="utf-16" but sends UTF-8), which stops feedparser
        # cold on the raw bytes. Retry on the decoded text with the declaration
        # stripped — a no-op for well-formed feeds, a rescue for mislabelled ones.
        stripped = re.sub(r"^\s*<\?xml[^>]*\?>", "", resp.text, count=1)
        if stripped != resp.text:
            feed = feedparser.parse(stripped)
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
    if sort:
        floor = datetime.min.replace(tzinfo=timezone.utc)
        items.sort(key=lambda x: x["_dt"] or floor, reverse=True)
    return items or None


def parse_news_sitemap(session: requests.Session, url: str, referer):
    """Parse a Google-news XML sitemap into the same item dicts as parse_feed.

    For outlets with no RSS (Channel 13): each <url> block carries <loc> (the
    article link), <news:title> (the Hebrew headline) and <news:publication_date>.
    Namespace prefixes vary, so the tag matches allow any 'prefix:' form.
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
    body = resp.text
    items = []
    for block in re.findall(r"<url>(.*?)</url>", body, re.S):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.S)
        title = re.search(r"<(?:\w+:)?title>\s*(.*?)\s*</(?:\w+:)?title>", block, re.S)
        if not (loc and title):
            continue
        link = html.unescape(loc.group(1)).strip()
        # Skip TV magazine / talk-show segments. Channel 13 lists these under its
        # news section too, but they're video clips ("Behind the Money" etc.), not
        # news articles — and it stamps them all with a date-only 00:00 timestamp,
        # so they'd otherwise dominate the feed.
        low = link.lower()
        if "/clips/" in low or "/episodes/" in low or re.search(r"/season-?\d", low):
            continue
        t = html.unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title.group(1), flags=re.S)).strip()
        if not link or not t:
            continue
        # Order by the most precise timestamp available: news:publication_date is
        # often date-only here, while <lastmod> carries the real time — take the max.
        dt = None
        for m in re.finditer(
                r"<(?:\w+:)?(?:publication_date|lastmod)>\s*([0-9T:+\-]{10,})\s*</", block):
            try:
                cand = datetime.fromisoformat(m.group(1).strip())
                if cand.tzinfo is None:
                    cand = cand.replace(tzinfo=timezone.utc)
                if dt is None or cand > dt:
                    dt = cand
            except Exception:
                pass
        items.append({
            "title": t,
            "url": link,
            "published": dt.isoformat() if dt else "",
            "description": "",     # sitemaps carry no summary; snippet expands the title
            "_dt": dt,
        })
    floor = datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=lambda x: x["_dt"] or floor, reverse=True)
    return items or None


def _clean_titles(items, source: str, domain: str, extra_affixes=None):
    """Strip Google News publisher affixes from every entry's title in place."""
    for it in items:
        it["title"] = clean_title(it["title"], source, domain, extra_affixes)


def fresh_items(items, source: str, drop_re=None):
    """Keep only recent, non-junk, dated entries. drop_re (optional) drops an
    outlet's opinion/teaser/eulogy items — those the outlet files as commentary
    rather than news (see the per-outlet 'drop_patterns' in SOURCES)."""
    if not items:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    return [
        it for it in items
        if it["_dt"] and it["_dt"] >= cutoff
        and not is_junk_title(it["title"], source)
        and not is_offtopic(it["title"], it.get("url", ""))
        and not (drop_re and drop_re.search(it["title"]))
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
    for freshness, junk and off-topic by fresh_items(). Doubles as the "show all
    headlines" list behind each outlet's top 5 on the site; items that are
    tracked-but-off-the-wall (e.g. the World Cup) are flagged "off" so the wall
    can hide them while Pulse and Trends still count them."""
    out = []
    for it in items[:COVERAGE_PER_OUTLET]:
        d = {"title": it["title"], "url": it["url"], "published": it["published"]}
        if TRACKED_OFFTOPIC_RE.search(it["title"]):
            d["off"] = True
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Per-outlet significance ranking (see the module docstring's priority order).
# ---------------------------------------------------------------------------
CLICK_WINDOW_DAYS = 7    # rolling window for the on-site click signal
CLICK_MIN_TOTAL = 3      # an outlet needs at least this many clicks in window…
CLICK_MIN_ARTICLES = 2   # …spread over at least this many distinct articles,
                         # or the "clicks" ranking would just amplify one tap.

# The named-entity gazetteer from the Top-5 clustering doubles as the breadth
# matcher here. Import is best-effort: without it breadth falls back to plain
# token overlap, which still works (just a little less cross-language reach).
try:
    from build_topstories import STRONG as _TS_STRONG, _entities as _ts_entities
except Exception:                                    # pragma: no cover
    _TS_STRONG, _ts_entities = set(), (lambda text: set())

# Unicode-aware title tokens (the clustering STOP list is ASCII-only; headlines
# here are English, Arabic and Hebrew). Only unambiguous, high-frequency
# function words are listed for Arabic/Hebrew — precision over recall.
_RANK_STOP = set((
    "the a an and or but for nor with without from into onto over under after "
    "before during amid says said say will would could should has have had was "
    "were are is be been being not its his her their they this that these those "
    "more most than then also just about against between among across near "
    "still yet how what when where who why while since because two three four "
    "five first last next new news live update updates report reports breaking "
    "latest today year years day days week weeks month months"
).split()) | {
    "على", "إلى", "الى", "التي", "الذي", "بعد", "قبل", "ضد", "بين", "خلال",
    "اليوم", "أمام", "حول", "منذ", "عاجل", "أخبار", "لكن", "حتى", "عندما",
    "של", "את", "עם", "אחרי", "לפני", "נגד", "בין", "היום", "כדי", "אבל",
    "גם", "כל", "על", "לא", "זה", "הוא", "היא",
}


def _uni_tokens(text: str) -> set:
    words = re.findall(r"[^\W\d_]{3,}", (text or "").lower())
    return {w for w in words if w not in _RANK_STOP}


def _norm_title(t: str) -> str:
    """Whitespace/punctuation-insensitive key for matching the same article
    across runs and data files (click records, most-read entries, candidates)."""
    return re.sub(r"\W+", " ", (t or "").casefold()).strip()[:120]


def compute_breadth(outlets: list):
    """Set it['_breadth'] on every candidate item: how many OTHER outlets carry
    a matching story. The pairwise test mirrors the site's client-side cluster
    fallback ((shared strong entity AND corroborating overlap) OR near-duplicate
    title), so 'widely covered' here agrees with the Top-5 strip's notion of a
    story. Titles only — snippets don't exist yet at this stage, and bylines in
    descriptions would inflate matches."""
    entries = []
    for oi, o in enumerate(outlets):
        for it in o.get("_candidates", []):
            it["_breadth"] = 0
            toks = _uni_tokens(it["title"])
            ents = _ts_entities(it["title"])
            entries.append((oi, it, toks, ents & _TS_STRONG, ents))
    n = len(entries)
    matched = [set() for _ in range(n)]          # item → other-outlet indexes
    for a in range(n):
        oa, ia, ta, sa, ea = entries[a]
        for b in range(a + 1, n):
            ob, ib, tb, sb, eb = entries[b]
            if oa == ob:
                continue
            st = len(ta & tb)
            if st < 2:                            # cheap gate: nothing shared
                continue
            if (sa & sb and (len(ea & eb) >= 2 or st >= 2)) or st >= 4:
                matched[a].add(ob)
                matched[b].add(oa)
        ia["_breadth"] = len(matched[a])


def fetch_click_counts(session: requests.Session) -> list:
    """Rolling per-article click totals from the Cloudflare Worker (the same
    PUSH_API used by send_push.py). Returns [{url, n, source?, title?}, …] or
    [] when unconfigured/unreachable — ranking then falls back to breadth."""
    api = os.environ.get("PUSH_API", "").strip().rstrip("/")
    if not api:
        print("  PUSH_API not set — on-site click ranking unavailable this run",
              file=sys.stderr)
        return []
    for attempt in range(2):
        try:
            r = session.get(f"{api}/clicks?days={CLICK_WINDOW_DAYS}", timeout=15)
            r.raise_for_status()
            clicks = r.json().get("clicks") or []
            print(f"  {len(clicks)} articles with on-site clicks in the last "
                  f"{CLICK_WINDOW_DAYS}d")
            return clicks
        except Exception as exc:
            if attempt == 0:
                time.sleep(2)
                continue
            print(f"  could not fetch click counts ({exc})", file=sys.stderr)
    return []


# Asset/utility URLs an <a href> in a most-read module can never be.
_MR_SKIP_RE = re.compile(
    r"\.(?:css|js|json|xml|ico|png|jpe?g|gif|svg|webp|mp4|pdf)(?:\?|#|$)"
    r"|/(?:feed|rss|wp-json|tags?|category|author)s?(?:/|$)", re.I)


def scrape_mostread_page(session: requests.Session, url: str, marker: str,
                         link_re: str, referer: str, domain: str) -> list:
    """Extract a server-rendered 'most read' HTML module: the article links, in
    ON-PAGE ORDER (that order IS the ranking). With `marker`, only the window
    after the marker text is read (a homepage module); with `link_re`, links
    matching it are read from the whole page (a dedicated trending section).
    Titles come from the anchor text. Returns parse_feed-shaped items (undated —
    most-read modules rarely carry timestamps)."""
    headers = dict(HEADERS)
    headers["Accept"] = "text/html,*/*"
    headers["Referer"] = referer
    try:
        r = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        print(f"      ! mostread page {url} -> {exc}", file=sys.stderr)
        return []
    text = r.text
    if marker:
        idx = text.lower().find(marker.lower())
        if idx < 0:
            return []
        text = text[idx: idx + 8000]
    filt = re.compile(link_re) if link_re else None
    items, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         text, re.I | re.S):
        href = html.unescape(m.group(1)).strip()
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = f"https://{domain}{href}"
        if (domain not in urlparse(href).netloc.lower() or href in seen
                or _MR_SKIP_RE.search(href)):
            continue
        if filt and not filt.search(urlparse(href).path):
            continue
        title = html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 18:                       # nav labels, "More", images
            continue
        seen.add(href)
        items.append({"title": title, "url": href, "published": "",
                      "description": "", "_dt": None})
        if len(items) >= 10:                      # a module is never longer
            break
    return items


def fetch_mostread(session: requests.Session, meta: dict) -> list:
    """The outlet's own most-read/trending list, in the OUTLET'S ranking order,
    passed through the same junk/off-topic/age filters as the main feed. The
    per-outlet 'mostread' config is either a feed URL string or
    {"page": url, "marker": "Most Read"} for a server-rendered HTML module."""
    cfg = meta.get("mostread")
    if not cfg:
        return []
    if isinstance(cfg, str):
        cfg = {"feed": cfg}
    source, domain = meta["source"], domain_of(meta["url"])
    ref = meta["url"] + "/"
    if cfg.get("feed"):
        items = parse_feed(session, cfg["feed"], ref, sort=False) or []
    else:
        items = scrape_mostread_page(session, cfg["page"], cfg.get("marker", ""),
                                     cfg.get("link_re", ""), ref, domain)
    _clean_titles(items, source, domain, meta.get("strip_affixes"))
    patterns = meta.get("drop_patterns")
    drop_re = re.compile("|".join(patterns)) if patterns else None
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    out = []
    for it in items:
        if is_junk_title(it["title"], source):
            continue
        if is_offtopic(it["title"], it.get("url", "")):
            continue
        if TRACKED_OFFTOPIC_RE.search(it["title"]):
            continue
        if drop_re and drop_re.search(it["title"]):
            continue
        # Dated-and-stale is dropped; undated is kept — a most-read list is by
        # construction current, and many trending modules carry no timestamps.
        if it["_dt"] and it["_dt"] < cutoff:
            continue
        out.append(it)
        if len(out) >= HEADLINES_PER_OUTLET:
            break
    return out


def rank_and_select(session: requests.Session, outlet: dict,
                    clicks_by_url: dict, clicks_by_title: dict):
    """Pick the outlet's displayed top 5 from its candidate pool by the best
    available signal (most_read → clicks → coverage → latest) and record which
    method decided, so the site can label the ranking honestly."""
    cands = outlet.pop("_candidates", [])
    mostread = outlet.pop("_mostread", [])
    if not (cands or mostread):
        return
    floor = datetime.min.replace(tzinfo=timezone.utc)

    def recency(it):
        return it["_dt"] or floor

    def click_count(it):
        # URL match first; title match (scoped to this outlet) catches articles
        # whose Google News redirect URL differs run-to-run.
        return (clicks_by_url.get(it.get("url", ""), 0)
                or clicks_by_title.get((outlet["source"], _norm_title(it["title"])), 0))

    def breadth(it):
        return it.get("_breadth", 0)

    if mostread:
        method = "most_read"
        # Prefer the candidate-pool copy of each most-read article (it carries
        # the description + timestamp the feed gave us); fall back to the
        # most-read entry itself for articles outside the recent-news pool.
        by_url = {it["url"]: it for it in cands}
        by_title = {_norm_title(it["title"]): it for it in cands}
        chosen, seen = [], set()
        for mr in mostread:
            it = by_url.get(mr["url"]) or by_title.get(_norm_title(mr["title"])) or mr
            key = _norm_title(it["title"])
            if key in seen:
                continue
            seen.add(key)
            chosen.append(it)
        rest = [it for it in cands if _norm_title(it["title"]) not in seen]
        rest.sort(key=lambda it: (breadth(it), recency(it)), reverse=True)
        chosen.extend(rest[:HEADLINES_PER_OUTLET - len(chosen)])
    else:
        clicked = [(click_count(it), it) for it in cands]
        total = sum(c for c, _ in clicked)
        distinct = sum(1 for c, _ in clicked if c)
        if total >= CLICK_MIN_TOTAL and distinct >= CLICK_MIN_ARTICLES:
            method = "clicks"
            cands.sort(key=lambda it: (click_count(it), breadth(it), recency(it)),
                       reverse=True)
        elif any(breadth(it) for it in cands):
            method = "coverage"
            cands.sort(key=lambda it: (breadth(it), recency(it)), reverse=True)
        else:
            method = "latest"                     # no signal → the old behavior
            cands.sort(key=recency, reverse=True)
        chosen = cands[:HEADLINES_PER_OUTLET]

    # Turn any Google News redirect links into the publisher's real article URL
    # so clicked headlines open the actual story, not a dead Google page.
    for it in chosen[:HEADLINES_PER_OUTLET]:
        it["url"] = resolve_gnews_url(session, it.get("url", ""))
    outlet["headlines"] = strip_internal(chosen)
    outlet["rank_method"] = method
    # Fill in missing descriptions from the article pages so every displayed
    # headline can get a snippet.
    filled = enrich_missing_descriptions(session, outlet["headlines"])
    extra = f", +{filled} desc" if filled else ""
    print(f"  + {outlet['source']}: {len(outlet['headlines'])} headlines "
          f"via {method}{extra}")


def _fetch_native(session: requests.Session, meta: dict) -> list:
    """Load an outlet's own feed: a Google-news sitemap, a merged list of RSS
    feeds, or a single RSS feed — returning parse_feed-shaped items (deduped by
    URL, newest first)."""
    ref = meta["url"] + "/"
    if meta.get("sitemap"):
        return parse_news_sitemap(session, meta["sitemap"], ref) or []
    rss = meta.get("rss")
    urls = rss if isinstance(rss, (list, tuple)) else ([rss] if rss else [])
    merged, seen = [], set()
    for ru in urls:
        for it in (parse_feed(session, ru, ref) or []):
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            merged.append(it)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    merged.sort(key=lambda x: x["_dt"] or floor, reverse=True)
    return merged


def fetch_outlet(session: requests.Session, meta: dict) -> dict:
    result = {
        "source": meta["source"], "country": meta["country"],
        "lang": meta["lang"], "url": meta["url"],
        "headlines": [], "coverage": [], "error": None,
    }
    source = meta["source"]
    domain = domain_of(meta["url"])
    affixes = meta.get("strip_affixes")
    # Outlet-scoped opinion/teaser/eulogy filter: some outlets file commentary and
    # section teasers in the same feed as news (e.g. Al-Masirah's "تغطية خاصة"
    # roundups, "مقامة" eulogies, "!!" op-eds). Keep those off the news wall.
    patterns = meta.get("drop_patterns")
    drop_re = re.compile("|".join(patterns)) if patterns else None

    # 1) Native source. A paper may declare a single RSS url, a LIST of RSS urls
    #    (merged — e.g. N12's per-section feeds), or a Google-news sitemap
    #    (outlets with no RSS, e.g. Channel 13).
    native = _fetch_native(session, meta)
    _clean_titles(native, source, domain, affixes)
    items = fresh_items(native, source, drop_re)
    via = "native"

    # 2) Fall back to Google News only if native has no fresh items.
    gn = []
    if not items:
        gn = parse_feed(session, gnews_url(meta), "https://news.google.com/") or []
        _clean_titles(gn, source, domain, affixes)
        items = fresh_items(gn, source, drop_re)
        via = "google-news"

    # 3) Last resort: if nothing is "fresh" anywhere, show the newest we have
    #    (still date-sorted, junk + dropped commentary removed) rather than an
    #    empty card.
    if not items:
        items = [it for it in (native or gn)
                 if not is_junk_title(it["title"], source)
                 and not is_offtopic(it["title"], it.get("url", ""))
                 and not (drop_re and drop_re.search(it["title"]))]
        via += "/stale"

    if items:
        # The Headlines wall stays geopolitics-only: tracked-but-off-the-wall
        # items (e.g. the World Cup) are excluded from the display CANDIDATES.
        # Which 5 candidates actually display is decided AFTER every outlet is
        # in (rank_and_select) — the ranking needs cross-outlet breadth and the
        # site-wide click counts.
        result["_candidates"] = [it for it in items[:COVERAGE_PER_OUTLET]
                                 if not TRACKED_OFFTOPIC_RE.search(it["title"])]
        # Broader coverage sample for the Trends/Pulse views and the site's
        # "show all headlines" expander, captured from the SAME already-filtered
        # item list — which DOES include the tracked off-the-wall topics.
        result["coverage"] = coverage_items(items)
        # The outlet's own most-read/trending list, when it publishes one — the
        # top-priority ranking signal for rank_and_select.
        result["_mostread"] = fetch_mostread(session, meta)
        mr = f", {len(result['_mostread'])} most-read" if result["_mostread"] else ""
        newest = items[0]["published"][:10] or "undated"
        print(f"  · {source}: {len(result['_candidates'])} candidates "
              f"({len(result['coverage'])} in coverage, {via}, newest {newest}{mr})")
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

    Mutates `regions` in place: adds "snippet", "title_he" and "snippet_he"
    fields to each headline and removes the temporary "description" field.
    Returns {"titles_hash": ...} when everything was produced (so the next run
    can skip unchanged content), or {} on skip/failure — always non-fatal.

    The site offers an EN ↔ HE toggle, so a Hebrew translation of every title
    and snippet is generated alongside the English snippets. That's three
    batched flash-lite calls per run (snippets, Hebrew titles, Hebrew snippets)
    — and none at all when the headlines are unchanged.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  GEMINI_API_KEY not set — skipping snippets", file=sys.stderr)
        _strip_descriptions(regions)
        return {}

    positions, titles, descriptions, langs = [], [], [], []
    for region, outlets in regions.items():
        for o_idx, outlet in enumerate(outlets):
            for h_idx, h in enumerate(outlet.get("headlines", [])):
                positions.append((region, o_idx, h_idx))
                titles.append(h["title"])
                descriptions.append(h.get("description", ""))
                langs.append(outlet.get("lang", "en"))

    if not titles:
        _strip_descriptions(regions)
        return {}

    current_hash = _titles_hash(regions)

    # Cache hit: reuse last run's snippets + Hebrew translations, no API call.
    if existing_output and existing_output.get("titles_hash") == current_hash:
        prev = existing_output.get("regions")
        if prev:
            for i, (region, o_idx, h_idx) in enumerate(positions):
                try:
                    prev_hl = prev[region][o_idx]["headlines"][h_idx]
                except Exception:
                    prev_hl = {}
                hl = regions[region][o_idx]["headlines"][h_idx]
                # Scrub on reuse too, so a previously-stored stale title (e.g.
                # "former president Trump") is corrected even on a cache hit.
                hl["snippet"] = scrub_stale_titles(
                    prev_hl.get("snippet", ""), f"{titles[i]} {descriptions[i]}")
                hl["title_he"] = prev_hl.get("title_he", "")
                hl["snippet_he"] = prev_hl.get("snippet_he", "")
            _strip_descriptions(regions)
            print(f"  Content unchanged — reusing snippets + Hebrew (hash {current_hash})")
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
    print(f"  Generated {n_snips}/{len(titles)} English snippets")

    # ---- Hebrew via Azure Translator (per-item cached; free-tier friendly) ----
    # Azure is a dedicated MT with a 2M-char/month free budget, so — unlike
    # Gemini's ~20 req/day — it can feed the whole wall. The per-item cache means
    # only genuinely new titles/snippets are ever sent, keeping monthly usage to
    # the real turnover of distinct headlines rather than the wall × every run.
    final_snips = [regions[r][oi]["headlines"][hi].get("snippet", "")
                   for (r, oi, hi) in positions]
    he_cache = _load_he_cache()
    # Hebrew-source titles are already Hebrew — never send them to Azure (a he→he
    # round-trip that wastes budget and could mangle the wording). We pass them as
    # blank so translate_he_cached skips them, then use the original title verbatim
    # below. The English snippet still gets a real Hebrew translation.
    titles_for_he = [("" if langs[i] == "he" else titles[i]) for i in range(len(titles))]
    he_titles = translate_he_cached(titles_for_he, he_cache["t"])
    he_snips = translate_he_cached(final_snips, he_cache["s"])
    _save_he_cache(he_cache)

    # Reuse-on-failure: if Azure is down and a headline isn't cached yet, keep
    # any Hebrew the headline already carries (carried-forward outlet) or that
    # the previous run stored, so the toggle doesn't blank while we wait to retry.
    prev_he = {}
    if existing_output:
        for outs in existing_output.get("regions", {}).values():
            for o in outs:
                for h in o.get("headlines", []):
                    if h.get("title_he") or h.get("snippet_he"):
                        prev_he[h.get("title", "")] = h

    n_he = 0
    for i, (region, o_idx, h_idx) in enumerate(positions):
        hl = regions[region][o_idx]["headlines"][h_idx]
        prev_hl = prev_he.get(titles[i], {})
        if langs[i] == "he":
            hl["title_he"] = titles[i]        # Hebrew headline: HE view = the original
        else:
            hl["title_he"] = (he_titles[i]
                              or hl.get("title_he", "") or prev_hl.get("title_he", ""))
        hl["snippet_he"] = (he_snips[i]
                            or hl.get("snippet_he", "") or prev_hl.get("snippet_he", ""))
        if hl["title_he"]:
            n_he += 1
    _strip_descriptions(regions)
    print(f"  Hebrew (Azure): {n_he}/{len(titles)} titles "
          f"({len(he_cache['t'])} titles cached)")

    # Hebrew rides its own persistent per-item cache and is best-effort, so the
    # English-snippet titles_hash gate depends on the snippets alone.
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
            # Keep the ranking-method tag the carried headlines were built with.
            o["rank_method"] = prev.get("rank_method", "latest")
            carried += 1
            print(f"  ~ {o['source']}: carried {len(o['headlines'])} prior headlines "
                  f"(feed down, {age_h:.1f}h old)", file=sys.stderr)
    return carried


def main():
    session = requests.Session()
    output = {"updated": datetime.now(timezone.utc).isoformat(), "regions": {}}
    all_outlets = []
    for region in REGION_ORDER:
        print(f"\n[{region}]")
        outs = [fetch_outlet(session, s) for s in SOURCES[region]]
        output["regions"][region] = outs
        all_outlets.extend(outs)

    # Pick each outlet's displayed top 5 by the best available significance
    # signal (see the module docstring): the outlet's own most-read list, the
    # site's own click counts, cross-outlet breadth, else recency.
    print("\n[Ranking]")
    compute_breadth(all_outlets)
    clicks = fetch_click_counts(session)
    clicks_by_url = {c["url"]: c["n"] for c in clicks
                     if c.get("url") and isinstance(c.get("n"), (int, float))}
    clicks_by_title = {(c.get("source", ""), _norm_title(c.get("title", ""))): c["n"]
                       for c in clicks
                       if c.get("title") and isinstance(c.get("n"), (int, float))}
    for o in all_outlets:
        rank_and_select(session, o, clicks_by_url, clicks_by_title)

    if _gnews_cache:
        resolved = sum(1 for v in _gnews_cache.values() if v)
        print(f"\n[Links] resolved {resolved}/{len(_gnews_cache)} Google News "
              f"redirects to real article URLs "
              f"({GNEWS_RESOLVE_BUDGET - _gnews_budget_left} network lookups)")

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
