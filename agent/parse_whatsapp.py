#!/usr/bin/env python3
"""Stage 1a — Parse a WhatsApp chat export and extract one author's messages.

Handles both export dialects:
  Android:  12/31/23, 9:14 PM - Sender Name: message text
  iOS:      [31/12/2023, 21:14:32] Sender Name: message text

plus locale variations (DD/MM vs MM/DD vs YYYY-MM-DD, dot dates, 24h time,
Hebrew AM/PM markers), Unicode bidi control characters that WhatsApp sprinkles
into RTL exports, multi-line messages, and system/media/deleted noise.

Usage (PowerShell):
  py agent\\parse_whatsapp.py "C:\\path\\WhatsApp Chat with Group.txt" --list-authors
  py agent\\parse_whatsapp.py "C:\\path\\WhatsApp Chat with Group.txt" --author "Your Name"

Output: agent/data/my_messages.json (gitignored — contains private messages).
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR, MY_MESSAGES_PATH, ensure_utf8_console

# Invisible direction/formatting marks WhatsApp inserts around RTL text.
BIDI_MARKS = re.compile("[‎‏‪-‮⁦-⁩﻿]")
# Narrow/regular no-break spaces (iOS puts U+202F before AM/PM).
ODD_SPACES = re.compile("[   ]")

HEADER_RE = re.compile(
    r"""^\[?
    (?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{2,4})
    ,?\s+
    (?P<time>\d{1,2}:\d{2}(?::\d{2})?)
    \s*(?P<meridiem>[APap]\.?\s?[Mm]\.?|לפנה.{0,2}צ|אחה.{0,2}צ)?
    \s*(?:\]|[-–—])\s+
    (?P<rest>.*)$""",
    re.VERBOSE,
)

# Placeholder bodies to drop (lowercased, exact or prefix match).
MEDIA_PLACEHOLDERS = {
    "<media omitted>", "image omitted", "video omitted", "audio omitted",
    "sticker omitted", "gif omitted", "document omitted", "voice call",
    "video call", "missed voice call", "missed video call", "null",
    "contact card omitted", "location shared", "live location shared",
    "<מדיה הושמטה>", "התמונה הושמטה", "הסרטון הושמט", "ההקלטה הושמטה",
    "המדבקה הושמטה", "המסמך הושמט", "כרטיס איש הקשר הושמט",
}
DELETED_MARKERS = {
    "this message was deleted", "you deleted this message",
    "הודעה זו נמחקה", "מחקת את ההודעה הזאת", "מחקת הודעה זו",
}
EDITED_SUFFIXES = ("<this message was edited>", "<נערכה הודעה זו>", "<הודעה זו נערכה>")
ATTACHED_RE = re.compile(r"^\S+\.\w{2,4} \((?:file attached|קובץ מצורף)\)$", re.IGNORECASE)


def clean_line(raw: str) -> str:
    return ODD_SPACES.sub(" ", BIDI_MARKS.sub("", raw)).rstrip("\n")


def parse_export(path: Path):
    """Return a list of {date, time, meridiem, sender, text} raw messages."""
    messages = []
    current = None
    with path.open(encoding="utf-8-sig", errors="replace") as fh:
        for raw in fh:
            line = clean_line(raw)
            m = HEADER_RE.match(line)
            if m:
                if current:
                    messages.append(current)
                rest = m.group("rest")
                if ": " in rest:
                    sender, text = rest.split(": ", 1)
                else:
                    sender, text = None, rest  # system message (no sender)
                current = {
                    "date": m.group("date"),
                    "time": m.group("time"),
                    "meridiem": (m.group("meridiem") or "").replace(".", "").replace(" ", ""),
                    "sender": sender.strip() if sender else None,
                    "text": text,
                }
            elif current:
                current["text"] += "\n" + line
            # Lines before the first header (rare) are ignored.
    if current:
        messages.append(current)
    return messages


def detect_date_order(messages, forced: str) -> str:
    """Infer dmy/mdy/ymd from the data; WhatsApp keeps one order per export."""
    if forced != "auto":
        return forced
    firsts, seconds = set(), set()
    for msg in messages:
        parts = re.split(r"[./-]", msg["date"])
        if len(parts[0]) == 4:
            return "ymd"
        firsts.add(int(parts[0]))
        seconds.add(int(parts[1]))
    if any(v > 12 for v in firsts):
        return "dmy"
    if any(v > 12 for v in seconds):
        return "mdy"
    return "dmy"  # ambiguous: default to day-first (IL/EU locales); --date-order to force


def to_timestamp(msg, order: str):
    parts = [int(p) for p in re.split(r"[./-]", msg["date"])]
    if order == "ymd":
        year, month, day = parts
    elif order == "mdy":
        month, day, year = parts
    else:
        day, month, year = parts
    if year < 100:
        year += 2000
    hm = [int(p) for p in msg["time"].split(":")]
    hour, minute = hm[0], hm[1]
    mer = msg["meridiem"].lower()
    if mer in ("pm",) or mer.startswith("אחה"):
        hour = hour % 12 + 12
    elif mer in ("am",) or mer.startswith("לפנה"):
        hour = hour % 12
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", BIDI_MARKS.sub("", name)).strip().casefold()


def match_author(senders, query: str):
    """Find the sender matching --author (exact, then substring, then digits)."""
    nq = normalize_name(query)
    by_norm = {normalize_name(s): s for s in senders}
    if nq in by_norm:
        return by_norm[nq]
    partial = [s for n, s in by_norm.items() if nq in n]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        sys.exit(f"ERROR: --author \"{query}\" matches multiple senders: {partial}\n"
                 "Use the exact name shown by --list-authors.")
    q_digits = re.sub(r"\D", "", query)
    if len(q_digits) >= 7:  # looks like a phone number
        for s in senders:
            if q_digits in re.sub(r"\D", "", s):
                return s
    return None


def clean_message_text(text: str):
    """Strip edited markers and return None for media/deleted placeholders."""
    text = text.strip()
    low = text.casefold()
    for suffix in EDITED_SUFFIXES:
        if low.endswith(suffix):
            text = text[: -len(suffix)].strip()
            low = text.casefold()
    if not text:
        return None
    if low in MEDIA_PLACEHOLDERS or low in DELETED_MARKERS:
        return None
    if low.startswith("<attached:") or ATTACHED_RE.match(text):
        return None
    if low.startswith("poll:"):
        return None
    return text


def main() -> None:
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="Extract one author's messages from a WhatsApp export.")
    ap.add_argument("export", help="Path to the exported chat .txt (without media)")
    ap.add_argument("--author", help="Your name exactly as it appears in the export")
    ap.add_argument("--list-authors", action="store_true",
                    help="List all senders found, with message counts, then exit")
    ap.add_argument("--date-order", choices=["auto", "dmy", "mdy", "ymd"], default="auto",
                    help="Force date interpretation if auto-detection guesses wrong")
    ap.add_argument("--out", default=str(MY_MESSAGES_PATH), help="Output JSON path")
    args = ap.parse_args()

    export_path = Path(args.export)
    if not export_path.is_file():
        sys.exit(f"ERROR: file not found: {export_path}")

    raw_messages = parse_export(export_path)
    if not raw_messages:
        sys.exit("ERROR: no messages recognized. Is this a WhatsApp 'Export Chat' .txt file?\n"
                 "(iOS exports arrive as a .zip — extract it and point me at _chat.txt)")

    sender_counts = Counter(m["sender"] for m in raw_messages if m["sender"])

    if args.list_authors or not args.author:
        print(f"Parsed {len(raw_messages)} messages. Senders found:\n")
        for sender, count in sender_counts.most_common():
            print(f"  {count:>6}  {sender}")
        if not args.author:
            print("\nRe-run with:  --author \"<your name exactly as shown above>\"")
        return

    author = match_author(list(sender_counts), args.author)
    if not author:
        sys.exit(f"ERROR: no sender matches \"{args.author}\".\n"
                 "Run with --list-authors to see the exact names in this export.")

    order = detect_date_order(raw_messages, args.date_order)

    kept, dropped_noise, dropped_undated = [], 0, 0
    for msg in raw_messages:
        if msg["sender"] != author:
            continue
        text = clean_message_text(msg["text"])
        if text is None:
            dropped_noise += 1
            continue
        ts = to_timestamp(msg, order)
        if ts is None:
            dropped_undated += 1
            continue
        kept.append({"ts": ts.isoformat(timespec="minutes"), "text": text})

    if not kept:
        sys.exit(f"ERROR: matched author \"{author}\" but kept 0 messages after filtering.")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "mena-agent/my-messages@1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": export_path.name,
        "author": author,
        "date_order": order,
        "stats": {
            "messages_in_export": len(raw_messages),
            "author_messages": sender_counts[author],
            "kept": len(kept),
            "dropped_media_or_deleted": dropped_noise,
            "dropped_unparseable_date": dropped_undated,
            "first_message": kept[0]["ts"],
            "last_message": kept[-1]["ts"],
        },
        "messages": kept,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    s = payload["stats"]
    print(f"Author matched : {author}")
    print(f"Date order     : {order} (override with --date-order if wrong)")
    print(f"Kept           : {s['kept']} messages "
          f"({s['dropped_media_or_deleted']} media/deleted dropped)")
    print(f"Range          : {s['first_message']} -> {s['last_message']}")
    print(f"Wrote          : {out_path}")
    print("\nNext:  py agent\\analyze_style.py")


if __name__ == "__main__":
    main()
