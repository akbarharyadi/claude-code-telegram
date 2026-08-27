# claude-code-telegram

Talk to [Claude Code](https://claude.com/claude-code) from Telegram. Send a message from your phone, Claude works in your repo on your machine, and the answer comes back in the chat. Send a screenshot and it reads it.

It bridges both directions:

```
  Telegram  ──▶  bot.py  ──▶  claude --print   ── works in your repo
     ▲                                          │
     └────────  formatted reply, live progress ─┘

  Claude Code session  ──▶  mcp_server.py  ──▶  Telegram
     "build finished"  ·  a screenshot  ·  "approve this?" and waits for you
```

- **`bot.py`** — a Telegram bot that runs Claude Code for you. Threads, screenshots, albums, live tool-by-tool progress, `/stop` to cancel.
- **`mcp_server.py`** — an MCP server so a Claude Code session on your desktop can message you, send you an image or a file, or *ask you a question and wait for the answer*.

Nothing that changes your machine runs unattended. Before any `Bash`, `Edit` or `Write`, the bot shows you the exact command or file and waits for you to tap **Allow** or **Deny** — and when Claude hits a real choice, it asks you instead of guessing:

```
🔐 Approval needed — Bash            ❓ Two ways to fix this. Which?
   uv run alembic upgrade head
   [ ✅ Allow ] [ ⛔ Deny ]             [ Patch the view          ]
   [ ✅ Allow every Bash in this run ]  [ Change the caller       ]
```

Use either half on its own. They share one `.env`.

New here? [**TODO.md**](TODO.md) is a ten-minute setup checklist, plus an honest list of what is not verified yet.

---

## ⚠️ Read this before you install

**This gives whoever can message your bot the ability to run code on your machine.** That is the entire point, and it is also the entire risk.

- `ALLOWED_USER_IDS` is mandatory. The bot refuses to start without it and ignores everyone not on the list.
- A leaked `TELEGRAM_BOT_TOKEN` is a shell on your machine. Treat it like an SSH key. `.env` is gitignored — keep it that way.
- Approvals are **on** by default, so nothing destructive runs without your tap. They fail closed: a timeout, an unreachable Telegram, or a crashing hook all result in **deny**, never in a silent allow.
- Approvals gate *your* attention, not other people's access. Anyone on `ALLOWED_USER_IDS` can approve — keep that list to yourself.
- If you do not want the bot pushing under your git identity at all, put it out of reach rather than relying on your own vigilance:
  ```
  CLAUDE_DISALLOWED_TOOLS=Bash(git push:*),Bash(gh pr create:*),Bash(gh pr comment:*)
  ```
- For a bot that answers questions but changes nothing, set `TELEGRAM_APPROVALS=0` and `CLAUDE_PERMISSION_MODE=plan`.

Verify the gate on your own machine before trusting it — `uv run doctor.py` asks Claude to create a file via `Bash`, denies it, and checks the file really was not created, then repeats with allow.

Messages you send pass through Telegram's servers. Do not paste secrets into the chat.

---

## Requirements

- Python 3.11+
- [Claude Code](https://claude.com/claude-code) installed and already logged in (`claude` runs from your terminal)
- [uv](https://docs.astral.sh/uv/) — or plain `pip`, see below
- A Telegram account

Works on Windows, macOS and Linux.

---

## Setup

**1. Create the bot**

Message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name and username. Copy the token it gives you.

**2. Configure**

```bash
git clone https://github.com/akbarharyadi/claude-code-telegram.git
cd claude-code-telegram
cp .env.example .env
```

Edit `.env` and set at minimum `TELEGRAM_BOT_TOKEN` and `CLAUDE_WORKDIR`.

**3. Find your user id**

```bash
uv run whoami.py
```

It tells you to message the bot, catches that message, prints your user and chat ids, and offers to write them into `.env`. No bot has to be running — which matters, because the bot deliberately refuses to start while `ALLOWED_USER_IDS` is empty.

**4. Talk to it**

Send any message. Send a screenshot with a caption like "why does this button overlap?". Send several images at once — the bot waits for the whole album before starting.

**5. (Optional) Register the MCP server**

```bash
claude mcp add telegram -s user -- uv run --directory /absolute/path/to/claude-code-telegram mcp_server.py
```

Now any Claude Code session can reach you on Telegram.

<details>
<summary>Without uv</summary>

```bash
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
.venv/bin/python bot.py
```
</details>

---

## Bot commands

| Command | What it does |
|---|---|
| *(any message)* | Ask Claude Code. The conversation continues across messages. |
| *(photo / file)* | Downloaded locally and handed to Claude to read. The caption is your prompt. |
| `/new` | Start a fresh conversation, forgetting the current context. |
| `/status` | Whether a run is in flight, plus repo, model, turns and spend. |
| `/stop` | Cancel the running job. |
| `/cd <name>` | Switch repository (from `CLAUDE_EXTRA_WORKDIRS`). Starts a new session. |
| `/model <name>` | Override the model for this chat, e.g. `/model opus`. `/model clear` resets. |
| `/whoami` | Your user id and chat id. |

Each chat keeps its own session. In a forum-style group, each topic gets its own session too. Sessions survive a bot restart.

---

## MCP tools

| Tool | Purpose |
|---|---|
| `telegram_send_message(text)` | Notify you. Markdown becomes Telegram formatting; long text is split. |
| `telegram_send_photo(path, caption)` | Send an image inline — a screenshot, a chart, a rendered diff. |
| `telegram_send_document(path, caption)` | Send any file up to 50 MB — logs, PDFs, reports. |
| `telegram_read_recent(limit)` | Read the latest messages the bot received. |
| `telegram_ask(question, timeout_seconds)` | Ask, then **block until you reply**. Approve a deploy from your phone. |

`telegram_read_recent` and `telegram_ask` need `bot.py` running: only one process may poll Telegram, so the bot records incoming messages to `state/inbox.jsonl` and the MCP server reads from there.

By default every tool is pinned to `TELEGRAM_DEFAULT_CHAT_ID`. Set `MCP_ALLOW_ANY_CHAT=1` to let Claude address other chats.

---

## Approvals and questions

Two separate mechanisms, both answered with a tap:

**Approvals** — a `PreToolUse` hook gates every tool in `CLAUDE_ASK_TOOLS` (`Bash`, `Edit`, `Write`, `NotebookEdit` by default). You see the exact shell command, or the file path plus a preview of what is about to be written, and choose Allow, Deny, or *Allow every `Bash` in this run* — which lasts for that one message and is forgotten when the run ends. Read-only tools in `CLAUDE_AUTO_ALLOW_TOOLS` never interrupt you.

**Questions** — Claude gets two extra tools inside bot-started runs:

| Tool | Purpose |
|---|---|
| `mcp__tg__ask_user(question, options)` | Ask you something and block. With options it renders as buttons; without, it waits for you to type. |
| `mcp__tg__notify(text)` | A one-line status note during a long run. Does not wait. |

The system prompt tells Claude to reach for `ask_user` rather than guess whenever a choice would change what it builds.

Every path fails closed. If Telegram is unreachable, if you never answer, or if the hook itself crashes, the answer is **deny** — and Claude is told why, so it explains and stops rather than retrying.

To turn all of this off and let runs proceed unattended, set `TELEGRAM_APPROVALS=0`.

---

## Browsing a page that needs a login

By default the Claude in Chrome tools are **not present in a bot run at all** — the CLI only exposes them with `--chrome`. Without them, asking for a screenshot makes Claude fall back to a headless browser, whose cookie jar is empty, so anything behind a login is out of reach.

Set `CLAUDE_ENABLE_CHROME=1` and the bot passes `--chrome`, handing Claude the browser you are already signed into. You also need the [Claude in Chrome](https://claude.com/chrome) extension installed, with the site permitted in it.

These tools act as you on every site you are signed into, so they are gated by the approval hook like `Bash` is. If the prompts get tiring, add the whole server to the auto-allow list:

```
CLAUDE_AUTO_ALLOW_TOOLS=Read,Grep,Glob,TodoWrite,Task,WebSearch,WebFetch,NotebookRead,BashOutput,KillShell,mcp__claude-in-chrome
```

---

## Configuration

Everything lives in `.env`; see [`.env.example`](.env.example) for the annotated list. The ones that matter most:

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | From @BotFather. Required. |
| `ALLOWED_USER_IDS` | — | Who may use the bot. Required; empty means the bot will not start. |
| `CLAUDE_WORKDIR` | — | The repo Claude works in. Required. |
| `CLAUDE_ADD_DIRS` | — | Extra directories Claude may touch (sibling repos). |
| `CLAUDE_EXTRA_WORKDIRS` | — | Targets for `/cd`. |
| `TELEGRAM_APPROVALS` | `1` | Ask before anything that changes state. `0` runs unattended. |
| `CLAUDE_ASK_TOOLS` | `Bash,Edit,Write,NotebookEdit` | Tools that need your tap. |
| `CLAUDE_AUTO_ALLOW_TOOLS` | read-only set | Tools that never interrupt you. |
| `APPROVAL_WAIT_SECONDS` | `540` | How long a question waits. Timeout means deny. |
| `CLAUDE_PERMISSION_MODE` | `acceptEdits` | Unattended mode only: `acceptEdits`, `bypassPermissions`, `plan`. |
| `CLAUDE_ALLOWED_TOOLS` | broad | Unattended mode only. Comma-separated allowlist. |
| `CLAUDE_DISALLOWED_TOOLS` | — | Denylist, applied in both modes. Wins over the allowlist. |
| `CLAUDE_MAX_BUDGET_USD` | `0` | Per-run spend cap. `0` disables it. |
| `CLAUDE_RUN_TIMEOUT_SECONDS` | `1800` | Kill a run that overruns. |
| `CLAUDE_CONTEXT_NOTE` | — | Appended to the system prompt. Describe your repos here. |

---

## How it works

Each incoming message becomes one `claude --print --output-format stream-json` subprocess. Streaming JSON is what makes the progress message tick over tool by tool instead of leaving you staring at a silent chat for five minutes.

Session continuity uses `--session-id` on the first run of a chat and `--resume` afterwards, with the mapping persisted to `state/sessions.json`. Attachments are downloaded into `state/downloads/<chat_id>/` and that directory is passed with `--add-dir`, so Claude can read them without widening access to anything else.

Replies are converted from markdown to the small HTML subset Telegram accepts, and split on line boundaries so a code block never lands half-open. If Telegram rejects the formatting anyway, the bot resends as plain text rather than dropping your answer.

Approvals travel over a small file queue in `state/approvals/` rather than a socket. The hook and the ask-server are grandchildren of the bot — spawned by `claude`, which the bot spawned — and only one process may hold Telegram's `getUpdates` poll, so they cannot talk to Telegram themselves. They drop a JSON request and block; the bot presents it, and writes the answer back. The handshake is plain files on purpose: you can watch it with `ls`, and unstick a wedged run by deleting one.

The hook is registered with `--settings`, generated at startup into `state/run-settings.json` so the absolute path to your Python interpreter is baked in. It uses exec form (`command` + `args`) rather than a shell string, which is what keeps a Windows path containing spaces from being mis-split.

---

## Troubleshooting

**Bot exits with a list of settings** — that is `.env` validation. Fix what it names and rerun.

**"Claude Code CLI not found"** — set `CLAUDE_BIN` to the absolute path (`where claude` on Windows, `which claude` elsewhere).

**Claude says it lacks permission to run something** — headless mode cannot prompt you. Add the tool to `CLAUDE_ALLOWED_TOOLS`, or switch `CLAUDE_PERMISSION_MODE` to `bypassPermissions`.

**"Conflict: terminated by other getUpdates request"** — the bot is already running somewhere else. Only one instance per token.

**Downloads fail on large files** — the Telegram Bot API caps *downloads* at 20 MB. Uploads from the MCP side go to 50 MB.

**Nothing happens when you message it** — check `ALLOWED_USER_IDS` contains your id. Unauthorized messages are ignored silently by design; `/whoami` always answers.

---

## License

MIT — see [LICENSE](LICENSE).
