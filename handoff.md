# Handoff: MENA Newsstand — PressReader CDN Integration

## Goal

Add PressReader CDN as a third front-page image source (alongside Freedom Forum and Kiosko)
to expand MENA newspaper cover coverage. The site currently shows only Haaretz and The National
from the Middle East; the goal is to add Gulf News, Khaleej Times, Arab News, The Peninsula,
Gulf Times, Oman Observer, Jordan Times, Jerusalem Post, and more.

---

## Active Branch

`claude/serene-dijkstra-ZsCTK` — **not yet merged to `main`**

---

## What Has Changed (commit `ae45268`)

Three files changed vs `main`:

### `scripts/fetch_frontpages.py`
- Added `("pr", CID)` source type to `candidate_url()`:
  `https://i.prcdn.co/img?cid={CID}&date={YYYYMMDD}&page=1&width=600`
- Added PressReader referer (`https://www.pressreader.com/`) to `referer_for()`
- Added 8 MENA papers to the `PAPERS` list with best-guess CIDs:
  Gulf News, Khaleej Times, Arab News, The Peninsula, Gulf Times,
  Oman Observer, Jordan Times, Jerusalem Post

### `scripts/probe_frontpages.py`
- Added `("pr", CID)` probing + PressReader referer to all helpers
- Added 16 MENA candidate papers (UAE, Saudi, Qatar, Oman, Kuwait, Jordan, Lebanon, Israel)
  each with 3–4 CID guesses to try
- Added `--discover` mode: scans 3000 PressReader CIDs concurrently (30 workers)
  across ranges 1000–2000 / 5000–6000 / 7000–8000, reports every CID returning
  a valid image (≥12KB, Content-Type: image/*); saves hits to `state/probe_results.json`
  under `pressreader_discovery.hits`

### `.github/workflows/fetch-headlines.yml`
- Added `run_probe` boolean input to `workflow_dispatch`
- Added "Probe front-page sources" step (runs `probe_frontpages.py --discover`)
  gated on `github.event.inputs.run_probe == 'true'`
- Added `git add state/probe_results.json` to the commit step so results persist

---

## Current State / What's Blocked

**The CIDs in `fetch_frontpages.py` and `probe_frontpages.py` are educated guesses.**
PressReader doesn't publish a CID catalog publicly, and the `i.prcdn.co` CDN is
inaccessible from this sandbox (returns 403 from restricted egress). The correct
CIDs can only be discovered by running the probe from a GitHub Actions runner
(which has open internet access).

**The probe step won't fire until the branch is merged to `main`**, because the
workflow checks out `ref: main` regardless of which branch triggered it.

---

## How to Move Forward

### Step 1 — Merge the branch to main

Merge `claude/serene-dijkstra-ZsCTK` → `main` (or cherry-pick the single commit `ae45268`).

### Step 2 — Trigger the probe

In GitHub: **Actions → MENA Newsstand → Run workflow**  
Check the box **"Run front-page source probe + PressReader CID discovery"**, run on `main`.

The job runs for ~5 minutes (3000 concurrent CID checks + the regular candidate list).
It commits `state/probe_results.json` to `main` with a `pressreader_discovery` section:

```json
{
  "pressreader_discovery": {
    "ranges_scanned": "1000-2000, 5000-6000, 7000-8000",
    "date": "2026-06-22",
    "hits": [1234, 5678, ...]
  }
}
```

Also check the MENA candidates in the regular `results` section — any that
landed on a correct guess will show `"ok": true` with the winning CID.

### Step 3 — Identify the papers

The hits list will contain numeric CIDs for ALL PressReader papers in those ranges —
not just MENA ones. To identify which CID belongs to which paper, open the
thumbnail URL in a browser:
`https://i.prcdn.co/img?cid=<HIT_CID>&date=<YYYYMMDD>&page=1&width=600`

### Step 4 — Update the PAPERS list

Once CIDs are confirmed, replace the guessed multi-CID arrays in `fetch_frontpages.py`
with the single confirmed CID, e.g.:

```python
# Before (guesses):
{"id": "gulf_news", ..., "src": [("pr", "5285"), ("pr", "4669"), ("pr", "7568")]}

# After (confirmed):
{"id": "gulf_news", ..., "src": [("pr", "1543")]}
```

Also remove papers whose CIDs weren't found (not on PressReader or blocked).

### Step 5 — Remove unconfirmed guesses from fetch_frontpages.py

Any paper still showing `"ok": false` after the real front-page fetch runs
should be removed from `PAPERS` so it doesn't appear as a broken entry on the site.

---

## Key URL Formats

| Source | URL Pattern |
|--------|------------|
| Freedom Forum | `https://cdn.freedomforum.org/dfp/jpg{DD}/lg/{CODE}.jpg` |
| Kiosko | `https://img.kiosko.net/{YYYY/MM/DD}/{geo}/{slug}.750.jpg` |
| PressReader | `https://i.prcdn.co/img?cid={CID}&date={YYYYMMDD}&page=1&width=600` |

## Key Files

| File | Role |
|------|------|
| `scripts/fetch_frontpages.py` | Production download — runs daily via Actions |
| `scripts/probe_frontpages.py` | One-time tester — discovers working sources |
| `state/probe_results.json` | Probe output — committed to repo by Actions |
| `.github/workflows/fetch-headlines.yml` | Workflow — `run_probe` input added |
| `frontpages/manifest.json` | Live index of available covers |

---

## Background Context

- Freedom Forum: covers US papers keyed by day-of-month. Code like `NY_NYT`, `ISR_HA`, `UAE_TN`.
- Kiosko: covers European/some MENA papers keyed by full date + geo + slug.
- PressReader: covers ~7000 global papers; MENA catalog is deep (Gulf papers, Arabic dailies).
  CDN at `i.prcdn.co` serves cover thumbnails without authentication for public issues.
- The workflow checks out `main` on every run, so all production changes must land on `main`.
- `state/daily.json` is the gatekeeper: tracks which timed tasks (briefing, frontpages, email)
  have already run in each daily slot so they don't repeat.
