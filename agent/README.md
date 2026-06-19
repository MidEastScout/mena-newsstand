# MENA Posting Agent

A personal agent that learns your writing style from your WhatsApp history, monitors MENA news (including this repo's `headlines.json`), and drafts posts in your voice for review before anything is sent.

**Nothing is ever sent automatically.** Every stage ends with you approving, editing, or rejecting.

## Roadmap

| Stage | What | Status |
|---|---|---|
| 1 | Style learning: parse WhatsApp export → `style_profile.json` | **Ready** |
| 2 | Content pipeline: rank & dedupe headlines.json + analyst feeds → `briefing.json` | **Ready** |
| 3 | Relay writer: short news-relay drafts in your voice → `drafts.json` | **Ready** |
| 4 | Review dashboard: approve / edit / reject, persists + logs feedback | **Ready** |
| 5 | Publishing: Telegram Bot API + WhatsApp (human-in-the-loop only) | Planned |

## ⚠️ Privacy first

This repo is **public** (it serves your GitHub Pages newsstand). Your WhatsApp export contains private messages from ~240 people.

- `agent/data/` is **gitignored** — exports, parsed messages and your style profile never leave your machine.
- Keep the export file outside the repo, or inside `agent/data/` — never anywhere else in the repo.
- Double-check with `git status` before committing: no chat data should ever appear there.

## Setup (Windows / PowerShell)

```powershell
cd path\to\mena-newsstand

# one-time: virtual env + dependencies
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r agent\requirements.txt

# API key (get one at https://platform.claude.com)
$env:ANTHROPIC_API_KEY = "sk-ant-..."          # this session only
# or persist it (takes effect in NEW terminals):
setx ANTHROPIC_API_KEY "sk-ant-..."
```

If PowerShell blocks the activation script: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## Stage 1 — Build your style profile

**1. Export your chat** (on your phone): WhatsApp → open the group → ⋮ → More → Export chat → **Without media**. Send the `.txt` to your PC (on iPhone it arrives as a `.zip` — extract `_chat.txt`). Put it at `agent\data\export.txt`.

**2. Find your exact sender name** as WhatsApp wrote it:

```powershell
py agent\parse_whatsapp.py agent\data\export.txt --list-authors
```

**3. Extract your messages:**

```powershell
py agent\parse_whatsapp.py agent\data\export.txt --author "Your Name"
```

Handles Android & iOS formats, Hebrew/RTL text, multi-line messages; drops media placeholders, deleted messages and system lines. If the printed date range looks wrong (months/days swapped), re-run with `--date-order dmy` or `--date-order mdy`.

**4. Preview, then analyze:**

```powershell
py agent\analyze_style.py --dry-run    # free: local stats + what would be sent
py agent\analyze_style.py              # one API call -> agent\data\style_profile.json
```

Typical cost on the default model: **~$0.10–0.25** per run (≈30K input tokens). The profile contains quantitative stats, a qualitative analysis (tone, structure, openings/closings, topics, Hebrew/English mixing, emoji, citation habits), verbatim representative examples, and a distilled `style_instructions` block that Stage 3 will inject into every draft request.

**Try it first on fake data:** `py agent\parse_whatsapp.py agent\sample_export.txt --author "Demo Author"` then `py agent\analyze_style.py --dry-run`.

## Stage 2 — Build today's ranked briefing

No API call, no cost. This reads the newsstand's own `headlines.json` (16 MENA
outlets, refreshed every 30 min by GitHub Actions), pulls a few analyst feeds
(Al-Monitor, INSS, Crisis Group, Reuters MENA), then deduplicates and ranks
every story by **significance**: how many outlets carry it, how many regions it
spans, how fresh it is, and how closely it matches your beat (Iran, Lebanon,
Hezbollah, Gulf–Israel normalization, strikes, Vision 2030…).

