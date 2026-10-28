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


def test_axis_score_one_failing_level_is_not_diluted_by_easy_ones():
    easy = [(level, 1.0) for level in range(1, 20)]
    assert benchmark.axis_score([*easy, (50, 0.0)]) == 19


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


# --- report, guidance, compare, CLI ---

import json  # noqa: E402


def test_guidance_picks_lowest_failing_level_per_axis():
    tasks = [t("a", "see", 5), t("b", "see", 9), t("c", "see", 20)]
    trials = [trial("a"), {**trial("b", False), "problems": ["files: x"]}, trial("c", False)]
    guide = benchmark.guidance(benchmark.evaluate(tasks, trials, CONFIG), tasks, trials)
    assert guide["blocked"]["see"] == {
        "task": "b", "level": 9, "rate": 0.0, "problems": ["files: x"]
    }


def test_guidance_weakest_axis_is_lowest_measured_score():
    tasks = [t("a", "see", 5), t("b", "read", 20)]
    trials = [trial("a"), trial("b", False)]
    guide = benchmark.guidance(benchmark.evaluate(tasks, trials, CONFIG), tasks, trials)
    assert guide["weakest_axis"] == "read"


def test_compare_reports_signed_deltas():
    new = {"overall": 40, "axes": {"see": {"score": 30}, "read": {"score": None}}}
    old = {"overall": 35, "axes": {"see": {"score": 20}, "read": {"score": 10}}}
    assert benchmark.compare(new, old) == {"overall_delta": 5, "axis_delta": {"see": 10}}


def test_render_shows_measured_axes_and_unmeasured_marker():
    report = benchmark.evaluate([t("a", "see", 40)], [trial("a")], CONFIG)
    text = benchmark.render(report, {"weakest_axis": "see", "blocked": {}})
    assert "see" in text and "40" in text and "측정 안 됨" in text and "█" in text


def _setup(monkeypatch, tmp_path, model="live-model", tasks=None):
    tasks_dir = tmp_path / "tasks"
    results = tmp_path / "results"
    tasks_dir.mkdir()
    results.mkdir()
    for task in tasks or [t("a", "see", 5)]:
        (tasks_dir / f"{task['id']}.json").write_text(json.dumps(task))
    (results / "20261028-000000.json").write_text(
        json.dumps({"versions": {"model": model}, "trials": [trial("a")]})
    )
    monkeypatch.setattr(benchmark, "TASKS", tasks_dir)
    monkeypatch.setattr(benchmark, "RESULTS", results)
    monkeypatch.setattr(benchmark, "BENCHMARKS", tmp_path / "benchmarks")
    monkeypatch.setattr(benchmark, "load_config", lambda: CONFIG)


def test_main_refuses_scripted_results(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, model="scripted")
    assert benchmark.main([]) == 2
    assert "--live" in capsys.readouterr().out
    assert not (tmp_path / "benchmarks").exists()


