"""Opt-in isolated acceptance run; no raw model output or credentials in reports."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from llm_mcp_transcript import TranscriptLog, TranscriptModel, read_records
from main import _build_mcp_manager, _build_undo_journal
from voice_action.config import load_config
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient, MCPError, ToolRejectedError
from voice_action.mcp_manager import MCPManager
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    Completed,
    Error,
    ModelTurn,
    OpenAICompatibleModel,
    Planner,
    PlannerError,
    PlannerTiming,
    ToolCall,
)
from voice_action.runtime import RuntimeController, RuntimeState

PROJECT = Path(__file__).resolve().parent
ROOT_NAME = "voice-action-test"
SETTING_FIELDS = (
    "provider", "model", "base_url", "llm_timeout_seconds", "mcp_timeout_seconds",
    "max_tool_rounds", "max_tool_result_chars",
)
ORIGINAL = "원본 bytes 😀\r\n끝\n".encode()
SOURCE = "회의 결정: 금요일 배포. 담당자는 민수.\n"
BUDGET_WRITE_DONE = "승인된 파일 변경은 완료되었습니다."
NOTHING_TO_UNDO = Completed("되돌릴 수 있는 최근 파일 쓰기가 없습니다.")


def environment_cause(exc):
    """Return a non-secret label when the failure is outside the application, else None."""
    if isinstance(exc, BaseExceptionGroup):
        causes = [environment_cause(item) for item in exc.exceptions]
        return causes[0] if all(causes) else None
    cause = exc.__cause__ if isinstance(exc, PlannerError) else exc
    if isinstance(cause, APIStatusError):
        # 400 is the model's own invalid request/tool call, not an unavailable service.
        return None if cause.status_code == 400 else f"llm_http_{cause.status_code}"
    if isinstance(cause, APIConnectionError):  # includes APITimeoutError
        return f"llm_{type(cause).__name__}"
    if isinstance(cause, ToolRejectedError):
        return None
    if isinstance(cause, (MCPError, OSError)):
        return f"mcp_{type(cause).__name__}"
    return None


def error_summary(exc):
    """Type, status, provider error code and failing harness line only; never messages."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]  # MCP task groups wrap the real failure
    cause = exc.__cause__ if isinstance(exc, PlannerError) and exc.__cause__ else exc
    lines = [
        frame.lineno for frame in traceback.extract_tb(exc.__traceback__)
        if frame.filename == __file__
    ]
    code = getattr(cause, "code", None)
    return {
        "type": type(cause).__name__,
        "status_code": getattr(cause, "status_code", None),
        "code": code if isinstance(code, str) and len(code) <= 64 else None,
        "harness_line": lines[-1] if lines else None,
    }


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


def code_baseline():
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=PROJECT, capture_output=True, text=True, check=True
        ).stdout
    return {
        "commit": git("rev-parse", "HEAD").strip(),
        "dirty": bool(git("status", "--porcelain").strip()),
        "diff_sha256": hashlib.sha256(git("diff", "HEAD").encode()).hexdigest(),
    }


class ObservedGateway:
    """Record which calls actually reached MCP; arguments and results are not kept."""

    def __init__(self, gateway):
        self._gateway = gateway
        self.calls = []  # (name, "ok" | "error" | "rejected", elapsed_ms)

    def __getattr__(self, name):
        return getattr(self._gateway, name)

    async def call_tool(self, name, arguments):
        started = time.perf_counter()
        status = "error"
        try:
            result = await self._gateway.call_tool(name, arguments)
            status = "ok" if result.ok else "error"
            return result
        except ToolRejectedError:
            status = "rejected"
            raise
        finally:
            self.calls.append((name, status, (time.perf_counter() - started) * 1000))

    def names(self, *statuses):
        return [name for name, status, _ in self.calls if status in statuses]


class Measurements:
    def __init__(self):
        self.tool_errors = self.duplicates = self.clarification_rejections = 0

    def log(self, text):
        # Never persist tool values, model text, exceptions, or raw arguments.
        if text.startswith("[TOOL-ERROR]"):
            self.tool_errors += 1
        elif text.startswith("[PLAN] 동일 도구 호출 반복"):
            self.duplicates += 1
        elif text.startswith("[PLAN] clarification policy rejected"):
            self.clarification_rejections += 1


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


