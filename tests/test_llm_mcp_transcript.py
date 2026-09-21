import asyncio
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest
from rich.console import Console

import llm_mcp_transcript as transcript_module
from voice_action.mcp_client import ToolDefinition
from voice_action.planner import Completed, ModelTurn, PlannerError, ToolCall


class ScriptedModel:
    def __init__(self, result):
        self.result = result

    async def generate(self, *_args, **_kwargs):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


async def test_concurrent_runs_append_parseable_records_with_matched_boundaries(tmp_path):
    path = tmp_path / "transcript.jsonl"

    async def record(label):
        transcript = transcript_module.TranscriptLog(path)
        transcript.start(label)
        transcript.event("llm_input", turn=1, tool_names=[label])
        await asyncio.sleep(0)
        transcript.finish("completed", outcome=Completed(label))
        return transcript.run_id

    run_ids = await asyncio.gather(record("first"), record("second"))
    records = transcript_module.read_records(path)

    assert {record["run_id"] for record in records} == set(run_ids)
    assert all(record["status"] == "completed" for record in records)
    assert all(record["events"][0]["event"] == "start" for record in records)
    assert all("started_at" in record and "ended_at" in record for record in records)


async def test_default_transcript_excludes_raw_messages_results_and_arguments(tmp_path):
    path = tmp_path / "transcript.jsonl"
    transcript = transcript_module.TranscriptLog(path)
    model = transcript_module.TranscriptModel(
        ScriptedModel(
            ModelTurn(
                "FILE_BODY_SECRET-LIKE and WEB SNAPSHOT CONTENT",
                [
                    ToolCall(
                        "call-1",
                        "filesystem__write_file",
                        '{"path":"/private/secret.md","content":"FILE_BODY_SECRET-LIKE"}',
                    )
                ],
            )
        ),
        transcript,
    )
    transcript.start("SECRET-LIKE user message with WEB SNAPSHOT CONTENT")

    await model.generate(
        [{"role": "user", "content": "FILE_BODY_SECRET-LIKE"}],
        [ToolDefinition("filesystem__write_file", "Write", {"type": "object"})],
        allow_tools=True,
    )
    transcript.finish("completed", outcome=Completed("FILE_BODY_SECRET-LIKE"))

    logged = path.read_text(encoding="utf-8")
    record = transcript_module.read_records(path)[0]
    assert "FILE_BODY_SECRET-LIKE" not in logged
    assert "WEB SNAPSHOT CONTENT" not in logged
    assert '"path":"/private/secret.md"' not in logged
    assert record["events"][2]["tool_calls"][0]["arguments"]["chars"] > 0


async def test_raw_payload_opt_in_includes_diagnostic_payloads(tmp_path):
    path = tmp_path / "transcript.jsonl"
    transcript = transcript_module.TranscriptLog(path, include_raw_payloads=True)
    model = transcript_module.TranscriptModel(
        ScriptedModel(ModelTurn("diagnostic body", [ToolCall("call", "tool", "{}")])) ,
        transcript,
    )
    transcript.start("diagnostic request")

    await model.generate(
        [
            {"role": "user", "content": "diagnostic request"},
            {"role": "user", "content": "OPENAI_API_KEY=test-secret"},
        ],
        [ToolDefinition("tool", "Tool", {"type": "object"})],
        allow_tools=True,
    )
    transcript.finish("completed", outcome=Completed("diagnostic body"))

    logged = path.read_text(encoding="utf-8")
    assert "diagnostic request" in logged
    assert "diagnostic body" in logged
    assert "raw_messages" in logged
    assert "test-secret" not in logged


class FakeManager:
    @asynccontextmanager
    async def connect(self):
        yield self

    async def list_tools(self):
        return []


class FakeClient:
    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


@pytest.mark.parametrize(
    ("model_result", "expected_status", "raises"),
    [
        (ModelTurn("done", []), "completed", None),
        (PlannerError("planner failed"), "planner_error", None),
        (asyncio.CancelledError(), "cancelled", asyncio.CancelledError),
        (RuntimeError("unexpected"), "exception", RuntimeError),
    ],
)
async def test_run_records_exactly_one_terminal_status_for_each_exit(
    config, tmp_path, monkeypatch, model_result, expected_status, raises
):
    path = tmp_path / f"{expected_status}.jsonl"
    transcript = transcript_module.TranscriptLog(path)
    model = ScriptedModel(model_result)
    monkeypatch.setattr(transcript_module, "load_config", lambda _overrides: config)
    monkeypatch.setattr(transcript_module, "_build_mcp_manager", lambda _config: FakeManager())
    monkeypatch.setattr(transcript_module, "_build_undo_journal", lambda _config: None)
    monkeypatch.setattr(transcript_module, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(transcript_module, "OpenAICompatibleModel", lambda *_args: model)

    if raises is None:
        await transcript_module.run("command", {}, Console(), transcript)
    else:
        with pytest.raises(raises):
            await transcript_module.run("command", {}, Console(), transcript)

    records = transcript_module.read_records(path)
    assert len(records) == 1
    assert records[0]["status"] == expected_status


async def test_transcript_write_failure_does_not_mask_original_exception(
    config, tmp_path, monkeypatch
):
    transcript = transcript_module.TranscriptLog(tmp_path / "unwritable.jsonl")
    model = ScriptedModel(RuntimeError("original failure"))
    monkeypatch.setattr(transcript, "_append_record", Mock(side_effect=OSError("full")))
    monkeypatch.setattr(transcript_module, "load_config", lambda _overrides: config)
    monkeypatch.setattr(transcript_module, "_build_mcp_manager", lambda _config: FakeManager())
    monkeypatch.setattr(transcript_module, "_build_undo_journal", lambda _config: None)
    monkeypatch.setattr(transcript_module, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(transcript_module, "OpenAICompatibleModel", lambda *_args: model)

    with pytest.raises(RuntimeError, match="original failure"):
        await transcript_module.run("command", {}, Console(), transcript)


def test_cli_log_file_option_is_preserved(tmp_path, monkeypatch):
    captured = {}

    async def fake_run(_text, _overrides, _console, transcript):
        captured["path"] = transcript.path

    monkeypatch.setattr(transcript_module, "run", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        ["llm_mcp_transcript.py", "command", "--log-file", str(tmp_path / "custom.jsonl")],
    )

    assert transcript_module.main() == 0
    assert captured["path"] == tmp_path / "custom.jsonl"