```powershell
py agent\build_briefing.py                    # full briefing -> agent\data\briefing.json
py agent\build_briefing.py --no-extra-feeds   # newsstand only, zero network calls
py agent\build_briefing.py --max-age-days 1 --top 15
```

It prints the top stories and writes `agent\data\briefing.json` — the ranked
list Stage 3 will draft from. Tune ranking with `--similarity` (how aggressively
near-duplicate headlines merge) and `--max-age-days` (recency window).

**Israeli domestic politics is excluded by default** (Knesset, coalition,
judicial overhaul, domestic elections) — but stories with a regional/security
angle are kept ("Netanyahu orders Lebanon strike" stays). Pass
`--include-israeli-politics` to keep them all.

## Stage 3 — Draft relay posts in your voice

Takes the top stories from `briefing.json` and your `style_profile.json` and
writes short **news-relay** updates in your voice — restating what a named
outlet reported, in Hebrew, with your attribution closing. It is a relay, not
an analyst: it only has the headline, so it relays the headline and **never
invents facts, quotes, numbers, or analysis**.

```powershell
py agent\draft_posts.py --dry-run          # free: shows which stories it would relay
py agent\draft_posts.py                     # draft top 3 -> agent\data\drafts.json
py agent\draft_posts.py --count 5 --lang he # more drafts; --lang he|en|ar|auto
```

Typical cost: **a few cents** per run. Each draft is saved with
`status: "pending"`, a `confidence` score, the exact `relays_facts` it conveys
(so you can verify nothing was added), and `review_flags` (e.g. "single
source"). **Nothing is ever sent** — Stage 4 will be the dashboard where you
approve, edit, or reject each draft.

> **Reading Hebrew drafts:** the Windows console renders Hebrew left-to-right
> and garbles it. So `draft_posts.py` also writes `agent\data\drafts.html` —
> **double-click it** to read the drafts in correct right-to-left layout, with
> a copy button per draft. (Re-render an existing `drafts.json` anytime with
> `py agent\render_drafts.py`.)

## Stage 4 — Review dashboard (approve / edit / reject)

The `drafts.html` viewer is read-only. Stage 4 is the interactive version: a
tiny **local web app** (Python standard library only — no installs, no
internet) where your decisions are saved back to `drafts.json`.

```powershell
py agent\review_server.py        # opens http://127.0.0.1:8765 in your browser
```

In the page you can, per draft: **edit** the Hebrew text in place, **✓ approve**,
**✗ reject**, **copy**, or **reset** to pending. Edits are saved when you
approve. A top bar tallies pending / approved / rejected and has a **"copy all
approved"** button to grab everything you greenlit at once. Press **Ctrl+C** in
PowerShell to stop the server.

- It binds to **127.0.0.1 only** — not exposed to your network.
- **Nothing is published.** Approving just marks a draft ready for manual
  posting (Stage 5 will add an always-manual publish step).
- Every edit is logged to `agent\data\feedback.json` (original vs. your final
  text) — the raw material for teaching future drafts to match your voice.

## Model

Default: **`claude-sonnet-4-6`**, configurable via `$env:CLAUDE_MODEL` or `--model`.

> The originally requested `claude-sonnet-4-20250514` is deprecated and **retires 2026-06-15**, after which it returns errors — `claude-sonnet-4-6` is Anthropic's designated replacement and supports structured outputs (guaranteed-valid JSON profiles). The scripts fall back gracefully on models without structured-output support.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no sender matches` | Run `--list-authors`; copy your name exactly (it may be your phone number if exported from someone else's phone) |
| Garbled Hebrew in terminal | Use Windows Terminal / PowerShell 7; the scripts already force UTF-8 output |
| Dates look swapped | `--date-order dmy` (Israel/EU) or `mdy` (US) |
| `ANTHROPIC_API_KEY is not set` after `setx` | Open a **new** terminal, or use `$env:ANTHROPIC_API_KEY=...` in the current one |
| Profile feels off | Re-run with `--max-samples 150` or a bigger `--max-chars`; quality scales with how many real posts it sees |
