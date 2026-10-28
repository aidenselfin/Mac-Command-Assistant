"""Test-only one-shot run of a command (the old `--text` path): Planner + RuntimeController."""

from openai import AsyncOpenAI
from rich.console import Console

import main
from voice_action.feedback import build_feedback
from voice_action.planner import OpenAICompatibleModel, Planner
from voice_action.runtime import RuntimeController


async def run_text(config, text: str, console: Console, *, allow_local_web=False,
                   undo_journal=None) -> None:
    manager = main._build_mcp_manager(config)
    feedback = build_feedback(config, lambda line: console.print(line, markup=False))
    try:
        async with manager.connect():
            async with AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.llm_timeout_seconds,
                max_retries=0,
            ) as client:
                model = OpenAICompatibleModel(client, config.model)
                planner = Planner(
                    config,
                    manager,
                    feedback.wrap(model) if feedback else model,
                    emit=lambda line: console.print(line, markup=False, highlight=False),
                    tools=await manager.list_tools(),
                    allow_local_web=allow_local_web,
                    undo_journal=undo_journal,
                )
                controller = RuntimeController(
                    planner,
                    emit=lambda message: console.print(message, markup=False),
                    feedback=feedback,
                )
                main.print_outcome(console, await controller.submit(text))
    finally:
        if feedback is not None:
            await feedback.close()
