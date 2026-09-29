import json

import pytest

from verify_de import SETTING_FIELDS, isolated_config, run_case
from voice_action.config import load_config


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


@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["create", "overwrite"])
async def test_isolated_runtime_approval_and_idle_undo(config, tmp_path, scenario):
    row = await run_case(config, tmp_path / scenario, scenario)

    assert row["status"] == "passed"
    assert row["settings_equal"] and row["files_restored"]
    assert row["write_verified"] and row["undo_verified"] and row["second_undo_unchanged"]
    assert row["model_calls"] == 3 <= row["model_limit"]
    assert row["requested_tools"] == row["executed_tools"] == [
        "filesystem__read_text_file", "filesystem__write_file",
    ]
    assert row["tool_errors"] == row["cached_app_calls"] == 0
    assert row["total_ms"] >= row["llm_ms"] + row["mcp_ms"]