def test_main_writes_record_and_prints_path(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    assert benchmark.main([]) == 0
    record = next((tmp_path / "benchmarks").glob("*.json"))
    assert json.loads(record.read_text())["report"]["overall"] == 5
    assert record.name in capsys.readouterr().out


def test_main_without_results_explains_how_to_get_them(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    next((tmp_path / "results").glob("*.json")).unlink()
    assert benchmark.main([]) == 2
    assert "run_eval.py --live" in capsys.readouterr().out


# --- task files ---

TASK_FILES = sorted((ROOT / "evals" / "tasks").glob("*.json"))


def _tasks():
    return [json.loads(p.read_text(encoding="utf-8")) for p in TASK_FILES]


def test_every_task_is_classified():
    axes = benchmark.load_config()["axes"]
    for task in _tasks():
        if task.get("negative"):
            continue
        assert task.get("axis") in axes, task["id"]
        assert isinstance(task.get("level"), int) and 1 <= task["level"] <= 100, task["id"]
        assert task.get("status") in {"active", "target"}, task["id"]


def test_task_ids_unique_and_match_filename():
    ids = [t["id"] for t in _tasks()]
    assert len(ids) == len(set(ids))
    assert ids == [p.stem for p in TASK_FILES]


def test_target_tasks_have_expect_and_no_script():
    targets = [t for t in _tasks() if t.get("status") == "target"]
    assert targets
    for task in targets:
        assert "expect" in task and "script" not in task, task["id"]


def test_overall_is_100_only_when_every_axis_is_100():
    assert benchmark.overall({"a": 100, "b": 99}, {"a": 1, "b": 1}) == 99
    assert benchmark.overall({"a": 15, "b": 34}, {"a": 1, "b": 1}) == 25
    assert benchmark.overall({"a": 100, "b": 100}, {"a": 1, "b": 1}) == 100


def test_axis_with_only_unrun_targets_is_unmeasured():
    tasks = [t("a", "see", 5), t("b", "read", 40, status="target")]
    report = benchmark.evaluate(tasks, [trial("a")], CONFIG)
    assert report["axes"]["read"]["measured"] is False and report["overall"] == 5


def test_stale_tasks_detects_changed_definition():
    import hashlib

    task = t("a", "see", 5)
    good = hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()[:12]
    assert benchmark.stale_tasks([task], {"a": good}) == []
    assert benchmark.stale_tasks([{**task, "level": 6}], {"a": good}) == ["a"]


def test_latest_live_result_skips_newer_replay_files(tmp_path):
    (tmp_path / "1.json").write_text(json.dumps({"versions": {"model": "live"}, "trials": []}))
    (tmp_path / "2.json").write_text(json.dumps({"versions": {"model": "scripted"}, "trials": []}))
    assert benchmark.latest_live_result(tmp_path).name == "1.json"


def test_axis_with_a_run_target_is_measured():
    tasks = [t("a", "see", 5), t("b", "read", 40, status="target")]
    report = benchmark.evaluate(tasks, [trial("a"), trial("b")], CONFIG)
    assert report["axes"]["read"]["score"] == 40


def test_overall_100_needs_every_configured_axis_measured():
    assert benchmark.overall({"a": 100, "b": None}, {"a": 1, "b": 1}) == 99


def test_guidance_ignores_unclassified_tasks():
    tasks = [t("a", "see", 5), {"id": "bad", "axis": "see"}]
    trials = [trial("a"), trial("bad", False)]
    report = benchmark.evaluate(tasks, trials, CONFIG)
    assert benchmark.guidance(report, tasks, trials)["blocked"] == {}


def test_report_counts_invalid_trials():
    tasks = [t("a", "see", 5)]
    trials = [trial("a"), trial("a", False, False), trial("a", False, False)]
    report = benchmark.evaluate(tasks, trials, CONFIG)
    assert report["invalid_trials"] == 2 and "INVALID" in benchmark.render(
        report, {"weakest_axis": None, "blocked": {}}
    )


def test_main_does_not_overwrite_a_record_from_the_same_second(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(benchmark.time, "strftime", lambda fmt: fmt.replace("%Y%m%d-%H%M%S", "X"))
    benchmark.main([])
    benchmark.main([])
    assert len(list((tmp_path / "benchmarks").glob("*.json"))) == 2


def test_main_refuses_to_record_when_mostly_invalid(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    path = next((tmp_path / "results").glob("*.json"))
    path.write_text(json.dumps({"versions": {"model": "m"}, "trials": [trial("a", False, False)]}))
    assert benchmark.main([]) == 2
    assert not (tmp_path / "benchmarks").exists()


def _write_trials(tmp_path, trials):
    path = next((tmp_path / "results").glob("*.json"))
    path.write_text(json.dumps({"versions": {"model": "m"}, "trials": trials}))


def test_main_counts_invalid_over_all_trials_including_unclassified(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _write_trials(tmp_path, [trial("a"), trial("zzz", False, False), trial("zzz", False, False)])
    assert benchmark.main([]) == 2


def test_main_records_when_exactly_half_invalid(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _write_trials(tmp_path, [trial("a"), trial("a", False, False)])
    assert benchmark.main([]) == 0
