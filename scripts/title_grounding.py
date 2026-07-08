#!/usr/bin/env python3
"""Deterministic grounding verification for Top-5 story titles.

WHY THIS EXISTS: the Top-5 strip's story titles are LLM-written one-liners, and
prompt instructions alone ("fact-check your line", "add no specifics") proved
unreliable — e.g. a title once claimed Turkey "is selected to host" a NATO
summit that was already under way. This module is the structural fix: a pure
Python, no-API verifier that checks every candidate title against the ACTUAL
headlines (+ English snippets) of the cluster it summarises, and rejects any
title that asserts something the current sources don't support.

It is the single source of truth for title acceptance and is used from three
places, so no title path escapes it:
  * scripts/build_topstories.py — verifies every generated line (Gemini AND
    Claude paths, fresh AND cached) before it is attached; failures trigger one
    regeneration with the verifier's concrete complaints, then fall back to the
    representative headline (a real source headline, grounded by construction).
  * scripts/check_topstories.py — the pre-publish gate in the workflow: strips
    any summary already in top_stories.json that no longer verifies.
  * tests/ — regression cases (including the NATO one) that pin the behaviour.

WHAT IS CHECKED (verify_title):
  1. ENTITY GROUNDING — every named entity in the title (proper nouns, folded
     for case/diacritics, matched with aliases: Türkiye/Turkey, Tehran/Iran…)
     must appear in at least one member's title or snippet.
  2. NUMBER GROUNDING — every digit-figure in the title must appear in a source.
  3. STATUS / TENSE vs RECENCY — a title may not use anticipatory framing
     ("to host", "set to meet") when any member already reports the event as
     current or finished, and may not assert completion ("concluded", "ended")
     unless a source does. This generalises the one-off NATO regex patch.
  4. NEUTRALITY — loaded/partisan vocabulary ("martyred", "colonists",
     "the regime"…) is rejected outright.
  5. SINGLE-OUTLET FRAMING — a title whose grammatical subject is one of the
     cluster's own outlets ("The New Arab reports on…") is rejected: the story
     is the event, not one outlet's coverage of it.

Verification reads title + snippet of EVERY member (the English snippet carries
the signal for Arabic/Persian/Hebrew-titled outlets), so grounding works across
languages. Deliberately precision-biased: sentence-initial capitalised words
are only treated as entities when they are known names, and generic role words
(President, Minister…) are exempt — a false rejection costs a regeneration or a
fallback to a real headline, but a false pass ships a wrong title.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Framing lexicons. Shared with build_topstories.py (representative selection)
# and check_topstories.py — EDIT HERE, in one place only.
# ---------------------------------------------------------------------------
# Anticipatory / selection framing that goes STALE once the event is under way —
# e.g. "Turkey to host the 2026 NATO summit" is fine before the summit but wrong
# once it has opened.
STALE_FRAME_RE = re.compile(
    r"\b(to\s+host|will\s+host|set\s+to\s+host|selected\s+to\s+host|"
    r"picked\s+to\s+host|chosen\s+to\s+host|to\s+be\s+held|will\s+be\s+held|"
    r"to\s+take\s+place|set\s+to\s+(?:begin|open|start|meet|convene|kick)|"
    r"prepares?\s+(?:to|for)|gears?\s+up|ahead\s+of|expected\s+to|due\s+to|"
    r"to\s+attend|to\s+visit|to\s+meet)\b", re.I)
# Present / ongoing / concluded framing that supersedes the anticipatory one.
CURRENT_FRAME_RE = re.compile(
    r"\b(under\s?way|gets?\s+under\s?way|kicks?\s+off|kicked\s+off|opens?|"
    r"opened|begins?|began|commences?|commenced|convenes?|convened|meets?|met|"
    r"arrives?|arrived|welcomes?|welcomed|gathers?|gathered|attends?|attended|"
    r"holds?\s+talks|live|hosts|hosting|hosted|wraps?\s+up|"
    r"concludes?|concluded|ends?|ended|signs?|signed|killed|struck)\b", re.I)
# Concluded/finished framing — a title may only assert these when a source does.
COMPLETED_RE = re.compile(
    r"\b(concluded?s?|wrap(?:s|ped)?\s+up|came\s+to\s+an?\s+(?:end|close)|"
    r"has\s+ended|have\s+ended|ended|finali[sz]ed?s?|sealed)\b", re.I)

# Partisan / loaded wording — never acceptable in a strip title, whichever
# outlet (or model) produced it.
LOADED_RE = re.compile(
    r"\b(martyr(?:ed|s|dom)?|death\s+towers?|colonists?|the\s+regime|terrorists?|"
    r"zionist|vengeance|heroic|brutal|barbaric|savage|cowardly|massacre|"
    r"slaughter|genocidal|apartheid|crusaders?|infidels?|the\s+enemy|"
    r"aggression|usurper(?:s|ing)?|occupier(?:s)?|mercenaries|puppet(?:s)?|"
    r"criminal\s+entity|zionist\s+entity)\b", re.I)

# A title whose subject is one of the cluster's own OUTLETS ("The New Arab
# reports on …") — single-source framing dressed up as the story itself.
_OUTLET_VERBS = r"(?:reports?|reported|says?|said|claims?|claimed|writes?|wrote|publishes?|published|reveals?|revealed)"


# ---------------------------------------------------------------------------
# Text folding + entity vocabulary.
# ---------------------------------------------------------------------------
def fold(text: str) -> str:
    """Casefold + strip diacritics + normalise apostrophes, so 'Türkiye' matches
    'Turkiye' and 'Erdoğan' matches 'Erdogan'."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.replace("’", "'").replace("‘", "'").casefold()


