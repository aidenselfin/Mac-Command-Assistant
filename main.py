"""Helpers shared by tools plus the lesson CLI: uv run python main.py --lessons."""

import argparse
import sqlite3
import sys
from pathlib import Path

from rich.console import Console

from voice_action.config import ConfigError
from voice_action.feedback import DEFAULT_DB_PATH, FeedbackLoop
from voice_action.macos_client import MacOSAXClient
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient, MCPError
from voice_action.mcp_manager import MCPManager
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    Completed,
    Error,
    PlannerError,
)
from voice_action.playwright_client import PlaywrightMCPClient
from voice_action.runtime import Busy, RuntimeResult
from voice_action.undo import UndoJournal, UndoJournalError


def print_outcome(console: Console, outcome: RuntimeResult) -> None:
    if isinstance(outcome, Completed):
        console.print(f"[DONE] {outcome.text}", markup=False, highlight=False)
    elif isinstance(outcome, Error):
        console.print(f"[ERROR] {outcome.message}", markup=False, highlight=False)
    elif isinstance(outcome, Clarification):
        console.print(f"[QUESTION] {outcome.question}", markup=False, highlight=False)
        for index, option in enumerate(outcome.shown_options, 1):
            console.print(f"  {index}. {option}", markup=False, highlight=False)
    elif isinstance(outcome, ApprovalRequired):
        if outcome.operation in {"delete", "move", "copy", "mkdir"}:
            from voice_action.ui import render_outcome

            console.print(render_outcome(outcome), markup=False, highlight=False)
            console.print("승인: 승인/approve/yes/y | 거절: 거절/reject/no/n/취소/cancel/c",
                          markup=False)
            return
        suffix = "…" if outcome.preview_truncated else ""
        console.print("[APPROVAL REQUIRED]", markup=False, highlight=False)
        console.print(f"tool: {outcome.tool_name}", markup=False, highlight=False)
        if outcome.operation == "convert" and outcome.targets:
            console.print(f"source: {outcome.targets[0]}", markup=False, highlight=False)
        console.print(f"target: {outcome.target_path}", markup=False, highlight=False)
        console.print(f"operation: {outcome.operation}", markup=False, highlight=False)
        console.print(
            f"content_chars: {outcome.content_chars}", markup=False, highlight=False
        )
        console.print(
            f"preview: {outcome.preview}{suffix}", markup=False, highlight=False
        )
        console.print(
            "승인: 승인/approve/yes/y | 거절: 거절/reject/no/n/취소/cancel/c",
            markup=False,
            highlight=False,
        )
    elif isinstance(outcome, Busy):
        console.print(f"[BUSY] {outcome.message}", markup=False, highlight=False)


def _build_mcp_manager(config) -> MCPManager:
    clients = {
        "filesystem": MCPClient(config, allowed_tools=SUPPORTED_TOOLS),
        "web": PlaywrightMCPClient(config),
    }
    if sys.platform == "darwin":
        clients["macos"] = MacOSAXClient()
    return MCPManager(clients)


def _build_undo_journal(config) -> UndoJournal | None:
    try:
        return UndoJournal.default(config.allowed_directories)
    except UndoJournalError:
        return None


def error_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return error_message(exc.exceptions[0])
    if isinstance(exc, (ConfigError, MCPError, PlannerError)):
        return str(exc)
    # SDK exceptions can contain request bodies and authentication headers.
    return f"실행 실패 ({type(exc).__name__}); 설정과 MCP/네트워크 연결을 확인하세요."


def manage_lessons(
    console: Console, lesson_off: int | None, lesson_on: int | None = None
) -> int:
    loop = FeedbackLoop(Path.cwd() / DEFAULT_DB_PATH)
    try:
        if lesson_off is not None:
            found = loop.deactivate_lesson(lesson_off)
            console.print(
                f"lesson #{lesson_off} 비활성화" if found else f"lesson #{lesson_off} 없음",
                markup=False,
            )
            return 0 if found else 1
        if lesson_on is not None:
            adopted = loop.activate_lesson(lesson_on)
            console.print(
                f"lesson #{lesson_on} 채택 (다음 명령부터 적용)"
                if adopted
                else f"후보 lesson #{lesson_on} 없음",
                markup=False,
            )
            return 0 if adopted else 1
        out = lambda text: console.print(text, markup=False, highlight=False)  # noqa: E731
        out("[활성 lesson]")
        rows = loop.active_lessons()
        for lesson_id, text in rows:
            out(f"  #{lesson_id} {text}")
        if not rows:
            out("  없음")
        out("[후보 lesson] 자동 평가 대기·보류 중 (수동 --lesson-on/--lesson-off ID)")
        candidates = loop.candidate_lessons()
        for lesson_id, text in candidates:
            out(f"  #{lesson_id} {text}")
        if not candidates:
            out("  없음")
        out("[최근 감지·분석]")
        incidents = loop.recent_incidents()
        for stamp, rule, signature, status, analysis in incidents:
            out(f"  {stamp} {rule} {status} · {signature[:60]}")
            if analysis:
                out(f"    원인: {analysis['root_cause']}")
                detail = analysis.get("lesson") or analysis.get("fix_request") or "-"
                out(f"    조치: {analysis['action']} · {detail}")
        if not incidents:
            out("  없음")
        out("[자동 개선]")
        runs = loop.recent_autonomy()
        for stamp, data in runs:
            out(f"  {stamp} {data.get('job')} #{data.get('ref')} {data.get('status')}"
                f" {data.get('why') or data.get('head') or ''}".rstrip())
        if not runs:
            out("  없음")
        return 0
    finally:
        loop.db.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="피드백 lesson 관리")
    command_mode = parser.add_mutually_exclusive_group(required=True)
    command_mode.add_argument("--lessons", action="store_true", help="활성 피드백 lesson 목록")
    command_mode.add_argument("--lesson-off", type=int, metavar="ID", help="lesson 비활성화")
    command_mode.add_argument("--lesson-on", type=int, metavar="ID", help="후보 lesson 채택")
    args = parser.parse_args(argv)
    console = Console()
    try:
        return manage_lessons(console, args.lesson_off, args.lesson_on)
    except (OSError, sqlite3.Error):
        console.print(
            "[ERROR] 피드백 기록을 읽을 수 없습니다. .voice-action/feedback.sqlite3를 "
            "확인하세요.",
            markup=False,
        )
        return 1
    except KeyboardInterrupt:
        console.print("[STOP] 종료했습니다.", markup=False)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
