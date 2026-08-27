"""Spawn the Claude Code CLI headlessly and stream its events back as they happen.

The bot needs three things the plain `--output-format json` mode cannot give it:
live progress (so a five-minute run is not a silent five minutes on your phone),
the session id (so the next Telegram message continues the same conversation),
and a clean cancel path. `--output-format stream-json` provides all three.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import config

# stream-json emits one JSON object per line, and a single tool result can be
# megabytes. The default 64 KiB StreamReader limit would raise mid-run.
_STDOUT_LIMIT = 32 * 1024 * 1024


@dataclass(slots=True)
class Event:
    """One thing that happened during a run."""

    kind: str  # "init" | "text" | "tool" | "result" | "error"
    text: str = ""
    tool: str = ""
    detail: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    is_error: bool = False


@dataclass(slots=True)
class RunSpec:
    """Everything needed to launch one Claude Code run."""

    prompt: str
    cwd: str
    session_id: str = ""
    resume: bool = False
    add_dirs: Sequence[str] = field(default_factory=tuple)
    model: str = ""
    effort: str = ""
    permission_mode: str = ""
    allowed_tools: Sequence[str] = field(default_factory=tuple)
    disallowed_tools: Sequence[str] = field(default_factory=tuple)
    append_system_prompt: str = ""
    max_budget_usd: float = 0.0
    timeout_seconds: int = 1800
    settings_path: str = ""  # registers the Telegram approval hook
    mcp_config_path: str = ""  # exposes the ask/notify tools to the run
    enable_chrome: bool = False  # drive the operator's logged-in Chrome
    env_extra: dict[str, str] = field(default_factory=dict)


def build_argv(spec: RunSpec) -> list[str]:
    """Assemble the CLI invocation.

    The prompt goes over stdin, never argv — a prompt beginning with `-` or
    containing a newline would otherwise be parsed as flags.
    """
    argv: list[str] = [
        config.CLAUDE_BIN,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
    ]

    if spec.session_id:
        # First run pins a session id we generated; later runs resume it.
        argv += ["--resume", spec.session_id] if spec.resume else ["--session-id", spec.session_id]

    # `--add-dir` is variadic: one flag, many values. Repeating the flag would
    # drop all but the last group.
    add_dirs = [d for d in spec.add_dirs if d]
    if add_dirs:
        argv += ["--add-dir", *add_dirs]

    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["--effort", spec.effort]
    if spec.permission_mode:
        argv += ["--permission-mode", spec.permission_mode]
    if spec.allowed_tools:
        argv += ["--allowedTools", *spec.allowed_tools]
    if spec.disallowed_tools:
        argv += ["--disallowedTools", *spec.disallowed_tools]
    if spec.append_system_prompt:
        argv += ["--append-system-prompt", spec.append_system_prompt]
    if spec.max_budget_usd > 0:
        argv += ["--max-budget-usd", str(spec.max_budget_usd)]
    if spec.settings_path:
        argv += ["--settings", spec.settings_path]
    if spec.mcp_config_path:
        argv += ["--mcp-config", spec.mcp_config_path]
    if spec.enable_chrome:
        # Without this the Chrome tools are simply absent, and Claude silently
        # falls back to whatever headless browser it can find.
        argv += ["--chrome"]

    return argv


def _tool_detail(name: str, tool_input: dict) -> str:
    """A one-line, phone-sized summary of what a tool call is about to do."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "pattern", "file_path", "path", "url", "query", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _events_from_payload(payload: dict) -> list[Event]:
    kind = payload.get("type")
    session_id = payload.get("session_id") or ""

    if kind == "system" and payload.get("subtype") == "init":
        return [Event(kind="init", session_id=session_id, detail=payload.get("model") or "")]

    if kind == "assistant":
        out: list[Event] = []
        content = (payload.get("message") or {}).get("content") or []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                out.append(Event(kind="text", text=block["text"], session_id=session_id))
            elif block.get("type") == "tool_use":
                name = block.get("name") or "tool"
                out.append(
                    Event(
                        kind="tool",
                        tool=name,
                        detail=_tool_detail(name, block.get("input") or {}),
                        session_id=session_id,
                    )
                )
        return out

    if kind == "result":
        return [
            Event(
                kind="result",
                text=payload.get("result") or "",
                session_id=session_id,
                cost_usd=float(payload.get("total_cost_usd") or 0.0),
                duration_ms=int(payload.get("duration_ms") or 0),
                num_turns=int(payload.get("num_turns") or 0),
                is_error=bool(payload.get("is_error")),
                detail=payload.get("subtype") or "",
            )
        ]

    return []


async def _drain(stream: asyncio.StreamReader | None, sink: list[str]) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        sink.append(line.decode("utf-8", "replace"))


async def stream_run(spec: RunSpec) -> AsyncIterator[Event]:
    """Run Claude Code and yield events. Cancelling the consumer kills the process."""
    argv = build_argv(spec)
    env = dict(os.environ)
    # Belt and braces: some terminals confuse the CLI's renderer when it thinks
    # it owns a TTY. Headless runs should never try to draw.
    env.setdefault("CI", "1")
    env.setdefault("TERM", "dumb")
    # The hook and the ask-server are grandchildren of this process; this is how
    # they learn which run and which chat they belong to.
    env.update(spec.env_extra)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=spec.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=_STDOUT_LIMIT,
        )
    except FileNotFoundError:
        yield Event(
            kind="error",
            text=(
                f"Claude Code CLI not found at {config.CLAUDE_BIN!r}. "
                "Install it, or set CLAUDE_BIN in .env to its absolute path."
            ),
            is_error=True,
        )
        return

    stderr_lines: list[str] = []
    stderr_task = asyncio.create_task(_drain(proc.stderr, stderr_lines))
    saw_result = False

    try:
        if proc.stdin is not None:
            proc.stdin.write(spec.prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        async with asyncio.timeout(spec.timeout_seconds):
            assert proc.stdout is not None
            while True:
                try:
                    raw = await proc.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    # One absurd line; skip it rather than kill the whole run.
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                for event in _events_from_payload(payload):
                    if event.kind == "result":
                        saw_result = True
                    yield event

            await proc.wait()

    except TimeoutError:
        _terminate(proc)
        yield Event(
            kind="error",
            text=(
                f"Timed out after {spec.timeout_seconds}s. "
                "Raise CLAUDE_RUN_TIMEOUT_SECONDS if that is normal for your repo."
            ),
            is_error=True,
        )
        return
    except asyncio.CancelledError:
        _terminate(proc)
        raise
    finally:
        stderr_task.cancel()

    if not saw_result:
        detail = "".join(stderr_lines).strip()[-1500:]
        yield Event(
            kind="error",
            text=(
                f"Claude Code exited with code {proc.returncode} and produced no result."
                + (f"\n\n```\n{detail}\n```" if detail else "")
            ),
            is_error=True,
        )


def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def describe_target(cwd: str, add_dirs: Sequence[str]) -> str:
    parts = [Path(cwd).name]
    parts += [Path(d).name for d in add_dirs if d]
    return " + ".join(dict.fromkeys(parts))
