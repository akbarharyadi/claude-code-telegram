"""MCP server: let a Claude Code session talk to you on Telegram.

This is the other direction from `bot.py`. Register it with Claude Code and a
session can ping you when a long build finishes, send you the screenshot it just
took, or stop and ask you a question you answer from your phone.

Reading is deliberately indirect: only one process may poll Telegram's
`getUpdates`, and that is the bot. The bot appends every message it receives to
`state/inbox.jsonl`, and this server tails that file. So `telegram_read_recent`
and `telegram_ask` only work while `bot.py` is running.

Register with:
    claude mcp add telegram -s user -- uv run --directory /path/to/repo mcp_server.py
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

import httpx

import config
import store
import tg_format

try:  # mcp >= 2.0 renamed FastMCP to MCPServer; the decorator API is unchanged.
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - only on mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _Server

API_ROOT = "https://api.telegram.org"
REQUEST_TIMEOUT = 60.0

mcp = _Server("telegram")


class TelegramError(RuntimeError):
    pass


def _resolve_chat(chat_id: str | int | None) -> str:
    """Pin every call to the configured chat unless explicitly unlocked."""
    if chat_id and config.MCP_ALLOW_ANY_CHAT:
        return str(chat_id)
    if chat_id and str(chat_id) != str(config.DEFAULT_CHAT_ID):
        raise TelegramError(
            f"This server is locked to chat {config.DEFAULT_CHAT_ID}. "
            "Set MCP_ALLOW_ANY_CHAT=1 in .env to address other chats."
        )
    if not config.DEFAULT_CHAT_ID:
        raise TelegramError(
            "TELEGRAM_DEFAULT_CHAT_ID is not set in .env. Send /whoami to your bot "
            "to find your chat id."
        )
    return str(config.DEFAULT_CHAT_ID)


async def _call(method: str, *, data: dict, files: dict | None = None) -> dict:
    if not config.TELEGRAM_BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set in .env.")
    url = f"{API_ROOT}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(url, data=data, files=files)
    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramError(f"Telegram returned non-JSON ({response.status_code}).") from exc
    if not payload.get("ok"):
        raise TelegramError(payload.get("description") or f"{method} failed.")
    return payload.get("result") or {}


def _resolve_file(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise TelegramError(f"No such file: {resolved}")
    # The Bot API rejects larger uploads outright; fail with a useful message.
    if resolved.stat().st_size > 50 * 1024 * 1024:
        raise TelegramError(f"{resolved.name} is over the 50 MB Telegram upload limit.")
    return resolved


@mcp.tool()
async def telegram_send_message(text: str, chat_id: str | None = None) -> str:
    """Send a text message to Telegram. Markdown is converted to Telegram HTML,
    and long text is split across several messages automatically.

    Args:
        text: Message body. Markdown (bold, code, fenced blocks) is supported.
        chat_id: Override the target chat. Rejected unless MCP_ALLOW_ANY_CHAT=1.
    """
    target = _resolve_chat(chat_id)
    chunks = tg_format.to_html_chunks(text)
    if not chunks:
        return "Nothing to send (empty text)."
    for chunk in chunks:
        try:
            await _call(
                "sendMessage",
                data={
                    "chat_id": target,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )
        except TelegramError:
            # Formatting is a nicety; delivery is not.
            for plain in tg_format.to_plain_chunks(text):
                await _call("sendMessage", data={"chat_id": target, "text": plain})
            return f"Sent {len(tg_format.to_plain_chunks(text))} message(s) as plain text."
    return f"Sent {len(chunks)} message(s) to chat {target}."


@mcp.tool()
async def telegram_send_photo(path: str, caption: str = "", chat_id: str | None = None) -> str:
    """Send an image so it renders inline in the chat — screenshots, charts, diffs.

    Args:
        path: Absolute path to a local image file (png, jpg, webp).
        caption: Optional caption, trimmed to Telegram's 1024-character limit.
        chat_id: Override the target chat. Rejected unless MCP_ALLOW_ANY_CHAT=1.
    """
    target = _resolve_chat(chat_id)
    resolved = _resolve_file(path)
    mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    with resolved.open("rb") as handle:
        await _call(
            "sendPhoto",
            data={"chat_id": target, "caption": tg_format.truncate(caption, tg_format.MAX_CAPTION)},
            files={"photo": (resolved.name, handle, mime)},
        )
    return f"Sent {resolved.name} to chat {target}."


@mcp.tool()
async def telegram_send_document(path: str, caption: str = "", chat_id: str | None = None) -> str:
    """Send any file as a downloadable document — logs, reports, PDFs, archives.

    Args:
        path: Absolute path to a local file (up to 50 MB).
        caption: Optional caption, trimmed to Telegram's 1024-character limit.
        chat_id: Override the target chat. Rejected unless MCP_ALLOW_ANY_CHAT=1.
    """
    target = _resolve_chat(chat_id)
    resolved = _resolve_file(path)
    mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    with resolved.open("rb") as handle:
        await _call(
            "sendDocument",
            data={"chat_id": target, "caption": tg_format.truncate(caption, tg_format.MAX_CAPTION)},
            files={"document": (resolved.name, handle, mime)},
        )
    return f"Sent {resolved.name} ({resolved.stat().st_size} bytes) to chat {target}."


@mcp.tool()
async def telegram_read_recent(limit: int = 10, chat_id: str | None = None) -> str:
    """Read the most recent messages the bot received. Requires bot.py to be running.

    Args:
        limit: How many messages to return, newest last.
        chat_id: Override the source chat. Rejected unless MCP_ALLOW_ANY_CHAT=1.
    """
    target = _resolve_chat(chat_id)
    entries = store.read_inbox(limit=max(1, min(limit, 100)), chat_id=target)
    if not entries:
        return "No messages recorded yet. Is bot.py running?"
    lines = []
    for entry in entries:
        who = entry.get("username") or entry.get("user_id") or "?"
        text = entry.get("text") or ""
        attachment = entry.get("file")
        if attachment:
            text = f"{text} [file: {attachment}]".strip()
        lines.append(f"@{who}: {text}")
    return "\n".join(lines)


@mcp.tool()
async def telegram_ask(
    question: str, timeout_seconds: int = 300, chat_id: str | None = None
) -> str:
    """Ask a question on Telegram and block until the next reply arrives.

    Use this to get a decision from the user when they are away from the machine
    — an approval before a deploy, a choice between two approaches. Requires
    bot.py to be running, since that is what receives the answer.

    Args:
        question: What to ask. Keep it short and answerable on a phone.
        timeout_seconds: How long to wait before giving up (default 5 minutes).
        chat_id: Override the target chat. Rejected unless MCP_ALLOW_ANY_CHAT=1.
    """
    target = _resolve_chat(chat_id)
    # Record the watermark BEFORE sending, so a fast reply cannot be missed.
    since = store.inbox_length()
    await telegram_send_message(f"❓ {question}", chat_id=chat_id)

    entry = await store.wait_for_inbox(
        since, chat_id=target, timeout=max(5, min(timeout_seconds, 3600))
    )
    if entry is None:
        return f"No reply within {timeout_seconds}s. Proceed without it, or ask again."
    answer = entry.get("text") or ""
    attachment = entry.get("file")
    if attachment:
        answer = f"{answer}\n[attached file: {attachment}]".strip()
    return answer or "(empty reply)"


if __name__ == "__main__":
    config.ensure_dirs()
    mcp.run()
