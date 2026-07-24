"""Pure stage logic for the Top-5 stories pipeline (no network, no file I/O).

The Top 5 is built as an explicit staged pipeline; each stage is a named
function with a plain-dict input/output contract so it can be tested and
debugged in isolation. scripts/build_topstories.py orchestrates the stages,
owns all I/O (headlines.json, translators, LLM calls, output files), and
documents the full flow. The stages that live HERE are the pure ones:

  normalize_stage  — force every item's display title to English (or drop it)
                     and drop items older than the freshness window
  relevance_stage  — per-item strict Top-5 relevance filter
  cluster_stage    — group items into stories (heuristic, or validated
                     LLM-provided clusters)
  rank_stage       — order clusters by distinct outlets → camp spread →
                     recency; build the story views
  validate_stage   — final gate: English-only display, minimum outlet count,
                     relevance, correct ordering

Every function is deterministic given its inputs (`now` is injectable), which
is what tests/test_topstories.py relies on.
"""
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations

# --------------------------------------------------------------------------
# Tunables (documented in scripts/build_topstories.py's module docstring).
# --------------------------------------------------------------------------
TOP_N = 5                     # stories surfaced on the site
MIN_STORY_OUTLETS = 2         # a "top" story must be carried by >= this many
                              # distinct outlets — never publish singletons
FRESHNESS_WINDOW_H = 48       # dated items older than this never enter the
                              # pipeline (staleness is solved at the input,
                              # so ranking can use plain outlet counts)
STALE_GAP_SEC = 6 * 3600      # representative must come from the newest wave
                              # of coverage (see pick_representative)

