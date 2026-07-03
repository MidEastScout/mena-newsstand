#!/usr/bin/env python3
"""Builds pulse.json — a media word-cloud from the current headlines.

Runs every refresh (cheap, no API). Counts how many headlines mention each
meaningful term across all titles + snippets, filters stopwords, and records
the matching headlines so the site can show "what's being talked about" and let
a reader click a term to see every headline that mentions it.

Output (pulse.json):
  { "updated": "<ISO>",
    "total_headlines": <int>,
    "terms": [ { "term": "gaza", "term_he": "עזה", "count": 12,
                 "headlines": [ {title,title_he,url,source,region}, … ] }, … ] }

term_he / title_he feed the site's EN ↔ HE toggle. title_he is copied straight
from headlines.json (no API); term_he comes from a persistent, ever-growing
dictionary (state/pulse_terms_he.json) so only never-seen-before terms cost a
Gemini call — the API cost converges to zero as the vocabulary stabilises.

This is the frequency-only "Pulse"; sentiment / over-time trends can layer on
later using the front-page archive infrastructure.
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT     = Path(__file__).parent.parent
HL_PATH  = ROOT / "headlines.json"
COV_PATH = ROOT / "coverage.json"
OUT_PATH = ROOT / "pulse.json"
HE_PATH  = ROOT / "state" / "pulse_terms_he.json"

# Same high-free-quota model the headline snippets use; one tiny batched call
# per run at most (only for cloud terms not yet in the cached dictionary).
GEMINI_MODEL = "gemini-2.5-flash-lite"

# Topics kept OFF the Headlines wall but still counted here, so the Pulse reflects
# what media is actually covering. These items don't appear in headlines.json, so
# they're pulled from the broad coverage sample (coverage.json). Mirrors
# TRACKED_OFFTOPIC_RE in fetch_headlines.py. Currently: the football World Cup.
TRACKED_RE = re.compile(r"\bworld\s*cup\b|كأس العالم|المونديال", re.I)

MIN_LEN          = 4    # ignore very short tokens
MAX_TERMS        = 48   # cap the cloud size
MAX_HL_PER_TERM  = 20   # cap stored headlines per term
CAP_RATIO        = 0.55 # show a term capitalised if it's upper-cased this often

WORD_RE = re.compile(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*")

# Curated multi-word terms common in MENA coverage. Each is detected as a whole
# phrase (case-insensitive) and counted as ONE term; its component words are
# then skipped so they aren't double-counted. Display form is exactly as written
# here, so proper nouns stay capitalised and concepts stay lower-case.
PHRASES = [
    "Saudi Arabia", "United Arab Emirates", "West Bank", "Gaza Strip",
    "Strait of Hormuz", "Red Sea", "Security Council", "Muslim Brotherhood",
    "Islamic Jihad", "Revolutionary Guard", "Arab League", "Gulf states",
    "nuclear file", "nuclear deal", "nuclear talks", "nuclear program",
    "drone strike", "drone strikes", "air strike", "air strikes",
    "ballistic missile", "ballistic missiles", "prisoner exchange",
    "peace deal", "two-state", "war crimes", "oil prices", "aid convoy",
    # Tracked off-the-wall topic — kept as one phrase so it isn't lost to the
    # "world"/"cup" stopwords (see TRACKED_RE / coverage merge in main()).
    "World Cup",
]
PHRASE_RES = sorted(
    ((re.compile(r"\b" + re.escape(p) + r"\b", re.I), p) for p in PHRASES),
    key=lambda pr: -len(pr[1]),
)
PHRASE_KEYS = {p.lower() for p in PHRASES}

# Curated lower-case CONCEPTS — the geopolitical vocabulary worth surfacing even
# though it isn't a proper noun. Everything that is neither a proper noun, a
# phrase, nor a listed concept is dropped, which keeps the cloud to places,
# people and topics (like a real media word cloud) instead of filler verbs.
CONCEPTS = {
    "ceasefire", "truce", "armistice", "normalization", "normalisation",
    "sanctions", "embargo", "talks", "negotiations", "negotiation", "summit",
    "diplomacy", "agreement", "accord", "treaty", "mediation", "rapprochement",
    "genocide", "occupation", "blockade", "siege", "annexation", "settlement",
    "settlements", "strike", "strikes", "airstrike", "airstrikes", "shelling",
    "bombardment", "bombing", "offensive", "invasion", "incursion", "escalation",
    "withdrawal", "hostages", "hostage", "prisoners", "captives", "detainees",
    "refugees", "displacement", "famine", "humanitarian", "reconstruction",
    "militants", "insurgents", "drones", "drone", "missile", "missiles",
    "rockets", "nuclear", "enrichment", "uranium", "proliferation", "warheads",
    "oil", "gas", "energy", "pipeline", "tanker", "protests", "uprising",
    "election", "elections", "referendum", "coup", "war", "conflict", "crisis",
    "attack", "assassination", "abduction", "raid", "clashes", "militia",
    "deployment", "mobilization", "insurgency", "terrorism", "extremism",
    "statehood", "sovereignty", "partition", "two-state", "aid", "truce",
}

POSS_RE = re.compile(r"[’']s$")          # trailing possessive, e.g. Iran's

# Common English words + news filler that would otherwise dominate the cloud.
STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had",
    "her", "was", "one", "our", "out", "day", "get", "has", "him", "his", "how",
    "may", "new", "now", "old", "see", "two", "way", "who", "boy", "did", "its",
    "let", "put", "say", "she", "too", "use", "that", "this", "with", "from",
    "they", "will", "have", "been", "were", "said", "says", "what", "when",
    "where", "which", "their", "there", "would", "could", "should", "about",
    "after", "again", "amid", "among", "over", "into", "more", "than", "then",
    "them", "these", "those", "such", "some", "only", "also", "just", "very",
    "most", "much", "many", "make", "made", "made", "take", "takes", "took",
    "report", "reports", "reported", "according", "news", "latest", "update",
    "updates", "live", "video", "watch", "read", "story", "stories", "today",
    "year", "years", "week", "month", "time", "first", "last", "next", "back",
    "still", "amid", "amids", "while", "during", "before", "between", "against",
    "because", "around", "another", "people", "official", "officials",
    "minister", "government", "country", "countries", "world", "national",
    "international", "former", "leader", "leaders", "president", "say", "set",
    "calls", "call", "called", "warns", "warn", "warned", "plan", "plans",
    "talk", "deal", "move", "amid", "sees", "could", "amid", "per",
    "via", "amid", "down", "near", "off", "out", "way", "top", "big", "key",
    "amid", "his", "she", "her", "its", "won", "win", "wins", "gets", "amid",
    # generic verbs / adjectives / fillers that leaked into the cloud
    "since", "through", "throughout", "under", "above", "below", "onto", "upon",
    "within", "without", "across", "along", "despite", "regarding", "concerning",
    "following", "amidst", "toward", "towards", "however", "meanwhile", "amongst",
    "team", "teams", "change", "changed", "changes", "decade", "decades",
    "least", "round", "rounds", "part", "parts", "face", "faces", "faced",
    "reach", "reached", "reaches", "state", "states", "stated", "stating",
    "conclude", "concludes", "concluded", "reveal", "reveals", "revealed",
    "detain", "detains", "detained", "challenge", "challenges", "challenged",
    "target", "targets", "targeted", "targeting", "follow", "follows", "shift",
    "begin", "begins", "began", "continue", "continues", "continued", "remain",
    "remains", "remained", "accuse", "accuses", "accused", "claim", "claims",
    "claimed", "urge", "urges", "urged", "vows", "vowed", "seeks", "seeking",
    "amid", "billion", "million", "thousand", "hundred", "dozens", "several",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday", "early", "late",
    "major", "recent", "current", "amid", "into", "after", "amid", "must",
    "analysis", "opinion", "editorial", "exclusive", "breaking", "comment",
    "foreign", "domestic", "local", "regional", "global", "central", "general",
    "killed", "kills", "kill", "dead", "death", "deaths", "wounded", "injured",
    "release", "released", "releases", "frozen", "freeze", "refers", "refer",
    "assets", "funds", "fund", "funding", "fire", "fires", "south", "north",
    "east", "west", "forces", "force", "match", "announce", "announced",
    "announces", "save", "saves", "saved", "article", "articles", "discuss",
    "discusses", "discussed", "impact", "impacts", "party", "parties", "school",
    "schools", "children", "child", "amid", "report", "case", "cases", "group",
    "groups", "meeting", "meet", "meets", "visit", "visits", "hold", "holds",
    # sports / entertainment proper nouns that aren't the geopolitical story
    "messi", "mbappe", "mbappé", "ronaldo", "lionel", "barcelona", "madrid",
    "league", "cup", "match", "goal", "goals", "striker", "coach", "player",
    "players", "club", "clubs", "tournament", "final", "champions",
    # generic institutions / honorifics (specific names like Knesset stay)
    "council", "parliament", "speaker", "congress", "senate", "cabinet",
    "committee", "assembly", "sultan", "king", "queen", "prince", "princess",
    "crown", "royal", "sheikh", "emir", "ministry", "agency",
    "prime", "deputy", "chief", "spokesman", "spokeswoman", "spokesperson",
    "envoy", "ambassador", "adviser", "advisor", "premier", "lawmaker",
    # publisher artifacts that survive as single concatenated tokens
    "farsnews", "almayadeen", "aljazeera", "arabiya", "wam", "petra", "mena",
}


def load_json(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_headlines() -> dict:
    return load_json(HL_PATH)


def hebrew_terms(terms: list) -> dict:
    """Return {English display term -> Hebrew} for the cloud's EN ↔ HE toggle.

    Backed by a persistent dictionary (state/pulse_terms_he.json) that only
    grows: each run translates just the terms not already cached — one small
    batched flash-lite call — so once the recurring vocabulary (Iran, Gaza,
    ceasefire…) is covered, most runs make no API call at all. Entirely
    non-fatal: without a key / on any failure, whatever is cached is used and
    the site falls back to English for the rest.
    """
    cache = load_json(HE_PATH) or {}
    missing = [t for t in terms if t not in cache]
    if not missing:
        return cache
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return cache
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        prompt = (
            "Translate each news-topic term in the JSON array below into Hebrew, "
            "as the term would appear in Israeli news coverage (e.g. Gaza → עזה, "
            "ceasefire → הפסקת אש, IDF → צה\"ל). Use the standard Hebrew spelling "
            "for proper nouns; transliterate names with no standard Hebrew form. "
            f"Return ONLY a JSON array of exactly {len(missing)} strings, same "
            "order.\n\n"
            f"Items:\n{json.dumps(missing, ensure_ascii=False)}"
        )
        resp = client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=list[str]))
        arr = json.loads((resp.text or "").strip())
        if isinstance(arr, list) and len(arr) == len(missing):
            for term, he in zip(missing, arr):
                if isinstance(he, str) and he.strip():
                    cache[term] = he.strip()
            HE_PATH.parent.mkdir(parents=True, exist_ok=True)
            HE_PATH.write_text(
                json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True),
                encoding="utf-8")
            print(f"  Hebrew terms: +{len(missing)} translated "
                  f"({len(cache)} cached total)")
        else:
            print(f"  Hebrew terms: unexpected response shape — skipped",
                  file=sys.stderr)
    except Exception as exc:
        print(f"  Hebrew terms skipped ({exc})", file=sys.stderr)
    return cache


def tokens(text: str):
    """Lowercased word tokens, keeping internal apostrophes/hyphens."""
    return [m.group(0).lower() for m in WORD_RE.finditer(text)]


# Generic outlet-name words that leak into titles/snippets but aren't topics.
EXTRA_OUTLET_WORDS = {
    "english", "daily", "press", "gazette", "herald", "online", "weekly",
    "tribune", "journal", "post", "wire", "agency", "media", "network",
}


def main():
    data = load_headlines()
    regions = data.get("regions", {})
    # Broad coverage sample — used to pull in tracked off-the-wall topics (World
    # Cup) that are deliberately absent from headlines.json (the displayed wall).
    cov_regions = load_json(COV_PATH).get("regions", {})

    # Build a dynamic stopword set from the outlet names so words like "times",
    # "arabiya" or "akhbar" don't dominate the cloud as publisher artifacts.
    stop = set(STOPWORDS) | EXTRA_OUTLET_WORDS
    for src in (regions, cov_regions):
        for outlets in src.values():
            for outlet in outlets:
                for w in tokens(outlet.get("source", "")):
                    # Add the whole token and its hyphen/apostrophe parts, so both
                    # "al-akhbar" and "akhbar" are treated as publisher artifacts.
                    for part in [w, *re.split(r"['’\-]", w)]:
                        if len(part) >= MIN_LEN:
                            stop.add(part)

    counts   = Counter()                 # term-key -> # headlines mentioning it
    term_hls = defaultdict(list)         # term-key -> [headline dicts]
    casing   = defaultdict(Counter)      # term-key -> Counter(original spelling)
    total    = 0
    seen_urls = set()                    # dedupe wall vs. coverage by article URL

    def record(key, display, entry, seen):
        if key in seen:
            return
        seen.add(key)
        counts[key] += 1
        casing[key][display] += 1
        if len(term_hls[key]) < MAX_HL_PER_TERM:
            term_hls[key].append(entry)

    def count_entry(title, snippet, url, source, region, title_he=""):
        nonlocal total
        title = (title or "").strip()
        if not title:
            return
        total += 1
        blob = f"{title} {snippet or ''}"
        entry = {"title": title, "url": url or "", "source": source, "region": region}
        if title_he:
            entry["title_he"] = title_he   # for the site's EN ↔ HE toggle
        seen = set()
        # Multi-word phrases first; strip them so their component words aren't
        # also counted as single tokens.
        residual = blob
        for pat, canon in PHRASE_RES:
            if pat.search(residual):
                record(canon.lower(), canon, entry, seen)
                residual = pat.sub(" ", residual)
        # Single tokens, keeping the original spelling for display.
        for m in WORD_RE.finditer(residual):
            orig = POSS_RE.sub("", m.group(0))     # drop possessive 's
            tok = orig.lower()
            if not tok or tok in stop or tok.isdigit():
                continue
            # Keep normal-length words, whitelisted short concepts (oil, gas,
            # war…) and short all-caps acronyms (UAE, OPEC, IDF).
            if (len(tok) >= MIN_LEN or tok in CONCEPTS
                    or (orig.isupper() and len(orig) >= 3)):
                record(tok, orig, entry, seen)

    # 1) The geopolitics wall — the displayed headlines (5/outlet).
    for region, outlets in regions.items():
        for outlet in outlets:
            source = outlet.get("source", "")
            for h in outlet.get("headlines", []):
                url = h.get("url", "")
                if url:
                    seen_urls.add(url)
                count_entry(h.get("title"), h.get("snippet", ""), url, source, region,
                            title_he=h.get("title_he", ""))

    # 2) Tracked off-the-wall topics (World Cup) from the broad coverage sample,
    #    so the Pulse counts them even though they never reach the wall.
    for region, outlets in cov_regions.items():
        for outlet in outlets:
            source = outlet.get("source", "")
            for c in outlet.get("coverage", []):
                title = c.get("title", "")
                if not TRACKED_RE.search(title):
                    continue
                url = c.get("url", "")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                count_entry(title, "", url, source, region)

    def cap_ratio(key):
        spellings = casing[key]
        tot = sum(spellings.values())
        if not tot:
            return 0.0
        return sum(n for s, n in spellings.items() if s[:1].isupper()) / tot

    def display_form(key):
        """Show a term's most common capitalised spelling if it's upper-cased
        most of the time (proper nouns like Iran, Gaza), else lower-case (concepts
        like ceasefire, sanctions)."""
        if cap_ratio(key) >= CAP_RATIO:
            spellings = casing[key]
            return max((s for s in spellings if s[:1].isupper()),
                       key=lambda s: spellings[s], default=key)
        return key

    def keep(key):
        """Surface only places/people (proper nouns), curated phrases and listed
        concepts — drop everything else so the cloud reads like the real topics."""
        return (key in PHRASE_KEYS or key in CONCEPTS
                or cap_ratio(key) >= CAP_RATIO)

    # Keep terms mentioned in at least two headlines (single mentions are noise),
    # restricted to real topics. Fall back progressively on a thin news day.
    ranked = counts.most_common()
    common = [(t, c) for t, c in ranked if c >= 2 and keep(t)][:MAX_TERMS]
    if not common:
        common = [(t, c) for t, c in ranked if keep(t)][:MAX_TERMS]
    if not common:
        common = ranked[:MAX_TERMS]

    # Hebrew display forms for the toggle — cached dictionary, rarely an API call.
    he_map = hebrew_terms([display_form(t) for t, _ in common])
    terms = []
    for t, c in common:
        disp = display_form(t)
        terms.append({"term": disp, "term_he": he_map.get(disp, ""),
                      "count": c, "headlines": term_hls[t]})

    OUT_PATH.write_text(
        json.dumps(
            {"updated": datetime.now(timezone.utc).isoformat(),
             "total_headlines": total, "terms": terms},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote pulse.json — {len(terms)} terms across {total} headlines")


if __name__ == "__main__":
    main()
