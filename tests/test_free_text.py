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
