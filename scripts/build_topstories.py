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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
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
            best, best_score = dense[0], -1
            for g in dense:
                score = sum(2 * len(all_ents[s] & all_ents[m])
                            + len(title_tok[s] & title_tok[m]) for m in g)
                if score > best_score:
                    best, best_score = g, score
            best.append(s)
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


def build_stories(items: list[dict], clusters: list[dict]) -> list[dict]:
    """clusters: [{member_indices, representative_index, summary?, summary_he?}].
    Rank by distinct-outlet count → category diversity → recency, keep the top N
    that pass the geopolitics/security topical filter."""
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

    # Keep only on-topic stories (geopolitics / security / military / diplomacy),
    # THEN take the top N — so a widely-carried but off-topic item (a court
    # ruling, a tech fine, a cup final) can't claim a slot. Safety net: if a
    # cycle somehow has no on-topic cluster, fall back to the raw ranking rather
    # than blanking the strip.
    relevant = [s for s in scored if _is_relevant(s[2])] or scored

    out = []
    for rank, (c, idxs, members, n_outlets, n_cats, newest) in enumerate(relevant[:TOP_N], 1):
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
