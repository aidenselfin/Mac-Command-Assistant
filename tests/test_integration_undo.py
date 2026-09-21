import asyncio
import json
import os
from unittest.mock import AsyncMock

import anyio
import pytest

from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient
from voice_action.mcp_manager import MCPManager
from voice_action.planner import ApprovalRequired, Completed, ModelTurn, Planner, ToolCall
from voice_action.undo import UndoJournal

pytestmark = pytest.mark.integration


@pytest.fixture
async def processes(monkeypatch):
    spawned = []
    original = anyio.open_process

    async def record(*args, **kwargs):
        process = await original(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(anyio, "open_process", record)
    yield spawned
    leaked = [process for process in spawned if process.returncode is None]
    for process in leaked:
        process.kill()
        await process.wait()
    assert not leaked, "Filesystem MCP subprocess leaked"
    for process in spawned:
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)


def write_turn(path, content):
    return ModelTurn(
        None,
        [
            ToolCall(
                "write",
                "filesystem__write_file",
                json.dumps({"path": str(path), "content": content}),
            )
        ],
    )


async def approved_write(config, tmp_path, processes, target, content):
    manager = MCPManager(
        {"filesystem": MCPClient(config, allowed_tools=SUPPORTED_TOOLS)}
    )
    model = AsyncMock()
    model.generate.side_effect = [write_turn(target, content), ModelTurn("완료", [])]
    undo_journal = UndoJournal(tmp_path / f"{target.name}.sqlite3", config.allowed_directories)

    async with manager.connect():
        planner = Planner(config, manager, model, undo_journal=undo_journal)
        pending = await planner.run(f"{target.name}에 저장해줘")
        assert isinstance(pending, ApprovalRequired)
        outcome = await planner.approve(pending.context)
        assert outcome == Completed("완료")
        assert await asyncio.to_thread(target.read_text, encoding="utf-8") == content
        undo_outcome = await planner.undo_last_write()
        assert model.generate.await_count == 2
        assert len(processes) == 1
    return undo_outcome, undo_journal


async def test_real_approved_create_write_then_undo_removes_file(
    config, tmp_path, processes
):
    target = config.allowed_directories[0] / "integration-created.txt"

    outcome, undo_journal = await approved_write(
        config, tmp_path, processes, target, "created content"
    )

    assert outcome == Completed("생성한 파일을 되돌렸습니다.")
    assert not await asyncio.to_thread(target.exists)
    assert undo_journal.entry_count(status="undone") == 1


async def test_real_approved_overwrite_then_undo_restores_original(
    config, tmp_path, processes
):
    target = config.allowed_directories[0] / "integration-existing.txt"
    original = "원본 bytes 😀\n".encode()
    await asyncio.to_thread(target.write_bytes, original)

    outcome, undo_journal = await approved_write(
        config, tmp_path, processes, target, "replacement"
    )

    assert outcome == Completed("이전 파일 내용을 복원했습니다.")
    assert await asyncio.to_thread(target.read_bytes) == original
    assert undo_journal.entry_count(status="undone") == 1
