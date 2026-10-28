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


def test_unmeasured_low_level_stops_the_ladder():
    tasks = [t("easy", "see", 1), t("hard", "see", 90)]
    report = benchmark.evaluate(tasks, [trial("easy", False, False), trial("hard")], CONFIG)
    assert report["axes"]["see"]["score"] == 0


def test_perfect_run_of_every_task_scores_100_overall():
    config = benchmark.load_config()
    tasks = [x for x in _tasks() if not x.get("negative")]
    trials = [trial(x["id"]) for x in tasks]
    assert benchmark.evaluate(tasks, trials, config)["overall"] == 100


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
        "task": "b", "level": 9, "rate": 0.0, "problems": ["files: x"], "kind": "active"
    }


def test_guidance_weakest_axis_is_lowest_measured_score():
    tasks = [t("a", "see", 5), t("b", "read", 20)]
    trials = [trial("a"), trial("b", False)]
    guide = benchmark.guidance(benchmark.evaluate(tasks, trials, CONFIG), tasks, trials)
    assert guide["weakest_axis"] == "read"


def test_compare_reports_signed_deltas():
    new = {"overall": 40, "axes": {"see": {"score": 30}}}
    old = {"overall": 35, "axes": {"see": {"score": 20}}}
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


def test_every_axis_ladder_is_filled_from_easy_to_hard():
    config = benchmark.load_config()
    for axis in config["axes"]:
        levels = sorted(
            t["level"] for t in _tasks() if not t.get("negative") and t.get("axis") == axis
        )
        assert len(levels) >= 8, axis
        assert levels[0] <= 15 and levels[-1] == 100, axis
        assert max(b - a for a, b in zip(levels, levels[1:], strict=False)) <= 25, axis


def test_active_tasks_have_scripts_so_replay_checks_the_grader():
    for task in _tasks():
        if task.get("status") == "active" and not task.get("negative"):
            assert "script" in task, task["id"]


def test_guidance_marks_unbuilt_targets_apart_from_broken_tasks():
    tasks = [t("a", "see", 5), t("b", "read", 40, status="target")]
    trials = [trial("a"), trial("b", False)]
    guide = benchmark.guidance(benchmark.evaluate(tasks, trials, CONFIG), tasks, trials)
    assert guide["blocked"]["read"]["kind"] == "target"


def test_render_lists_next_goals_by_level_with_cost():
    tasks = [t("a", "see", 30), t("b", "read", 10)]
    trials = [{**trial("a", False), "model_calls": 3, "seconds": 120},
              {**trial("b", False), "model_calls": 2, "seconds": 60}]
    report = benchmark.evaluate(tasks, trials, CONFIG)
    text = benchmark.render(report, benchmark.guidance(report, tasks, trials))
    assert text.index("b (read 10") < text.index("a (see 30")
    assert "모델 호출 5회" in text and "3분" in text


def test_compare_is_skipped_when_the_ladder_changed(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"report": {"ladder": 1, "overall": 1, "axes": {}}}))
    assert benchmark.main(["--compare", str(old)]) == 0
    assert "사다리가 달라" in capsys.readouterr().out


