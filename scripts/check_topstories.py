#!/usr/bin/env python3
"""Pre-publish grounding gate for top_stories.json.

Runs in the workflow right after build_topstories.py — and before the push-
notification sender — so a Top-5 title whose claims can't be traced back to
its own cluster's source headlines never reaches the site or a phone. The
builder already verifies every line it attaches; this gate is the independent
second net that also catches builder regressions, verifier updates landing
between builds, and hand-edited data.

For every story it verifies the DISPLAYED title — the neutral `summary` when
present, else the representative headline — with title_grounding.verify_title
against the story's member headlines, enriched with each member's English
snippet from headlines.json (joined by URL: Arabic/Persian-titled outlets
carry their grounding signal in the snippet).

  * A failing `summary` is STRIPPED (together with `summary_he`): the story
    falls back to its representative headline, which is a real source headline
    and grounded by construction. The file is rewritten and the run continues —
    self-healing, never publish-blocking.
  * A failing representative headline cannot be stripped (it IS the fallback),
    so it is warned about only; pick_representative_heuristic already demotes
    stale/loaded reps, so this should stay rare.

Exit status: 0 in default mode even when it fixed something (the gate must
never break the ~30-min refresh). With --check-only nothing is rewritten and
any finding exits 1 — for local inspection and tests.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from title_grounding import verify_title

ROOT = Path(__file__).parent.parent
TOP_PATH = ROOT / "top_stories.json"
HL_PATH = ROOT / "headlines.json"


def snippets_by_url(headlines: dict) -> dict[str, str]:
    """URL → English snippet across every outlet in headlines.json."""
    out = {}
    for outlets in (headlines.get("regions") or {}).values():
        for o in outlets:
            for h in (o.get("headlines") or []):
                if h.get("url"):
                    out[h["url"]] = h.get("snippet") or ""
    return out


def check_stories(top: dict, snippets: dict[str, str]) -> list[dict]:
    """Verify each story's displayed title. Returns findings:
    [{rank, kind: 'summary'|'rep', title, problems, story}]."""
    findings = []
    for s in top.get("stories") or []:
        members = []
        for m in s.get("members") or []:
            mm = dict(m)
            if not mm.get("snippet"):
                mm["snippet"] = snippets.get(mm.get("url") or "", "")
            members.append(mm)
        summary = (s.get("summary") or "").strip()
        if summary:
            res = verify_title(summary, members)
            if not res.ok:
                findings.append({"rank": s.get("rank"), "kind": "summary",
                                 "title": summary, "problems": res.problems,
                                 "story": s})
        rep_title = ((s.get("rep") or {}).get("title") or "").strip()
        if rep_title:
            res = verify_title(rep_title, members)
            if not res.ok:
                findings.append({"rank": s.get("rank"), "kind": "rep",
                                 "title": rep_title, "problems": res.problems,
                                 "story": s})
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check-only", action="store_true",
                    help="report findings without rewriting the file; exit 1 on any")
    args = ap.parse_args()

    try:
        top = json.loads(TOP_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"No top_stories.json to check ({exc}) — nothing to do")
        return 0
    try:
        snippets = snippets_by_url(json.loads(HL_PATH.read_text(encoding="utf-8")))
    except Exception:
        snippets = {}   # verify against member titles only — still a real check

    findings = check_stories(top, snippets)
    changed = False
    for f in findings:
        # ::warning:: makes the finding an annotation on the Actions run.
        print(f"::warning::Top-5 story #{f['rank']} {f['kind']} failed grounding: "
              f"{f['title']!r} — " + "; ".join(f["problems"]))
        if f["kind"] == "summary" and not args.check_only:
            f["story"].pop("summary", None)
            f["story"].pop("summary_he", None)
            changed = True

    if changed:
        TOP_PATH.write_text(json.dumps(top, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"Stripped {sum(1 for f in findings if f['kind'] == 'summary')} "
              "unverifiable summary line(s) — those stories fall back to their "
              "representative headlines. top_stories.json rewritten.")
    elif not findings:
        print(f"All {len(top.get('stories') or [])} top-story titles verified "
              "against their sources.")

    return 1 if (args.check_only and findings) else 0


if __name__ == "__main__":
    sys.exit(main())
