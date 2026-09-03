"""Telegram bot that runs Claude Code on your machine and reports back.

Send a message, get an answer. Send a screenshot, Claude reads it. Reply in the
same chat and the conversation continues in the same Claude Code session.

Run with:  uv run bot.py
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import time
import uuid
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import agent_runner
import bridge
import chrome
import config
import pr_review
import store
import tg_format
from agent_runner import RunSpec

log = logging.getLogger("claude-telegram")

# How long to wait for the rest of an album before treating it as complete.
MEDIA_GROUP_DEBOUNCE = 1.5
# Telegram tolerates about one edit per second; stay well under it.
PROGRESS_EDIT_INTERVAL = 2.5
MAX_PROGRESS_LINES = 6

_sessions: dict[str, store.Session] = {}
_jobs: dict[str, asyncio.Task] = {}
_media_groups: dict[str, dict] = {}
_media_lock = asyncio.Lock()

# Approval/question plumbing (see bridge.py).
_run_settings = ""
_run_mcp = ""
_seen_requests: dict[str, float] = {}
_awaiting_text: dict[str, str] = {}  # session key -> request id awaiting a typed answer
_watcher_task: asyncio.Task | None = None
_review_task: asyncio.Task | None = None


# ── access control ────────────────────────────────────────────────────────


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return False
    if user.id not in config.ALLOWED_USER_IDS:
        return False
    if config.ALLOWED_CHAT_IDS and chat.id not in config.ALLOWED_CHAT_IDS:
        return False
    return True


# ── session bookkeeping ───────────────────────────────────────────────────


def session_key(update: Update) -> str:
    chat = update.effective_chat
    message = update.effective_message
    thread = getattr(message, "message_thread_id", None) if message else None
    return f"{chat.id}:{thread or 0}"


def _fresh_session(cwd: str, add_dirs: list[str], model: str) -> store.Session:
    """Claude Code lets the bot pin the session id up front; OpenCode mints its
    own (`ses_…`) when the server creates the session, so start empty there."""
    session_id = "" if config.AGENT_BACKEND == "opencode" else str(uuid.uuid4())
    return store.Session(
        session_id=session_id,
        cwd=cwd,
        add_dirs=add_dirs,
        model=model,
        backend=config.AGENT_BACKEND,
    )


def get_session(key: str) -> store.Session:
    session = _sessions.get(key)
    if session is None:
        session = _fresh_session(
            config.CLAUDE_WORKDIR,
            list(config.CLAUDE_ADD_DIRS),
            config.CLAUDE_MODEL,
        )
        _sessions[key] = session
        store.save_sessions(_sessions)
    return session


def reset_session(key: str) -> store.Session:
    previous = _sessions.get(key)
    session = _fresh_session(
        previous.cwd if previous else config.CLAUDE_WORKDIR,
        list(previous.add_dirs) if previous else list(config.CLAUDE_ADD_DIRS),
        previous.model if previous else config.CLAUDE_MODEL,
    )
    _sessions[key] = session
    store.save_sessions(_sessions)
    return session


# ── sending ───────────────────────────────────────────────────────────────


async def send_markdown(update: Update, text: str) -> None:
    """Send Claude's markdown, degrading to plain text if Telegram rejects the HTML."""
    message = update.effective_message
    if message is None:
        return
    for chunk in tg_format.to_html_chunks(text):
        try:
            await message.reply_text(
                chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        except BadRequest as exc:
            log.warning("HTML send rejected (%s); falling back to plain text", exc)
            for plain in tg_format.to_plain_chunks(text):
                await message.reply_text(plain, disable_web_page_preview=True)
            return
        await asyncio.sleep(0.05)


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


class Progress:
    """One editable Telegram message that shows what Claude is doing right now."""

    def __init__(self, message, header: str) -> None:
        self._message = message
        self._header = header
        self._lines: list[str] = []
        self._steps = 0
        self._started = time.monotonic()
        self._last_edit = 0.0
        self._last_text = ""

    def note_tool(self, tool: str, detail: str) -> None:
        self._steps += 1
        label = html.escape(tool)
        suffix = f" · <code>{html.escape(tg_format.truncate(detail, 90))}</code>" if detail else ""
        self._lines.append(f"🔧 <b>{label}</b>{suffix}")
        del self._lines[:-MAX_PROGRESS_LINES]

    def _render(self) -> str:
        elapsed = _fmt_duration(time.monotonic() - self._started)
        body = "\n".join(self._lines) or "<i>thinking…</i>"
        return f"{self._header}\n{body}\n\n⏱ {elapsed} · {self._steps} steps"

    async def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_edit < PROGRESS_EDIT_INTERVAL:
            return
        text = self._render()
        if text == self._last_text:
            return
        self._last_edit = now
        self._last_text = text
        try:
            await self._message.edit_text(text, parse_mode=ParseMode.HTML)
        except BadRequest:
            pass  # "message is not modified", or the user deleted it
        except TelegramError as exc:
            log.debug("progress edit failed: %s", exc)

    async def finish(self, summary: str) -> None:
        try:
            await self._message.edit_text(summary, parse_mode=ParseMode.HTML)
        except TelegramError:
            pass

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started


# ── the core: run Claude for one incoming message ─────────────────────────


async def run_job(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    key = session_key(update)
    message = update.effective_message
    if message is None:
        return

    if key in _jobs and not _jobs[key].done():
        await message.reply_text(
            "⏳ Still working on the previous message. Send /stop to cancel it."
        )
        return

    session = get_session(key)
    if session.backend != config.AGENT_BACKEND:
        # A session id from the other backend means nothing to this one.
        session = reset_session(key)
    add_dirs = [*session.add_dirs, str(config.DOWNLOAD_DIR)]
    target = agent_runner.describe_target(session.cwd, session.add_dirs)
    header = f"🤖 <b>{html.escape(target)}</b>"

    placeholder = await message.reply_text(f"{header}\n<i>thinking…</i>", parse_mode=ParseMode.HTML)
    progress = Progress(placeholder, header)

    run_id = uuid.uuid4().hex[:12]
    thread_id = getattr(message, "message_thread_id", None)

    spec = RunSpec(
        prompt=prompt,
        cwd=session.cwd,
        session_id=session.session_id,
        resume=session.started,
        add_dirs=add_dirs,
        model=session.model,
        effort=config.CLAUDE_EFFORT,
        permission_mode=config.effective_permission_mode(),
        allowed_tools=config.effective_allowed_tools(),
        disallowed_tools=config.DISALLOWED_TOOLS,
        append_system_prompt=config.append_system_prompt(),
        max_budget_usd=config.MAX_BUDGET_USD,
        timeout_seconds=config.RUN_TIMEOUT_SECONDS,
        settings_path=_run_settings,
        mcp_config_path=_run_mcp,
        enable_chrome=config.ENABLE_CHROME,
        env_extra={
            "CCTG_RUN_ID": run_id,
            "CCTG_CHAT_ID": str(message.chat_id),
            "CCTG_THREAD_ID": str(thread_id or ""),
            "CCTG_SESSION_KEY": key,
        },
    )

    async def worker() -> None:
        final_text = ""
        streamed: list[str] = []
        error_text = ""
        cost = 0.0
        typing_at = 0.0

        try:
            async for event in agent_runner.stream_run(spec):
                now = time.monotonic()
                if now - typing_at > 4:
                    typing_at = now
                    try:
                        await context.bot.send_chat_action(
                            chat_id=message.chat_id, action=ChatAction.TYPING
                        )
                    except TelegramError:
                        pass

                if event.kind == "tool":
                    progress.note_tool(event.tool, event.detail)
                    await progress.refresh()
                elif event.kind == "text":
                    streamed.append(event.text)
                elif event.kind == "init" and event.session_id:
                    session.session_id = event.session_id
                elif event.kind == "result":
                    final_text = event.text
                    cost = event.cost_usd
                    if event.session_id:
                        session.session_id = event.session_id
                    session.started = True
                    session.turns += 1
                    session.cost_usd += cost
                    session.updated_at = time.time()
                    store.save_sessions(_sessions)
                    if event.is_error and not final_text:
                        error_text = f"Claude reported an error ({event.detail or 'unknown'})."
                elif event.kind == "error":
                    error_text = event.text

        except asyncio.CancelledError:
            await progress.finish("🛑 Cancelled.")
            raise
        except Exception as exc:  # noqa: BLE001 - a bot that dies is worse than a logged bug
            log.exception("run failed")
            error_text = f"Bridge error: {type(exc).__name__}: {exc}"

        summary = f"✅ {_fmt_duration(progress.elapsed)}"
        if cost:
            summary += f" · ${cost:.3f}"
        if error_text:
            summary = f"⚠️ {_fmt_duration(progress.elapsed)}"
        await progress.finish(summary)

        body = final_text or "\n".join(streamed).strip()
        if error_text:
            body = f"⚠️ {error_text}" + (f"\n\n{body}" if body else "")
        if not body:
            body = "_(Claude produced no output.)_"
        await send_markdown(update, body)

    task = asyncio.create_task(worker())
    _jobs[key] = task
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if _jobs.get(key) is task:
            _jobs.pop(key, None)
        # Standing "allow all Bash" approvals last for one run only.
        bridge.clear_rules(run_id)
        _awaiting_text.pop(key, None)


# ── attachment handling ───────────────────────────────────────────────────


async def download_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Save a photo or document next to the chat id and return its absolute path."""
    message = update.effective_message
    if message is None:
        return None

    config.ensure_dirs()
    chat_dir = config.DOWNLOAD_DIR / str(update.effective_chat.id)
    chat_dir.mkdir(parents=True, exist_ok=True)

    if message.photo:
        photo = message.photo[-1]  # highest resolution
        target = chat_dir / f"{int(time.time())}_{photo.file_unique_id}.jpg"
        file = await context.bot.get_file(photo.file_id)
    elif message.document:
        document = message.document
        suffix = Path(document.file_name or "").suffix or ".bin"
        stem = Path(document.file_name or "file").stem[:60]
        target = chat_dir / f"{int(time.time())}_{stem}{suffix}"
        file = await context.bot.get_file(document.file_id)
    else:
        return None

    await file.download_to_drive(custom_path=target)
    log.info("saved attachment %s", target)
    return str(target)


def build_attachment_prompt(paths: list[str], caption: str) -> str:
    listing = "\n".join(f"- {path}" for path in paths)
    noun = "file" if len(paths) == 1 else "files"
    instruction = (
        f"The user attached {len(paths)} {noun} from Telegram. "
        "Open each one with the Read tool (it renders images) before answering.\n"
        f"{listing}"
    )
    caption = caption.strip()
    return f"{instruction}\n\n{caption}" if caption else instruction


async def flush_media_group(
    group_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await asyncio.sleep(MEDIA_GROUP_DEBOUNCE)
    async with _media_lock:
        group = _media_groups.pop(group_id, None)
    if not group or not group["paths"]:
        return
    await run_job(update, context, build_attachment_prompt(group["paths"], group["caption"]))


# ── approvals and questions ───────────────────────────────────────────────

_ask_options: dict[str, list[str]] = {}


def short_tool(tool: str) -> str:
    """`mcp__claude-in-chrome__computer` reads as noise on a phone; show
    `computer · claude-in-chrome` instead."""
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        if len(parts) >= 3:
            return f"{parts[-1]} · {parts[1]}"
    return tool


def _render_permission(request: dict) -> str:
    tool = str(request.get("tool_name") or "tool")
    summary = request.get("summary") or {}
    lines = [f"🔐 <b>Approval needed</b> — <b>{html.escape(short_tool(tool))}</b>"]

    if tool == "Bash":
        note = summary.get("note") or ""
        if note:
            lines.append(html.escape(tg_format.truncate(note, 160)))
        lines.append(f"<pre>{html.escape(str(summary.get('command') or '')[:1200])}</pre>")
    elif tool in {"Edit", "Write", "NotebookEdit"}:
        target = str(summary.get("file") or "")
        scope = " (all occurrences)" if summary.get("replace_all") else ""
        lines.append(f"<code>{html.escape(target)}</code>{scope}")
        preview = str(summary.get("preview") or "")
        if preview:
            lines.append(f"<pre>{html.escape(preview[:700])}</pre>")
    else:
        for key, value in list(summary.items())[:4]:
            lines.append(
                f"<b>{html.escape(str(key))}</b> "
                f"<code>{html.escape(tg_format.truncate(str(value), 200))}</code>"
            )

    return "\n".join(lines)


def _permission_keyboard(request_id: str, tool: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Allow", callback_data=f"p:{request_id}:a"),
                InlineKeyboardButton("⛔ Deny", callback_data=f"p:{request_id}:d"),
            ],
            [
                InlineKeyboardButton(
                    f"✅ Allow every {short_tool(tool)} in this run",
                    callback_data=f"p:{request_id}:A",
                )
            ],
        ]
    )


async def _present_request(app: Application, request: dict) -> None:
    """Turn one queued request into a Telegram message (with buttons, if it needs an answer)."""
    request_id = str(request.get("id") or "")
    kind = str(request.get("kind") or "")
    try:
        chat_id = int(request.get("chat_id") or 0)
    except ValueError:
        chat_id = 0
    if not request_id or not chat_id:
        return

    send_kwargs: dict = {"chat_id": chat_id, "parse_mode": ParseMode.HTML}
    thread_id = str(request.get("thread_id") or "")
    if thread_id.isdigit():
        send_kwargs["message_thread_id"] = int(thread_id)

    try:
        if kind == "notify":
            text = tg_format.truncate(str(request.get("text") or ""), 900)
            await app.bot.send_message(text=f"📣 {html.escape(text)}", **send_kwargs)
            bridge.discard(request_id)
            return

        if kind == "permission":
            tool = str(request.get("tool_name") or "tool")
            await app.bot.send_message(
                text=_render_permission(request),
                reply_markup=_permission_keyboard(request_id, tool),
                **send_kwargs,
            )
            return

        if kind == "ask":
            question = html.escape(str(request.get("question") or ""))
            options = [str(option) for option in (request.get("options") or [])]
            if options:
                _ask_options[request_id] = options
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                tg_format.truncate(option, 60), callback_data=f"c:{request_id}:{i}"
                            )
                        ]
                        for i, option in enumerate(options)
                    ]
                )
                await app.bot.send_message(
                    text=f"❓ {question}", reply_markup=keyboard, **send_kwargs
                )
            else:
                session_key_value = str(request.get("session_key") or f"{chat_id}:0")
                _awaiting_text[session_key_value] = request_id
                await app.bot.send_message(
                    text=f"❓ {question}\n\n<i>Reply with your answer.</i>", **send_kwargs
                )
            return

    except TelegramError as exc:
        # Never leave the hook blocked on a message that failed to send.
        log.error("could not present %s request: %s", kind, exc)
        bridge.respond(request_id, {"choice": "deny", "note": f"Could not reach Telegram: {exc}"})


async def approval_watcher(app: Application) -> None:
    """Poll the disk queue. Human-speed decisions do not need anything faster."""
    bridge.prune_stale()
    while True:
        try:
            now = time.time()
            for request_id, seen_at in list(_seen_requests.items()):
                if now - seen_at > 3600:
                    _seen_requests.pop(request_id, None)
                    _ask_options.pop(request_id, None)

            for request in bridge.pending():
                request_id = str(request.get("id") or "")
                if not request_id or request_id in _seen_requests:
                    continue
                _seen_requests[request_id] = now
                asyncio.create_task(_present_request(app, request))
        except Exception:  # noqa: BLE001 - the watcher must outlive any single bad request
            log.exception("approval watcher")
        await asyncio.sleep(0.4)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not is_authorized(update):
        await query.answer("Not authorized.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer()
        return
    prefix, request_id, choice = parts

    if prefix == "p":
        decision = {"a": "allow", "A": "allow_always", "d": "deny"}.get(choice, "deny")
        bridge.respond(request_id, {"choice": decision})
        label = {
            "allow": "✅ Allowed",
            "allow_always": "✅ Allowed for the rest of this run",
            "deny": "⛔ Denied",
        }[decision]
    elif prefix == "c":
        options = _ask_options.pop(request_id, [])
        try:
            answer = options[int(choice)]
        except (ValueError, IndexError):
            await query.answer("That option expired.", show_alert=True)
            return
        bridge.respond(request_id, {"answer": answer})
        label = f"✅ {tg_format.truncate(answer, 80)}"
    else:
        await query.answer()
        return

    await query.answer()
    try:
        original = query.message.text_html if query.message else ""
        await query.edit_message_text(
            f"{original}\n\n<b>{html.escape(label)}</b>", parse_mode=ParseMode.HTML
        )
    except TelegramError:
        pass


# ── handlers ──────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not is_authorized(update):
        await update.effective_message.reply_text(
            "⛔ Not authorized.\n\n"
            f"Your Telegram user id is <code>{user.id}</code> and this chat id is "
            f"<code>{chat.id}</code>.\n"
            "If you own this bot, add the user id to ALLOWED_USER_IDS in .env and restart.",
            parse_mode=ParseMode.HTML,
        )
        return
    await cmd_help(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    session = get_session(session_key(update))
    choices = ", ".join(sorted(config.workdir_choices())) or "—"
    await update.effective_message.reply_text(
        "<b>Claude Code ↔ Telegram</b>\n"
        "Just type to talk to Claude Code in this repo. Send a screenshot and it will read it.\n\n"
        "<b>Commands</b>\n"
        "/new — start a fresh conversation\n"
        "/status — what is running right now\n"
        "/stop — cancel the current run\n"
        "/cd &lt;name&gt; — switch repo\n"
        "/model &lt;name|clear&gt; — override the model\n"
        "/reviews [dry|force|quick|approve] — review the PRs waiting on you\n"
        "/review &lt;owner/repo&gt;#&lt;n&gt; — re-review one PR now, no questions asked\n"
        "/whoami — your ids\n\n"
        f"<b>Repo</b> <code>{html.escape(session.cwd)}</code>\n"
        f"<b>Available</b> {html.escape(choices)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    key = session_key(update)
    task = _jobs.get(key)
    if task and not task.done():
        task.cancel()
    session = reset_session(key)
    await update.effective_message.reply_text(
        f"🆕 New session in <code>{html.escape(Path(session.cwd).name)}</code>.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    key = session_key(update)
    session = get_session(key)
    task = _jobs.get(key)
    running = "yes" if task and not task.done() else "no"
    if config.APPROVALS_ENABLED:
        gate = f"ask me for {', '.join(short_tool(t) for t in config.effective_ask_tools())}"
    else:
        gate = f"unattended ({config.PERMISSION_MODE})"
    await update.effective_message.reply_text(
        f"<b>Running</b> {running}\n"
        f"<b>Approvals</b> {html.escape(gate)}\n"
        f"<b>Backend</b> {html.escape(config.AGENT_BACKEND)}\n"
        f"<b>Repo</b> <code>{html.escape(session.cwd)}</code>\n"
        f"<b>Model</b> {html.escape(session.model or 'default')}\n"
        f"<b>Turns</b> {session.turns} · <b>Spent</b> ${session.cost_usd:.3f}\n"
        f"<b>Session</b> <code>{session.session_id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    task = _jobs.get(session_key(update))
    if task and not task.done():
        task.cancel()
        await update.effective_message.reply_text("🛑 Cancelling…")
    else:
        await update.effective_message.reply_text("Nothing is running.")


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    choices = config.workdir_choices()
    wanted = " ".join(context.args or []).strip().lower()
    if not wanted:
        listing = "\n".join(f"• <code>{html.escape(name)}</code>" for name in sorted(choices))
        await update.effective_message.reply_text(
            f"Usage: <code>/cd &lt;name&gt;</code>\n\n{listing or 'No directories configured.'}",
            parse_mode=ParseMode.HTML,
        )
        return
    if wanted not in choices:
        await update.effective_message.reply_text(
            f"Unknown directory {wanted!r}. Allowed: {', '.join(sorted(choices)) or '—'}"
        )
        return

    key = session_key(update)
    session = reset_session(key)  # a different repo deserves a different session
    session.cwd = choices[wanted]
    store.save_sessions(_sessions)
    await update.effective_message.reply_text(
        f"📂 Switched to <code>{html.escape(session.cwd)}</code> (new session).",
        parse_mode=ParseMode.HTML,
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    session = get_session(session_key(update))
    wanted = " ".join(context.args or []).strip()
    if not wanted:
        await update.effective_message.reply_text(
            f"Model: <code>{html.escape(session.model or 'default')}</code>\n"
            "Set with <code>/model opus</code>, reset with <code>/model clear</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    session.model = "" if wanted.lower() in {"clear", "default", "reset"} else wanted
    store.save_sessions(_sessions)
    await update.effective_message.reply_text(f"Model set to {session.model or 'default'}.")


async def cmd_chrome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    if not config.ENABLE_CHROME:
        await update.effective_message.reply_text(
            "Chrome tools are off. Set CLAUDE_ENABLE_CHROME=1 in .env and restart, "
            "otherwise browsing falls back to a headless browser that cannot log in."
        )
        return

    if (context.args or [""])[0].lower() in {"forget", "reset", "clear"}:
        config.forget_device_id()
        await update.effective_message.reply_text(
            "🔁 Forgotten. You will be asked which browser on the next browser task."
        )
        return

    message = await update.effective_message.reply_text("🌐 Checking Chrome…")
    running, note = await chrome.ensure()

    device_id = config.effective_device_id()
    if device_id:
        pinned = "pinned in .env" if config.CHROME_DEVICE_ID else "chosen by you"
        browser = f"\n<b>Browser</b> <code>{html.escape(device_id)}</code> ({pinned})"
        browser += "\nSend <code>/chrome forget</code> to be asked again."
    else:
        browser = "\n<b>Browser</b> not chosen yet — you will be asked on the first browser task."

    await message.edit_text(
        f"{'✅' if running else '⚠️'} Chrome: {html.escape(note)}{browser}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drain the PR review queue. `/reviews dry` decides but posts nothing."""
    if not is_authorized(update):
        return

    args = [a.lower() for a in (context.args or [])]
    dry_run = "dry" in args
    force = "force" in args
    mode = next((a for a in args if a in ("quick", "deep", "approve")), "")

    if not config.REVIEW_REPOS:
        await update.effective_message.reply_text(
            "No repos configured. Set <code>REVIEW_REPOS</code> in .env, e.g.\n"
            "<code>REVIEW_REPOS=owner/repo-one owner/repo-two</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    effective = mode or config.REVIEW_MODE
    note = "reading each diff" if effective == "quick" else "approving without reading"
    message = await update.effective_message.reply_text(
        f"🔎 Sweeping {len(config.REVIEW_REPOS)} repo(s), {note}…"
        + (" (dry run)" if dry_run else ""),
    )

    try:
        outcomes = await pr_review.sweep(mode=mode, dry_run=dry_run, force=force)
    except pr_review.ReviewError as exc:
        await message.edit_text(f"⚠️ {exc}")
        return

    with contextlib.suppress(TelegramError):
        await message.delete()
    await send_markdown(update, pr_review.summarize(outcomes, dry_run=dry_run))


_REVIEW_TARGET = re.compile(r"(?:(?P<repo>[\w.-]+/[\w.-]+)[#\s]+)?#?(?P<number>\d+)$")

# Plain text that is really a review wish: "re review o/r#123", "review #2307 dry".
# Narrow on purpose — the message must be nothing but the wish plus optional
# flags; anything chattier ("review the auth flow please") still goes to the
# agent.
_REVIEW_MSG = re.compile(
    r"^(?:please\s+|pls\s+)?(?:re[\s-]?)?review\s+(?P<rest>.+)$", re.I | re.S
)


def _parse_review_target(rest: str) -> tuple[str, int, str, bool] | None:
    """'owner/repo#123 [dry|quick|approve]' in any spacing — None if it is not one."""
    words = rest.split()
    dry_run = any(w.lower() == "dry" for w in words)
    mode = next((w.lower() for w in words if w.lower() in ("quick", "deep", "approve")), "")
    target = " ".join(w for w in words if w.lower() not in ("dry", "quick", "deep", "approve"))
    m = _REVIEW_TARGET.fullmatch(target)
    if not m or not m.group("number"):
        return None
    return m.group("repo") or "", int(m.group("number")), mode, dry_run


async def _resolve_review_repo(update: Update, repo: str, number: int) -> str:
    """Fill in the repo when the message named only a PR number."""
    if repo:
        return repo
    if len(config.REVIEW_REPOS) == 1:
        return config.REVIEW_REPOS[0]
    await update.effective_message.reply_text(
        f"Which repo? REVIEW_REPOS lists {len(config.REVIEW_REPOS)}. "
        f"Use <code>/review owner/repo#{number}</code>.",
        parse_mode=ParseMode.HTML,
    )
    return ""


async def _run_single_review(
    update: Update, repo: str, number: int, mode: str, dry_run: bool
) -> None:
    message = await update.effective_message.reply_text(
        f"🔎 Reviewing <code>{html.escape(repo)}#{number}</code>…",
        parse_mode=ParseMode.HTML,
    )
    try:
        outcome = await pr_review.review_now(repo, number, mode=mode, dry_run=dry_run)
    except pr_review.ReviewError as exc:
        await message.edit_text(f"⚠️ {html.escape(str(exc))}")
        return

    with contextlib.suppress(TelegramError):
        await message.delete()
    await send_markdown(update, pr_review.summarize([outcome], dry_run=dry_run))


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-review one PR right now: /review owner/repo#123 [dry|quick|approve].

    This is the automation path — it never asks for tool approvals, unlike
    phrasing the same wish as a plain message, which the chat agent picks up
    and Bash-approvals all over.
    """
    if not is_authorized(update):
        return
    parsed = _parse_review_target(" ".join(context.args or []))
    if parsed is None:
        await update.effective_message.reply_text(
            "Usage: <code>/review owner/repo#123</code> [dry|quick|approve]",
            parse_mode=ParseMode.HTML,
        )
        return
    repo, number, mode, dry_run = parsed
    repo = await _resolve_review_repo(update, repo, number)
    if not repo:
        return
    await _run_single_review(update, repo, number, mode, dry_run)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"user id <code>{user.id}</code>\nchat id <code>{chat.id}</code>\n"
        f"authorized: {'yes' if is_authorized(update) else 'no'}",
        parse_mode=ParseMode.HTML,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return

    # A run is blocked on mcp__tg__ask_user: this message is the answer, not a
    # new instruction.
    pending_ask = _awaiting_text.pop(session_key(update), "")
    if pending_ask:
        bridge.respond(pending_ask, {"answer": text})
        await message.reply_text("✅ Sent to Claude.")
        return

    # "re review owner/repo#123" typed as plain text is still a review command:
    # route it to the automation instead of waking the agent for a round of
    # Bash approvals.
    review_wish = _REVIEW_MSG.match(text)
    if review_wish:
        parsed = _parse_review_target(review_wish.group("rest"))
        if parsed:
            repo, number, mode, dry_run = parsed
            repo = await _resolve_review_repo(update, repo, number)
            if repo:
                await _run_single_review(update, repo, number, mode, dry_run)
            return

    store.append_inbox(
        {
            "chat_id": update.effective_chat.id,
            "user_id": update.effective_user.id,
            "username": update.effective_user.username or "",
            "text": text,
        }
    )

    quoted = ""
    reply_to = message.reply_to_message
    if reply_to is not None and (reply_to.text or reply_to.caption):
        quoted = (reply_to.text or reply_to.caption or "").strip()
        if len(quoted) > 1500:
            quoted = quoted[:1500] + "…"
        quoted = f"[Replying to an earlier message]\n{quoted}\n\n"

    await run_job(update, context, f"{quoted}{text}")


async def on_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    message = update.effective_message
    caption = (message.caption or "").strip()

    try:
        path = await download_attachment(update, context)
    except TelegramError as exc:
        await message.reply_text(
            f"Could not download that file: {exc}\n"
            "(The Telegram Bot API caps downloads at 20 MB.)"
        )
        return
    if path is None:
        return

    store.append_inbox(
        {
            "chat_id": update.effective_chat.id,
            "user_id": update.effective_user.id,
            "username": update.effective_user.username or "",
            "text": caption,
            "file": path,
        }
    )

    group_id = message.media_group_id
    if not group_id:
        await run_job(update, context, build_attachment_prompt([path], caption))
        return

    # Albums arrive as separate updates; collect them before starting a run.
    async with _media_lock:
        group = _media_groups.get(group_id)
        if group is None:
            group = {"paths": [], "caption": "", "task": None}
            _media_groups[group_id] = group
        group["paths"].append(path)
        if caption:
            group["caption"] = caption
        if group["task"]:
            group["task"].cancel()
        group["task"] = asyncio.create_task(flush_media_group(group_id, update, context))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error", exc_info=context.error)


# ── startup ───────────────────────────────────────────────────────────────


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("new", "Start a fresh conversation"),
            BotCommand("status", "What is running right now"),
            BotCommand("stop", "Cancel the current run"),
            BotCommand("cd", "Switch repository"),
            BotCommand("model", "Override the model"),
            BotCommand("review", "Re-review one PR now"),
            BotCommand("chrome", "Open or check the browser"),
            BotCommand("reviews", "Review the PRs waiting on you"),
            BotCommand("whoami", "Show your Telegram ids"),
            BotCommand("help", "Show help"),
        ]
    )
    me = await app.bot.get_me()
    log.info("connected as @%s", me.username)

    # Tell the owner the bot came (back) up - a silent restart after a crash
    # or a deploy is otherwise invisible until something breaks.
    if config.DEFAULT_CHAT_ID:
        with contextlib.suppress(TelegramError):
            await app.bot.send_message(
                chat_id=config.DEFAULT_CHAT_ID,
                text=(
                    f"🟢 Bot back online as @{me.username}"
                    f" (mode: {config.AGENT_BACKEND}, reviews: {config.REVIEW_MODE})."
                ),
            )

    # Plain asyncio, not Application.create_task: post_init runs before the
    # application is started, and PTB will not adopt a task created that early.
    # We own its lifetime instead and cancel it in post_stop.
    global _watcher_task, _review_task
    _watcher_task = asyncio.create_task(approval_watcher(app))

    if config.REVIEW_WATCH and config.REVIEW_REPOS and config.DEFAULT_CHAT_ID:

        async def report(text: str) -> None:
            for chunk in tg_format.to_html_chunks(text):
                with contextlib.suppress(TelegramError):
                    await app.bot.send_message(
                        chat_id=config.DEFAULT_CHAT_ID,
                        text=chunk,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )

        _review_task = asyncio.create_task(pr_review.watch(report))
        log.info("review sweep every %ss in %s", config.REVIEW_POLL_SECONDS, config.REVIEW_REPOS)


def deny_pending() -> int:
    """Refuse anything still waiting for an answer.

    A hook blocked on approval would otherwise sit out the full
    APPROVAL_WAIT_SECONDS after the bot is already gone.
    """
    refused = 0
    for request in bridge.pending():
        bridge.respond(
            str(request.get("id")),
            {"choice": "deny", "note": "The Telegram bot shut down before you answered."},
        )
        refused += 1
    return refused


async def post_stop(app: Application) -> None:
    global _watcher_task, _review_task
    for task in (_watcher_task, _review_task):
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    _watcher_task = None
    _review_task = None
    deny_pending()
    agent_runner.shutdown()


def main() -> None:
    config.enable_utf8_console()
    config.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_DIR / "bot.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    problems = config.missing_settings()
    if problems:
        for problem in problems:
            log.error(problem)
        raise SystemExit(
            "\nFix the settings above in .env (copy .env.example if you have not yet), "
            "then run again."
        )

    _sessions.update(store.load_sessions())

    global _run_settings, _run_mcp
    _run_settings, _run_mcp = config.write_run_configs()

    log.info(
        "backend=%s workdir=%s add_dirs=%s users=%s",
        config.AGENT_BACKEND,
        config.CLAUDE_WORKDIR,
        config.CLAUDE_ADD_DIRS,
        sorted(config.ALLOWED_USER_IDS),
    )
    if config.AGENT_BACKEND == "opencode":
        log.info(
            "opencode2: bin=%s port=%s model=%s",
            config.OPENCODE_BIN,
            config.OPENCODE_PORT,
            config.OPENCODE_MODEL or "default",
        )
    if config.ENABLE_CHROME:
        binary = chrome.find_binary()
        log.info(
            "chrome ON — autostart=%s binary=%s",
            config.CHROME_AUTOSTART,
            binary or "NOT FOUND (set CLAUDE_CHROME_BINARY)",
        )
    if config.APPROVALS_ENABLED:
        log.info(
            "approvals ON — asking on Telegram for: %s (auto-allowed: %s)",
            ", ".join(config.effective_ask_tools()),
            ", ".join(sorted(config.AUTO_ALLOW_TOOLS)),
        )
    else:
        log.warning(
            "approvals OFF — permission_mode=%s, tools run unattended", config.PERMISSION_MODE
        )

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)  # so /stop is answerable while a run is in flight
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("chrome", cmd_chrome))
    app.add_handler(CommandHandler("reviews", cmd_reviews))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_attachment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except KeyboardInterrupt:
        # PTB catches the first Ctrl+C, but a second one — or one that lands while
        # its shutdown is already running — escapes from inside PTB's own finally
        # block. That is an ordinary stop, not a crash; do not print a traceback.
        log.info("interrupted during shutdown")
    finally:
        # post_stop may not have run if the interrupt arrived mid-shutdown.
        refused = deny_pending()
        if refused:
            log.info("refused %d approval request(s) left waiting", refused)
        log.info("stopped")


if __name__ == "__main__":
    main()
