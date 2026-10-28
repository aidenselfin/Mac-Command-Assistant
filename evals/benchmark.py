"""Capability score (1-100) per axis and overall, computed from a live eval result.

Replay results cannot be scored: their model answers are scripted, so they always pass.
Usage: run `evals/run_eval.py --live --repeat 3`, then `evals/benchmark.py`.
"""

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "benchmark.json"
TASKS = HERE / "tasks"
RESULTS = HERE / "results"
BENCHMARKS = HERE / "benchmarks"


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
    """Highest level whose own tasks meet the threshold, with every lower level also met.

    Stops at the first level that falls short, so an easy pass above a failure never counts.
    Levels are not averaged together: one failing hard task must not hide behind many easy ones.
    """
    levels = defaultdict(list)
    for level, rate in rates:
        levels[level].append(rate)
    score = 0
    for level in sorted(levels):
        if sum(levels[level]) / len(levels[level]) < threshold:
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


def guidance(report: dict, tasks: list[dict], trials: list[dict], threshold: float = 0.8) -> dict:
    """Where to develop next: the weakest axis and, per axis, the lowest unmet task."""
    classified = [t for t in tasks if not t.get("negative") and t.get("axis") in report["axes"]]
    rates = task_rates(classified, trials)
    blocked = {}
    for axis, info in report["axes"].items():
        if not info["measured"]:
            continue
        failing = [
            t for t in classified
            if t["axis"] == axis and rates.get(t["id"]) is not None and rates[t["id"]] < threshold
        ]
        if failing:
            t = min(failing, key=lambda x: x["level"])
            problems = sorted({
                p for r in trials if r["task"] == t["id"] and r["valid"] and not r["passed"]
                for p in r.get("problems", []) + r.get("violations", [])
            })
            blocked[axis] = {
                "task": t["id"], "level": t["level"], "rate": rates[t["id"]], "problems": problems
            }
    measured = {a: i["score"] for a, i in report["axes"].items() if i["measured"]}
    weakest = min(measured, key=measured.get) if measured else None
    return {"weakest_axis": weakest, "blocked": blocked}


def compare(new: dict, old: dict) -> dict:
    axis_delta = {}
    for axis, info in new["axes"].items():
        before = old["axes"].get(axis, {}).get("score")
        if info["score"] is not None and before is not None:
            axis_delta[axis] = info["score"] - before
    delta = None
    if new["overall"] is not None and old["overall"] is not None:
        delta = new["overall"] - old["overall"]
    return {"overall_delta": delta, "axis_delta": axis_delta}


def render(report: dict, guide: dict, delta: dict | None = None) -> str:
    lines = []
    for axis, info in report["axes"].items():
        if not info["measured"]:
            lines.append(f"{axis:10} 측정 안 됨")
            continue
        filled = round(info["score"] / 5)
        change = ""
        if delta and axis in delta["axis_delta"]:
            change = f" ({delta['axis_delta'][axis]:+d})"
        lines.append(f"{axis:10} {'█' * filled}{'░' * (20 - filled)} {info['score']:3d}{change}")
    total = "측정 안 됨" if report["overall"] is None else str(report["overall"])
    if delta and delta["overall_delta"] is not None:
        total += f" ({delta['overall_delta']:+d})"
    lines.append(f"종합 {total}")
    if guide["weakest_axis"]:
        lines.append(f"가장 약한 축: {guide['weakest_axis']}")
    for axis, b in guide["blocked"].items():
        lines.append(f"막힘 {axis}: {b['task']} (단계 {b['level']}, 통과율 {b['rate']:.0%})")
    notes = (("not_run", "미실행"), ("unclassified", "미분류"), ("invalid_only", "INVALID뿐"))
    for key, label in notes:
        if report[key]:
            lines.append(f"{label} {len(report[key])}개: {', '.join(report[key])}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, help="run_eval live result (default: latest)")
    parser.add_argument("--compare", type=Path, help="earlier file from evals/benchmarks/")
    args = parser.parse_args(argv)
    result = args.result or max(RESULTS.glob("*.json"), default=None)
    if result is None:
        print("결과 없음. 먼저 `python evals/run_eval.py --live --repeat 3`를 실행하세요.")
        return 2
    data = json.loads(result.read_text(encoding="utf-8"))
    if data.get("versions", {}).get("model") == "scripted":
        print("재생 결과는 능력을 재지 못합니다. --live 결과를 쓰세요.")
        return 2
    config = load_config()
    tasks = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(TASKS.glob("*.json"))]
    report = evaluate(tasks, data["trials"], config)
    guide = guidance(report, tasks, data["trials"], config["threshold"])
    delta = None
    if args.compare:
        delta = compare(report, json.loads(args.compare.read_text(encoding="utf-8"))["report"])
    print(render(report, guide, delta))
    BENCHMARKS.mkdir(exist_ok=True)
    out = BENCHMARKS / time.strftime("%Y%m%d-%H%M%S.json")
    out.write_text(
        json.dumps(
            {"versions": data.get("versions"), "source": result.name, "report": report,
             "guidance": guide},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"saved {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
