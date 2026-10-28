"""Capability benchmark: scoring is pure, deterministic and live-results only."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("benchmark", ROOT / "evals" / "benchmark.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)

CONFIG = {"threshold": 0.8, "axes": {"see": 1, "read": 1}}


def trial(task_id, passed=True, valid=True):
    return {"task": task_id, "passed": passed, "valid": valid}


def t(task_id, axis, level, **extra):
    return {"id": task_id, "axis": axis, "level": level, "status": "active", **extra}


def test_axis_score_threshold_boundary():
    assert benchmark.axis_score([(10, 0.8)]) == 10
    assert benchmark.axis_score([(10, 0.79)]) == 0


def test_axis_score_stops_at_first_failing_level():
    assert benchmark.axis_score([(10, 1.0), (20, 0.0), (30, 1.0)]) == 10


def test_axis_score_first_level_fails_is_zero():
    assert benchmark.axis_score([(5, 0.0), (10, 1.0)]) == 0


def test_axis_score_groups_tasks_of_the_same_level():
    assert benchmark.axis_score([(10, 1.0), (10, 0.0), (20, 1.0)]) == 0


def test_overall_skips_unmeasured_axes():
    assert benchmark.overall({"a": 50, "b": None}, {"a": 1, "b": 1}) == 50


def test_overall_none_when_nothing_measured():
    assert benchmark.overall({"a": None}, {"a": 1}) is None


def test_overall_is_weighted():
    assert benchmark.overall({"a": 100, "b": 0}, {"a": 3, "b": 1}) == 75


def test_task_rates_excludes_invalid_trials():
    rates = benchmark.task_rates([t("x", "see", 1)], [trial("x"), trial("x", False, False)])
    assert rates == {"x": 1.0}


def test_task_rates_all_invalid_is_none():
    assert benchmark.task_rates([t("x", "see", 1)], [trial("x", False, False)]) == {"x": None}


def test_task_rates_missing_target_is_zero_missing_active_is_not_run():
    tasks = [t("a", "see", 1), t("b", "see", 2, status="target")]
    assert benchmark.task_rates(tasks, []) == {"b": 0.0}


def test_evaluate_axis_without_tasks_is_unmeasured():
    report = benchmark.evaluate([t("a", "see", 5)], [trial("a")], CONFIG)
    assert report["axes"]["read"]["measured"] is False
    assert report["axes"]["see"]["score"] == 5
    assert report["overall"] == 5


def test_evaluate_target_blocks_the_ladder():
    tasks = [t("a", "see", 5), t("b", "see", 50, status="target")]
    report = benchmark.evaluate(tasks, [trial("a")], CONFIG)
    assert report["axes"]["see"]["score"] == 5


def test_evaluate_flags_unclassified_and_not_run_but_ignores_controls():
    tasks = [
        t("a", "see", 5),
        t("b", "see", 6),
        {"id": "c", "request": "?"},
        {"id": "n", "negative": True},
    ]
    report = benchmark.evaluate(tasks, [trial("a")], CONFIG)
    assert report["unclassified"] == ["c"] and report["not_run"] == ["b"]


def test_evaluate_is_deterministic():
    tasks = [t("a", "see", 5), t("b", "read", 20)]
    trials = [trial("a"), trial("b", False)]
    assert benchmark.evaluate(tasks, trials, CONFIG) == benchmark.evaluate(tasks, trials, CONFIG)
