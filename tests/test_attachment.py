"""A dropped file becomes the target of one command, inside the allowed folders only."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from voice_action.attachment import AttachmentError, check_attachment
from voice_action.conversion import CONVERT_TOOL
from voice_action.deletion import DELETE_TOOL
from voice_action.mcp_client import ToolDefinition, ToolResult
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    Completed,
    Error,
    ModelTurn,
    Planner,
    ToolCall,
)
from voice_action.undo import UndoJournal

TOOLS = [
    ToolDefinition(name, name, {"type": "object"})
    for name in ("filesystem__read_text_file", "filesystem__write_file")
]


def roots_of(config):
    return config.allowed_directories


def test_a_file_inside_the_allowed_folder_is_accepted_with_spaces_and_hangul(config):
    root = roots_of(config)[0]
    target = root / "하위 폴더" / "회의 노트 (최종).md"
    target.parent.mkdir()
    target.write_text("x")
    assert check_attachment(str(target), roots_of(config)) == str(target)
    assert check_attachment(f"  {target}  ", roots_of(config)) == str(target)


@pytest.mark.parametrize(
    "kind", ["outside", "link", "ancestor_link", "folder", "missing", "relative"]
)
def test_everything_else_is_refused_before_a_command_exists(config, tmp_path, kind):
    root = roots_of(config)[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    (root / "real.txt").write_text("x")
    (root / "dir").mkdir()
    (root / "inner-link.txt").symlink_to(root / "real.txt")
    (root / "out-link").symlink_to(outside, target_is_directory=True)
    raw = {
        "outside": str(outside / "secret.txt"),
        "link": str(root / "inner-link.txt"),
        "ancestor_link": str(root / "out-link" / "secret.txt"),
        "folder": str(root / "dir"),
        "missing": str(root / "nope.txt"),
        "relative": "real.txt",
    }[kind]
    with pytest.raises(AttachmentError) as error:
        check_attachment(raw, roots_of(config))
    assert str(root) in str(error.value) or kind == "folder"


@pytest.mark.parametrize("raw", [None, "", "   ", 3])
def test_empty_or_non_text_paths_are_refused(config, raw):
    with pytest.raises(AttachmentError):
        check_attachment(raw, roots_of(config))


# ---- planner ----------------------------------------------------------------------------


def make(config, turns):
    gateway = AsyncMock(tools=[])
    gateway.list_tools.return_value = list(TOOLS)
    gateway.validate_result_path.side_effect = lambda value: Path(value)
    gateway.call_tool.return_value = ToolResult(True, "본문")

    async def normalize(_name, arguments):
        return dict(arguments)

    gateway.normalize_tool_arguments.side_effect = normalize
    model = AsyncMock()
    model.generate.side_effect = list(turns)
    root = roots_of(config)[0]
    undo = UndoJournal(root / ".att-undo.sqlite3", config.allowed_directories)
    return Planner(config, gateway, model, undo_journal=undo), gateway, model


def call(name, arguments):
    return ModelTurn(None, [ToolCall("c", name, json.dumps(arguments, ensure_ascii=False))])


async def test_the_attached_file_is_the_target_without_any_search(config):
    root = roots_of(config)[0]
    target = root / "보고서 초안.md"
    target.write_text("x")
    planner, gateway, model = make(
        config,
        [call("filesystem__read_text_file", {"path": str(target)}), ModelTurn("요약", [])],
    )

    outcome = await planner.run("이 파일 요약해줘", attachment=str(target))

    assert outcome == Completed("요약")
    gateway.call_tool.assert_awaited_once_with("filesystem__read_text_file", {"path": str(target)})
    first = model.generate.await_args_list[0]
    assert f"사용자가 첨부한 파일: {target}" in first.args[0][0]["content"]
    assert "local__find_files" in {tool.name for tool in first.args[1]}  # offered, but refused:


async def test_search_and_other_files_are_refused_while_a_file_is_attached(config):
    root = roots_of(config)[0]
    target, other = root / "a.md", root / "b.md"
    target.write_text("x")
    other.write_text("y")
    planner, gateway, model = make(
        config,
        [
            call("local__find_files", {"keywords": ["b"]}),
            call("filesystem__read_text_file", {"path": str(other)}),
            call("filesystem__read_text_file", {"path": str(target)}),
            ModelTurn("요약", []),
        ],
    )
    assert await planner.run("이 파일 요약해줘", attachment=str(target)) == Completed("요약")
    gateway.call_tool.assert_awaited_once_with("filesystem__read_text_file", {"path": str(target)})


async def test_a_content_request_cannot_finish_without_reading_the_attached_file(config):
    root = roots_of(config)[0]
    target = root / "a.md"
    target.write_text("x")
    planner, _, _ = make(config, [ModelTurn("읽지 않고 답합니다", [])])
    outcome = await planner.run("이 파일 요약해줘", attachment=str(target))
    assert isinstance(outcome, Error) and "읽지 않아" in outcome.message


async def test_a_file_outside_the_folders_refuses_the_whole_request(config, tmp_path):
    outside = tmp_path / "밖.txt"
    outside.write_text("x")
    planner, gateway, model = make(config, [])
    outcome = await planner.run("이 파일 요약해줘", attachment=str(outside))
    assert isinstance(outcome, Error) and "요청을 거부" in outcome.message
    model.generate.assert_not_awaited()
    gateway.call_tool.assert_not_awaited()


async def test_a_file_swapped_for_a_link_after_the_drop_is_refused_at_run_time(config, tmp_path):
    root = roots_of(config)[0]
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    target = root / "a.md"
    target.write_text("x")
    assert check_attachment(str(target), roots_of(config))
    target.unlink()
    os.symlink(secret, target)
    planner, _, model = make(config, [])
    outcome = await planner.run("이 파일 요약해줘", attachment=str(target))
    assert isinstance(outcome, Error)
    model.generate.assert_not_awaited()


async def test_deleting_the_attachment_needs_no_filename_in_the_text_but_still_approval(config):
    root = roots_of(config)[0]
    target = root / "임시 메모.txt"
    other = root / "keep.txt"
    target.write_text("x")
    other.write_text("y")
    planner, _, model = make(
        config,
        [
            call(DELETE_TOOL, {"paths": [str(other)]}),  # a different file: refused
            call(DELETE_TOOL, {"paths": [str(target)]}),
        ],
    )
    pending = await planner.run("이 파일 삭제해줘", attachment=str(target))
    assert isinstance(pending, ApprovalRequired) and pending.operation == "delete"
    assert pending.targets == (str(target),)
    assert target.exists() and other.exists()  # nothing happens before approval
    assert "첨부한 파일 하나만" in model.generate.await_args_list[1].args[0][-1]["content"]


async def test_a_delete_without_an_attachment_still_needs_a_named_file(config):
    planner, _, model = make(config, [])
    outcome = await planner.run("이 파일 삭제해줘")
    assert isinstance(outcome, Error) and "지정해 주세요" in outcome.message
    model.generate.assert_not_awaited()


async def test_converting_is_limited_to_the_attached_file(config):
    root = roots_of(config)[0]
    target, other = root / "a.txt", root / "b.txt"
    target.write_text("x")
    other.write_text("y")
    planner, _, model = make(
        config,
        [
            call(CONVERT_TOOL, {"source": str(other), "target_format": "html"}),
            ModelTurn("끝", []),
        ],
    )
    await planner.run("이 파일 html로 변환해줘", attachment=str(target))
    assert "첨부한 파일만 변환" in model.generate.await_args_list[1].args[0][-1]["content"]


async def test_the_attachment_survives_a_clarification_and_the_approval_that_follows(config):
    root = roots_of(config)[0]
    target = root / "정리할 문서.md"
    target.write_text("x")
    planner, _, model = make(
        config,
        [
            call("filesystem__read_text_file", {"path": str(target)}),
            call("filesystem__write_file", {"path": str(target), "content": "정리됨"}),
        ],
    )

    question = await planner.run("이 문서 보기 좋게 정리해줘", attachment=str(target))
    assert isinstance(question, Clarification) and question.context.attachment == str(target)

    pending = await planner.resume(question.context, "맞춤법과 표현 교정")
    assert isinstance(pending, ApprovalRequired)
    assert pending.context.attachment == str(target)
    assert "사용자가 첨부한 파일" in model.generate.await_args_list[0].args[0][0]["content"]
