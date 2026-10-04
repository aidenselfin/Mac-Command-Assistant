"""Run a text command and append one privacy-bounded JSONL record per execution."""

import argparse
import asyncio
import fcntl
import hashlib
import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from rich.console import Console

from main import _build_mcp_manager, _build_undo_journal, print_outcome
from voice_action.config import load_config
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    Completed,
    Error,
    ModelClient,
    ModelTurn,
    OpenAICompatibleModel,
    Planner,
    PlannerError,
)
from voice_action.runtime import RuntimeController

DEFAULT_LOG_PATH = Path("llm_mcp_transcript.log")
RAW_PAYLOAD_FLAG = "--include-raw-payloads"


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat()


def _digest(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _summary(value: object) -> dict[str, int | str]:
    text = str(value)
    return {"chars": len(text), "sha256": _digest(text)}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(
                token in key.casefold()
                for token in ("api_key", "authorization", "password", "token", "secret")
            )
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        lowered = value.casefold()
        if any(
            marker in lowered
            for marker in ("sk-", "gsk_", "bearer ", "api_key=", "openai_api_key", "groq_api_key")
        ):
            return "[REDACTED]"
    return value


def _raw(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    return _redact(value)


def read_records(path: Path) -> list[dict[str, Any]]:
    """Parse the append-only JSONL transcript format by execution record."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


class TranscriptLog:
    """Collect one execution and atomically append its bounded JSONL record."""

    def __init__(self, path: Path, *, include_raw_payloads: bool = False):
        self.path = path
        self.include_raw_payloads = include_raw_payloads
        self.run_id = str(uuid.uuid4())
        self._started_at: str | None = None
        self._events: list[dict[str, Any]] = []
        self._finished = False
        self.write_error: str | None = None

    def start(self, text: str) -> None:
        self._started_at = _timestamp()
        event: dict[str, Any] = {
            "event": "start",
            "sequence": 1,
            "user_text": _summary(text),
        }
        if self.include_raw_payloads:
            event["raw_user_text"] = _redact(text)
        self._events.append(event)

    def event(self, event: str, **fields: Any) -> None:
        self._events.append({"event": event, "sequence": len(self._events) + 1, **fields})

    def finish(
        self,
        status: str,
        *,
        outcome: object | None = None,
        error: BaseException | None = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        record: dict[str, Any] = {
            "version": 1,
            "run_id": self.run_id,
            "started_at": self._started_at or _timestamp(),
            "ended_at": _timestamp(),
            "status": status,
            "events": self._events,
        }
        if outcome is not None:
            record["outcome"] = _outcome_summary(outcome, self.include_raw_payloads)
        if error is not None:
            record["error"] = {
                "type": type(error).__name__,
                "message": _summary(error),
            }
            if self.include_raw_payloads:
                record["error"]["raw_message"] = _redact(str(error))
        try:
            self._append_record(record)
        except Exception as exc:
            self.write_error = type(exc).__name__

    def _append_record(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.write(encoded)
                stream.flush()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _outcome_summary(outcome: object, include_raw_payloads: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"type": type(outcome).__name__}
    if isinstance(outcome, Completed):
        result["text"] = _summary(outcome.text)
    elif isinstance(outcome, Error):
        result["message"] = _summary(outcome.message)
    elif isinstance(outcome, Clarification):
        result["question"] = _summary(outcome.question)
        result["option_count"] = len(outcome.options)
    elif isinstance(outcome, ApprovalRequired):
        result.update(
            {
                "tool_name": outcome.tool_name,
                "operation": outcome.operation,
                "target_path": _summary(outcome.target_path),
                "content_chars": outcome.content_chars,
            }
        )
    if include_raw_payloads:
        result["raw"] = _raw(outcome)
    return result


class TranscriptModel(ModelClient):
    """Record bounded model-turn metadata; raw payloads require explicit opt-in."""

    def __init__(self, model: ModelClient, transcript: TranscriptLog):
        self._model = model
        self._transcript = transcript
        self._turn = 0

    async def generate(
        self, messages, tools, *, allow_tools, allow_clarification=True
    ) -> ModelTurn:
        self._turn += 1
        input_event: dict[str, Any] = {
            "turn": self._turn,
            "message_count": len(messages),
            "message_roles": [message.get("role") for message in messages],
            "tool_names": [tool.name for tool in tools],
            "allow_tools": allow_tools,
            "allow_clarification": allow_clarification,
        }
        if self._transcript.include_raw_payloads:
            input_event["raw_messages"] = _raw(messages)
            input_event["raw_tools"] = _raw(
                [
                    {"name": tool.name, "description": tool.description, "schema": tool.schema}
                    for tool in tools
                ]
            )
        self._transcript.event("llm_input", **input_event)
        turn = await self._model.generate(
            messages,
            tools,
            allow_tools=allow_tools,
            allow_clarification=allow_clarification,
        )
        output_event: dict[str, Any] = {
            "turn": self._turn,
            "text": _summary(turn.text or ""),
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": _summary(call.arguments)}
                for call in turn.tool_calls
            ],
            "clarification": None
            if turn.clarification is None
            else {
                "kind": turn.clarification.kind,
                "question": _summary(turn.clarification.question),
                "option_count": len(turn.clarification.options),
            },
        }
        if self._transcript.include_raw_payloads:
            output_event["raw"] = _raw(turn)
        self._transcript.event("llm_output", **output_event)
        return turn


async def run(
    text: str, overrides: dict[str, Any], console: Console, transcript: TranscriptLog
) -> None:
    transcript.start(text)
    try:
        config = load_config(overrides)
        transcript.event("configuration", model=config.model, provider=config.provider)
        manager = _build_mcp_manager(config)
        async with manager.connect():
            async with AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.llm_timeout_seconds,
                max_retries=0,
            ) as client:
                model = TranscriptModel(
                    OpenAICompatibleModel(client, config.model), transcript
                )
                planner = Planner(
                    config,
                    manager,
                    model,
                    emit=lambda message: console.print(message, markup=False),
                    tools=await manager.list_tools(),
                    undo_journal=_build_undo_journal(config),
                )
                outcome = await RuntimeController(
                    planner,
                    emit=lambda message: console.print(message, markup=False),
                ).submit(text)
    except PlannerError as exc:
        transcript.finish("planner_error", error=exc)
        console.print(f"[ERROR] {exc}", markup=False)
        console.print(f"[LOG] {transcript.path}", markup=False)
        return
    except asyncio.CancelledError as exc:
        transcript.finish("cancelled", error=exc)
        raise
    except BaseException as exc:
        transcript.finish("exception", error=exc)
        raise
    transcript.finish("completed", outcome=outcome)
    print_outcome(console, outcome)
    console.print(f"[LOG] {transcript.path}", markup=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Append bounded LLM/MCP execution records.")
    parser.add_argument("text", help="Text command to run")
    parser.add_argument("--provider", choices=("openai", "groq"))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--allowed-directory", action="append", dest="allowed_directories")
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument(
        RAW_PAYLOAD_FLAG,
        action="store_true",
        help="Developer-only: include redacted raw model payloads in the transcript.",
    )
    args = vars(parser.parse_args())
    text = args.pop("text")
    transcript = TranscriptLog(
        args.pop("log_file"), include_raw_payloads=args.pop("include_raw_payloads")
    )
    asyncio.run(run(text, args, Console(), transcript))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