COMMANDS = {
    # D: isolated approval → IDLE undo → second undo.
    "create": "source.txt를 요약해서 target.txt에 저장해줘",
    "overwrite": "source.txt를 요약해서 기존 target.txt에 덮어써줘",
    "overwrite_literal": "result.txt 내용을 검증 완료로 바꿔줘",
    # Original command table (README) and plan3 E additions.
    "listing": "voice-action-test 폴더 파일 목록 보여줘",
    "read": "test1.md 내용을 읽어줘",
    "summary": "test1.md를 요약해줘",
    "search": "회의록 파일을 찾아줘",
    "meeting_zero": "회의록 요약해줘",
    "meeting_one": "회의록 요약해줘",
    "meeting_many": "회의록 요약해줘",
    "meeting_resume": "회의록 요약해줘",
    "missing_source": "result.txt에 요약을 저장해줘",
    "web_read": "https://example.com 페이지를 읽고 요약해줘",
    "web_particle": "https://example.com을 읽고 summary.md에 저장해줘",
    "web_space": "https://example.com 읽고 summary.md에 저장해줘",
    "web_compare": "https://example.com 페이지와 test1.md를 비교해줘",
    "web_no_save": "https://example.com 페이지를 저장하지 말고 요약해줘",
    "active_app": "현재 활성 앱을 알려줘",
    "app_listing": "현재 활성 앱과 파일 목록을 알려줘",
    "app_write": "활성 앱을 알려주고 result.txt에 저장해줘",
}
WRITES = {  # scenario → (target, operation); each is approved once and then undone.
    "create": ("target.txt", "create"),
    "overwrite": ("target.txt", "overwrite"),
    "overwrite_literal": ("result.txt", "overwrite"),
    "app_write": ("result.txt", "create"),
}
MEETING_NOTES = {"search": 2, "meeting_one": 1, "meeting_many": 2, "meeting_resume": 2}


def prepare_case(cwd, scenario):
    cwd.mkdir()
    root = cwd / ROOT_NAME
    root.mkdir()
    (root / "source.txt").write_text(SOURCE, encoding="utf-8")
    (root / "test1.md").write_text(SOURCE, encoding="utf-8")
    (root / "sentinel.txt").write_bytes(b"must remain unchanged")
    if scenario == "overwrite":
        (root / "target.txt").write_bytes(ORIGINAL)
    if scenario == "overwrite_literal":
        (root / "result.txt").write_bytes(ORIGINAL)
    if scenario.startswith("meeting_"):
        # plan3 A: an unrelated .txt must not be picked as the meeting note.
        (root / "result.txt").write_text("회의와 무관한 결과\n", encoding="utf-8")
    for index in range(MEETING_NOTES.get(scenario, 0)):
        (root / f"회의록-{index + 1}.md").write_text(SOURCE, encoding="utf-8")
    return root


async def approve_and_undo(controller, outcome, scenario, root, before, journal, timing, gateway,
                           row):
    name, operation = WRITES[scenario]
    assert isinstance(outcome, ApprovalRequired)
    assert controller.state is RuntimeState.WAITING_APPROVAL
    assert outcome.target_path == str(root / name)
    assert outcome.operation == operation
    # Bind the single approval to the exact inspected payload in memory.
    content = json.loads(outcome.context.prepared_write.canonical_arguments)["content"]
    if scenario == "overwrite_literal":
        assert content.strip() == "검증 완료"
    expected = content.encode()
    assert expected and expected != (ORIGINAL if operation == "overwrite" else b"")
    row["approval"] = "approved"
    outcome = await controller.approve_pending()
    # plan3 C: a budget exhausted after the approved write must still report the change.
    row["budget_exhausted_after_write"] = isinstance(outcome, Error)
    assert isinstance(outcome, Completed) or (
        isinstance(outcome, Error) and outcome.message.endswith(BUDGET_WRITE_DONE)
    )
    assert controller.state is RuntimeState.IDLE
    assert await asyncio.to_thread((root / name).read_bytes) == expected
    assert await asyncio.to_thread(journal.entry_count, status="active") == 1
    assert await asyncio.to_thread(journal.entry_count) == 1
    row["write_verified"] = True
    model_calls, mcp_calls = len(timing.llm_round_ms), len(gateway.calls)
    undone = await controller.undo_last_write()
    assert isinstance(undone, Completed)
    assert controller.state is RuntimeState.IDLE
    assert await asyncio.to_thread(file_state, root) == before
    assert await asyncio.to_thread(journal.entry_count, status="undone") == 1
    assert await controller.undo_last_write() == NOTHING_TO_UNDO
    assert await asyncio.to_thread(file_state, root) == before
    assert await asyncio.to_thread(journal.entry_count, status="undone") == 1
    assert (len(timing.llm_round_ms), len(gateway.calls)) == (model_calls, mcp_calls)
    row.update(undo_verified=True, second_undo_unchanged=True)
    return outcome


