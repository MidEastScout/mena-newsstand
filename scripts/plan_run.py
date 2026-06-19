#!/usr/bin/env python3
"""Decides what each workflow run should do, using the reliable 30-min trigger
as a clock instead of GitHub's flaky `schedule` events.

Background: GitHub frequently drops/delays scheduled runs, so the old design —
"send the email and refresh front pages on the 04:05 cron" — meant the email
arrived irregularly (or not at all) and the front pages went stale. The every-
30-min refresh, by contrast, is driven by an external cron-job.org trigger that
fires like clockwork. So we let THAT heartbeat drive everything:

  • every run            → refresh headlines.json (handled by the workflow)
  • first run >= 07:00   → also refresh front pages AND send the morning email
    Israel time            (once per day)
  • first run >= 14:00   → also refresh front pages (once per day), to pick up
    Israel time            the Western editions that post later

"Once per day" is enforced with a tiny committed marker (state/daily.json) that
records the last date each routine ran, keyed to Israel local dates. Times are
computed in Asia/Jerusalem so 07:00 means 7 AM local all year — DST included.

The decision is exported to GITHUB_ENV as DO_FRONTPAGES / DO_EMAIL.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")
MORNING_HOUR = 7        # 07:00 Israel — front pages + email
AFTERNOON_HOUR = 14     # 14:00 Israel — front pages only (Western editions)

STATE_PATH = Path(__file__).parent.parent / "state" / "daily.json"


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
    now = datetime.now(TZ)
    today = now.date().isoformat()
    hour = now.hour
    state = load_state()

    do_fp = False
    do_email = False
    reason = "headlines only"

    # Morning routine claims the day first: even if the 07:00/07:30 runs were
    # missed, the next heartbeat after 07:00 still sends the email + refreshes.
    if hour >= MORNING_HOUR and state.get("morning") != today:
        do_fp = True
        do_email = True
        state["morning"] = today
        reason = "morning routine (front pages + email)"
    elif hour >= AFTERNOON_HOUR and state.get("afternoon") != today:
        do_fp = True
        state["afternoon"] = today
        reason = "afternoon refresh (front pages)"

    # Manual override via workflow_dispatch inputs (handy for testing). These
    # force the action without claiming the daily marker.
    if os.environ.get("FORCE_FRONTPAGES", "").lower() == "true":
        do_fp = True
        reason += " + forced front pages"
    if os.environ.get("FORCE_EMAIL", "").lower() == "true":
        do_email = True
        reason += " + forced email"

    # Always rewrite the marker (no-op diff on headline-only runs, so git won't
    # commit it); on a claimed routine the new date lands in the same commit and
    # blocks the next heartbeat from repeating it.
    save_state(state)
    emit_env(DO_FRONTPAGES=str(do_fp).lower(), DO_EMAIL=str(do_email).lower())

    print(f"Israel time {now:%Y-%m-%d %H:%M %Z} (hour={hour}) -> {reason}")
    print(f"  DO_FRONTPAGES={do_fp}  DO_EMAIL={do_email}")
    print(f"  state={state}")


if __name__ == "__main__":
    main()
