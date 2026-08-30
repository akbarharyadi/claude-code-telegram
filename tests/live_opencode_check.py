"""Manual live check for the opencode2 backend. Run from the repo root:

    AGENT_BACKEND=opencode uv run python tests/live_opencode_check.py

Needs a working `opencode2` (authenticated) and consumes a little GLM quota.
Exercises: server startup, session creation, a permission ask answered with
allow, session resume, and a permission ask answered with deny.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("AGENT_BACKEND", "opencode")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logging.getLogger("opencode_runner").setLevel(logging.DEBUG)

import bridge  # noqa: E402
from claude_runner import RunSpec  # noqa: E402
from opencode_runner import stream_run  # noqa: E402

SANDBOX = Path(os.environ.get("TEMP", "/tmp")) / "opencode" / "sandbox"


async def responder(run_id: str, choice: str) -> None:
    """Stand in for the phone: answer every approval this run raises."""
    while True:
        for request in bridge.pending():
            if request.get("run_id") != run_id:
                continue
            print(f"  [approval asked] {request.get('tool_name')}: {request.get('summary')}")
            bridge.respond(str(request.get("id")), {"choice": choice})
        await asyncio.sleep(0.25)


async def run_case(title, prompt, run_id, choice, check, session_id="", resume=False):
    print(f"\n== {title}", flush=True)
    spec = RunSpec(
        prompt=prompt,
        cwd=str(SANDBOX),
        session_id=session_id,
        resume=resume,
        timeout_seconds=120,
        env_extra={
            "CCTG_RUN_ID": run_id,
            "CCTG_CHAT_ID": "12345",
            "CCTG_THREAD_ID": "",
            "CCTG_SESSION_KEY": run_id,
        },
    )
    task = asyncio.create_task(responder(run_id, choice))
    events = []
    try:
        async for event in stream_run(spec):
            events.append(event)
            if event.kind == "init":
                print(f"  init: {event.session_id}", flush=True)
            elif event.kind == "tool":
                print(f"  tool: {event.tool} · {event.detail[:90]}", flush=True)
            elif event.kind == "error":
                print(f"  error: {event.text[:140]}", flush=True)
    finally:
        task.cancel()

    results = [e for e in events if e.kind == "result"]
    result = results[0] if results else None
    ok = check(events, result)
    sid = result.session_id if result else ""
    print(f"  -> {'PASS' if ok else 'FAIL'} | turns={result.num_turns if result else '-'} "
          f"| text: {(result.text[:80] if result else None)!r}")
    return ok, sid


async def main() -> int:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    verdicts = {}

    allow_file = SANDBOX / "cctg-allow.txt"
    allow_file.unlink(missing_ok=True)
    verdicts["allow"], sid = await run_case(
        "permission -> allow (file must appear)",
        "Use the shell tool to create a file named cctg-allow.txt in the current "
        "directory containing exactly the word banana. Then reply with one word: done.",
        "live-allow",
        "allow",
        lambda events, result: result is not None and not result.is_error and allow_file.exists(),
    )

    verdicts["resume"], _ = await run_case(
        "session resume",
        "Reply with exactly one word: pong.",
        "live-resume",
        "allow",
        lambda events, result: result is not None and bool(result.text.strip())
        and result.session_id == sid,
        session_id=sid,
        resume=True,
    )

    deny_file = SANDBOX / "cctg-deny.txt"
    deny_file.unlink(missing_ok=True)
    verdicts["deny"], _ = await run_case(
        "permission -> deny (file must NOT appear)",
        "Use the shell tool to create a file named cctg-deny.txt in the current "
        "directory. If that is refused, just reply: refused.",
        "live-deny",
        "deny",
        lambda events, result: result is not None and not deny_file.exists(),
    )

    print("\n==== verdicts:", verdicts)
    return 0 if all(verdicts.values()) else 1


if __name__ == "__main__":
    from opencode_runner import shutdown

    try:
        code = asyncio.run(main())
    finally:
        shutdown()
    sys.exit(code)
