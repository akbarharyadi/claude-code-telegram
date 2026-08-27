"""A tiny request/response queue on disk, between the bot and the processes
Claude Code spawns.

The permission hook and the ask-server are short-lived child processes of the
`claude` subprocess. They cannot talk to Telegram themselves — the bot owns the
only allowed `getUpdates` poll, and only the bot knows which chat a run belongs
to. So they drop a request file here and block; the bot picks it up, asks you,
and writes the answer back.

Files are plain JSON so you can watch the handshake happen with `ls`, and
delete a stuck request by hand.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import config

REQ_SUFFIX = ".req.json"
RES_SUFFIX = ".res.json"

# Requests older than this are junk from a killed run; the bot prunes them.
STALE_AFTER_SECONDS = 3 * 3600


def approvals_dir() -> Path:
    path = config.STATE_DIR / "approvals"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_id() -> str:
    """Short, because Telegram caps callback_data at 64 bytes."""
    return uuid.uuid4().hex[:10]


def run_context() -> dict[str, str]:
    """Which run and which chat this child process belongs to.

    The bot injects these into the `claude` subprocess environment, and they are
    inherited by the hook and the ask-server it spawns.
    """
    return {
        "run_id": os.environ.get("CCTG_RUN_ID", ""),
        "chat_id": os.environ.get("CCTG_CHAT_ID", ""),
        "thread_id": os.environ.get("CCTG_THREAD_ID", ""),
        "session_key": os.environ.get("CCTG_SESSION_KEY", ""),
    }


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ── request side (hook / ask-server) ──────────────────────────────────────


def submit(request: dict) -> str:
    request_id = new_id()
    request = {"id": request_id, "created_at": time.time(), **request}
    _write_atomic(approvals_dir() / f"{request_id}{REQ_SUFFIX}", request)
    return request_id


def _take_response(request_id: str) -> dict | None:
    response = _read_json(approvals_dir() / f"{request_id}{RES_SUFFIX}")
    if response is None:
        return None
    discard(request_id)
    return response


def wait_response(request_id: str, timeout: float, poll: float = 0.35) -> dict | None:
    """Blocking wait. Used by the hook, which is a plain synchronous process."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _take_response(request_id)
        if response is not None:
            return response
        time.sleep(poll)
    discard(request_id)
    return None


async def wait_response_async(request_id: str, timeout: float, poll: float = 0.35) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _take_response(request_id)
        if response is not None:
            return response
        await asyncio.sleep(poll)
    discard(request_id)
    return None


def discard(request_id: str) -> None:
    for suffix in (REQ_SUFFIX, RES_SUFFIX):
        try:
            (approvals_dir() / f"{request_id}{suffix}").unlink()
        except OSError:
            pass


# ── response side (bot) ───────────────────────────────────────────────────


def pending() -> list[dict]:
    """Requests nobody has answered yet, oldest first."""
    out: list[dict] = []
    for path in approvals_dir().glob(f"*{REQ_SUFFIX}"):
        request = _read_json(path)
        if request and request.get("id"):
            out.append(request)
    out.sort(key=lambda item: item.get("created_at", 0))
    return out


def respond(request_id: str, response: dict) -> None:
    _write_atomic(approvals_dir() / f"{request_id}{RES_SUFFIX}", response)


def prune_stale() -> int:
    """Drop leftovers from runs that were killed mid-question."""
    cutoff = time.time() - STALE_AFTER_SECONDS
    removed = 0
    for path in approvals_dir().glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ── per-run standing rules ("allow all Bash for this run") ────────────────


def _rules_path(run_id: str) -> Path:
    safe = "".join(char for char in run_id if char.isalnum() or char in "-_")[:64]
    return approvals_dir() / f"rules-{safe}.json"


def load_rules(run_id: str) -> dict:
    rules = _read_json(_rules_path(run_id)) or {}
    return rules if isinstance(rules, dict) else {}


def allow_tool_for_run(run_id: str, tool_name: str) -> None:
    rules = load_rules(run_id)
    tools = set(rules.get("tools") or [])
    tools.add(tool_name)
    rules["tools"] = sorted(tools)
    _write_atomic(_rules_path(run_id), rules)


def tool_allowed_for_run(run_id: str, tool_name: str) -> bool:
    return tool_name in set(load_rules(run_id).get("tools") or [])


def clear_rules(run_id: str) -> None:
    try:
        _rules_path(run_id).unlink()
    except OSError:
        pass
