"""Make sure a Chrome is running before Claude needs one.

The Claude in Chrome extension can only attach to a browser that is actually
open. Asking the bot for a screenshot when Chrome is closed otherwise ends in a
headless fallback that cannot see anything behind a login — so if Chrome is not
up, we start it.

Deliberately dependency-free: a `tasklist` / `pgrep` call is enough, and adding
psutil to a bot that mostly shells out anyway would not earn its keep.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import config

# Chrome needs a moment to come up and for the extension to attach.
LAUNCH_SETTLE_SECONDS = 6.0
_PROCESS_NAMES = ("chrome.exe", "chrome", "Google Chrome")


def _windows_candidates() -> list[Path]:
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    return [
        Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
        for root in roots
        if root
    ]


def find_binary() -> str:
    """Where Chrome lives, or "" if we cannot find it."""
    override = config.CHROME_BINARY
    if override:
        return override if Path(override).exists() else ""

    if sys.platform == "win32":
        for candidate in _windows_candidates():
            if candidate.exists():
                return str(candidate)
        return shutil.which("chrome") or ""

    if sys.platform == "darwin":
        app = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        return str(app) if app.exists() else ""

    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return ""


async def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except (FileNotFoundError, OSError):
        return 1, ""
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8", "replace")


async def is_running() -> bool:
    if sys.platform == "win32":
        code, out = await _run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"])
        return code == 0 and "chrome.exe" in out.lower()

    for name in _PROCESS_NAMES:
        code, out = await _run(["pgrep", "-f", name])
        if code == 0 and out.strip():
            return True
    return False


def _spawn(binary: str) -> bool:
    """Start Chrome detached, so stopping the bot does not close the browser."""
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen([binary], **kwargs)
        return True
    except OSError:
        return False


async def ensure() -> tuple[bool, str]:
    """(is a browser up now, what happened) — safe to call before every run."""
    if await is_running():
        return True, "already running"

    binary = find_binary()
    if not binary:
        return False, (
            "Chrome not found. Install it, or set CLAUDE_CHROME_BINARY in .env "
            "to its absolute path."
        )

    if not _spawn(binary):
        return False, f"could not start {binary}"

    # Poll rather than sleeping blindly, so a fast machine is not punished.
    deadline = time.monotonic() + LAUNCH_SETTLE_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        if await is_running():
            # The process exists; the extension still needs a beat to attach.
            await asyncio.sleep(1.5)
            return True, "launched"

    return False, f"started {Path(binary).name} but it did not come up in time"


def ensure_sync() -> tuple[bool, str]:
    """`ensure` for the permission hook, which is a plain synchronous process."""
    try:
        return asyncio.run(ensure())
    except Exception as exc:  # noqa: BLE001 - never let this break a decision
        return False, f"{type(exc).__name__}: {exc}"
