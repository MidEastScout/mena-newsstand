#!/usr/bin/env python3
"""Stage 2 — Content pipeline: turn raw headlines into a ranked briefing.

Reads the repo's headlines.json (16 MENA outlets, kept fresh every 30 min by
scripts/fetch_headlines.py), optionally adds a few analyst-grade feeds the
newsstand doesn't carry (Al-Monitor, INSS, Crisis Group, Reuters MENA), then:

  1. flattens every headline into one list,
  2. clusters near-duplicate stories across outlets (token overlap),
  3. scores each story by significance:
       multi-outlet coverage + regional spread + freshness + beat relevance
       (Iran / Lebanon / Hezbollah / Gulf-Israel normalization / strikes /
        Vision 2030 ...) + an analyst-source bonus,
  4. writes agent/data/briefing.json — the input Stage 3 will draft from.

Nothing is published. This only decides *what is worth writing about today*.

Usage (PowerShell):
  py agent\\build_briefing.py                  # full briefing (fetches analyst feeds)
  py agent\\build_briefing.py --no-extra-feeds # newsstand only, no network calls
  py agent\\build_briefing.py --max-age-days 1 --top 15
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from config import (BRIEFING_PATH, HEADLINES_PATH, ensure_utf8_console)

# ---------------------------------------------------------------------------
# Analyst feeds the newsstand doesn't carry. These are lower-volume, higher-
# signal sources for this beat. We pull them via Google News site-search (the
# same trick fetch_headlines.py uses), which is far more reliable from a home
# IP than the outlets' own often-blocked RSS. Fetched best-effort: if a feed
# is empty or errors, we just skip it.
# ---------------------------------------------------------------------------
ANALYST_FEEDS = [
    {"source": "Al-Monitor", "site": "al-monitor.com", "lang": "en"},
    {"source": "INSS", "site": "inss.org.il", "lang": "en"},
    {"source": "Crisis Group", "site": "crisisgroup.org", "lang": "en"},
    {"source": "Reuters Middle East", "site": "reuters.com/world/middle-east", "lang": "en"},
]
ANALYST_SOURCES = {f["source"] for f in ANALYST_FEEDS}
ANALYST_REGION = "Analysis"

# Beat relevance: matched case-insensitively against each story's title.
# Weights reflect how central a topic is to the author's voice profile
# (Israel-Iran-Lebanon-US arena, military/diplomatic, "who benefits").
BEAT_KEYWORDS = {
    # high
    "iran": 3.0, "tehran": 3.0, "irgc": 3.0, "hezbollah": 3.0, "nasrallah": 3.0,
    "lebanon": 3.0, "beirut": 2.5, "nuclear": 3.0, "enrichment": 2.5,
    "normalization": 3.0, "normalisation": 3.0, "abraham accords": 3.0,
    "idf": 2.5, "strike": 2.5, "airstrike": 2.5, "missile": 2.5, "drone": 2.5,
    "israel": 2.0, "israeli": 2.0, "gaza": 2.0,
    # medium
    "saudi": 2.0, "riyadh": 2.0, "vision 2030": 2.5, "mbs": 2.0,
    "uae": 1.5, "emirati": 1.5, "qatar": 1.5, "doha": 1.5, "oman": 2.0,
    "muscat": 2.0, "houthi": 2.0, "yemen": 1.5, "syria": 2.0, "iraq": 1.5,
    "pmf": 2.0, "militia": 1.5, "proxy": 2.0, "opec": 1.5,
    # context / framing
    "ceasefire": 1.5, "truce": 1.5, "sanctions": 1.5, "talks": 1.0,
    "mediation": 2.0, "back-channel": 2.5, "normalize": 2.5,
    "washington": 1.0, "diplomacy": 1.0, "escalation": 1.5,
}

# Israeli *domestic* politics — excluded by default. The author covers the
# Israel-Iran-Lebanon-US arena (strikes, the nuclear file, normalization), not
# Knesset coalition drama, the judicial overhaul, or domestic elections.
# These terms are inherently about internal Israeli politics.
ISRAELI_POLITICS_TERMS = {
    "knesset", "coalition", "judicial reform", "judicial overhaul",
    "judicial", "high court of justice", "attorney general", "likud",
    "haredi", "ultra-orthodox", "conscription law", "draft law", "draft bill",
    "no-confidence", "no confidence", "opposition leader", "netanyahu trial",
    "netanyahu's trial", "corruption trial", "shin bet chief", "lapid",
    "gantz", "bennett", "ben gvir", "ben-gvir", "smotrich", "herzog",
    "coalition crisis", "government collapse", "snap election", "primaries",
}
# If a story carries any of these hard regional/security/diplomatic terms, it
# stays even when a politics word also appears ("Netanyahu orders Lebanon
# strike" is beat news, not domestic politics). Deliberately excludes the
# ambiguous "israel"/"israeli"/"gaza".
PROTECT_TERMS = {
    "iran", "tehran", "irgc", "hezbollah", "nasrallah", "lebanon", "beirut",
    "nuclear", "enrichment", "normalization", "normalisation", "abraham accords",
    "strike", "airstrike", "missile", "drone", "saudi", "riyadh", "uae",
    "qatar", "oman", "houthi", "yemen", "syria", "iraq", "hormuz", "hamas",
    "ceasefire", "sanctions", "mediation",
}

# Tokens ignored when comparing titles for duplicates.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "as", "at", "by", "from", "is", "are", "was", "were", "be", "after",
    "over", "amid", "say", "says", "said", "report", "reports", "new",
    "latest", "news", "live", "update", "updates", "video", "watch",
    "this", "that", "its", "it", "his", "her", "their", "they", "he", "she",
}

# Trailing " - Outlet" / " | Outlet" that Google News appends to titles.
SUFFIX_RE = re.compile(r"\s*[-|–—]\s*[^-|–—]{1,40}$")
WORD_RE = re.compile(r"[\wא-ת؀-ۿ]+", re.UNICODE)

REQUEST_TIMEOUT = 20
GNEWS_LOCALE = {"en": ("en-US", "US", "US:en"), "ar": ("ar", "EG", "EG:ar")}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# Scoring weights.
W_OUTLET = 2.5      # per extra outlet covering the story
W_REGION = 1.5      # per extra region it spans
W_RECENCY = 4.0     # multiplied by a 0..1 freshness score
W_BEAT = 1.0        # multiplied by summed keyword weights (capped)
W_ANALYST = 3.0     # bonus if a dedicated analyst source covers it
BEAT_CAP = 8.0      # don't let a keyword-stuffed title run away


def clean_title(title: str) -> str:
    title = title.strip()
    stripped = SUFFIX_RE.sub("", title)
    # Only drop the suffix if something substantial remains.
    return stripped if len(stripped) >= 15 else title


def tokens(title: str) -> set:
    return {w for w in (t.lower() for t in WORD_RE.findall(title))
            if w not in STOPWORDS and len(w) > 2}


def parse_iso(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def load_newsstand(path: Path):
    """Flatten headlines.json into a list of article dicts."""
    if not path.is_file():
        sys.exit(f"ERROR: {path} not found. It is produced by "
                 "scripts/fetch_headlines.py (and committed by GitHub Actions).")
    data = json.loads(path.read_text(encoding="utf-8"))
    items = []
    for region, outlets in data.get("regions", {}).items():
        for outlet in outlets:
            for h in outlet.get("headlines", []):
                items.append({
                    "title": clean_title(h["title"]),
                    "url": h["url"],
                    "source": outlet["source"],
                    "region": region,
                    "lang": outlet.get("lang", "en"),
                    "published": h.get("published", ""),
                })
    return items, data.get("updated", "")


def fetch_analyst_feeds():
    """Best-effort pull of analyst sources via Google News site-search."""
    try:
        import feedparser
        import requests
    except ImportError:
        print("NOTE: feedparser/requests not installed; skipping analyst feeds. "
              "Install with: pip install -r agent\\requirements.txt", file=sys.stderr)
        return []

    session = requests.Session()
    items = []
    for feed in ANALYST_FEEDS:
        hl, gl, ceid = GNEWS_LOCALE.get(feed["lang"], GNEWS_LOCALE["en"])
        query = quote_plus(f"site:{feed['site']} when:7d")
        url = f"https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"
        try:
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"  ! {feed['source']}: {exc}", file=sys.stderr)
            continue
        n = 0
        for e in parsed.entries[:8]:
            title = (e.get("title") or "").strip()
            link = e.get("link") or e.get("id", "")
            if not title or not link:
                continue
            dt = None
            for attr in ("published_parsed", "updated_parsed"):
                t = getattr(e, attr, None)
                if t:
                    dt = datetime(*t[:6], tzinfo=timezone.utc)
                    break
            items.append({
                "title": clean_title(title), "url": link,
                "source": feed["source"], "region": ANALYST_REGION,
                "lang": feed["lang"],
                "published": dt.isoformat() if dt else "",
            })
            n += 1
        print(f"  + {feed['source']}: {n} items")
    return items


def filter_recent(items, max_age_days: int):
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    kept = []
    for it in items:
        dt = parse_iso(it["published"])
        # Keep undated analyst pieces (think-tanks often omit dates); drop
        # undated newsstand items, which are usually evergreen junk.
        if dt is None:
            if it["source"] in ANALYST_SOURCES:
                kept.append(it)
            continue
        if dt >= cutoff:
            it["_dt"] = dt
            kept.append(it)
    return kept


def cluster(items, threshold: float):
    """Greedy single-pass clustering on title token overlap (Jaccard)."""
    clusters = []  # each: {"tokens": set, "items": [..]}
    for it in items:
        tok = tokens(it["title"])
        if not tok:
            continue
        best, best_sim = None, 0.0
        for c in clusters:
            inter = tok & c["tokens"]
            if len(inter) < 2:
                continue
            sim = len(inter) / len(tok | c["tokens"])
            if sim > best_sim:
                best, best_sim = c, sim
        if best and best_sim >= threshold:
            best["items"].append(it)
            best["tokens"] |= tok
        else:
            clusters.append({"tokens": set(tok), "items": [it]})
    return clusters


def freshness(dts) -> float:
    """0..1 score: 1.0 = minutes ago, decaying over 48h."""
    dated = [d for d in dts if d]
    if not dated:
        return 0.3  # undated analyst piece: mild, non-zero
    age_h = (datetime.now(timezone.utc) - max(dated)).total_seconds() / 3600
    return max(0.0, 1.0 - age_h / 48.0)


def beat_match(title: str):
    low = title.lower()
    hits, score = [], 0.0
    for kw, weight in BEAT_KEYWORDS.items():
        if kw in low:
            hits.append(kw)
            score += weight
    return hits, min(score, BEAT_CAP)


def is_israeli_domestic_politics(title: str) -> bool:
    """True for internal-politics stories with no regional/security angle."""
    low = title.lower()
    if not any(term in low for term in ISRAELI_POLITICS_TERMS):
        return False
    return not any(term in low for term in PROTECT_TERMS)


def humanize_age(dt) -> str:
    if not dt:
        return "undated"
    mins = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if mins < 2:
        return "just now"
    if mins < 90:
        return f"{int(mins)}m ago"
    if mins < 60 * 36:
        return f"{int(mins / 60)}h ago"
    return f"{int(mins / 1440)}d ago"


def score_cluster(c) -> dict:
    items = c["items"]
    outlets = sorted({it["source"] for it in items})
    regions = sorted({it["region"] for it in items})
    langs = sorted({it["lang"] for it in items})
    dts = [it.get("_dt") for it in items]
    # Representative = the dated article from the most "central" (longest) title;
    # fall back to the first.
    rep = max(items, key=lambda it: (it.get("_dt") is not None, len(it["title"])))
    has_analyst = any(it["source"] in ANALYST_SOURCES for it in items)

    fresh = freshness(dts)
    tags, beat = beat_match(rep["title"])
    score = (W_OUTLET * (len(outlets) - 1)
             + W_REGION * (len(regions) - 1)
             + W_RECENCY * fresh
             + W_BEAT * beat
             + (W_ANALYST if has_analyst else 0.0))
    # Off-beat stories (no matched keyword, no analyst source) are noise for
    # this author — sports, generic world news. Halve them so they can't
    # outrank genuine on-beat reporting on outlet-count alone.
    if not tags and not has_analyst:
        score *= 0.5

    newest_dt = max([d for d in dts if d], default=None)
    why = []
    if len(outlets) > 1:
        why.append(f"covered by {len(outlets)} outlets")
    if len(regions) > 1:
        why.append(f"across {len(regions)} regions")
    if tags:
        why.append("matches your beat (" + ", ".join(tags[:4]) + ")")
    if has_analyst:
        why.append("analyst coverage")
    if fresh > 0.85:
        why.append("breaking in the last few hours")
    if not why:
        why.append("single-outlet item on your beat" if tags else "single-outlet item")

    return {
        "headline": rep["title"],
        "score": round(score, 1),
        "outlet_count": len(outlets),
        "outlets": outlets,
        "regions": regions,
        "languages": langs,
        "newest": newest_dt.isoformat() if newest_dt else "",
        "beat_tags": tags,
        "has_analyst_source": has_analyst,
        "why": "; ".join(why).capitalize() + ".",
        "articles": sorted(
            [{"source": it["source"], "title": it["title"], "url": it["url"],
              "published": it["published"]} for it in items],
            key=lambda a: a["published"], reverse=True),
        "_newest_dt": newest_dt,
    }


def main() -> None:
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="Build a ranked MENA briefing from headlines.json.")
    ap.add_argument("--headlines", default=str(HEADLINES_PATH),
                    help="Path to headlines.json (default: repo root)")
    ap.add_argument("--out", default=str(BRIEFING_PATH))
    ap.add_argument("--max-age-days", type=int, default=2,
                    help="Drop newsstand stories older than this (default: 2)")
    ap.add_argument("--top", type=int, default=20,
                    help="How many ranked stories to print to the console")
    ap.add_argument("--similarity", type=float, default=0.45,
                    help="Title overlap (0-1) needed to merge two stories")
    ap.add_argument("--no-extra-feeds", action="store_true",
                    help="Skip the analyst feeds (no network calls)")
    ap.add_argument("--include-israeli-politics", action="store_true",
                    help="Keep Israeli domestic-politics stories (excluded by default)")
    args = ap.parse_args()

    newsstand, updated = load_newsstand(Path(args.headlines))
    print(f"Newsstand      : {len(newsstand)} headlines (updated {updated[:16] or '?'})")

    analyst = []
    if not args.no_extra_feeds:
        print("Analyst feeds  :")
        analyst = fetch_analyst_feeds()

    items = filter_recent(newsstand + analyst, args.max_age_days)
    print(f"Within {args.max_age_days}d window: {len(items)} articles "
          f"({len(newsstand)+len(analyst)-len(items)} dropped as old/undated)")

    if not args.include_israeli_politics:
        before = len(items)
        items = [it for it in items if not is_israeli_domestic_politics(it["title"])]
        dropped = before - len(items)
        if dropped:
            print(f"Israeli politics: {dropped} domestic-politics stories excluded "
                  "(use --include-israeli-politics to keep them)")

    clusters = cluster(items, args.similarity)
    stories = sorted((score_cluster(c) for c in clusters),
                     key=lambda s: s["score"], reverse=True)

    print(f"Stories        : {len(stories)} after clustering\n")
    print(f"=== TOP {min(args.top, len(stories))} BY SIGNIFICANCE ===")
    for i, s in enumerate(stories[:args.top], 1):
        age = humanize_age(s["_newest_dt"])
        spread = f"{s['outlet_count']} outlet{'s' if s['outlet_count'] != 1 else ''}"
        tags = (" · tags: " + ", ".join(s["beat_tags"][:4])) if s["beat_tags"] else ""
        print(f"{i:2}. [{s['score']:5.1f}] {s['headline']}")
        print(f"     {spread} · {', '.join(s['regions'])}{tags} · {age}")

    payload = {
        "schema": "mena-agent/briefing@1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "newsstand_updated": updated,
        "window_days": args.max_age_days,
        "sources_used": {
            "newsstand_articles": len(newsstand),
            "analyst_articles": len(analyst),
            "analyst_feeds": sorted(ANALYST_SOURCES) if not args.no_extra_feeds else [],
        },
        "story_count": len(stories),
        "stories": [{k: v for k, v in s.items() if not k.startswith("_")}
                    for s in stories],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\nWrote          : {out_path}")
    print("Review the ranking. Stage 3 will draft posts from the top stories "
          "using your style_profile.json.")


if __name__ == "__main__":
    main()
