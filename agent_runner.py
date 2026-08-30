"""Pick the agent backend: Claude Code CLI or OpenCode 2.

Both backends expose the same contract — `RunSpec` in, `stream_run(spec)` yielding
`claude_runner.Event` out — so `bot.py`, `doctor.py` and `pr_review.py` stay
backend-agnostic. The `.env` switch is `AGENT_BACKEND=claude|opencode`.
"""

from __future__ import annotations

import config

if config.AGENT_BACKEND == "opencode":
    import opencode_runner
else:
    import claude_runner as opencode_runner  # type: ignore[no-redef]

# The canonical definitions, whoever the active backend is.
from claude_runner import Event, RunSpec  # noqa: F401  (re-exported)


def stream_run(spec: RunSpec):  # noqa: ANN201 - AsyncIterator[Event], see above
    """Delegate to the active backend's stream_run."""
    return opencode_runner.stream_run(spec)


def describe_target(cwd: str, add_dirs) -> str:
    return opencode_runner.describe_target(cwd, add_dirs)


def shutdown() -> None:
    """Release backend resources on bot shutdown (no-op for the CLI backend)."""
    stop = getattr(opencode_runner, "shutdown", None)
    if callable(stop):
        stop()
