#!/usr/bin/env python3
"""Stage 3 — Relay/update writer: draft short posts in the author's voice.

Takes the ranked briefing (Stage 2) and the style profile (Stage 1) and writes
a few SHORT news-relay updates — the kind of post that is the bulk of the
author's real output: restating what a named outlet reported, in the author's
Hebrew voice, with their attribution closing. It does NOT write original
analysis and does NOT invent facts: it only has the headline, so it relays the
headline.

Nothing is published. Drafts are written to agent/data/drafts.json for review
(Stage 4). Every draft must be approved by you before it goes anywhere.

Usage (PowerShell):
  py agent\\draft_posts.py --dry-run    # show stories + prompt, no API call
  py agent\\draft_posts.py              # draft top 3 stories -> agent\\data\\drafts.json
  py agent\\draft_posts.py --count 5 --lang he
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import (BRIEFING_PATH, DRAFTS_PATH, STYLE_PROFILE_PATH,
                    ensure_utf8_console, get_model, require_api_key)

SYSTEM_PROMPT = """You write SHORT news-relay updates in ONE specific author's voice for their Middle East news channel.

You are given (a) the author's style profile and verbatim examples of their real posts, and (b) ranked news stories. Each story is only a HEADLINE from one or more named outlets — you do NOT have the article body.

Your job: for each story, write one short post the author could publish as-is.

Hard rules:
- RELAY ONLY. Restate what the headline reports, as a short update. You have ONLY the headline — never invent numbers, quotes, names, locations, causes, motives, or consequences that the headline does not state. If the headline is vague, keep the post vague.
- NO analysis, opinion, prediction, or strategic assessment. The author has a separate analytical mode; you are NOT writing that. These are factual relays.
- Match the author's voice exactly: their typical SHORT length, their language (write in the requested language), their opening and closing habits, and especially their attribution style (how they credit the source outlet/journalist).
- Attribute the report to the source outlet(s) you are given, in the author's usual attribution style.
- Do not add hashtags, calls to action, or emojis unless the style profile shows the author uses them in relays.

