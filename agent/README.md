# MENA Posting Agent

A personal agent that learns your writing style from your WhatsApp history, monitors MENA news (including this repo's `headlines.json`), and drafts posts in your voice for review before anything is sent.

**Nothing is ever sent automatically.** Every stage ends with you approving, editing, or rejecting.

## Roadmap

| Stage | What | Status |
|---|---|---|
| 1 | Style learning: parse WhatsApp export → `style_profile.json` | **Ready** |
| 2 | Content pipeline: rank & dedupe headlines.json + analyst feeds → `briefing.json` | **Ready** |
| 3 | Draft engine: 2-3 daily drafts in your style, with reasoning + confidence | Planned |
| 4 | Review dashboard: approve / edit / reject / schedule, feedback loop | Planned |
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
