"""A question only the user can answer (a file name), and a provider's 400 explained safely."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI, BadRequestError

from voice_action.mcp_client import ToolDefinition, ToolResult
from voice_action.planner import (
    CLARIFICATION_TOOL,
    Clarification,
    ClarificationContext,
    ClarificationRequest,
    Completed,
    Error,
    ModelTurn,
    OpenAICompatibleModel,
    Planner,
    PlannerError,
    ToolCall,
)
from voice_action.runtime import RuntimeController
from voice_action.undo import UndoJournal

# ---- the provider's 400 -------------------------------------------------------------------


def bad_request(body):
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return BadRequestError("raw provider text with secret-token", response=response, body=body)


async def failing_model(error):
    client = AsyncMock(spec=AsyncOpenAI)
    client.chat = AsyncMock()
    client.chat.completions.create.side_effect = error
    with pytest.raises(PlannerError) as caught:
        await OpenAICompatibleModel(client, "m").generate(
            [{"role": "user", "content": "x"}], [], allow_tools=True
        )
    return str(caught.value)


async def test_a_rejected_tool_call_names_the_tool_and_the_schema_violation():
    message = (
        "Tool call validation failed: tool call validation failed: parameters for tool "
        "request_clarification did not match schema: errors: [`/options`: minItems: got 0, "
        "want 1]"
    )
    body = {
        "message": message, "type": "invalid_request_error", "code": "tool_use_failed",
        "failed_generation": '{"arguments": {"question": "비밀 파일 본문 SECRET-BODY"}}',
    }
    text = await failing_model(bad_request(body))
    assert text == (
        "LLM API 요청 실패 (HTTP 400: tool_use_failed — request_clarification: "
        "`/options`: minItems: got 0, want 1)"
    )
    assert "SECRET" not in text and "secret-token" not in text


@pytest.mark.parametrize(
    "body, expected",
    [
        (None, "LLM API 요청 실패 (HTTP 400)"),
        ("<html>gateway</html>", "LLM API 요청 실패 (HTTP 400)"),
        (
            {"message": "context too long: 사용자 글", "code": "context_length_exceeded"},
            "LLM API 요청 실패 (HTTP 400: context_length_exceeded)",
        ),
        ({"code": "BAD CODE with spaces <script>", "message": "x"}, "LLM API 요청 실패 (HTTP 400)"),
    ],
)
async def test_only_the_error_code_is_kept_when_the_body_says_nothing_about_a_tool(body, expected):
    assert await failing_model(bad_request(body)) == expected


# ---- the question schema -----------------------------------------------------------------


def test_the_question_tool_allows_a_typed_answer_without_options():
    schema = CLARIFICATION_TOOL["function"]["parameters"]
    assert "minItems" not in schema["properties"]["options"]
    assert "free_text" in schema["properties"]["kind"]["enum"]
    arguments = json.dumps(
        {"question": "파일 이름을 알려 주세요", "options": [], "kind": "file_candidate"}
    )
    # An empty list is a typed question whatever kind the model wrote on it.
    assert OpenAICompatibleModel._parse_clarification("c", arguments) == ClarificationRequest(
        "파일 이름을 알려 주세요", (), "free_text", "c"
    )
    picked = json.dumps({"question": "?", "options": ["a", "b"], "kind": "file_candidate"})
    assert OpenAICompatibleModel._parse_clarification("c", picked).options == ("a", "b")
    typed = json.dumps({"question": "?", "options": ["a"], "kind": "free_text"})
    assert OpenAICompatibleModel._parse_clarification("c", typed).options == ()


# ---- the planner and runtime -------------------------------------------------------------

TOOLS = [
    ToolDefinition(name, name, {"type": "object"})
    for name in ("filesystem__write_file", "filesystem__list_directory")
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
    undo = UndoJournal(root / ".free-undo.sqlite3", config.allowed_directories)
    return Planner(config, gateway, model, undo_journal=undo), model


def ask(question="새 파일의 이름을 알려 주세요."):
    request = ClarificationRequest(question, (), "free_text", "q")
    return ModelTurn(None, [], request)


def write(path):
    arguments = json.dumps({"path": str(path), "content": ""})
    return ModelTurn(None, [ToolCall("w", "filesystem__write_file", arguments)])


async def test_a_typed_question_has_no_buttons_and_keeps_the_original_request(config):
    planner, _ = make(config, [ask()])
    outcome = await planner.run("빈 md 파일 하나 만들어줘")
    assert isinstance(outcome, Clarification) and outcome.options == ()
    assert outcome.context.kind == "free_text" and outcome.context.clarification_count == 1
    assert outcome.context.original_request == "빈 md 파일 하나 만들어줘"
    assert outcome.shown_options == ()


async def test_the_typed_answer_continues_the_same_task_to_an_approval(config):
    root = config.allowed_directories[0]
    planner, model = make(config, [ask(), write(root / "메모.md")])
    question = await planner.run("빈 md 파일 하나 만들어줘")

    pending = await planner.resume(question.context, "메모.md")

    assert pending.operation == "create" and pending.target_path == str(root / "메모.md")
    system = model.generate.await_args_list[1].args[0][0]["content"]
    assert "사용자가 질문에 직접 답한 내용(누적): 메모.md" in system
    assert "승인 규칙은 바뀌지 않습니다" in system
    user = model.generate.await_args_list[1].args[0][1]["content"]
    assert user == "빈 md 파일 하나 만들어줘\n\n(질문에 대한 사용자의 답: 메모.md)"
    assert not (root / "메모.md").exists()  # still nothing written before approval


async def test_typed_answers_accumulate_and_the_budget_is_two_questions(config):
    planner, model = make(config, [ask("이름?"), ask("내용?"), ask("또 뭐?")])
    first = await planner.run("파일 하나 만들어줘")
    second = await planner.resume(first.context, "메모.md")
    assert isinstance(second, Clarification) and second.context.conditions == ("메모.md",)
    third = await planner.resume(second.context, "비워 둬")
    assert isinstance(third, Error) and "clarification 최대" in third.message
    system = model.generate.await_args_list[2].args[0][0]["content"]
    assert "메모.md; 비워 둬" in system


async def test_a_blank_typed_answer_is_refused_and_options_are_not_required(config):
    planner, _ = make(config, [])
    context = ClarificationContext("파일 만들어줘", "free_text", (), 1)
    assert isinstance(await planner.resume(context, "   "), Error)


async def test_runtime_sends_any_typed_answer_to_the_task_and_cancel_still_cancels(config):
    root = config.allowed_directories[0]
    planner, _ = make(config, [ask(), write(root / "2.md"), ask()])
    controller = RuntimeController(planner)

    assert isinstance(await controller.submit("파일 만들어줘"), Clarification)
    # "2" is just text here: a typed question has no numbered options to pick from.
    pending = await controller.submit("2.md")
    assert pending.operation == "create" and pending.target_path == str(root / "2.md")
    await controller.reject_pending()

    assert isinstance(await controller.submit("또 만들어줘"), Clarification)
    cancelled = await controller.submit("취소")
    assert cancelled == Completed("요청을 취소했습니다. 파일은 변경하지 않았습니다.")


def test_more_options_than_fit_are_cut_not_rejected_by_the_provider_schema():
    schema = CLARIFICATION_TOOL["function"]["parameters"]["properties"]["options"]
    assert "maxItems" not in schema
    many = json.dumps(
        {"question": "?", "options": [str(i) for i in range(10)], "kind": "file_candidate"}
    )
    assert len(OpenAICompatibleModel._parse_clarification("c", many).options) == 6


def test_jpg_is_an_accepted_target_format_for_conversion():
    from voice_action.conversion import CONVERT_DEFINITION, _validate

    enum = CONVERT_DEFINITION.schema["properties"]["target_format"]["enum"]
    assert {"jpg", "jpeg", "tif", "htm", "markdown"} <= set(enum)
    _validate(CONVERT_DEFINITION, {"source": "/r/사진.heic", "target_format": "jpg"})


# ---- when a typed question is not passed on -------------------------------------------------


async def test_a_yes_no_question_is_turned_back_so_the_approval_screen_does_the_asking(config):
    root = config.allowed_directories[0]
    (root / "old.log").write_text("x")
    planner, model = make(
        config,
        [ask("old.log 파일을 삭제하시겠습니까?"),
         ModelTurn(None, [ToolCall("d", "local__delete_files",
                                   json.dumps({"paths": [str(root / "old.log")]}))])],
    )
    outcome = await planner.run("old.log 삭제해줘")
    assert getattr(outcome, "operation", None) == "delete"  # went to the approval, not a question
    refusal = model.generate.await_args_list[1].args[0][-1]["content"]
    assert "확인 질문은 필요 없습니다" in refusal


async def test_asking_where_a_file_is_comes_after_searching_for_it(config):
    root = config.allowed_directories[0]
    (root / "예산안.md").write_text("x")
    arguments = json.dumps({"keywords": ["예산안"]})
    find = ModelTurn(None, [ToolCall("f", "local__find_files", arguments)])
    planner, model = make(
        config, [ask("읽을 파일의 이름을 알려 주세요."), find, ModelTurn("찾았습니다", [])]
    )
    outcome = await planner.run("예산안 내용 읽어줘")
    assert "먼저 찾으세요" in model.generate.await_args_list[1].args[0][-1]["content"]
    # Once it did search, a question is allowed if the search settled nothing.
    assert isinstance(outcome, Error | Completed)  # the single match was never read: no claim


@pytest.mark.parametrize("text", ["빈 md 파일 하나 만들어줘", "내말은 파일로 저장해 달라는거였어"])
async def test_creating_or_saving_may_ask_for_a_name_straight_away(config, text):
    planner, _ = make(config, [ask("새 파일의 이름을 알려 주세요.")])
    outcome = await planner.run(text)
    assert isinstance(outcome, Clarification) and outcome.context.kind == "free_text"


async def test_a_known_target_from_the_last_command_lets_a_follow_up_ask_freely(config):
    from voice_action.planner import Exchange

    planner, _ = make(config, [ask("어떤 이름으로 저장할까요?")])  # open: asks for a name
    previous = Exchange("a.md 읽어줘", "내용", ("/r/a.md",))
    outcome = await planner.run("요약해줘", previous=previous)
    assert isinstance(outcome, Clarification)


def test_the_delete_tool_given_to_the_provider_is_plain_but_the_code_still_checks_strictly():
    from jsonschema import Draft202012Validator, ValidationError

    from voice_action.deletion import DELETE_DEFINITION, DELETE_SCHEMA

    schema = DELETE_DEFINITION.schema
    # What the provider checks has no oneOf, required or size limits to trip a generated call.
    assert set(schema) == {"type", "properties"}
    assert all(set(value) <= {"type", "items"} for value in schema["properties"].values())
    for arguments in ({"directory": "/r", "pattern": "*.txt"}, {"paths": ["/r/a"]}):
        Draft202012Validator(DELETE_SCHEMA).validate(arguments)
    for bad in ({}, {"paths": []}, {"paths": ["/r/a"], "pattern": "*"}, {"directory": "/r"}):
        with pytest.raises(ValidationError):
            Draft202012Validator(DELETE_SCHEMA).validate(bad)


@pytest.mark.parametrize(
    "question, refused",
    [
        ("old.log 파일을 삭제하시겠습니까?", True),
        ("저장할까요?", True),
        ("변환해도 될까요?", True),
        ("어떤 이름으로 저장할까요?", False),
        ("저장할 내용을 알려 주세요.", False),
    ],
)
def test_which_typed_questions_are_really_confirmations(question, refused):
    from voice_action.planner import _free_text_refusal

    class State:
        find_tried, known_target, user_text = True, True, "파일 만들어줘"

    assert (_free_text_refusal(State(), question) is not None) is refused
