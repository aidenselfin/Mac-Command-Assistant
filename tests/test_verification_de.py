import json

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from verify_de import SETTING_FIELDS, environment_cause, error_summary, isolated_config, run_case
from voice_action.config import load_config
from voice_action.mcp_client import MCPConnectionError, ToolRejectedError
from voice_action.planner import PlannerError


def test_isolation_inherits_effective_settings_through_environment(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "voice-action-test").mkdir()
    (project / ".voice-action").mkdir()
    (project / ".voice-action/config.env").write_text("GROQ_API_KEY=test-only\n")
    (project / "config.json").write_text(json.dumps({
        "provider": "groq", "model": "project-model",
        "base_url": "https://example.test/v1", "max_tool_rounds": 4,
        "llm_timeout_seconds": 17, "mcp_timeout_seconds": 19,
    }))
    baseline = load_config(cwd=project, home=tmp_path / "home", environ={})
    cwd = tmp_path / "isolated"
    cwd.mkdir()
    root = cwd / "allowed"
    root.mkdir()

    isolated = isolated_config(baseline, cwd, root)

    assert all(getattr(isolated, name) == getattr(baseline, name) for name in SETTING_FIELDS)
    assert isolated.allowed_directories == (root,)
    assert not (project / ".voice-action/undo.sqlite3").exists()


def _planner_api_error(status):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    try:
        raise PlannerError("LLM API 요청 실패") from APIStatusError(
            "x", response=httpx.Response(status, request=request), body=None
        )
    except PlannerError as exc:
        return exc


def test_environment_cause_separates_service_failures_from_functional_ones():
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    connection = PlannerError("LLM API 요청 실패")
    connection.__cause__ = APIConnectionError(request=request)

    assert environment_cause(_planner_api_error(429)) == "llm_http_429"
    assert environment_cause(_planner_api_error(503)) == "llm_http_503"
    assert environment_cause(connection) == "llm_APIConnectionError"
    assert environment_cause(MCPConnectionError("down")) == "mcp_MCPConnectionError"
    assert environment_cause(_planner_api_error(400)) is None
    assert environment_cause(ToolRejectedError("policy")) is None
    assert environment_cause(AssertionError()) is None


def test_error_summary_unwraps_task_group_to_the_api_status():
    summary = error_summary(ExceptionGroup("mcp", [_planner_api_error(400)]))

    assert (summary["type"], summary["status_code"]) == ("APIStatusError", 400)


@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["create", "overwrite"])
async def test_isolated_runtime_approval_and_idle_undo(config, tmp_path, scenario):
    row = await run_case(config, tmp_path / scenario, scenario)

    assert row["status"] == "passed"
    assert row["settings_equal"] and row["files_restored"] and not row["changed_files"]
    assert row["write_verified"] and row["undo_verified"] and row["second_undo_unchanged"]
    assert row["approval"] == "approved" and not row["budget_exhausted_after_write"]
    assert row["model_calls"] == 3 <= row["model_limit"]
    assert row["requested_tools"] == row["executed_tools"] == [
        "filesystem__read_text_file", "filesystem__write_file",
    ]
    assert row["tool_errors"] == row["policy_rejections"] == row["cached_app_calls"] == 0
    assert row["total_ms"] >= row["llm_ms"] + row["mcp_ms"]
