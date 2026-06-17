#!/usr/bin/env python3
"""Stage 1b — Analyze the author's writing style and produce style_profile.json.

Two layers:
  1. Quantitative (computed locally, free): message lengths, language mix
     (Hebrew/English/Arabic), emoji habits, link/citation rate, posting-hour
     histogram, common openers/closers.
  2. Qualitative (one Claude API call): tone, argument structure, openings/
     closings, topics, signature phrases, dos/donts, representative examples,
     and a distilled "write like me" instruction block that the Stage 3 draft
     engine will reuse on every draft.

Usage (PowerShell):
  py agent\\analyze_style.py --dry-run     # preview what would be sent, no API call
  py agent\\analyze_style.py               # full analysis -> agent/data/style_profile.json
"""
import argparse
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from config import (MY_MESSAGES_PATH, STYLE_PROFILE_PATH, ensure_utf8_console,
                    get_model, require_api_key)

HEBREW_RE = re.compile(r"[֐-׿]")
ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿ]")
LATIN_RE = re.compile(r"[A-Za-z]")
EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF☀-➿⬀-⯿←-⇿]"
)
URL_RE = re.compile(r"https?://\S+")

SYSTEM_PROMPT = """You are an expert writing-style analyst.

You will receive (a) quantitative statistics and (b) a sample of messages, all
written by ONE author in a WhatsApp group focused on Middle East geopolitical
analysis. Your job is to produce a precise, reusable style profile, so that an
AI drafting assistant can later write new posts in this author's exact voice.

Rules:
- Be concrete and evidence-based. Quote the author's actual phrases (verbatim,
  in the original language) instead of generic descriptions.
- The author may mix Hebrew, English and Arabic. Describe exactly when and how
  each language/script is used. Quote Hebrew/Arabic verbatim where relevant;
  write your analysis itself in English.
- "representative_examples" must be copied verbatim from the samples - never
  invent or paraphrase them.
- "style_instructions" is the most important field: a self-contained
  instruction block (second person: "Write posts that...") that captures
  length, tone, structure, openings, closings, language mixing, emoji and
  citation habits well enough that drafts become indistinguishable from the
  author's real posts. Include short verbatim phrase examples inside it."""

STR = {"type": "string"}
STR_LIST = {"type": "array", "items": STR}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": list(props), "properties": props}


STYLE_SCHEMA = _obj({
    "voice_summary": STR,
    "tone": _obj({
        "analytical_vs_reactive": STR,
        "formality": STR,
        "emotional_register": STR,
        "hedging_and_confidence": STR,
    }),
    "post_length": _obj({"typical": STR, "notes": STR}),
    "openings": _obj({"patterns": STR_LIST, "verbatim_examples": STR_LIST}),
    "closings": _obj({"patterns": STR_LIST, "verbatim_examples": STR_LIST}),
    "structure": _obj({
        "argument_pattern": STR,
        "paragraphing": STR,
        "formatting_habits": STR,
    }),
    "topics": {"type": "array", "items": _obj({
        "topic": STR,
        "prominence": {"type": "string", "enum": ["high", "medium", "low"]},
        "typical_angle": STR,
    })},
    "language_mixing": _obj({
        "primary_language": STR,
        "pattern": STR,
        "switching_triggers": STR,
    }),
    "emoji_usage": _obj({"frequency": STR, "function": STR, "common_emojis": STR_LIST}),
    "sources_and_citations": _obj({
        "cites_sources": STR,
        "citation_style": STR,
        "typical_sources": STR_LIST,
    }),
    "signature_phrases": STR_LIST,
    "rhetorical_devices": STR_LIST,
    "dos": STR_LIST,
    "donts": STR_LIST,
    "representative_examples": {"type": "array", "items": _obj({"text": STR, "why": STR})},
    "style_instructions": STR,
})


