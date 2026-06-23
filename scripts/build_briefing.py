#!/usr/bin/env python3
"""Synthesises a global world-affairs briefing and writes briefing.json.

Run every 6 hours by the workflow (when DO_BRIEFING=true). The briefing
covers the full global picture — great-power politics, wars, the economy —
and treats the Middle East as one part of the world order, not the focus.

Output (briefing.json):
  { "updated": "<ISO>", "html": "<p>...</p>…", "model": "<model-id>" }

send_email.py reads this file rather than calling the API a second time.
index.html loads it directly to show the in-page world briefing.
"""
import html as html_lib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

REGION_ORDER = ["Pan-Arab", "Levant", "Gulf", "Israel", "Iran"]
# Both synthesis and translation use flash so they share a separate daily quota
# from the flash-lite quota used by headline snippets/translations.
GEMINI_MODEL = "gemini-2.5-flash"
TRANSLATE_MODEL = "gemini-2.5-flash"
BRIEFING_WORDS = "350-500"

# Same languages offered for the headlines toggle (en is the source).
LANG_TARGETS = {
    "he": "Hebrew",
    "ar": "Arabic",
}

# Major international feeds that ground the briefing in today's world news.
WORLD_FEEDS = [
    ("BBC World",        "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("The Guardian",     "https://www.theguardian.com/world/rss"),
    ("NPR World",        "https://feeds.npr.org/1004/rss.xml"),
    ("France 24",        "https://www.france24.com/en/rss"),
    ("Deutsche Welle",   "https://rss.dw.com/rdf/rss-en-world"),
    ("Reuters",          "https://feeds.reuters.com/reuters/worldNews"),
    ("Google News Top",
     "https://news.google.com/rss/headlines/section/topic/WORLD"
     "?hl=en-US&gl=US&ceid=US:en"),
]
WORLD_PER_FEED = 8

OUT_PATH    = Path(__file__).parent.parent / "briefing.json"
HL_PATH     = Path(__file__).parent.parent / "headlines.json"


def _para_to_html(text: str) -> str:
    """Convert plain-text paragraphs with **bold** lead-ins to safe HTML."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out = []
    for p in paras:
        safe = html_lib.escape(p)
        safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
        safe = safe.replace("\n", "<br>")
        out.append(f"<p>{safe}</p>")
    return "\n".join(out)


def text_to_cards(text: str) -> list:
    """Split the plain-text briefing into topic cards for the swipe carousel.

    Each paragraph becomes one card. The leading **bold lead-in** (e.g.
    '**Russia-Ukraine:**') becomes the card heading; the rest is the summary.
    The opening lead paragraph, which has no lead-in, becomes 'The Big Picture'.
    """
    cards = []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for i, p in enumerate(paras):
        m = re.match(r"\*\*(.+?)\*\*[:：]?\s*", p)
        if m:
            heading = m.group(1).strip().rstrip(":：").strip()
            summary = p[m.end():].strip()
        else:
            heading = "The Big Picture" if i == 0 else ""
            summary = p
        # Flatten any remaining inline bold and whitespace.
        summary = re.sub(r"\*\*(.+?)\*\*", r"\1", summary)
        summary = re.sub(r"\s+", " ", summary).strip()
        if not summary:
            continue
        if not heading:
            heading = " ".join(summary.split()[:4]).rstrip(".,:;")
        cards.append({"heading": heading, "summary": summary})
    return cards


def fetch_world_lines() -> list[str]:
    """Pull today's top stories from major international RSS feeds."""
    try:
        import requests
        from fetch_headlines import fresh_items, parse_feed
    except Exception as exc:
        print(f"World-feed helpers unavailable ({exc})", file=sys.stderr)
        return []

    session = requests.Session()
    lines, seen = [], set()
    for name, url in WORLD_FEEDS:
        items = parse_feed(session, url, None) or []
        for it in (fresh_items(items, name) or items[:WORLD_PER_FEED])[:WORLD_PER_FEED]:
            title = (it.get("title") or "").strip()
            key = title.lower()
            if not title or key in seen:
                continue
            seen.add(key)
            entry = f"[World · {name}] {title}"
            desc = (it.get("description") or "").strip()
            if desc:
                entry += f" — {desc}"
            lines.append(entry)
    print(f"Collected {len(lines)} world headlines from {len(WORLD_FEEDS)} feeds")
    return lines


def fetch_mena_lines() -> list[str]:
    """Pull today's MENA headlines + snippets from headlines.json."""
    try:
        data = json.loads(HL_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    lines = []
    for region in REGION_ORDER:
        for outlet in data.get("regions", {}).get(region, []):
            for h in outlet.get("headlines", []):
                title = (h.get("title") or "").strip()
                if not title:
                    continue
                entry = f"[{region} · {outlet.get('source', '')}] {title}"
                snip = (h.get("snippet") or "").strip()
                if snip:
                    entry += f" — {snip}"
                lines.append(entry)
    return lines


def build_prompt(world: list[str], mena: list[str]) -> str:
    return (
        "You are the editor of a daily world-affairs intelligence briefing. "
        "Below are today's headlines from major international outlets, followed "
        "by Middle East & North Africa headlines.\n\n"
        "Write a cohesive GLOBAL briefing for a well-informed reader:\n"
        "- Lead with the single biggest development shaping the world order today, "
        "in 1-2 sentences.\n"
        "- Then 5-7 short paragraphs covering the day's most consequential global "
        "stories: great-power politics (US, China, Russia/Ukraine, Europe), wars "
        "and security, the global economy and markets, and other major world events.\n"
        "- Treat the Middle East as ONE part of the global picture — include MENA "
        "stories only when they rank among the day's most consequential worldwide.\n"
        "- Synthesize across outlets; note where framing diverges when relevant.\n"
        "- Stay factual and grounded STRICTLY in the material provided; never "
        "invent facts, figures, or attributions that are not in the headlines.\n"
        "- Begin each paragraph with a short bold lead-in in **double asterisks** "
        "(e.g. **Russia-Ukraine:**).\n"
        f"- Aim for {BRIEFING_WORDS} words. Return plain text only — no headings, "
        "no bullet lists.\n\n"
        "WORLD HEADLINES:\n" + "\n".join(world or ["(none available)"]) +
        "\n\nMIDDLE EAST & NORTH AFRICA HEADLINES:\n" +
        "\n".join(mena or ["(none available)"])
    )


def generate_with_retry(client, model: str, prompt: str, label: str,
                        attempts: int = 4) -> str:
    """Call Gemini with exponential backoff on transient errors (503 UNAVAILABLE,
    429 RESOURCE_EXHAUSTED, 500). Returns the response text, or '' if every
    attempt fails. These models are shared free-tier endpoints that routinely
    return brief 503 'high demand' blips, so a single attempt drops translations
    far too often — retrying recovers almost all of them."""
    for attempt in range(attempts):
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            text = (resp.text or "").strip()
            if text:
                return text
            print(f"  [{label}] empty response (attempt {attempt + 1})", file=sys.stderr)
        except Exception as exc:
            msg = str(exc)
            transient = any(code in msg for code in
                            ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500"))
            if transient and attempt < attempts - 1:
                wait = 5 * (2 ** attempt)   # 5s, 10s, 20s
                print(f"  [{label}] transient error — retrying in {wait}s "
                      f"({msg[:80]})", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  [{label}] failed: {exc}", file=sys.stderr)
            return ""
        if attempt < attempts - 1:
            time.sleep(5 * (2 ** attempt))
    return ""


def translate_text(client, text: str, lang_name: str) -> str:
    """Translate the plain-text briefing into lang_name, preserving paragraph
    breaks and the **bold** lead-in markers. Returns '' on failure."""
    prompt = (
        f"Translate the following news briefing into {lang_name}. Preserve the "
        "paragraph structure exactly (keep the blank lines between paragraphs), "
        "and keep the **double-asterisk bold markers** around the same lead-in "
        "phrases. Translate naturally and journalistically. Return ONLY the "
        "translated text — no notes, no preamble.\n\n" + text
    )
    return generate_with_retry(client, TRANSLATE_MODEL, prompt, lang_name)


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY not set — skipping briefing", file=sys.stderr)
        sys.exit(0)

    try:
        from google import genai
    except ImportError:
        print("google-genai not installed — skipping briefing", file=sys.stderr)
        sys.exit(0)

    world = fetch_world_lines()
    mena  = fetch_mena_lines()

    if not world and not mena:
        print("No headlines available — skipping briefing", file=sys.stderr)
        sys.exit(0)

    prompt = build_prompt(world, mena)

    client = genai.Client(api_key=api_key)
    text   = generate_with_retry(client, GEMINI_MODEL, prompt, "synthesis")

    if not text:
        print("Empty response from Gemini — skipping write", file=sys.stderr)
        sys.exit(0)

    # English first, then translate into each supported language. Retries above
    # absorb transient 503/429 blips. If a translation still fails AND the new
    # English text is unchanged from the previous run, reuse the prior
    # translation so the language doesn't silently revert to English.
    try:
        prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    prev_by_lang = prev.get("html_by_lang") or {}
    prev_en = (prev_by_lang.get("en") or prev.get("html") or "").strip()

    new_en = _para_to_html(text)
    html_by_lang = {"en": new_en}
    for code, name in LANG_TARGETS.items():
        translated = translate_text(client, text, name)
        if translated:
            html_by_lang[code] = _para_to_html(translated)
            print(f"  [{code}] translated briefing")
        elif new_en == prev_en and prev_by_lang.get(code):
            html_by_lang[code] = prev_by_lang[code]
            print(f"  [{code}] kept previous translation (refresh failed)")

    updated = datetime.now(timezone.utc).isoformat()
    OUT_PATH.write_text(
        json.dumps(
            {
                "updated": updated,
                "model": GEMINI_MODEL,
                "html": html_by_lang["en"],   # back-compat: English
                "html_by_lang": html_by_lang,
                "cards": text_to_cards(text),  # swipe-carousel topic cards (EN)
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote briefing.json ({len(text.split())} words, "
          f"{len(html_by_lang)} languages)")


if __name__ == "__main__":
    main()
