import asyncio
from collections.abc import Awaitable, Callable

import pytest

from voice_action.planner import (
    Clarification,
    ClarificationContext,
    Completed,
    Error,
    PlannerEvent,
    PlannerEventObserver,
    PlannerEventType,
    PlannerOutcome,
)
from voice_action.runtime import Busy, RuntimeController, RuntimeState

RunAction = PlannerOutcome | BaseException | Callable[["ScriptedPlanner", str], Awaitable]
ResumeAction = (
    PlannerOutcome
    | BaseException
    | Callable[["ScriptedPlanner", ClarificationContext, str], Awaitable]
)


class ScriptedPlanner:
    def __init__(
        self,
        run_actions: list[RunAction],
        resume_actions: list[ResumeAction] | None = None,
    ):
        self.run_actions = run_actions
        self.resume_actions = resume_actions or []
        self.observer: PlannerEventObserver | None = None
        self.run_inputs: list[str] = []
        self.resume_inputs: list[tuple[ClarificationContext, str]] = []

    def set_event_observer(self, observer: PlannerEventObserver | None) -> None:
        self.observer = observer

    async def run(self, text: str) -> PlannerOutcome:
        self.run_inputs.append(text)
        action = self.run_actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return await action(self, text)
        return action

    async def resume(
        self, context: ClarificationContext, selection: str
    ) -> PlannerOutcome:
        self.resume_inputs.append((context, selection))
        action = self.resume_actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return await action(self, context, selection)
        return action

    def event(
        self,
        event_type: PlannerEventType,
        iteration: int = 1,
        tool_name: str | None = None,
    ) -> None:
        assert self.observer is not None
        self.observer(PlannerEvent(event_type, iteration, tool_name))


def state_path(controller: RuntimeController) -> list[RuntimeState]:
    return [RuntimeState.IDLE, *(item.current for item in controller.transition_history)]


def clarification() -> Clarification:
    options = ("/allowed/회의록-1.md", "/allowed/회의록-2.md")
    return Clarification(
        "요약할 회의록을 선택해 주세요.",
        options,
        ClarificationContext("회의록 요약해줘", "file_candidate", options, 1),
    )


async def test_normal_command_transitions_to_done_then_idle():
    planner = ScriptedPlanner([Completed("완료")])
    logs = []
    controller = RuntimeController(planner, emit=logs.append)

    assert await controller.submit("파일 목록") == Completed("완료")

    assert state_path(controller) == [
        RuntimeState.IDLE,
        RuntimeState.PLANNING,
        RuntimeState.DONE,
        RuntimeState.IDLE,
    ]
    assert controller.state is RuntimeState.IDLE
    assert logs == [
        "[STATE] IDLE -> PLANNING reason=command_started",
        "[STATE] PLANNING -> DONE reason=completed",
        "[STATE] DONE -> IDLE reason=result_observed",
    ]


async def test_multi_tool_command_transitions_from_structured_events():
    async def run(planner, _text):
        planner.event(PlannerEventType.PLANNING_STARTED, 1)
        planner.event(PlannerEventType.TOOL_STARTED, 1, "read_text_file")
        planner.event(PlannerEventType.TOOL_FINISHED, 1, "read_text_file")
        planner.event(PlannerEventType.PLANNING_STARTED, 2)
        planner.event(PlannerEventType.TOOL_STARTED, 2, "write_file")
        planner.event(PlannerEventType.TOOL_FINISHED, 2, "write_file")
        planner.event(PlannerEventType.PLANNING_STARTED, 3)
        return Completed("저장 완료")

    planner = ScriptedPlanner([run])
    controller = RuntimeController(planner)

    assert await controller.submit("읽고 저장") == Completed("저장 완료")

    assert state_path(controller) == [
        RuntimeState.IDLE,
        RuntimeState.PLANNING,
        RuntimeState.EXECUTING_TOOL,
        RuntimeState.PLANNING,
        RuntimeState.EXECUTING_TOOL,
        RuntimeState.PLANNING,
        RuntimeState.DONE,
        RuntimeState.IDLE,
    ]


