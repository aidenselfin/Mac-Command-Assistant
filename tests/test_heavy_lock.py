import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _clean_env():
    return {k: v for k, v in os.environ.items() if k != "VOICE_ACTION_HEAVY_LOCK_HELD"}


def _run(lock_path, hold, extra=""):
    code = (
        f"import sys, time; sys.path.insert(0, {str(SCRIPTS)!r}); import heavy_lock; "
        f"from pathlib import Path; heavy_lock.LOCK_PATH = Path({str(lock_path)!r}); "
        f"heavy_lock.acquire(); {extra} time.sleep({hold})"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code], stderr=subprocess.PIPE, text=True, env=_clean_env()
    )


def test_second_process_waits_for_first(tmp_path):
    lock = tmp_path / "heavy.lock"
    first = _run(lock, 1.5)
    time.sleep(0.5)
    start = time.time()
    second = _run(lock, 0)
    _, err = second.communicate(timeout=10)
    assert time.time() - start >= 0.8
    assert "기다립니다" in err
    first.wait()


def test_child_of_holder_does_not_deadlock(tmp_path):
    lock = tmp_path / "heavy.lock"
    child = f"subprocess.run([sys.executable, '-c', {_child_code(lock)!r}], timeout=5, check=True)"
    holder = _run(lock, 0, extra=f"import subprocess; {child};")
    assert holder.wait(timeout=10) == 0


def _child_code(lock):
    return (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import heavy_lock; "
        f"from pathlib import Path; heavy_lock.LOCK_PATH = Path({str(lock)!r}); "
        "heavy_lock.acquire()"
    )
