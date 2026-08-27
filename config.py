"""Shared configuration for the Telegram <-> Claude Code bridge.

Both `bot.py` (the Telegram side) and `mcp_server.py` (the Claude Code side)
read from the same `.env`, so there is exactly one place to change a token,
a chat lock, or the tool allowlist.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv


def enable_utf8_console() -> None:
    """Windows consoles still default to cp1252, which raises on the first
    non-ASCII character — a box-drawing glyph, or anyone typing in Japanese."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _csv(value: str | None) -> list[str]:
    """Split on commas only — a tool spec like `Bash(git push:*)` has spaces in it."""
    return [part.strip() for part in _clean(value).split(",") if part.strip()]


def _ws(value: str | None) -> list[str]:
    """Split on commas OR whitespace — for ids and directory lists."""
    return [part.strip() for part in _clean(value).replace(",", " ").split() if part.strip()]


def _ints(value: str | None) -> set[int]:
    out: set[int] = set()
    for part in _ws(value):
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def _bool(value: str | None, default: bool = False) -> bool:
    raw = _clean(value).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _float(value: str | None, default: float) -> float:
    try:
        return float(_clean(value))
    except ValueError:
        return default


def _int(value: str | None, default: int) -> int:
    try:
        return int(_clean(value))
    except ValueError:
        return default


# ── Telegram ──────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
ALLOWED_USER_IDS = _ints(os.getenv("ALLOWED_USER_IDS"))
ALLOWED_CHAT_IDS = _ints(os.getenv("ALLOWED_CHAT_IDS"))
DEFAULT_CHAT_ID = _clean(os.getenv("TELEGRAM_DEFAULT_CHAT_ID"))
MCP_ALLOW_ANY_CHAT = _bool(os.getenv("MCP_ALLOW_ANY_CHAT"))

# ── Claude Code ───────────────────────────────────────────────────────────
CLAUDE_BIN = _clean(os.getenv("CLAUDE_BIN")) or shutil.which("claude") or "claude"
CLAUDE_WORKDIR = _clean(os.getenv("CLAUDE_WORKDIR"))
CLAUDE_ADD_DIRS = _ws(os.getenv("CLAUDE_ADD_DIRS"))
CLAUDE_EXTRA_WORKDIRS = _ws(os.getenv("CLAUDE_EXTRA_WORKDIRS"))
CLAUDE_MODEL = _clean(os.getenv("CLAUDE_MODEL"))
CLAUDE_EFFORT = _clean(os.getenv("CLAUDE_EFFORT"))
PERMISSION_MODE = _clean(os.getenv("CLAUDE_PERMISSION_MODE")) or "acceptEdits"
ALLOWED_TOOLS = _csv(os.getenv("CLAUDE_ALLOWED_TOOLS"))
DISALLOWED_TOOLS = _csv(os.getenv("CLAUDE_DISALLOWED_TOOLS"))
MAX_BUDGET_USD = _float(os.getenv("CLAUDE_MAX_BUDGET_USD"), 0.0)
RUN_TIMEOUT_SECONDS = _int(os.getenv("CLAUDE_RUN_TIMEOUT_SECONDS"), 1800)

CONTEXT_NOTE = _clean(os.getenv("CLAUDE_CONTEXT_NOTE"))
_TELEGRAM_ETIQUETTE = (
    "You are replying through a Telegram bot, read on a phone. "
    "Lead with the answer in the first line. Keep it short and scannable: "
    "short paragraphs, small code blocks, no whole-file dumps, no preamble. "
    "When you change files, end with a one-line-per-file list of what changed and why. "
    "Never print secrets, tokens, or the contents of .env files into the chat."
)
APPEND_SYSTEM_PROMPT = "\n\n".join(part for part in (_TELEGRAM_ETIQUETTE, CONTEXT_NOTE) if part)

# ── Local state ───────────────────────────────────────────────────────────
STATE_DIR = Path(_clean(os.getenv("STATE_DIR")) or (ROOT / "state"))
DOWNLOAD_DIR = STATE_DIR / "downloads"
SESSION_FILE = STATE_DIR / "sessions.json"
INBOX_FILE = STATE_DIR / "inbox.jsonl"
LOG_DIR = STATE_DIR / "logs"


def ensure_dirs() -> None:
    for path in (STATE_DIR, DOWNLOAD_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def workdir_choices() -> dict[str, str]:
    """Name -> absolute path, for the /cd command and for validating stored cwds."""
    choices: dict[str, str] = {}
    for raw in [CLAUDE_WORKDIR, *CLAUDE_EXTRA_WORKDIRS]:
        path = Path(raw).expanduser()
        if path.is_dir():
            choices[path.name.lower()] = str(path)
    return choices


def missing_settings() -> list[str]:
    problems: list[str] = []
    if not TELEGRAM_BOT_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN is empty — get one from @BotFather.")
    if not ALLOWED_USER_IDS:
        problems.append(
            "ALLOWED_USER_IDS is empty — the bot would accept commands from anyone. "
            "Send /start to the bot to learn your numeric user id, then fill it in."
        )
    if not CLAUDE_WORKDIR:
        problems.append(
            "CLAUDE_WORKDIR is empty — point it at the repository Claude should work in."
        )
    elif not Path(CLAUDE_WORKDIR).is_dir():
        problems.append(f"CLAUDE_WORKDIR does not exist: {CLAUDE_WORKDIR}")
    return problems
