"""PreToolUse hook: ask for permission on Telegram instead of auto-approving.

Claude Code runs this once per gated tool call, handing it the call on stdin.
It answers with `allow` or `deny` — after asking you, unless a standing rule for
this run already covers the tool.

Fail-closed on purpose. An exit code other than 0 or 2 is a "non-blocking error"
to Claude Code, which lets the tool run; so every failure path here still exits 0
with an explicit deny rather than crashing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge  # noqa: E402
import config  # noqa: E402


def decide(decision: str, reason: str = "") -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        payload["hookSpecificOutput"]["permissionDecisionReason"] = reason
    json.dump(payload, sys.stdout)
    sys.stdout.flush()
    raise SystemExit(0)


def passthrough() -> None:
    """Emit nothing: Claude Code falls back to its normal permission flow."""
    raise SystemExit(0)


def allow(tool_name: str, tool_input: dict | None = None) -> None:
    """Permit the call, doing the two browser chores that have to happen here.

    This is the last point before the tool actually runs, which makes it the
    right place to start Chrome (never for a call that was denied), and the only
    place that sees which browser was chosen.
    """
    if not tool_name.startswith(f"{config.CHROME_SERVER}__"):
        decide("allow")

    if tool_name == f"{config.CHROME_SERVER}__select_browser":
        # Remember it, so the next run does not have to ask again. Every run is
        # a fresh process and the Chrome tools keep no selection of their own.
        config.remember_device_id(str((tool_input or {}).get("deviceId") or ""))

    if config.ENABLE_CHROME and config.CHROME_AUTOSTART:
        import chrome  # local: nothing else in this hot path needs it

        chrome.ensure_sync()
    decide("allow")


def summarize(tool_name: str, tool_input: dict) -> dict:
    """The few fields worth putting on a phone screen."""
    if not isinstance(tool_input, dict):
        return {}

    if tool_name == "Bash":
        return {
            "command": tool_input.get("command") or "",
            "note": tool_input.get("description") or "",
        }
    if tool_name in {"Edit", "Write", "NotebookEdit"}:
        preview = tool_input.get("new_string") or tool_input.get("content") or ""
        return {
            "file": tool_input.get("file_path") or tool_input.get("notebook_path") or "",
            "preview": preview[:400],
            "replace_all": bool(tool_input.get("replace_all")),
        }
    if tool_name == "WebFetch":
        return {"url": tool_input.get("url") or "", "note": tool_input.get("prompt") or ""}

    # Unknown tool: show whatever short string fields it has.
    return {
        key: value[:200]
        for key, value in tool_input.items()
        if isinstance(value, str) and value.strip()
    }


def main() -> None:
    try:
        raw = sys.stdin.read()
    except OSError:
        decide("deny", "Approval hook could not read the tool call.")

    try:
        event = json.loads(raw or "{}")
    except json.JSONDecodeError:
        decide("deny", "Approval hook received malformed input.")

    tool_name = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") or {}

    context = bridge.run_context()
    run_id = context["run_id"]
    if not run_id or not context["chat_id"]:
        # Not a bot-launched run (someone ran `claude` by hand with these
        # settings loaded). Do not hijack their permission prompts.
        passthrough()

    if config.is_auto_allowed(tool_name):
        allow(tool_name, tool_input)

    if bridge.tool_allowed_for_run(run_id, tool_name):
        allow(tool_name, tool_input)

    request_id = bridge.submit(
        {
            "kind": "permission",
            **context,
            "tool_name": tool_name,
            "summary": summarize(tool_name, tool_input),
        }
    )

    response = bridge.wait_response(request_id, timeout=config.APPROVAL_WAIT_SECONDS)

    if response is None:
        decide(
            "deny",
            f"No answer on Telegram within {int(config.APPROVAL_WAIT_SECONDS)}s, so the "
            "call was refused. Ask the user again, or continue with something else.",
        )

    choice = str(response.get("choice") or "deny")
    if choice == "allow_always":
        bridge.allow_tool_for_run(run_id, tool_name)
        allow(tool_name, tool_input)
    if choice == "allow":
        allow(tool_name, tool_input)

    note = str(response.get("note") or "").strip()
    decide("deny", note or "The user declined this on Telegram.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - a crash here would auto-allow
        decide("deny", f"Approval hook failed ({type(exc).__name__}); refusing by default.")
