#!/usr/bin/env python3
"""One command for Claude to check its own change before reporting it done.

uv run python scripts/check.py            # lint, tests, eval replay, harder generated cases
uv run python scripts/check.py --fast     # skip pytest (lint + replays only)

Prints one PASS/FAIL line per step and exits 1 if any failed. No screen is opened: input in,
state out. UI changes still need a person to look at the app.
"""

import subprocess
import sys
from pathlib import Path

import heavy_lock

ROOT = Path(__file__).resolve().parents[1]
RUN = ["uv", "run", "--locked", "python"]


def steps(fast: bool):
    yield "ruff", ["uv", "run", "--locked", "ruff", "check", "."]
    if not fast:
        yield "pytest", ["uv", "run", "--locked", "pytest", "-q", "-x"]
    yield "eval replay", [*RUN, "evals/run_eval.py"]
    yield "make cases", [*RUN, "evals/make_cases.py"]
    for level in (1, 2, 3):
        yield f"replay L{level}", [*RUN, "evals/run_eval.py", "--dir", f"evals/generated/L{level}"]


def main() -> int:
    heavy_lock.acquire()
    failed = []
    for name, command in steps("--fast" in sys.argv):
        done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        lines = (done.stdout + done.stderr).strip().splitlines()
        tail = lines[-3:]
        summary = [x for x in lines if x.startswith(("passed", "L1", "L2", "L3", "All checks"))]
        ok = done.returncode == 0
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {(summary or tail or [''])[-1]}")
        if not ok:
            failed.append(name)
            print("\n".join(f"      {line}" for line in tail))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