def test_no_save_keeps_no_record(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert benchmark.main(["--no-save"]) == 0
    assert not (tmp_path / "benchmarks").exists()


def test_render_shortens_long_name_lists():
    report = benchmark.evaluate([t("a", "see", 5)], [trial("a")], CONFIG)
    report["not_run"] = [f"t{i}" for i in range(9)]
    line = [x for x in benchmark.render(report, {"weakest_axis": None, "blocked": {}}).split("\n")
            if x.startswith("미실행")][0]
    assert "외 4개" in line and "t8" not in line


def test_tasks_edited_after_the_run_are_not_scored(monkeypatch, tmp_path, capsys):
    import hashlib

    _setup(monkeypatch, tmp_path)
    task = t("a", "see", 5)
    edited = json.dumps({**task, "level": 9}, sort_keys=True).encode()
    stale = hashlib.sha256(edited).hexdigest()[:12]
    path = next((tmp_path / "results").glob("*.json"))
    versions = {"model": "m", "tasks": {"a": stale}}
    path.write_text(json.dumps({"versions": versions, "trials": [trial("a")]}))
    benchmark.main(["--no-save"])
    out = capsys.readouterr().out
    assert "측정 안 됨" in out and "측정 뒤 바뀐 과제" in out


def test_task_files_with_reply_checks_are_well_formed():
    for task in _tasks():
        expect = task["expect"]
        for key in ("reply_contains", "reply_excludes", "reply_order"):
            assert all(isinstance(w, str) and w for w in expect.get(key, [])), (task["id"], key)


def test_live_false_tasks_are_not_scored():
    tasks = [t("a", "see", 5), t("pinned", "see", 6, live=False)]
    report = benchmark.evaluate(tasks, [trial("a")], CONFIG)
    assert report["axes"]["see"]["score"] == 5 and report["not_run"] == []


def test_guidance_lists_the_unmeasured_level_that_blocks_the_score():
    tasks = [t("easy", "see", 1), t("mid", "see", 50)]
    trials = [trial("easy", False, False), trial("mid")]
    report = benchmark.evaluate(tasks, trials, CONFIG)
    blocked = benchmark.guidance(report, tasks, trials)["blocked"]["see"]
    assert (blocked["task"], blocked["kind"], blocked["rate"]) == ("easy", "unmeasured", None)
    assert "다시 잴 것: easy" in benchmark.render(report, benchmark.guidance(report, tasks, trials))


def test_one_unmeasured_task_blocks_its_level_even_among_passing_ones():
    tasks = [t(f"l{i}", "see", 10) for i in range(5)] + [t("next", "see", 20)]
    trials = [trial(f"l{i}") for i in range(4)] + [trial("next")]
    assert benchmark.evaluate(tasks, trials, CONFIG)["axes"]["see"]["score"] == 0


def test_compare_skips_overall_when_measured_axes_differ():
    new = {"overall": 10, "axes": {"see": {"score": 10}, "read": {"score": None}}}
    old = {"overall": 55, "axes": {"see": {"score": 10}, "read": {"score": 100}}}
    assert benchmark.compare(new, old) == {"overall_delta": None, "axis_delta": {"see": 0}}


def test_each_axis_has_a_demanding_top_of_the_ladder():
    """Finishing a ladder must mean something: several hard rungs, not one token task."""
    for axis in benchmark.load_config()["axes"]:
        top = [t for t in _tasks() if not t.get("negative") and t.get("axis") == axis
               and t["level"] >= 85]
        assert len(top) >= 3, axis
        assert any(t["level"] == 100 for t in top), axis


def test_the_top_of_the_ladders_covers_real_world_difficulty():
    top = [t for t in _tasks() if not t.get("negative") and t["level"] >= 60]
    assert sum(1 for t in top if len(t["files"]) >= 8) >= 3          # folders of real size
    assert any(isinstance(v, dict) for t in top for v in t["files"].values())  # real file types
    assert sum(1 for t in top if t.get("ages")) >= 5                  # date-based work
    assert sum(1 for t in top if t.get("followups") or t["expect"].get("undo")) >= 2  # multi-step
    assert any(t["expect"].get("reply_excludes") for t in top)        # traps, not just hits


def test_latest_live_result_skips_results_of_other_task_folders(tmp_path):
    for name, task_id in (("1.json", "a"), ("2.json", "a~L2")):
        (tmp_path / name).write_text(json.dumps(
            {"versions": {"model": "live"}, "trials": [trial(task_id)]}))
    assert benchmark.latest_live_result(tmp_path, {"a"}).name == "1.json"


def test_main_refuses_a_result_from_another_task_folder(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    _write_trials(tmp_path, [trial("zzz~L2")])
    path = next((tmp_path / "results").glob("*.json"))
    assert benchmark.main(["--no-save", "--result", str(path)]) == 2
    assert "다른 과제 폴더" in capsys.readouterr().out


def test_compare_accepts_a_raw_run_eval_result(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    before = tmp_path / "before.json"
    before.write_text(json.dumps({"versions": {"model": "m"}, "trials": [trial("a", False)]}))
    assert benchmark.main(["--no-save", "--compare", str(before)]) == 0
    assert "(+5)" in capsys.readouterr().out


def test_compare_refuses_a_replay_result(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    before = tmp_path / "before.json"
    before.write_text(json.dumps({"versions": {"model": "scripted"}, "trials": [trial("a")]}))
    assert benchmark.main(["--no-save", "--compare", str(before)]) == 2
    assert "재생 결과" in capsys.readouterr().out


def test_compare_leaves_out_tasks_edited_since_the_earlier_run(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    before = tmp_path / "before.json"
    versions = {"model": "m", "tasks": {"a": "000000000000"}}
    before.write_text(json.dumps({"versions": versions, "trials": [trial("a", False)]}))
    assert benchmark.main(["--no-save", "--compare", str(before)]) == 0
    assert "(+" not in capsys.readouterr().out  # an edited task: nothing is compared


def test_compare_skips_a_saved_record_when_tasks_changed_since(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    record = tmp_path / "record.json"
    report = {"ladder": None, "overall": 1, "axes": {"see": {"score": 1}}}
    record.write_text(json.dumps({"report": report, "versions": {"tasks": {"a": "000000000000"}}}))
    assert benchmark.main(["--no-save", "--compare", str(record)]) == 0
    out = capsys.readouterr().out
    assert "바뀌거나 지워져" in out and "(+" not in out


def test_compare_skips_when_a_task_was_removed_since(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    before = tmp_path / "before.json"
    versions = {"model": "m", "tasks": {"gone": "000000000000"}}
    before.write_text(json.dumps({"versions": versions, "trials": [trial("gone", False)]}))
    assert benchmark.main(["--no-save", "--compare", str(before)]) == 0
    out = capsys.readouterr().out
    assert "바뀌거나 지워져" in out and "(+" not in out


def test_compare_skips_when_this_result_has_tasks_edited_since_its_run(
        monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    path = next((tmp_path / "results").glob("*.json"))
    path.write_text(json.dumps({"versions": {"model": "m", "tasks": {"a": "000000000000"}},
                                "trials": [trial("a")]}))
    before = tmp_path / "before.json"
    before.write_text(json.dumps({"versions": {"model": "m"}, "trials": [trial("a", False)]}))
    benchmark.main(["--no-save", "--compare", str(before)])
    assert "(+" not in capsys.readouterr().out
