"""Telegram bot that runs Claude Code on your machine and reports back.

Send a message, get an answer. Send a screenshot, Claude reads it. Reply in the
same chat and the conversation continues in the same Claude Code session.

Run with:  uv run bot.py
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
import uuid
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import claude_runner
import config
import store
import tg_format
from claude_runner import RunSpec

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


def get_session(key: str) -> store.Session:
    session = _sessions.get(key)
    if session is None:
        session = store.Session(
            session_id=str(uuid.uuid4()),
            cwd=config.CLAUDE_WORKDIR,
            add_dirs=list(config.CLAUDE_ADD_DIRS),
            model=config.CLAUDE_MODEL,
        )
        _sessions[key] = session
        store.save_sessions(_sessions)
    return session


def reset_session(key: str) -> store.Session:
    previous = _sessions.get(key)
    session = store.Session(
        session_id=str(uuid.uuid4()),
        cwd=previous.cwd if previous else config.CLAUDE_WORKDIR,
        add_dirs=list(previous.add_dirs) if previous else list(config.CLAUDE_ADD_DIRS),
        model=previous.model if previous else config.CLAUDE_MODEL,
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
    add_dirs = [*session.add_dirs, str(config.DOWNLOAD_DIR)]
    target = claude_runner.describe_target(session.cwd, session.add_dirs)
    header = f"🤖 <b>{html.escape(target)}</b>"

    placeholder = await message.reply_text(f"{header}\n<i>thinking…</i>", parse_mode=ParseMode.HTML)
    progress = Progress(placeholder, header)

    spec = RunSpec(
        prompt=prompt,
        cwd=session.cwd,
        session_id=session.session_id,
        resume=session.started,
        add_dirs=add_dirs,
        model=session.model,
        effort=config.CLAUDE_EFFORT,
        permission_mode=config.PERMISSION_MODE,
        allowed_tools=config.ALLOWED_TOOLS,
        disallowed_tools=config.DISALLOWED_TOOLS,
        append_system_prompt=config.APPEND_SYSTEM_PROMPT,
        max_budget_usd=config.MAX_BUDGET_USD,
        timeout_seconds=config.RUN_TIMEOUT_SECONDS,
    )

    async def worker() -> None:
        final_text = ""
        streamed: list[str] = []
        error_text = ""
        cost = 0.0
        typing_at = 0.0

        try:
            async for event in claude_runner.stream_run(spec):
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
    await update.effective_message.reply_text(
        f"<b>Running</b> {running}\n"
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
            BotCommand("whoami", "Show your Telegram ids"),
            BotCommand("help", "Show help"),
        ]
    )
    me = await app.bot.get_me()
    log.info("connected as @%s", me.username)


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
    log.info(
        "workdir=%s add_dirs=%s permission_mode=%s users=%s",
        config.CLAUDE_WORKDIR,
        config.CLAUDE_ADD_DIRS,
        config.PERMISSION_MODE,
        sorted(config.ALLOWED_USER_IDS),
    )

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)  # so /stop is answerable while a run is in flight
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_attachment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
