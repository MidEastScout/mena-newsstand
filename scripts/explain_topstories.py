#!/usr/bin/env python3
"""Readable report over the Top-5 pipeline's per-cycle debug trace.

The pipeline writes state/topstories_debug.json every refresh cycle (see
scripts/build_topstories.py). That file is complete but is JSON; this renders
it as something you can actually read, so questions like "why did this story
rank #1 today?", "why is that the headline?" and "why did outlet X vanish from
the Top 5?" are answerable without guesswork.

Usage:
    python3 scripts/explain_topstories.py               # the whole cycle
    python3 scripts/explain_topstories.py --stage rank  # one stage
    python3 scripts/explain_topstories.py --why 1       # why story #1 ranked there
    python3 scripts/explain_topstories.py --dropped     # everything filtered out, with reasons
    python3 scripts/explain_topstories.py --outlet "Kan 11"   # trace one outlet through the stages
    python3 scripts/explain_topstories.py --health      # recent validation failures
    python3 scripts/explain_topstories.py --file path/to/topstories_debug.json

Exits non-zero if the cycle it describes failed validation, so it doubles as
a check in a shell pipeline.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEBUG = ROOT / "state" / "topstories_debug.json"
FAILURES = ROOT / "state" / "topstories_failures.json"

# ANSI, disabled when piped so the output stays greppable.
_TTY = sys.stdout.isatty()
def _c(code, s):  return f"\033[{code}m{s}\033[0m" if _TTY else s
def bold(s):      return _c("1", s)
def dim(s):       return _c("2", s)
def green(s):     return _c("32", s)
def red(s):       return _c("31", s)
def yellow(s):    return _c("33", s)


def rule(title: str) -> None:
    print(f"\n{bold('━' * 78)}\n{bold(title)}\n{bold('━' * 78)}")


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"No trace at {path} — run scripts/build_topstories.py first.")
    except Exception as exc:
        sys.exit(f"Could not read {path}: {exc}")


# ---------------------------------------------------------------------------
def show_fetch(st: dict) -> None:
    rule("1 · FETCH — what came in")
    outlets = st.get("outlets") or {}
    print(f"{st.get('items', 0)} headlines from {len(outlets)} outlets "
          f"(headlines.json updated {st.get('headlines_updated', '?')})")
    problems = [(s, o) for s, o in outlets.items() if o.get("stale") or o.get("error")
                or not o.get("headlines")]
    if problems:
        print(f"\n{yellow('Outlets needing attention:')}")
        for s, o in problems:
            flags = []
            if o.get("error"):
                flags.append(f"error={o['error']}")
            if o.get("stale"):
                flags.append("carried forward (stale)")
            if not o.get("headlines"):
                flags.append("no headlines")
            print(f"  {s:22s} {', '.join(flags)}")
    else:
        print(dim("  every outlet returned fresh headlines"))


def show_normalize(st: dict) -> None:
    rule("2 · NORMALIZE — forcing English before anything else runs")
    s = st.get("stats", {})
    print(f"{s.get('in', 0)} in → {bold(str(s.get('kept', 0)))} kept\n")
    print("  Display titles resolved from:")
    for key, label in (("title_native", "the outlet's own English headline"),
                       ("title_translated", "machine translation of a non-English headline"),
                       ("title_snippet", "the item's English snippet")):
        if s.get(key):
            print(f"    {s[key]:4d}  {label}")
    dropped = st.get("dropped") or []
    if dropped:
        by_reason: dict[str, list] = {}
        for d in dropped:
            by_reason.setdefault(d["reason"], []).append(d)
        print(f"\n  {yellow('Dropped here:')}")
        for reason, items in by_reason.items():
            print(f"    {len(items):4d}  {reason}")
            for d in items[:3]:
                print(dim(f"           {d['source']}: {d['title'][:64]}"))
            if len(items) > 3:
                print(dim(f"           … and {len(items) - 3} more (--dropped for all)"))


def show_relevance(st: dict) -> None:
    rule("3 · RELEVANCE — strict Top-5 topical filter")
    print(f"{st.get('in', 0)} in → {bold(str(st.get('kept', 0)))} kept "
          f"({len(st.get('dropped') or [])} dropped)\n")
    dropped = st.get("dropped") or []
    by_reason: dict[str, int] = {}
    for d in dropped:
        by_reason[d["reason"]] = by_reason.get(d["reason"], 0) + 1
    for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {reason}")
    if dropped:
        print(dim("\n  (--dropped lists every one with its security/off-topic scores)"))


def show_cluster(st: dict) -> None:
    rule("4 · CLUSTER — grouping items into stories")
    sizes = st.get("sizes") or []
    multi = [n for n in sizes if n > 1]
    print(f"method: {bold(st.get('method', '?'))} · {st.get('clusters', 0)} clusters")
    print(f"  {len(multi)} multi-item stories (sizes: {', '.join(map(str, multi))})")
    print(f"  {len(sizes) - len(multi)} single-item clusters "
          + dim("(can never qualify — need 2+ outlets)"))


def show_rank(st: dict, verbose: bool = False) -> None:
    rule("5 · RANK — " + st.get("rule", "distinct outlets → camp spread → recency"))
    print(dim(f"minimum {st.get('min_outlets', 2)} distinct outlets to qualify; "
              f"ties break on outlets, then camps, then recency\n"))
    rows = [r for r in st.get("candidates", []) if "rank_key" in r]
    if not rows and st.get("candidates"):
        sys.exit("This trace predates the current debug format — "
                 "re-run scripts/build_topstories.py to regenerate it.")
    print(f"  {'#':>3}  {'outlets':>7} {'camps':>5}  {'newest':16s}  headline")
    print(dim("  " + "─" * 74))
    for r in rows:
        k = r["rank_key"]
        sel = r.get("selected_rank")
        mark = green(f"#{sel}") if sel else (dim(" ·") if r["qualified"] else red(" ✗"))
        line = (f"  {mark:>3}  {k['outlets']:>7} {k['camps']:>5}  "
                f"{(k['newest'] or '—')[:16]:16s}  {r['headline'][:44]}")
        print(line)
        if not r["qualified"]:
            print(dim(f"        └─ excluded: {r['why_not']}"))
        elif verbose:
            print(dim(f"        └─ outlets: {', '.join(r['outlet_names'])}"))
    note = next((r for r in st.get("candidates", []) if "note" in r), None)
    if note:
        print(dim(f"\n  {note['note']}"))


def show_validate(st: dict) -> None:
    rule("6 · VALIDATE — the gate before anything is published")
    if st.get("forced_failure"):
        print(yellow(f"  self-test active: TOPSTORIES_FORCE_FAIL={st['forced_failure']}"))
    for r in st.get("repairs") or []:
        print(yellow(f"  repaired  story {r['story']}: {r['action']} — {r.get('text', '')[:50]}"))
    if st.get("ok"):
        print(green("  PASSED") + " — English-only, ≥ minimum outlets, on-topic, correctly ordered")
    else:
        print(red("  FAILED") + " — nothing was published this cycle:")
        for f in st.get("failures", []):
            where = f" (story {f['story']})" if f.get("story") else ""
            print(red(f"    {f['check']}{where}: {f['detail']}"))


def show_publish(st: dict, health: dict | None) -> None:
    rule("7 · PUBLISH")
    if st.get("published"):
        print(green("  published top_stories.json") + " and refreshed the last-known-good copy")
    else:
        print(yellow(f"  did NOT publish — {st.get('fallback')}"))
        age = st.get("serving_age_hours")
        if age is not None:
            print(f"  currently serving data {age}h old")
        for key, label in (("rejected_existing", "previous output rejected"),
                           ("rejected_lkg", "last-known-good rejected")):
            for f in st.get(key) or []:
                print(dim(f"    {label}: {f['check']} — {f['detail'][:60]}"))
    if health:
        streak = health.get("consecutive_failures", 0)
        if streak:
            print(red(f"  {streak} consecutive failed cycle(s)"))
        print(dim(f"  last success: {(health.get('last_success') or '—')[:19]}"))


def show_why(debug: dict, rank_n: int) -> None:
    """Answer 'why did this story rank #N?'"""
    st = debug["stages"].get("rank", {})
    rows = [r for r in st.get("candidates", []) if "rank_key" in r]
    me = next((r for r in rows if r.get("selected_rank") == rank_n), None)
    if not me:
        sys.exit(f"No story ranked #{rank_n} in this cycle.")
    k = me["rank_key"]
    rule(f"WHY DID THIS RANK #{rank_n}?")
    print(bold(me["headline"]) + "\n")
    print(f"It is carried by {bold(str(k['outlets']) + ' distinct outlets')} across "
          f"{k['camps']} camps, newest item {k['newest']}.")
    print(f"  outlets: {', '.join(me['outlet_names'])}")
    print(f"  camps:   {', '.join(me['camps'])}")

    above = [r for r in rows if r.get("selected_rank") and r["selected_rank"] < rank_n]
    below = [r for r in rows if r.get("selected_rank") and r["selected_rank"] > rank_n]
    if above:
        print(f"\n{bold('Ranked above it:')}")
        for r in above:
            rk = r["rank_key"]
            print(f"  #{r['selected_rank']} {rk['outlets']} outlets — {r['headline'][:52]}")
    if below:
        r = below[0]
        rk = r["rank_key"]
        why = ("more outlets" if k["outlets"] > rk["outlets"] else
               "same outlets, wider camp spread" if k["camps"] > rk["camps"] else
               "same outlets and camps, more recent")
        print(f"\n{bold('Beat the next story on:')} {why}")
        print(f"  #{r['selected_rank']} {rk['outlets']} outlets / {rk['camps']} camps "
              f"— {r['headline'][:48]}")

    rep = me.get("representative")
    if rep:
        print(f"\n{bold('Why this headline heads the story:')}")
        print(f"  [{rep['source']}] {rep['title']}")
        for w in rep["why"]:
            print(f"    · {w}")
        if rep["passed_over"]:
            print(f"\n  {dim('Members passed over:')}")
            for p in rep["passed_over"]:
                print(dim(f"    [{p['source']}] {p['title'][:56]}"))
                print(dim(f"       rejected: {p['rejected_by']}"))


def show_dropped(debug: dict) -> None:
    """Everything the pipeline filtered out this cycle, with reasons."""
    rule("EVERYTHING DROPPED THIS CYCLE")
    n = debug["stages"].get("normalize", {})
    print(bold("\nNormalize:"))
    for d in n.get("dropped") or []:
        print(f"  {d['source']:20s} {d['reason'][:38]:38s} {d['title'][:50]}")
    r = debug["stages"].get("relevance", {})
    print(bold("\nRelevance:") + dim("  (sec = security signals, off = off-topic signals)"))
    for d in r.get("dropped") or []:
        print(f"  {d['source']:20s} sec={d['sec']:<2} off={d['off']:<2} "
              f"{d['reason'][:30]:30s} {d['title'][:44]}")
    print(dim("\nItems dropped here still appear on the normal headlines wall — "
              "they are only excluded from the Top 5."))


def show_outlet(debug: dict, name: str) -> None:
    """Trace one outlet through the stages — 'why did X vanish from the Top 5?'"""
    rule(f"OUTLET TRACE — {name}")
    st = debug["stages"]
    meta = (st.get("fetch", {}).get("outlets") or {}).get(name)
    if meta is None:
        known = sorted((st.get("fetch", {}).get("outlets") or {}).keys())
        sys.exit(f"Unknown outlet {name!r}. Known: {', '.join(known)}")
    print(f"fetch:      {meta['headlines']} headlines (lang={meta.get('lang')}"
          + (", carried forward/stale" if meta.get("stale") else "")
          + (f", error={meta['error']}" if meta.get("error") else "") + ")")
    for stage in ("normalize", "relevance"):
        mine = [d for d in (st.get(stage, {}).get("dropped") or []) if d["source"] == name]
        if mine:
            print(f"{stage + ':':11s} {len(mine)} dropped")
            for d in mine:
                extra = f" [sec={d['sec']} off={d['off']}]" if "sec" in d else ""
                print(f"              · {d['title'][:56]}")
                print(dim(f"                {d['reason']}{extra}"))
        else:
            print(f"{stage + ':':11s} none dropped")
    hits = [r for r in st.get("rank", {}).get("candidates", [])
            if name in (r.get("outlet_names") or [])]
    if hits:
        print("rank:       appears in:")
        for r in hits:
            where = (green(f"#{r['selected_rank']}") if r.get("selected_rank")
                     else red("not selected"))
            print(f"              {where} {r['headline'][:52]}")
            if not r["qualified"]:
                print(dim(f"                {r['why_not']}"))
    else:
        print("rank:       " + yellow("no surviving item reached a cluster"))


def show_health() -> None:
    rule("VALIDATION HEALTH — recent failures")
    h = load(FAILURES)
    streak = h.get("consecutive_failures", 0)
    print((red(f"{streak} consecutive failed cycles") if streak else green("healthy — 0 consecutive failures")))
    print(f"  last success: {(h.get('last_success') or '—')[:19]}")
    print(f"  last failure: {(h.get('last_failure') or '—')[:19]}")
    recent = h.get("recent") or []
    if recent:
        print(f"\n  {bold('Last ' + str(len(recent)) + ' failures:')}")
        for r in recent:
            print(f"    {r['at'][:19]}  {', '.join(r['checks']):28s} → {r['fallback']}")
            for f in r.get("failures", [])[:2]:
                print(dim(f"      {f['check']}: {f['detail'][:66]}"))
    else:
        print(dim("\n  no failures recorded"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", type=Path, default=DEBUG, help="debug trace to read")
    ap.add_argument("--stage", choices=["fetch", "normalize", "relevance", "cluster",
                                        "rank", "validate", "publish"])
    ap.add_argument("--why", type=int, metavar="RANK", help="explain why a story ranked there")
    ap.add_argument("--dropped", action="store_true", help="everything filtered out, with reasons")
    ap.add_argument("--outlet", metavar="NAME", help="trace one outlet through the stages")
    ap.add_argument("--health", action="store_true", help="recent validation failures")
    ap.add_argument("-v", "--verbose", action="store_true", help="show outlet lists in the rank table")
    args = ap.parse_args()

    if args.health:
        show_health()
        return
    debug = load(args.file)
    st = debug.get("stages", {})

    if args.why is not None:
        show_why(debug, args.why)
    elif args.dropped:
        show_dropped(debug)
    elif args.outlet:
        show_outlet(debug, args.outlet)
    elif args.stage:
        fn = {"fetch": show_fetch, "normalize": show_normalize, "relevance": show_relevance,
              "cluster": show_cluster, "validate": show_validate}.get(args.stage)
        if args.stage == "rank":
            show_rank(st.get("rank", {}), args.verbose)
        elif args.stage == "publish":
            show_publish(st.get("publish", {}), debug.get("health"))
        else:
            fn(st.get(args.stage, {}))
    else:
        status = green("PASSED") if debug.get("ok") else red("FAILED")
        print(f"\nTop-5 pipeline cycle {bold(debug.get('updated', '?')[:19])} — {status}")
        show_fetch(st.get("fetch", {}))
        show_normalize(st.get("normalize", {}))
        show_relevance(st.get("relevance", {}))
        show_cluster(st.get("cluster", {}))
        show_rank(st.get("rank", {}), args.verbose)
        show_validate(st.get("validate", {}))
        show_publish(st.get("publish", {}), debug.get("health"))
        print(dim("\n--why N · --dropped · --outlet NAME · --health · --stage NAME for detail\n"))

    if not debug.get("ok", True):
        sys.exit(1)


if __name__ == "__main__":
    main()
