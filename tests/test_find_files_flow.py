"""local__find_files inside the planner and runtime: pick, narrow, single, resume."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

from voice_action.mcp_client import ToolDefinition
from voice_action.planner import (
    Clarification,
    ClarificationContext,
    Completed,
    Error,
    ModelTurn,
    Planner,
    ToolCall,
)
from voice_action.undo import UndoJournal

TOOLS = [
    ToolDefinition(name, name, {"type": "object"})
    for name in ("filesystem__read_text_file", "filesystem__search_files")
]


def make_planner(config, model):
    gateway = AsyncMock(tools=[])
    gateway.list_tools.return_value = list(TOOLS)
    gateway.validate_result_path.side_effect = lambda value: Path(value)
    root = config.allowed_directories[0]
    undo = UndoJournal(root / ".find-undo.sqlite3", config.allowed_directories)
    return Planner(config, gateway, model, undo_journal=undo), gateway


def find_turn(*keywords, **extra):
    arguments = json.dumps({"keywords": list(keywords), **extra}, ensure_ascii=False)
    return ModelTurn(None, [ToolCall("find", "local__find_files", arguments)])


def read_turn(path):
    arguments = json.dumps({"path": str(path)})
    return ModelTurn(None, [ToolCall("read", "filesystem__read_text_file", arguments)])


def scripted(*turns):
    model = AsyncMock()
    model.generate.side_effect = list(turns)
    return model


async def test_find_files_is_offered_and_single_match_continues(config):
    root = config.allowed_directories[0]
    (root / "예산안.md").write_text("내용")
    model = scripted(find_turn("예산안"), ModelTurn("찾았습니다", []))
    planner, _ = make_planner(config, model)

    outcome = await planner.run("예산안 알려줘")

    assert outcome == Completed("찾았습니다")
    offered = {tool.name for tool in model.generate.await_args_list[0].args[1]}
    assert "local__find_files" in offered
    tool_message = model.generate.await_args_list[1].args[0][-1]
    assert json.loads(tool_message["content"])["decision"] == "single"


async def test_few_candidates_ask_with_labels_and_resume_selects_the_file(config):
    root = config.allowed_directories[0]
    for name in ("계획-초안.md", "계획-최종.md"):
        (root / name).write_text("내용")
    model = scripted(find_turn("계획"))
    planner, _ = make_planner(config, model)

    outcome = await planner.run("계획 문서 요약해줘")

    assert isinstance(outcome, Clarification) and outcome.context.kind == "file_candidate"
    assert len(outcome.options) == len(outcome.labels) == 2
    assert outcome.options[0].startswith(str(root)) and "·" in outcome.labels[0]
    assert outcome.shown_options == outcome.labels

    chosen = outcome.options[0]
    model = scripted(read_turn(chosen), ModelTurn("요약", []))
    planner, gateway = make_planner(config, model)
    resumed = await planner.resume(outcome.context, chosen)
    assert resumed == Completed("요약")
    gateway.call_tool.assert_awaited_once_with("filesystem__read_text_file", {"path": chosen})


async def test_many_candidates_are_not_listed_but_narrowed(config):
    root = config.allowed_directories[0]
    for index in range(1, 9):
        (root / f"임시-{index}.txt").write_text("x")
    model = scripted(find_turn("임시"))
    planner, _ = make_planner(config, model)

    outcome = await planner.run("임시 파일 읽어줘")

    assert isinstance(outcome, Clarification) and outcome.context.kind == "narrow_search"
    assert "8개" in outcome.question and "임시-1 ~ 임시-8" in outcome.question
    assert all("임시-3" not in text for text in (outcome.question, *outcome.shown_options))
    assert outcome.options[0].endswith(".txt")  # the newest file is offered as a button


async def test_narrow_free_text_reruns_the_search_and_option_path_selects_it(config):
    root = config.allowed_directories[0]
    context = ClarificationContext("임시 파일 읽어줘", "narrow_search", (str(root / "a.txt"),), 1)
    model = scripted(find_turn("임시-12"), ModelTurn("끝", []))
    planner, _ = make_planner(config, model)
    assert await planner.resume(context, "임시-12번") == Completed("끝")
    system = model.generate.await_args_list[0].args[0][0]["content"]
    assert "사용자가 좁힌 조건: 임시-12번" in system

    (root / "a.txt").write_text("x")
    model = scripted(read_turn(root / "a.txt"), ModelTurn("읽음", []))
    planner, _ = make_planner(config, model)
    assert await planner.resume(context, str(root / "a.txt")) == Completed("읽음")
    assert "사용자가 선택한 파일" in model.generate.await_args_list[0].args[0][0]["content"]


async def test_narrow_resume_rejects_blank_and_other_kinds_reject_free_text(config):
    root = config.allowed_directories[0]
    planner, _ = make_planner(config, scripted())
    narrow = ClarificationContext("x", "narrow_search", (), 1)
    assert isinstance(await planner.resume(narrow, "  "), Error)
    pick = ClarificationContext("x", "file_candidate", (str(root / "a.md"),), 1)
    assert isinstance(await planner.resume(pick, "아무거나"), Error)


async def test_clarification_budget_applies_to_find_files_questions(config):
    root = config.allowed_directories[0]
    for name in ("계획-초안.md", "계획-최종.md"):
        (root / name).write_text("x")
    model = scripted(find_turn("계획"))
    planner, _ = make_planner(config, model)
    outcome = await planner._run_loop("계획 요약해줘", prior_clarifications=2)
    assert isinstance(outcome, Error) and "clarification 최대" in outcome.message


async def test_find_files_is_refused_after_the_user_picked_a_file(config):
    root = config.allowed_directories[0]
    (root / "a.md").write_text("x")
    model = scripted(find_turn("a"), read_turn(root / "a.md"), ModelTurn("읽음", []))
    planner, gateway = make_planner(config, model)
    context = ClarificationContext("a 읽어줘", "file_candidate", (str(root / "a.md"),), 1)
    assert await planner.resume(context, str(root / "a.md")) == Completed("읽음")
    refusal = model.generate.await_args_list[1].args[0][-1]["content"]
    assert "이미 파일을 선택" in refusal


async def test_big_file_answer_says_it_was_only_partly_read(config):
    from voice_action.mcp_client import ToolResult

    root = config.allowed_directories[0]
    big = root / "큰파일.txt"
    big.write_text("x")
    model = scripted(read_turn(big), ModelTurn("요약", []))
    planner, gateway = make_planner(config, model)
    gateway.call_tool.return_value = ToolResult(True, "가" * 1234, truncated=True)

    outcome = await planner.run("큰파일.txt 요약해줘")

    assert outcome.text.startswith("요약") and "앞부분 약 1,234자만" in outcome.text
    gateway.call_tool.return_value = ToolResult(True, "짧음")
    model = scripted(read_turn(big), ModelTurn("요약", []))
    planner, _ = make_planner(config, model)
    planner._gateway.call_tool.return_value = ToolResult(True, "짧음")
    assert await planner.run("큰파일.txt 요약해줘") == Completed("요약")
