"""local__run_read_command: one table for the command check, a few real runs for the output."""

import json
import os
import unicodedata
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from voice_action import read_command
from voice_action.mcp_client import ToolDefinition
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    ClarificationRequest,
    ModelTurn,
    Planner,
    ToolCall,
)
from voice_action.read_command import (
    READ_COMMAND_TOOL,
    CommandError,
    OutsideError,
    check,
    run_read_command,
)
from voice_action.undo import UndoJournal


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "root"
    (base / "sub").mkdir(parents=True)
    (base / "a.txt").write_text("hello budget\n")
    (base / "sub" / "b.md").write_text("budget plan\n")
    (tmp_path / "outside.txt").write_text("secret budget\n")
    os.symlink(tmp_path / "outside.txt", base / "link.txt")
    return base.resolve()


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["find", "{root}", "-name", "*.txt"], None),
        (["grep", "-rl", "budget", "{root}"], None),
        (["mdfind", "-onlyin", "{root}", "budget"], None),
        (["head", "-n", "5", "{root}/a.txt"], None),
        (["rm", "{root}/a.txt"], CommandError),  # not a read program
        (["/tmp/find", "{root}"], CommandError),  # same name, other binary
        (["find", "{root}", "-delete"], CommandError),
        (["find", "{root}", "-exec", "cat", "{}", ";"], CommandError),
        (["find", "-L", "{root}"], CommandError),
        (["grep", "-Rl", "x", "{root}"], CommandError),
        (["mdfind", "budget"], CommandError),  # -onlyin is required
        (["mdfind", "-live", "-onlyin", "{root}", "x"], CommandError),
        (["find", "{root}/..", "-name", "x"], OutsideError),
        (["grep", "-rl", "x", "/etc"], OutsideError),
        (["head", "{root}/link.txt"], OutsideError),  # a link leading out
        (["mdfind", "-onlyin", "/", "x"], OutsideError),
        (["grep", "-f", "/etc/passwd", "{root}"], OutsideError),  # a pattern file is a path too
        (["find", "{root}", "-name", "x; rm -rf /"], None),  # no shell: just a name pattern
        ("find {root}", CommandError),  # a shell string, not an argv list
    ],
)
def test_check(root, argv, error):
    argv = argv.replace("{root}", str(root)) if isinstance(argv, str) else [
        part.replace("{root}", str(root)) for part in argv
    ]
    if error is None:
        assert check(argv, (root,))[0].startswith("/")
    else:
        with pytest.raises(error):
            check(argv, (root,))


def test_run_keeps_inside_paths_only_and_normalizes(root):
    (root / unicodedata.normalize("NFD", "회의록.txt")).write_text("budget\n")
    result = run_read_command({"argv": ["grep", "-rl", "budget", str(root)]}, (root,))
    assert result.ok
    paths = result.structured["paths"]
    assert str(root / "a.txt") in paths and str(root / "sub" / "b.md") in paths
    assert unicodedata.normalize("NFC", str(root / "회의록.txt")) in paths
    assert "outside" not in result.text and str(root / "link.txt") not in paths


def test_run_cuts_long_output(root, monkeypatch):
    monkeypatch.setattr(read_command, "MAX_LINES", 2)
    result = run_read_command({"argv": ["find", str(root)]}, (root,))
    assert result.truncated and "잘림" in result.text and len(result.structured["paths"]) <= 2


def test_run_stops_at_timeout(root, monkeypatch):
    monkeypatch.setattr(read_command, "TIMEOUT", 0.2)
    monkeypatch.setitem(read_command.PROGRAMS, "head", "/bin/sleep")  # a command that hangs
    result = run_read_command({"argv": ["head", "5"]}, (root,))
    assert result.truncated and "시간" in result.text


def _planner(config, turns):
    gateway = AsyncMock(tools=[])
    gateway.list_tools.return_value = [ToolDefinition("filesystem__read_text_file", "r", {})]
    gateway.validate_result_path.side_effect = lambda value: Path(value)

    async def normalize(_name, arguments):
        return dict(arguments)

    gateway.normalize_tool_arguments.side_effect = normalize
    model = AsyncMock()
    model.generate.side_effect = list(turns)
    root = config.allowed_directories[0]
    undo = UndoJournal(root.parent / "undo.sqlite3", config.allowed_directories)
    return Planner(replace(config, search_commands=True), gateway, model, undo_journal=undo), model


def _call(name, arguments):
    return ModelTurn(None, [ToolCall("c", name, json.dumps(arguments, ensure_ascii=False))])


async def test_delete_target_must_come_from_a_read_command(config):
    root = config.allowed_directories[0]
    (root / "피그마-시안.fig").write_text("x")
    guess = _call("local__delete_files", {"paths": [str(root / "피그마-시안.fig")]})
    planner, model = _planner(config, [guess] + [ModelTurn("못 했습니다.", [])] * 4)
    assert not isinstance(await planner.run("피그마 파일 지워줘"), ApprovalRequired)
    tools = [tool.name for tool in model.generate.call_args_list[0].args[1]]
    assert READ_COMMAND_TOOL in tools and "local__find_files" not in tools

    search = _call(READ_COMMAND_TOOL, {"argv": ["find", str(root), "-iname", "*피그마*"]})
    planner, _ = _planner(config, [search, guess])
    assert isinstance(await planner.run("피그마 파일 지워줘"), ApprovalRequired)


async def test_model_may_ask_only_among_paths_it_found(config):
    root = config.allowed_directories[0]
    for name in ("계획-초안.md", "계획-최종.md"):
        (root / name).write_text("x")
    found = [str(root / "계획-초안.md"), str(root / "계획-최종.md")]
    ask = ModelTurn(None, [], ClarificationRequest("어느 것?", tuple(found), "file_candidate"))
    search = _call(READ_COMMAND_TOOL, {"argv": ["find", str(root), "-iname", "*계획*"]})
    planner, _ = _planner(config, [search, ask])
    assert isinstance(await planner.run("계획 문서 읽어줘"), Clarification)

    made_up = ModelTurn(None, [], ClarificationRequest("어느 것?", ("/a.md", "/b.md"),
                                                      "file_candidate"))
    planner, _ = _planner(config, [search, made_up, ModelTurn("찾았습니다.", [])])
    assert not isinstance(await planner.run("계획 문서 읽어줘"), Clarification)