# --------------------------------------------------------------------------
# Script detection. The display contract for this feature is ENGLISH ONLY:
# a display string must contain Latin letters and no characters from any
# non-Latin letter block. Hebrew is explicitly covered — the historical bug
# was a guard that screened Arabic/Persian but deliberately let Hebrew pass.
# --------------------------------------------------------------------------
_HEBREW = re.compile(r"[֐-׿יִ-ﭏ]")
_ARABIC = re.compile(  # Arabic + Persian share these blocks
    r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_OTHER_NONLATIN = re.compile(  # Cyrillic, Greek, CJK, Devanagari, Thai, Hangul
    r"[Ѐ-ӿͰ-Ͽऀ-ॿ฀-๿"
    r"぀-ヿ一-鿿가-힯]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")
# Invisible bidi control marks that RTL feeds leave inside otherwise-English
# strings; they flip punctuation rendering in an LTR page, so strip them.
_BIDI_CONTROLS = re.compile(r"[‎‏‪-‮⁦-⁩]")


def detect_script(text: str) -> str:
    """'hebrew' | 'arabic' | 'other' | 'latin' | 'empty' for a title."""
    if not (text or "").strip():
        return "empty"
    if _HEBREW.search(text):
        return "hebrew"
    if _ARABIC.search(text):
        return "arabic"
    if _OTHER_NONLATIN.search(text):
        return "other"
    if _LATIN_LETTER.search(text):
        return "latin"
    return "other"


def is_english_display(text: str) -> bool:
    """True iff `text` is safe to show in the English UI: has Latin letters
    and not a single character from a non-Latin letter block."""
    if not text:
        return False
    return (bool(_LATIN_LETTER.search(text))
            and not _HEBREW.search(text)
            and not _ARABIC.search(text)
            and not _OTHER_NONLATIN.search(text))


# --------------------------------------------------------------------------
# Title cleaning.
# --------------------------------------------------------------------------
def _seg_matches_source(seg: str, source: str) -> bool:
    st = set(re.findall(r"[a-z]{3,}", seg.lower()))
    so = set(re.findall(r"[a-z]{3,}", (source or "").lower()))
    return bool(st & so)


def clean_title(title: str, source: str) -> str:
    """Strip bidi control marks, a leading most-read rank label ('1 ANALYSIS
    …'), and a trailing ' - <outlet>' attribution some feeds append."""
    t = _BIDI_CONTROLS.sub("", (title or "")).strip()
    # Most-read scrapes leak their list position as a leading number when the
    # next token is an ALLCAPS section label ("1 ANALYSIS …", "2 INSIGHT …").
    # Only that unambiguous shape is stripped — "5 killed in strike" is news.
    t = re.sub(r"^\d{1,2}\s+(?=[A-Z]{3,}\b)", "", t).strip()
    m = re.search(r"\s[-–—]\s([^-–—]{1,40})$", t)
    if m:
        seg = m.group(1).strip()
        if not is_english_display(seg) or _seg_matches_source(seg, source):
            t = t[:m.start()].strip()
    return t


def first_sentence(text: str, maxlen: int = 110) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    s = (m.group(1) if m else t).strip()
    if len(s) > maxlen:
        s = s[:maxlen].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return s


def parse_ts(iso) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# --------------------------------------------------------------------------
# Outlet → regional lens (camp). Kept in sync with SRC_CATS in index.html;
# EDIT BOTH PLACES when adding an outlet. Camp diversity across these is the
# secondary ranking signal (cross-camp coverage = more significant story).
# --------------------------------------------------------------------------
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
CAT_ORDER = ["israeli", "gulf", "panarab", "iranian", "levant", "turkish", "intl"]


# ==========================================================================
# Stage 2 — NORMALIZE
# ==========================================================================
def normalize_stage(items: list[dict], translate=None, now: float | None = None) -> dict:
    """Force a guaranteed-English `display_title` onto every item BEFORE any
    clustering/ranking, and drop items outside the freshness window.

    English resolution order per item:
      1. the cleaned original title, when it already passes is_english_display
         (title_source="native");
      2. a machine translation of the title via the injected `translate`
         callable (title_source="translated") — the runner wires this to the
         Azure Translator with a persistent per-title cache;
      3. the first sentence of the item's English snippet
         (title_source="snippet");
      4. no English text obtainable → the item is DROPPED from this feature
         (it still appears on the normal headlines wall).

    Nothing non-English can leave this stage: display_title is re-checked
    with is_english_display whichever branch produced it.

    `translate(texts) -> list[str|None]` is called at most once, with the
    batch of titles that need it. `now` (epoch seconds) is injectable so the
    freshness window is testable.

    Returns {"items": [...], "dropped": [{source,title,reason}], "stats": {}}.
    """
    if now is None:
        now = datetime.now(timezone.utc).timestamp()

    kept, dropped = [], []
    stats = Counter()

    # Pass 1: clean titles, staleness window, split by script.
    pending = []      # items surviving staleness, in order
    need_tr = []      # indices (into pending) that need translation
    for it in items:
        cleaned = clean_title(it.get("title", ""), it.get("source", ""))
        ts = it.get("_t", 0.0)
        if ts and (now - ts) > FRESHNESS_WINDOW_H * 3600:
            dropped.append({"source": it.get("source"), "title": cleaned[:120],
                            "reason": f"stale (older than {FRESHNESS_WINDOW_H}h)"})
            stats["dropped_stale"] += 1
            continue
        norm = dict(it)
        norm["title"] = cleaned
        norm["title_he"] = clean_title(it.get("title_he", ""), it.get("source", ""))
        norm["title_lang"] = detect_script(cleaned)
        pending.append(norm)
        if not is_english_display(cleaned):
            need_tr.append(len(pending) - 1)

    # Pass 2: one batched translation call for everything non-English.
    translations = {}
    if need_tr and translate is not None:
        results = translate([pending[i]["title"] for i in need_tr])
        for i, tr in zip(need_tr, results or []):
            translations[i] = tr

    # Pass 3: resolve display_title, English-verified whatever the source.
    for i, it in enumerate(pending):
        if is_english_display(it["title"]):
            it["display_title"] = it["title"]
            it["title_source"] = "native"
        else:
            tr = clean_title(translations.get(i) or "", it.get("source", ""))
            snip = first_sentence(it.get("snippet", ""))
            if tr and is_english_display(tr):
                it["display_title"] = tr
                it["title_source"] = "translated"
            elif snip and is_english_display(snip):
                it["display_title"] = snip
                it["title_source"] = "snippet"
            else:
                dropped.append({"source": it.get("source"), "title": it["title"][:120],
                                "reason": "no English text obtainable (title/translation/snippet)"})
                stats["dropped_no_english"] += 1
                continue
        stats[f"title_{it['title_source']}"] += 1
        kept.append(it)

    stats["in"], stats["kept"] = len(items), len(kept)
    return {"items": kept, "dropped": dropped, "stats": dict(stats)}


# ==========================================================================
# Stage 3 — RELEVANCE (strict, per item, Top-5 specific)
# ==========================================================================
# The feed-level filter in fetch_headlines.py is deliberately high-precision
# (drops only unambiguous sports/ads/lifestyle). Top 5 is the site's most
# visible surface, so this SECOND pass is stricter and per-item: every item
# entering clustering must itself carry a security/geopolitics signal that is
# not outweighed by off-topic signals. This is what keeps a soft item (exam
# results, human interest, listicles) from riding into a Top-5 cluster.
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
detained detention insurgent militiamen intercepted interception intercept intercepts
attack attacks attacked attackers struck shot shooting shootings injured condemn condemns condemned
condemnation target targets targeted targeting
""".split())
SEC_PHRASES = (
    "air strike", "west bank", "gaza strip", "strait of hormuz", "red sea", "security council",
    "foreign minister", "defense minister", "defence minister", "war crimes", "death toll",
    "peace deal", "prisoner exchange", "ground offensive", "revolutionary guard", "islamic jihad",
    "peace talks", "war on", "human rights", "aid convoy", "arms deal", "air defence", "air defense",
    "shot down", "self-defense", "self-defence", "al-aqsa", "al aqsa", "frozen assets",
    "acts of aggression",
)
OFF_WORDS = set("""
football soccer fifa uefa afcon match matches league tournament striker goalkeeper goals penalty
penalties coach olympics medal medals championship cricket tennis basketball app apps iphone android
google apple microsoft meta whatsapp tiktok website websites login logins password startup startups
gadget smartphone smartphones ecommerce streaming stock stocks shares bourse ipo dividend
cryptocurrency bitcoin crypto recipe recipes watermelon cuisine celebrity movie movies film films
cinema actor actress singer concert festival fashion wedding horoscope zodiac diet skincare tourism
tourist weather forecast rainfall exam exams graduation restaurant restaurants cafe chef menu
""".split())
OFF_PHRASES = (
    "broadcast regulator", "broadcasting authority", "supreme court", "high court", "court of appeal",
    "attorney general", "box office", "red carpet", "stock market", "interest rate", "exchange rate",
    "world cup", "champions league", "transfer window", "oil output", "output hike", "judicial",
    # education / listicle / human-interest leakage seen in production
    "school exams", "exam results", "general secondary", "secondary school certificate",
    "high school exam", "school certificate", "top students", "top ten students",
    "achieved a score", "achieved an average", "best restaurants", "things to do",
    "found safe and sound",
)
# Natural disasters / accidents read as "security" via casualty words but are
# off-topic UNLESS a real conflict dimension is present.
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


def relevance_scores(text: str) -> dict:
    """Security vs off-topic signal counts for a blob of English text."""
    t = (text or "").lower()
    toks = set(re.findall(r"[a-z]+", t))
    sec = len(toks & SEC_WORDS) + sum(p in t for p in SEC_PHRASES)
    off = len(toks & OFF_WORDS) + sum(p in t for p in OFF_PHRASES)
    disaster = bool(toks & DISASTER_WORDS) and not (
        (toks & CONFLICT_WORDS) or any(p in t for p in CONFLICT_PHRASES))
    return {"sec": sec, "off": off, "disaster_only": disaster}


def item_is_relevant(item: dict) -> tuple[bool, dict]:
    """Strict per-item gate: the item's own text must carry >=1 security
    signal, not be outweighed by off-topic signals, and not be a pure
    disaster/accident story."""
    s = relevance_scores(f"{item.get('display_title', '')} {item.get('snippet', '')}")
    if s["disaster_only"]:
        return False, {**s, "reason": "disaster/accident with no conflict dimension"}
    if s["sec"] < 1:
        return False, {**s, "reason": "no security/geopolitics signal"}
    if s["off"] > s["sec"]:
        return False, {**s, "reason": "off-topic signals outweigh security signals"}
    return True, s


def cluster_is_relevant(members: list[dict]) -> bool:
    """Cluster-level check over all members' combined text (title+snippet).
    Used as a belt-and-suspenders re-check at rank and validate time."""
    text = " ".join(f"{m.get('display_title', m.get('title', ''))} {m.get('snippet', '')}"
                    for m in members)
    s = relevance_scores(text)
    return not s["disaster_only"] and s["sec"] >= 1 and s["sec"] >= s["off"]


def relevance_stage(items: list[dict]) -> dict:
    """Apply item_is_relevant to every normalized item.
    Returns {"items": kept, "dropped": [{source,title,sec,off,reason}]}."""
    kept, dropped = [], []
    for it in items:
        ok, s = item_is_relevant(it)
        if ok:
            kept.append(it)
        else:
            dropped.append({"source": it.get("source"), "title": it.get("display_title", "")[:120],
                            "sec": s["sec"], "off": s["off"], "reason": s["reason"]})
    return {"items": kept, "dropped": dropped}


# ==========================================================================
# Stage 4 — CLUSTER
# ==========================================================================
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

# Named-entity gazetteer. STRONG entities are specific events/people/places —
# sharing one is good evidence of the same story. BROAD entities are
# nation-level lenses that co-occur across unrelated stories and must never,
# alone, glue two headlines together. EDIT HERE to sharpen entity resolution.
ENTITIES_STRONG = {
    "khamenei": ["khamenei", "ayatollah", "supreme leader", "slain leader", "martyred leader",
                 "revolution leader"],
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
    "boeing": ["boeing", "saudia"],
    # Nation-TARGETS of a cross-border strike wave. These states surface
    # rarely; when a wave hits them nearly every headline naming one is the
    # same story. Matched TITLE-ONLY (below) to dodge byline/dateline noise.
    "jordan":  ["jordan", "jordanian", "azraq", "amman", "jaf", "jordan armed forces"],
    "kuwait":  ["kuwait", "kuwaiti"],
    "bahrain": ["bahrain", "bahraini", "manama"],
    "qatar":   ["qatar", "qatari", "doha", "al udeid", "al-udeid"],
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
# Country names leak into snippets as outlet bylines/datelines ("Jordan
# Times", "TEHRAN (MNA)"), so nation-target entities anchor on the title only.
TITLE_ONLY_ENTITIES = {"jordan", "kuwait", "bahrain", "qatar"}
_ENT_RE = {
    canon: [re.compile(r"\b" + re.escape(a).replace(r"\ ", r"\s+") + r"\b", re.I) for a in aliases]
    for canon, aliases in ENTITIES.items()
}


def _tokens(text: str) -> set:
    words = re.findall(r"[a-z][a-z-]{3,}", (text or "").lower())
    return {w for w in words if w not in STOP}


# Country/region words carry no event information — in this feed nearly every
# headline names one, so shared geography must never be the only evidence for
# folding a straggler into a story (that's how exam results glued onto a
# West-Bank cluster: both said "Gaza" and "West Bank").
GEO_TOKENS = set("""
gaza west bank israel israeli israelis iran iranian iranians tehran syria syrian yemen yemeni
lebanon lebanese turkey turkish russia russian ukraine ukrainian jordan jordanian kuwait kuwaiti
bahrain bahraini qatar qatari washington american united states palestinian palestinians
""".split())


def _entities(text: str) -> set:
    found = set()
    for canon, regexes in _ENT_RE.items():
        if any(r.search(text or "") for r in regexes):
            found.add(canon)
    return found


def _item_entities(it: dict) -> set:
    """Entities over display_title + snippet (all English post-normalize).
    TITLE_ONLY_ENTITIES are credited only when present in the title itself."""
    ents = _entities(f"{it['display_title']} {it.get('snippet', '')}")
    if ents & TITLE_ONLY_ENTITIES:
        title_ents = _entities(it["display_title"])
        ents = (ents - TITLE_ONLY_ENTITIES) | (title_ents & TITLE_ONLY_ENTITIES)
    return ents


def cluster_heuristic(items: list[dict]) -> list[list[int]]:
    """Group items into stories without letting one bridging headline fuse
    two unrelated events (the naive single-linkage trap).

    1. Strong entities that genuinely CO-OCCUR across >=2 headlines (and in
       >=40% of the rarer one's headlines) merge into a topic anchor.
    2. Each item joins its best-fit anchor (most of its strong entities land
       there; rarer entity breaks ties).
    3. Within an anchor, distinct sub-events split by title-word overlap.
       A leftover single item folds into the sub-event it matches best ONLY
       if it shows real affinity to some member (>=2 shared entities, or a
       shared strong entity plus >=2 shared title words). Without that
       affinity it stays alone — better an honest singleton (which the
       min-outlet rule then excludes) than an inflated cluster.
    4. Items with no known entity cluster among themselves by near-duplicate
       title only (>=4 shared informative words).
    """
    n = len(items)
    all_ents = [_item_entities(it) for it in items]
    strong = [e & STRONG for e in all_ents]
    title_tok = [_tokens(it["display_title"]) for it in items]

    # 1. Co-occurrence anchors over strong entities.
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

    # 2. Best-fit anchor per item.
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

    # 3. Split each anchor into distinct events; fold stragglers only on
    #    real EVENT-LEVEL affinity: a shared strong entity plus >=2 shared
    #    non-geographic title words (or >=3 such words outright). Shared
    #    geography alone is never enough.
    event_tok = [t - GEO_TOKENS for t in title_tok]

    def _affine(s, m):
        shared = len(event_tok[s] & event_tok[m])
        return (shared >= 3
                or (len(strong[s] & strong[m]) >= 1 and shared >= 2))

    def subsplit(idxs):
        if len(idxs) <= 2:
            return [idxs]
        parent = {i: i for i in idxs}

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        # Shared geography (event_tok excludes it) never merges two items on
        # its own — "…in Gaza and the West Bank" must not glue an education
        # story onto a West-Bank-violence story.
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                if (len(event_tok[i] & event_tok[j]) >= 3
                        or (len(all_ents[i] & all_ents[j]) >= 2
                            and len(event_tok[i] & event_tok[j]) >= 1)):
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[ri] = rj
        groups = defaultdict(list)
        for i in idxs:
            groups[find(i)].append(i)
        dense = [g for g in groups.values() if len(g) >= 2]
        singles = [g[0] for g in groups.values() if len(g) == 1]
        if not dense:
            return [idxs]                       # one coherent story — keep whole
        dense.sort(key=len, reverse=True)
        out_singles = []
        for s in singles:
            best, best_score = None, -1
            for g in dense:
                if not any(_affine(s, m) for m in g):
                    continue
                score = sum(2 * len(all_ents[s] & all_ents[m])
                            + len(title_tok[s] & title_tok[m]) for m in g)
                if score > best_score:
                    best, best_score = g, score
            if best is not None:
                best.append(s)
            else:
                out_singles.append([s])
        return dense + out_singles

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


def cluster_stage(items: list[dict], llm_clusters: list[dict] | None = None) -> dict:
    """Produce story clusters over the filtered items.

    If `llm_clusters` is given (the runner's validated Claude output:
    [{member_indices, representative_index, summary, summary_he}]) it is
    used as-is; otherwise the deterministic heuristic runs. Either way the
    output shape is identical, so everything downstream — including the
    validation gate — treats both paths the same.

    Returns {"clusters": [...], "method": "llm"|"heuristic"}.
    """
    if llm_clusters is not None:
        return {"clusters": llm_clusters, "method": "llm"}
    clusters = [
        {"member_indices": g, "representative_index": pick_representative(items, g)}
        for g in cluster_heuristic(items)
    ]
    return {"clusters": clusters, "method": "heuristic"}


# --------------------------------------------------------------------------
# Representative selection (which member's wording heads the cluster).
# --------------------------------------------------------------------------
_LOADED_RE = re.compile(
    r"\b(martyr(?:ed|s|dom)?|death\s+towers?|colonists?|the\s+regime|terrorists?|"
    r"zionist|vengeance|heroic|brutal|barbaric|savage|cowardly|massacre|"
    r"slaughter|genocidal|apartheid|crusaders?|infidels?|the\s+enemy|"
    r"aggression|usurper(?:s|ing)?|occupier(?:s)?|mercenaries|puppet(?:s)?|"
    r"criminal\s+entity|zionist\s+entity)\b", re.I)

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
_TITLE_SOURCE_RANK = {"native": 2, "translated": 1, "snippet": 0}

# Anticipatory framing that goes stale once the event is under way…
_STALE_FRAME_RE = re.compile(
    r"\b(to\s+host|will\s+host|set\s+to\s+host|selected\s+to\s+host|"
    r"picked\s+to\s+host|chosen\s+to\s+host|to\s+be\s+held|will\s+be\s+held|"
    r"to\s+take\s+place|set\s+to\s+(?:begin|open|start|meet|convene|kick)|"
    r"prepares?\s+(?:to|for)|gears?\s+up|ahead\s+of|expected\s+to|due\s+to|"
    r"to\s+attend|to\s+visit|to\s+meet)\b", re.I)
# …and the present/concluded framing that supersedes it.
_CURRENT_FRAME_RE = re.compile(
    r"\b(under\s?way|gets?\s+under\s?way|kicks?\s+off|kicked\s+off|opens?|"
    r"opened|begins?|began|commences?|commenced|convenes?|convened|meets?|met|"
    r"arrives?|arrived|holds?\s+talks|live|hosts|hosting|hosted|wraps?\s+up|"
    r"concludes?|concluded|ends?|ended|signs?|signed|killed|struck)\b", re.I)

_GENERIC_STUB_RE = re.compile(
    r"^(live|breaking|war on|live blog|watch|video|photos|in pictures|update|main news)\b", re.I)


def pick_representative(items: list[dict], idxs: list[int]) -> int:
    """Choose the clearest member to head the story: neutral wording, current
    (not stale-anticipatory) framing, from the newest wave of coverage,
    covering the story's dominant entities; then prefer native-English titles
    over translated/snippet-derived ones, mainstream phrasing, non-stub,
    natural length, centrality, recency."""
    strong_counts = Counter()
    for i in idxs:
        for e in _item_entities(items[i]) & STRONG:
            strong_counts[e] += 1
    top = max(strong_counts.values(), default=0)
    dominant = {e for e, c in strong_counts.items() if c >= top * 0.5}

    tok = {i: _tokens(items[i]["display_title"]) for i in idxs}
    newest_t = max((items[i].get("_t", 0.0) for i in idxs), default=0.0)
    cluster_has_current = any(_CURRENT_FRAME_RE.search(items[i]["display_title"]) for i in idxs)

    best, best_key = idxs[0], None
    for i in idxs:
        title = items[i]["display_title"]
        covers = len(_entities(title) & STRONG & dominant)
        stub = bool(_GENERIC_STUB_RE.match(title)) or len(title) < 18
        neutral = not (_LOADED_RE.search(title) or "!" in title)
        src_rank = _REP_SOURCE_RANK.get(items[i]["source"], _REP_RANK_DEFAULT)
        stale_frame = bool(_STALE_FRAME_RE.search(title)) and not _CURRENT_FRAME_RE.search(title)
        current_ok = not (stale_frame and cluster_has_current)
        fresh = (newest_t - items[i].get("_t", 0.0)) <= STALE_GAP_SEC
        native = _TITLE_SOURCE_RANK.get(items[i].get("title_source"), 0)
        length_fit = -abs(len(title) - 50)
        central = sum(len(tok[i] & tok[j]) for j in idxs if j != i)
        key = (neutral, current_ok, fresh, covers, native, src_rank,
               not stub, length_fit, central, items[i].get("_t", 0.0))
        if best_key is None or key > best_key:
            best_key, best = key, i
    return best


def summary_contradicts_recency(summary_en: str, story: dict) -> bool:
    """True if a generated summary still leads with anticipatory framing
    while a member headline already shows the event happening/finished."""
    if not summary_en:
        return False
    if not (_STALE_FRAME_RE.search(summary_en) and not _CURRENT_FRAME_RE.search(summary_en)):
        return False
    return any(_CURRENT_FRAME_RE.search(m.get("title", "")) for m in story.get("members", []))


# ==========================================================================
# Stage 5 — RANK
# ==========================================================================
def _member_view(it: dict) -> dict:
    """The public per-member shape shipped in top_stories.json. `title` is
    the guaranteed-English display title; `title_he` serves the site's HE
    mode."""
    return {
        "source": it["source"],
        "category": it.get("category"),
        "title": it["display_title"],
        "title_he": it.get("title_he", ""),
        "url": it.get("url", ""),
        "published": it.get("published", ""),
    }


def rank_stage(items: list[dict], clusters: list[dict]) -> dict:
    """Order clusters by the published ranking rule and assemble story views.

    Ranking rule (in priority order, all descending):
      1. number of DISTINCT outlets carrying the story;
      2. spread across outlet camps (categories);
      3. recency of the newest member.
    Ties beyond that break on the representative URL so runs are stable.

    Only clusters with >= MIN_STORY_OUTLETS distinct outlets AND a passing
    cluster-level relevance check qualify; the top TOP_N qualifiers are
    returned (fewer than TOP_N when supply is short — never padded).

    Returns {"stories": [...], "table": [...]} where `table` records every
    candidate cluster and why it did or didn't qualify (for the debug log).
    """
    scored = []
    for c in clusters:
        idxs = c["member_indices"]
        members = [items[i] for i in idxs]
        outlets = {m["source"] for m in members}
        cats = {m.get("category") for m in members if m.get("category")}
        newest = max((m.get("_t", 0.0) for m in members), default=0.0)
        rep = items[c["representative_index"]]
        relevant = cluster_is_relevant(members)
        scored.append({
            "cluster": c, "members": members, "rep": rep,
            "outlets": len(outlets), "cats": len(cats), "newest": newest,
            "relevant": relevant,
            "qualified": len(outlets) >= MIN_STORY_OUTLETS and relevant,
        })

    scored.sort(key=lambda s: (-s["outlets"], -s["cats"], -s["newest"],
                               s["rep"].get("url", "")))

    stories = []
    for s in scored:
        if not s["qualified"] or len(stories) >= TOP_N:
            continue
        cats = sorted({m.get("category") for m in s["members"] if m.get("category")},
                      key=lambda x: CAT_ORDER.index(x) if x in CAT_ORDER else 99)
        member_views = sorted((_member_view(m) for m in s["members"]),
                              key=lambda m: parse_ts(m["published"]), reverse=True)
        story = {
            "rank": len(stories) + 1,
            "outlets": s["outlets"],
            "categories": cats,
            "rep": _member_view(s["rep"]),
            "members": member_views,
            # internal (stripped before publish): full items for validation
            "_members_full": s["members"],
        }
        c = s["cluster"]
        if c.get("summary"):
            story["summary"] = c["summary"]
        if c.get("summary_he"):
            story["summary_he"] = c["summary_he"]
        stories.append(story)

    table = [{
        "headline": (s["cluster"].get("summary") or s["rep"]["display_title"])[:100],
        "outlets": s["outlets"], "cats": s["cats"],
        "newest": datetime.fromtimestamp(s["newest"], tz=timezone.utc).isoformat(timespec="minutes")
                  if s["newest"] else None,
        "members": len(s["members"]),
        "qualified": s["qualified"],
        "why_not": (None if s["qualified"] else
                    ("only 1 outlet" if s["outlets"] < MIN_STORY_OUTLETS else "off-topic")),
    } for s in scored]

    return {"stories": stories, "table": table}


# ==========================================================================
# Stage 6 — VALIDATE
# ==========================================================================
def validate_stage(stories: list[dict]) -> dict:
    """Final automated gate before anything is displayed. Re-verifies, from
    scratch and without trusting upstream stages:

      english   — every displayed string (summary, rep title, member titles)
                  passes is_english_display. A bad SUMMARY is repairable
                  (dropped; the guaranteed-English rep title shows instead);
                  a bad rep/member title fails the cycle.
      outlets   — every story has >= MIN_STORY_OUTLETS distinct outlets, and
                  its `outlets` field matches its members.
      relevance — no story matches the irrelevant-content filters
                  (re-checked over the full member text, snippets included).
      ordering  — stories are ordered by the ranking rule: outlet count desc,
                  then camp spread desc, then newest member desc.
      structure — 1..TOP_N contiguous ranks, non-empty members, members
                  sorted newest-first.

    Returns {"ok": bool, "failures": [...], "repairs": [...], "stories": [...]}.
    On ok=False the caller must NOT publish `stories` — it falls back to the
    last known-good output and logs `failures`.
    """
    failures, repairs = [], []
    stories = [dict(s) for s in stories]  # shallow copy; we may drop summaries

    if not stories:
        failures.append({"check": "structure", "detail": "no qualifying stories"})

    for s in stories:
        rank = s.get("rank")

        # -- english
        if s.get("summary") and not is_english_display(s["summary"]):
            repairs.append({"story": rank, "action": "dropped non-English summary",
                            "text": s["summary"][:80]})
            s.pop("summary", None)
        rep_title = (s.get("rep") or {}).get("title", "")
        if not is_english_display(rep_title):
            failures.append({"check": "english", "story": rank,
                             "detail": f"rep title not English: {rep_title[:80]!r}"})
        for m in s.get("members", []):
            if not is_english_display(m.get("title", "")):
                failures.append({"check": "english", "story": rank,
                                 "detail": f"member title not English "
                                           f"({m.get('source')}): {m.get('title', '')[:80]!r}"})

        # -- outlets
        distinct = {m.get("source") for m in s.get("members", [])}
        if len(distinct) < MIN_STORY_OUTLETS:
            failures.append({"check": "outlets", "story": rank,
                             "detail": f"{len(distinct)} outlet(s) < min {MIN_STORY_OUTLETS}"})
        if s.get("outlets") != len(distinct):
            failures.append({"check": "outlets", "story": rank,
                             "detail": f"outlets field {s.get('outlets')} != "
                                       f"{len(distinct)} distinct member sources"})

        # -- relevance (full text incl. snippets when available)
        basis = s.get("_members_full") or s.get("members", [])
        if not cluster_is_relevant(basis):
            failures.append({"check": "relevance", "story": rank,
                             "detail": "story matches irrelevant-content filters"})

        # -- structure
        if not s.get("members"):
            failures.append({"check": "structure", "story": rank, "detail": "no members"})
        ts = [parse_ts(m.get("published")) for m in s.get("members", [])]
        if any(a < b for a, b in zip(ts, ts[1:])):
            failures.append({"check": "structure", "story": rank,
                             "detail": "members not sorted newest-first"})

    # -- ranks contiguous
    if [s.get("rank") for s in stories] != list(range(1, len(stories) + 1)):
        failures.append({"check": "structure",
                         "detail": f"ranks not contiguous: {[s.get('rank') for s in stories]}"})
    if len(stories) > TOP_N:
        failures.append({"check": "structure", "detail": f"more than {TOP_N} stories"})

    # -- ordering by the ranking rule
    def sort_key(s):
        cats = {m.get("category") for m in s.get("members", []) if m.get("category")}
        newest = max((parse_ts(m.get("published")) for m in s.get("members", [])), default=0.0)
        return (len({m.get("source") for m in s.get("members", [])}), len(cats), newest)

    keys = [sort_key(s) for s in stories]
    if any(a < b for a, b in zip(keys, keys[1:])):
        failures.append({"check": "ordering",
                         "detail": f"stories not ordered by (outlets, camps, recency): "
                                   f"{[k[:2] for k in keys]}"})

    return {"ok": not failures, "failures": failures, "repairs": repairs, "stories": stories}


def strip_internal(stories: list[dict]) -> list[dict]:
    """Remove internal underscore-prefixed keys before publishing."""
    return [{k: v for k, v in s.items() if not k.startswith("_")} for s in stories]
