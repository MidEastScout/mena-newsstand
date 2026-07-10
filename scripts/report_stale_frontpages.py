#!/usr/bin/env python3
"""Escalates chronic front-page staleness from a buried Actions-log warning to
a GitHub issue on the repo, so a dead cover source gets noticed the day it
breaks instead of days later.

Reads frontpages/manifest.json (written by fetch_frontpages.py just before
this runs). Papers whose `missed_editions` is >= FRONTPAGES_STALE_WARN_DAYS
(default 2 — same threshold as the ::warning:: in fetch_frontpages.py) are
"chronically stale": their source has failed on several consecutive days where
a new edition was expected, which one more retry won't fix.

  - If any exist: open ONE issue (title below) listing them, or update its
    body if it's already open — never a second issue, never comment spam.
  - If none exist and the issue is open: close it with a short comment, so the
    issue tracker mirrors reality after recovery.

Runs inside the workflow with GITHUB_TOKEN (needs `issues: write`). Every
failure path exits 0 — visibility must never break the data refresh itself.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

MANIFEST = Path(__file__).parent.parent / "frontpages" / "manifest.json"
THRESHOLD = int(os.environ.get("FRONTPAGES_STALE_WARN_DAYS", "2"))
ISSUE_TITLE = "Stale front pages: cover source needs attention"
LABEL = "frontpages-health"
API = "https://api.github.com"


def gh(session, method, path, **kwargs):
    r = session.request(method, f"{API}{path}", timeout=20, **kwargs)
    if r.status_code >= 400:
        print(f"  ! GitHub API {method} {path} -> {r.status_code}: {r.text[:200]}",
              file=sys.stderr)
    return r


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        print("GITHUB_TOKEN/GITHUB_REPOSITORY not set — skipping stale report.")
        return 0

    try:
        papers = json.loads(MANIFEST.read_text(encoding="utf-8"))["papers"]
    except Exception as exc:
        print(f"Cannot read manifest ({exc}) — skipping stale report.")
        return 0

    stale = [p for p in papers
             if isinstance(p.get("missed_editions"), int)
             and p["missed_editions"] >= THRESHOLD]

    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })

    # Find our issue among open issues. Matched by exact title, NOT by label:
    # a label filter silently matches nothing if the label was ever deleted,
    # and the next run would then open a duplicate issue.
    r = gh(s, "GET", f"/repos/{repo}/issues",
           params={"state": "open", "per_page": 100})
    existing = None
    if r.ok:
        existing = next((i for i in r.json()
                         if i.get("title") == ISSUE_TITLE
                         and "pull_request" not in i), None)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not stale:
        if existing:
            num = existing["number"]
            gh(s, "POST", f"/repos/{repo}/issues/{num}/comments",
               json={"body": f"All covers are refreshing again as of {now} — "
                             "closing automatically."})
            gh(s, "PATCH", f"/repos/{repo}/issues/{num}",
               json={"state": "closed"})
            print(f"Recovered — closed issue #{num}.")
        else:
            print("No chronically stale covers — nothing to report.")
        return 0

    lines = [
        f"The covers below have missed **{THRESHOLD}+ expected editions** in a "
        "row (per-paper no-print days already excluded), which means their "
        "upstream source is dead or has changed — automatic retries alone "
        "won't recover them.",
        "",
        "| Paper | Last edition shown | Expected editions missed | Last source |",
        "|---|---|---|---|",
    ]
    for p in sorted(stale, key=lambda x: -x["missed_editions"]):
        lines.append(f"| {p['name']} (`{p['id']}`) | {p.get('date') or '?'} "
                     f"| {p['missed_editions']} | {p.get('src') or '?'} |")
    lines += [
        "",
        "What to do: run the *Probe front-page sources* workflow (or "
        "`scripts/probe_frontpages.py`) to find a working source, then update "
        "the paper's `src` list in `scripts/fetch_frontpages.py`.",
        "",
        f"_Auto-maintained by `scripts/report_stale_frontpages.py` — last "
        f"updated {now}. This issue closes itself when every cover refreshes "
        "again._",
    ]
    body = "\n".join(lines)

    if existing:
        # Refresh the body only when the situation changed, so watchers aren't
        # re-notified every 30 minutes about the same outage.
        if existing.get("body") != body:
            gh(s, "PATCH", f"/repos/{repo}/issues/{existing['number']}",
               json={"body": body})
            print(f"Updated issue #{existing['number']} "
                  f"({len(stale)} stale cover(s)).")
        else:
            print(f"Issue #{existing['number']} already up to date.")
    else:
        r = gh(s, "POST", f"/repos/{repo}/issues",
               json={"title": ISSUE_TITLE, "body": body, "labels": [LABEL]})
        if not r.ok:
            # Some tokens can open issues but not create the missing label.
            r = gh(s, "POST", f"/repos/{repo}/issues",
                   json={"title": ISSUE_TITLE, "body": body})
        if r.ok:
            print(f"Opened issue #{r.json().get('number')} "
                  f"({len(stale)} stale cover(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
