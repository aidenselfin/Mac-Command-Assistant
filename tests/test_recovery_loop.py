"""Recovery inside one command: fixable mistakes go back to the model, safety stops stay stops."""

import json
import unicodedata
from dataclasses import replace
from unittest.mock import AsyncMock

from voice_action.mcp_client import ToolDefinition, ToolRejectedError, ToolResult
from voice_action.planner import (
    ApprovalRequired,
    Completed,
    Error,
    ModelFormatError,
    ModelTurn,
    Planner,
    PlannerEventType,
    ToolCall,
)
from voice_action.undo import UndoJournal

READ = "filesystem__read_text_file"
DOC = "local__read_document"


def gateway():
    gw = AsyncMock(tools=[])
    gw.list_tools.return_value = [ToolDefinition(READ, READ, {"type": "object"})]

    async def normalize(_name, arguments):
        return dict(arguments)

    gw.normalize_tool_arguments.side_effect = normalize
    return gw


def make(config, gw, model, **kwargs):
    return Planner(
        config, gw, model,
        undo_journal=UndoJournal(
            config.allowed_directories[0] / ".undo.sqlite3", config.allowed_directories
        ),
        **kwargs,
    )


def read(path, call_id="r", tool=READ):
    return ModelTurn(None, [ToolCall(call_id, tool, json.dumps({"path": str(path)}))])


def all_text(model) -> str:
    return "\n".join(
        str(message.get("content")) for call in model.generate.await_args_list
        for message in call.args[0]
    )


async def test_format_error_goes_back_to_the_model_instead_of_ending(config):
    model = AsyncMock()
    model.generate.side_effect = [
        ModelFormatError("clarification과 MCP 도구를 동시에 요청할 수 없습니다."),
        ModelTurn("안녕하세요", []),
    ]

    outcome = await make(config, gateway(), model).run("인사해줘")

    assert outcome == Completed("안녕하세요")
    retry_messages = model.generate.await_args_list[1].args[0]
    assert retry_messages[-1]["role"] == "system"
    assert "동시에" in retry_messages[-1]["content"]


async def test_adapter_reports_clarification_with_tools_as_a_fixable_format_error():
    from types import SimpleNamespace

    from voice_action.planner import OpenAICompatibleModel

    def call(name, arguments):
        return SimpleNamespace(
            id=name, type="function", function=SimpleNamespace(name=name, arguments=arguments)
        )

    message = SimpleNamespace(content=None, tool_calls=[
        call("request_clarification", json.dumps({"question": "어느?", "options": ["a", "b"],
                                                  "kind": "file_candidate"})),
        call(READ, "{}"),
    ])
    client = AsyncMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=message)]
    )
    try:
        await OpenAICompatibleModel(client, "m").generate([], [], allow_tools=True)
    except ModelFormatError:
        return
    raise AssertionError("expected ModelFormatError")


async def test_missing_nfd_korean_file_lists_real_names_and_stops_a_doom_loop(config):
    """Ops record 69/70: a mangled Korean NFD name read again and again until the round limit."""
    root = config.allowed_directories[0]
    real = unicodedata.normalize("NFD", "회의록 정리.docx")
    (root / real).write_bytes(b"x")
    broken = root / "회의록 졍리.docx"  # one syllable mangled while copying the NFD name
    model = AsyncMock()
    model.generate.side_effect = [
        read(broken, "a", DOC), read(broken, "b", DOC), read(broken, "c", DOC),
        ModelTurn("회의록 정리.docx를 찾았지만 경로를 잘못 적어 읽지 못했습니다.", []),
    ]

    outcome = await make(config, gateway(), model).run("정리 문서 내용 알려줘")

    assert isinstance(outcome, Error)
    assert "반복" in outcome.message
    assert "경로를 잘못 적어" in outcome.message  # the model's own honest report
    seen = all_text(model)
    assert "회의록 정리.docx" in unicodedata.normalize("NFC", seen)  # a real name to copy
    assert "그대로 복사" in seen
    assert "같은 시도가 같은 결과" in seen
    assert model.generate.await_count == 4  # stopped at the third repeat, not at 20 rounds
    assert model.generate.await_args_list[-1].kwargs["allow_tools"] is False


async def test_nfc_request_reads_the_file_under_its_on_disk_nfd_name(config):
    root = config.allowed_directories[0]
    real = unicodedata.normalize("NFD", "보고서.txt")
    (root / real).write_text("내용")
    gw = gateway()
    gw.call_tool.return_value = ToolResult(True, "내용")
    model = AsyncMock()
    model.generate.side_effect = [
        read(root / unicodedata.normalize("NFC", "보고서.txt")), ModelTurn("내용입니다", []),
    ]

    outcome = await make(config, gw, model).run("보고서.txt 읽어줘")

    assert outcome == Completed("내용입니다")
    assert gw.call_tool.await_args.args[1]["path"] == str(root / real)


async def test_missing_file_hint_never_lists_names_outside_allowed_folders(config, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "salary-2026.txt").write_text("x")
    gw = gateway()
    gw.call_tool.side_effect = ToolRejectedError("허용 경로 밖 접근입니다.")
    model = AsyncMock()
    model.generate.side_effect = [read(outside / "salary.txt"), ModelTurn("못 읽었습니다", [])]

    await make(config, gw, model).run("salary.txt 읽어줘")

    assert "salary-2026" not in all_text(model)


