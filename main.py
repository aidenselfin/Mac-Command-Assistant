"""Run with uv run python main.py --text 'voice-action-test 폴더 파일 목록 보여줘'."""

import argparse
import asyncio
import os
import re
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI
from rich.console import Console

from voice_action.attachment import AttachmentError
from voice_action.config import ConfigError, load_config
from voice_action.feedback import DEFAULT_DB_PATH, RATING_TAGS, FeedbackLoop, build_feedback
from voice_action.finder import reveal
from voice_action.macos_client import MacOSAXClient
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient, MCPError
from voice_action.mcp_manager import MCPManager
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    Completed,
    Error,
    OpenAICompatibleModel,
    Planner,
    PlannerError,
    PlannerTiming,
)
from voice_action.playwright_client import PlaywrightMCPClient
from voice_action.runtime import Busy, RuntimeController, RuntimeResult, RuntimeState
from voice_action.ui import run_ui
from voice_action.undo import UndoJournal, UndoJournalError

APPROVE_TOKENS = frozenset({"승인", "approve", "yes", "y"})
REJECT_TOKENS = frozenset({"거절", "reject", "no", "n", "취소", "cancel", "c"})
UNDO_TOKENS = frozenset({"undo", "되돌리기"})


@dataclass
class ExecutionTiming:
    mcp_startup_ms: float = 0.0
    planner: PlannerTiming = field(default_factory=PlannerTiming.empty)
    total_ms: float = 0.0

    def lines(self) -> list[str]:
        rounds = [*self.planner.llm_round_ms, 0.0, 0.0, 0.0]
        return [
            f"mcp_startup_ms={self.mcp_startup_ms:.2f}",
            f"llm_round_1_ms={rounds[0]:.2f}",
            f"mcp_read_ms={self.planner.mcp_read_ms:.2f}",
            f"llm_round_2_ms={rounds[1]:.2f}",
            f"mcp_write_ms={self.planner.mcp_write_ms:.2f}",
            f"llm_round_3_ms={rounds[2]:.2f}",
            f"total_ms={self.total_ms:.2f}",
        ]


def print_timing(console: Console, timing: ExecutionTiming) -> None:
    console.print("[TIMING]", markup=False)
    for line in timing.lines():
        console.print(line, markup=False)


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
        if outcome.operation == "delete":
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


def show_outcome(console: Console, outcome: RuntimeResult, reveal=None) -> None:
    """Print an outcome, then show the files it changed in Finder if `reveal` is given."""
    print_outcome(console, outcome)
    if reveal is not None and isinstance(outcome, Completed) and outcome.changed_paths:
        reveal(outcome.changed_paths)


def finder_reveal(config):
    """The Finder hook for this config, or None when it is switched off."""
    if not config.finder_reveal:
        return None
    return lambda paths: reveal(paths, config.allowed_directories)


async def run_command(
    controller: RuntimeController, text: str, console: Console, reveal=None
) -> RuntimeResult:
    console.print("[PLAN] 모델 응답 대기 중...", markup=False)
    outcome = await controller.submit(text)
    show_outcome(console, outcome, reveal)
    return outcome


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


async def run_text(
    config,
    text: str,
    console: Console,
    timing: ExecutionTiming | None = None,
    *,
    allow_local_web: bool = False,
    undo_journal: UndoJournal | None = None,
) -> ExecutionTiming:
    timing = timing or ExecutionTiming()
    total_started = time.perf_counter_ns()
    startup_started = time.perf_counter_ns()
    manager = _build_mcp_manager(config)
    feedback = build_feedback(config, lambda text: console.print(text, markup=False))
    try:
        async with manager.connect():
            timing.mcp_startup_ms = (time.perf_counter_ns() - startup_started) / 1_000_000
            async with AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.llm_timeout_seconds,
                max_retries=0,
            ) as client:
                tools = await manager.list_tools()
                model = OpenAICompatibleModel(client, config.model)
                planner = Planner(
                    config,
                    manager,
                    feedback.wrap(model) if feedback else model,
                    emit=lambda text: console.print(text, markup=False, highlight=False),
                    tools=tools,
                    timing=timing.planner,
                    allow_local_web=allow_local_web,
                    undo_journal=undo_journal,
                )
                controller = RuntimeController(
                    planner,
                    emit=lambda message: console.print(message, markup=False),
                    feedback=feedback,
                )
                await run_command(controller, text, console)
    finally:
        timing.total_ms = (time.perf_counter_ns() - total_started) / 1_000_000
        if feedback is not None:
            await feedback.close()
    return timing


