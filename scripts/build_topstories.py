#!/usr/bin/env python3
"""Top-5 stories builder — an explicit staged pipeline → top_stories.json.

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
    """Returns (translate, save). translate(texts) -> list[str|None]."""
    key = os.environ.get("AZURE_TRANSLATOR_KEY")
    region = os.environ.get("AZURE_TRANSLATOR_REGION")
    cache = _load_json(EN_CACHE)
    entries: dict = cache.get("en") or {}
    state = {"dirty": False}

    def translate(texts):
        need = [t for t in dict.fromkeys(texts) if t and t not in entries]
        if need and key:
            try:
                import requests
                headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/json"}
                if region:
                    headers["Ocp-Apim-Subscription-Region"] = region
                for i in range(0, len(need), 50):
                    batch = need[i:i + 50]
                    r = requests.post(
                        AZURE_ENDPOINT, params={"api-version": "3.0", "to": "en"},
                        headers=headers, json=[{"text": t} for t in batch], timeout=20)
                    r.raise_for_status()
                    for src, item in zip(batch, r.json()):
                        entries[src] = item["translations"][0]["text"]
                        state["dirty"] = True
            except Exception as exc:
                print(f"  [normalize] Azure en-translation failed ({exc}) — "
                      f"falling back to snippets", file=sys.stderr)
        return [entries.get(t) for t in texts]

    def save():
        if not state["dirty"]:
            return
        pruned = dict(list(entries.items())[-EN_CACHE_MAX:])
        EN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(EN_CACHE, {"en": pruned})

    return translate, save


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
def _display_clean(stories) -> bool:
    """Structural screen for a fallback candidate: non-empty, and every
    displayed string is English. (Full validation needs snippets, which
    published files don't carry.)"""
    if not isinstance(stories, list) or not stories:
        return False
    for s in stories:
        if s.get("summary") and not tp.is_english_display(s["summary"]):
            return False
        if not tp.is_english_display((s.get("rep") or {}).get("title", "")):
            return False
        for m in s.get("members", []):
            if not tp.is_english_display(m.get("title", "")):
                return False
    return True


def publish_stage(stories: list[dict], ok: bool, method: str, model: str | None) -> dict:
    """ok=True → write top_stories.json atomically + refresh the LKG copy.
    ok=False → keep serving the previous good output: leave the existing
    file if its display strings are clean, else restore the LKG copy, else
    write an empty-stories payload (the site hides the strip) rather than
    ever publishing broken data."""
    now_iso = datetime.now(timezone.utc).isoformat()
    if ok:
        payload = {"updated": now_iso, "method": method, "stories": tp.strip_internal(stories)}
        if model:
            payload["model"] = model
        _atomic_write(OUT_PATH, payload)
        LKG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(LKG_PATH, payload)
        return {"published": True, "fallback": None}

    existing = _load_json(OUT_PATH)
    if _display_clean(existing.get("stories")):
        return {"published": False, "fallback": "kept existing top_stories.json"}
    lkg = _load_json(LKG_PATH)
    if _display_clean(lkg.get("stories")):
        _atomic_write(OUT_PATH, lkg)
        return {"published": False, "fallback": "restored state/topstories_lkg.json"}
    _atomic_write(OUT_PATH, {"updated": now_iso, "method": method, "stories": []})
    return {"published": False, "fallback": "no clean fallback — wrote empty stories (strip hidden)"}


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
    translate, save_translations = make_translator()
    norm = tp.normalize_stage(items, translate=translate)
    save_translations()
    debug["stages"]["normalize"] = {"stats": norm["stats"], "dropped": norm["dropped"]}

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
    debug["stages"]["rank"] = {"candidates": rank["table"][:20],
                               "selected": len(stories)}

    # Neutral summaries (best-effort; validated below like everything else).
    attach_neutral_summaries(stories)

    # 6. VALIDATE ----------------------------------------------------------
    val = tp.validate_stage(stories)
    debug["stages"]["validate"] = {"ok": val["ok"], "failures": val["failures"],
                                   "repairs": val["repairs"]}

    # 7. PUBLISH -----------------------------------------------------------
    pub = publish_stage(val["stories"], val["ok"], method, model)
    debug["stages"]["publish"] = pub
    debug["ok"] = val["ok"]
    write_debug(debug)

    if val["ok"]:
        print(f"Wrote top_stories.json — {len(val['stories'])} stories via {method} "
              f"from {len(items)} headlines "
              f"({len(norm['items'])} after normalize, {len(rel['items'])} after relevance)")
    else:
        # GitHub Actions warning annotation + a debuggable trace on disk.
        reasons = "; ".join(f"{f['check']}: {f['detail']}" for f in val["failures"][:5])
        print(f"::warning title=Top-5 validation failed::{reasons}")
        print(f"Validation failed — {pub['fallback']}. Full trace in "
              f"state/topstories_debug.json", file=sys.stderr)

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
