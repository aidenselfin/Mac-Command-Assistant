"""Let only one heavy job (pytest, live eval) run at a time across all sessions and worktrees.

Several Claude/Codex sessions test in parallel and saturate the CPU. Call acquire() before the
heavy work; the lock is held until the process exits, so there is nothing to release.
"""

import fcntl
import os
import sys
from pathlib import Path

LOCK_PATH = Path.home() / ".claude" / "voice-action" / "heavy.lock"
HELD_ENV = "VOICE_ACTION_HEAVY_LOCK_HELD"
_held = None


def acquire() -> None:
    global _held
    if _held is not None or os.environ.get(HELD_ENV):  # child of a holder: already inside the lock
        return
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("다른 세션의 무거운 작업이 끝나길 기다립니다...", file=sys.stderr, flush=True)
        fcntl.flock(handle, fcntl.LOCK_EX)  # ponytail: no timeout; a crashed holder frees it
    os.environ[HELD_ENV] = "1"
    _held = handle