@asynccontextmanager
async def input_reader():
    """Cancellable stdin on macOS; a thread blocked in input() prevents shutdown."""
    reader = asyncio.StreamReader()
    pipe = os.fdopen(os.dup(sys.stdin.fileno()), "rb")
    try:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), pipe
        )
    except BaseException:
        pipe.close()
        raise
    try:
        yield reader
    finally:
        transport.close()


async def run_interactive(
    config, console: Console, undo_journal: UndoJournal | None = None
) -> None:
    manager = _build_mcp_manager(config)
    feedback = build_feedback(config, lambda text: console.print(text, markup=False))
    try:
        await _interactive_loop(config, console, undo_journal, manager, feedback)
    finally:
        if feedback is not None:
            await feedback.close()


async def _interactive_loop(config, console, undo_journal, manager, feedback) -> None:
    async with manager.connect():
        async with AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.llm_timeout_seconds,
            max_retries=0,
        ) as client:
            tools = await manager.list_tools()
            model = OpenAICompatibleModel(client, config.model)
            planner = Planner(
                config,
                manager,
                feedback.wrap(model) if feedback else model,
                emit=lambda message: console.print(message, markup=False, highlight=False),
                tools=tools,
                undo_journal=undo_journal,
            )
            controller = RuntimeController(
                planner,
                emit=lambda message: console.print(message, markup=False),
                feedback=feedback,
            )
            async with input_reader() as reader:
                console.print(
                    "[READY] 명령을 입력하세요. 종료하려면 exit 또는 Ctrl+C를 누르세요.",
                    markup=False,
                )
                while True:
                    console.print("> ", end="", markup=False)
                    line = await reader.readline()
                    if not line:
                        return
                    text = line.decode(sys.stdin.encoding or "utf-8").strip()
                    if text.lower() in {"exit", "quit"}:
                        return
                    if controller.state is RuntimeState.WAITING_APPROVAL:
                        try:
                            token = text.casefold()
                            if token in APPROVE_TOKENS:
                                show_outcome(
                                    console,
                                    await controller.approve_pending(),
                                    finder_reveal(config),
                                )
                            elif token in REJECT_TOKENS:
                                show_outcome(console, await controller.reject_pending())
                            else:
                                console.print(
                                    "[APPROVAL REQUIRED] 승인 또는 거절 입력이 "
                                    "필요합니다.",
                                    markup=False,
                                    highlight=False,
                                )
                        except Exception as exc:
                            console.print(f"[ERROR] {error_message(exc)}", markup=False)
                        continue
                    if (
                        controller.state is RuntimeState.IDLE
                        and text in UNDO_TOKENS
                    ):
                        try:
                            show_outcome(console, await controller.undo_last_write())
                        except Exception as exc:
                            console.print(f"[ERROR] {error_message(exc)}", markup=False)
                        continue
                    if controller.state is RuntimeState.IDLE and attach_command(
                        controller, text, console
                    ):
                        continue
                    rating = parse_rating(text)
                    if rating is not None and controller.state is RuntimeState.IDLE:
                        saved = await controller.rate(*rating)
                        console.print(
                            "[RATE] 평가를 저장했습니다." if saved
                            else "[RATE] 평가할 답변이 없습니다.",
                            markup=False,
                        )
                        continue
                    if text:
                        try:
                            await run_command(controller, text, console, finder_reveal(config))
                            if controller.ratable:
                                console.print(RATE_HINT, markup=False, highlight=False)
                        except Exception as exc:
                            console.print(f"[ERROR] {error_message(exc)}", markup=False)


