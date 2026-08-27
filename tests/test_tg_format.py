"""The formatter is the one piece that can silently corrupt an answer, so it
carries the tests: Telegram rejects a whole message if a tag is unbalanced."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tg_format  # noqa: E402

_TAG = re.compile(r"</?([a-z]+)")


def assert_balanced(html: str) -> None:
    stack: list[str] = []
    for match in re.finditer(r"<(/?)([a-z]+)[^>]*>", html):
        closing, name = match.group(1), match.group(2)
        if closing:
            assert stack and stack[-1] == name, f"unbalanced </{name}> in {html!r}"
            stack.pop()
        else:
            stack.append(name)
    assert not stack, f"unclosed {stack} in {html!r}"


def test_plain_text_is_escaped():
    (chunk,) = tg_format.to_html_chunks("a < b & c > d")
    assert chunk == "a &lt; b &amp; c &gt; d"


def test_inline_code_escapes_its_contents():
    (chunk,) = tg_format.to_html_chunks("use `List<int>` here")
    assert "<code>List&lt;int&gt;</code>" in chunk
    assert_balanced(chunk)


def test_fenced_block_becomes_pre_with_language():
    (chunk,) = tg_format.to_html_chunks("```python\nprint('<hi>')\n```")
    assert '<pre><code class="language-python">' in chunk
    assert "print(&#x27;&lt;hi&gt;&#x27;)" in chunk or "print('&lt;hi&gt;')" in chunk
    assert_balanced(chunk)


def test_unterminated_fence_still_closes():
    (chunk,) = tg_format.to_html_chunks("```\nno closing fence")
    assert_balanced(chunk)


def test_bold_italic_and_links():
    (chunk,) = tg_format.to_html_chunks("**bold** and *soft* see [docs](https://x.dev/a_b)")
    assert "<b>bold</b>" in chunk
    assert "<i>soft</i>" in chunk
    # Underscores inside a URL must not become italics.
    assert '<a href="https://x.dev/a_b">docs</a>' in chunk
    assert_balanced(chunk)


def test_headings_and_bullets():
    chunks = tg_format.to_html_chunks("## Title\n- one\n- two")
    body = "\n".join(chunks)
    assert "<b>Title</b>" in body
    assert "• one" in body


def test_every_chunk_fits_and_stays_balanced():
    source = "\n".join(f"line {i} with some filler text" for i in range(400))
    chunks = tg_format.to_html_chunks(source, limit=500)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 500
        assert_balanced(chunk)


def test_oversized_code_block_splits_into_valid_blocks():
    body = "\n".join(f"    row_{i} = compute({i})" for i in range(300))
    chunks = tg_format.to_html_chunks(f"```python\n{body}\n```", limit=600)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 600
        assert_balanced(chunk)
        assert "<pre>" in chunk


def test_no_content_is_lost_across_chunks():
    source = "\n".join(f"unique-token-{i}" for i in range(200))
    joined = "".join(tg_format.to_html_chunks(source, limit=400))
    for i in range(200):
        assert f"unique-token-{i}" in joined


def test_empty_input_sends_nothing():
    assert tg_format.to_html_chunks("") == []
    assert tg_format.to_html_chunks("   \n  ") == []


def test_plain_fallback_respects_limit():
    chunks = tg_format.to_plain_chunks("x" * 5000, limit=1000)
    assert chunks
    assert all(len(chunk) <= 1000 for chunk in chunks)


@pytest.mark.parametrize(
    "text,limit,expected",
    [("hello", 10, "hello"), ("hello world", 8, "hello w…")],
)
def test_truncate(text, limit, expected):
    assert tg_format.truncate(text, limit) == expected