def _strip_possessive(tok: str) -> str:
    return re.sub(r"'s$", "", tok)


# Alias groups: any member of a group counts as support for any other. Single
# words are matched as tokens; multi-word entries as substrings of the folded
# source text. Keep SMALL and safe — the prefix rule below already covers
# regular inflections (iran/iranian, qatar/qatari, palestin-…).
ALIAS_GROUPS: list[set[str]] = [
    {"turkiye", "turkey", "turkish", "ankara"},
    {"iran", "iranian", "tehran"},
    {"israel", "israeli"},
    {"us", "usa", "u.s", "united states", "american", "washington"},
    {"uk", "britain", "british", "united kingdom", "london"},
    {"un", "united nations"},
    {"eu", "european union", "brussels"},
    {"russia", "russian", "moscow"},
    {"ukraine", "ukrainian", "kyiv", "kiev"},
    {"saudi", "saudi arabia", "riyadh"},
    {"qatar", "qatari", "doha"},
    {"gaza", "gazan"},
    {"houthi", "houthis", "ansar allah", "ansarallah"},
    {"hezbollah", "hizbullah", "hizballah"},
    {"khamenei", "ayatollah"},
    {"palestinian", "palestinians", "palestine"},
]
_ALIAS_OF: dict[str, set[str]] = {}
for _g in ALIAS_GROUPS:
    for _a in _g:
        _ALIAS_OF.setdefault(_a, set()).update(_g)

# Known single-token entities: ALSO checked when they open the sentence (a
# sentence-initial capital is otherwise ambiguous, so unknown ones are skipped).
KNOWN_ENTITY_TOKENS = {a for g in ALIAS_GROUPS for a in g if " " not in a} | set("""
hamas hezbollah houthi houthis yemen yemeni syria syrian damascus lebanon
lebanese beirut erdogan khamenei netanyahu trump putin pezeshkian araghchi
nato hormuz egypt egyptian cairo jordan amman iraq iraqi baghdad najaf karbala
idf irgc knesset kurdish kurds venezuela caracas jerusalem ramallah jenin
nablus hebron rafah maliki pavel zelensky zelenskyy afghanistan pakistan
""".split())

# Generic capitalised words that are NOT grounding-checked: roles, honorifics,
# institutions-as-role, calendar words, directions — they routinely appear in a
# title without appearing verbatim in any source headline.
GENERIC_CAPS = set("""
the a an and but for nor with from into over under after before during amid
mr mrs ms dr president prime minister ministers premier chancellor chief
leader leaders king queen prince princess crown sheikh emir sultan general
colonel commander spokesman spokeswoman spokesperson envoy ambassador
secretary deputy vice head officials official government parliament senate
house court ministry army forces navy police foreign defense defence interior
supreme national international regional strait sea gulf river valley mount
city province state states republic kingdom emirates union council summit
assembly committee commission agency organisation organization
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november
december north south east west northern southern eastern western middle new
live breaking video watch update
""".split())


