#!/usr/bin/env python3
"""Shared configuration for the MENA posting agent.

API key: set ANTHROPIC_API_KEY in your environment, or put it in a `.env`
file at the repo root or inside agent/ (both are gitignored):

    ANTHROPIC_API_KEY=sk-ant-...

Model: the agent was originally specced for claude-sonnet-4-20250514, but
that model is deprecated and retires on 2026-06-15. We default to its direct
replacement, claude-sonnet-4-6. Override with the CLAUDE_MODEL env var or
the --model flag on any script.
"""
import os
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
DATA_DIR = AGENT_DIR / "data"

DEFAULT_MODEL = "claude-sonnet-4-6"

MY_MESSAGES_PATH = DATA_DIR / "my_messages.json"
STYLE_PROFILE_PATH = DATA_DIR / "style_profile.json"

# Stage 2 — content pipeline.
# headlines.json is produced at the repo root by scripts/fetch_headlines.py
# (refreshed every 30 min by GitHub Actions); the briefing is our ranked,
# deduplicated digest derived from it plus a few analyst feeds.
HEADLINES_PATH = REPO_ROOT / "headlines.json"
BRIEFING_PATH = DATA_DIR / "briefing.json"


def ensure_utf8_console() -> None:
    """Avoid UnicodeEncodeError for Hebrew/Arabic/emoji on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None) or ""
        if enc.lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def load_dotenv() -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    for candidate in (REPO_ROOT / ".env", AGENT_DIR / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def get_model(override: str | None = None) -> str:
    return override or os.environ.get("CLAUDE_MODEL") or DEFAULT_MODEL


def require_api_key() -> str:
    load_dotenv()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        sys.exit(
            "ERROR: ANTHROPIC_API_KEY is not set.\n"
            "  PowerShell (this session):  $env:ANTHROPIC_API_KEY = \"sk-ant-...\"\n"
            "  PowerShell (persistent):    setx ANTHROPIC_API_KEY \"sk-ant-...\"  (then open a new terminal)\n"
            "  Or create a .env file next to this script with: ANTHROPIC_API_KEY=sk-ant-..."
        )
    return key
