"""EvalRunner review items (docs/reviews/2026-10-04-recent-changes-review.md, section 7)."""

import importlib.util
import json
from pathlib import Path

import pytest

from voice_action.config import Config
from voice_action.planner import ModelTurn

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_eval", ROOT / "evals" / "run_eval.py")
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)


def task(**extra):
    return {
        "id": "t",
        "files": {"notes.txt": "x"},
        "request": "notes.txt 내용 알려줘",
        "expect": {"files": {}},
        **extra,
    }


class Raising:
    calls = 0

    async def generate(self, *args, **kwargs):
        raise RuntimeError("Authorization: Bearer sk-live-SECRET request body")


class FakeClient:
    closed = False

    async def close(self):
        FakeClient.closed = True


class Answering:
    calls = 0

    async def generate(self, *args, **kwargs):
        Answering.calls += 1
        return ModelTurn("x입니다.", [])


@pytest.fixture(autouse=True)
def _no_heavy_lock(monkeypatch):
    # The real lock waits for any live eval on this machine: one test once took 28 minutes.
    monkeypatch.setattr(run_eval.heavy_lock, "acquire", lambda: None)


def live_config():
    return Config("openai", "live-model", "unused-key", (ROOT,), feedback_enabled=False)


@pytest.mark.integration
async def test_provider_error_text_never_reaches_results(monkeypatch):
    monkeypatch.setattr(run_eval, "live_model", lambda config: (Raising(), FakeClient()))
    result = await run_eval.run_trial(task(), live_config(), [])
    assert result["status"] == "provider_failure"
    assert "SECRET" not in json.dumps(result) and "Bearer" not in json.dumps(result)


@pytest.mark.integration
async def test_live_client_is_closed_after_each_trial(monkeypatch):
    FakeClient.closed = False
    monkeypatch.setattr(run_eval, "live_model", lambda config: (Answering(), FakeClient()))
    result = await run_eval.run_trial(task(), live_config(), [])
    assert result["status"] == "completed" and FakeClient.closed


@pytest.mark.integration
async def test_required_tool_must_actually_run():
    script = [{"text": "요약했습니다."}]  # answers without reading the file
    result = await run_eval.run_trial(
        task(script=script, expect={"files": {}, "must_run": ["filesystem__read_text_file"]}),
        None,
        [],
    )
    assert not result["passed"] and result["tools_run"] == []
    assert result["graded"] == "state_and_side_effects"


async def test_live_runs_leave_out_negative_control_tasks(monkeypatch):
    seen = []

    async def fake_trial(t, config, lessons):
        seen.append(t["id"])
        return {"task": t["id"], "passed": True, "valid": True, "status": "completed",
                "model_calls": 0, "problems": [], "violations": []}

    monkeypatch.setattr(run_eval, "run_trial", fake_trial)
    monkeypatch.setattr(run_eval, "load_config", live_config)
    monkeypatch.setattr(run_eval, "RESULTS", ROOT / "evals" / "results")
    monkeypatch.setattr(run_eval, "versions", lambda *a: {})
    await run_eval.main(["--live"])
    controls = {
        json.loads(p.read_text())["id"]
        for p in (ROOT / "evals" / "tasks").glob("*.json")
        if json.loads(p.read_text()).get("negative")
    }
    assert controls and not controls & set(seen)
    for out in (ROOT / "evals" / "results").glob("*.json"):
        if json.loads(out.read_text()).get("versions") == {}:
            out.unlink()  # the stub run's results file


async def test_trial_result_carries_axis_and_level():
    result = await run_eval.run_trial(
        task(script=[{"text": "x"}], axis="see", level=10), None, []
    )
    assert (result["axis"], result["level"]) == ("see", 10)