# ---------------------------------------------------------------------------
# Claim extraction from a candidate title.
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ]+(?:['’][A-Za-z]+)?")


def extract_entities(title: str) -> list[str]:
    """Folded named-entity tokens in the title that must be grounded in the
    sources. Capitalised mid-sentence words count; sentence-initial words count
    only when they are KNOWN entity tokens; generic role/calendar words never
    count. Precision-biased by design (see module docstring)."""
    ents, seen = [], set()
    for i, m in enumerate(_WORD_RE.finditer(title or "")):
        w = m.group(0)
        if not w[0].isupper():
            continue
        f = _strip_possessive(fold(w))
        if len(f) < 2 or f in GENERIC_CAPS or f in seen:
            continue
        if i == 0 and f not in KNOWN_ENTITY_TOKENS:
            continue        # ambiguous sentence-initial capital — skip
        seen.add(f)
        ents.append(f)
    return ents


def extract_numbers(title: str) -> list[str]:
    """Digit figures in the title (counts, dates, ordinals), comma-normalised."""
    return [n.replace(",", "") for n in re.findall(r"\d[\d,]*", title or "")]


def title_frame(title: str) -> str | None:
    """'anticipatory' | 'current' | None. Mirrors the long-standing rule: a
    title is anticipatory only when it has stale framing AND no current cue."""
    t = title or ""
    if CURRENT_FRAME_RE.search(t):
        return "current"
    if STALE_FRAME_RE.search(t):
        return "anticipatory"
    return None


# ---------------------------------------------------------------------------
# Verification.
# ---------------------------------------------------------------------------
@dataclass
class GroundingResult:
    ok: bool
    problems: list[str] = field(default_factory=list)

    def __bool__(self):
        return self.ok


def _member_text(m: dict) -> str:
    return f"{m.get('title') or ''} {m.get('snippet') or ''}"


def _entity_supported(ent: str, corpus_text: str, corpus_tokens: set[str]) -> bool:
    if ent in corpus_tokens:
        return True
    # Alias groups (multi-word aliases matched as substrings of the folded text).
    for alias in _ALIAS_OF.get(ent, ()):
        if (" " in alias and alias in corpus_text) or alias in corpus_tokens:
            return True
    # Regular inflections: one token a prefix of the other, shorter side >= 4
    # chars (iran/iranian, qatar/qatari, palestin-/palestinian).
    for tok in corpus_tokens:
        short, long_ = (ent, tok) if len(ent) <= len(tok) else (tok, ent)
        if len(short) >= 4 and long_.startswith(short):
            return True
    return False


def verify_title(title: str, members: list[dict]) -> GroundingResult:
    """Check a candidate story title against the cluster's ACTUAL sources.

    members: dicts with at least 'title'; 'snippet' and 'source' are used when
    present. Returns GroundingResult(ok, problems) — each problem is a concrete,
    human-readable complaint suitable for feeding back into a regeneration
    prompt ("'Qatar' is not mentioned in any source in this cluster").
    """
    problems: list[str] = []
    title = (title or "").strip()
    if not title:
        return GroundingResult(False, ["empty title"])

    corpus_text = fold(" ".join(_member_text(m) for m in members))
    corpus_tokens: set[str] = set()
    for tok in re.findall(r"[a-z0-9][a-z0-9.'-]*", corpus_text):
        tok = _strip_possessive(tok.strip(".'-"))
        corpus_tokens.add(tok)
        if "-" in tok:                      # 'al-maliki' must support 'Maliki'
            corpus_tokens.update(p for p in tok.split("-") if p)

    # 1. Entity grounding.
    for ent in extract_entities(title):
        if not _entity_supported(ent, corpus_text, corpus_tokens):
            problems.append(
                f"'{ent}' is not mentioned in any source headline/snippet in this cluster")

    # 2. Number grounding.
    corpus_digits = corpus_text.replace(",", "")
    for num in extract_numbers(title):
        if num not in corpus_digits:
            problems.append(f"the figure '{num}' appears in no source in this cluster")

    # 3. Status / tense vs the cluster's freshest framing.
    frame = title_frame(title)
    if frame == "anticipatory" and any(
            CURRENT_FRAME_RE.search(m.get("title") or "") for m in members):
        problems.append(
            "anticipatory framing (e.g. 'to host', 'set to meet') but sources already "
            "report the event as under way or finished — state the CURRENT status")
    if COMPLETED_RE.search(title) and not any(
            COMPLETED_RE.search(_member_text(m)) for m in members):
        problems.append(
            "asserts the event is concluded/ended, but no source reports it as finished")

    # 4. Loaded / partisan vocabulary.
    lm = LOADED_RE.search(title)
    if lm:
        problems.append(f"loaded/partisan term {lm.group(0)!r} — use neutral wording")

    # 5. An outlet as the story's subject ("The New Arab reports on …").
    ft = fold(title)
    for src in {m.get("source") or "" for m in members}:
        fs = fold(src)
        if fs and re.match(re.escape(fs) + r"\s+" + _OUTLET_VERBS + r"\b", ft):
            problems.append(
                f"the title presents one outlet ({src}) as the story — state the event "
                "itself, neutrally, not one outlet's coverage of it")

    return GroundingResult(not problems, problems)
