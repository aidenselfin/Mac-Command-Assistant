"""Opt-in isolated acceptance run; no raw model output or credentials in reports."""

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from openai import APIError, AsyncOpenAI

from llm_mcp_transcript import TranscriptLog, TranscriptModel, read_records
from main import _build_mcp_manager, _build_undo_journal
from voice_action.config import load_config
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient, MCPError
from voice_action.mcp_manager import MCPManager
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    Completed,
    ModelTurn,
    OpenAICompatibleModel,
    Planner,
    PlannerError,
    PlannerEventType,
    PlannerTiming,
    ToolCall,
)
from voice_action.runtime import RuntimeController, RuntimeState

SETTING_FIELDS = (
    "provider", "model", "base_url", "llm_timeout_seconds", "mcp_timeout_seconds",
    "max_tool_rounds", "max_tool_result_chars",
)
ORIGINAL = "원본 bytes 😀\r\n끝\n".encode()
SOURCE = "회의 결정: 금요일 배포. 담당자는 민수.\n"


def is_environment_error(exc):
    if isinstance(exc, BaseExceptionGroup):
        return all(is_environment_error(item) for item in exc.exceptions)
    if isinstance(exc, (APIError, MCPError, OSError)):
        return True
    return isinstance(exc, PlannerError) and isinstance(exc.__cause__, APIError)


def isolated_config(baseline, cwd, root):
    """Pass effective settings through the existing environment interface."""
    env = {
        f"VOICE_ACTION_{name.upper()}": str(getattr(baseline, name))
        for name in SETTING_FIELDS if getattr(baseline, name) is not None
    }
    key_name = "GROQ_API_KEY" if baseline.provider == "groq" else "OPENAI_API_KEY"
    env[key_name] = baseline.api_key
    env["VOICE_ACTION_ALLOWED_DIRECTORIES"] = json.dumps([str(root)])
    config = load_config(cwd=cwd, home=cwd / "isolated-home", environ=env)
    assert all(getattr(config, name) == getattr(baseline, name) for name in SETTING_FIELDS)
    assert config.api_key == baseline.api_key
    assert config.allowed_directories == (root.resolve(),)
    return config


def file_state(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    }


class Measurements:
    def __init__(self):
        self.executed = []
        self.tool_ms = 0.0
        self.started = 0.0
        self.tool_errors = 0

    def event(self, event):
        if event.type is PlannerEventType.TOOL_STARTED:
            self.executed.append(event.tool_name)
            self.started = time.perf_counter()
        elif event.type is PlannerEventType.TOOL_FINISHED:
            self.tool_ms += (time.perf_counter() - self.started) * 1000

    def log(self, text):
        # Never persist tool values, model text, exceptions, or raw arguments.
        if text.startswith("[TOOL-ERROR]"):
            self.tool_errors += 1


class ScriptedWriteModel:
    def __init__(self, root, target):
        self.turns = iter([
            ModelTurn(None, [ToolCall(
                "read", "filesystem__read_text_file",
                json.dumps({"path": str(root / "source.txt")}),
            )]),
            ModelTurn(None, [ToolCall(
                "write", "filesystem__write_file",
                json.dumps({"path": str(target), "content": "금요일 배포"}),
            )]),
            ModelTurn("저장했습니다.", []),
        ])

    async def generate(self, messages, tools, *, allow_tools, allow_clarification=True):
        return next(self.turns)


def prepare_case(cwd, scenario):
    cwd.mkdir()
    root = cwd / "allowed"
    root.mkdir()
    (root / "source.txt").write_text(SOURCE, encoding="utf-8")
    (root / "test1.md").write_text(SOURCE, encoding="utf-8")
    (root / "sentinel.txt").write_bytes(b"must remain unchanged")
    if scenario == "overwrite":
        (root / "target.txt").write_bytes(ORIGINAL)
    for index in range({"meeting_one": 1, "meeting_many": 2}.get(scenario, 0)):
        (root / f"회의록-{index + 1}.md").write_text(SOURCE, encoding="utf-8")
    return root


