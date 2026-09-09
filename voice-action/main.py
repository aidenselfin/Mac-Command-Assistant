"""
Voice-Action AI v2.0 MVP Main Entrypoint
Minimalist Rich CLI interface for Voice-to-MCP filesystem automation.
"""

import argparse
import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from pynput import keyboard
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from audio import AudioRecorder
from config import Config, load_config
from mcp_client import MCPClient
from planner import LLMPlanner, PlannerResponse
from stt import SpeechRecognitionError, transcribe

console = Console()
recorder = AudioRecorder()
is_recording = False
pipeline_busy = False
active_mcp_client: Optional[MCPClient] = None
active_planner: Optional[LLMPlanner] = None
active_loop: Optional[asyncio.AbstractEventLoop] = None


def print_banner(config: Config, tool_count: int = 0) -> None:
    """Prints startup dashboard using Rich formatting."""
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_row("[bold cyan]LLM Provider:[/]", f"[yellow]{config.provider.upper()}[/] ({config.model})")
    table.add_row("[bold cyan]Push-to-Talk Hotkey:[/]", f"[magenta]Right Command (Key.cmd_r)[/]")
    table.add_row("[bold cyan]STT Model:[/]", f"[green]faster-whisper ({config.whisper_model})[/]")
    table.add_row("[bold cyan]Connected MCP Tools:[/]", f"[bold green]{tool_count} tools available[/]")
    table.add_row("[bold cyan]Allowed Directories:[/]", ", ".join(f"[blue]{d}[/]" for d in config.allowed_directories))

    panel = Panel(
        table,
        title="[bold white on blue] 🎙️ Voice-Action AI v2.0 MVP [/]",
        subtitle="[dim]Push-to-Talk Voice to Filesystem MCP Automation[/dim]",
        border_style="cyan",
    )
    console.print(panel)


def on_tool_start_callback(name: str, args: dict) -> None:
    console.print(f"[bold blue]🔌 [MCP Execute][/] [cyan]{name}[/]([dim]{args}[/])")


def on_tool_end_callback(name: str, result: str) -> None:
    # Truncate result if too long for preview
    preview = result.strip()
    if len(preview) > 200:
        preview = preview[:200] + "... (truncated)"
    console.print(f"[bold green]   ↳ [Result][/] [dim]{preview}[/]")


async def process_utterance(query: str, planner: LLMPlanner) -> None:
    """Executes the planner on the transcribed query and outputs results."""
    console.print(f"[bold yellow]🤖 [LLM Planning...][/] Processing command: [italic]\"{query}\"[/]")
    start_time = time.time()
    
    response: PlannerResponse = await planner.execute(query)
    duration = time.time() - start_time

    if response.success:
        result_text = Text()
        result_text.append(f"⏱️ Done in {duration:.2f}s | Actions: {len(response.actions)} | Turns: {response.raw_turns}\n\n", style="dim")
        result_text.append(response.final_text or "(No message returned)", style="bold white")
        
        console.print(Panel(
            result_text,
            title="[bold green]✅ [완료] Execution Summary[/]",
            border_style="green",
        ))
    else:
        console.print(Panel(
            f"[bold red]Error:[/] {response.error_message}",
            title="[bold red]❌ [오류] Execution Failed[/]",
            border_style="red",
        ))
    console.print("\n[dim]대기 중... (오른쪽 Command 키를 누른 채로 말하세요 / Ctrl+C로 종료)[/]\n")


def on_press(key):
    global is_recording
    if key == keyboard.Key.cmd_r and not is_recording and not pipeline_busy:
        is_recording = True
        recorder.start()
        console.print("[bold red]🔴 [녹음 중...][/] 오른쪽 Command 홀드 (말씀하신 후 키를 떼세요)")


