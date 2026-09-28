"""Known text-command regressions, isolated from external services.

The xfail cases document behavior that is currently unsafe or too broad.  They
should become XPASS only when the corresponding product contract is defined
and implemented deliberately.
"""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from voice_action.mcp_client import ToolDefinition, ToolResult
from voice_action.planner import (
    ClarificationRequest,
    Completed,
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


@pytest.mark.xfail(
    strict=True,
    reason="a successful active-app result currently completes the whole command",
)
async def test_regression_composite_active_app_request_runs_following_file_listing(config):
    gateway = fake_gateway()
    gateway.call_tool.side_effect = [ToolResult(True, "Finder"), ToolResult(True, "[FILE]")]
    model = AsyncMock()
    model.generate.return_value = ModelTurn(
        None,
        [
            ToolCall("active", "macos__get_active_app", "{}"),
            ToolCall(
                "list",
                "filesystem__list_directory",
                json.dumps({"path": str(config.allowed_directories[0])}),
            ),
        ],
    )

    await planner(config, gateway, model).run("현재 활성 앱과 파일 목록을 알려줘")

    assert [call.args[0] for call in gateway.call_tool.await_args_list] == [
        "macos__get_active_app",
        "filesystem__list_directory",
    ]


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

    assert "웹 저장 절차" in outcome.message
    assert model.generate.await_count == 3
    assert len(timing.llm_round_ms) == model.generate.await_count
    assert [event.iteration for event in events] == [1, 1, 1]
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