async def _main_seen(monkeypatch, tmp_path, argv):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for name, extra in {"a": {}, "b": {"status": "target"}}.items():
        (tasks_dir / f"{name}.json").write_text(json.dumps(task(id=name, **extra)))
    seen = []

    async def fake_trial(t, config, lessons):
        seen.append(t["id"])
        return {"task": t["id"], "passed": True, "valid": True, "status": "completed",
                "model_calls": 0, "problems": [], "violations": []}

    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    monkeypatch.setattr(run_eval, "TASKS", tasks_dir)
    monkeypatch.setattr(run_eval, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(run_eval, "run_trial", fake_trial)
    monkeypatch.setattr(run_eval, "load_config", live_config)
    monkeypatch.setattr(run_eval, "versions", lambda *a: {})
    await run_eval.main(argv)
    return seen


async def test_main_skips_target_tasks_by_default(monkeypatch, tmp_path):
    assert await _main_seen(monkeypatch, tmp_path, ["--live"]) == ["a"]


async def test_main_include_targets_runs_them(monkeypatch, tmp_path):
    seen = await _main_seen(monkeypatch, tmp_path, ["--live", "--include-targets"])
    assert seen == ["a", "b"]


async def test_reply_contains_grades_the_answer():
    base = {"script": [{"text": "파일은 a.txt, b.md 입니다."}]}
    ok = await run_eval.run_trial(
        task(**base, expect={"files": {}, "reply_contains": ["a.txt", "b.md"]}), None, []
    )
    bad = await run_eval.run_trial(
        task(**base, expect={"files": {}, "reply_contains": ["c.csv"]}), None, []
    )
    assert ok["passed"] and not bad["passed"]
    assert bad["problems"] == ["reply: missing 'c.csv'"]


async def test_reply_order_and_excludes_are_graded():
    base = {"script": [{"text": "b.txt, c.txt, a.txt 순서입니다."}]}
    ok = await run_eval.run_trial(
        task(**base, expect={"files": {}, "reply_order": ["b.txt", "c.txt", "a.txt"],
                             "reply_excludes": ["old.txt"]}), None, [])
    bad = await run_eval.run_trial(
        task(**base, expect={"files": {}, "reply_order": ["a.txt", "b.txt"],
                             "reply_excludes": ["c.txt"]}), None, [])
    assert ok["passed"] and not bad["passed"] and len(bad["problems"]) == 2


async def test_binary_fixture_and_files_contain_are_graded():
    import base64

    blob = base64.b64encode(b"\x89PNG\x00\x01").decode()
    script = [{"calls": [{"name": "filesystem__write_file",
                          "args": {"path": "{root}/out.md", "content": "매출 120억, 비용 30억"}}]},
              {"text": "저장했습니다."}]
    base = {"files": {"pic.png": {"b64": blob}}, "script": script, "user": {"approve": [True]}}
    ok = await run_eval.run_trial(task(**base, expect={
        "files": {"out.md": "*"}, "files_contain": {"out.md": ["120억", "30억"]}}), None, [])
    bad = await run_eval.run_trial(task(**base, expect={
        "files": {"out.md": "*"}, "files_contain": {"out.md": ["999억"]}}), None, [])
    assert ok["passed"] and not bad["passed"]
    assert bad["problems"] == ["out.md: missing '999억'"]


async def test_reply_order_allows_an_intro_that_mentions_a_name_first():
    script = [{"text": "a.txt를 포함해 정렬하면 b.txt, c.txt, a.txt 순입니다."}]
    result = await run_eval.run_trial(
        task(script=script, expect={"files": {}, "reply_order": ["b.txt", "c.txt", "a.txt"]}),
        None, [])
    assert result["passed"]


async def test_absent_dirs_catches_a_leftover_folder():
    script = [{"calls": [{"name": "local__create_directory", "args": {"path": "{root}/정리"}}]},
              {"text": "만들었습니다."}]
    result = await run_eval.run_trial(
        task(script=script, request="정리 폴더를 만들어줘", user={"approve": [True]},
             expect={"files": {}, "absent_dirs": ["정리"]}), None, [])
    assert not result["passed"] and "정리: directory should not exist" in result["problems"]


async def test_run_stops_after_three_provider_failures_in_a_row(monkeypatch, tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for i in range(6):
        (tasks_dir / f"t{i}.json").write_text(json.dumps(task(id=f"t{i}")))
    seen = []

    async def failing_trial(t, config, lessons):
        seen.append(t["id"])
        return {"task": t["id"], "passed": False, "valid": False, "status": "provider_failure",
                "model_calls": 1, "problems": [], "violations": [], "reply": "LLM 인증 실패"}

    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    monkeypatch.setattr(run_eval, "TASKS", tasks_dir)
    monkeypatch.setattr(run_eval, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(run_eval, "run_trial", failing_trial)
    monkeypatch.setattr(run_eval, "load_config", live_config)
    monkeypatch.setattr(run_eval, "versions", lambda *a: {})
    await run_eval.main(["--live"])
    assert len(seen) == 3


@pytest.mark.integration
async def test_live_trial_records_what_the_model_heard_and_said(monkeypatch):
    monkeypatch.setattr(run_eval, "live_model", lambda config: (Answering(), FakeClient()))
    result = await run_eval.run_trial(task(), live_config(), [])
    assert result["trace"] and result["trace"][0]["said"] == "x입니다."


async def test_ages_set_modification_time_before_the_run(monkeypatch):
    seen = {}
    real = run_eval.manifest

    def spy(root):
        seen.setdefault("age", (__import__("time").time() - (root / "notes.txt").stat().st_mtime))
        return real(root)

    monkeypatch.setattr(run_eval, "manifest", spy)
    await run_eval.run_trial(task(script=[{"text": "x"}], ages={"notes.txt": 5}), None, [])
    assert 5 * 86400 - 60 < seen["age"] < 5 * 86400 + 600


async def test_axis_filter_runs_only_that_axis(monkeypatch, tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for name, axis in {"a": "see", "b": "read"}.items():
        (tasks_dir / f"{name}.json").write_text(json.dumps(task(id=name, axis=axis)))
    seen = []

    async def fake_trial(t, config, lessons):
        seen.append(t["id"])
        return {"task": t["id"], "passed": True, "valid": True, "status": "completed",
                "model_calls": 0, "problems": [], "violations": []}

    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    monkeypatch.setattr(run_eval, "TASKS", tasks_dir)
    monkeypatch.setattr(run_eval, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(run_eval, "run_trial", fake_trial)
    monkeypatch.setattr(run_eval, "load_config", live_config)
    monkeypatch.setattr(run_eval, "versions", lambda *a: {})
    await run_eval.main(["--live", "--axis", "read"])
    assert seen == ["b"]


def test_code_hash_covers_entry_point_and_harness():
    names = {p.name for p in run_eval.code_files()}
    assert {"main.py", "run_eval.py", "planner.py"} <= names


def test_auto_answer_picks_the_option_the_request_names_and_gives_up_otherwise():
    from types import SimpleNamespace

    ask = lambda *options: SimpleNamespace(options=options)  # noqa: E731
    t = {"request": "a.txt 지워줘", "expect": {"files": {}}}
    assert run_eval.auto_answer(ask("/r/a_old.txt", "/r/a.txt"), t, Path("/r")) == "2번"
    assert run_eval.auto_answer(ask("/r/x.txt", "/r/y.txt"), t, Path("/r")) is None
    assert "/r" in run_eval.auto_answer(ask(), t, Path("/r"))
