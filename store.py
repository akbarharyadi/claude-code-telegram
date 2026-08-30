"""Small JSON-file state shared by the bot and the MCP server.

Two things live here:

* **sessions** — which Claude Code session id and working directory belong to a
  chat, so a restart of the bot does not lose your conversation.
* **inbox** — an append-only log of incoming Telegram messages. Only one process
  may poll `getUpdates`, so the MCP server cannot read Telegram itself; the bot
  writes here and the MCP server tails the file. That is what makes
  `telegram_ask` (ask a question, wait for the reply) possible.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import config


@dataclass
class Session:
    session_id: str = ""
    cwd: str = ""
    add_dirs: list[str] = field(default_factory=list)
    model: str = ""
    started: bool = False  # False until one run has completed, so we know to --resume
    turns: int = 0
    cost_usd: float = 0.0
    updated_at: float = 0.0
    backend: str = ""  # which agent runner the session id belongs to


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def load_sessions() -> dict[str, Session]:
    raw = _read_json(config.SESSION_FILE, {})
    out: dict[str, Session] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if isinstance(value, dict):
            known = {f: value.get(f) for f in Session.__dataclass_fields__ if f in value}
            try:
                out[key] = Session(**known)
            except TypeError:
                continue
    return out


def save_sessions(sessions: dict[str, Session]) -> None:
    config.ensure_dirs()
    payload = {key: asdict(value) for key, value in sessions.items()}
    tmp = config.SESSION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(config.SESSION_FILE)


def append_inbox(entry: dict) -> None:
    config.ensure_dirs()
    entry = {"ts": time.time(), **entry}
    with config.INBOX_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_inbox(limit: int = 20, chat_id: str | int | None = None) -> list[dict]:
    if not config.INBOX_FILE.exists():
        return []
    try:
        lines = config.INBOX_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    entries: list[dict] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if chat_id is not None and str(entry.get("chat_id")) != str(chat_id):
            continue
        entries.append(entry)
    return entries[-limit:] if limit > 0 else entries


def inbox_length() -> int:
    if not config.INBOX_FILE.exists():
        return 0
    try:
        return sum(1 for _ in config.INBOX_FILE.open("r", encoding="utf-8"))
    except OSError:
        return 0


async def wait_for_inbox(
    since: int, *, chat_id: str | int | None, timeout: float, poll: float = 1.0
) -> dict | None:
    """Block until a message lands after line `since`, or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entries = read_inbox(limit=0)
        for entry in entries[since:]:
            if chat_id is not None and str(entry.get("chat_id")) != str(chat_id):
                continue
            return entry
        await asyncio.sleep(poll)
    return None
