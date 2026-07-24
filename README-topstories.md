# Top 5 Stories — pipeline reference

"Top Stories Right Now" is the strip at the top of the Headlines tab: the five
biggest stories on the wall right now, ranked by how many different outlets are
carrying each one.

This document is the maintainer's contract for that feature. If you change the
logic, change this file with it — the thresholds below are asserted in
`tests/test_topstories.py`, so they cannot drift silently.

---

## Where the code lives

| File | Role |
| --- | --- |
| `scripts/topstories_pipeline.py` | The stage logic. Pure functions — no network, no file I/O, no clock (`now` is injectable). This is what the tests drive. |
| `scripts/build_topstories.py` | The runner. Owns all I/O and the optional network services, and orchestrates the stages. |
| `scripts/explain_topstories.py` | Readable report over a cycle's debug trace. |
| `tests/test_topstories.py` | Regression tests, stdlib `unittest` — one class per failure this feature has shipped. |
| `index.html` | Renders `top_stories.json`. Contains **no** clustering logic. |

The split matters: every rule that decides what appears on the site is in the
first file and is directly testable without a network, a clock, or a fixture
server.

---

## The seven stages

Each stage does one job and hands a plain list of dicts to the next, so a
failure is always attributable to one of them.

### 1 · Fetch — `build_topstories.fetch_stage()`

Reads `headlines.json` (written earlier in the same CI run by
`fetch_headlines.py`) and flattens regions → outlets → headlines into one list,
each item keeping its outlet and camp.

### 2 · Normalize — `normalize_stage()`

**Guarantees every item carries a `display_title` that is English, and runs
before clustering** so nothing non-English can reach the display layer.

English is resolved in this order, and the result is re-checked with
`is_english_display()` whichever branch produced it:

1. the outlet's own headline, when it is already English → `title_source: native`
2. a machine translation of the headline (Azure Translator, cached per title in
   `state/topstories_en_cache.json`) → `translated`
3. the first sentence of the item's English snippet → `snippet`
4. nothing English obtainable → **the item is dropped from this feature.** It
   still appears on the normal headlines wall.

Also drops dated items older than the freshness window. Undated items are kept
(many outlets publish without a timestamp).

> **Why the snippet is only third.** `fetch_headlines.py` fills `snippet` with
> an AI summary, but when Gemini's daily quota is spent it falls back to
> copying the feed's own description — which is Hebrew or Arabic for those
> outlets. Trusting the snippet blindly was one of the original leak paths, so
> its script is checked like everything else.

> **Why English is forced here and not at render time.** The historic bug was a
> script guard that screened Arabic and Persian but deliberately skipped
> Hebrew, applied late. Forcing the language at the front of the pipeline means
> clustering, ranking and the gate all operate on the same strings the reader
> sees.

### 3 · Relevance — `relevance_stage()`

A **stricter second pass** on top of the feed-level filter in
`fetch_headlines.py`. That one is deliberately high-precision (it only drops
unambiguous sports/ads/lifestyle) because it protects the whole wall. Top 5 is
the most visible surface on the site, so a bad story here costs more than one
buried in the feed.

Applied **per item, not per cluster**: an item must carry its own
security/geopolitics signal. This is what stops an off-topic item riding into
the Top 5 inside a cluster whose other members are security stories — how exam
results and human-interest items reached the strip before.

An item is dropped when it is a natural disaster or accident with no conflict
dimension, has no security signal at all, or its off-topic signals outweigh its
security signals. Vocabulary lives in `SEC_WORDS` / `SEC_PHRASES` /
`OFF_WORDS` / `OFF_PHRASES` / `DISASTER_WORDS` / `CONFLICT_WORDS`.

### 4 · Cluster — `cluster_stage()`

Groups items reporting the same underlying event. Two paths, both feeding the
same downstream gates:

- **Heuristic** (default, no key needed). Strong entities that genuinely
  co-occur merge into a topic anchor; each item joins its best-fit anchor;
  within an anchor, distinct sub-events split by wording overlap. A leftover
  item folds into a sub-event only on real event-level affinity.
- **Semantic** (`ANTHROPIC_API_KEY` set). Claude groups by meaning, which
  catches paraphrases sharing almost no vocabulary. Its output is validated
  (indices in range, each assigned once) and then treated exactly like the
  heuristic's.

**Shared geography is never evidence.** Country and region words are subtracted
before overlap is measured (`GEO_TOKENS`). In this feed nearly every headline
names a place, so counting geography merged unrelated stories — an education
item was folded into the West Bank violence story because both said "Gaza" and
"the West Bank", inflating that story's outlet count.