For each draft also return:
- "relays_facts": the specific claims from the headline your post conveys (so a human can verify nothing was added).
- "confidence": 0.0-1.0 — how confident you are the post is faithful to the headline AND matches the voice. Lower it for vague or single-source headlines.
- "review_flags": short warnings for the human reviewer (e.g. "single source", "headline ambiguous", "claim attributed to one side", "could not verify")."""

STR = {"type": "string"}
STR_LIST = {"type": "array", "items": STR}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": list(props), "properties": props}


DRAFTS_SCHEMA = _obj({
    "drafts": {"type": "array", "items": _obj({
        "source_headline": STR,
        "post_text": STR,
        "language": {"type": "string", "enum": ["he", "en", "ar"]},
        "attribution": STR,
        "relays_facts": STR_LIST,
        "confidence": {"type": "number"},
        "review_flags": STR_LIST,
    })},
})

LANG_NAME = {"he": "Hebrew", "en": "English", "ar": "Arabic"}


def load_json(path: Path, what: str, hint: str) -> dict:
    if not path.is_file():
        sys.exit(f"ERROR: {path} not found — {what}.\n  {hint}")
    return json.loads(path.read_text(encoding="utf-8"))


def style_digest(profile: dict) -> str:
    """Compact, voice-anchoring slice of the style profile for the prompt."""
    q = profile.get("qualitative", {})
    parts = [profile.get("style_instructions", "") or q.get("style_instructions", "")]

    openings = q.get("openings", {}).get("verbatim_examples", [])
    closings = q.get("closings", {}).get("verbatim_examples", [])
    sig = q.get("signature_phrases", [])
    cite = q.get("sources_and_citations", {})
    if openings:
        parts.append("Typical openings (verbatim): " + " | ".join(openings[:6]))
    if closings:
        parts.append("Typical closings (verbatim): " + " | ".join(closings[:6]))
    if sig:
        parts.append("Signature phrases (verbatim): " + " | ".join(sig[:8]))
    if cite:
        parts.append("Attribution/citation habit: "
                     + (cite.get("citation_style", "") or cite.get("cites_sources", "")))

    examples = q.get("representative_examples", [])
    if examples:
        parts.append("\nVerbatim examples of real posts (match this voice exactly):")
        for ex in examples[:4]:
            parts.append(f'  - "{ex.get("text", "")}"')
    return "\n".join(p for p in parts if p)


def stories_block(stories: list[dict], count: int) -> str:
    lines = []
    for i, s in enumerate(stories[:count], 1):
        outlets = ", ".join(s.get("outlets", [])) or "unknown"
        srcs = "; ".join(f'{a["source"]}: "{a["title"]}"'
                         for a in s.get("articles", [])[:4])
        lines.append(
            f"--- story {i} ---\n"
            f"headline: {s['headline']}\n"
            f"outlet(s): {outlets}\n"
            f"regions: {', '.join(s.get('regions', []))}\n"
            f"source items: {srcs}")
    return "\n".join(lines)


def build_user_prompt(profile: dict, stories: list[dict], count: int, lang: str) -> str:
    if lang == "auto":
        primary = (profile.get("qualitative", {})
                   .get("language_mixing", {}).get("primary_language", "Hebrew"))
        lang_instr = f"Write each post in the author's primary language ({primary})."
    else:
        lang_instr = (f"Write each post in {LANG_NAME[lang]}. If the author's "
                      "style mixes in another language for specific elements "
                      "(e.g. quoting a source), follow that habit.")
    return (
        "== AUTHOR STYLE PROFILE ==\n"
        f"{style_digest(profile)}\n\n"
        f"== STORIES TO RELAY (top {min(count, len(stories))} by significance) ==\n"
        f"{stories_block(stories, count)}\n\n"
        f"== TASK ==\n"
        f"Write one short relay post per story, in the author's voice. {lang_instr}\n"
        "Relay only what each headline states. Return the drafts now.")


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def call_claude(model: str, user_prompt: str) -> tuple[dict, dict]:
    import anthropic

    client = anthropic.Anthropic()
    base = dict(model=model, max_tokens=8000, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}])
    try:
        response = client.messages.create(
            **base,
            extra_body={"output_config": {"format": {
                "type": "json_schema", "schema": DRAFTS_SCHEMA}}},
        )
    except anthropic.BadRequestError as err:
        if "output_config" not in str(err) and "format" not in str(err):
            raise
        print(f"NOTE: {model} rejected structured outputs; falling back to plain JSON.")
        base["messages"][0]["content"] += (
            '\n\nReturn ONLY a JSON object: {"drafts": [{"source_headline", '
            '"post_text", "language", "attribution", "relays_facts", '
            '"confidence", "review_flags"}, ...]} — no prose, no markdown fences.')
        response = client.messages.create(**base)

    if response.stop_reason == "refusal":
        sys.exit("ERROR: the model refused this request. Check the input stories.")
    if response.stop_reason == "max_tokens":
        print("WARNING: response hit max_tokens and may be truncated.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    usage = {"input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens}
    return extract_json(text), usage


def main() -> None:
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="Draft relay posts in the author's voice.")
    ap.add_argument("--briefing", default=str(BRIEFING_PATH))
    ap.add_argument("--profile", default=str(STYLE_PROFILE_PATH))
    ap.add_argument("--out", default=str(DRAFTS_PATH))
    ap.add_argument("--model", default=None, help="Override model (default: %s)" % get_model())
    ap.add_argument("--count", type=int, default=3, help="How many top stories to draft")
    ap.add_argument("--lang", choices=["he", "en", "ar", "auto"], default="he",
                    help="Language for the drafts (default: he)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show stories and the prompt; no API call")
    args = ap.parse_args()

    profile = load_json(Path(args.profile), "style profile missing",
                        "Run Stage 1: py agent\\analyze_style.py")
    briefing = load_json(Path(args.briefing), "briefing missing",
                         "Run Stage 2: py agent\\build_briefing.py")
    stories = briefing.get("stories", [])
    if not stories:
        sys.exit("ERROR: briefing has no stories. Re-run build_briefing.py.")

    model = get_model(args.model)
    user_prompt = build_user_prompt(profile, stories, args.count, args.lang)
    est_tokens = (len(user_prompt) + len(SYSTEM_PROMPT)) // 4

    print(f"Profile        : {profile.get('author', '?')} "
          f"(style from {profile.get('generated_at', '?')[:10]})")
    print(f"Briefing       : {len(stories)} stories; drafting top {args.count} in '{args.lang}'")
    print(f"Model          : {model}  (~{est_tokens:,} input tokens)")

    if args.dry_run:
        print("\n-- DRY RUN: stories to be relayed --")
        print(stories_block(stories, args.count))
        print("\nNo API call made. Remove --dry-run to generate drafts.")
        return

    require_api_key()
    print("Calling Claude ...")
    result, usage = call_claude(model, user_prompt)
    drafts = result.get("drafts", [])

    payload = {
        "schema": "mena-agent/drafts@1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "author": profile.get("author"),
        "model": model,
        "language": args.lang,
        "source_briefing": {
            "generated_at": briefing.get("generated_at"),
            "window_days": briefing.get("window_days"),
        },
        "drafts": [
            {**d, "status": "pending", "significance": stories[i].get("score")
             if i < len(stories) else None}
            for i, d in enumerate(drafts)
        ],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # Also render a self-contained HTML page — the Windows console can't show
    # Hebrew right-to-left, so this is where you actually read the drafts.
    from render_drafts import write_html
    html_path = write_html(payload, out_path.with_suffix(".html"))

    print(f"\nAPI usage      : {usage['input_tokens']:,} in / {usage['output_tokens']:,} out tokens")
    print(f"Wrote          : {out_path}  ({len(drafts)} drafts, all status=pending)")
    print(f"Read drafts in : {html_path}")
    print("  ^ double-click this file — Hebrew renders correctly (right-to-left) there.\n")
    print("=" * 70)
    # Summary only — NOT the Hebrew body. The console renders Hebrew left-to-
    # right and garbles it, so the real text lives in the HTML page above.
    for i, d in enumerate(drafts, 1):
        conf = d.get("confidence")
        conf_s = f"{conf:.0%}" if isinstance(conf, (int, float)) else "?"
        chars = len(d.get("post_text", "").strip())
        print(f"DRAFT {i}  ·  confidence {conf_s}  ·  {chars} chars  ·  "
              f"relays: {d.get('source_headline', '')}")
        if d.get("review_flags"):
            print(f"         ⚠ {', '.join(d['review_flags'])}")
    print("=" * 70)
    print("Hebrew is garbled in the console — open the HTML file above to read "
          "the drafts properly (right-to-left).")
    print("NOTHING SENT. Stage 4 will let you approve / edit / reject each one.")


if __name__ == "__main__":
    main()
