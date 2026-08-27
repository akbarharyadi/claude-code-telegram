"""Check that this machine can actually run the bridge, before you rely on it.

    uv run doctor.py            # config + Claude Code + Telegram reachability
    uv run doctor.py --no-llm   # skip the live Claude Code call

It verifies the two halves independently, so a missing Telegram token does not
hide a broken Claude Code install, and vice versa.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

import claude_runner
import config
from claude_runner import RunSpec

OK = "  ok   "
WARN = " warn  "
FAIL = " fail  "


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0

    def line(self, status: str, message: str) -> None:
        print(f"[{status}] {message}")
        if status is FAIL:
            self.failed += 1
        elif status is WARN:
            self.warned += 1

    def section(self, title: str) -> None:
        print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def check_config(report: Report) -> None:
    report.section("Configuration")
    env_file = config.ROOT / ".env"
    if env_file.exists():
        report.line(OK, f".env found at {env_file}")
    else:
        report.line(FAIL, f"No .env at {env_file} — copy .env.example and fill it in.")

    for problem in config.missing_settings():
        report.line(FAIL, problem)

    if config.CLAUDE_WORKDIR:
        report.line(OK, f"workdir: {config.CLAUDE_WORKDIR}")
    for extra in config.CLAUDE_ADD_DIRS:
        status = OK if Path(extra).is_dir() else FAIL
        report.line(status, f"add-dir: {extra}")

    choices = config.workdir_choices()
    report.line(OK, f"/cd targets: {', '.join(sorted(choices)) or '(none)'}")
    report.line(OK, f"permission mode: {config.PERMISSION_MODE}")
    report.line(OK, f"allowed tools: {', '.join(config.ALLOWED_TOOLS) or '(default)'}")
    if config.DISALLOWED_TOOLS:
        report.line(OK, f"denied tools: {', '.join(config.DISALLOWED_TOOLS)}")
    elif config.PERMISSION_MODE == "bypassPermissions":
        report.line(
            WARN,
            "bypassPermissions with no denylist — anyone on ALLOWED_USER_IDS can run anything.",
        )


def check_claude_binary(report: Report) -> bool:
    report.section("Claude Code")
    binary = Path(config.CLAUDE_BIN)
    if binary.is_file() or config.CLAUDE_BIN == "claude":
        report.line(OK, f"binary: {config.CLAUDE_BIN}")
        return True
    report.line(FAIL, f"binary not found: {config.CLAUDE_BIN} — set CLAUDE_BIN in .env")
    return False


async def check_claude_run(report: Report) -> None:
    cwd = config.CLAUDE_WORKDIR or str(config.ROOT)
    if not Path(cwd).is_dir():
        report.line(FAIL, f"cannot run: {cwd} is not a directory")
        return

    spec = RunSpec(
        prompt="Reply with exactly the word: pong. Nothing else.",
        cwd=cwd,
        permission_mode="plan",  # read-only: the check must not touch the repo
        allowed_tools=("Read",),
        timeout_seconds=180,
    )
    print("        running a one-word prompt (this costs a fraction of a cent)…")

    session_id = ""
    result = ""
    error = ""
    try:
        async for event in claude_runner.stream_run(spec):
            if event.session_id:
                session_id = event.session_id
            if event.kind == "result":
                result = event.text.strip()
            elif event.kind == "error":
                error = event.text
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    if error:
        report.line(FAIL, f"run failed: {error[:400]}")
        return
    if not result:
        report.line(
            FAIL, "run produced no result — is Claude Code logged in? Try `claude` manually."
        )
        return

    report.line(OK, f"run returned: {result[:80]!r}")
    report.line(OK if session_id else FAIL, f"session id captured: {session_id or '(none)'}")


async def check_telegram(report: Report) -> None:
    report.section("Telegram")
    if not config.TELEGRAM_BOT_TOKEN:
        report.line(FAIL, "TELEGRAM_BOT_TOKEN is empty — create a bot with @BotFather.")
        return

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            payload = (await client.get(url)).json()
    except httpx.HTTPError as exc:
        report.line(FAIL, f"cannot reach api.telegram.org: {exc}")
        return

    if not payload.get("ok"):
        report.line(FAIL, f"token rejected: {payload.get('description')}")
        return

    bot = payload.get("result") or {}
    report.line(OK, f"bot: @{bot.get('username')} (id {bot.get('id')})")
    report.line(
        OK if config.ALLOWED_USER_IDS else FAIL,
        f"authorized users: {sorted(config.ALLOWED_USER_IDS) or '(none — bot will not start)'}",
    )
    report.line(
        OK if config.DEFAULT_CHAT_ID else WARN,
        f"MCP default chat: {config.DEFAULT_CHAT_ID or '(unset — MCP send tools will refuse)'}",
    )


async def main() -> int:
    config.enable_utf8_console()
    config.ensure_dirs()
    report = Report()
    print("claude-code-telegram — setup check")

    check_config(report)
    if check_claude_binary(report) and "--no-llm" not in sys.argv:
        await check_claude_run(report)
    await check_telegram(report)

    print()
    if report.failed:
        print(f"{report.failed} problem(s) to fix. See README.md → Troubleshooting.")
        return 1
    if report.warned:
        print(f"Ready, with {report.warned} warning(s). Start with: uv run bot.py")
        return 0
    print("All good. Start with: uv run bot.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