Entity vocabulary is in `ENTITIES_STRONG` (specific events, people, places —
sharing one is real evidence) and `ENTITIES_BROAD` (nation-level lenses like
Iran or Israel, which co-occur across unrelated stories and must never merge
two items on their own).

### 5 · Rank — `rank_stage()`

**The ranking rule, in priority order, all descending:**

1. **number of distinct outlets** carrying the story
2. **spread across outlet camps** — `israeli`, `gulf`, `panarab`, `iranian`,
   `levant`, `turkish`, `intl` (mapped per outlet in `SRC_CATS`, displayed in
   `CAT_ORDER` order)
3. **recency** of the newest member

Ties beyond that break on the representative's URL, so repeated runs over
unchanged data produce identical output.

A cluster qualifies only if it has at least `MIN_STORY_OUTLETS` distinct outlets
and passes a cluster-level relevance check. The top `TOP_N` qualifiers are kept
— **if fewer qualify, fewer are shown.** The strip is never padded with weak
stories.

> **The sort key is the number on the badge.** An earlier version ranked by a
> freshness-decayed outlet weight while displaying the raw count, so the
> displayed order looked wrong in 67% of cycles. Staleness is now handled at
> the input (stage 2) instead, which leaves ranking as the plain count you can
> verify by eye.

**Which member's headline leads** is chosen by `pick_representative()`: neutral
wording and current (non-stale) framing are gates, then freshness, coverage of
the story's dominant entities, native-English over translated, mainstream
outlet phrasing, non-stub, natural length, centrality, recency.
`describe_representative()` explains the choice for the debug trace.

### 6 · Validate — `validate_stage()`

The gate. Re-verifies from scratch, without trusting any upstream stage:

| Check | Rule | On failure |
| --- | --- | --- |
| `english` | Every displayed string — summary, representative title, member titles — passes `is_english_display()` | A bad **summary** is repaired (dropped; the guaranteed-English headline shows instead). A bad **title** fails the cycle. |
| `outlets` | Every story has ≥ `MIN_STORY_OUTLETS` distinct outlets, and its `outlets` field matches its members | Fails the cycle |
| `relevance` | No story matches the irrelevant-content filters, re-checked over full member text including snippets | Fails the cycle |
| `ordering` | Stories are in ranking-rule order | Fails the cycle |
| `structure` | Contiguous ranks 1..N, non-empty members, members newest-first | Fails the cycle |

The summary is the only displayed string that is repairable, because the
representative headline behind it is already guaranteed English.

### 7 · Publish — `publish_stage()`

On success: atomic write of `top_stories.json`, and the same payload is copied
to `state/topstories_lkg.json` as the new last-known-good.

On failure, **nothing broken is ever published.** The fallback chain, in order:

1. keep the existing `top_stories.json` — if it still passes the gate
2. else restore `state/topstories_lkg.json` — if *it* still passes
3. else publish `{"stories": []}`, which makes the site hide the strip entirely

