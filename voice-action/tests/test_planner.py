import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from config import Config
from mcp_client import MCPClient
from planner import LLMPlanner, PlannerResponse


@pytest.mark.asyncio
async def test_planner_tool_calling_loop(tmp_path):
    workspace = str(tmp_path)
    client = MCPClient(allowed_directories=[workspace])
    cfg = Config(provider="openai", model="gpt-4o-mini", openai_api_key="test-key")

    async with client:
        planner = LLMPlanner(config=cfg, mcp_client=client)

        # Mock OpenAI response: Round 1 requests tool call 'write_file', Round 2 finishes with confirmation
        mock_tool_call = MagicMock()
        mock_tool_call.id = "call_123"
        mock_tool_call.function.name = "write_file"
        mock_tool_call.function.arguments = f'{{"path": "{workspace}/note.txt", "content": "Sample Note"}}'

        # Response 1: Tool call
        choice1 = MagicMock()
        choice1.message.content = None
        choice1.message.tool_calls = [mock_tool_call]
        resp1 = MagicMock()
        resp1.choices = [choice1]

        # Response 2: Final text
        choice2 = MagicMock()
        choice2.message.content = "메모 작성이 성공적으로 완료되었습니다."
        choice2.message.tool_calls = None
        resp2 = MagicMock()
        resp2.choices = [choice2]

        with patch("openai.AsyncOpenAI") as mock_openai_cls:
            mock_instance = MagicMock()
            mock_openai_cls.return_value = mock_instance
            mock_instance.chat.completions.create = AsyncMock(side_effect=[resp1, resp2])

            res: PlannerResponse = await planner.execute("메모장 하나 써줘")

            assert res.success is True
            assert len(res.actions) == 1
            assert res.actions[0].tool_name == "write_file"
            assert "메모 작성이 성공적으로 완료되었습니다." in res.final_text
            assert Path(f"{workspace}/note.txt").exists()