async def check_read_only(controller, outcome, scenario, root, timing, gateway, row):
    reached = gateway.names("ok", "error")
    ok = gateway.names("ok")
    expected_tool = {
        "listing": "filesystem__list_directory",
        "read": "filesystem__read_text_file",
        "summary": "filesystem__read_text_file",
        "search": "filesystem__search_files",
        "active_app": "macos__get_active_app",
    }.get(scenario)
    if expected_tool:
        assert isinstance(outcome, Completed) and expected_tool in ok
    if scenario == "search":
        assert "filesystem__read_text_file" not in reached
    if scenario.startswith("meeting_"):
        assert "filesystem__search_files" in ok
        if scenario in {"meeting_many", "meeting_resume"}:
            assert isinstance(outcome, Clarification) and len(outcome.options) == 2
            assert "filesystem__read_text_file" not in reached
        if scenario == "meeting_resume":
            outcome = await controller.submit("2")
            row["resumed_outcome"] = type(outcome).__name__
            assert isinstance(outcome, Completed)
            assert gateway.names("ok", "error").count("filesystem__read_text_file") == 1
        elif scenario != "meeting_many":
            assert isinstance(outcome, Completed)
            assert ("filesystem__read_text_file" in reached) == (scenario == "meeting_one")
    if scenario.startswith("web_"):
        assert "web__browser_navigate" in ok
        index = reached.index("web__browser_navigate")
        assert reached[index + 1] == "web__browser_snapshot" and "web__browser_snapshot" in ok
        if scenario in {"web_particle", "web_space"}:
            assert isinstance(outcome, ApprovalRequired)
            assert outcome.target_path == str(root / "summary.md")
            assert outcome.operation == "create"
            row["approval"] = "rejected"
            outcome = await controller.reject_pending()
        assert isinstance(outcome, Completed)
        if scenario == "web_compare":
            assert "filesystem__read_text_file" in ok
    if scenario == "missing_source":
        # plan3 E: no source → ask for content and never reach an approval/write.
        assert isinstance(outcome, (Completed, Clarification))
    if scenario in {"active_app", "app_listing"}:
        # A permission error answered as text must not count as a lookup.
        assert "macos__get_active_app" not in gateway.names("error")
    if scenario == "active_app":
        assert len(timing.llm_round_ms) == 1  # deterministic single-lookup completion
    if scenario == "app_listing":
        assert isinstance(outcome, Completed)
        assert {"macos__get_active_app", "filesystem__list_directory"} <= set(ok)
    assert "filesystem__write_file" not in reached
    return outcome


