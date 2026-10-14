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

    outcome = await planner.run("예산안 어디 있어?")

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
    assert "사용자가 좁힌 조건(누적): 임시-12번" in system

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
    assert "이미 대상 파일이 정해졌습니다" in refusal


async def test_big_file_answer_says_it_was_only_partly_read(config):
    from voice_action.mcp_client import ToolResult

    root = config.allowed_directories[0]
    big = root / "큰파일.txt"
    big.write_text("x")
    model = scripted(read_turn(big), ModelTurn("요약", []))
    planner, gateway = make_planner(config, model)
    gateway.call_tool.return_value = ToolResult(True, "가" * 1234, truncated=True)

    outcome = await planner.run("큰파일.txt 요약해줘")

    assert outcome.text.startswith("요약")
    assert "파일 전체가 아니라 앞부분 약 1,234자만" in outcome.text
    gateway.call_tool.return_value = ToolResult(True, "짧음")
    model = scripted(read_turn(big), ModelTurn("요약", []))
    planner, _ = make_planner(config, model)
    planner._gateway.call_tool.return_value = ToolResult(True, "짧음")
    assert await planner.run("큰파일.txt 요약해줘") == Completed("요약")


async def test_raw_search_is_never_offered_to_the_model(config):
    for text in ("예산안 알려줘", "회의록 요약해줘"):
        model = scripted(ModelTurn("끝", []))
        planner, _ = make_planner(config, model)
        await planner.run(text)
        offered = {tool.name for tool in model.generate.await_args.args[1]}
        assert "filesystem__search_files" not in offered and "local__find_files" in offered


def read_turn_with(path, **extra):
    arguments = json.dumps({"path": str(path), **extra})
    return ModelTurn(None, [ToolCall("read", "filesystem__read_text_file", arguments)])


async def test_head_and_tail_reads_say_which_part_was_read(config):
    from voice_action.mcp_client import ToolResult

    big = config.allowed_directories[0] / "로그.txt"
    big.write_text("x")
    for extra, text, expected in (
        ({"head": 3}, "1\n2\n3", "파일 전체가 아니라 앞 3줄만"),
        ({"tail": 3}, "7\n8\n9", "파일 전체가 아니라 뒤 3줄만"),
        ({"head": 3}, "1\n2", None),  # the file ended before the limit: it was all read
    ):
        model = scripted(read_turn_with(big, **extra), ModelTurn("요약", []))
        planner, gateway = make_planner(config, model)
        gateway.call_tool.return_value = ToolResult(True, text)
        outcome = await planner.run("로그.txt 요약해줘")
        if expected is None:
            assert outcome == Completed("요약")
        else:
            assert expected in outcome.text, (extra, outcome.text)


async def test_an_incomplete_search_for_meeting_notes_names_what_it_could_not_check(config):
    root = config.allowed_directories[0]
    (root / "보고서.pdf").write_bytes(b"%PDF")  # a document whose text is never searched
    model = scripted(find_turn("회의록"))
    planner, _ = make_planner(config, model)

    outcome = await planner.run("회의록 요약해줘")

    assert isinstance(outcome, Completed)
    assert outcome.text.startswith("검사한 범위에서는 일치하는 파일을 찾지 못했습니다.")
    assert "pdf·docx 등 문서 1개" in outcome.text
    # A complete search keeps the plain wording.
    (root / "보고서.pdf").unlink()
    model = scripted(find_turn("회의록"))
    planner, _ = make_planner(config, model)
    plain = await planner.run("회의록 요약해줘")
    assert plain.text == (
        "요청과 일치하는 파일을 찾지 못했습니다. 파일명을 더 구체적으로 알려주세요."
    )


async def test_partial_read_counts_only_what_was_read_not_the_cut_off_marker(config):
    from voice_action.mcp_client import TRUNCATION_MARKER, ToolResult

    big = config.allowed_directories[0] / "큰.txt"
    big.write_text("x")
    model = scripted(read_turn(big), ModelTurn("요약", []))
    planner, gateway = make_planner(config, model)
    gateway.call_tool.return_value = ToolResult(
        True, "가" * 50_000 + TRUNCATION_MARKER, truncated=True
    )
    outcome = await planner.run("큰.txt 요약해줘")
    assert "앞부분 약 50,000자만" in outcome.text and "50,012" not in outcome.text


async def test_files_the_user_named_in_full_are_all_targets_not_a_choice(config):
    root = config.allowed_directories[0]
    for name in ("a.csv", "b.csv", "c.csv", "d.csv"):
        (root / name).write_text("x,y\n1,2\n")
    model = scripted(find_turn("a.csv", "b.csv", "c.csv"), ModelTurn("세 파일을 처리합니다", []))
    planner, _ = make_planner(config, model)

    outcome = await planner.run("a.csv, b.csv, c.csv를 모두 json으로 바꿔줘")

    assert outcome == Completed("세 파일을 처리합니다")  # no question asked
    told = json.loads(tool_text(model, 1))
    assert told["decision"] == "named" and len(told["paths"]) == 3


async def test_two_files_of_one_name_in_two_folders_are_still_a_choice(config):
    root = config.allowed_directories[0]
    for folder in ("가", "나"):
        (root / folder).mkdir()
        (root / folder / "a.csv").write_text("x")
    model = scripted(find_turn("a.csv"))
    planner, _ = make_planner(config, model)
    outcome = await planner.run("a.csv를 json으로 바꿔줘")
    assert isinstance(outcome, Clarification) and len(outcome.options) == 2


async def test_candidates_the_user_did_not_name_are_still_asked_about(config):
    root = config.allowed_directories[0]
    for name in ("계획-초안.md", "계획-최종.md"):
        (root / name).write_text("x")
    model = scripted(find_turn("계획"))
    planner, _ = make_planner(config, model)
    assert isinstance(await planner.run("계획 문서 읽어줘"), Clarification)


def tool_text(model, index):
    return [m["content"] for m in model.generate.await_args_list[index].args[0]
            if m["role"] == "tool"][-1]


async def test_a_delete_named_by_a_pattern_does_not_ask_which_file(config):
    root = config.allowed_directories[0]
    (root / "삭제연습").mkdir()
    for name in ("임시-1.txt", "임시-2.txt"):
        (root / "삭제연습" / name).write_text("x")
    model = scripted(find_turn("삭제연습", "txt"), ModelTurn("삭제를 준비합니다", []))
    planner, _ = make_planner(config, model)

    outcome = await planner.run("삭제연습 폴더의 *.txt 파일을 모두 삭제해줘")

    assert outcome == Completed("삭제를 준비합니다")  # no "which file?" question
    told = json.loads(tool_text(model, 1))
    assert told["decision"] == "pattern" and "directory와 pattern" in told["note"]


async def test_a_plain_search_with_a_star_in_it_is_not_taken_for_a_pattern_delete(config):
    root = config.allowed_directories[0]
    for name in ("계획-초안.md", "계획-최종.md"):
        (root / name).write_text("x")
    model = scripted(find_turn("계획"))
    planner, _ = make_planner(config, model)
    assert isinstance(await planner.run("계획* 문서 읽어줘"), Clarification)  # not a delete