async def test_clarification_number_suffix_resumes_pending_plan():
    pending = clarification()

    async def resume(planner, context, selection):
        assert context is pending.context
        assert selection == pending.options[1]
        planner.event(PlannerEventType.PLANNING_STARTED, 1)
        return Completed("두 번째 회의록 요약 완료")

    planner = ScriptedPlanner([pending], [resume])
    controller = RuntimeController(planner)

    assert await controller.submit("회의록 요약해줘") is pending
    assert controller.state is RuntimeState.WAITING_USER_INPUT
    assert await controller.submit("2번") == Completed("두 번째 회의록 요약 완료")

    assert state_path(controller) == [
        RuntimeState.IDLE,
        RuntimeState.PLANNING,
        RuntimeState.WAITING_USER_INPUT,
        RuntimeState.RESUMING_PLAN,
        RuntimeState.PLANNING,
        RuntimeState.DONE,
        RuntimeState.IDLE,
    ]
    assert planner.run_inputs == ["회의록 요약해줘"]
    assert len(planner.resume_inputs) == 1


async def test_error_outcome_and_unexpected_exception_recover_to_idle():
    planner = ScriptedPlanner(
        [Error("실패"), RuntimeError("unexpected"), Completed("복구 완료")]
    )
    controller = RuntimeController(planner)

    assert await controller.submit("첫 명령") == Error("실패")
    with pytest.raises(RuntimeError, match="unexpected"):
        await controller.submit("둘째 명령")
    assert await controller.submit("셋째 명령") == Completed("복구 완료")

    assert state_path(controller) == [
        RuntimeState.IDLE,
        RuntimeState.PLANNING,
        RuntimeState.ERROR,
        RuntimeState.IDLE,
        RuntimeState.PLANNING,
        RuntimeState.ERROR,
        RuntimeState.IDLE,
        RuntimeState.PLANNING,
        RuntimeState.DONE,
        RuntimeState.IDLE,
    ]


@pytest.mark.parametrize(
    ("emit_tool", "busy_state"),
    [
        (False, RuntimeState.PLANNING),
        (True, RuntimeState.EXECUTING_TOOL),
    ],
)
async def test_duplicate_input_is_busy_and_never_reaches_planner(emit_tool, busy_state):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(planner, _text):
        planner.event(PlannerEventType.PLANNING_STARTED, 1)
        if emit_tool:
            planner.event(PlannerEventType.TOOL_STARTED, 1, "read_text_file")
        entered.set()
        await release.wait()
        if emit_tool:
            planner.event(PlannerEventType.TOOL_FINISHED, 1, "read_text_file")
        return Completed("첫 명령 완료")

    planner = ScriptedPlanner([blocked])
    controller = RuntimeController(planner)
    first = asyncio.create_task(controller.submit("첫 명령"))
    await entered.wait()

    busy = await controller.submit("두 번째 명령")

    assert busy == Busy("다른 명령을 처리 중입니다.", busy_state)
    assert planner.run_inputs == ["첫 명령"]
    release.set()
    assert await first == Completed("첫 명령 완료")
    assert controller.state is RuntimeState.IDLE


async def test_cancel_while_waiting_never_calls_resume():
    planner = ScriptedPlanner([clarification()])
    controller = RuntimeController(planner)

    assert isinstance(await controller.submit("회의록 요약해줘"), Clarification)
    outcome = await controller.submit("취소")

    assert isinstance(outcome, Completed)
    assert "변경하지 않았습니다" in outcome.text
    assert planner.resume_inputs == []
    assert state_path(controller)[-3:] == [
        RuntimeState.WAITING_USER_INPUT,
        RuntimeState.DONE,
        RuntimeState.IDLE,
    ]


def test_voice_compatibility_states_are_declared():
    assert {state.name for state in RuntimeState} == {
        "IDLE",
        "RECORDING",
        "TRANSCRIBING",
        "PLANNING",
        "EXECUTING_TOOL",
        "WAITING_USER_INPUT",
        "RESUMING_PLAN",
        "DONE",
        "ERROR",
    }
