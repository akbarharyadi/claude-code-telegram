"""Check that this machine can actually run the bridge, before you rely on it.

    uv run doctor.py            # config + Claude Code + Telegram reachability
    uv run doctor.py --no-llm   # skip the live Claude Code call

It verifies the two halves independently, so a missing Telegram token does not
hide a broken Claude Code install, and vice versa.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import httpx

import agent_runner
import bridge
import config
from agent_runner import RunSpec

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

    # Report what a run will ACTUALLY use — with approvals on, the raw
    # CLAUDE_PERMISSION_MODE / CLAUDE_ALLOWED_TOOLS values are not applied.
    mode = config.effective_permission_mode()
    report.line(OK, f"permission mode: {mode or 'default (ask)'}")
    report.line(OK, f"runs without asking: {', '.join(config.effective_allowed_tools())}")
    if config.DISALLOWED_TOOLS:
        report.line(OK, f"always denied: {', '.join(config.DISALLOWED_TOOLS)}")
    if not config.APPROVALS_ENABLED and mode == "bypassPermissions":
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
        async for event in agent_runner.stream_run(spec):
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


async def _auto_respond(run_id: str, decision: dict, seen: list[dict]) -> None:
    """Stand in for the phone: answer every request this run raises."""
    while True:
        for request in bridge.pending():
            if request.get("run_id") != run_id:
                continue
            seen.append(request)
            bridge.respond(str(request.get("id")), decision)
        await asyncio.sleep(0.2)


async def _gate_probe(decision: dict, probe: Path) -> tuple[bool, list[dict], str]:
    """Ask Claude to create a file via Bash, answer the approval with `decision`,
    and report whether the file actually appeared."""
    probe.unlink(missing_ok=True)
    run_id = f"doctor-{uuid.uuid4().hex[:8]}"
    settings_path, mcp_path = config.write_run_configs()
    seen: list[dict] = []

    spec = RunSpec(
        prompt=(
            "Use the Bash tool to run exactly this command, and nothing else:\n"
            f"  echo probe > '{probe.as_posix()}'\n"
            "Then reply with one word: done."
        ),
        cwd=str(config.ROOT),
        allowed_tools=config.effective_allowed_tools(),
        append_system_prompt=config.append_system_prompt(),
        timeout_seconds=180,
        settings_path=settings_path,
        mcp_config_path=mcp_path,
        env_extra={
            "CCTG_RUN_ID": run_id,
            "CCTG_CHAT_ID": "0",
            "CCTG_SESSION_KEY": run_id,
        },
    )

    responder = asyncio.create_task(_auto_respond(run_id, decision, seen))
    error = ""
    try:
        async for event in agent_runner.stream_run(spec):
            if event.kind == "error":
                error = event.text
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        responder.cancel()
        bridge.clear_rules(run_id)

    created = probe.exists()
    probe.unlink(missing_ok=True)
    return created, seen, error


async def check_approval_gate(report: Report) -> None:
    report.section("Approval gate")
    if not config.APPROVALS_ENABLED:
        report.line(
            WARN, "TELEGRAM_APPROVALS=0 — tools run unattended, nothing is asked on Telegram."
        )
        return

    report.line(OK, f"asks for: {', '.join(config.effective_ask_tools())}")
    probe = config.STATE_DIR / "gate-probe.txt"

    print("        probing with DENY (the command must NOT run)…")
    created, seen, error = await _gate_probe({"choice": "deny", "note": "doctor probe"}, probe)
    if error:
        report.line(FAIL, f"deny probe failed to run: {error[:300]}")
        return
    if not seen:
        report.line(FAIL, "no approval was ever requested — the hook is not firing.")
        return
    report.line(OK, f"hook fired for: {seen[0].get('tool_name')}")
    report.line(
        OK if not created else FAIL,
        "deny blocked the command" if not created else "DENY DID NOT BLOCK — the gate is open!",
    )

    print("        probing with ALLOW (the command must run)…")
    created, seen, error = await _gate_probe({"choice": "allow"}, probe)
    if error:
        report.line(FAIL, f"allow probe failed to run: {error[:300]}")
        return
    report.line(
        OK if created else FAIL,
        "allow let the command through" if created else "allow did not run the command",
    )


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
        await check_approval_gate(report)
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