RATE_HINT = (
    "[RATE] 답변 평가: rate 1~5 (5가 최고). 3점 이하면 원인을 덧붙일 수 있어요: "
    + " ".join(sorted(RATING_TAGS))
)


def attach_command(controller: RuntimeController, text: str, console: Console) -> bool:
    """`attach <path>` / `detach`: the terminal twin of dropping a file on the window."""
    head, _, rest = text.strip().partition(" ")
    if head.casefold() == "detach" and not rest:
        controller.detach()
        console.print("[ATTACH] 첨부를 해제했습니다.", markup=False)
        return True
    if head.casefold() != "attach" or not rest.strip():
        return False
    try:
        path = controller.attach(rest.strip().strip("'\""))
        console.print(f"[ATTACH] 다음 명령의 대상 파일: {path}", markup=False, highlight=False)
    except AttachmentError as exc:
        console.print(f"[ATTACH-ERROR] {exc}", markup=False, highlight=False)
    return True


def parse_rating(text: str) -> tuple[int, tuple[str, ...]] | None:
    """`rate 2 wrong_file too_slow` -> (2, ("wrong_file", "too_slow")); other text -> None."""
    match = re.fullmatch(r"rate\s+([1-5])((?:[\s,]+\w+)*)", text.strip(), re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1)), tuple(re.findall(r"\w+", match.group(2)))


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
        out("[후보 lesson] 평가로 비교한 뒤 --lesson-on ID로 채택, --lesson-off ID로 거절")
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
                detail = analysis.get("lesson") or analysis.get("user_hint") or "-"
                out(f"    조치: {analysis['action']} · {detail}")
        if not incidents:
            out("  없음")
        if loop.report_path.exists():
            out(f"[개발자 리포트] {loop.report_path}")
        return 0
    finally:
        loop.db.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="허용 폴더 안의 파일 읽기·쓰기·승인 기반 삭제")
    command_mode = parser.add_mutually_exclusive_group(required=True)
    command_mode.add_argument("--text")
    command_mode.add_argument("--interactive", action="store_true")
    command_mode.add_argument("--ui", action="store_true")
    command_mode.add_argument("--lessons", action="store_true", help="활성 피드백 lesson 목록")
    command_mode.add_argument("--lesson-off", type=int, metavar="ID", help="lesson 비활성화")
    command_mode.add_argument("--lesson-on", type=int, metavar="ID", help="후보 lesson 채택")
    parser.add_argument("--provider", choices=("openai", "groq", "openrouter"))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--allowed-directory", action="append", dest="allowed_directories")
    args = vars(parser.parse_args(argv))
    lessons, lesson_off = args.pop("lessons"), args.pop("lesson_off")
    lesson_on = args.pop("lesson_on")
    if lessons or lesson_off is not None or lesson_on is not None:
        console = Console()
        try:
            return manage_lessons(console, lesson_off, lesson_on)
        except (OSError, sqlite3.Error):
            console.print(
                "[ERROR] 피드백 기록을 읽을 수 없습니다. .voice-action/feedback.sqlite3를 "
                "확인하세요.",
                markup=False,
            )
            return 1
    text = args.pop("text")
    interactive = args.pop("interactive")
    ui = args.pop("ui")
    console = Console()
    try:
        if text is not None and not text.strip():
            raise ConfigError("명령이 비어 있습니다.")
        config = load_config(args)
        if ui:
            run_ui(config)
        elif interactive:
            asyncio.run(
                run_interactive(config, console, _build_undo_journal(config))
            )
        else:
            timing = asyncio.run(
                run_text(
                    config,
                    text,
                    console,
                    undo_journal=_build_undo_journal(config),
                )
            )
            print_timing(console, timing)
    except KeyboardInterrupt:
        console.print("[STOP] 종료했습니다.", markup=False)
        return 130
    except Exception as exc:
        console.print(f"[ERROR] {error_message(exc)}", markup=False)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
