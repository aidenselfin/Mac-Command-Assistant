"""Run a text command and append every effective LLM input/output turn to a log."""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from rich.console import Console

from main import _build_mcp_manager, _build_undo_journal, print_outcome
from voice_action.config import load_config
from voice_action.planner import (
    ModelClient,
    ModelTurn,
    OpenAICompatibleModel,
    Planner,
    PlannerError,
)
from voice_action.runtime import RuntimeController

DEFAULT_LOG_PATH = Path("llm_mcp_transcript.log")


class TranscriptLog:
    """Append developer-visible LLM conversation records to one UTF-8 text file."""

    def __init__(self, path: Path):
        self.path = path

    def write(self, heading: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(f"\n===== {heading} =====\n{payload}\n")

    def start(self, text: str) -> None:
        self.write(
            "TRANSCRIPT START",
            {"timestamp": datetime.now().astimezone().isoformat(), "user_text": text},
        )


class TranscriptModel(ModelClient):
    """Record the exact messages/tools handed to the model and its parsed response."""

    def __init__(self, model: OpenAICompatibleModel, transcript: TranscriptLog):
        self._model = model
        self._transcript = transcript
        self._turn = 0

    async def generate(
        self, messages, tools, *, allow_tools, allow_clarification=True
    ) -> ModelTurn:
        self._turn += 1
        self._transcript.write(
            f"LLM INPUT turn {self._turn}",
            {
                "messages": messages,
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "schema": tool.schema,
                    }
                    for tool in tools
                ],
                "allow_tools": allow_tools,
                "allow_clarification": allow_clarification,
            },
        )
        turn = await self._model.generate(
            messages,
            tools,
            allow_tools=allow_tools,
            allow_clarification=allow_clarification,
        )
        self._transcript.write(
            f"LLM OUTPUT turn {self._turn}",
            {
                "text": turn.text,
                "tool_calls": [
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in turn.tool_calls
                ],
                "clarification": (
                    None
                    if turn.clarification is None
                    else {
                        "question": turn.clarification.question,
                        "options": turn.clarification.options,
                        "kind": turn.clarification.kind,
                    }
                ),
            },
        )
        return turn


async def run(
    text: str, overrides: dict[str, Any], console: Console, transcript: TranscriptLog
) -> None:
    transcript.start(text)
    config = load_config(overrides)
    manager = _build_mcp_manager(config)
    async with manager.connect():
        async with AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.llm_timeout_seconds,
            max_retries=0,
        ) as client:
            model = TranscriptModel(OpenAICompatibleModel(client, config.model), transcript)
            planner = Planner(
                config,
                manager,
                model,
                emit=lambda message: console.print(message, markup=False),
                tools=await manager.list_tools(),
                undo_journal=_build_undo_journal(config),
            )
            try:
                outcome = await RuntimeController(
                    planner,
                    emit=lambda message: console.print(message, markup=False),
                ).submit(text)
            except PlannerError as exc:
                transcript.write("LLM ERROR", {"error": str(exc)})
                console.print(f"[ERROR] {exc}", markup=False)
                console.print(f"[LOG] {transcript.path}", markup=False)
                return
            transcript.write("FINAL OUTCOME", outcome)
            print_outcome(console, outcome)
            console.print(f"[LOG] {transcript.path}", markup=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Append LLM/MCP conversation to a log file.")
    parser.add_argument("text", help="Text command to run")
    parser.add_argument("--provider", choices=("openai", "groq"))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--allowed-directory", action="append", dest="allowed_directories")
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_PATH)
    args = vars(parser.parse_args())
    text = args.pop("text")
    transcript = TranscriptLog(args.pop("log_file"))
    asyncio.run(run(text, args, Console(), transcript))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