async def run_case(baseline, cwd, scenario, *, live=False):
    root = await asyncio.to_thread(prepare_case, cwd, scenario)
    previous_cwd = Path.cwd()
    transcript = TranscriptLog(cwd / "transcript.jsonl")
    metrics, timing, gateway = Measurements(), PlannerTiming.empty(), None
    config = baseline
    before = await asyncio.to_thread(file_state, root)
    row = {
        "scenario": scenario, "command": COMMANDS[scenario], "run_id": transcript.run_id,
        "mode": "live" if live else "scripted", "fixture": before,
        "cwd": str(cwd), "allowed_root": str(root), "settings_equal": False,
        "status": "unverified", "approval": None,
    }
    transcript.start(COMMANDS[scenario])
    started = time.perf_counter()
    os.chdir(cwd)
    try:
        config = isolated_config(baseline, cwd, root)
        row.update(
            settings_equal=True, provider=config.provider,
            model=config.model if live else "scripted",
        )
        journal = await asyncio.to_thread(_build_undo_journal, config)
        assert journal is not None
        assert journal.path == cwd / ".voice-action/undo.sqlite3"
        row["journal"] = str(journal.path)
        manager = _build_mcp_manager(config) if live else MCPManager({
            "filesystem": MCPClient(config, allowed_tools=SUPPORTED_TOOLS),
        })
        async with manager.connect():
            row["startup_ms"] = round((time.perf_counter() - started) * 1000, 3)
            gateway = ObservedGateway(manager)
            async with AsyncOpenAI(
                api_key=config.api_key, base_url=config.base_url,
                timeout=config.llm_timeout_seconds, max_retries=0,
            ) as client:
                model = OpenAICompatibleModel(client, config.model) if live else ScriptedWriteModel(
                    root, root / "target.txt"
                )
                core = Planner(
                    config, gateway, TranscriptModel(model, transcript), timing=timing,
                    undo_journal=journal, emit=metrics.log, tools=await manager.list_tools(),
                )
                controller = RuntimeController(core)
                command_started = time.perf_counter()
                outcome = await controller.submit(COMMANDS[scenario])
                row["initial_outcome"] = type(outcome).__name__
                assert await asyncio.to_thread(file_state, root) == before
                assert await asyncio.to_thread(journal.entry_count) == 0
                if scenario in WRITES:
                    outcome = await approve_and_undo(
                        controller, outcome, scenario, root, before, journal, timing, gateway,
                        row,
                    )
                else:
                    outcome = await check_read_only(
                        controller, outcome, scenario, root, timing, gateway, row
                    )
                    assert await asyncio.to_thread(journal.entry_count) == 0
                row["command_ms"] = round((time.perf_counter() - command_started) * 1000, 3)
                row["outcome"] = type(outcome).__name__
                assert len(timing.llm_round_ms) <= config.max_tool_rounds
                row["status"] = "passed"
    except Exception as exc:
        # No exception text, body, header, key, or raw provider response is recorded.
        row["error"] = error_summary(exc)
        blocked = [
            name for name in (gateway.names("error") if gateway else [])
            if name.startswith(("web__", "macos__"))
        ]
        cause = environment_cause(exc) or (f"tool_{blocked[0]}" if blocked else None)
        row["status"] = "environment_blocked" if cause else "functional_failure"
        row["blocked_by"] = cause
    finally:
        transcript.finish(row["status"])
        records = await asyncio.to_thread(read_records, transcript.path)
        events = records[0]["events"] if records else []
        outputs = [event for event in events if event["event"] == "llm_output"]
        requested = [call["name"] for event in outputs for call in event["tool_calls"]]
        calls = gateway.calls if gateway else []
        reached = [name for name, status, _ in calls if status != "rejected"]
        after = await asyncio.to_thread(file_state, root)
        row.update(
            model_calls=sum(event["event"] == "llm_input" for event in events),
            model_limit=config.max_tool_rounds, requested_tools=requested,
            executed_tools=reached,
            execution_errors=[name for name, status, _ in calls if status == "error"],
            path_rejections=sum(status == "rejected" for _, status, _ in calls),
            tool_errors=metrics.tool_errors,
            policy_rejections=metrics.tool_errors - sum(
                status == "error" for _, status, _ in calls
            ),
            clarification_rejections=metrics.clarification_rejections,
            duplicate_calls=metrics.duplicates,
            cached_app_calls=max(
                0, requested.count("macos__get_active_app") - reached.count("macos__get_active_app")
            ),
            llm_ms=round(sum(timing.llm_round_ms), 3),
            mcp_ms=round(sum(elapsed for _, _, elapsed in calls), 3),
            total_ms=round((time.perf_counter() - started) * 1000, 3),
            changed_files=sorted(
                name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
            ),
            files_restored=after == before,
        )
        os.chdir(previous_cwd)
    return row


async def run_acceptance(live, only=None, pause=0.0):
    baseline = load_config(cwd=PROJECT)
    work = await asyncio.to_thread(
        lambda: Path(tempfile.mkdtemp(prefix="voice-action-de-")).resolve()
    )
    protected = [*baseline.allowed_directories, PROJECT / ".voice-action"]
    before = [await asyncio.to_thread(file_state, path) for path in protected]
    settings = {name: getattr(baseline, name) for name in SETTING_FIELDS}
    if settings["base_url"]:
        parts = urlsplit(settings["base_url"])
        settings["base_url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    report = {
        "settings": settings, "mode": "live" if live else "scripted",
        "code": await asyncio.to_thread(code_baseline), "pause_seconds": pause, "runs": [],
    }
    scenarios = only or (list(COMMANDS) if live else ["create", "overwrite"])
    for position, scenario in enumerate(scenarios):
        if position and pause:
            await asyncio.sleep(pause)  # pacing between scenarios, never a retry
        row = await run_case(baseline, work / scenario, scenario, live=live)
        report["runs"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if row["status"] == "environment_blocked" and row["blocked_by"].startswith("llm_"):
            # plan3 D.7: stop on API failure and leave the rest unverified.
            report["unverified"] = scenarios[position + 1:]
            break
    report["protected_files_unchanged"] = before == [
        await asyncio.to_thread(file_state, path) for path in protected
    ]
    report["complete"] = not report.get("unverified") and all(
        row["status"] == "passed" for row in report["runs"]
    )
    path = work / "report.json"
    await asyncio.to_thread(path.write_text, json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report: {path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the configured external model")
    parser.add_argument("--only", nargs="+", choices=list(COMMANDS), help="Scenarios to run")
    parser.add_argument("--pause", type=float, default=0.0, help="Seconds between scenarios")
    args = parser.parse_args()
    asyncio.run(run_acceptance(args.live, args.only, args.pause))
