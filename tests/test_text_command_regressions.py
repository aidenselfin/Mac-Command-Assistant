"""Known text-command regressions, isolated from external services."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from voice_action.mcp_client import ToolDefinition, ToolResult
from voice_action.planner import (
    ApprovalRequired,
    ClarificationRequest,
    Completed,
    Error,
    ModelTurn,
    Planner,
    PlannerEventType,
    PlannerTiming,
    ToolCall,
)
from voice_action.undo import UndoJournal


def fake_gateway():
    gateway = AsyncMock(tools=[])
    gateway.list_tools.return_value = []
    gateway.validate_result_path.side_effect = lambda value: Path(value)

    async def normalize(_name, arguments):
        return dict(arguments)

    gateway.normalize_tool_arguments.side_effect = normalize
    return gateway


def planner(config, gateway, model, **kwargs):
    return Planner(
        config,
        gateway,
        model,
        undo_journal=UndoJournal(
            config.allowed_directories[0] / ".regression-undo.sqlite3",
            config.allowed_directories,
        ),
        **kwargs,
    )


def search_call(root, pattern):
    return ModelTurn(
        None,
        [
            ToolCall(
                "search",
                "filesystem__search_files",
                json.dumps({"path": str(root), "pattern": pattern}, ensure_ascii=False),
            )
        ],
    )


async def test_regression_meeting_note_summary_rejects_unrelated_safe_txt_search_result(
    config,
):
    root = config.allowed_directories[0]
    for filename in ("회의록-1.md", "회의록-2.md", "result.txt"):
        (root / filename).write_text("fixture", encoding="utf-8")
    candidates = (root / "회의록-1.md", root / "회의록-2.md")
    gateway = fake_gateway()
    gateway.call_tool.return_value = ToolResult(True, "\n".join(map(str, candidates)))
    model = AsyncMock()
    model.generate.side_effect = [
        search_call(root, "*.txt"),
        search_call(root, "**/*회의록*"),
    ]

    outcome = await planner(config, gateway, model).run("회의록 요약해줘")

    assert outcome.options == tuple(map(str, candidates))
    gateway.call_tool.assert_awaited_once_with(
        "filesystem__search_files",
        {"path": str(root), "pattern": "**/*회의록*"},
    )
    assert all(
        call.args[0] not in {"filesystem__read_text_file", "filesystem__write_file"}
        for call in gateway.call_tool.await_args_list
    )


async def test_regression_web_save_accepts_url_before_korean_object_particle(config):
    gateway = fake_gateway()
    target = config.allowed_directories[0] / "summary.md"
    gateway.call_tool.side_effect = [
        ToolResult(
            True, "### Page state\n- Page URL: https://example.com\n- Page Title: Fixture"
        ),
        ToolResult(
            True, "### Page state\n- Page URL: https://example.com\n- Page Snapshot:\nfixture"
        ),
    ]
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(
            None,
            [
                ToolCall(
                    "navigate",
                    "web__browser_navigate",
                    json.dumps({"url": "https://example.com"}),
                )
            ],
        ),
        ModelTurn(None, [ToolCall("snapshot", "web__browser_snapshot", "{}")]),
        ModelTurn(
            None,
            [
                ToolCall(
                    "write",
                    "filesystem__write_file",
                    json.dumps({"path": str(target), "content": "summary"}),
                )
            ],
        ),
    ]

    pending = await planner(config, gateway, model).run(
        "https://example.com을 읽고 summary.md에 저장해줘"
    )

    assert gateway.call_tool.await_args_list[0].args == (
        "web__browser_navigate",
        {"url": "https://example.com"},
    )
    assert pending.tool_name == "filesystem__write_file"


async def test_regression_web_file_comparison_does_not_enter_web_save_flow(config):
    config = replace(config, max_tool_rounds=1)
    gateway = fake_gateway()
    gateway.list_tools.return_value = [
        ToolDefinition("web__browser_navigate", "Navigate", {"type": "object"}),
        ToolDefinition("web__browser_snapshot", "Snapshot", {"type": "object"}),
        ToolDefinition("filesystem__read_text_file", "Read", {"type": "object"}),
        ToolDefinition("filesystem__write_file", "Write", {"type": "object"}),
    ]
    model = AsyncMock()
    model.generate.return_value = ModelTurn("비교 결과", [])

    outcome = await planner(config, gateway, model).run(
        "https://example.com 페이지와 test1.md를 비교해줘"
    )

    assert outcome == Completed("비교 결과")
    first_tools = model.generate.await_args_list[0].args[1]
    assert "filesystem__write_file" not in {tool.name for tool in first_tools}
    gateway.call_tool.assert_not_awaited()


async def test_web_file_comparison_rejects_hidden_write_after_reads(config):
    root = config.allowed_directories[0]
    local_file = root / "test1.md"
    local_file.write_text("LOCAL", encoding="utf-8")
    target = root / "summary.md"
    gateway = fake_gateway()
    gateway.list_tools.return_value = [
        ToolDefinition("web__browser_navigate", "Navigate", {"type": "object"}),
        ToolDefinition("web__browser_snapshot", "Snapshot", {"type": "object"}),
        ToolDefinition("filesystem__read_text_file", "Read", {"type": "object"}),
        ToolDefinition("filesystem__write_file", "Write", {"type": "object"}),
    ]
    url = "https://example.com"
    gateway.call_tool.side_effect = [
        ToolResult(True, f"### Page state\n- Page URL: {url}\n- Page Title: Fixture"),
        ToolResult(True, f"### Page state\n- Page URL: {url}\n- Page Snapshot:\nWEB"),
        ToolResult(True, "LOCAL"),
    ]
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(None, [ToolCall("nav", "web__browser_navigate", json.dumps({"url": url}))]),
        ModelTurn(None, [ToolCall("snap", "web__browser_snapshot", "{}")]),
        ModelTurn(
            None,
            [ToolCall("read", "filesystem__read_text_file", json.dumps({"path": str(local_file)}))],
        ),
        ModelTurn(
            None,
            [
                ToolCall(
                    "hidden-write",
                    "filesystem__write_file",
                    json.dumps({"path": str(target), "content": "SHOULD NOT WRITE"}),
                )
            ],
        ),
        ModelTurn("비교 완료", []),
    ]

    outcome = await planner(config, gateway, model).run(
        "https://example.com 페이지와 test1.md를 비교해줘"
    )

    assert outcome == Completed("비교 완료")
    assert [call.args for call in gateway.call_tool.await_args_list] == [
        ("web__browser_navigate", {"url": url}),
        ("web__browser_snapshot", {}),
        ("filesystem__read_text_file", {"path": str(local_file)}),
    ]
    assert not target.exists()
    assert "저장 요청" in model.generate.await_args_list[-1].args[0][-1]["content"]


@pytest.mark.parametrize(
    "user_text",
    [
        "https://example.com 페이지와 test1.md를 비교해줘",
        "https://example.com 페이지에서 summary.md 내용을 읽어줘",
        "https://example.com 페이지를 summary.md에 저장하지 말고 읽어줘",
    ],
)
async def test_non_save_web_request_rejects_hidden_write_before_navigation(config, user_text):
    target = config.allowed_directories[0] / "summary.md"
    gateway = fake_gateway()
    gateway.list_tools.return_value = [
        ToolDefinition("filesystem__write_file", "Write", {"type": "object"}),
    ]
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(
            None,
            [
                ToolCall(
                    "hidden-write",
                    "filesystem__write_file",
                    json.dumps({"path": str(target), "content": "SHOULD NOT WRITE"}),
                )
            ],
        ),
        ModelTurn("거부했습니다.", []),
    ]

    outcome = await planner(config, gateway, model).run(user_text)

    assert outcome == Completed("거부했습니다.")
    assert "filesystem__write_file" not in {
        tool.name for tool in model.generate.await_args_list[0].args[1]
    }
    gateway.call_tool.assert_not_awaited()
    assert not target.exists()


async def test_web_save_negation_does_not_expose_write_or_request_approval(config):
    gateway = fake_gateway()
    gateway.list_tools.return_value = [
        ToolDefinition("web__browser_navigate", "Navigate", {"type": "object"}),
        ToolDefinition("filesystem__write_file", "Write", {"type": "object"}),
    ]
    model = AsyncMock()
    model.generate.return_value = ModelTurn("요약만 했습니다.", [])

    outcome = await planner(config, gateway, model).run(
        "https://example.com 페이지를 저장하지 말고 요약해줘"
    )

    assert outcome == Completed("요약만 했습니다.")
    assert "filesystem__write_file" not in {
        tool.name for tool in model.generate.await_args.args[1]
    }
    gateway.call_tool.assert_not_awaited()


async def test_regression_composite_active_app_request_runs_following_file_listing(config):
    gateway = fake_gateway()
    gateway.call_tool.side_effect = [ToolResult(True, "Finder"), ToolResult(True, "[FILE]")]
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(
            None,
            [
                ToolCall("active", "macos__get_active_app", "{}"),
                ToolCall(
                    "list",
                    "filesystem__list_directory",
                    json.dumps({"path": str(config.allowed_directories[0])}),
                ),
            ],
        ),
        ModelTurn("Finder에서 파일 목록을 확인했습니다.", []),
    ]

    outcome = await planner(config, gateway, model).run("현재 활성 앱과 파일 목록을 알려줘")

    assert outcome == Completed("Finder에서 파일 목록을 확인했습니다.")
    assert [call.args[0] for call in gateway.call_tool.await_args_list] == [
        "macos__get_active_app",
        "filesystem__list_directory",
    ]
    assert any(
        message.get("tool_call_id") == "active" and message["content"] == "Finder"
        for message in model.generate.await_args_list[1].args[0]
    )


async def test_regression_standalone_active_app_still_completes_after_one_call(config):
    gateway = fake_gateway()
    gateway.call_tool.return_value = ToolResult(True, "Finder")
    model = AsyncMock()
    model.generate.return_value = ModelTurn(
        None, [ToolCall("active", "macos__get_active_app", "{}")]
    )

    outcome = await planner(config, gateway, model).run("현재 활성 앱을 알려줘")

    assert outcome == Completed("현재 활성 앱은 Finder입니다.")
    gateway.call_tool.assert_awaited_once_with("macos__get_active_app", {})


async def test_regression_missing_source_rejection_lets_model_ask_instead_of_guessing(config):
    gateway = fake_gateway()
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(None, [], ClarificationRequest("무엇을 요약할까요?", ("원본",), "edit_criteria")),
        ModelTurn("요약할 원본 내용이나 파일을 알려주세요.", []),
    ]

    outcome = await planner(config, gateway, model).run("result.txt에 요약을 저장해줘")

    assert outcome == Completed("요약할 원본 내용이나 파일을 알려주세요.")
    rejection = model.generate.await_args_list[1].args[0][-1]["content"]
    assert "지어내지 말고" in rejection and "최종 응답" in rejection
    gateway.call_tool.assert_not_awaited()


@pytest.mark.parametrize(
    "command", ["현재 활성 앱 이름을 memo.txt에 기록해줘", "활성 앱과 test1.md 내용 보여줘"]
)
async def test_regression_active_app_with_named_file_is_not_a_standalone_lookup(
    config, command
):
    gateway = fake_gateway()
    gateway.call_tool.return_value = ToolResult(True, "Finder")
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(None, [ToolCall("active", "macos__get_active_app", "{}")]),
        ModelTurn("Finder", []),
    ]

    outcome = await planner(config, gateway, model).run(command)

    assert model.generate.await_count == 2
    assert outcome == Completed("Finder")
    gateway.call_tool.assert_awaited_once_with("macos__get_active_app", {})


@pytest.mark.parametrize("same_turn", [True, False])
async def test_regression_active_app_cache_skips_same_and_later_repeated_calls(
    config, same_turn
):
    gateway = fake_gateway()
    gateway.call_tool.side_effect = [ToolResult(True, "Finder"), ToolResult(True, "[FILE]")]
    list_call = ToolCall(
        "list",
        "filesystem__list_directory",
        json.dumps({"path": str(config.allowed_directories[0])}),
    )
    model = AsyncMock()
    model.generate.side_effect = (
        [
            ModelTurn(
                None,
                [
                    ToolCall("active", "macos__get_active_app", "{}"),
                    ToolCall("again", "macos__get_active_app", "{}"),
                    list_call,
                ],
            ),
            ModelTurn("Finder에서 확인했습니다.", []),
        ]
        if same_turn
        else [
            ModelTurn(None, [ToolCall("active", "macos__get_active_app", "{}")]),
            ModelTurn(
                None,
                [ToolCall("again", "macos__get_active_app", "{}"), list_call],
            ),
            ModelTurn("Finder에서 확인했습니다.", []),
        ]
    )

    outcome = await planner(config, gateway, model).run("활성 앱과 파일 목록을 알려줘")

    assert outcome == Completed("Finder에서 확인했습니다.")
    assert [call.args[0] for call in gateway.call_tool.await_args_list] == [
        "macos__get_active_app",
        "filesystem__list_directory",
    ]
    assert model.generate.await_count <= config.max_tool_rounds


async def test_regression_failed_active_app_is_not_cached_before_other_work(config):
    gateway = fake_gateway()
    gateway.call_tool.side_effect = [
        ToolResult(False, "denied", error="denied"),
        ToolResult(True, "[FILE]"),
        ToolResult(True, "Finder"),
    ]
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(
            None,
            [
                ToolCall("active", "macos__get_active_app", "{}"),
                ToolCall(
                    "list",
                    "filesystem__list_directory",
                    json.dumps({"path": str(config.allowed_directories[0])}),
                ),
            ],
        ),
        ModelTurn(None, [ToolCall("retry", "macos__get_active_app", "{}")]),
        ModelTurn("Finder에서 목록을 확인했습니다.", []),
    ]

    assert await planner(config, gateway, model).run("활성 앱과 파일 목록을 알려줘") == Completed(
        "Finder에서 목록을 확인했습니다."
    )
    assert gateway.call_tool.await_count == 3


async def test_regression_approval_keeps_active_app_cache_and_new_command_refreshes(config):
    target = config.allowed_directories[0] / "result.txt"
    gateway = fake_gateway()
    gateway.call_tool.side_effect = [
        ToolResult(True, "Finder"),
        ToolResult(True, "saved"),
        ToolResult(True, "Safari"),
    ]
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(
            None,
            [
                ToolCall("active", "macos__get_active_app", "{}"),
                ToolCall(
                    "write",
                    "filesystem__write_file",
                    json.dumps({"path": str(target), "content": "ok"}),
                ),
            ],
        ),
        ModelTurn(None, [ToolCall("again", "macos__get_active_app", "{}")]),
        ModelTurn("Finder에서 저장했습니다.", []),
        ModelTurn(None, [ToolCall("new", "macos__get_active_app", "{}")]),
    ]
    active_planner = planner(config, gateway, model)

    pending = await active_planner.run("활성 앱을 알려주고 result.txt에 저장해줘")
    assert isinstance(pending, ApprovalRequired)
    assert await active_planner.approve(pending.context) == Completed("Finder에서 저장했습니다.")
    assert any(
        message.get("tool_call_id") == "active" and message["content"] == "Finder"
        for message in model.generate.await_args_list[1].args[0]
    )
    assert await active_planner.run("현재 활성 앱을 알려줘") == Completed(
        "현재 활성 앱은 Safari입니다."
    )
    assert [call.args[0] for call in gateway.call_tool.await_args_list] == [
        "macos__get_active_app",
        "filesystem__write_file",
        "macos__get_active_app",
    ]


@pytest.mark.parametrize("branch", ["empty_turn", "rejected_clarification"])
async def test_regression_web_save_corrections_count_model_calls_separately_from_iterations(
    config, branch
):
    config = replace(config, max_tool_rounds=1)
    gateway = fake_gateway()
    timing = PlannerTiming.empty()
    events = []
    if branch == "empty_turn":
        turn = ModelTurn("아직 완료", [])
    else:
        turn = ModelTurn(
            None,
            [],
            ClarificationRequest("형식?", ("요약",), "edit_criteria"),
        )
    model = AsyncMock()
    model.generate.return_value = turn

    outcome = await planner(
        config, gateway, model, timing=timing, event_observer=events.append
    ).run("https://example.com 내용을 summary.md에 저장해줘")

    assert isinstance(outcome, Error)
    assert "모델 호출 한도" in outcome.message
    assert model.generate.await_count == 1
    assert len(timing.llm_round_ms) == model.generate.await_count
    assert [event.iteration for event in events] == [1]
    assert all(event.type is PlannerEventType.PLANNING_STARTED for event in events)


async def test_regression_approval_continuation_exposes_model_calls_and_iterations(config):
    config = replace(config, max_tool_rounds=3)
    root = config.allowed_directories[0]
    target = root / "summary.txt"
    gateway = fake_gateway()
    gateway.call_tool.side_effect = [ToolResult(True, "fixture"), ToolResult(True, "created")]
    timing = PlannerTiming.empty()
    events = []
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(
            None,
            [
                ToolCall(
                    "read", "filesystem__read_text_file", json.dumps({"path": str(root / "a.txt")})
                )
            ],
        ),
        ModelTurn(
            None,
            [
                ToolCall(
                    "write",
                    "filesystem__write_file",
                    json.dumps({"path": str(target), "content": "summary"}),
                )
            ],
        ),
        ModelTurn("저장했습니다.", []),
    ]
    active_planner = planner(
        config, gateway, model, timing=timing, event_observer=events.append
    )

    pending = await active_planner.run("a.txt를 읽고 summary.txt에 저장해줘")
    outcome = await active_planner.approve(pending.context)

    assert outcome == Completed("저장했습니다.")
    assert model.generate.await_count == 3
    assert len(timing.llm_round_ms) == model.generate.await_count
    assert [
        event.iteration for event in events if event.type is PlannerEventType.PLANNING_STARTED
    ] == [1, 2, 3]


async def test_regression_last_allowed_call_can_write_without_another_model_call(config):
    config = replace(config, max_tool_rounds=1)
    target = config.allowed_directories[0] / "last-call.txt"
    gateway = fake_gateway()
    gateway.call_tool.return_value = ToolResult(True, "saved")
    model = AsyncMock()
    model.generate.return_value = ModelTurn(
        None,
        [
            ToolCall(
                "write",
                "filesystem__write_file",
                json.dumps({"path": str(target), "content": "ok"}),
            )
        ],
    )
    active_planner = planner(config, gateway, model)

    pending = await active_planner.run("last-call.txt에 저장해줘")
    assert isinstance(pending, ApprovalRequired)
    outcome = await active_planner.approve(pending.context)

    assert isinstance(outcome, Error)
    assert "승인된 파일 변경은 완료" in outcome.message
    assert "파일을 변경하지 않았" not in outcome.message
    assert model.generate.await_count == 1


async def test_regression_reapproval_keeps_the_original_model_call_budget(config):
    config = replace(config, max_tool_rounds=1)
    target = config.allowed_directories[0] / "renewed.txt"
    gateway = fake_gateway()
    gateway.call_tool.return_value = ToolResult(True, "saved")
    model = AsyncMock()
    model.generate.return_value = ModelTurn(
        None,
        [
            ToolCall(
                "write",
                "filesystem__write_file",
                json.dumps({"path": str(target), "content": "ok"}),
            )
        ],
    )
    active_planner = planner(config, gateway, model)

    pending = await active_planner.run("renewed.txt에 저장해줘")
    assert isinstance(pending, ApprovalRequired)
    target.write_text("changed", encoding="utf-8")
    renewed = await active_planner.approve(pending.context)
    assert isinstance(renewed, ApprovalRequired)
    outcome = await active_planner.approve(renewed.context)

    assert isinstance(outcome, Error)
    assert "승인된 파일 변경은 완료" in outcome.message
    assert model.generate.await_count == 1
