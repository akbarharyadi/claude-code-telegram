"""OpenCode 2 backend: same contract as claude_runner, different plumbing.

Claude Code is driven as a subprocess per message. OpenCode 2 (the `opencode2`
beta) does not work that way headlessly — a permission ask in `opencode2 run`
is rejected immediately, because nothing is attached to answer it. What it does
have is a local HTTP API (the same one its TUI uses): sessions, prompts as
events, and — critically — *replyable* permission requests and question forms.
That is what makes the Telegram approval buttons work here.

So this module keeps one `opencode2 serve` process running on localhost and,
for each run: creates or reuses a session, posts the prompt, then consumes the
server's event stream, translating it into the same `Event` sequence the bot
already understands. Approvals travel over the same `state/approvals` file
queue the Claude Code hook uses, so the bot's presentation code is unchanged.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import os
import subprocess
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx

import bridge
import config
from claude_runner import Event, RunSpec, describe_target  # re-exported

log = logging.getLogger(__name__)

# Permission actions opencode can ask for, mapped to the tool names the bot's
# approval renderer already knows how to show. The beta calls Bash "shell".
_ACTION_TO_TOOL = {
    "bash": "Bash",
    "shell": "Bash",
    "edit": "Edit",
    "write": "Write",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "task": "Task",
    "read": "Read",
    "external_directory": "ExternalDir",
    "doom_loop": "Repeat",
}

_SERVER_START_TIMEOUT = 60.0
# The server only streams log lines, but the same belt-and-braces limit the
# Claude runner uses costs nothing.
_STDOUT_LIMIT = 32 * 1024 * 1024
# A server-configured agent with the dangerous tools removed entirely; runs
# that must be read-only (PR review) execute as this agent.
LOCKED_AGENT = "cctg-locked"


# ── server lifecycle ──────────────────────────────────────────────────────

# `opencode2 serve` prints its credentials to stdout:
#   server listening on http://127.0.0.1:42777
#   server password <token>
# Every API call needs HTTP Basic auth with them. We persist the password so a
# restarted bot can adopt the server it spawned before instead of piling up
# processes on the same port.

_server_proc: asyncio.subprocess.Process | None = None
_server_password: str = ""
_server_config_hash: str = ""


def _base_url() -> str:
    return f"http://127.0.0.1:{config.OPENCODE_PORT}"


def _credentials_file() -> Path:
    return config.STATE_DIR / "opencode-server.json"


def _bin_command() -> list[str]:
    """How to launch the CLI. npm/pnpm shims on Windows are .cmd files, which
    only cmd.exe can execute."""
    bin_path = config.OPENCODE_BIN
    if bin_path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", bin_path]
    return [bin_path]


def _permission_config() -> dict:
    """The `permission` block for the server config, mirroring the Claude Code
    gate: ask before anything that changes state, allow read-only tools,
    and honour CLAUDE_DISALLOWED_TOOLS as explicit denies."""
    denies = config.DISALLOWED_TOOLS
    if not config.APPROVALS_ENABLED:
        permission: dict | str = "allow"
        if denies:
            permission = {}
            _apply_denies(permission, denies)
        return permission

    permission = {
        "bash": {"*": "ask"},
        "edit": "ask",
        # Parity with the Claude runner: `--add-dir` siblings and the download
        # folder are explicitly configured by the operator, so reading them is
        # not a fresh escalation.
        "external_directory": {
            **{f"{path}/**": "allow" for path in _external_dirs()},
            "**": "ask",
        },
        # Claude has no equivalent gate; a loop breaking its own repetition
        # would only spam approvals.
        "doom_loop": "allow",
    }
    _apply_denies(permission, denies)
    return permission


def _apply_denies(permission: dict, denies: Sequence[str]) -> None:
    """Translate `Tool` / `Tool(pattern:*)` specs into opencode denies."""
    for entry in denies:
        name, _, pattern = entry.partition("(")
        name = name.strip()
        pattern = pattern.rstrip(")").rstrip(":*").strip()
        key = _ACTION_TO_TOOL.get(name.lower(), name.lower())
        if key == "Write":
            key = "edit"  # opencode folds write/patch into `edit`
        if pattern:
            if not isinstance(permission.get(key), dict):
                permission[key] = {}
            permission[key][f"{pattern}*"] = "deny"
        else:
            permission[key] = "deny"


def _external_dirs() -> list[str]:
    from config import CLAUDE_EXTRA_WORKDIRS, DOWNLOAD_DIR  # local: config is cheap

    dirs = [str(DOWNLOAD_DIR), *CLAUDE_EXTRA_WORKDIRS]
    return [str(Path(d).resolve()) for d in dirs if d]


def _etiquette_for_opencode() -> str:
    """The system prompt additions, adjusted for what exists in opencode2:
    no `mcp__tg__*` tools there — questions go through its built-in question
    tool, which the bot answers from Telegram."""
    text = config.append_system_prompt()
    text = text.replace("mcp__tg__ask_user", "the built-in question tool")
    text = text.replace("mcp__tg__notify", "a short status line in your reply")
    if config.ENABLE_CHROME:
        text += (
            " The Claude-in-Chrome browser tools are unavailable in this backend; "
            "use a headless browser instead."
        )
    return text


def _server_config_content() -> str:
    """Inline config (OPENCODE_CONFIG_CONTENT): the permission gate plus the
    Telegram etiquette as instructions. Server-scoped, so it is rebuilt only
    when something that affects it changes."""
    instructions = config.STATE_DIR / "opencode-instructions.md"
    config.ensure_dirs()
    instructions.write_text(_etiquette_for_opencode(), encoding="utf-8")

    content = {
        "permission": _permission_config(),
        "instructions": [str(instructions)],
        "agent": {
            LOCKED_AGENT: {
                "tools": {
                    "bash": False,
                    "edit": False,
                    "write": False,
                    "patch": False,
                    "webfetch": False,
                    "websearch": False,
                    "task": False,
                },
            },
        },
    }
    return json.dumps(content)


async def ensure_server() -> tuple[str, str]:
    """Make sure an `opencode2 serve` we can authenticate against is running.

    Returns (base_url, password). Adopts a healthy server we started earlier
    (credentials + config hash from state); otherwise spawns one and waits for
    it to print its password.
    """
    global _server_proc, _server_password, _server_config_hash

    config_hash = _server_config_content()
    stored = _read_credentials()

    if stored and _healthy(_base_url(), stored["password"]):
        if stored.get("config") == config_hash:
            _server_password = stored["password"]
            _server_config_hash = config_hash
            return _base_url(), _server_password
        # The running server still gates with the old config; replace it.
        _kill_stored_pid(stored)

    if _server_proc is not None and _server_proc.returncode is None:
        _kill_server()
        _server_proc = None

    await _wait_port_free()

    env = dict(os.environ)
    env["OPENCODE_CONFIG_CONTENT"] = config_hash
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    password = ""
    for attempt in range(2):
        _server_proc = await asyncio.create_subprocess_exec(
            *_bin_command(),
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(config.OPENCODE_PORT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=flags,
            limit=_STDOUT_LIMIT,
        )
        try:
            async with asyncio.timeout(_SERVER_START_TIMEOUT):
                while _server_proc.stdout is not None:
                    line = await _server_proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", "replace").strip()
                    if text.startswith("server password "):
                        password = text.removeprefix("server password ").strip()
                        break
        except TimeoutError:
            _kill_server()
            raise RuntimeError(
                f"opencode2 serve printed no password within {int(_SERVER_START_TIMEOUT)}s. "
                "Is the port already taken by something else? Try another OPENCODE_PORT."
            ) from None
        if password:
            break
        # Usually the port was still held for a moment by the server we just
        # replaced; give the socket a beat and try once more.
        if attempt == 0:
            await asyncio.sleep(2.0)
            await _wait_port_free()

    if not password:
        _kill_server()
        raise RuntimeError(
            "opencode2 serve exited before printing a password "
            "(see `uv run doctor.py` or run `opencode2 serve` by hand)."
        )

    _server_password = password
    _server_config_hash = config_hash
    _write_credentials(password, config_hash)

    deadline = time.monotonic() + _SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        if _healthy(_base_url(), password):
            return _base_url(), password
        await asyncio.sleep(0.4)

    _kill_server()
    raise RuntimeError(
        f"opencode2 server did not come up on port {config.OPENCODE_PORT} "
        f"within {int(_SERVER_START_TIMEOUT)}s."
    )


def _read_credentials() -> dict | None:
    try:
        data = json.loads(_credentials_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("password"):
        return None
    return data


def _write_credentials(password: str, config_hash: str) -> None:
    config.ensure_dirs()
    pid = _server_proc.pid if _server_proc is not None else None
    _credentials_file().write_text(
        json.dumps({"url": _base_url(), "password": password, "pid": pid, "config": config_hash}),
        encoding="utf-8",
    )


def _kill_stored_pid(stored: dict) -> None:
    """The server from a previous bot run gates with a stale config. It is not
    ours to manage gracefully — terminate it by pid."""
    pid = stored.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def _healthy(base_url: str, password: str) -> bool:
    try:
        with httpx.Client(timeout=2.0, auth=("opencode", password)) as client:
            return client.get(f"{base_url}/api/project").status_code == 200
    except httpx.HTTPError:
        return False


def _kill_server() -> None:
    global _server_proc
    if _server_proc is not None and _server_proc.returncode is None:
        try:
            # os.kill works even when the caller has no running event loop
            # (atexit after asyncio.run), unlike the transport-based kill.
            os.kill(_server_proc.pid, 9)
        except OSError:
            pass
    _server_proc = None


async def _wait_port_free(timeout: float = 8.0) -> None:
    """Block until OPENCODE_PORT accepts a bind, so a just-replaced server's
    lingering socket cannot fail the fresh spawn."""
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", config.OPENCODE_PORT))
                return
            except OSError:
                pass
        await asyncio.sleep(0.4)


def shutdown() -> None:
    """Stop the server this process spawned (bot shutdown / atexit)."""
    _kill_server()


# ── the API surface ───────────────────────────────────────────────────────


def _sse_payload(line: str) -> dict | None:
    """One SSE line -> the event object. Handles both `data: {json}` framing
    and bare JSON lines, whichever the beta emits."""
    text = line[5:].strip() if line.startswith("data:") else line.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    # Dev-branch events nest under `payload`; the beta sends them flat.
    inner = parsed.get("payload")
    return inner if isinstance(inner, dict) else parsed


def _event_data(payload: dict) -> dict:
    data = payload.get("data") or payload.get("properties") or {}
    return data if isinstance(data, dict) else {}


def _model_ids(model: str) -> dict | None:
    """`provider/model` -> the wire shape the beta wants: {providerID, id}."""
    provider, _, model_id = model.partition("/")
    if not provider or not model_id:
        return None
    return {"providerID": provider, "id": model_id.split("#", 1)[0]}


def _resolved_model(requested: str) -> dict | None:
    """The model to pin a session to, falling back to OPENCODE_MODEL.

    `_model_ids` wants `provider/model`, and a bare name — say the claude
    backend's `claude-opus-5` left in REVIEW_MODEL after a backend switch —
    parses to None, which silently drops the field and lets the server pick
    its own gateway model. That is how a review once ended up on a 256k-context
    default and 400'd on diffs the 1M GLM reads fine. A malformed request now
    falls back loudly to the model we know the plan provides.
    """
    ids = _model_ids(requested)
    if ids or not config.OPENCODE_MODEL:
        return ids
    log.warning(
        "model %r is not provider/model — falling back to %s",
        requested,
        config.OPENCODE_MODEL,
    )
    return _model_ids(config.OPENCODE_MODEL)


def _tool_label(action: str) -> str:
    return _ACTION_TO_TOOL.get(action, action.capitalize() or "Tool")


def _summarize(action: str, data: dict) -> dict:
    """The few fields worth putting on a phone screen, matching the shapes
    bot._render_permission already renders for Claude tool calls."""
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    message = str(data.get("message") or "")
    resources = [str(r) for r in (data.get("resources") or []) if r]

    if action in {"bash", "shell"}:
        command = str(metadata.get("command") or (resources or [""])[0] or message)
        return {"command": command, "note": str(metadata.get("description") or "")}
    if action in {"edit", "write"}:
        file_path = str(metadata.get("path") or metadata.get("filePath") or (resources or [""])[0])
        return {
            "file": file_path,
            "preview": str(metadata.get("content") or "")[:400],
            "replace_all": bool(metadata.get("replace_all")),
        }
    if action == "webfetch":
        return {"url": str(metadata.get("url") or (resources or [""])[0]), "note": ""}
    if action == "external_directory":
        return {"path": (resources or [""])[0], "note": message}
    # Unknown action: show whatever short string fields exist.
    return {
        key: str(value)[:200]
        for key, value in {**metadata, "note": message}.items()
        if isinstance(value, str) and value.strip()
    }


async def _reply_permission(
    client: httpx.AsyncClient, session_id: str, permission_id: str, response: str,
    message: str = "",
) -> None:
    body: dict = {"reply": response}
    if message:
        body["message"] = message
    try:
        reply = await client.post(
            f"{_base_url()}/api/session/{session_id}/permission/{permission_id}/reply",
            json=body,
        )
        if reply.status_code >= 400:
            # Without a accepted reply the tool call sits pending forever, so a
            # rejected answer here is the difference between working and stuck.
            log.error(
                "permission reply rejected (%s): %s", reply.status_code, reply.text[:200]
            )
    except httpx.HTTPError as exc:
        log.warning("permission reply failed: %s", exc)


async def _decide_permission(client: httpx.AsyncClient, spec: RunSpec, data: dict) -> None:
    """Ask the operator on Telegram, then answer the server. Same queue, same
    buttons, same fail-closed timeout as the Claude Code hook."""
    context = {
        "run_id": spec.env_extra.get("CCTG_RUN_ID", ""),
        "chat_id": spec.env_extra.get("CCTG_CHAT_ID", ""),
        "thread_id": spec.env_extra.get("CCTG_THREAD_ID", ""),
        "session_key": spec.env_extra.get("CCTG_SESSION_KEY", ""),
    }
    action = str(data.get("action") or "")
    tool = _tool_label(action)
    permission_id = str(data.get("id") or "")
    session_id = str(data.get("sessionID") or "")

    if spec.env_extra.get("CCTG_REVIEW"):
        # A review run executes as the locked agent, whose mutating tools do
        # not exist — so the only ask that can surface is a read gate (the
        # deep-review worktree sits outside the configured dirs). Allow it:
        # rejecting here once killed a whole seven-minute review mid-flight.
        await _reply_permission(client, session_id, permission_id, "once")
        return

    if not context["run_id"] or not context["chat_id"]:
        # Not a bot-launched run (doctor, manual use). Do not hijack it, but a
        # pending ask must not hang the stream either — the default is reject.
        await _reply_permission(client, session_id, permission_id, "reject")
        return

    if bridge.tool_allowed_for_run(context["run_id"], tool):
        await _reply_permission(client, session_id, permission_id, "once")
        return

    request = {
        "kind": "permission",
        **context,
        "tool_name": tool,
        "summary": _summarize(action, data),
    }
    request_id = bridge.submit(request)

    response = await bridge.wait_response_async(
        request_id, timeout=config.APPROVAL_WAIT_SECONDS
    )
    if response is None:
        await _reply_permission(
            client, session_id, permission_id, "reject",
            message="No answer on Telegram in time, so the call was refused.",
        )
        return

    choice = str(response.get("choice") or "deny")
    if choice == "allow_always":
        bridge.allow_tool_for_run(context["run_id"], tool)
        await _reply_permission(client, session_id, permission_id, "always")
    elif choice == "allow":
        await _reply_permission(client, session_id, permission_id, "once")
    else:
        note = str(response.get("note") or "").strip()
        await _reply_permission(client, session_id, permission_id, "reject", message=note)


async def _decide_form(client: httpx.AsyncClient, spec: RunSpec, data: dict) -> None:
    """OpenCode's native question tool surfaces as a form; route it to Telegram
    like the ask-server does for Claude runs."""
    form = data.get("form") if isinstance(data.get("form"), dict) else data
    form_id = str(form.get("id") or "")
    session_id = str(form.get("sessionID") or "")
    fields = form.get("fields") if isinstance(form.get("fields"), list) else []

    context = {
        "run_id": spec.env_extra.get("CCTG_RUN_ID", ""),
        "chat_id": spec.env_extra.get("CCTG_CHAT_ID", ""),
        "thread_id": spec.env_extra.get("CCTG_THREAD_ID", ""),
        "session_key": spec.env_extra.get("CCTG_SESSION_KEY", ""),
    }
    if not context["run_id"] or not context["chat_id"] or not form_id:
        try:
            await client.post(
                f"{_base_url()}/api/session/{session_id}/form/{form_id}/cancel"
            )
        except httpx.HTTPError:
            pass
        return

    options: list[str] = []
    field_key = ""
    for field in fields:
        if not isinstance(field, dict):
            continue
        field_key = str(field.get("key") or field.get("id") or field.get("name") or "")
        raw_options = field.get("options")
        if isinstance(raw_options, list) and raw_options:
            options = [
                str(o.get("label") if isinstance(o, dict) else o) for o in raw_options
            ]
            break

    request = {
        "kind": "ask",
        **context,
        "question": str(form.get("title") or form.get("message") or "The agent has a question."),
        "options": options,
    }
    request_id = bridge.submit(request)
    response = await bridge.wait_response_async(
        request_id, timeout=config.ASK_WAIT_SECONDS
    )

    answer = str((response or {}).get("answer") or "")
    if not answer:
        try:
            await client.post(
                f"{_base_url()}/api/session/{session_id}/form/{form_id}/cancel"
            )
        except httpx.HTTPError:
            pass
        return

    body = {field_key or "answer": answer}
    try:
        await client.post(
            f"{_base_url()}/api/session/{session_id}/form/{form_id}/reply", json=body
        )
    except httpx.HTTPError as exc:
        log.warning("form reply failed: %s", exc)


# ── the run loop ──────────────────────────────────────────────────────────


async def _create_session(client: httpx.AsyncClient, spec: RunSpec, auth: tuple[str, str]) -> str:
    body: dict = {
        "title": describe_target(spec.cwd, spec.add_dirs),
        "location": {"directory": spec.cwd},
    }
    model = _resolved_model(spec.model or config.OPENCODE_MODEL)
    if model:
        body["model"] = model
    if spec.disallowed_tools:
        # A run that must not touch anything (the PR reviewer) gets a
        # purpose-built agent whose dangerous tools do not exist at all.
        body["agent"] = LOCKED_AGENT

    response = await client.post(
        f"{_base_url()}/api/session", json=body, auth=auth
    )
    if response.status_code in {400, 422} and "location" in body:
        # Older beta: location was a plain directory string.
        body["location"] = spec.cwd
        response = await client.post(
            f"{_base_url()}/api/session", json=body, auth=auth
        )
    response.raise_for_status()
    data = response.json()
    session_id = str((data.get("data") or data).get("id") or "")
    if not session_id:
        raise RuntimeError(f"opencode2 returned no session id: {data}")
    return session_id


async def stream_run(spec: RunSpec) -> AsyncIterator[Event]:
    """Run one OpenCode turn and yield events. Cancelling the consumer aborts
    the session."""
    started = time.monotonic()
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0)

    try:
        base_url, password = await ensure_server()
    except (RuntimeError, FileNotFoundError) as exc:
        yield Event(kind="error", text=str(exc), is_error=True)
        return

    auth = ("opencode", password)
    async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
        session_id = spec.session_id if spec.resume else ""

        try:
            async with asyncio.timeout(spec.timeout_seconds):
                async with client.stream("GET", f"{_base_url()}/api/event") as stream:
                    if not session_id:
                        session_id = await _create_session(client, spec, auth)
                        log.info("created session %s", session_id)
                    else:
                        log.info("resuming session %s", session_id)
                    yield Event(kind="init", session_id=session_id)

                    body: dict = {"text": spec.prompt}
                    model = _resolved_model(spec.model or config.OPENCODE_MODEL)
                    if model and spec.resume:
                        # The model was pinned at creation; /model changes and
                        # resumed sessions get the switch applied here.
                        with contextlib.suppress(httpx.HTTPError):
                            await client.post(
                                f"{_base_url()}/api/session/{session_id}/model",
                                json={"model": model},
                            )
                    posted = await client.post(
                        f"{_base_url()}/api/session/{session_id}/prompt",
                        json=body,
                    )
                    if posted.status_code == 404 and spec.resume:
                        # Session from before a `/new`, a backend switch, or a
                        # wiped server — start over instead of failing.
                        session_id = await _create_session(client, spec, auth)
                        yield Event(kind="init", session_id=session_id)
                        posted = await client.post(
                            f"{_base_url()}/api/session/{session_id}/prompt",
                            json=body,
                        )
                    posted.raise_for_status()
                    log.info("prompt accepted for %s", session_id)

                    texts_by_message: dict[str, list[str]] = {}
                    message_order: list[str] = []
                    stats = {"cost": 0.0, "turns": 0, "error": ""}
                    async for _event in _consume(
                        stream, client, spec, session_id, texts_by_message, message_order, stats
                    ):
                        yield _event
                    final_text = ""
                    if message_order:
                        final_text = "".join(texts_by_message[message_order[-1]]).strip()
                    cost = float(stats["cost"])
                    turns = int(stats["turns"])
                    error_text = str(stats["error"])
        except TimeoutError:
            await _abort(session_id)
            yield Event(
                kind="error",
                text=(
                    f"Timed out after {spec.timeout_seconds}s. "
                    "Raise CLAUDE_RUN_TIMEOUT_SECONDS if that is normal for your repo."
                ),
                session_id=session_id,
                is_error=True,
            )
            return
        except asyncio.CancelledError:
            await _abort(session_id)
            raise
        except httpx.HTTPError as exc:
            yield Event(
                kind="error",
                text=f"opencode2 server error: {exc}",
                session_id=session_id,
                is_error=True,
            )
            return

        yield Event(
            kind="result",
            text=final_text,
            session_id=session_id,
            cost_usd=cost,
            duration_ms=int((time.monotonic() - started) * 1000),
            num_turns=turns,
            is_error=bool(error_text and not final_text),
            detail=error_text or "ok",
        )
        if error_text and final_text:
            yield Event(kind="error", text=error_text, session_id=session_id, is_error=True)


async def _consume(
    stream: httpx.Response,
    client: httpx.AsyncClient,
    spec: RunSpec,
    session_id: str,
    texts_by_message: dict[str, list[str]],
    message_order: list[str],
    stats: dict,
) -> AsyncIterator[Event]:
    """Read the SSE stream until the execution finishes, yielding bot events
    and filling `texts_by_message` / `stats` for the final result.

    Event names are the opencode2 beta's own (verified empirically): text
    streams as `session.text.delta`, tool calls as `session.tool.called`, the
    turn ends with `session.execution.succeeded` or `session.execution.failed`.
    """
    tool_names: dict[str, str] = {}

    async for line in stream.aiter_lines():
        payload = _sse_payload(line)
        if not payload:
            continue
        etype = str(payload.get("type") or "")
        data = _event_data(payload)
        data_session = str(data.get("sessionID") or "")
        if etype != "session.usage.updated":
            log.debug("sse %s %s", etype, str(data)[:120])

        if etype.startswith("session."):
            if data_session and data_session != session_id:
                continue

            if etype == "session.text.delta":
                message_id = str(data.get("assistantMessageID") or "")
                if message_id not in texts_by_message:
                    texts_by_message[message_id] = []
                    message_order.append(message_id)
                delta = str(data.get("delta") or "")
                texts_by_message[message_id].append(delta)
                if delta:
                    yield Event(kind="text", text=delta, session_id=session_id)

            elif etype == "session.text.ended":
                message_id = str(data.get("assistantMessageID") or "")
                text = str(data.get("text") or "")
                if message_id and text:
                    texts_by_message[message_id] = [text]

            elif etype == "session.tool.input.started":
                tool_names[str(data.get("id") or "")] = str(data.get("name") or "tool")

            elif etype == "session.tool.called":
                call_id = str(data.get("id") or "")
                yield Event(
                    kind="tool",
                    tool=tool_names.get(call_id, "tool"),
                    detail=_tool_detail(data.get("input")),
                    session_id=session_id,
                )

            elif etype == "session.usage.updated":
                stats["cost"] = float(data.get("cost") or 0.0)
                stats["tokens"] = data.get("tokens") or {}

            elif etype == "session.execution.succeeded":
                stats["turns"] = int(stats.get("turns") or 0) + 1
                return

            elif etype == "session.execution.failed":
                error = data.get("error")
                message = ""
                if isinstance(error, dict):
                    inner = error.get("data") or error
                    message = str(inner.get("message") or "") if isinstance(inner, dict) else ""
                stats["error"] = message or "the run failed"
                yield Event(
                    kind="error", text=stats["error"], session_id=session_id, is_error=True
                )
                return

            elif etype == "session.execution.interrupted":
                # opencode2 ends the whole turn when an approval is rejected —
                # unlike Claude Code, the model does not get to explain after.
                stats["error"] = "the run was stopped (a tool call was declined)"
                yield Event(
                    kind="error",
                    text=stats["error"],
                    session_id=session_id,
                    is_error=True,
                )
                return

        elif etype == "permission.asked":
            if data_session == session_id:
                await _decide_permission(client, spec, data)

        elif etype == "form.created":
            form = data.get("form") if isinstance(data.get("form"), dict) else {}
            if str(form.get("sessionID") or data_session) == session_id:
                await _decide_form(client, spec, data)


def _tool_detail(tool_input: object) -> str:
    """A one-line, phone-sized summary of what a tool call is doing."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "pattern", "filePath", "path", "url", "query", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


async def _abort(session_id: str) -> None:
    if not session_id:
        return
    try:
        auth = ("opencode", _server_password) if _server_password else None
        async with httpx.AsyncClient(timeout=10.0, auth=auth) as client:
            await client.post(f"{_base_url()}/api/session/{session_id}/interrupt")
    except httpx.HTTPError:
        pass


atexit.register(shutdown)
