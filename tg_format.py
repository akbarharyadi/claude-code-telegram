"""Render Claude's markdown into Telegram-safe HTML chunks.

Telegram accepts a small HTML subset and rejects the whole message if a tag is
unbalanced, so the conversion is deliberately conservative: escape everything,
then re-introduce only the handful of tags Telegram documents. Chunking splits
on line boundaries, and no tag this module emits ever spans a newline — that is
what makes splitting safe without tracking open tags across chunks.
"""

from __future__ import annotations

import re

# Telegram's hard limit is 4096 UTF-16 code units; leave room for a chunk marker.
MAX_MESSAGE = 3900
MAX_CAPTION = 1000

_SENTINEL = "\x00"

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_ALT = re.compile(r"__([^_\n]+)__")
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_STRIKE = re.compile(r"~~([^~\n]+)~~")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_HRULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    """Convert inline markdown. Code and links are stashed so their contents
    are never re-processed as markdown (a URL full of underscores, say)."""
    slots: list[str] = []

    def stash(html: str) -> str:
        slots.append(html)
        return f"{_SENTINEL}{len(slots) - 1}{_SENTINEL}"

    text = _INLINE_CODE.sub(lambda m: stash(f"<code>{escape(m.group(1))}</code>"), text)
    text = _LINK.sub(
        lambda m: stash(f'<a href="{escape(m.group(2))}">{escape(m.group(1))}</a>'), text
    )
    text = escape(text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD_ALT.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1)}</i>", text)

    return re.sub(
        rf"{_SENTINEL}(\d+){_SENTINEL}", lambda m: slots[int(m.group(1))], text
    )


def _line_html(line: str) -> str:
    if _HRULE.match(line):
        return "──────────"

    heading = _HEADING.match(line)
    if heading:
        return f"<b>{_inline(heading.group(2))}</b>" if heading.group(2) else ""

    bullet = _BULLET.match(line)
    if bullet:
        indent, body = bullet.group(1), bullet.group(2)
        return f"{indent}• {_inline(body)}"

    return _inline(line)


def _pre_html(body: str, lang: str) -> str:
    opener = f'<pre><code class="language-{escape(lang)}">' if lang else "<pre>"
    closer = "</code></pre>" if lang else "</pre>"
    return f"{opener}{escape(body)}{closer}"


def _split_pre(body: str, lang: str, limit: int) -> list[str]:
    """Break one oversized code block into several that each fit a message."""
    blocks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            blocks.append(_pre_html("\n".join(current), lang))
            current.clear()

    for line in body.split("\n"):
        # A single line longer than the limit still has to be cut somewhere.
        pieces = [line[i : i + limit // 2] for i in range(0, len(line), limit // 2)] or [""]
        for piece in pieces:
            candidate = current + [piece]
            if len(_pre_html("\n".join(candidate), lang)) > limit and current:
                flush()
            current.append(piece)
    flush()
    return blocks or [_pre_html("", lang)]


def _hard_split(text: str, limit: int) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [text]


def to_html_chunks(markdown: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Markdown in, a list of ready-to-send `parse_mode="HTML"` strings out."""
    if not markdown or not markdown.strip():
        return []

    lines = markdown.replace("\r\n", "\n").split("\n")
    units: list[tuple[str, str, str]] = []  # (kind, payload, lang)

    index = 0
    while index < len(lines):
        stripped = lines[index].lstrip()
        if stripped.startswith("```"):
            lang = stripped[3:].strip().split()[0] if stripped[3:].strip() else ""
            index += 1
            body: list[str] = []
            while index < len(lines) and not lines[index].lstrip().startswith("```"):
                body.append(lines[index])
                index += 1
            index += 1  # consume the closing fence (absent at EOF is fine)
            units.append(("pre", "\n".join(body), lang))
            continue
        units.append(("line", _line_html(lines[index]), ""))
        index += 1

    chunks: list[str] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            chunks.append(buffer.rstrip("\n"))
        buffer = ""

    for kind, payload, lang in units:
        if kind == "line":
            blocks = [payload + "\n"]
        else:
            blocks = [block + "\n" for block in _split_pre(payload, lang, limit)]
        for block in blocks:
            if len(block) > limit:
                flush()
                chunks.extend(_hard_split(block.rstrip("\n"), limit))
                continue
            if len(buffer) + len(block) > limit:
                flush()
            buffer += block

    flush()
    return chunks


def to_plain_chunks(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Fallback when Telegram rejects our HTML: never fails, never formats."""
    chunks: list[str] = []
    buffer = ""
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        for piece in _hard_split(line, limit) if len(line) > limit else [line]:
            candidate = f"{buffer}{piece}\n"
            if len(candidate) > limit and buffer:
                chunks.append(buffer.rstrip("\n"))
                buffer = f"{piece}\n"
            else:
                buffer = candidate
    if buffer.strip():
        chunks.append(buffer.rstrip("\n"))
    return chunks


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