def quantitative_profile(messages: list[dict]) -> dict:
    texts = [m["text"] for m in messages]
    char_lens = [len(t) for t in texts]
    word_lens = [len(t.split()) for t in texts]

    def pct(n):
        return round(100 * n / len(texts), 1)

    emoji_counter = Counter(ch for t in texts for ch in EMOJI_RE.findall(t))
    domains = Counter()
    for t in texts:
        for url in URL_RE.findall(t):
            netloc = urlparse(url).netloc.lower().removeprefix("www.")
            if netloc:
                domains[netloc] += 1

    hours = Counter()
    weekdays = Counter()
    for m in messages:
        try:
            dt = datetime.fromisoformat(m["ts"])
            hours[dt.hour] += 1
            weekdays[dt.strftime("%A")] += 1
        except ValueError:
            pass

    substantive = [t for t in texts if len(t) >= 150]
    first_words = Counter(t.split()[0] for t in substantive if t.split())
    last_words = Counter(t.split()[-1] for t in substantive if t.split())

    return {
        "message_count": len(texts),
        "length_chars": {
            "mean": round(statistics.mean(char_lens), 1),
            "median": statistics.median(char_lens),
            "p90": sorted(char_lens)[int(len(char_lens) * 0.9)],
            "max": max(char_lens),
        },
        "length_words": {
            "mean": round(statistics.mean(word_lens), 1),
            "median": statistics.median(word_lens),
        },
        "length_buckets_pct": {
            "short_<80ch": pct(sum(1 for c in char_lens if c < 80)),
            "medium_80-300ch": pct(sum(1 for c in char_lens if 80 <= c < 300)),
            "long_300-800ch": pct(sum(1 for c in char_lens if 300 <= c < 800)),
            "verylong_>=800ch": pct(sum(1 for c in char_lens if c >= 800)),
        },
        "language_pct": {
            "contains_hebrew": pct(sum(1 for t in texts if HEBREW_RE.search(t))),
            "contains_english": pct(sum(1 for t in texts if LATIN_RE.search(t))),
            "contains_arabic": pct(sum(1 for t in texts if ARABIC_RE.search(t))),
        },
        "emoji": {
            "messages_with_emoji_pct": pct(sum(1 for t in texts if EMOJI_RE.search(t))),
            "top": [f"{e} x{c}" for e, c in emoji_counter.most_common(10)],
        },
        "links": {
            "messages_with_link_pct": pct(sum(1 for t in texts if URL_RE.search(t))),
            "top_domains": [f"{d} x{c}" for d, c in domains.most_common(10)],
        },
        "punctuation_pct": {
            "question_mark": pct(sum(1 for t in texts if "?" in t)),
            "exclamation": pct(sum(1 for t in texts if "!" in t)),
            "ellipsis": pct(sum(1 for t in texts if "..." in t or "…" in t)),
        },
        "top_posting_hours": [f"{h:02d}:00 x{c}" for h, c in hours.most_common(5)],
        "weekday_distribution": dict(weekdays.most_common()),
        "substantive_messages_>=150ch": len(substantive),
        "top_opening_words": [f"{w} x{c}" for w, c in first_words.most_common(10)],
        "top_closing_words": [f"{w} x{c}" for w, c in last_words.most_common(10)],
    }


def select_samples(messages: list[dict], min_chars: int, max_samples: int,
                   max_chars: int) -> tuple[list[dict], list[dict], int]:
    """Pick recent substantive posts (plus a few short reactions) within budget."""
    substantive = [m for m in messages if len(m["text"]) >= min_chars]
    if len(substantive) < 20:
        lowered = max(60, min_chars // 2)
        print(f"NOTE: only {len(substantive)} messages >= {min_chars} chars; "
              f"lowering threshold to {lowered}.")
        min_chars = lowered
        substantive = [m for m in messages if len(m["text"]) >= min_chars]

    picked, used = [], 0
    for m in reversed(substantive):  # newest first
        cost = len(m["text"]) + 40
        if len(picked) >= max_samples or used + cost > max_chars:
            break
        picked.append(m)
        used += cost
    picked.reverse()

    shorts = [m for m in reversed(messages) if len(m["text"]) < min_chars][:15]
    shorts.reverse()
    return picked, shorts, min_chars


def build_user_prompt(meta: dict, stats: dict, samples: list[dict],
                      shorts: list[dict]) -> str:
    lines = [
        f"Author: \"{meta['author']}\". Messages span {meta['stats']['first_message']} "
        f"to {meta['stats']['last_message']}.",
        "",
        "== QUANTITATIVE STATISTICS (computed over all kept messages) ==",
        json.dumps(stats, ensure_ascii=False, indent=1),
        "",
        f"== SUBSTANTIVE POSTS ({len(samples)} samples, oldest to newest) ==",
    ]
    for i, m in enumerate(samples, 1):
        lines.append(f"--- post {i} [{m['ts']}] ---\n{m['text']}")
    if shorts:
        lines.append(f"\n== SHORT / QUICK-REACTION MESSAGES ({len(shorts)} samples) ==")
        for m in shorts:
            lines.append(f"[{m['ts']}] {m['text']}")
    lines.append("\nProduce the style profile now.")
    return "\n".join(lines)


def extract_json(text: str) -> dict:
    """Parse JSON from a plain-text response (handles ``` fences and prose)."""
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
    base = dict(model=model, max_tokens=16000, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}])
    try:
        # Structured outputs guarantee schema-valid JSON on current models
        # (passed via extra_body so older SDK versions also forward it).
        response = client.messages.create(
            **base,
            extra_body={"output_config": {"format": {
                "type": "json_schema", "schema": STYLE_SCHEMA}}},
        )
    except anthropic.BadRequestError as err:
        if "output_config" not in str(err) and "format" not in str(err):
            raise
        print(f"NOTE: {model} rejected structured outputs; falling back to plain JSON.")
        base["messages"][0]["content"] += (
            "\n\nReturn ONLY a JSON object with exactly these keys (no prose, "
            "no markdown fences): " + ", ".join(STYLE_SCHEMA["required"]))
        response = client.messages.create(**base)

    if response.stop_reason == "refusal":
        sys.exit("ERROR: the model refused this request. Check the input for sensitive content.")
    if response.stop_reason == "max_tokens":
        print("WARNING: response hit max_tokens and may be truncated.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    usage = {"input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens}
    return extract_json(text), usage


