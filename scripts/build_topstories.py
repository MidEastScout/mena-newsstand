#!/usr/bin/env python3
"""Clusters the current headline set into cross-outlet stories → top_stories.json.

Run every refresh cycle (~30 min) by the workflow, AFTER fetch_headlines.py. It
takes every headline from all outlets in headlines.json and groups the ones that
are about the SAME underlying event/issue — even when worded very differently
across outlets and languages — then ranks those clusters by genuine cross-outlet
significance so the site can surface the day's five biggest stories.

Clustering strategy (recomputed fresh every cycle — clusters never persist):
  1. If a Claude API key is available (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN),
     ask Claude to group the headlines by story and pick the clearest/most
     neutral representative plus a short neutral summary line per story. This is
     semantic — it clusters "Israel strikes Hezbollah position" with "IDF
     targets Hezbollah site in south Lebanon" even though they share few words.
  2. Otherwise fall back to a pure-Python heuristic: named-entity matching (same
     people / places / organisations) combined with informative-word overlap,
     over each item's English title + snippet (the English snippet carries the
     signal for Arabic-titled outlets). No network, no key required.

Ranking (identical for both paths — the user's stated priority order):
  • Primary   : number of DISTINCT outlets carrying the story.
  • Secondary : diversity of outlet CATEGORY / regional camp (Israeli, Gulf,
                Pan-Arab, Iranian, Levant, Turkish, International) — cross-camp
                attention is itself a significance signal.
  • Tertiary  : recency (newest item), tiebreak only.

Output (top_stories.json):
  {
    "updated": "<ISO>",
    "method":  "llm" | "heuristic",
    "model":   "<model-id>",            # present only when method == "llm"
    "stories": [
      {
        "rank": 1,
        "summary": "...", "summary_he": "...",   # present only on the llm path
        "outlets": 9,                            # distinct outlet count
        "categories": ["iranian","panarab","levant","gulf","turkish"],
        "rep": {source,category,title,title_he,url,published},   # representative
        "members": [ {source,category,title,title_he,url,published}, ... ]
      }, ...
    ]
  }

index.html loads this file and renders each story as an expandable card; if the
file is missing it falls back to clustering client-side, so the strip is never
empty.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
HL_PATH = ROOT / "headlines.json"
OUT_PATH = ROOT / "top_stories.json"

TOP_N = 5                       # stories surfaced on the site
CLAUDE_MODEL = os.environ.get("TOPSTORIES_MODEL", "claude-opus-4-8")

# --- Outlet → regional lens (camp). Kept in sync with SRC_CATS in index.html.
# EDIT BOTH PLACES when adding an outlet. Category diversity across these camps
# is the secondary ranking signal (cross-camp coverage = more significant).
SRC_CATS = {
    # Pan-Arab
    "Al Jazeera": "panarab", "Middle East Eye": "panarab", "Al Arabiya": "panarab",
    "The New Arab": "panarab", "Al Mayadeen": "panarab",
    # Iranian
    "IRNA": "iranian", "Mehr News": "iranian", "Iran International": "iranian",
    # Levant
    "Jordan Times": "levant", "L'Orient Today": "levant", "Egypt Independent": "levant",
    "Al-Akhbar": "levant", "Al Manar": "levant", "WAFA News": "levant",
    "Falastin al-Youm": "levant",
    # Gulf
    "Arab News": "gulf", "Gulf News": "gulf",
    # Turkish
    "Anadolu Agency": "turkish", "Daily Sabah": "turkish",
    "Hürriyet Daily News": "turkish", "TRT World": "turkish",
    # Israeli
    "Kan 11": "israeli", "N12": "israeli", "Channel 13": "israeli",
    "Times of Israel": "israeli", "The Jerusalem Post": "israeli", "Haaretz": "israeli",
    "Ynet News": "israeli",
    "Reuters": "intl", "BBC": "intl", "Associated Press": "intl", "AFP": "intl", "CNN": "intl",
}
# Deterministic display order for category tags on a story.
CAT_ORDER = ["israeli", "gulf", "panarab", "iranian", "levant", "turkish", "intl"]

# Arabic + Persian script (they share the Arabic Unicode blocks). Hebrew
# (U+0590–U+05FF) is deliberately NOT included — the site's HE mode is fine.
_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")


def _seg_matches_source(seg: str, source: str) -> bool:
    st = set(re.findall(r"[a-z]{3,}", seg.lower()))
    so = set(re.findall(r"[a-z]{3,}", (source or "").lower()))
    return bool(st & so)


def _clean_title(title: str, source: str) -> str:
    """Strip a trailing ' - <outlet>' / ' - <Persian tail>' attribution that some
    feeds append (e.g. '… - ایران اینترنشنال', '… - WAFA Agency')."""
    t = (title or "").strip()
    m = re.search(r"\s[-–—]\s([^-–—]{1,40})$", t)
    if m:
        seg = m.group(1).strip()
        if _ARABIC.search(seg) or _seg_matches_source(seg, source):
            t = t[:m.start()].strip()
    return t


def _first_sentence(text: str, maxlen: int = 110) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    s = (m.group(1) if m else t).strip()
    if len(s) > maxlen:
        s = s[:maxlen].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return s


def _english_display(it: dict) -> str:
    """A guaranteed-English headline for the strip: the cleaned title when it's
    English, otherwise the outlet's English snippet (present for every item, incl.
    Arabic/Persian-titled outlets). Never returns Arabic or Persian text."""
    t = _clean_title(it.get("title", ""), it.get("source", ""))
    if t and not _ARABIC.search(t):
        return t
    s = _first_sentence(it.get("snippet", ""))
    if s and not _ARABIC.search(s):
        return s
    return t or s   # last resort (shouldn't happen — snippets are English)


# ---------------------------------------------------------------------------
# Flatten headlines.json → one flat list of items, each keeping its outlet.
# ---------------------------------------------------------------------------
def flatten(data: dict) -> list[dict]:
    items = []
    for outlets in (data.get("regions") or {}).values():
        for o in outlets:
            source = o.get("source", "")
            cat = SRC_CATS.get(source)
            for h in (o.get("headlines") or []):
                title = (h.get("title") or "").strip()
                if not title:
                    continue
                items.append({
                    "source": source,
                    "category": cat,
                    "title": title,
                    "title_he": (h.get("title_he") or "").strip(),
                    "snippet": (h.get("snippet") or "").strip(),
                    "url": h.get("url") or "",
                    "published": h.get("published") or "",
                    "_t": _parse_ts(h.get("published")),
                })
    return items


def _parse_ts(iso) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Heuristic clustering: named-entity matching + informative-word overlap.
# ---------------------------------------------------------------------------
STOP = set((
    "the a an and or but for nor with without from into onto over under after before during amid "
    "says said say saying will would could should shall has have had was were are is be been being not no its his her "
    "their they them this that these those as at by in of on to up out off down more most than then also just about "
    "against between among across near still yet how what when where who why while since because per via de la el "
    "al us he she it we you your our two three four five first last next new news live update updates report reports "
    "breaking latest today year years day days week weeks month months amid still says video photos live blog "
    "against toward towards call calls chief official officials people country countries government minister ministry "
    "president leader talks deal state week days year over past hours near amid set vows warn warns begins begin"
).split())

# Named-entity gazetteer. Each canonical entity maps to English aliases matched
# (whole-word, case-insensitive) against the item's title + snippet. Grouping by
# shared entities catches same-story headlines that share almost no plain words.
# The English snippet is present even on Arabic-titled outlets, so cross-language
# stories still cluster on it. EDIT HERE to sharpen entity resolution.
#
# STRONG entities are specific events / people / places / organisations — sharing
# one is good evidence of the same story. BROAD entities are nation-level lenses
# (Iran, Israel, the US, Turkey) that co-occur across many unrelated stories in
# this region, so they must not, on their own, glue two headlines together.
ENTITIES_STRONG = {
    "khamenei": ["khamenei", "ayatollah", "supreme leader", "slain leader", "martyred leader",
                 "revolution leader", "martyred leader"],
    "funeral": ["funeral", "mourning", "mourn", "laid in state", "laid to rest", "commemoration",
                "state funeral", "mass funeral", "week of mourning", "burial", "funeral ceremonies"],
    "hormuz": ["hormuz", "strait of hormuz"],
    "nuclear": ["nuclear", "enrichment", "uranium", "natanz", "fordow"],
    "gaza": ["gaza", "hamas", "rafah", "khan younis", "jabaliya", "deir al-balah", "islamic jihad",
             "gaza strip"],
    "westbank": ["west bank", "ramallah", "jenin", "nablus", "hebron", "settler", "settlers",
                 "colonist", "colonists", "settlement", "demolition", "demolitions", "tulkarem"],
    "hezbollah": ["hezbollah", "nasrallah"],
    "lebanon_israel_deal": ["lebanon-israel", "lebanese-israeli", "framework agreement",
                            "washington agreement", "17 may", "after israel agreement"],
    "hezbollah_south": ["bint jbeil", "litani", "wadi slouki", "south lebanon", "southern lebanon"],
    "syria": ["syria", "syrian", "damascus", "druze", "aleppo", "shaibani"],
    "ukraine": ["ukraine", "ukrainian", "kyiv", "kiev", "russian strike", "russian attack"],
    "nato": ["nato", "nato summit"],
    "venezuela": ["venezuela", "venezuelan", "caracas"],
    "earthquake": ["earthquake", "quake"],
    "yemen": ["yemen", "yemeni", "houthi", "houthis"],
    "afd": ["afd", "alternative for germany"],
    "us_iran_deal": ["us-iran deal", "iran deal", "nuclear deal"],
    "football": ["afcon", "quarter-final", "quarter-finals", "co-hosts"],
    "boeing": ["boeing", "saudia"],
}
ENTITIES_BROAD = {
    "iran": ["iran", "iranian", "tehran", "irgc", "revolutionary guard", "pezeshkian", "araghchi"],
    "israel": ["israel", "israeli", "idf", "netanyahu", "knesset", "tel aviv", "occupation",
               "colonists", "settlers"],
    "washington": ["united states", "washington", "u.s.", "trump", "american"],
    "turkey": ["turkey", "türkiye", "turkish", "ankara", "erdogan"],
    "russia": ["russia", "russian", "moscow", "putin"],
}
ENTITIES = {**ENTITIES_STRONG, **ENTITIES_BROAD}
STRONG = set(ENTITIES_STRONG)
_ENT_RE = {
    canon: [re.compile(r"\b" + re.escape(a).replace(r"\ ", r"\s+") + r"\b", re.I) for a in aliases]
    for canon, aliases in ENTITIES.items()
}


def _tokens(text: str) -> set:
    words = re.findall(r"[a-z][a-z-]{3,}", (text or "").lower())
    return {w for w in words if w not in STOP}


def _entities(text: str) -> set:
    found = set()
    for canon, regexes in _ENT_RE.items():
        if any(r.search(text or "") for r in regexes):
            found.add(canon)
    return found


def cluster_heuristic(items: list[dict]) -> list[list[int]]:
    """Union-find over pairwise similarity. Two headlines are the same story when
    they share a SPECIFIC entity (backed by a second shared entity or word
    overlap), or their titles are near-duplicates. Broad nation-level entities
    alone never merge — that keeps the many Iran/Israel stories from collapsing
    into one blob. Entities are read from title + snippet (so Arabic-titled
    outlets cluster on their English snippet); word overlap uses titles only (so
    two headlines aren't merged just because their snippets share filler)."""
    n = len(items)
    title_tok, ents, strong_ents = [], [], []
    for it in items:
        title_tok.append(_tokens(it["title"]))
        e = _entities(f"{it['title']} {it['snippet']}")
        ents.append(e)
        strong_ents.append(e & STRONG)

    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            shared_strong = len(strong_ents[i] & strong_ents[j])
            shared_ent = len(ents[i] & ents[j])
            shared_tok = len(title_tok[i] & title_tok[j])
            same = (
                (shared_strong >= 1 and (shared_ent >= 2 or shared_tok >= 2))  # specific story
                or shared_tok >= 4                                             # near-duplicate title
            )
            if same:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def pick_representative_heuristic(items: list[dict], idxs: list[int]) -> int:
    """Choose the clearest, most on-topic member: an English headline that names
    the story's dominant specific entities, isn't a bare 'Live'/'Breaking' stub,
    and reads at a natural headline length. Title-word centrality and recency
    break remaining ties."""
    # The story's dominant strong entities = those appearing in the most members.
    strong_counts = {}
    for i in idxs:
        for e in _entities(f"{items[i]['title']} {items[i]['snippet']}") & STRONG:
            strong_counts[e] = strong_counts.get(e, 0) + 1
    dominant = {e for e, c in strong_counts.items() if c >= max(strong_counts.values(), default=0) * 0.5}

    tok = {i: _tokens(items[i]["title"]) for i in idxs}
    generic = re.compile(r"^(live|breaking|war on|live blog|watch|video|photos|update|main news)\b", re.I)
    best, best_key = idxs[0], None
    for i in idxs:
        title = items[i]["title"]
        clean = _clean_title(title, items[i]["source"])
        english = bool(clean) and not _ARABIC.search(clean)    # real English headline available
        title_ents = _entities(title) & STRONG
        covers = len(title_ents & dominant)                    # names the shared story
        stub = bool(generic.match(clean)) or len(clean) < 18
        length_fit = -abs(len(clean) - 50)                     # prefer natural headline length
        central = sum(len(tok[i] & tok[j]) for j in idxs if j != i)
        # English first, so the card shows a genuine English headline rather than
        # falling back to a snippet; then story-coverage, non-stub, length, etc.
        key = (english, covers, not stub, length_fit, central, items[i]["_t"])
        if best_key is None or key > best_key:
            best_key, best = key, i
    return best


# ---------------------------------------------------------------------------
# Claude clustering (used when an API key is available). Falls back to None on
# any error so the pipeline degrades to the heuristic and never breaks.
# ---------------------------------------------------------------------------
CLUSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "summary_he": {"type": "string"},
                    "representative_index": {"type": "integer"},
                    "member_indices": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["summary", "summary_he", "representative_index", "member_indices"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["stories"],
    "additionalProperties": False,
}


def _claude_client():
    """Return an Anthropic client if a key/credential and the SDK are available,
    else None (heuristic path)."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    try:
        import anthropic
    except ImportError:
        print("anthropic SDK not installed — using heuristic clustering", file=sys.stderr)
        return None
    try:
        return anthropic.Anthropic()
    except Exception as exc:
        print(f"Anthropic client init failed ({exc}) — using heuristic", file=sys.stderr)
        return None


def cluster_with_claude(client, items: list[dict]):
    """Ask Claude to group every headline by underlying story. Returns a list of
    stories (each: member indices, representative index, neutral EN/HE summary)
    or None on failure. Every index must be assigned to exactly one story."""
    lines = []
    for i, it in enumerate(items):
        snip = it["snippet"][:180]
        cat = it["category"] or "other"
        lines.append(f"[{i}] ({it['source']} · {cat}) {it['title']}"
                     + (f" — {snip}" if snip else ""))
    listing = "\n".join(lines)

    prompt = (
        "You are a wire editor clustering live Middle East headlines from many "
        "outlets. Below are today's headlines, one per line, each numbered [i] "
        "with its outlet and outlet-category.\n\n"
        "Group the headlines that are about the SAME underlying event or issue "
        "into stories — even when worded very differently or in different "
        "languages across outlets (e.g. 'Israel strikes Hezbollah position' and "
        "'IDF targets Hezbollah site in south Lebanon' are ONE story). Cluster by "
        "meaning, not shared words. A headline that stands alone is its own "
        "one-item story. Assign EVERY headline index to exactly one story, and "
        "never invent an index.\n\n"
        "For each story return:\n"
        "  - member_indices: all headline indices in the story.\n"
        "  - representative_index: the member with the clearest, most neutral "
        "phrasing (avoid loaded wording and bare 'Live'/'Breaking' stubs).\n"
        "  - summary: a short, neutral one-line summary of the story in English "
        "(max ~14 words), attributing claims where outlets differ.\n"
        "  - summary_he: the same neutral summary in Hebrew.\n\n"
        "Return ONLY the JSON object.\n\n"
        "HEADLINES:\n" + listing
    )

    try:
        # Non-streaming stays well under the SDK's ~16K HTTP-timeout guard; the
        # headroom lets adaptive thinking run without truncating the JSON (a
        # truncated body just parses-fails below and falls back to the heuristic).
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": CLUSTER_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        print(f"Claude clustering call failed ({exc}) — using heuristic", file=sys.stderr)
        return None

    if resp.stop_reason == "refusal":
        print("Claude declined the clustering request — using heuristic", file=sys.stderr)
        return None

    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
        stories = parsed["stories"]
    except Exception as exc:
        print(f"Could not parse Claude clustering output ({exc}) — using heuristic",
              file=sys.stderr)
        return None

    # Validate: indices in range, each assigned once, representative in members.
    n = len(items)
    seen, clean = set(), []
    for s in stories:
        idxs = [i for i in s.get("member_indices", []) if isinstance(i, int) and 0 <= i < n]
        idxs = [i for i in idxs if i not in seen]
        if not idxs:
            continue
        seen.update(idxs)
        rep = s.get("representative_index")
        if rep not in idxs:
            rep = idxs[0]
        clean.append({
            "member_indices": idxs,
            "representative_index": rep,
            "summary": (s.get("summary") or "").strip(),
            "summary_he": (s.get("summary_he") or "").strip(),
        })
    # Any headline Claude dropped becomes its own one-item story.
    for i in range(n):
        if i not in seen:
            clean.append({"member_indices": [i], "representative_index": i,
                          "summary": "", "summary_he": ""})
    return clean


# ---------------------------------------------------------------------------
# Ranking + assembling the output stories (shared by both clustering paths).
# ---------------------------------------------------------------------------
def _member_view(it: dict) -> dict:
    # `title` is forced to English for the strip (EN mode); `title_he` (Hebrew)
    # stays for HE mode. Neither is ever Arabic or Persian.
    return {
        "source": it["source"],
        "category": it["category"],
        "title": _english_display(it),
        "title_he": _clean_title(it.get("title_he", ""), it["source"]),
        "url": it["url"],
        "published": it["published"],
    }


def build_stories(items: list[dict], clusters: list[dict]) -> list[dict]:
    """clusters: [{member_indices, representative_index, summary?, summary_he?}].
    Rank by distinct-outlet count → category diversity → recency, keep top N."""
    scored = []
    for c in clusters:
        idxs = c["member_indices"]
        members = [items[i] for i in idxs]
        outlets = {m["source"] for m in members}
        cats = {m["category"] for m in members if m["category"]}
        newest = max((m["_t"] for m in members), default=0.0)
        scored.append((c, idxs, members, len(outlets), len(cats), newest))

    # Primary: outlet count. Secondary: category (camp) diversity. Tertiary: recency.
    scored.sort(key=lambda x: (x[3], x[4], x[5]), reverse=True)

    out = []
    for rank, (c, idxs, members, n_outlets, n_cats, newest) in enumerate(scored[:TOP_N], 1):
        rep = items[c["representative_index"]]
        cats = sorted({m["category"] for m in members if m["category"]},
                      key=lambda x: CAT_ORDER.index(x) if x in CAT_ORDER else 99)
        member_views = sorted((_member_view(m) for m in members),
                              key=lambda m: _parse_ts(m["published"]), reverse=True)
        story = {
            "rank": rank,
            "outlets": n_outlets,
            "categories": cats,
            "rep": _member_view(rep),
            "members": member_views,
        }
        if c.get("summary"):
            story["summary"] = c["summary"]
        if c.get("summary_he"):
            story["summary_he"] = c["summary_he"]
        out.append(story)
    return out


def main():
    try:
        data = json.loads(HL_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read headlines.json ({exc}) — skipping top stories", file=sys.stderr)
        sys.exit(0)

    items = flatten(data)
    if not items:
        print("No headlines to cluster — skipping", file=sys.stderr)
        sys.exit(0)

    method, model, clusters = "heuristic", None, None
    client = _claude_client()
    if client is not None:
        clusters = cluster_with_claude(client, items)
        if clusters is not None:
            method, model = "llm", CLAUDE_MODEL

    if clusters is None:  # heuristic path (fallback or no key)
        clusters = [
            {"member_indices": g,
             "representative_index": pick_representative_heuristic(items, g)}
            for g in cluster_heuristic(items)
        ]

    stories = build_stories(items, clusters)

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "stories": stories,
    }
    if model:
        payload["model"] = model

    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote top_stories.json — {len(stories)} stories via {method} "
          f"from {len(items)} headlines")

    if os.environ.get("TOPSTORIES_SAMPLE"):
        _print_sample(stories, method, model)


def _print_sample(stories, method, model):
    """Human-readable dump of the ranked Top 5 for sanity-checking the clustering."""
    print("\n" + "=" * 74)
    print(f"  TOP {len(stories)} STORIES  ·  method={method}" + (f" ({model})" if model else ""))
    print("=" * 74)
    for s in stories:
        head = s.get("summary") or s["rep"]["title"]
        print(f"\n#{s['rank']}  {head}")
        print(f"    {s['outlets']} outlets  ·  camps: {', '.join(s['categories']) or '—'}"
              f"  ·  {len(s['members'])} headlines")
        print(f"    representative: [{s['rep']['source']}] {s['rep']['title']}")
        for m in s["members"]:
            print(f"      · [{m['source']:20s}|{m['category'] or '—':8s}] {m['title'][:88]}")
    print()


if __name__ == "__main__":
    main()
