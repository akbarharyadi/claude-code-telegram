"""MCP server wired into every bot-launched run, so Claude can put a decision
back to you instead of guessing.

`ask_user` with options renders as tappable buttons in the chat; without
options it waits for you to type a reply. `notify` is fire-and-forget.

This is separate from `mcp_server.py` on purpose: that one is the general
"reach me anywhere" server you register globally, while this one only makes
sense inside a run the bot started, because the chat to ask comes from the
run's environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge  # noqa: E402
import config  # noqa: E402

try:  # mcp >= 2.0 renamed FastMCP to MCPServer; the decorator API is unchanged.
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - only on mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _Server

mcp = _Server("tg")

MAX_OPTIONS = 6


def _unavailable(context: dict[str, str]) -> bool:
    return not context.get("run_id") or not context.get("chat_id")


@mcp.tool()
async def ask_user(question: str, options: list[str] | None = None) -> str:
    """Ask the user a question on Telegram and wait for the answer.

    Call this instead of guessing whenever a choice would change what you build:
    which approach to take, which file is the right one, whether an assumption
    holds. Prefer giving `options` — they become one-tap buttons on the phone,
    which is far easier to answer than typing.

    Args:
        question: The question. One or two short sentences; it is read on a phone.
        options: Up to 6 short choices. Omit to ask for a free-text reply.
    """
    context = bridge.run_context()
    if _unavailable(context):
        return "Unavailable: this tool only works inside a run started by the Telegram bot."

    question = (question or "").strip()
    if not question:
        return "Nothing was asked (empty question)."

    cleaned = [str(option).strip() for option in (options or []) if str(option).strip()]
    if len(cleaned) > MAX_OPTIONS:
        return f"Too many options ({len(cleaned)}); Telegram buttons are limited to {MAX_OPTIONS}."

    request_id = bridge.submit(
        {"kind": "ask", **context, "question": question, "options": cleaned}
    )
    response = await bridge.wait_response_async(request_id, timeout=config.ASK_WAIT_SECONDS)

    if response is None:
        return (
            f"No answer within {int(config.ASK_WAIT_SECONDS)}s. Do not block on this — "
            "either proceed with the safest assumption and say which one you chose, "
            "or stop and explain what you need."
        )
    answer = str(response.get("answer") or response.get("choice") or "").strip()
    return answer or "(the user replied with nothing)"


@mcp.tool()
async def notify(text: str) -> str:
    """Post a short status note into the Telegram chat without waiting for a reply.

    Useful mid-run: "migration applied, running the test suite now". Keep it to
    one line — the progress message already shows tool-by-tool activity.

    Args:
        text: The note to post.
    """
    context = bridge.run_context()
    if _unavailable(context):
        return "Unavailable: this tool only works inside a run started by the Telegram bot."

    text = (text or "").strip()
    if not text:
        return "Nothing to post (empty text)."

    bridge.submit({"kind": "notify", **context, "text": text})
    return "Posted."


if __name__ == "__main__":
    config.ensure_dirs()
    mcp.run()
