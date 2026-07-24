#!/usr/bin/env python3
"""Top-5 stories builder — an explicit staged pipeline → top_stories.json.

Full reference (stages, ranking rule, thresholds, debugging, how to tune):
README-topstories.md in the repository root.

Run every refresh cycle (~30 min) by the workflow, AFTER fetch_headlines.py.
The stage logic itself lives in scripts/topstories_pipeline.py (pure,
tested by tests/test_topstories.py); this script wires the stages to the
real inputs/outputs and to the optional network services.

    1. FETCH      read headlines.json → one flat list of items
    2. NORMALIZE  force a guaranteed-English display title per item
                  (native → Azure-translated → English snippet → drop),
                  drop items older than the freshness window
    3. RELEVANCE  strict per-item Top-5 topical filter (second pass on top
                  of the feed-level filter in fetch_headlines.py)
    4. CLUSTER    group items into stories — Claude semantic clustering
                  when ANTHROPIC_API_KEY is set, else the deterministic
                  entity-co-occurrence heuristic; both feed the same gate
    5. RANK       distinct outlets → camp spread → recency; only clusters
                  with >= MIN_STORY_OUTLETS outlets qualify; top TOP_N kept
                  (fewer when supply is short — never padded)
    6. VALIDATE   final gate: English-only display, min outlet count,
                  relevance, correct ordering. A failing cycle is NEVER
                  published.
    7. PUBLISH    atomic write of top_stories.json + last-known-good copy
                  (state/topstories_lkg.json). On validation failure the
                  previous good output stays up (restored from LKG if
                  needed) and the reason is logged.

Every cycle also writes state/topstories_debug.json — a per-stage trace
(what was fetched, what each stage dropped and why, every candidate
cluster with its ranking signals, validation verdict, publish outcome) so
"why did this story rank #1 today" is answerable without guesswork.
Read it with:

    python3 scripts/explain_topstories.py            # the whole cycle
    python3 scripts/explain_topstories.py --why 1    # why story #1 ranked there
    python3 scripts/explain_topstories.py --dropped  # what was filtered, and why
    python3 scripts/explain_topstories.py --health   # recent validation failures

Cross-cycle health (consecutive failures, the last 20 failure records)
lives in state/topstories_failures.json, since the debug trace above is
overwritten every run.

Neutral one-line summaries (EN+HE) are attached best-effort via Gemini
(cached in state/topstories_summaries.json; only refreshed when the Top-5
set actually changes). A missing summary just leaves the representative
headline — which normalization already guarantees is English — in place.

index.html renders this file directly and hides the strip if it is
missing/empty; there is deliberately NO client-side fallback clusterer any
more (a second unvalidated implementation was a recurring source of
divergence).
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import topstories_pipeline as tp

ROOT = Path(__file__).parent.parent
HL_PATH = ROOT / "headlines.json"
OUT_PATH = ROOT / "top_stories.json"
LKG_PATH = ROOT / "state" / "topstories_lkg.json"
DEBUG_PATH = ROOT / "state" / "topstories_debug.json"
FAILURES_PATH = ROOT / "state" / "topstories_failures.json"
SUMM_CACHE = ROOT / "state" / "topstories_summaries.json"
EN_CACHE = ROOT / "state" / "topstories_en_cache.json"

CLAUDE_MODEL = os.environ.get("TOPSTORIES_MODEL", "claude-opus-5")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ==========================================================================
# Stage 1 — FETCH
# ==========================================================================
def fetch_stage(path: Path) -> dict:
    """Flatten headlines.json → items (each keeping its outlet/camp), plus
    per-outlet counts for the debug trace. Raises on unreadable input."""
    data = json.loads(path.read_text(encoding="utf-8"))
    items, outlets = [], {}
    for region, outs in (data.get("regions") or {}).items():
        for o in outs:
            source = o.get("source", "")
            cat = tp.SRC_CATS.get(source)
            hs = o.get("headlines") or []
            outlets[source] = {"headlines": len(hs), "lang": o.get("lang"),
                               "stale": bool(o.get("stale")), "error": o.get("error")}
            for h in hs:
                title = (h.get("title") or "").strip()
                if not title:
                    continue
                items.append({
                    "source": source,
                    "category": cat,
                    "region": region,
                    "title": title,
                    "title_he": (h.get("title_he") or "").strip(),
                    "snippet": (h.get("snippet") or "").strip(),
                    "url": h.get("url") or "",
                    "published": h.get("published") or "",
                    "_t": tp.parse_ts(h.get("published")),
                })
    return {"items": items, "outlets": outlets, "updated": data.get("updated")}


# ==========================================================================
# Stage 2 support — Azure title translation (he/ar → en), per-title cache.
# Same free-tier Azure Translator that already powers the site's HE mode;
# steady-state usage is only the handful of genuinely new non-English
# titles per cycle. No key / any failure → None per title, and normalize
# falls back to the English snippet.
# ==========================================================================
AZURE_ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"
EN_CACHE_MAX = 4000


def make_translator():
    """Returns (translate, save, status). translate(texts) -> list[str|None].

    `status` is a dict the caller copies into the debug trace. A translation
    outage is otherwise invisible — every title silently falls through to the
    snippet branch — and the CI job log is not always reachable, so the reason
    has to land in a committed file.
    """
    key = os.environ.get("AZURE_TRANSLATOR_KEY")
    region = os.environ.get("AZURE_TRANSLATOR_REGION")
    cache = _load_json(EN_CACHE)
    entries: dict = cache.get("en") or {}
    state = {"dirty": False}
    status = {"configured": bool(key), "region": region or "global",
              "requested": 0, "translated": 0, "cached": len(entries), "error": None}

    def translate(texts):
        need = [t for t in dict.fromkeys(texts) if t and t not in entries]
        status["requested"] = len(need)
        if not need:
            return [entries.get(t) for t in texts]
        if not key:
            status["error"] = "AZURE_TRANSLATOR_KEY not set"
            print(f"  [normalize] {status['error']} — {len(need)} non-English title(s) "
                  f"fall back to their English snippet", file=sys.stderr)
            return [entries.get(t) for t in texts]
        try:
            import requests
            # Field name and region default MUST match scripts/fetch_headlines.py's
            # azure_translate(): the API's body key is "Text" (capital T), and a
            # missing region header is rejected by regional keys.
            headers = {"Ocp-Apim-Subscription-Key": key,
                       "Ocp-Apim-Subscription-Region": region or "global",
                       "Content-Type": "application/json"}
            for i in range(0, len(need), 50):
                batch = need[i:i + 50]
                r = requests.post(
                    AZURE_ENDPOINT, params={"api-version": "3.0", "to": "en"},
                    headers=headers, json=[{"Text": t} for t in batch], timeout=30)
                if r.status_code >= 400:
                    # Record the API's own explanation (quota exhausted, bad key,
                    # wrong region…) — this is the line that makes the outage
                    # diagnosable from the committed trace alone.
                    status["error"] = f"HTTP {r.status_code}: {r.text[:300]}"
                    r.raise_for_status()
                for src, item in zip(batch, r.json()):
                    entries[src] = item["translations"][0]["text"]
                    state["dirty"] = True
        except Exception as exc:
            if not status["error"]:
                status["error"] = f"{type(exc).__name__}: {exc}"[:300]
            print(f"  [normalize] Azure en-translation failed — {status['error']} "
                  f"— falling back to snippets", file=sys.stderr)
        status["translated"] = sum(1 for t in need if t in entries)
        print(f"  [normalize] translated {status['translated']}/{len(need)} non-English titles",
              file=sys.stderr)
        return [entries.get(t) for t in texts]

    def save():
        if not state["dirty"]:
            return
        pruned = dict(list(entries.items())[-EN_CACHE_MAX:])
        EN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(EN_CACHE, {"en": pruned})

    return translate, save, status


# ==========================================================================
# Stage 4 support — optional Claude semantic clustering. Best-effort: any
# problem returns None and the deterministic heuristic runs instead. Its
# output feeds the SAME rank/validate gates as the heuristic.
# ==========================================================================
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


def claude_clusters(items: list[dict]):
    """Ask Claude to group the normalized items by underlying story.
    Returns a validated cluster list or None (→ heuristic)."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    try:
        import anthropic
    except ImportError:
        print("anthropic SDK not installed — using heuristic clustering", file=sys.stderr)
        return None
    try:
        client = anthropic.Anthropic()
    except Exception as exc:
        print(f"Anthropic client init failed ({exc}) — using heuristic", file=sys.stderr)
        return None

    lines = []
    for i, it in enumerate(items):
        snip = it.get("snippet", "")[:180]
        cat = it.get("category") or "other"
        lines.append(f"[{i}] ({it['source']} · {cat}) {it['display_title']}"
                     + (f" — {snip}" if snip else ""))

    prompt = (
        "You are a wire editor clustering live Middle East headlines from many "
        "outlets. Below are today's headlines, one per line, each numbered [i] "
        "with its outlet and outlet-category.\n\n"
        "Group the headlines that are about the SAME underlying event or issue "
        "into stories — even when worded very differently across outlets (e.g. "
        "'Israel strikes Hezbollah position' and 'IDF targets Hezbollah site in "
        "south Lebanon' are ONE story). Cluster by meaning, not shared words. A "
        "headline that stands alone is its own one-item story. Assign EVERY "
        "headline index to exactly one story, and never invent an index.\n\n"
        "For each story return:\n"
        "  - member_indices: all headline indices in the story.\n"
        "  - representative_index: the member with the clearest, most neutral, "
        "MOST CURRENT phrasing (avoid loaded wording, bare 'Live'/'Breaking' "
        "stubs, and stale framing — prefer the newest member's status).\n"
        "  - summary: a short, neutral, punchy one-line summary in English "
        "(max ~14 words, active voice), attributing claims where outlets differ. "
        "Base the status on the most recent headline: if an older one says an "
        "event is upcoming ('to host', 'leaders to meet') but a newer one shows "
        "it happening or done ('under way', 'met'), write the CURRENT state. Add "
        "no specifics the headlines don't state.\n"
        "  - summary_he: the same neutral summary in Hebrew.\n\n"
        "Return ONLY the JSON object.\n\n"
        "HEADLINES:\n" + "\n".join(lines)
    )

    kwargs = dict(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": CLUSTER_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        # Non-streaming stays under the SDK's HTTP-timeout guard at this size.
        # On models with safety-classifier declines, a server-side fallback
        # re-runs the request on the recommended substitute automatically.
        if CLAUDE_MODEL.startswith(("claude-opus-5", "claude-fable-5", "claude-mythos-5")):
            resp = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
    except Exception as exc:
        print(f"Claude clustering call failed ({exc}) — using heuristic", file=sys.stderr)
        return None

    if resp.stop_reason == "refusal":
        print("Claude declined the clustering request — using heuristic", file=sys.stderr)
        return None

    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        stories = json.loads(text)["stories"]
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
    for i in range(n):
        if i not in seen:
            clean.append({"member_indices": [i], "representative_index": i,
                          "summary": "", "summary_he": ""})
    return clean


# ==========================================================================
# Neutral one-line summaries (EN+HE) via Gemini — best-effort enhancement.
# Cached per Top-5 set so the API is only called when the stories change.
# The validation gate screens whatever comes back (non-English → dropped).
# ==========================================================================
SUMM_MODEL = os.environ.get("TOPSTORIES_SUMMARY_MODEL", "gemini-2.5-flash")
SUMM_PROMPT_VERSION = "v5-evolving"


def _top5_signature(stories: list[dict]) -> str:
    """Cache key: each story's representative AND newest member URL, so a
    running story's summary refreshes when its lead item changes."""
    parts = [SUMM_PROMPT_VERSION]
    for s in stories:
        members = s.get("members") or []
        parts.append((s.get("rep") or {}).get("url", ""))
        parts.append((members[0].get("url", "") if members else ""))
    return sha256("|".join(parts).encode("utf-8")).hexdigest()


def _gemini_summaries(stories: list[dict]):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
    except ImportError:
        print("google-genai not installed — skipping neutral summaries", file=sys.stderr)
        return None
    try:
        client = genai.Client(api_key=api_key)
    except Exception as exc:
        print(f"Gemini client init failed ({exc}) — skipping neutral summaries", file=sys.stderr)
        return None

    blocks = []
    for i, s in enumerate(stories, 1):
        lines = []
        for j, m in enumerate(m for m in s.get("members", [])[:6] if m.get("title")):
            when = (m.get("published") or "")[:16].replace("T", " ")
            tag = "  [MOST RECENT]" if j == 0 else ""
            lines.append(f"- ({m.get('source', '?')}, {when}{tag}) {m['title'].strip()}")
        blocks.append(f"STORY {i} (headlines newest-first):\n" + "\n".join(lines))
    prompt = (
        f"You are a neutral newswire editor. Below are the day's top {len(stories)} "
        "Middle-East stories; each is a cluster of headlines from different outlets "
        "about the SAME event.\n\n"
        "For EACH story, write ONE short, factual, strictly NEUTRAL summary line "
        "and its natural Hebrew translation. Rules:\n"
        "- LEAD WITH THE LATEST. The headlines are listed newest-first and the "
        "newest is tagged [MOST RECENT]. Base the event's status on it: if an "
        "older headline frames the event as upcoming or a decision ('to host', "
        "'selected to host', 'set to meet') but a newer one shows it happening or "
        "finished ('summit under way', 'leaders meet', 'talks concluded'), write "
        "the CURRENT state — never the superseded one.\n"
        "- If the story has ESCALATED (e.g. tanker attacks → retaliatory strikes "
        "→ sanctions), the line is about the LATEST development; mention the "
        "trigger briefly at most ('after Hormuz attacks'), never as the lead.\n"
        "- ACCURACY FIRST. Never assert more than the headlines support. Mirror "
        "the status the newest headlines give — planned→planned, under "
        "way→under way, concluded→concluded — and never upgrade a plan into a "
        "completed act.\n"
        "- FACT-CHECK your line against the listed headlines before writing it: "
        "every actor, action and claim must appear in them (especially the most "
        "recent). Assert no host, selection, number, or outcome that isn't there.\n"
        "- Do not invent or add specifics (arrivals, numbers, casualties, "
        "locations, outcomes) that are not in the headlines. If outlets differ on "
        "a detail, omit it or hedge it — never resolve it yourself.\n"
        "- MAKE IT PUNCHY: active voice, concrete nouns, lead with what's "
        "newsworthy. A strong verb early. Avoid limp, administrative openers like "
        "'Incidents reported in…', 'Reports from… detail…', 'Developments "
        "regarding…' — state who did what.\n"
        "- Be specific about who and what: name the main actors and the concrete "
        "development so the line is informative, not vague.\n"
        "- State only what outlets agree on; attribute any contested claim "
        "(e.g. 'Hamas says…', 'the Israeli military says…').\n"
        "- Remove loaded or partisan wording and scare-quotes. Use plain, neutral "
        "terms: 'settlers' not 'colonists'; name people plainly — 'Iran's late "
        "supreme leader Khamenei', never an honorific like 'martyred Leader' or "
        "'the Leader'; 'fighters'/'militants' per context, not 'terrorists' or "
        "'heroes'. Don't surface a side's rhetoric ('vengeance', 'victory') as "
        "fact — drop it or attribute it.\n"
        "- No praise, no condemnation, no adjectives of judgement. Max ~16 words.\n"
        "- The Hebrew must be equally neutral, natural, accurate and journalistic.\n\n"
        f"Return ONLY a JSON array of exactly {len(stories)} objects, in the same "
        'order, each {"en": "...", "he": "..."}. No prose, no code fences.\n\n'
        + "\n\n".join(blocks)
    )

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


def attach_neutral_summaries(stories: list[dict]) -> None:
    """Attach a neutral summary / summary_he to each story (Gemini + cache).
    Best-effort: on any miss the story keeps its representative headline."""
    if not stories:
        return
    if all(s.get("summary") and s.get("summary_he") for s in stories):
        return  # the LLM clustering path already produced summaries

    sig = _top5_signature(stories)
    cache = _load_json(SUMM_CACHE)
    items = cache.get("items") if cache.get("sig") == sig else None
    if not (isinstance(items, list) and len(items) >= len(stories)):
        items = _gemini_summaries(stories)
        if items:
            SUMM_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(SUMM_CACHE, {"sig": sig, "items": items})

    if not items:
        return
    for s, it in zip(stories, items):
        en = it.get("en", "")
        # Deterministic fact-check: a summary that still asserts stale
        # anticipatory framing is dropped (the representative shows instead).
        if en and tp.summary_contradicts_recency(en, s):
            print(f"  [summaries] dropped stale summary → using representative: {en!r}",
                  file=sys.stderr)
            continue
        if en:
            s["summary"] = en
        if it.get("he"):
            s["summary_he"] = it["he"]


# ==========================================================================
# Stage 7 — PUBLISH (with last-known-good fallback)
# ==========================================================================
def _fallback_usable(payload: dict) -> tuple[bool, list]:
    """Re-validate an already-published payload before serving it as the
    fallback. Runs every gate check except relevance (see validate_stage's
    check_relevance) so a corrupted or truncated file can never be recycled
    into place just because it happens to be the previous output."""
    stories = (payload or {}).get("stories")
    if not isinstance(stories, list) or not stories:
        return False, [{"check": "structure", "detail": "no stories"}]
    v = tp.validate_stage(stories, check_relevance=False)
    return v["ok"], v["failures"]


# Consecutive failed cycles tolerated before the CI annotation escalates from
# a warning to an error, and the age at which a served fallback is called out
# as stale. At a ~30-min cadence 3 cycles is ~1.5h of frozen Top 5.
FAIL_ESCALATE_AFTER = 3
LKG_STALE_HOURS = 3.0
FAILURE_LOG_KEEP = 20


def _health_update(ok: bool, failures: list, fallback: str | None) -> dict:
    """Persist cross-cycle health in state/topstories_failures.json.

    state/topstories_debug.json is overwritten every cycle, so a failure at
    03:00 is invisible by morning. This file keeps the last FAILURE_LOG_KEEP
    failure records plus the consecutive-failure count, which is what drives
    the escalation below."""
    health = _load_json(FAILURES_PATH)
    recent = health.get("recent") or []
    now_iso = datetime.now(timezone.utc).isoformat()
    if ok:
        health = {"consecutive_failures": 0, "last_success": now_iso,
                  "last_failure": health.get("last_failure"), "recent": recent}
    else:
        health = {
            "consecutive_failures": int(health.get("consecutive_failures") or 0) + 1,
            "last_success": health.get("last_success"),
            "last_failure": now_iso,
            "recent": ([{"at": now_iso,
                         "checks": sorted({f["check"] for f in failures}),
                         "failures": failures[:10],
                         "fallback": fallback}] + recent)[:FAILURE_LOG_KEEP],
        }
    try:
        FAILURES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(FAILURES_PATH, health)
    except Exception as exc:
        print(f"  could not write failure log: {exc}", file=sys.stderr)
    return health


def publish_stage(stories: list[dict], ok: bool, method: str, model: str | None) -> dict:
    """ok=True → write top_stories.json atomically + refresh the LKG copy.
    ok=False → keep serving the last known-good output rather than ever
    publishing broken data: prefer the existing file, else the LKG copy —
    each re-validated first — else an empty-stories payload, which makes the
    site hide the strip entirely.

    Returns the publish outcome plus the age of what is now being served, so
    the caller can escalate when a fallback has been frozen in place."""
    now = datetime.now(timezone.utc)
    if ok:
        payload = {"updated": now.isoformat(), "method": method,
                   "stories": tp.strip_internal(stories)}
        if model:
            payload["model"] = model
        _atomic_write(OUT_PATH, payload)
        LKG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(LKG_PATH, payload)
        return {"published": True, "fallback": None, "serving_age_hours": 0.0}

    def _age_hours(payload: dict) -> float | None:
        t = tp.parse_ts((payload or {}).get("updated"))
        return round((now.timestamp() - t) / 3600.0, 2) if t else None

    existing = _load_json(OUT_PATH)
    usable, why = _fallback_usable(existing)
    if usable:
        return {"published": False, "fallback": "kept existing top_stories.json",
                "serving_age_hours": _age_hours(existing)}
    lkg = _load_json(LKG_PATH)
    lkg_usable, lkg_why = _fallback_usable(lkg)
    if lkg_usable:
        _atomic_write(OUT_PATH, lkg)
        return {"published": False, "fallback": "restored state/topstories_lkg.json",
                "rejected_existing": why[:3], "serving_age_hours": _age_hours(lkg)}
    _atomic_write(OUT_PATH, {"updated": now.isoformat(), "method": method, "stories": []})
    return {"published": False,
            "fallback": "no usable fallback — wrote empty stories (strip hidden)",
            "rejected_existing": why[:3], "rejected_lkg": lkg_why[:3],
            "serving_age_hours": None}


# --------------------------------------------------------------------------
# Safety-net self-test. TOPSTORIES_FORCE_FAIL=<check> corrupts the ranked
# stories in a specific way just before validation, so you can confirm the
# gate catches it and the fallback holds — without waiting for a real
# failure. No-op unless the variable is set.
#   english   — put a Hebrew string in a member title
#   outlets   — reduce a story to a single outlet
#   relevance — replace a story with sports content
#   ordering  — swap the top two stories
#   summary   — put a Hebrew string in a summary (expected to REPAIR, not fail)
# --------------------------------------------------------------------------
def force_fail(stories: list[dict], mode: str) -> list[dict]:
    if not stories:
        return stories
    s = stories[0]
    if mode == "english":
        s["members"][0]["title"] = "דיווח: ישראל תקפה יעדים בדרום לבנון"
        s["rep"]["title"] = s["members"][0]["title"]
    elif mode == "outlets":
        keep = s["members"][0]["source"]
        s["members"] = [m for m in s["members"] if m["source"] == keep][:1]
        s["_members_full"] = s["_members_full"][:1]
        s["outlets"] = 1
    elif mode == "relevance":
        for m in s["members"]:
            m["title"] = "Barcelona beat Real Madrid 3-1 in the Champions League final"
        s["rep"]["title"] = s["members"][0]["title"]
        for m in s.get("_members_full", []):
            m["display_title"] = s["members"][0]["title"]
            m["snippet"] = "A football match report from the Champions League final."
        s.pop("summary", None)
    elif mode == "ordering" and len(stories) >= 2:
        stories[0], stories[1] = stories[1], stories[0]
        stories[0]["rank"], stories[1]["rank"] = 1, 2
    elif mode == "summary":
        s["summary"] = "כותרת בעברית שלא אמורה להופיע"
    else:
        print(f"  [self-test] unknown TOPSTORIES_FORCE_FAIL={mode!r}", file=sys.stderr)
        return stories
    print(f"  [self-test] injected '{mode}' breakage into the ranked stories",
          file=sys.stderr)
    return stories


def write_debug(debug: dict) -> None:
    try:
        DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(DEBUG_PATH, debug)
    except Exception as exc:
        print(f"  could not write debug trace: {exc}", file=sys.stderr)


# ==========================================================================
# Orchestration
# ==========================================================================
def main():
    debug = {"updated": datetime.now(timezone.utc).isoformat(), "stages": {}}

    # 1. FETCH ------------------------------------------------------------
    try:
        fetched = fetch_stage(HL_PATH)
    except Exception as exc:
        print(f"Could not read headlines.json ({exc}) — keeping previous top stories",
              file=sys.stderr)
        debug["stages"]["fetch"] = {"error": str(exc)}
        debug["ok"] = False
        write_debug(debug)
        sys.exit(0)
    items = fetched["items"]
    debug["stages"]["fetch"] = {"items": len(items), "outlets": fetched["outlets"],
                                "headlines_updated": fetched["updated"]}
    if not items:
        print("No headlines to cluster — keeping previous top stories", file=sys.stderr)
        debug["ok"] = False
        write_debug(debug)
        sys.exit(0)

    # 2. NORMALIZE ---------------------------------------------------------
    translate, save_translations, tr_status = make_translator()
    norm = tp.normalize_stage(items, translate=translate)
    save_translations()
    debug["stages"]["normalize"] = {"stats": norm["stats"],
                                    "translator": tr_status,
                                    "dropped": norm["dropped"]}
    if tr_status["requested"] and not tr_status["translated"]:
        # Not fatal — the snippet fallback usually covers it — but it means the
        # designed path is down, so say so where CI surfaces it.
        print(f"::warning title=Top-5 translation unavailable::"
              f"{tr_status['requested']} non-English title(s) needed translation, "
              f"0 succeeded ({tr_status['error']}). Falling back to English snippets; "
              f"items with no English snippet are dropped from the Top 5.")

    # 3. RELEVANCE ---------------------------------------------------------
    rel = tp.relevance_stage(norm["items"])
    debug["stages"]["relevance"] = {"in": len(norm["items"]), "kept": len(rel["items"]),
                                    "dropped": rel["dropped"]}

    # 4. CLUSTER -----------------------------------------------------------
    llm = claude_clusters(rel["items"])
    clus = tp.cluster_stage(rel["items"], llm)
    method = clus["method"]
    model = CLAUDE_MODEL if method == "llm" else None
    debug["stages"]["cluster"] = {"method": method,
                                  "clusters": len(clus["clusters"]),
                                  "sizes": sorted((len(c["member_indices"])
                                                   for c in clus["clusters"]), reverse=True)}

    # 5. RANK --------------------------------------------------------------
    rank = tp.rank_stage(rel["items"], clus["clusters"])
    stories = rank["stories"]
    debug["stages"]["rank"] = {"rule": "distinct outlets → camp spread → recency",
                               "min_outlets": tp.MIN_STORY_OUTLETS,
                               "selected": len(stories),
                               "candidates": rank["table"]}

    # Neutral summaries (best-effort; validated below like everything else).
    attach_neutral_summaries(stories)

    # 6. VALIDATE ----------------------------------------------------------
    forced = os.environ.get("TOPSTORIES_FORCE_FAIL")
    if forced:
        stories = force_fail(stories, forced)
    val = tp.validate_stage(stories)
    debug["stages"]["validate"] = {"ok": val["ok"], "failures": val["failures"],
                                   "repairs": val["repairs"],
                                   "forced_failure": forced or None}

    # 7. PUBLISH -----------------------------------------------------------
    pub = publish_stage(val["stories"], val["ok"], method, model)
    health = _health_update(val["ok"], val["failures"], pub["fallback"])
    debug["stages"]["publish"] = pub
    debug["health"] = {k: v for k, v in health.items() if k != "recent"}
    debug["ok"] = val["ok"]
    write_debug(debug)

    if val["ok"]:
        print(f"Wrote top_stories.json — {len(val['stories'])} stories via {method} "
              f"from {len(items)} headlines "
              f"({len(norm['items'])} after normalize, {len(rel['items'])} after relevance)")
        if val["repairs"]:
            print(f"  ({len(val['repairs'])} repaired before publish: "
                  f"{'; '.join(r['action'] for r in val['repairs'])})")
    else:
        # Escalate from a warning to an error once the Top 5 has been frozen
        # for several cycles or the served fallback has gone stale — a single
        # bad cycle is self-healing, a persistent one needs a human.
        reasons = "; ".join(f"{f['check']}: {f['detail']}" for f in val["failures"][:5])
        streak = health["consecutive_failures"]
        age = pub.get("serving_age_hours")
        stale = age is None or age >= LKG_STALE_HOURS
        level = "error" if (streak >= FAIL_ESCALATE_AFTER or stale) else "warning"
        serving = f", serving {age}h-old data" if age is not None else ", nothing to serve"
        print(f"::{level} title=Top-5 validation failed "
              f"({streak} cycle{'s' if streak != 1 else ''} in a row{serving})::{reasons}")
        print(f"Validation failed — {pub['fallback']}. Full trace in "
              f"state/topstories_debug.json, history in state/topstories_failures.json",
              file=sys.stderr)

    if os.environ.get("TOPSTORIES_SAMPLE"):
        _print_sample(val["stories"], method, model)


def _print_sample(stories, method, model):
    """Human-readable dump of the ranked Top 5 for sanity-checking."""
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
