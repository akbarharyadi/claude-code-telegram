"""Find your Telegram ids before the bot is allowed to start.

    uv run whoami.py

The bot refuses to run with an empty ALLOWED_USER_IDS, but you need your numeric
id to fill it in — and Telegram only reveals it once you message the bot. This
breaks that loop: it reads pending updates directly, so no bot has to be running.

It offers to write the ids into .env for you. Nothing is written without a yes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

import config

POLL_SECONDS = 25
OVERALL_DEADLINE = 180


async def call(method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=POLL_SECONDS + 15) as client:
        payload = (await client.get(url, params=params or {})).json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description") or f"{method} failed")
    return payload


def update_env(path: Path, values: dict[str, str]) -> list[str]:
    """Fill in the named keys in place, leaving comments and order untouched."""
    lines = path.read_text(encoding="utf-8").splitlines()
    written: list[str] = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values:
            lines[index] = f"{key}={values[key]}"
            written.append(key)

    for key, value in values.items():
        if key not in written:
            lines.append(f"{key}={value}")
            written.append(key)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


async def main() -> int:
    config.enable_utf8_console()

    if not config.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is empty in .env — create a bot with @BotFather first.")
        return 1

    try:
        me = (await call("getMe"))["result"]
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"Could not reach Telegram: {exc}")
        return 1

    username = me.get("username") or "?"
    print(f"Bot is @{username}.")
    print(f"Open Telegram, find @{username}, and send it any message — /start will do.")
    print("Waiting…\n")

    offset = 0
    deadline = asyncio.get_event_loop().time() + OVERALL_DEADLINE
    message = None

    while message is None and asyncio.get_event_loop().time() < deadline:
        try:
            result = (await call("getUpdates", {"timeout": POLL_SECONDS, "offset": offset}))[
                "result"
            ]
        except (httpx.HTTPError, RuntimeError) as exc:
            print(f"Poll failed: {exc}")
            return 1

        for update in result:
            offset = max(offset, update.get("update_id", 0) + 1)
            candidate = update.get("message") or update.get("channel_post")
            if candidate and candidate.get("from"):
                message = candidate
                break

    if message is None:
        print(f"No message arrived within {OVERALL_DEADLINE}s. Run this again when ready.")
        return 1

    sender = message["from"]
    chat = message.get("chat") or {}
    user_id = sender.get("id")
    chat_id = chat.get("id")
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or "?"

    print(f"Message from {name} (@{sender.get('username') or '—'})")
    print(f"  user id : {user_id}")
    print(f"  chat id : {chat_id}  ({chat.get('type')})")
    print()

    values = {"ALLOWED_USER_IDS": str(user_id), "TELEGRAM_DEFAULT_CHAT_ID": str(chat_id)}
    env_path = config.ROOT / ".env"

    if not env_path.exists():
        print("No .env found. Copy .env.example to .env, then add:")
        for key, value in values.items():
            print(f"  {key}={value}")
        return 1

    if not sys.stdin.isatty():
        print("Add these to .env:")
        for key, value in values.items():
            print(f"  {key}={value}")
        return 0

    answer = input(f"Write these into {env_path}? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        print("Left .env untouched. Add these yourself:")
        for key, value in values.items():
            print(f"  {key}={value}")
        return 0

    written = update_env(env_path, values)
    print(f"Updated {', '.join(written)} in .env.")
    print("\nNow run:  uv run bot.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