COMMANDS = {
    "create": "source.txt를 요약해서 target.txt에 저장해줘",
    "overwrite": "source.txt를 요약해서 기존 target.txt에 덮어써줘",
    "listing": "파일 목록 보여줘",
    "read": "test1.md 내용을 읽어줘",
    "summary": "test1.md를 요약해줘",
    "search": "회의록 파일을 찾아줘",
    "meeting_zero": "회의록 요약해줘",
    "meeting_one": "회의록 요약해줘",
    "meeting_many": "회의록 요약해줘",
    "missing_source": "result.txt에 요약을 저장해줘",
    "web_read": "https://example.com 페이지를 읽고 요약해줘",
    "web_particle": "https://example.com을 읽고 summary.md에 저장해줘",
    "web_space": "https://example.com 읽고 summary.md에 저장해줘",
    "web_compare": "https://example.com 페이지와 test1.md를 비교해줘",
    "active_app": "현재 활성 앱을 알려줘",
    "app_listing": "현재 활성 앱과 파일 목록을 알려줘",
}


async def run_case(baseline, cwd, scenario, *, live=False):
    root = await asyncio.to_thread(prepare_case, cwd, scenario)
    config = isolated_config(baseline, cwd, root)
    previous_cwd = Path.cwd()
    os.chdir(cwd)
    transcript = TranscriptLog(cwd / "transcript.jsonl")
    metrics, timing = Measurements(), PlannerTiming.empty()
    before = await asyncio.to_thread(file_state, root)
    started = time.perf_counter()
    row = {
        "scenario": scenario, "command": COMMANDS[scenario], "run_id": transcript.run_id,
        "mode": "live" if live else "scripted", "fixture": before,
        "cwd": str(cwd), "allowed_root": str(root), "settings_equal": True,
        "status": "unverified", "approval": False,
    }
    transcript.start(COMMANDS[scenario])
    try:
        journal = await asyncio.to_thread(_build_undo_journal, config)
        assert journal is not None
        assert journal.path == cwd / ".voice-action/undo.sqlite3"
        row["journal"] = str(journal.path)
        manager = _build_mcp_manager(config) if live else MCPManager({
            "filesystem": MCPClient(config, allowed_tools=SUPPORTED_TOOLS),
        })
        async with manager.connect():
            async with AsyncOpenAI(
                api_key=config.api_key, base_url=config.base_url,
                timeout=config.llm_timeout_seconds, max_retries=0,
            ) as client:
                model = OpenAICompatibleModel(client, config.model) if live else ScriptedWriteModel(
                    root, root / "target.txt"
                )
                core = Planner(
                    config, manager, TranscriptModel(model, transcript), timing=timing,
                    undo_journal=journal, emit=metrics.log,
                )
                controller = RuntimeController(core, planner_event_observer=metrics.event)
                outcome = await controller.submit(COMMANDS[scenario])
                row["initial_outcome"] = type(outcome).__name__
                assert await asyncio.to_thread(file_state, root) == before
                assert await asyncio.to_thread(journal.entry_count) == 0
                if scenario in {"create", "overwrite"}:
                    assert isinstance(outcome, ApprovalRequired)
                    assert controller.state is RuntimeState.WAITING_APPROVAL
                    assert outcome.target_path == str(root / "target.txt")
                    assert outcome.operation == scenario
                    # Bind the single approval to the exact inspected payload in memory.
                    arguments = json.loads(outcome.context.prepared_write.canonical_arguments)
                    expected = arguments["content"].encode()
                    assert expected and expected != (ORIGINAL if scenario == "overwrite" else b"")
                    row["approval"] = True
                    outcome = await controller.approve_pending()
                    assert isinstance(outcome, Completed)
                    assert controller.state is RuntimeState.IDLE
                    assert await asyncio.to_thread((root / "target.txt").read_bytes) == expected
                    assert await asyncio.to_thread(journal.entry_count, status="active") == 1
                    row["write_verified"] = True
                    calls_before_undo = len(timing.llm_round_ms)
                    undone = await controller.undo_last_write()
                    assert isinstance(undone, Completed)
                    assert controller.state is RuntimeState.IDLE
                    assert await asyncio.to_thread(file_state, root) == before
                    assert await asyncio.to_thread(journal.entry_count, status="undone") == 1
                    second = await controller.undo_last_write()
                    assert second == Completed("되돌릴 수 있는 최근 파일 쓰기가 없습니다.")
                    assert await asyncio.to_thread(file_state, root) == before
                    assert len(timing.llm_round_ms) == calls_before_undo
                    row.update(undo_verified=True, second_undo_unchanged=True)
                else:
                    names = metrics.executed
                    expected_tool = {
                        "listing": "filesystem__list_directory",
                        "read": "filesystem__read_text_file",
                        "summary": "filesystem__read_text_file",
                        "search": "filesystem__search_files",
                        "active_app": "macos__get_active_app",
                    }.get(scenario)
                    if expected_tool:
                        assert isinstance(outcome, Completed) and expected_tool in names
                    if scenario.startswith("meeting_"):
                        assert "filesystem__search_files" in names
                        if scenario == "meeting_many":
                            assert isinstance(outcome, Clarification) and len(outcome.options) == 2
                        else:
                            assert isinstance(outcome, Completed)
                        assert ("filesystem__read_text_file" in names) == (
                            scenario == "meeting_one"
                        )
                    if scenario.startswith("web_"):
                        assert names[:2] == ["web__browser_navigate", "web__browser_snapshot"]
                        if scenario in {"web_particle", "web_space"}:
                            assert isinstance(outcome, ApprovalRequired)
                            await controller.reject_pending()
                        else:
                            assert isinstance(outcome, Completed)
                        if scenario == "web_compare":
                            assert "filesystem__read_text_file" in names
                    if scenario == "missing_source":
                        assert isinstance(outcome, (Completed, Clarification))
                        assert "filesystem__write_file" not in names
                    if scenario == "app_listing":
                        assert isinstance(outcome, Completed)
                        assert "macos__get_active_app" in names
                        assert "filesystem__list_directory" in names
                    assert await asyncio.to_thread(journal.entry_count) == 0
                row["outcome"] = type(outcome).__name__
                assert len(timing.llm_round_ms) <= config.max_tool_rounds
                row["status"] = "passed"
    except AssertionError:
        row["status"] = "functional_failure"
    except Exception as exc:
        # No exception text, body, header, key, or raw provider response is recorded.
        row["status"] = "environment_blocked" if is_environment_error(exc) else "functional_failure"
    finally:
        transcript.finish(row["status"])
        record = (await asyncio.to_thread(read_records, transcript.path))[0]
        events = record["events"]
        outputs = [event for event in events if event["event"] == "llm_output"]
        requested = [call["name"] for event in outputs for call in event["tool_calls"]]
        app_requests = requested.count("macos__get_active_app")
        row.update(
            model_calls=sum(event["event"] == "llm_input" for event in events),
            model_limit=config.max_tool_rounds, requested_tools=requested,
            executed_tools=metrics.executed, tool_errors=metrics.tool_errors,
            cached_app_calls=max(0, app_requests - metrics.executed.count("macos__get_active_app")),
            llm_ms=round(sum(timing.llm_round_ms), 3),
            mcp_ms=round(metrics.tool_ms, 3),
            total_ms=round((time.perf_counter() - started) * 1000, 3),
            files_restored=(await asyncio.to_thread(file_state, root)) == before,
        )
        os.chdir(previous_cwd)
    return row


async def run_acceptance(live):
    baseline = load_config()
    project = Path.cwd()
    work = await asyncio.to_thread(
        lambda: Path(tempfile.mkdtemp(prefix="voice-action-de-")).resolve()
    )
    protected = [*baseline.allowed_directories, project / ".voice-action"]
    before = [await asyncio.to_thread(file_state, path) for path in protected]
    settings = {name: getattr(baseline, name) for name in SETTING_FIELDS}
    if settings["base_url"]:
        parts = urlsplit(settings["base_url"])
        settings["base_url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    report = {"settings": settings, "mode": "live" if live else "scripted", "runs": []}
    scenarios = list(COMMANDS) if live else ["create", "overwrite"]
    for scenario in scenarios:
        row = await run_case(baseline, work / scenario, scenario, live=live)
        report["runs"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if row["status"] == "environment_blocked":
            report["unverified"] = scenarios[len(report["runs"]):]
            break
    report["protected_files_unchanged"] = before == [
        await asyncio.to_thread(file_state, path) for path in protected
    ]
    path = work / "report.json"
    await asyncio.to_thread(path.write_text, json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report: {path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the configured external model")
    args = parser.parse_args()
    asyncio.run(run_acceptance(args.live))