def main() -> None:
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="Build style_profile.json from parsed messages.")
    ap.add_argument("--input", default=str(MY_MESSAGES_PATH),
                    help="my_messages.json produced by parse_whatsapp.py")
    ap.add_argument("--out", default=str(STYLE_PROFILE_PATH))
    ap.add_argument("--model", default=None, help="Override model (default: %s)" % get_model())
    ap.add_argument("--min-chars", type=int, default=150,
                    help="Minimum length for a message to count as a substantive post")
    ap.add_argument("--max-samples", type=int, default=100,
                    help="Max substantive posts sent for analysis")
    ap.add_argument("--max-chars", type=int, default=120_000,
                    help="Char budget for samples (~30K tokens)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show stats and what would be sent; no API call")
    args = ap.parse_args()

    input_path = Path(args.input)
    if not input_path.is_file():
        sys.exit(f"ERROR: {input_path} not found. Run parse_whatsapp.py first.")
    data = json.loads(input_path.read_text(encoding="utf-8"))
    messages = data["messages"]
    if len(messages) < 10:
        sys.exit(f"ERROR: only {len(messages)} messages — too few for a meaningful profile.")

    stats = quantitative_profile(messages)
    samples, shorts, min_chars = select_samples(
        messages, args.min_chars, args.max_samples, args.max_chars)
    user_prompt = build_user_prompt(data, stats, samples, shorts)
    est_tokens = (len(user_prompt) + len(SYSTEM_PROMPT)) // 4
    model = get_model(args.model)

    print(f"Messages       : {len(messages)} total, "
          f"{stats['substantive_messages_>=150ch']} substantive")
    print(f"Sending        : {len(samples)} posts + {len(shorts)} short messages "
          f"(threshold {min_chars} chars)")
    print(f"Model          : {model}  (~{est_tokens:,} input tokens)")

    if args.dry_run:
        print("\n-- DRY RUN: quantitative stats --")
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        print("\nNo API call made. Remove --dry-run to build the full profile.")
        return

    require_api_key()
    print("Calling Claude ...")
    qualitative, usage = call_claude(model, user_prompt)

    profile = {
        "schema": "mena-agent/style-profile@1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "author": data["author"],
        "model": model,
        "source": {
            "file": data.get("source_file"),
            "messages_analyzed": len(messages),
            "samples_sent": len(samples),
            "range": [data["stats"]["first_message"], data["stats"]["last_message"]],
        },
        "quantitative": stats,
        "qualitative": qualitative,
        "style_instructions": qualitative.get("style_instructions", ""),
        "feedback": {"edits": [], "approved": 0, "rejected": 0},  # Stage 4 feedback loop
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\nWrote          : {out_path}")
    print(f"API usage      : {usage['input_tokens']:,} in / {usage['output_tokens']:,} out tokens")
    print(f"Voice summary  : {qualitative.get('voice_summary', '(missing)')}")
    print("\nReview the profile, especially 'style_instructions'. "
          "Re-run anytime after exporting fresh chat history.")


if __name__ == "__main__":
    main()
