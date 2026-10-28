"""Capability score (1-100) per axis and overall, computed from a live eval result.

Replay results cannot be scored: their model answers are scripted, so they always pass.
Usage: run `evals/run_eval.py --live --repeat 3`, then `evals/benchmark.py`.
"""

import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "benchmark.json"


def task_rates(tasks: list[dict], trials: list[dict]) -> dict[str, float | None]:
    """Pass rate per task id over valid trials. A target task nobody ran counts as 0."""
    by_task = defaultdict(list)
    for r in trials:
        by_task[r["task"]].append(r)
    rates = {}
    for t in tasks:
        runs = by_task.get(t["id"])
        if not runs:
            if t.get("status") == "target":
                rates[t["id"]] = 0.0
            continue  # active task missing from the result = not run
        valid = [r for r in runs if r["valid"]]
        rates[t["id"]] = sum(r["passed"] for r in valid) / len(valid) if valid else None
    return rates


def axis_score(rates: list[tuple[int, float]], threshold: float = 0.8) -> int:
    """Highest level whose cumulative pass rate (all tasks up to it) meets the threshold.

    Stops at the first level that falls short, so an easy pass above a failure never counts.
    """
    levels = defaultdict(list)
    for level, rate in rates:
        levels[level].append(rate)
    score, seen = 0, []
    for level in sorted(levels):
        seen += levels[level]
        if sum(seen) / len(seen) < threshold:
            break
        score = level
    return score


def overall(axis_scores: dict[str, int | None], weights: dict[str, float]) -> int | None:
    measured = {a: s for a, s in axis_scores.items() if s is not None}
    total = sum(weights.get(a, 1) for a in measured)
    if not total:
        return None
    return round(sum(s * weights.get(a, 1) for a, s in measured.items()) / total)


def evaluate(tasks: list[dict], trials: list[dict], config: dict) -> dict:
    axes = config["axes"]
    graded = [t for t in tasks if not t.get("negative")]
    classified = [
        t for t in graded
        if t.get("axis") in axes and isinstance(t.get("level"), int) and 1 <= t["level"] <= 100
    ]
    rates = task_rates(classified, trials)
    pairs = defaultdict(list)
    for t in classified:
        if rates.get(t["id"]) is not None:
            pairs[t["axis"]].append((t["level"], rates[t["id"]]))
    scores = {a: axis_score(pairs[a], config["threshold"]) if pairs[a] else None for a in axes}
    return {
        "axes": {
            a: {"score": scores[a], "measured": scores[a] is not None, "tasks": len(pairs[a])}
            for a in axes
        },
        "overall": overall(scores, axes),
        "not_run": [t["id"] for t in classified if t["id"] not in rates],
        "unclassified": [t["id"] for t in graded if t not in classified],
        "invalid_only": [i for i, r in rates.items() if r is None],
    }


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