Both fallback candidates are re-validated before being served (every check
except relevance, which needs snippets a published file doesn't carry), so a
corrupted file can't be recycled into place just because it was there before.

---

## Thresholds

Asserted in `tests/test_topstories.py::Thresholds` — change them there too.

| Constant | Value | Meaning |
| --- | --- | --- |
| `TOP_N` | 5 | Stories shown |
| `MIN_STORY_OUTLETS` | 2 | Distinct outlets required to qualify as "top". Prevents a single-outlet story filling a slot. |
| `FRESHNESS_WINDOW_H` | 48 | Dated items older than this never enter the pipeline |
| `STALE_GAP_SEC` | 21600 (6h) | The headline must come from the story's newest wave of coverage |
| `FAIL_ESCALATE_AFTER` | 3 | Consecutive failed cycles before CI escalates warning → error |
| `LKG_STALE_HOURS` | 3.0 | Age of served fallback data that escalates on its own |

---

## Debugging a cycle

Every run writes `state/topstories_debug.json` — a per-stage trace: what was
fetched, what each stage dropped and why, every candidate cluster with its
ranking signals and outlet list, the validation verdict, and the publish
outcome. Read it with:

```bash
python3 scripts/explain_topstories.py            # the whole cycle, stage by stage
python3 scripts/explain_topstories.py --why 1    # why story #1 ranked there
python3 scripts/explain_topstories.py --dropped  # everything filtered out, with reasons
python3 scripts/explain_topstories.py --outlet "Kan 11"   # trace one outlet
python3 scripts/explain_topstories.py --health   # recent validation failures
python3 scripts/explain_topstories.py --stage rank        # one stage only
```

It exits non-zero when the cycle it describes failed validation, so it also
works as a check in a shell pipeline.

`--why N` answers the ranking question directly: the outlet list behind the
count, what the story beat and on which tiebreak, why that headline leads, and
which members were passed over and for what reason.

Cross-cycle health lives in `state/topstories_failures.json` (consecutive
failures, last success, the last 20 failure records), because the debug trace
is overwritten every run — without it a 03:00 failure would be invisible by
morning.

Both files ship to the live site, so they're also readable in a browser at
`/state/topstories_debug.json`.

---

## Testing

```bash
python3 tests/test_topstories.py          # no dependencies, ~0.1s
python3 tests/test_topstories.py -v       # with names
```

CI runs them on any change to the pipeline or its tests — not on the ~30-minute
data-refresh commits, so a red X always means logic broke, never that a news
feed moved.

**Exercise the safety net on demand** without waiting for a real failure:

```bash
TOPSTORIES_FORCE_FAIL=english python3 scripts/build_topstories.py
```

Accepts `english`, `outlets`, `relevance`, `ordering`, or `summary`; injects
that breakage just before validation so you can watch the gate catch it and the
fallback hold. No-op unless set.

**When adding a test, verify it fails against the bug it targets.** Two tests in
the original suite passed for the wrong reason and would have caught nothing —
one had a fixture too small to reproduce the condition, the other derived its
threshold from the constant it was meant to be testing. Reintroduce the bug,
confirm the test goes red, then restore.

---

## Environment

Everything optional degrades gracefully — the pipeline always produces a valid,
display-clean result with no keys at all.

| Variable | Effect if unset |
| --- | --- |
| `AZURE_TRANSLATOR_KEY` / `_REGION` | Non-English headlines fall back to their English snippet, or are dropped. Recommended: without it the Israeli and Arabic-titled outlets lose items. |
| `ANTHROPIC_API_KEY` | Clustering uses the deterministic heuristic instead of the semantic path. |
| `GEMINI_API_KEY` | No neutral summary line; the representative headline shows instead. |
| `TOPSTORIES_MODEL` | Defaults to `claude-opus-5`. |
| `TOPSTORIES_SUMMARY_MODEL` | Defaults to `gemini-2.5-flash`. |
| `TOPSTORIES_SAMPLE` | Set to print the ranked Top 5 to the console after a run. |
| `TOPSTORIES_FORCE_FAIL` | Gate self-test (above). |

Files written each cycle, all committed by the refresh workflow:
`top_stories.json`, `state/topstories_lkg.json`, `state/topstories_debug.json`,
`state/topstories_failures.json`, `state/topstories_en_cache.json`,
`state/topstories_summaries.json`.

---

## Making changes

| To change… | Edit |
| --- | --- |
| What counts as on-topic | `SEC_WORDS` / `SEC_PHRASES` / `OFF_WORDS` / `OFF_PHRASES` in `topstories_pipeline.py` |
| How stories group | `ENTITIES_STRONG` / `ENTITIES_BROAD` / `GEO_TOKENS` |
| Which headline leads a story | `_REP_SOURCE_RANK`, `_LOADED_RE`, `pick_representative()` |
| A threshold | The constant **and** `tests/test_topstories.py::Thresholds` **and** this file |
| Adding an outlet | `SRC_CATS` in `topstories_pipeline.py` **and** `SRC_CATS` in `index.html` (kept in sync by hand) |

After any change: run the tests, then run the builder and read
`explain_topstories.py` output to see the effect on real data.

---

## Known limitations

- **Lexically disjoint paraphrases don't merge in the heuristic path.** "US and
  Saudi Arabia sign nuclear agreement" and "Riyadh and Washington conclude
  nuclear deal talks" share one content word once stopwords and geography are
  removed. No word-overlap rule can join them without also joining unrelated
  stories. The semantic Claude path handles this; the limitation is documented
  in `test_known_limit_lexically_disjoint_paraphrases_do_not_merge`.
- **Story granularity follows the running arc.** A developing story (killings,
  the political response, UN reaction) clusters as one story rather than three.
  That is what "most-covered story" measures, but it means outlet counts
  reflect an arc, not a single wire report.
- **The heuristic depends on hand-maintained vocabulary.** A genuinely new
  topic with no gazetteer entry clusters only on wording overlap. `--dropped`
  and `--why` make that visible when it happens.
