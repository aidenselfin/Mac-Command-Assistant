"""
PRD Section 5: MVP Verification Scenarios
1. 조회 테스트 (list_directory)
2. 생성 테스트 (write_file)
3. 복합 내용 작성 테스트 (read_text_file -> LLM -> write_file)
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from config import Config
from mcp_client import MCPClient
from planner import LLMPlanner


@pytest.mark.asyncio
async def test_scenario_1_list_files(tmp_path):
    """시나리오 1: 조회 테스트 ('바탕화면에 있는 파일 목록 보여줘')"""
    workspace = str(tmp_path)
    (tmp_path / "report1.pdf").touch()
    (tmp_path / "todo.txt").touch()

    client = MCPClient(allowed_directories=[workspace])
    cfg = Config(provider="openai", model="gpt-4o-mini", openai_api_key="test-key")

    async with client:
        planner = LLMPlanner(config=cfg, mcp_client=client)

        mock_call = MagicMock()
        mock_call.id = "call_list"
        mock_call.function.name = "list_directory"
        mock_call.function.arguments = f'{{"path": "{workspace}"}}'

        choice1 = MagicMock(message=MagicMock(content=None, tool_calls=[mock_call]))
        choice2 = MagicMock(message=MagicMock(content="디렉토리에 report1.pdf, todo.txt 파일이 있습니다.", tool_calls=None))

        with patch("openai.AsyncOpenAI") as mock_openai_cls:
            mock_inst = MagicMock()
            mock_openai_cls.return_value = mock_inst
            mock_inst.chat.completions.create = AsyncMock(side_effect=[
                MagicMock(choices=[choice1]),
                MagicMock(choices=[choice2]),
            ])

            res = await planner.execute("바탕화면에 있는 파일 목록 보여줘")
            assert res.success is True
            assert len(res.actions) == 1
            assert res.actions[0].tool_name == "list_directory"
            assert "report1.pdf" in res.actions[0].result
            assert "todo.txt" in res.actions[0].result


@pytest.mark.asyncio
async def test_scenario_2_create_note(tmp_path):
    """시나리오 2: 생성 테스트 ('테스트 폴더에 오늘 날짜로 메모장 파일 하나 만들어줘')"""
    workspace = str(tmp_path)
    target_file = f"{workspace}/2026-09-02_memo.txt"

    client = MCPClient(allowed_directories=[workspace])
    cfg = Config(provider="openai", model="gpt-4o-mini", openai_api_key="test-key")

    async with client:
        planner = LLMPlanner(config=cfg, mcp_client=client)

        mock_call = MagicMock()
        mock_call.id = "call_write"
        mock_call.function.name = "write_file"
        mock_call.function.arguments = f'{{"path": "{target_file}", "content": "오늘의 메모 내용입니다."}}'

        choice1 = MagicMock(message=MagicMock(content=None, tool_calls=[mock_call]))
        choice2 = MagicMock(message=MagicMock(content="오늘 날짜 메모장 파일을 생성했습니다.", tool_calls=None))

        with patch("openai.AsyncOpenAI") as mock_openai_cls:
            mock_inst = MagicMock()
            mock_openai_cls.return_value = mock_inst
            mock_inst.chat.completions.create = AsyncMock(side_effect=[
                MagicMock(choices=[choice1]),
                MagicMock(choices=[choice2]),
            ])

            res = await planner.execute("테스트 폴더에 오늘 날짜로 메모장 파일 하나 만들어줘")
            assert res.success is True
            assert len(res.actions) == 1
            assert Path(target_file).exists()
            assert Path(target_file).read_text() == "오늘의 메모 내용입니다."


@pytest.mark.asyncio
async def test_scenario_3_read_and_summarize(tmp_path):
    """시나리오 3: 복합 내용 작성 테스트 ('README.md 파일 읽고 요약해서 summary.txt로 저장해줘')"""
    workspace = str(tmp_path)
    readme_path = f"{workspace}/README.md"
    summary_path = f"{workspace}/summary.txt"

    Path(readme_path).write_text("# Project Info\nVoice-Action AI v2.0 MVP is an audio-to-MCP automation system.")

    client = MCPClient(allowed_directories=[workspace])
    cfg = Config(provider="openai", model="gpt-4o-mini", openai_api_key="test-key")

    async with client:
        planner = LLMPlanner(config=cfg, mcp_client=client)

        # Turn 1: read_text_file
        mock_call_read = MagicMock()
        mock_call_read.id = "call_read"
        mock_call_read.function.name = "read_text_file"
        mock_call_read.function.arguments = f'{{"path": "{readme_path}"}}'
        choice1 = MagicMock(message=MagicMock(content=None, tool_calls=[mock_call_read]))

        # Turn 2: write_file with summary
        mock_call_write = MagicMock()
        mock_call_write.id = "call_write"
        mock_call_write.function.name = "write_file"
        mock_call_write.function.arguments = f'{{"path": "{summary_path}", "content": "요약: Voice-Action AI v2.0 MVP 음성 자동화 시스템"}}'
        choice2 = MagicMock(message=MagicMock(content=None, tool_calls=[mock_call_write]))

        # Turn 3: Final message
        choice3 = MagicMock(message=MagicMock(content="README.md를 읽고 summary.txt로 요약 저장을 완료했습니다.", tool_calls=None))

        with patch("openai.AsyncOpenAI") as mock_openai_cls:
            mock_inst = MagicMock()
            mock_openai_cls.return_value = mock_inst
            mock_inst.chat.completions.create = AsyncMock(side_effect=[
                MagicMock(choices=[choice1]),
                MagicMock(choices=[choice2]),
                MagicMock(choices=[choice3]),
            ])

            res = await planner.execute("README.md 파일 읽고 요약해서 summary.txt로 저장해줘")
            assert res.success is True
            assert len(res.actions) == 2
            assert res.actions[0].tool_name == "read_text_file"
            assert res.actions[1].tool_name == "write_file"
            assert Path(summary_path).exists()
            assert "요약:" in Path(summary_path).read_text()
