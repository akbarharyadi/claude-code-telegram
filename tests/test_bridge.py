"""The approval path must fail closed, and the hook's output keys must match
what Claude Code parses. A typo in either is a gate that silently lets
everything through, so both are pinned here."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge  # noqa: E402
import config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hook_permission.py"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    return tmp_path


# ── the queue ─────────────────────────────────────────────────────────────


def test_submit_then_pending_then_respond(state):
    request_id = bridge.submit({"kind": "permission", "tool_name": "Bash"})

    waiting = bridge.pending()
    assert [item["id"] for item in waiting] == [request_id]
    assert waiting[0]["tool_name"] == "Bash"

    bridge.respond(request_id, {"choice": "allow"})
    assert bridge.wait_response(request_id, timeout=2) == {"choice": "allow"}


def test_reading_a_response_clears_both_files(state):
    request_id = bridge.submit({"kind": "ask"})
    bridge.respond(request_id, {"answer": "yes"})
    bridge.wait_response(request_id, timeout=2)

    assert list(bridge.approvals_dir().glob(f"{request_id}*")) == []
    assert bridge.pending() == []


def test_wait_returns_none_on_timeout_and_cleans_up(state):
    request_id = bridge.submit({"kind": "permission"})
    started = time.monotonic()

    assert bridge.wait_response(request_id, timeout=0.6, poll=0.05) is None

    assert time.monotonic() - started >= 0.5
    assert bridge.pending() == []  # a timed-out request must not be re-presented


def test_pending_is_oldest_first(state):
    first = bridge.submit({"kind": "permission"})
    time.sleep(0.01)
    second = bridge.submit({"kind": "permission"})
    assert [item["id"] for item in bridge.pending()] == [first, second]


def test_callback_data_fits_telegrams_64_byte_budget():
    # "p:<id>:allow_always" style payloads must stay under the API limit.
    assert len(f"p:{bridge.new_id()}:A".encode()) <= 64


# ── per-run standing rules ────────────────────────────────────────────────


def test_run_rules_are_scoped_and_clearable(state):
    bridge.allow_tool_for_run("run-a", "Bash")

    assert bridge.tool_allowed_for_run("run-a", "Bash")
    assert not bridge.tool_allowed_for_run("run-a", "Write")
    assert not bridge.tool_allowed_for_run("run-b", "Bash")  # never leaks across runs

    bridge.clear_rules("run-a")
    assert not bridge.tool_allowed_for_run("run-a", "Bash")


def test_run_context_reads_the_injected_environment(monkeypatch):
    monkeypatch.setenv("CCTG_RUN_ID", "r1")
    monkeypatch.setenv("CCTG_CHAT_ID", "42")
    monkeypatch.setenv("CCTG_THREAD_ID", "")
    monkeypatch.setenv("CCTG_SESSION_KEY", "42:0")

    assert bridge.run_context() == {
        "run_id": "r1",
        "chat_id": "42",
        "thread_id": "",
        "session_key": "42:0",
    }


# ── browser selection ─────────────────────────────────────────────────────


def test_browser_choice_is_remembered_then_forgotten(state):
    assert config.remembered_device_id() == ""

    config.remember_device_id("dev-123")
    assert config.remembered_device_id() == "dev-123"
    assert config.effective_device_id() == "dev-123"

    config.forget_device_id()
    assert config.effective_device_id() == ""


def test_env_pin_beats_a_remembered_choice(state, monkeypatch):
    config.remember_device_id("chosen-on-telegram")
    monkeypatch.setattr(config, "CHROME_DEVICE_ID", "pinned-in-env")
    assert config.effective_device_id() == "pinned-in-env"


def test_remembering_nothing_is_a_no_op(state):
    config.remember_device_id("")
    assert config.remembered_device_id() == ""


def test_auto_allow_accepts_a_whole_mcp_server(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ALLOW_TOOLS", {"Read", "mcp__claude-in-chrome"})

    assert config.is_auto_allowed("Read")
    assert config.is_auto_allowed("mcp__claude-in-chrome__computer")
    assert not config.is_auto_allowed("Bash")
    # A server entry must not match a different server that merely shares a prefix.
    assert not config.is_auto_allowed("mcp__claude-in-chrome-other__computer")


# ── the hook's contract with Claude Code ──────────────────────────────────


def run_hook(event: dict, state_dir: Path, **env_overrides) -> tuple[int, dict | None]:
    env = {
        **os.environ,
        "STATE_DIR": str(state_dir),
        "CCTG_RUN_ID": "run-1",
        "CCTG_CHAT_ID": "42",
        "APPROVAL_WAIT_SECONDS": "1",
        **env_overrides,
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    stdout = proc.stdout.strip()
    return proc.returncode, (json.loads(stdout) if stdout else None)


def decision_of(payload: dict | None) -> str | None:
    if not payload:
        return None
    return payload.get("hookSpecificOutput", {}).get("permissionDecision")


def test_hook_auto_allows_read_only_tools(tmp_path):
    code, payload = run_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}}, tmp_path)
    assert code == 0
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert decision_of(payload) == "allow"


def test_hook_denies_when_nobody_answers(tmp_path):
    code, payload = run_hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, tmp_path)
    assert code == 0
    assert decision_of(payload) == "deny"
    assert "Telegram" in payload["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_denies_on_malformed_input(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        env={**os.environ, "STATE_DIR": str(tmp_path), "CCTG_RUN_ID": "r", "CCTG_CHAT_ID": "1"},
        timeout=60,
    )
    assert proc.returncode == 0  # a non-zero exit would let the tool run
    assert decision_of(json.loads(proc.stdout)) == "deny"


def test_hook_stays_out_of_non_bot_runs(tmp_path):
    """Someone running `claude` by hand must keep their own permission prompts."""
    code, payload = run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        tmp_path,
        CCTG_RUN_ID="",
        CCTG_CHAT_ID="",
    )
    assert code == 0
    assert payload is None  # no decision emitted


def test_hook_allows_once_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    answered = threading.Event()

    def approve() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for request in bridge.pending():
                bridge.respond(str(request["id"]), {"choice": "allow"})
                answered.set()
                return
            time.sleep(0.05)

    responder = threading.Thread(target=approve, daemon=True)
    responder.start()
    code, payload = run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
        tmp_path,
        APPROVAL_WAIT_SECONDS="30",
    )
    responder.join(timeout=5)

    assert answered.is_set(), "the hook never queued a request"
    assert code == 0
    assert decision_of(payload) == "allow"


def test_hook_honours_a_standing_run_rule_without_asking(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    bridge.allow_tool_for_run("run-1", "Bash")

    # APPROVAL_WAIT_SECONDS=1, so anything that queues a request would deny.
    code, payload = run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, tmp_path)

    assert code == 0
    assert decision_of(payload) == "allow"
    assert bridge.pending() == []
