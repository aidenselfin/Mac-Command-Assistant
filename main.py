"""Run with uv run python main.py --text 'voice-action-test 폴더 파일 목록 보여줘'."""

import argparse
import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from openai import AsyncOpenAI
from rich.console import Console

from voice_action.config import ConfigError, load_config
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient, MCPError
from voice_action.planner import (
    Clarification,
    Completed,
    Error,
    OpenAICompatibleModel,
    Planner,
    PlannerError,
    PlannerTiming,
)
from voice_action.runtime import Busy, RuntimeController, RuntimeResult


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
        for index, option in enumerate(outcome.options, 1):
            console.print(f"  {index}. {option}", markup=False, highlight=False)
    elif isinstance(outcome, Busy):
        console.print(f"[BUSY] {outcome.message}", markup=False, highlight=False)


async def run_command(
    controller: RuntimeController, text: str, console: Console
) -> RuntimeResult:
    console.print("[PLAN] 모델 응답 대기 중...", markup=False)
    outcome = await controller.submit(text)
    print_outcome(console, outcome)
    return outcome


async def run_text(
    config, text: str, console: Console, timing: ExecutionTiming | None = None
) -> ExecutionTiming:
    timing = timing or ExecutionTiming()
    total_started = time.perf_counter_ns()
    startup_started = time.perf_counter_ns()
    mcp = MCPClient(config, allowed_tools=SUPPORTED_TOOLS)
    try:
        async with mcp.connect():
            timing.mcp_startup_ms = (time.perf_counter_ns() - startup_started) / 1_000_000
            async with AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.llm_timeout_seconds,
                max_retries=0,
            ) as client:
                tools = await mcp.list_tools()
                planner = Planner(
                    config,
                    mcp,
                    OpenAICompatibleModel(client, config.model),
                    emit=lambda text: console.print(text, markup=False, highlight=False),
                    tools=tools,
                    timing=timing.planner,
                )
                controller = RuntimeController(
                    planner,
                    emit=lambda message: console.print(message, markup=False),
                )
                await run_command(controller, text, console)
    finally:
        timing.total_ms = (time.perf_counter_ns() - total_started) / 1_000_000
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


async def run_interactive(config, console: Console) -> None:
    mcp = MCPClient(config, allowed_tools=SUPPORTED_TOOLS)
    async with mcp.connect():
        async with AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.llm_timeout_seconds,
            max_retries=0,
        ) as client:
            tools = await mcp.list_tools()
            planner = Planner(
                config,
                mcp,
                OpenAICompatibleModel(client, config.model),
                emit=lambda message: console.print(message, markup=False, highlight=False),
                tools=tools,
            )
            controller = RuntimeController(
                planner,
                emit=lambda message: console.print(message, markup=False),
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
                    if text:
                        try:
                            await run_command(controller, text, console)
                        except Exception as exc:
                            console.print(f"[ERROR] {error_message(exc)}", markup=False)


def error_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return error_message(exc.exceptions[0])
    if isinstance(exc, (ConfigError, MCPError, PlannerError)):
        return str(exc)
    # SDK exceptions can contain request bodies and authentication headers.
    return f"실행 실패 ({type(exc).__name__}); 설정과 MCP/네트워크 연결을 확인하세요."


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="허용 폴더 안의 텍스트 파일 작업")
    command_mode = parser.add_mutually_exclusive_group(required=True)
    command_mode.add_argument("--text")
    command_mode.add_argument("--interactive", action="store_true")
    parser.add_argument("--provider", choices=("openai", "openrouter"))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--allowed-directory", action="append", dest="allowed_directories")
    args = vars(parser.parse_args(argv))
    text, interactive = args.pop("text"), args.pop("interactive")
    console = Console()
    try:
        if text is not None and not text.strip():
            raise ConfigError("명령이 비어 있습니다.")
        config = load_config(args)
        if interactive:
            asyncio.run(run_interactive(config, console))
        else:
            timing = asyncio.run(run_text(config, text, console))
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
