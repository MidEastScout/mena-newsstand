---
name: verify
description: Verify changes to the MENA newsstand site (static index.html + Python data pipeline) by running the builders and driving the page in headless Chromium.
---

# Verifying changes to this repo

This is a static GitHub Pages site (`index.html`) fed by JSON files that a
GitHub Actions workflow regenerates every ~30 min via `scripts/*.py`.

## Surfaces

1. **Pipeline scripts** — run them and inspect the JSON they write.
2. **The page** — serve the repo root over HTTP and drive it in a browser.

## Recipe that works

Work in a scratch copy so repo data files aren't mutated:

```bash
cp -r index.html headlines.json briefing.json pulse.json trends.json \
      favicon.svg scripts state frontpages "$SCRATCH/site/"
cd "$SCRATCH/site"
GEMINI_API_KEY= python3 scripts/build_pulse.py    # no-key path must stay non-fatal
python3 scripts/build_trends.py
python3 -m http.server 8901 &
```

- `fetch_headlines.py` / `build_briefing.py` need `GEMINI_API_KEY` for their AI
  fields (snippet/title_he/snippet_he, html_by_lang.he). Without a key, inject
  those fields into the JSON by hand to drive the frontend rendering paths.
- The pulse Hebrew-term dictionary can be exercised with no key by seeding
  `state/pulse_terms_he.json` — `build_pulse.py` then uses cache only.

Browser: `npm install playwright-core`, launch with
`executablePath: '/opt/pw-browsers/chromium'` (it's a symlink to the binary —
do NOT append `/chrome-linux/chrome`). Capture `pageerror`/console; Google
Fonts requests fail with ERR_CONNECTION_RESET in the sandbox — ignore those.

## Flows worth driving

- EN ↔ HE toggle (`#lang-toggle`): flips `<html lang/dir>`, all chrome via
  `data-i18n`, re-renders every tab from cached data; choice persists in
  localStorage (`mes-lang`) across reloads.
- Each tab after toggle: briefing carousel (RTL arrow direction), headlines
  (title_he/snippet_he fallback, region/country names), pulse (term_he,
  detail panel), trends (label_he, bidi of "+X.Xpp" deltas).
- Headline filter: typing a Hebrew keyword must match `title_he`/`snippet_he`;
  an English keyword must still match in HE mode.

## Gotchas

- Only `.navbtn[data-sec]` buttons switch tabs — the language toggle shares
  the .navbtn class and must not enter that handler.
- `applyLang()` runs mid-script: any state it reads must be declared above
  the call site (TDZ), see the `let briefData, pulseData…` block at the top.
