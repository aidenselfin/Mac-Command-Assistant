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


def test_code_hash_covers_entry_point_and_harness():
    names = {p.name for p in run_eval.code_files()}
    assert {"main.py", "run_eval.py", "planner.py"} <= names
