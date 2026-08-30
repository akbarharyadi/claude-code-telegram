"""The review sweep posts under a real GitHub identity, so the parts that
decide *what* gets posted are pinned here: verdict parsing (a malformed reply
must raise, never silently approve), the disclosure line (every body must carry
one, and the unread case must say so), and the guard that downgrades an approval
when only part of the diff was read."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pr_review  # noqa: E402
from pr_review import PullRequest, ReviewError, Verdict  # noqa: E402


def test_extracts_a_fenced_verdict():
    verdict = pr_review._extract_verdict(
        'Here you go:\n```json\n{"verdict": "approve", "summary": "Looks fine.", '
        '"findings": []}\n```'
    )
    assert verdict.verdict == "approve"
    assert verdict.summary == "Looks fine."
    assert verdict.findings == []


def test_extracts_a_verdict_without_the_fence():
    verdict = pr_review._extract_verdict('{"verdict": "request_changes", "findings": ["boom"]}')
    assert verdict.verdict == "request_changes"
    assert verdict.findings == ["boom"]


@pytest.mark.parametrize(
    "reply",
    [
        "Looks good to me!",  # prose, no json at all
        '```json\n{"verdict": "lgtm"}\n```',  # not one of ours
        '```json\n{"verdict": approve}\n```',  # invalid json
    ],
)
def test_unparseable_replies_raise_rather_than_approve(reply):
    """A reply we cannot read must stop the sweep, not fall through to approve."""
    with pytest.raises(ReviewError):
        pr_review._extract_verdict(reply)


def test_non_list_findings_are_coerced():
    verdict = pr_review._extract_verdict('{"verdict": "comment", "findings": "just one"}')
    assert verdict.findings == ["just one"]


def test_extracts_inline_comments_and_skips_broken_ones():
    """Line comments ride along; rows we cannot read are dropped, not fatal."""
    verdict = pr_review._extract_verdict(
        '{"verdict": "approve", "summary": "s", "findings": [], "comments": ['
        '{"path": "a/x.py", "line": "12", "body": "checked bounds"}, '
        '{"path": "y.py"}, '
        '{"path": "z.py", "line": "nope", "body": "b"}, '
        '"junk"]}'
    )
    assert [(c.path, c.line, c.body) for c in verdict.comments] == [
        ("a/x.py", 12, "checked bounds")
    ]


_SAMPLE_DIFF = "\n".join(
    [
        "diff --git a/app.py b/app.py",
        "index 1111111..2222222 100644",
        "--- a/app.py",
        "+++ b/app.py",
        "@@ -1,3 +1,4 @@",
        " context",
        "-removed",
        "+added",
        "+another",
        " tail",
        "@@ -10,3 +10,1 @@",
        " ctx",
        "-del one",
        "-del two",
        "diff --git a/lib/old.py b/lib/new.py",
        "similarity index 90%",
        "rename from lib/old.py",
        "rename to lib/new.py",
        "@@ -5 +5 @@",
        "-before",
        "+after",
    ]
)


def test_diff_anchors_track_both_sides():
    anchors = pr_review.diff_anchors(_SAMPLE_DIFF)
    assert anchors["app.py"]["RIGHT"] == {1, 2, 3, 4, 10}
    assert anchors["app.py"]["LEFT"] == {1, 2, 3, 10, 11, 12}
    assert anchors["lib/new.py"]["RIGHT"] == {5}
    assert anchors["lib/new.py"]["LEFT"] == {5}


def test_diff_anchors_survive_header_lookalikes():
    """A deleted line whose text starts with `--` must not parse as a header."""
    diff = "\n".join(
        [
            "diff --git a/flags.py b/flags.py",
            "--- a/flags.py",
            "+++ b/flags.py",
            "@@ -1,2 +1,2 @@",
            "---verbose",  # deleting the line "--verbose"
            "+--quiet",
        ]
    )
    anchors = pr_review.diff_anchors(diff)
    assert anchors["flags.py"]["LEFT"] == {1}
    assert anchors["flags.py"]["RIGHT"] == {1}


def test_inline_comments_that_cannot_anchor_are_dropped():
    """One invented path or line voids a whole review on GitHub, so bad
    anchors are dropped here instead of posted."""
    kept = pr_review.fit_comments(
        [
            pr_review.LineComment(path="app.py", line=2, body="on the addition"),
            pr_review.LineComment(path="b/app.py", line=4, body="prefix stripped"),
            pr_review.LineComment(path="app.py", line=3, body="prefers the new side"),
            pr_review.LineComment(path="app.py", line=11, body="a deleted line, old side"),
            pr_review.LineComment(path="ghost.py", line=1, body="invented file"),
            pr_review.LineComment(path="app.py", line=99, body="invented line"),
            pr_review.LineComment(path="app.py", line=2, body="duplicate anchor"),
            pr_review.LineComment(path="app.py", line=1, body=""),
        ],
        _SAMPLE_DIFF,
    )
    assert [(c.path, c.line, c.side, c.body) for c in kept] == [
        ("app.py", 2, "RIGHT", "on the addition"),
        ("app.py", 4, "RIGHT", "prefix stripped"),
        ("app.py", 3, "RIGHT", "prefers the new side"),
        ("app.py", 11, "LEFT", "a deleted line, old side"),
    ]


@pytest.mark.anyio
async def test_submit_review_posts_inline_comments_via_the_api(monkeypatch):
    """Only the reviews endpoint takes per-line comments, so `gh pr review`
    is out; the payload rides to `gh api` on stdin."""
    calls: list[tuple[tuple[str, ...], str | None]] = []

    async def fake_gh(*args, check=True, stdin_data=None):
        calls.append((args, stdin_data))
        return ""

    monkeypatch.setattr(pr_review, "_gh", fake_gh)
    pr = PullRequest(repo="o/r", number=5, title="t", author="a", url="u", head_sha="cafe")
    verdict = Verdict(
        verdict="approve",
        summary="ok",
        comments=[pr_review.LineComment(path="app.py", line=2, body="checked")],
    )

    await pr_review.submit_review(pr, verdict)

    args, stdin = calls[0]
    assert "repos/o/r/pulls/5/reviews" in args
    payload = json.loads(stdin or "")
    assert payload["event"] == "APPROVE"
    assert payload["commit_id"] == "cafe"
    assert payload["comments"] == [
        {"path": "app.py", "line": 2, "side": "RIGHT", "body": "checked"}
    ]


def test_a_reviewed_body_is_just_the_review():
    """quick mode is your own tool-assisted work — no footer on it."""
    body = pr_review.render_body(Verdict(verdict="approve", summary="Fine."))
    assert body == "Fine."


def test_the_body_is_the_review_and_nothing_else():
    """No footer in either mode — the body carries the review, full stop."""
    body = pr_review.render_body(
        Verdict(verdict="approve", summary="Approved.", unread=True)
    )
    assert body == "Approved."


def test_findings_are_rendered_as_bullets():
    body = pr_review.render_body(
        Verdict(verdict="request_changes", summary="Two problems.", findings=["a", "b"])
    )
    assert "- a" in body
    assert "- b" in body


@pytest.mark.anyio
async def test_a_truncated_diff_downgrades_an_approval(monkeypatch):
    """Approving a diff we only partly read would overstate what was checked."""

    async def fake_stream(spec):
        yield type("E", (), {"kind": "text", "text": '{"verdict": "approve", "summary": "ok"}'})()
        yield type(
            "E", (), {"kind": "result", "text": "", "cost_usd": 0.01, "is_error": False}
        )()

    monkeypatch.setattr(pr_review, "stream_run", fake_stream)
    pr = PullRequest(repo="o/r", number=1, title="t", author="someone", url="u")

    verdict = await pr_review.ask_claude(pr, "x" * (pr_review.MAX_DIFF_CHARS + 1))

    assert verdict.verdict == "comment"
    assert "truncated" in pr_review.render_body(verdict).lower() or "exceeds" in verdict.summary


@pytest.mark.anyio
async def test_a_short_diff_keeps_its_approval(monkeypatch):
    async def fake_stream(spec):
        yield type("E", (), {"kind": "text", "text": '{"verdict": "approve", "summary": "ok"}'})()
        yield type(
            "E", (), {"kind": "result", "text": "", "cost_usd": 0.0, "is_error": False}
        )()

    monkeypatch.setattr(pr_review, "stream_run", fake_stream)
    pr = PullRequest(repo="o/r", number=1, title="t", author="someone", url="u")

    verdict = await pr_review.ask_claude(pr, "a small diff")

    assert verdict.verdict == "approve"


@pytest.mark.anyio
async def test_your_own_prs_are_skipped(monkeypatch):
    """GitHub refuses a self-review, so filing one would only ever 422."""

    async def fake_gh(*args, **kwargs):
        return (
            '[{"repository": {"nameWithOwner": "o/r"}, "number": 1, "title": "mine",'
            ' "author": {"login": "octocat"}, "isDraft": false, "url": "u"},'
            ' {"repository": {"nameWithOwner": "o/r"}, "number": 2, "title": "theirs",'
            ' "author": {"login": "someone"}, "isDraft": false, "url": "u"}]'
        )

    monkeypatch.setattr(pr_review, "_gh", fake_gh)
    found = await pr_review.find_pending(["o/r"], "octocat")

    assert [p.number for p in found] == [2]


@pytest.mark.anyio
async def test_approve_mode_never_fetches_a_diff(monkeypatch):
    """The whole point of approve mode is that it reads nothing."""

    async def explode(*args, **kwargs):
        raise AssertionError("approve mode must not fetch the diff")

    monkeypatch.setattr(pr_review, "fetch_diff", explode)
    pr = PullRequest(repo="o/r", number=1, title="t", author="someone", url="u")

    outcome = await pr_review.review_one(pr, mode="approve", dry_run=True)

    assert outcome.verdict is not None
    assert outcome.verdict.verdict == "approve"
    assert outcome.verdict.unread is True
    assert outcome.posted is False


@pytest.mark.anyio
async def test_a_dry_run_posts_nothing(monkeypatch):
    async def explode(*args, **kwargs):
        raise AssertionError("a dry run must not submit a review")

    monkeypatch.setattr(pr_review, "submit_review", explode)
    pr = PullRequest(repo="o/r", number=1, title="t", author="someone", url="u")

    outcome = await pr_review.review_one(pr, mode="approve", dry_run=True)

    assert outcome.posted is False


@pytest.mark.anyio
async def test_an_unknown_mode_is_refused(monkeypatch):
    monkeypatch.setattr(pr_review.config, "REVIEW_REPOS", ["o/r"])
    with pytest.raises(ReviewError, match="REVIEW_MODE"):
        await pr_review.sweep(mode="rubber-stamp")


@pytest.mark.anyio
async def test_no_repos_configured_is_refused(monkeypatch):
    """Better to refuse than to sweep every repo the token can see."""
    monkeypatch.setattr(pr_review.config, "REVIEW_REPOS", [])
    with pytest.raises(ReviewError, match="REVIEW_REPOS"):
        await pr_review.sweep()


def test_summarize_reports_errors_without_hiding_them():
    pr = PullRequest(repo="o/r", number=7, title="t", author="a", url="u")
    text = pr_review.summarize([pr_review.Outcome(pr=pr, error="gh exploded")])
    assert "o/r#7" in text
    assert "gh exploded" in text


def test_summarize_marks_unposted_verdicts_as_a_dry_run():
    pr = PullRequest(repo="o/r", number=7, title="t", author="a", url="u")
    text = pr_review.summarize(
        [pr_review.Outcome(pr=pr, verdict=Verdict(verdict="approve", summary="ok"), posted=False)]
    )
    assert "dry run" in text


@pytest.mark.anyio
async def test_an_oversized_diff_comments_instead_of_approving(monkeypatch):
    """GitHub caps its diff API at 20,000 lines. A change that big is exactly
    the kind a skim must not wave through."""

    async def too_big(*args, **kwargs):
        raise pr_review.DiffTooLarge("HTTP 406: Sorry, the diff exceeded the maximum")

    async def explode(*args, **kwargs):
        raise AssertionError("must not consult the model on a diff it cannot read")

    monkeypatch.setattr(pr_review, "fetch_diff", too_big)
    monkeypatch.setattr(pr_review, "ask_claude", explode)
    pr = PullRequest(repo="o/r", number=798, title="huge", author="someone", url="u")

    outcome = await pr_review.review_one(pr, mode="quick", dry_run=True)

    assert outcome.verdict is not None
    assert outcome.verdict.verdict == "comment"
    assert "needs a person" in outcome.verdict.summary


@pytest.mark.anyio
async def test_other_gh_diff_failures_still_raise(monkeypatch):
    """Only the size cap is an answer; everything else is a real error."""

    async def fake_gh(*args, **kwargs):
        raise ReviewError("gh pr diff failed (1): network unreachable")

    monkeypatch.setattr(pr_review, "_gh", fake_gh)
    pr = PullRequest(repo="o/r", number=1, title="t", author="a", url="u")

    with pytest.raises(ReviewError) as caught:
        await pr_review.fetch_diff(pr)
    assert not isinstance(caught.value, pr_review.DiffTooLarge)


@pytest.mark.anyio
async def test_limit_stops_the_sweep_early(monkeypatch, tmp_path):
    """A first run should be able to try one PR, not the whole backlog."""
    monkeypatch.setattr(pr_review.config, "REVIEW_REPOS", ["o/r"])
    monkeypatch.setattr(pr_review.config, "REVIEW_STATE_FILE", tmp_path / "reviews.json")

    async def fake_pending(repos, me):
        return [
            PullRequest(repo="o/r", number=n, title="t", author="someone", url="u")
            for n in (1, 2, 3)
        ]

    reviewed: list[int] = []

    async def fake_review_one(pr, *, mode, dry_run):
        reviewed.append(pr.number)
        return pr_review.Outcome(pr=pr, verdict=Verdict(verdict="approve"), posted=False)

    monkeypatch.setattr(pr_review, "find_pending", fake_pending)
    monkeypatch.setattr(pr_review, "head_sha", lambda pr: _sha())
    monkeypatch.setattr(pr_review, "review_one", fake_review_one)

    outcomes = await pr_review.sweep(mode="quick", dry_run=True, limit=1)

    assert reviewed == [1]
    assert len(outcomes) == 1


async def _sha():
    return "deadbeef"


@pytest.mark.anyio
async def test_a_mismatched_gh_account_is_refused(monkeypatch):
    """`gh pr review` posts as the ACTIVE account, not the one we searched for.
    Switching logins for an unrelated push must not file approvals as the
    wrong person."""
    monkeypatch.setattr(pr_review.config, "REVIEW_REPOS", ["o/r"])
    monkeypatch.setattr(pr_review.config, "REVIEW_LOGIN", "work-account")

    async def signed_in_as_someone_else():
        return "personal-account"

    async def explode(*args, **kwargs):
        raise AssertionError("must not search or post under the wrong account")

    monkeypatch.setattr(pr_review, "whoami", signed_in_as_someone_else)
    monkeypatch.setattr(pr_review, "find_pending", explode)

    with pytest.raises(ReviewError, match="personal-account"):
        await pr_review.sweep()


@pytest.mark.anyio
async def test_a_matching_gh_account_proceeds(monkeypatch, tmp_path):
    monkeypatch.setattr(pr_review.config, "REVIEW_REPOS", ["o/r"])
    monkeypatch.setattr(pr_review.config, "REVIEW_LOGIN", "work-account")
    monkeypatch.setattr(pr_review.config, "REVIEW_STATE_FILE", tmp_path / "reviews.json")

    async def signed_in_correctly():
        return "work-account"

    async def no_prs(repos, me):
        assert me == "work-account"
        return []

    monkeypatch.setattr(pr_review, "whoami", signed_in_correctly)
    monkeypatch.setattr(pr_review, "find_pending", no_prs)

    assert await pr_review.sweep() == []


@pytest.mark.anyio
async def test_an_approval_posts_no_inline_comments(monkeypatch):
    """Inline notes are for defects only — verification receipts on a clean
    approval just spam every hunk of the diff."""

    async def approving(pr, diff):
        return Verdict(
            verdict="approve",
            summary="clean",
            comments=[pr_review.LineComment(path="x.py", line=1, body="receipt")],
        )

    monkeypatch.setattr(pr_review, "fetch_diff", _tiny_diff)
    monkeypatch.setattr(pr_review, "ask_claude", approving)
    pr = PullRequest(repo="o/r", number=9, title="t", author="someone", url="u")

    outcome = await pr_review.review_one(pr, mode="quick", dry_run=True)

    assert outcome.verdict is not None
    assert outcome.verdict.verdict == "approve"
    assert outcome.verdict.comments == []


@pytest.mark.anyio
async def test_requested_changes_keep_their_inline_comments(monkeypatch):
    async def defect(pr, diff):
        return Verdict(
            verdict="request_changes",
            summary="one defect",
            comments=[pr_review.LineComment(path="x.py", line=1, body="what breaks")],
        )

    monkeypatch.setattr(pr_review, "fetch_diff", _tiny_diff)
    monkeypatch.setattr(pr_review, "ask_claude", defect)
    pr = PullRequest(repo="o/r", number=9, title="t", author="someone", url="u")

    outcome = await pr_review.review_one(pr, mode="quick", dry_run=True)

    assert outcome.verdict is not None
    assert [(c.path, c.line, c.body) for c in outcome.verdict.comments] == [
        ("x.py", 1, "what breaks")
    ]


@pytest.mark.anyio
async def test_review_now_reviews_a_pr_nobody_is_waiting_on(monkeypatch, tmp_path):
    """/review is the on-demand path: it must not care that the PR was already
    handled, and it must record the head so the sweep skips it after."""
    monkeypatch.setattr(pr_review.config, "REVIEW_REPOS", ["o/r"])
    monkeypatch.setattr(pr_review.config, "REVIEW_LOGIN", "")  # trust the active gh login
    monkeypatch.setattr(pr_review.config, "REVIEW_STATE_FILE", tmp_path / "reviews.json")

    async def fake_gh(*args, check=True, stdin_data=None):
        if args[:2] == ("pr", "view"):
            return (
                '{"number": 7, "title": "the pr", "author": {"login": "someone"},'
                ' "isDraft": false, "url": "u", "headRefOid": "cafe"}'
            )
        return "work-account"  # whoami

    monkeypatch.setattr(pr_review, "_gh", fake_gh)
    monkeypatch.setattr(pr_review, "fetch_diff", _tiny_diff)
    monkeypatch.setattr(pr_review, "ask_claude", _ok_verdict)

    posted: list[int] = []

    async def fake_submit(pr, verdict):
        posted.append(pr.number)

    monkeypatch.setattr(pr_review, "submit_review", fake_submit)

    outcome = await pr_review.review_now("o/r", 7)

    assert outcome.posted is True
    assert outcome.verdict is not None and outcome.verdict.verdict == "approve"
    assert posted == [7]
    state = json.loads((tmp_path / "reviews.json").read_text(encoding="utf-8"))
    assert state == {"o/r#7": "cafe"}


async def _tiny_diff(pr):
    return "diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-ok\n+fine\n"


async def _ok_verdict(pr, diff):
    return Verdict(verdict="approve", summary="ok")


@pytest.mark.anyio
async def test_review_now_refuses_a_mismatched_account(monkeypatch, tmp_path):
    monkeypatch.setattr(pr_review.config, "REVIEW_REPOS", ["o/r"])
    monkeypatch.setattr(pr_review.config, "REVIEW_LOGIN", "work-account")
    monkeypatch.setattr(pr_review.config, "REVIEW_STATE_FILE", tmp_path / "reviews.json")

    async def signed_in_as_someone_else():
        return "personal-account"

    monkeypatch.setattr(pr_review, "whoami", signed_in_as_someone_else)

    with pytest.raises(ReviewError, match="personal-account"):
        await pr_review.review_now("o/r", 7)
