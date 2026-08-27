# TODO

Two lists: what **you** do to get running, and what **this project** still needs.

---

## Your first ten minutes

- [ ] **1. Create the bot.** Message [@BotFather](https://t.me/BotFather) → `/newbot` → choose a name and a username ending in `bot`. Copy the token.
- [ ] **2. Configure.** `cp .env.example .env`, then set `TELEGRAM_BOT_TOKEN` and `CLAUDE_WORKDIR` (the repo Claude should work in). Everything else has a sane default.
- [ ] **3. Claim the bot.** `uv run whoami.py`, then message your bot. It prints your ids and offers to write them into `.env`. Say yes.
- [ ] **4. Check the install.** `uv run doctor.py`. It runs a real Claude Code call and proves the approval gate blocks a denied command. Do not skip this — it is the only thing that tells you the gate is actually closed.
- [ ] **5. Start it.** `uv run bot.py`. Leave the terminal open.
- [ ] **6. Smoke-test all four paths** from your phone:
  - [ ] A read-only question (`how many routers are in this repo?`) → answers with no prompt.
  - [ ] A gated command (`run git status`) → shows a 🔐 card. **Tap Deny first** and confirm Claude stops instead of retrying.
  - [ ] A screenshot with a caption → confirms image download and reading.
  - [ ] A question back to you (`ask me which approach you should take`) → renders as buttons.
- [ ] **7. Decide your denylist.** If the bot should never push under your git identity, this is worth more than your own vigilance:
      `CLAUDE_DISALLOWED_TOOLS=Bash(git push:*),Bash(gh pr create:*),Bash(gh pr comment:*)`
- [ ] **8. Optional — reach yourself from the desktop.**
      `claude mcp add telegram -s user -- uv run --directory /abs/path/to/this/repo mcp_server.py`

### Two rules that will bite you otherwise

- **Never run two pollers on one token.** `bot.py` twice, or `whoami.py` while `bot.py` is up, gives `Conflict: terminated by other getUpdates request`. Stop one.
- **Approvals gate your attention, not other people's access.** Everyone on `ALLOWED_USER_IDS` can approve. Keep that list to yourself.

---

## Known gaps

Honest status. Contributions very welcome — open an issue first if it is a big one.

### Not yet verified
- [x] ~~**Windows, end to end.**~~ Verified 2026-08-27: approval buttons, callbacks, screenshot upload and multi-turn sessions all exercised against a real bot.
- [ ] **macOS and Linux.** Developed and exercised on Windows only. CI runs the unit tests on Ubuntu, but nobody has driven the bot end to end there.
- [ ] **Group chats and forum topics.** The code keys a session per `chat_id:thread_id` and should work, but it has only been used one-to-one.
- [ ] **Long albums** (10+ images at once) against the debounce window.

### Rough edges
- [ ] **Sessions are per chat, not per topic in the UI.** `/status` shows one session; there is no way to list or switch between several.
- [ ] **`allow all` is per run.** Deliberate, but a long refactor means tapping again on the next message. A `/trust <tool> <minutes>` command would help without becoming a standing grant.
- [ ] **No output streaming.** You see tool-by-tool progress, then the whole answer at the end. `--include-partial-messages` could stream the text as it is written.
- [ ] **Cancelling mid-approval** leaves the 🔐 card sitting there with live buttons. Harmless — the request is refused on shutdown — but it looks stale.
- [ ] **`state/inbox.jsonl` grows forever.** Needs rotation.

### Ideas
- [ ] `/diff` — show the working tree diff as a syntax-highlighted image or a file.
- [ ] Voice notes → transcription → prompt. Dictating a bug report while walking is the obvious use.
- [ ] A `Dockerfile`, so the blast radius of "arbitrary shell" is a container rather than your laptop.
- [ ] Approve from a desktop Claude Code session too, not only from the phone.
- [ ] Per-tool approval policy in `.env` (`Bash(git *)` auto, everything else ask).

---

## Contributing

```bash
uv sync --group dev
uv run ruff check .
uv run pytest -q
```

CI runs both on Linux and Windows for Python 3.11 and 3.12.

The two things worth being careful about:

- **The approval path must fail closed.** Claude Code treats a non-zero hook exit as a *non-blocking error* and runs the tool anyway, so `hook_permission.py` catches everything and still exits 0 with a deny. `tests/test_bridge.py` pins that, and the exact `hookSpecificOutput` key names. If you change the hook, keep those tests honest.
- **Telegram rejects a whole message if one tag is unbalanced.** `tg_format.py` escapes everything and re-introduces only the documented subset, and never emits a tag spanning a newline — that is what makes chunking safe. `tests/test_tg_format.py` asserts balance on every chunk.
