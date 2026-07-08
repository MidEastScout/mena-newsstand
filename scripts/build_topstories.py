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
  2. Otherwise fall back to a pure-Python heuristic that clusters by strong-entity
     CO-OCCURRENCE anchors, then splits distinct sub-events within a topic. This
     avoids the naive-single-linkage trap where one bridging headline fuses two
     unrelated stories into a giant blob. It reads each item's English title +
     snippet (the snippet carries the signal for Arabic/Hebrew/Persian-titled
     outlets), so a story is counted across every outlet in every language, not
     just the English ones. No network, no key required.

Topical filter (both paths): only Middle-Eastern / global geopolitics, security,
military, conflict and diplomacy stories are eligible — the Ukraine war counts;
domestic-administrative politics (e.g. Israeli coalition / court / regulator
stories), sports, tech & consumer business, markets and lifestyle are dropped so
they can't take a Top-5 slot from a real security story.

Ranking (identical for both paths — the user's stated priority order):
  • Primary   : salience score = freshness-weighted DISTINCT-outlet votes (each
                outlet votes once, decaying with a 12-hour half-life on its
                newest item) × a cross-camp diversity bonus (Israeli, Gulf,
                Pan-Arab, Iranian, Levant, Turkish, International) — breadth of
                outlets is the main signal, cross-camp attention lifts it.
  • Secondary : recency (newest item).
  • Tertiary  : a hash of the member URLs — a pure content tie-break, so the
                order NEVER depends on ingestion or cluster enumeration order.

Output (top_stories.json):
  {
    "updated": "<ISO>",
    "method":  "llm" | "heuristic",
    "model":   "<model-id>",            # present only when method == "llm"
    "stories": [
      {
        "rank": 1,
        "summary": "...", "summary_he": "...",   # neutral EN/HE line (Gemini/LLM)
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
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from itertools import combinations
from pathlib import Path

# Deterministic grounding verification for generated titles + the shared
# framing/neutrality lexicons (single source of truth: title_grounding.py).
sys.path.insert(0, str(Path(__file__).parent))
from title_grounding import (CURRENT_FRAME_RE, LOADED_RE, STALE_FRAME_RE,
                             verify_title)

ROOT = Path(__file__).parent.parent
HL_PATH = ROOT / "headlines.json"
OUT_PATH = ROOT / "top_stories.json"
SUMM_CACHE = ROOT / "state" / "topstories_summaries.json"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

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
    "Falastin al-Youm": "levant", "SANA": "levant", "Syria Direct": "levant",
    # Yemen — Al-Masirah (Houthi) carries the pan-Arab resistance lens.
    "Al-Masirah": "panarab",
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
    """Group headlines into stories WITHOUT letting a single bridging headline
    fuse two unrelated events — the failure mode of naive single-linkage, where
    one 'Gaza funeral' item glues the whole Gaza story onto Khamenei's funeral and
    produces one giant, meaningless top 'story'.

    Strategy — story-topic anchors from strong-entity CO-OCCURRENCE:
      1. Strong entities that genuinely co-occur across many headlines (khamenei +
         funeral) merge into one topic anchor; an incidental one-off overlap
         (gaza + funeral, seen once) does NOT, so distinct topics stay apart.
      2. Each headline joins the anchor it fits best: most of its strong entities
         land there, breaking ties toward its more SPECIFIC (rarer) entity — so a
         generic 'funeral'/'mourning' word can't drag an unrelated death story (a
         Venezuela quake, a Lebanon burial) into Khamenei's funeral.
      3. Within an anchor, distinct sub-events separate by wording overlap, while
         topic stragglers — including Arabic/Hebrew-titled items that carry their
         signal in the English snippet, not the title — fold into the sub-event
         they match best instead of fragmenting off. That keeps cross-language
         coverage COUNTED, never stranded as its own bogus one-outlet story.
         BUT a straggler must share real evidence (a strong entity, or clear
         wording overlap) with the sub-event it joins; one with none stays its
         own one-item story rather than contaminating the biggest sub-event.
      4. Headlines with no known entity cluster among themselves by near-duplicate
         title only.

    Entities are read from title + snippet (the English snippet is present on
    every item, so Arabic/Persian/Hebrew-titled outlets cluster too); wording
    overlap uses titles. Result: the ranking counts every outlet carrying a story
    in any language, and the top five are genuinely distinct events."""
    n = len(items)
    all_ents  = [_entities(f"{it['title']} {it['snippet']}") for it in items]
    strong    = [e & STRONG for e in all_ents]
    title_tok = [_tokens(it["title"]) for it in items]

    # 1. Co-occurrence anchors over strong entities. Two strong entities name the
    #    same story when they co-occur in >=2 headlines AND in >=40% of the rarer
    #    one's headlines — real pairs (khamenei+funeral) merge, one-offs don't.
    ent_count, cooc = Counter(), Counter()
    for se in strong:
        for e in se:
            ent_count[e] += 1
        for e, f in combinations(sorted(se), 2):
            cooc[(e, f)] += 1
    eparent = {e: e for e in ent_count}

    def efind(e):
        while eparent[e] != e:
            eparent[e] = eparent[eparent[e]]
            e = eparent[e]
        return e

    for (e, f), c in cooc.items():
        if c >= 2 and c >= 0.4 * min(ent_count[e], ent_count[f]):
            ra, rb = efind(e), efind(f)
            if ra != rb:
                eparent[ra] = rb

    # 2. Assign each item to its best-fit anchor (most entities there; more
    #    specific entity wins ties). No-entity items are held for phase 4.
    def item_anchor(i):
        se = strong[i]
        if not se:
            return None
        by = defaultdict(list)
        for e in se:
            by[efind(e)].append(e)
        return max(by, key=lambda a: (len(by[a]),
                                      sum(1.0 / ent_count[e] for e in by[a])))

    anchor_of = [item_anchor(i) for i in range(n)]
    by_anchor = defaultdict(list)
    for i in range(n):
        if anchor_of[i] is not None:
            by_anchor[anchor_of[i]].append(i)

    # 3. Split each anchor into distinct events; fold stragglers into the event
    #    they match best (default: the largest = the topic's main story). Bounded
    #    to one anchor, so this single-linkage can never bridge across topics.
    def subsplit(idxs):
        if len(idxs) <= 2:
            return [idxs]
        parent = {i: i for i in idxs}

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                if (len(title_tok[i] & title_tok[j]) >= 3
                        or (len(all_ents[i] & all_ents[j]) >= 2
                            and len(title_tok[i] & title_tok[j]) >= 1)):
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[ri] = rj
        groups = defaultdict(list)
        for i in idxs:
            groups[find(i)].append(i)
        dense   = [g for g in groups.values() if len(g) >= 2]
        singles = [g[0] for g in groups.values() if len(g) == 1]
        if not dense:
            return [idxs]                       # one coherent story — keep whole
        dense.sort(key=len, reverse=True)
        for s in singles:
            # A straggler only folds into a sub-event it shares REAL evidence
            # with: 2+ entities, or 2+ informative title words, with some
            # member (a relaxed echo of the pairwise-union rule above — no
            # title requirement, so Arabic/Hebrew-titled items whose entities
            # come from the English snippet still fold). Sharing only the
            # anchor's single topic entity (gaza, funeral, nato…) is NOT
            # enough: that is how a chickenpox-outbreak item joined the
            # Gaza-strikes story and an unrelated quote joined the NATO-summit
            # story, poisoning their titles. A straggler with no qualifying
            # sub-event stays a one-item story instead.
            best, best_score = None, 0
            for g in dense:
                if not any(len(all_ents[s] & all_ents[m]) >= 2
                           or len(title_tok[s] & title_tok[m]) >= 2 for m in g):
                    continue
                score = sum(2 * len(all_ents[s] & all_ents[m])
                            + len(title_tok[s] & title_tok[m]) for m in g)
                if score > best_score:
                    best, best_score = g, score
            if best is not None:
                best.append(s)
            else:
                dense.append([s])
        return dense

    clusters = []
    for idxs in by_anchor.values():
        clusters.extend(subsplit(idxs))

    # 4. No-entity leftovers: merge only near-duplicate titles.
    leftover = [i for i in range(n) if anchor_of[i] is None]
    lp = {i: i for i in leftover}

    def lfind(i):
        while lp[i] != i:
            lp[i] = lp[lp[i]]
            i = lp[i]
        return i

    for a in range(len(leftover)):
        for b in range(a + 1, len(leftover)):
            i, j = leftover[a], leftover[b]
            if len(title_tok[i] & title_tok[j]) >= 4:
                ri, rj = lfind(i), lfind(j)
                if ri != rj:
                    lp[ri] = rj
    lg = defaultdict(list)
    for i in leftover:
        lg[lfind(i)].append(i)
    clusters.extend(lg.values())

    return clusters


# Partisan / loaded wording (LOADED_RE, imported from title_grounding). When a
# cluster has both loaded and plain headlines, prefer the plain one as the
# representative (used only as a tiebreaker among members of the SAME story, so
# it never changes which stories are shown).

# Which outlet's wording heads the cluster. The Top-5 falls back to this
# representative headline whenever the neutral LLM summary is unavailable (the
# common case on Gemini's free tier), so it must read cleanly. Mainstream wires
# and broadsheets phrase events plainly and hedge status correctly ("leaders to
# meet", "heads to summit"); state / heavily partisan outlets lead with framing
# ("10 strategic messages of the Leader's funeral", "Yemen exposes the
# aggression"). Prefer the former FOR THE HEADLINE — every member still shows,
# attributed, in the expanded view. Higher = preferred; unlisted outlets = 1.
_REP_SOURCE_RANK = {
    "Reuters": 3, "Associated Press": 3, "AFP": 3, "BBC": 3, "CNN": 3,
    "Al Jazeera": 2, "Anadolu Agency": 2, "Arab News": 2, "Gulf News": 2,
    "Daily Sabah": 2, "Jordan Times": 2, "Egypt Independent": 2, "Al Arabiya": 2,
    "The New Arab": 2, "Middle East Eye": 2, "Hürriyet Daily News": 2,
    "TRT World": 2, "L'Orient Today": 2, "The National": 2,
    "IRNA": 0, "Mehr News": 0, "Al Manar": 0, "Al Mayadeen": 0, "Al-Masirah": 0,
    "Falastin al-Youm": 0,
}
_REP_RANK_DEFAULT = 1

# Anticipatory framing that goes STALE once the event is under way (imported
# STALE_FRAME_RE / CURRENT_FRAME_RE — see title_grounding.py): we demote such a
# headline as the representative whenever a NEWER member frames the same story
# as happening or finished, and verify_title rejects it in a generated summary.
# A member more than this far behind the cluster's newest item is treated as not
# "fresh" for representative selection. Kept TIGHT (6h): in a running story
# (attack → retaliation → sanctions) yesterday's wave still uses active verbs
# ("ships struck…"), so tense regexes can't spot it — only recency can. The
# headline must come from the newest wave of coverage; older members lead only
# when no fresh member survives the english/neutral gates.
STALE_GAP_SEC = 6 * 3600


def pick_representative_heuristic(items: list[dict], idxs: list[int]) -> int:
    """Choose the clearest, most on-topic member: a NEUTRAL English headline that
    names the story's dominant specific entities, isn't a bare 'Live'/'Breaking'
    stub, and reads at a natural headline length. Title-word centrality and
    recency break remaining ties."""
    # The story's dominant strong entities = those appearing in the most members.
    strong_counts = {}
    for i in idxs:
        for e in _entities(f"{items[i]['title']} {items[i]['snippet']}") & STRONG:
            strong_counts[e] = strong_counts.get(e, 0) + 1
    dominant = {e for e, c in strong_counts.items() if c >= max(strong_counts.values(), default=0) * 0.5}

    tok = {i: _tokens(items[i]["title"]) for i in idxs}
    generic = re.compile(r"^(live|breaking|war on|live blog|watch|video|photos|in pictures|update|main news)\b", re.I)
    # Recency signals for the whole cluster: the newest timestamp, and whether any
    # member frames the story as already happening/finished — if one does, an
    # older "to host / selected to host" member is out of date and must not lead.
    newest_t = max((items[i]["_t"] for i in idxs), default=0.0)
    cluster_has_current = any(CURRENT_FRAME_RE.search(items[i]["title"]) for i in idxs)
    best, best_key = idxs[0], None
    for i in idxs:
        title = items[i]["title"]
        clean = _clean_title(title, items[i]["source"])
        english = bool(clean) and not _ARABIC.search(clean)    # real English headline available
        title_ents = _entities(title) & STRONG
        covers = len(title_ents & dominant)                    # names the shared story
        stub = bool(generic.match(clean)) or len(clean) < 18
        neutral = not (LOADED_RE.search(clean) or "!" in clean)
        src_rank = _REP_SOURCE_RANK.get(items[i]["source"], _REP_RANK_DEFAULT)
        # Recency: reject stale anticipatory framing when the cluster has a more
        # current member, and prefer members within STALE_GAP of the newest.
        stale_frame = bool(STALE_FRAME_RE.search(clean)) and not CURRENT_FRAME_RE.search(clean)
        current_ok = not (stale_frame and cluster_has_current)
        fresh = (newest_t - items[i]["_t"]) <= STALE_GAP_SEC
        length_fit = -abs(len(clean) - 50)                     # prefer natural headline length
        central = sum(len(tok[i] & tok[j]) for j in idxs if j != i)
        # English + neutral are gates; then reject stale framing (current_ok);
        # then FRESHNESS — the headline must come from the cluster's newest wave
        # of coverage, so an older stage of a running story ("ships struck…")
        # can't lead once the story has escalated (US strikes, sanctions). Only
        # among equally-fresh members do coverage, a mainstream outlet's plainer
        # phrasing, non-stub, length and centrality decide; raw recency, then
        # URL (pure determinism — never input order) settle what's left.
        key = (english, neutral, current_ok, fresh, covers, src_rank,
               not stub, length_fit, central, items[i]["_t"], items[i]["url"])
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
        "  - representative_index: the member with the clearest, most neutral, "
        "MOST CURRENT phrasing (avoid loaded wording, bare 'Live'/'Breaking' "
        "stubs, and stale framing — prefer the newest member's status).\n"
        "  - summary: a short, neutral, punchy one-line summary in English "
        "(max ~14 words, active voice). TOPIC = the consensus: the event most "
        "of the story's outlets report — never a single outlet's angle, and "
        "never an outlet as the subject ('X reports…'). STATUS from the most "
        "recent headline: if an older one says an event is upcoming ('to host', "
        "'leaders to meet') but a newer one shows it happening or done ('under "
        "way', 'met'), write the CURRENT state. Every actor, action, number "
        "and outcome must appear in the story's headlines — add nothing they "
        "don't state; attribute claims where outlets differ. When camps frame "
        "the event differently, state what happened plainly and attribute each "
        "side's interpretation — adopt no camp's framing or vocabulary.\n"
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
# Topical filter — keep only what the site is FOR: Middle-Eastern and global
# geopolitics / security / military / conflict / diplomacy (the Ukraine war
# counts). Drop domestic-administrative politics (especially Israeli coalition /
# court / regulator stories), sports, tech & consumer business, markets/finance,
# and lifestyle/health/entertainment — so a heavily-carried but off-topic item
# never takes a Top-5 slot from a real security story.
# ---------------------------------------------------------------------------
SEC_WORDS = set("""
war wars warplane warplanes military militia militias militant militants fighter fighters army troops
soldier soldiers forces gunmen airstrike airstrikes strike strikes shelling bombard bombardment
bombing bombings blast blasts explosion explosions missile missiles rocket rockets drone drones
artillery offensive incursion raid raids ambush clash clashes fighting combat frontline siege
blockade ceasefire truce armistice killed dead casualties wounded slain massacre genocide hostage
hostages captive captives prisoner prisoners abducted kidnapped assassination assassinated martyr
martyred martyrs mourning funeral coup uprising revolt insurgency insurgents terror terrorist
terrorists extremist extremists jihad hamas hezbollah houthi houthis irgc nuclear enrichment uranium
centrifuges ballistic sanctions sanction embargo diplomat diplomacy diplomatic summit negotiations
negotiation talks treaty accord envoy delegation occupation settler settlers settlement settlements
annexation annex sovereignty escalation retaliation deterrence naval warship warships airspace
checkpoint checkpoints intelligence espionage refugees refugee displaced famine evacuation crackdown
detained detention insurgent militiamen
""".split())
SEC_PHRASES = (
    "air strike", "west bank", "gaza strip", "strait of hormuz", "red sea", "security council",
    "foreign minister", "defense minister", "defence minister", "war crimes", "death toll",
    "peace deal", "prisoner exchange", "ground offensive", "revolutionary guard", "islamic jihad",
    "peace talks", "war on", "human rights", "aid convoy", "arms deal",
)
OFF_WORDS = set("""
football soccer fifa uefa afcon match matches league tournament striker goalkeeper goals penalty
penalties coach olympics medal medals championship cricket tennis basketball app apps iphone android
google apple microsoft meta whatsapp tiktok website websites login logins password startup startups
gadget smartphone smartphones ecommerce streaming stock stocks shares bourse ipo dividend
cryptocurrency bitcoin crypto recipe watermelon cuisine celebrity movie movies film films cinema
actor actress singer concert festival fashion wedding horoscope zodiac diet skincare tourism tourist
weather forecast rainfall
""".split())
OFF_PHRASES = (
    "broadcast regulator", "broadcasting authority", "supreme court", "high court", "court of appeal",
    "attorney general", "box office", "red carpet", "stock market", "interest rate", "exchange rate",
    "world cup", "champions league", "transfer window", "oil output", "output hike", "judicial",
)
# Natural disasters / accidents read as "security" via casualty words (killed,
# refugees) but aren't geopolitics/military — a landslide or quake is off-topic
# UNLESS a real conflict dimension is present. So they're excluded only when no
# CONFLICT word appears.
DISASTER_WORDS = set("""
landslide landslides mudslide mudslides flood floods flooding earthquake earthquakes quake quakes
aftershock aftershocks storm storms cyclone hurricane hurricanes typhoon monsoon wildfire wildfires
avalanche drought tornado tornadoes stampede crash crashes collision derailment capsized wreck
""".split())
CONFLICT_WORDS = set("""
war wars warplane warplanes military militia militias militant militants fighter fighters troops
soldier soldiers gunmen airstrike airstrikes strike strikes shelling bombard bombardment bombing
bombings missile missiles rocket rockets drone drones artillery offensive incursion raid raids ambush
clash clashes siege blockade ceasefire truce hostage hostages militiamen insurgent insurgency
insurgents terror terrorist terrorists jihad hamas hezbollah houthi houthis irgc nuclear enrichment
sanctions embargo occupation settler settlers settlement settlements annexation coup assassination
assassinated warship warships naval
""".split())
CONFLICT_PHRASES = ("air strike", "ground offensive", "war crimes", "revolutionary guard", "islamic jihad")


def _is_relevant(members: list[dict]) -> bool:
    """True if a cluster is on-topic: it carries a clear security / geopolitical /
    military / diplomacy signal that is not outweighed by off-topic (sports, tech,
    market, lifestyle, domestic-admin) signals. Read over every member's title +
    English snippet, so the judgement holds across languages."""
    text = " ".join(f"{m.get('title','')} {m.get('snippet','')}" for m in members).lower()
    toks = set(re.findall(r"[a-z]+", text))
    # A natural disaster / accident with no conflict dimension is off-topic even
    # though it mentions casualties or refugees (e.g. a Bangladesh landslide).
    if (toks & DISASTER_WORDS) and not ((toks & CONFLICT_WORDS)
                                        or any(p in text for p in CONFLICT_PHRASES)):
        return False
    sec = len(toks & SEC_WORDS) + sum(p in text for p in SEC_PHRASES)
    off = len(toks & OFF_WORDS) + sum(p in text for p in OFF_PHRASES)
    return sec >= 1 and sec >= off


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


# Ranking half-life: an outlet's vote on a story decays with the age of that
# outlet's newest item on it, halving every RANK_HALFLIFE_H hours. Breadth stays
# the primary signal, but a story whose coverage is uniformly old (yesterday's
# stage of a running story) can no longer outrank the day's fresh development
# just because it has had more hours to accumulate outlets.
RANK_HALFLIFE_H = 12.0
# Cross-camp coverage bonus: each additional regional camp carrying the story
# lifts its score by this fraction. Folded INTO the score (not a tie-break key)
# because the freshness-weighted vote is a float — exact ties never happen, so
# a mere secondary sort key would be dead code and camp diversity would never
# actually influence the ranking.
DIVERSITY_BONUS = 0.15


def build_stories(items: list[dict], clusters: list[dict], now: float | None = None) -> list[dict]:
    """clusters: [{member_indices, representative_index, summary?, summary_he?}].
    Rank by salience score = freshness-weighted outlet votes × (1 + camp-
    diversity bonus), then recency, then a content hash — fully deterministic
    and independent of ingestion/cluster order. Keep the top N that pass the
    geopolitics/security topical filter. `now` is injectable for tests."""
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    scored = []
    for c in clusters:
        idxs = c["member_indices"]
        members = [items[i] for i in idxs]
        outlets = {m["source"] for m in members}
        cats = {m["category"] for m in members if m["category"]}
        newest = max((m["_t"] for m in members), default=0.0)
        # Each outlet votes once, weighted by how recently it last touched the
        # story (its newest member), with a RANK_HALFLIFE_H-hour half-life.
        per_outlet_newest = {}
        for m in members:
            per_outlet_newest[m["source"]] = max(per_outlet_newest.get(m["source"], 0.0), m["_t"])
        weight = sum(0.5 ** (max(0.0, (now - t) / 3600.0) / RANK_HALFLIFE_H)
                     for t in per_outlet_newest.values())
        score = weight * (1.0 + DIVERSITY_BONUS * max(0, len(cats) - 1))
        # Content-derived final tie-break, so equal-score stories order by WHAT
        # they contain, never by the order clusters happened to be enumerated.
        urlsig = sha256("|".join(sorted(m["url"] for m in members)).encode("utf-8")).hexdigest()
        scored.append((c, idxs, members, len(outlets), len(cats), newest, score, urlsig))

    # Salience score (outlet votes × camp diversity) → recency → content hash.
    scored.sort(key=lambda x: (x[6], x[5], x[7]), reverse=True)

    # Keep only on-topic stories (geopolitics / security / military / diplomacy),
    # THEN take the top N — so a widely-carried but off-topic item (a court
    # ruling, a tech fine, a cup final) can't claim a slot. Safety net: if a
    # cycle somehow has no on-topic cluster, fall back to the raw ranking rather
    # than blanking the strip.
    relevant = [s for s in scored if _is_relevant(s[2])] or scored

    out = []
    for rank, (c, idxs, members, n_outlets, n_cats, newest, _score, _sig) in enumerate(relevant[:TOP_N], 1):
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


# ---------------------------------------------------------------------------
# Neutral one-line summaries (English + Hebrew) for the Top 5 — GROUNDED.
#
# Outlet headlines are often loaded ("martyred Leader", "death towers",
# "colonists", scare-quoted "ceasefire"). Rather than surface one outlet's
# framing, we ask Gemini — already configured for the World Briefing — for a
# short, factual, NEUTRAL line per story in both languages, and the site shows
# that instead of the representative headline.
#
# STRUCTURAL GUARANTEE (the fix for titles drifting from their sources): no
# generated line is ever attached without passing title_grounding.verify_title
# against the story's CURRENT members — fresh Gemini output, summaries the
# Claude clustering path produced, and cached lines alike, every cycle. A
# rejected line gets ONE regeneration carrying the verifier's concrete
# complaints; if it still fails, the story falls back to its representative
# headline, which IS a source headline and therefore grounded by construction.
# scripts/check_topstories.py re-runs the same check as a pre-publish gate.
# ---------------------------------------------------------------------------
# Gemini's free tier is only ~20 requests/day PER MODEL. The headline snippets
# in fetch_headlines.py already spend flash-lite's daily budget every run, so a
# summary call on flash-lite just 429s (RESOURCE_EXHAUSTED) and the Top-5 falls
# back to raw outlet headlines. gemini-2.5-flash's budget, by contrast, is spent
# only by the World Briefing (≈4 runs/day), leaving real headroom — so summaries
# actually generate here. The neutral summary is a best-effort enhancement over
# the representative headline, which pick_representative_heuristic already keeps
# neutral and mainstream, so a quota miss degrades gracefully rather than badly.
SUMM_MODEL = os.environ.get("TOPSTORIES_SUMMARY_MODEL", "gemini-2.5-flash")
# Bump when the summary PROMPT changes so cached summaries written under the old
# wording are invalidated and regenerated (the version keys the cache).
SUMM_PROMPT_VERSION = "v6-grounded"
# Headlines shown to the generator per story, capped. Selection guarantees the
# newest item of EVERY outlet gets in first, so each outlet — and therefore
# each regional camp — is represented, not just whoever published last.
GEN_MAX_MEMBERS = 12


def _story_cache_key(story: dict) -> str:
    """Per-story cache key: prompt version + representative + newest member.
    A new development joining the cluster changes the newest member and thus
    the key, so quota is spent on real developments, not on every run. Cached
    lines are ALSO re-verified against the current members each cycle, so no
    cache hit can keep serving a line the sources no longer support."""
    members = story.get("members") or []
    parts = [SUMM_PROMPT_VERSION,
             (story.get("rep") or {}).get("url", ""),
             (members[0].get("url", "") if members else "")]
    return sha256("|".join(parts).encode("utf-8")).hexdigest()


def _prompt_members(story: dict) -> list[dict]:
    """Members shown to the generator: the newest item per outlet first (every
    outlet and camp represented), then the rest, newest-first overall, capped
    at GEN_MAX_MEMBERS."""
    members = [m for m in story.get("members", []) if m.get("title")]
    seen, primary, rest = set(), [], []
    for m in members:                       # members arrive newest-first
        (rest if m.get("source") in seen else primary).append(m)
        seen.add(m.get("source"))
    sel = (primary + rest)[:GEN_MAX_MEMBERS]
    sel.sort(key=lambda m: _parse_ts(m.get("published")), reverse=True)
    return sel


def _age_str(published: str, now: float) -> str:
    t = _parse_ts(published)
    if not t:
        return "age unknown"
    h = max(0.0, now - t) / 3600.0
    return f"{h * 60:.0f} min ago" if h < 1 else f"{h:.0f}h ago"


def _story_block(i: int, story: dict, now: float, rejected=None) -> str:
    """One story's section of the generation prompt: outlet count, camps, and
    each selected headline tagged [outlet · camp · age], newest first. For a
    retry, `rejected` = (previous_line, [problems]) appends the fact-check
    complaints so the model can fix exactly what failed."""
    camps = ", ".join(story.get("categories") or []) or "unknown"
    lines = []
    for j, m in enumerate(_prompt_members(story)):
        tag = "   <-- MOST RECENT (take the story's current status from here)" if j == 0 else ""
        camp = m.get("category") or "other"
        lines.append(f"- [{m.get('source', '?')} · {camp} · "
                     f"{_age_str(m.get('published', ''), now)}] {m['title'].strip()}{tag}")
    block = (f"STORY {i} — carried by {story.get('outlets', '?')} outlets across "
             f"these camps: {camps}\n" + "\n".join(lines))
    if rejected:
        prev, problems = rejected
        block += ("\n\nNOTE: your previous line for this story was REJECTED by an "
                  f"automated fact-check against the headlines above.\nPrevious line: {prev!r}\n"
                  "Problems:\n" + "\n".join(f"  - {p}" for p in problems) +
                  "\nWrite a corrected line that fixes every problem, using ONLY "
                  "claims present in the headlines above.")
    return block


def _summary_prompt(stories: list[dict], now: float, rejected: dict | None = None) -> str:
    """The full generation prompt. `rejected`: {index-in-stories: (line,
    problems)} on the retry pass. The rules encode the site's title policy —
    consensus topic, status from the newest sources, sourced claims only,
    cross-camp neutrality — and the deterministic verifier in
    title_grounding.py enforces the verifiable ones after generation."""
    blocks = [_story_block(i + 1, s, now, (rejected or {}).get(i))
              for i, s in enumerate(stories)]
    return (
        f"You are a neutral newswire editor writing the display titles for the "
        f"day's top {len(stories)} Middle-East stories. Each STORY below is a "
        "cluster of headlines about the SAME event from outlets across different "
        "regional camps (israeli, gulf, panarab, iranian, levant, turkish, intl), "
        "listed newest-first with outlet, camp and age.\n\n"
        "For EACH story write ONE short, factual, strictly NEUTRAL headline line "
        "in English plus its natural Hebrew translation. Hard rules:\n"
        "1. TOPIC = THE CONSENSUS. Write the event the cluster as a whole is "
        "covering — what most outlets report. A single outlet's newest statement, "
        "reaction or side angle is NOT the story; at most it may close the line "
        "('…; Tehran rejects Qatari claim').\n"
        "2. STATUS FROM THE NEWEST. Take the event's current status and tense "
        "from the most recent headlines: planned stays planned, under way is "
        "under way, finished is finished. If an older headline calls the event "
        "upcoming ('to host', 'selected to host', 'set to meet') but a newer one "
        "shows it happening or done, write the CURRENT state — never the "
        "superseded one, and never upgrade a plan into a completed act.\n"
        "3. ONLY WHAT THE SOURCES SAY. Every actor, action, place, number and "
        "outcome in your line must appear in that story's listed headlines. Do "
        "not infer, add or resolve anything yourself. A detail outlets disagree "
        "on: attribute it ('Hamas says…', 'the Israeli military says…') or omit "
        "it.\n"
        "4. NEUTRAL ACROSS CAMPS. Where camps frame the event differently, state "
        "the underlying event plainly (what happened) and attribute each side's "
        "interpretation; never adopt one camp's framing or vocabulary. Plain "
        "terms only: 'settlers' not 'colonists'; \"Iran's late supreme leader "
        "Khamenei\", never an honorific like 'martyred Leader'; "
        "'fighters'/'militants' per context, not 'terrorists' or 'heroes'. A "
        "side's rhetoric ('vengeance', 'victory') is never stated as fact — drop "
        "it or attribute it. No praise, no condemnation, no scare-quotes, no "
        "adjectives of judgement.\n"
        "5. NEVER make an outlet the subject of the line ('Al Jazeera reports "
        "on…') — report the event, not the coverage of it.\n"
        "6. MAKE IT PUNCHY: active voice, concrete nouns, a strong verb early, "
        "name the main actors, max ~16 words. The Hebrew line must be equally "
        "neutral, natural, accurate and journalistic.\n\n"
        f"Return ONLY a JSON array of exactly {len(stories)} objects, in the "
        'same order, each {"en": "...", "he": "..."}. No prose, no code fences.\n\n'
        + "\n\n".join(blocks)
    )


def _gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
    except ImportError:
        print("google-genai not installed — skipping neutral summaries", file=sys.stderr)
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception as exc:
        print(f"Gemini client init failed ({exc}) — skipping neutral summaries", file=sys.stderr)
        return None


def _gemini_summaries(client, stories: list[dict], now: float,
                      rejected: dict | None = None):
    """One Gemini call: a neutral {en, he} line per story, aligned to `stories`.
    Returns the parsed list, or None on any failure."""
    prompt = _summary_prompt(stories, now, rejected)
    text = ""
    for attempt in range(3):
        try:
            resp = client.models.generate_content(model=SUMM_MODEL, contents=prompt)
            text = (resp.text or "").strip()
            if text:
                break
        except Exception as exc:
            msg = str(exc)
            if any(c in msg for c in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500")) \
                    and attempt < 2:
                time.sleep(4 * (2 ** attempt))
                continue
            print(f"  [summaries] Gemini failed: {exc}", file=sys.stderr)
            return None
    if not text:
        return None

    m = re.search(r"\[.*\]", text, re.S)   # tolerate stray preamble / ``` fences
    if not m:
        print("  [summaries] no JSON array in Gemini output", file=sys.stderr)
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception as exc:
        print(f"  [summaries] could not parse Gemini JSON ({exc})", file=sys.stderr)
        return None
    if not isinstance(arr, list) or len(arr) < len(stories):
        return None
    return [{"en": (o.get("en") or "").strip(), "he": (o.get("he") or "").strip()}
            if isinstance(o, dict) else {"en": "", "he": ""}
            for o in arr[:len(stories)]]


def _generate_verified(client, stories: list[dict], now: float):
    """Generate a GROUNDED {en, he} line per story: generate → verify each line
    against the story's members → regenerate the rejects ONCE, feeding the
    verifier's concrete complaints back into the prompt → verify again.
    Returns a list aligned to `stories` in which a still-unverifiable story
    gets {"en": "", "he": ""} (= keep the representative headline), or None if
    the API was unavailable."""
    arr = _gemini_summaries(client, stories, now)
    if arr is None:
        return None
    out, rejects = [], {}
    for k, (s, ent) in enumerate(zip(stories, arr)):
        res = verify_title(ent["en"], s.get("members", []))
        if ent["en"] and res.ok:
            out.append(ent)
        else:
            print(f"  [summaries] rejected ({'; '.join(res.problems)}): {ent['en']!r}",
                  file=sys.stderr)
            out.append({"en": "", "he": ""})
            rejects[k] = (ent["en"], res.problems)
    if rejects:
        retry_idx = sorted(rejects)
        arr2 = _gemini_summaries(client, [stories[k] for k in retry_idx], now,
                                 rejected={j: rejects[k] for j, k in enumerate(retry_idx)})
        for j, k in enumerate(retry_idx):
            ent = (arr2[j] if arr2 and j < len(arr2) else None) or {"en": "", "he": ""}
            res = verify_title(ent["en"], stories[k].get("members", []))
            if ent["en"] and res.ok:
                out[k] = ent
            else:
                print("  [summaries] still unverified after retry — story keeps its "
                      f"representative headline: {ent['en']!r}", file=sys.stderr)
    return out


def attach_neutral_summaries(stories: list[dict]) -> None:
    """Attach a verified-neutral summary / summary_he to each Top-5 story.
    EVERY title path is grounded here: cluster-time (Claude) summaries, cached
    lines and fresh Gemini output all pass title_grounding.verify_title against
    the story's CURRENT members before they are used; whatever fails is
    regenerated once and otherwise dropped, leaving the representative headline
    (a real source headline) to stand. Best-effort: with no API key the stories
    simply keep their representative headlines."""
    if not stories:
        return
    now = datetime.now(timezone.utc).timestamp()

    # 1. Cluster-time (Claude-path) summaries get no free pass.
    for s in stories:
        en = (s.get("summary") or "").strip()
        if not en:
            s.pop("summary", None)
            continue
        res = verify_title(en, s.get("members", []))
        if not res.ok:
            print(f"  [summaries] cluster-time summary failed grounding "
                  f"({'; '.join(res.problems)}) — regenerating: {en!r}", file=sys.stderr)
            s.pop("summary", None)
            s.pop("summary_he", None)

    # 2. Per-story cache. A hit is re-verified against the CURRENT members; an
    #    entry with en == "" records "no verifiable line exists for this
    #    composition", so quota isn't re-burned until the story evolves (its
    #    key changes with the newest member).
    cache = _load_json(SUMM_CACHE)
    entries = cache.get("entries") if cache.get("version") == SUMM_PROMPT_VERSION else None
    entries = entries if isinstance(entries, dict) else {}
    keep: dict[str, dict] = {}
    need: list[int] = []
    for i, s in enumerate(stories):
        if s.get("summary"):
            continue                        # verified cluster-time line stands
        key = _story_cache_key(s)
        ent = entries.get(key)
        if isinstance(ent, dict) and "en" in ent:
            en = (ent.get("en") or "").strip()
            if not en:                      # cached "unfixable" marker
                keep[key] = {"en": "", "he": ""}
                continue
            if verify_title(en, s.get("members", [])).ok:
                keep[key] = ent
                s["summary"] = en
                if ent.get("he"):
                    s["summary_he"] = ent["he"]
                continue
            print(f"  [summaries] cached line no longer verifies — regenerating: {en!r}",
                  file=sys.stderr)
        need.append(i)

    # 3. Generate (and verify) lines for the stories that still need one.
    if need:
        client = _gemini_client()
        results = _generate_verified(client, [stories[i] for i in need], now) \
            if client else None
        if results is not None:
            for i, ent in zip(need, results):
                keep[_story_cache_key(stories[i])] = ent
                if ent.get("en"):
                    stories[i]["summary"] = ent["en"]
                    if ent.get("he"):
                        stories[i]["summary_he"] = ent["he"]
        # API unavailable → nothing cached for these stories; reps stand and
        # the next cycle tries again.

    # 4. Persist the cache — only this cycle's keys, so the file stays tiny —
    #    and only when its content actually changed (avoids a no-op git diff).
    new_cache = {"version": SUMM_PROMPT_VERSION, "entries": keep}
    if keep and new_cache != cache:
        SUMM_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SUMM_CACHE.write_text(
            json.dumps(new_cache, ensure_ascii=False, indent=2), encoding="utf-8")


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

    # Replace loaded outlet phrasing with a neutral EN+HE line per story.
    attach_neutral_summaries(stories)

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