def on_release(key):
    global is_recording, pipeline_busy, active_planner, active_loop
    if key == keyboard.Key.cmd_r and is_recording:
        is_recording = False
        console.print("[bold yellow]⚡ [녹음 종료][/] 오디오 버퍼 캡처 완료.")
        wav_bytes = recorder.stop()

        if not wav_bytes or len(wav_bytes) < 1000:
            console.print("[dim red]⚠️ 녹음된 오디오가 너무 짧습니다.[/]")
            return

        def run_stt_and_pipeline():
            global pipeline_busy
            pipeline_busy = True
            try:
                console.print("[bold cyan]⚡ [STT 변환 중...][/] 로컬 Whisper 처리 중...")
                stt_start = time.time()
                text = transcribe(wav_bytes)
                stt_dur = time.time() - stt_start
                console.print(f"[bold green]⚡ [STT 완료 ({stt_dur:.2f}s)][/] [bold white]\"{text}\"[/]")
                
                if active_loop and active_planner:
                    asyncio.run_coroutine_threadsafe(
                        process_utterance(text, active_planner),
                        active_loop,
                    ).result()
            except SpeechRecognitionError as e:
                console.print(f"[bold red]⚠️ [STT 오류][/] {e}")
            except Exception as e:
                console.print(f"[bold red]❌ [파이프라인 오류][/] {e}")
            finally:
                pipeline_busy = False

        threading.Thread(target=run_stt_and_pipeline, daemon=True).start()


async def interactive_cli(planner: LLMPlanner) -> None:
    """Provides a text-based REPL loop for quick testing."""
    console.print("[bold cyan]Interactive Text Console Mode[/] (Type 'exit' to quit)\n")
    while True:
        try:
            query = await asyncio.get_event_loop().run_in_executor(None, input, "Voice-Action> ")
            query = query.strip()
            if not query:
                continue
            if query.lower() in ("exit", "quit", "q"):
                break
            await process_utterance(query, planner)
        except (KeyboardInterrupt, EOFError):
            break


async def run_app(args: argparse.Namespace) -> None:
    global active_mcp_client, active_planner, active_loop
    active_loop = asyncio.get_running_loop()
    config = load_config()

    # Validate API keys depending on provider
    if config.provider == "anthropic" and not config.anthropic_api_key:
        console.print("[bold red]Error: ANTHROPIC_API_KEY is not set.[/] Please check config.json or environment variables.")
        return
    if config.provider in ("openai", "openrouter") and not (config.openai_api_key or config.openrouter_api_key):
        console.print("[bold yellow]Warning: OPENAI_API_KEY / OPENROUTER_API_KEY not found in config.[/]")

    console.print("[bold blue]Connecting to MCP Filesystem Server...[/]")
    mcp_client = MCPClient(
        command=config.mcp_server_command,
        args=config.mcp_server_args,
        allowed_directories=config.allowed_directories,
    )
    active_mcp_client = mcp_client

    async with mcp_client:
        planner = LLMPlanner(
            config=config,
            mcp_client=mcp_client,
            on_tool_start=on_tool_start_callback,
            on_tool_end=on_tool_end_callback,
        )
        active_planner = planner

        print_banner(config, tool_count=len(mcp_client.tools))

        if args.text:
            # One-shot text query execution
            console.print(f"[bold cyan]Running one-shot command:[/] {args.text}")
            await process_utterance(args.text, planner)
            return

        if args.interactive:
            # Interactive CLI mode
            await interactive_cli(planner)
            return

        # Voice Push-to-Talk mode
        console.print("[bold green]Ready! [white]오른쪽 Command (R-cmd) 키를 누른 채로 말하세요.[/][/]")
        console.print("[dim]종료하려면 Ctrl+C를 누르세요.\n[/dim]")

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()

        try:
            while True:
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Voice-Action AI v2.0 MVP")
    parser.add_argument("--text", "-t", type=str, help="단일 텍스트 명령 직접 실행 (마이크 없이 테스트)")
    parser.add_argument("--interactive", "-i", action="store_true", help="텍스트 대화형 콘솔 모드 실행")
    parser.add_argument("--check", action="store_true", help="환경 및 의존성 진단")
    args = parser.parse_args()

    if args.check:
        console.print("[bold cyan]Running environment check...[/]")
        cfg = load_config()
        console.print(f"Provider: {cfg.provider}, Model: {cfg.model}")
        console.print(f"Allowed dirs: {cfg.allowed_directories}")
        console.print("[bold green]Check passed.[/]")
        return

    try:
        asyncio.run(run_app(args))
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Voice-Action AI terminated by user.[/]")


if __name__ == "__main__":
    main()
