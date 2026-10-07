"""Which files a command changed, and the Finder reveal that follows."""

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from voice_action.conversion import CONVERT_TOOL
from voice_action.mcp_client import ToolDefinition, ToolResult
from voice_action.planner import ApprovalRequired, Completed, ModelTurn, Planner, ToolCall
from voice_action.undo import UndoJournal

TOOLS = [
    ToolDefinition(name, name, {"type": "object"})
    for name in ("filesystem__read_text_file", "filesystem__write_file")
]


def make(config, turns):
    gateway = AsyncMock(tools=[])
    gateway.list_tools.return_value = list(TOOLS)
    gateway.validate_result_path.side_effect = lambda value: Path(value)
    gateway.call_tool.return_value = ToolResult(True, "ok")

    async def normalize(_name, arguments):
        return dict(arguments)

    gateway.normalize_tool_arguments.side_effect = normalize
    model = AsyncMock()
    model.generate.side_effect = list(turns)
    root = config.allowed_directories[0]
    undo = UndoJournal(root / ".finder-undo.sqlite3", config.allowed_directories)
    return Planner(config, gateway, model, undo_journal=undo), gateway


def call(name, arguments):
    return ModelTurn(None, [ToolCall("c", name, json.dumps(arguments, ensure_ascii=False))])


async def test_write_success_reports_the_exact_path_the_app_wrote(config):
    root = config.allowed_directories[0]
    target = root / "한글 요약.txt"
    planner, _ = make(
        config,
        [call("filesystem__write_file", {"path": str(target), "content": "x"}),
         ModelTurn("저장했습니다. /etc/passwd에 있습니다.", [])],
    )
    pending = await planner.run("한글 요약.txt에 x 저장해줘")
    assert isinstance(pending, ApprovalRequired)

    done = await planner.approve(pending.context)

    # The model's sentence names another path; the result carries only what was written.
    assert isinstance(done, Completed) and done.changed_paths == (str(target),)


async def test_nothing_is_reported_before_approval_or_after_a_reject(config):
    root = config.allowed_directories[0]
    planner, gateway = make(
        config, [call("filesystem__write_file", {"path": str(root / "a.txt"), "content": "x"})]
    )
    pending = await planner.run("a.txt에 x 저장해줘")
    assert isinstance(pending, ApprovalRequired)  # not a Completed: nothing to reveal yet
    rejected = planner.reject(pending.context)
    assert rejected.changed_paths == ()
    gateway.call_tool.assert_not_awaited()


async def test_a_failed_write_reports_no_path(config):
    root = config.allowed_directories[0]
    planner, gateway = make(
        config,
        [call("filesystem__write_file", {"path": str(root / "a.txt"), "content": "x"}),
         ModelTurn("실패했습니다.", [])],
    )
    gateway.call_tool.return_value = ToolResult(False, "disk full", error="disk full")
    pending = await planner.run("a.txt에 x 저장해줘")
    done = await planner.approve(pending.context)
    assert getattr(done, "changed_paths", ()) == ()


async def test_a_read_only_answer_changes_nothing(config):
    root = config.allowed_directories[0]
    (root / "a.txt").write_text("x")
    planner, _ = make(
        config, [call("filesystem__read_text_file", {"path": str(root / "a.txt")}),
                 ModelTurn("x입니다.", [])]
    )
    assert (await planner.run("a.txt 읽어줘")).changed_paths == ()


async def test_delete_reports_no_path_to_reveal(config):
    root = config.allowed_directories[0]
    (root / "old.log").write_text("x")
    planner, _ = make(
        config, [call("local__delete_files", {"paths": [str(root / "old.log")]})]
    )
    pending = await planner.run("old.log 삭제해줘")
    assert isinstance(pending, ApprovalRequired) and pending.operation == "delete"
    done = await planner.approve(pending.context)
    assert isinstance(done, Completed) and done.changed_paths == ()
    assert not (root / "old.log").exists()


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("textutil"), reason="macOS")
async def test_conversion_reports_the_new_file_not_the_source(config):
    root = config.allowed_directories[0]
    (root / "메모.txt").write_text("hello")
    planner, _ = make(
        config,
        [call(CONVERT_TOOL, {"source": str(root / "메모.txt"), "target_format": "html"}),
         ModelTurn("변환했습니다.", [])],
    )
    pending = await planner.run("메모.txt를 html로 변환해줘")
    done = await planner.approve(pending.context)
    assert done.changed_paths == (str(root / "메모.html"),)
    assert Path(done.changed_paths[0]).exists()  # noqa: ASYNC240 - one stat in a test
