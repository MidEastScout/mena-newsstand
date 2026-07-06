#!/usr/bin/env python3
"""Decides what each workflow run should do, using the reliable 30-min trigger
as a clock instead of GitHub's flaky `schedule` events.

Every run:
  - Fetch headlines (always, handled by the workflow itself)

Every 6 hours (slots 00:00, 06:00, 12:00, 18:00 Israel time — first run
after each boundary):
  - DO_BRIEFING=true  → build_briefing.py writes briefing.json

Once a day at ~07:00 Israel time (morning):
  - DO_FRONTPAGES=true (full refresh) → fetch_frontpages.py downloads today's
    covers; titles not yet scanned upstream fall back to yesterday's edition.

Once a day at ~14:00 Israel time (afternoon):
  - DO_FRONTPAGES=true (full refresh) → fetch_frontpages.py (catches re-plates)

Between those, on every ~30-min run after the morning grab:
  - DO_FRONTPAGES=true + FRONTPAGES_FILL_ONLY=true → re-fetch ONLY the papers
    still showing an older edition, so each title upgrades to today's cover
    within one cycle of appearing upstream instead of waiting for 14:00. This
    stops firing once every cover is current (frontpages_stale() → False).

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
MANIFEST_PATH = Path(__file__).parent.parent / "frontpages" / "manifest.json"


def frontpages_stale(today: str) -> bool:
    """True if the front-page manifest is missing/empty or any tracked paper is
    still showing an edition older than `today` (an ISO date string).

    Drives the between-slots catch-up: while this is True the workflow keeps
    re-fetching the stragglers each cycle; once every cover is dated today it
    returns False and the catch-up goes quiet until tomorrow.

    Note: fetch_frontpages.py stamps manifest dates in UTC, while `today` here
    is Asia/Jerusalem. The catch-up only runs in the MORNING_HOUR..23 window
    (UTC+2/+3), where the Jerusalem and UTC calendar dates always agree, so the
    comparison is sound.
    """
    try:
        papers = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["papers"]
    except Exception:
        return True
    if not papers:
        return True
    return any(p.get("date") != today for p in papers)


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
    fp_force    = False   # full refresh (morning/afternoon) vs fill-only catch-up
    do_briefing = False
    reasons     = []

    # ── Front pages: morning + afternoon full refreshes (once each per day) ───
    if hour >= MORNING_HOUR and state.get("fp_morning") != today:
        do_fp = True
        fp_force = True
        state["fp_morning"] = today
        reasons.append("front pages (morning)")

    if hour >= AFTERNOON_HOUR and state.get("fp_afternoon") != today:
        do_fp = True
        fp_force = True
        state["fp_afternoon"] = today
        reasons.append("front pages (afternoon)")

    # Between the fixed slots, keep catching today's editions as they appear
    # upstream: re-fetch only the papers still a day behind. Self-limiting —
    # goes quiet once every cover is current, so CDNs aren't hit needlessly.
    if not do_fp and hour >= MORNING_HOUR and frontpages_stale(today):
        do_fp = True
        reasons.append("front pages (catch-up: some covers still stale)")

    # ── World briefing: every 6-hour slot ─────────────────────────────────────
    # Slot 0 = 00:00-05:59, slot 1 = 06:00-11:59, etc.
    slot       = hour // 6
    slot_key   = f"{today}_s{slot}"
    slot_start = now.replace(hour=slot * 6, minute=0, second=0, microsecond=0)
    if state.get("briefing_slot") != slot_key:
        # First run of the slot: (re)build the briefing once.
        do_briefing = True
        state["briefing_slot"] = slot_key
        reasons.append(f"briefing (slot {slot}, ~{slot*6:02d}:00-{slot*6+5:02d}:59)")
    elif briefing_is_stale(slot_start):
        # The slot's first attempt produced no briefing — e.g. a transient Gemini
        # outage, where build_briefing.py exits 0 without writing. Retry on every
        # run until it lands, instead of leaving the briefing stale for the whole
        # 6-hour slot.
        do_briefing = True
        reasons.append("briefing retry (no fresh briefing yet this slot)")

    # ── Manual overrides (workflow_dispatch inputs, handy for testing) ─────────
    if os.environ.get("FORCE_FRONTPAGES", "").lower() == "true":
        do_fp = True
        fp_force = True
        reasons.append("forced front pages")
    if os.environ.get("FORCE_BRIEFING", "").lower() == "true":
        do_briefing = True
        reasons.append("forced briefing")

    reason = " | ".join(reasons) if reasons else "headlines only"

    # Catch-up runs fill only the stale papers; the fixed/forced slots do a full
    # refresh. (Only meaningful when DO_FRONTPAGES is true.)
    fill_only = do_fp and not fp_force

    save_state(state)
    emit_env(
        DO_FRONTPAGES=str(do_fp).lower(),
        FRONTPAGES_FILL_ONLY=str(fill_only).lower(),
        DO_BRIEFING=str(do_briefing).lower(),
    )

    print(f"Israel time {now:%Y-%m-%d %H:%M %Z} (hour={hour}, slot={slot}) → {reason}")
    print(f"  DO_FRONTPAGES={do_fp}  FILL_ONLY={fill_only}  "
          f"DO_BRIEFING={do_briefing}")
    print(f"  state={state}")


if __name__ == "__main__":
    main()
