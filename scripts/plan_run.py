#!/usr/bin/env python3
"""Decides what each workflow run should do, using the reliable 30-min trigger
as a clock instead of GitHub's flaky `schedule` events.

Every run:
  - Fetch headlines (always, handled by the workflow itself)

Every 6 hours (slots 00:00, 06:00, 12:00, 18:00 Israel time — first run
after each boundary):
  - DO_BRIEFING=true  → build_briefing.py writes briefing.json
  - DO_EMAIL=true     → send_email.py sends the digest

Once a day at ~07:00 Israel time (morning):
  - DO_FRONTPAGES=true → fetch_frontpages.py downloads today's covers

Once a day at ~14:00 Israel time (afternoon):
  - DO_FRONTPAGES=true → fetch_frontpages.py (catches later editions)

"Once per slot/day" is enforced with state/daily.json. Times are in
Asia/Jerusalem so DST is handled automatically.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ             = ZoneInfo("Asia/Jerusalem")
MORNING_HOUR   = 7    # front pages + first briefing slot of the day
AFTERNOON_HOUR = 14   # front pages again for later editions

STATE_PATH = Path(__file__).parent.parent / "state" / "daily.json"
BRIEFING_PATH = Path(__file__).parent.parent / "briefing.json"


def briefing_is_stale(slot_start) -> bool:
    """True if briefing.json is missing or its last SUCCESSFUL build predates the
    current 6-hour slot.

    build_briefing.py only updates briefing.json's "updated" field when it
    actually writes a briefing (on an empty/failed Gemini response it exits 0
    WITHOUT writing). So this timestamp is the real signal of whether the slot
    still needs a briefing — letting us retry through transient outages instead
    of consuming the slot on the first failed attempt.
    """
    try:
        data = json.loads(BRIEFING_PATH.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(data["updated"])
    except Exception:
        return True
    return updated < slot_start


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def emit_env(**values) -> None:
    gh_env = os.environ.get("GITHUB_ENV")
    if not gh_env:
        return
    with open(gh_env, "a", encoding="utf-8") as fh:
        for key, val in values.items():
            fh.write(f"{key}={val}\n")


def main() -> None:
    now   = datetime.now(TZ)
    today = now.date().isoformat()
    hour  = now.hour
    state = load_state()

    do_fp       = False
    do_briefing = False
    do_email    = False
    reasons     = []

    # ── Front pages: morning + afternoon (once each per day) ──────────────────
    if hour >= MORNING_HOUR and state.get("fp_morning") != today:
        do_fp = True
        state["fp_morning"] = today
        reasons.append("front pages (morning)")

    if hour >= AFTERNOON_HOUR and state.get("fp_afternoon") != today:
        do_fp = True
        state["fp_afternoon"] = today
        reasons.append("front pages (afternoon)")

    # ── World briefing + email: every 6-hour slot ─────────────────────────────
    # Slot 0 = 00:00-05:59, slot 1 = 06:00-11:59, etc.
    slot       = hour // 6
    slot_key   = f"{today}_s{slot}"
    slot_start = now.replace(hour=slot * 6, minute=0, second=0, microsecond=0)
    if state.get("briefing_slot") != slot_key:
        # First run of the slot: (re)build the briefing and send the digest once.
        do_briefing = True
        do_email    = True
        state["briefing_slot"] = slot_key
        reasons.append(f"briefing + email (slot {slot}, ~{slot*6:02d}:00-{slot*6+5:02d}:59)")
    elif briefing_is_stale(slot_start):
        # The slot's first attempt produced no briefing — e.g. a transient Gemini
        # outage, where build_briefing.py exits 0 without writing. Retry on every
        # run until it lands, instead of leaving the briefing stale for the whole
        # 6-hour slot. No email here, so a blip can't trigger duplicate digests.
        do_briefing = True
        reasons.append("briefing retry (no fresh briefing yet this slot)")

    # ── Manual overrides (workflow_dispatch inputs, handy for testing) ─────────
    if os.environ.get("FORCE_FRONTPAGES", "").lower() == "true":
        do_fp = True
        reasons.append("forced front pages")
    if os.environ.get("FORCE_EMAIL", "").lower() == "true":
        do_briefing = True
        do_email    = True
        reasons.append("forced email + briefing")
    if os.environ.get("FORCE_BRIEFING", "").lower() == "true":
        do_briefing = True
        reasons.append("forced briefing")

    reason = " | ".join(reasons) if reasons else "headlines only"

    save_state(state)
    emit_env(
        DO_FRONTPAGES=str(do_fp).lower(),
        DO_BRIEFING=str(do_briefing).lower(),
        DO_EMAIL=str(do_email).lower(),
    )

    print(f"Israel time {now:%Y-%m-%d %H:%M %Z} (hour={hour}, slot={slot}) → {reason}")
    print(f"  DO_FRONTPAGES={do_fp}  DO_BRIEFING={do_briefing}  DO_EMAIL={do_email}")
    print(f"  state={state}")


if __name__ == "__main__":
    main()