async def test_repeated_reads_with_new_results_are_not_treated_as_a_loop(config):
    root = config.allowed_directories[0]
    (root / "log.txt").write_text("x")
    gw = gateway()
    gw.call_tool.side_effect = [ToolResult(True, f"줄 {n}") for n in range(3)]
    model = AsyncMock()
    model.generate.side_effect = [
        read(root / "log.txt", "a"), read(root / "log.txt", "b"), read(root / "log.txt", "c"),
        ModelTurn("끝", []),
    ]

    outcome = await make(config, gw, model).run("log.txt 읽어줘")

    assert outcome == Completed("끝")
    assert "같은 시도" not in all_text(model)


async def test_two_failed_recoveries_hand_the_conversation_to_the_stronger_model(config):
    main, strong = AsyncMock(), AsyncMock()
    main.generate.side_effect = [ModelFormatError("형식 오류 1"), ModelFormatError("형식 오류 2")]
    strong.generate.side_effect = [ModelTurn("상위 모델 답", [])]
    events = []

    outcome = await make(
        config, gateway(), main, escalation_model=strong, event_observer=events.append
    ).run("인사해줘")

    assert outcome == Completed("상위 모델 답")
    handed = strong.generate.await_args.args[0]
    assert sum("형식 오류" in str(m.get("content")) for m in handed) == 2  # failures kept
    assert any(e.type is PlannerEventType.RECOVERING for e in events)


async def test_without_a_stronger_model_the_main_model_keeps_recovering(config):
    main = AsyncMock()
    main.generate.side_effect = [
        ModelFormatError("오류 1"), ModelFormatError("오류 2"), ModelTurn("결국 답", []),
    ]

    assert await make(config, gateway(), main).run("인사해줘") == Completed("결국 답")


async def test_endless_format_errors_still_end(config):
    main = AsyncMock()
    main.generate.side_effect = ModelFormatError("늘 같은 오류")

    outcome = await make(config, gateway(), main).run("인사해줘")

    assert isinstance(outcome, Error)
    assert main.generate.await_count < config.max_tool_rounds


async def test_second_empty_reply_goes_to_the_stronger_model(config):
    main, strong = AsyncMock(), AsyncMock()
    main.generate.side_effect = [ModelTurn("", []), ModelTurn(None, [])]
    strong.generate.side_effect = [ModelTurn("답", [])]

    outcome = await make(config, gateway(), main, escalation_model=strong).run("인사해줘")

    assert outcome == Completed("답")


async def test_run_time_limit_ends_the_command(config):
    config = replace(config, max_run_seconds=90)
    model = AsyncMock()
    model.generate.side_effect = ModelFormatError("오류")
    planner = make(config, gateway(), model)
    ticks = iter([0, 10, 100, 200, 300])
    planner._clock = lambda: next(ticks)

    outcome = await planner.run("none.txt 읽어줘")

    assert isinstance(outcome, Error)
    assert "시간 한도" in outcome.message


async def test_recovery_state_survives_an_approval(config):
    root = config.allowed_directories[0]
    main, strong = AsyncMock(), AsyncMock()
    main.generate.side_effect = [
        ModelFormatError("오류 1"),
        ModelTurn(None, [ToolCall(
            "w", "filesystem__write_file",
            json.dumps({"path": str(root / "a.txt"), "content": "x"}),
        )]),
        ModelFormatError("오류 2"),
    ]
    strong.generate.side_effect = [ModelTurn("저장했습니다", [])]
    gw = gateway()
    gw.list_tools.return_value.append(
        ToolDefinition("filesystem__write_file", "w", {"type": "object"})
    )
    gw.call_tool.return_value = ToolResult(True, "ok")
    planner = make(config, gw, main, escalation_model=strong)

    pending = await planner.run("a.txt에 x 저장해줘")
    assert isinstance(pending, ApprovalRequired)
    outcome = await planner.approve(pending.context)

    assert isinstance(outcome, Completed)
    assert outcome.text == "저장했습니다"


async def test_slow_model_call_is_cut_at_the_time_limit(config):
    import asyncio

    config = replace(config, max_run_seconds=0.05)
    model = AsyncMock()

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(5)

    model.generate.side_effect = slow

    outcome = await make(config, gateway(), model).run("인사해줘")

    assert isinstance(outcome, Error)
    assert "시간 한도" in outcome.message


async def test_time_before_an_approval_still_counts_after_it(config):
    root = config.allowed_directories[0]
    config = replace(config, max_run_seconds=90)
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(None, [ToolCall(
            "w", "filesystem__write_file",
            json.dumps({"path": str(root / "a.txt"), "content": "x"}),
        )]),
        ModelTurn("저장했습니다", []),
    ]
    gw = gateway()
    gw.list_tools.return_value.append(
        ToolDefinition("filesystem__write_file", "w", {"type": "object"})
    )
    gw.call_tool.return_value = ToolResult(True, "ok")
    planner = make(config, gw, model)
    # 80 s before the approval; the user then waits 1000 s; 20 s more after it is over 90.
    ticks = iter([0, 0, 80, 1080, 1100])
    planner._clock = lambda: next(ticks)

    pending = await planner.run("a.txt에 x 저장해줘")
    outcome = await planner.approve(pending.context)

    assert isinstance(outcome, Error)
    assert "시간 한도" in outcome.message
    assert "승인된 파일 변경은 완료" in outcome.message
