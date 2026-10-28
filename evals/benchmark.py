"""Capability score (1-100) per axis and overall, computed from a live eval result.

Replay results cannot be scored: their model answers are scripted, so they always pass.
Usage: run `evals/run_eval.py --live --repeat 3`, then `evals/benchmark.py`.
"""

import argparse
import hashlib
import json
import math
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
    value = sum(s * weights.get(a, 1) for a, s in measured.items()) / total
    if len(measured) == len(weights) and all(s == 100 for s in measured.values()):
        return 100
    return min(99, math.floor(value + 0.5))  # 100 is reserved for every axis being 100


def is_classified(task: dict, axes) -> bool:
    level = task.get("level")
    return task.get("axis") in axes and isinstance(level, int) and 1 <= level <= 100


def evaluate(tasks: list[dict], trials: list[dict], config: dict) -> dict:
    axes = config["axes"]
    graded = [t for t in tasks if not t.get("negative")]
    classified = [t for t in graded if is_classified(t, axes)]
    rates = task_rates(classified, trials)
    ran_ids = {r["task"] for r in trials}
    pairs, ran = defaultdict(list), set()
    for t in classified:
        if rates.get(t["id"]) is not None:
            pairs[t["axis"]].append((t["level"], rates[t["id"]]))
            if t.get("status") != "target" or t["id"] in ran_ids:
                ran.add(t["axis"])  # unrun targets alone must not make an axis "measured"
    scores = {a: axis_score(pairs[a], config["threshold"]) if a in ran else None for a in axes}
    return {
        "axes": {
            a: {"score": scores[a], "measured": scores[a] is not None, "tasks": len(pairs[a])}
            for a in axes
        },
        "overall": overall(scores, axes),
        "not_run": [t["id"] for t in classified if t["id"] not in rates],
        "unclassified": [t["id"] for t in graded if t not in classified],
        "invalid_only": [i for i, r in rates.items() if r is None],
        "invalid_trials": sum(
            1 for r in trials if not r["valid"] and r["task"] in {t["id"] for t in classified}
        ),
    }


def stale_tasks(tasks: list[dict], recorded: dict) -> list[str]:
    """Tasks whose definition changed since the result was produced (hash as in run_eval)."""
    def digest(t):
        text = json.dumps(t, sort_keys=True)
        return hashlib.sha256(text.encode()).hexdigest()[:12]
    return [t["id"] for t in tasks if t["id"] in recorded and recorded[t["id"]] != digest(t)]


def latest_live_result(directory: Path) -> Path | None:
    live = [
        p for p in sorted(directory.glob("*.json"))
        if json.loads(p.read_text(encoding="utf-8")).get("versions", {}).get("model") != "scripted"
    ]
    return live[-1] if live else None


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def guidance(report: dict, tasks: list[dict], trials: list[dict], threshold: float = 0.8) -> dict:
    """Where to develop next: the weakest axis and, per axis, the lowest unmet task."""
    classified = [
        t for t in tasks if not t.get("negative") and is_classified(t, report["axes"])
    ]
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
    if report.get("invalid_trials"):
        lines.append(f"INVALID 시도 {report['invalid_trials']}회는 통과율에서 제외")
    if guide["weakest_axis"]:
        lines.append(f"가장 약한 축: {guide['weakest_axis']}")
    for axis, b in guide["blocked"].items():
        lines.append(f"막힘 {axis}: {b['task']} (단계 {b['level']}, 통과율 {b['rate']:.0%})")
    notes = (("not_run", "미실행"), ("unclassified", "미분류"), ("invalid_only", "INVALID뿐"),
             ("stale", "과제 바뀜"))
    for key, label in notes:
        if report.get(key):
            lines.append(f"{label} {len(report[key])}개: {', '.join(report[key])}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, help="run_eval live result (default: latest)")
    parser.add_argument("--compare", type=Path, help="earlier file from evals/benchmarks/")
    args = parser.parse_args(argv)
    result = args.result or latest_live_result(RESULTS)
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
    report["stale"] = stale_tasks(tasks, data.get("versions", {}).get("tasks", {}))
    guide = guidance(report, tasks, data["trials"], config["threshold"])
    delta = None
    if args.compare:
        delta = compare(report, json.loads(args.compare.read_text(encoding="utf-8"))["report"])
    print(render(report, guide, delta))
    BENCHMARKS.mkdir(exist_ok=True)
    record = json.dumps(
        {"versions": data.get("versions"), "source": result.name, "report": report,
         "guidance": guide},
        ensure_ascii=False, indent=2,
    )
    n = 1
    while True:  # exclusive create: never overwrite a record, even from a concurrent run
        suffix = "" if n == 1 else f"-{n}"
        out = BENCHMARKS / time.strftime(f"%Y%m%d-%H%M%S{suffix}.json")
        try:
            with out.open("x", encoding="utf-8") as fh:
                fh.write(record)
            break
        except FileExistsError:
            n += 1
    print(f"saved {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
