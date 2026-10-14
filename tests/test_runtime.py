import asyncio
from collections.abc import Awaitable, Callable

import pytest

from voice_action.planner import (
    ApprovalRequired,
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
        approve_actions: list[RunAction] | None = None,
        undo_actions: list[RunAction] | None = None,
    ):
        self.run_actions = run_actions
        self.resume_actions = resume_actions or []
        self.approve_actions = approve_actions or []
        self.undo_actions = undo_actions or []
        self.observer: PlannerEventObserver | None = None
        self.run_inputs: list[str] = []
        self.resume_inputs: list[tuple[ClarificationContext, str]] = []
        self.approve_inputs = []
        self.reject_inputs = []
        self.undo_count = 0

    def set_event_observer(self, observer: PlannerEventObserver | None) -> None:
        self.observer = observer

    async def run(self, text: str, **_options) -> PlannerOutcome:
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

    async def approve(self, context):
        self.approve_inputs.append(context)
        action = self.approve_actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return await action(self, context)
        return action

    def reject(self, context):
        self.reject_inputs.append(context)
        return Completed("파일 쓰기를 거절했습니다. 파일은 변경하지 않았습니다.")

    async def undo_last_write(self):
        self.undo_count += 1
        action = self.undo_actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return await action(self, "undo")
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


def approval(path="/allowed/summary.txt") -> ApprovalRequired:
    return ApprovalRequired(
        "filesystem__write_file",
        path,
        "create",
        7,
        "summary",
        False,
        object(),
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


async def test_typed_state_and_planner_event_observers_preserve_emit_contract():
    async def run(planner, _text):
        planner.event(
            PlannerEventType.TOOL_STARTED, 1, "filesystem__read_text_file"
        )
        planner.event(
            PlannerEventType.TOOL_FINISHED, 1, "filesystem__read_text_file"
        )
        return Completed("완료")

    planner = ScriptedPlanner([run])
    transitions = []
    events = []
    logs = []
    controller = RuntimeController(
        planner,
        emit=logs.append,
        state_observer=transitions.append,
        planner_event_observer=events.append,
    )

    assert await controller.submit("읽기") == Completed("완료")

    assert [event.tool_name for event in events] == [
        "filesystem__read_text_file",
        "filesystem__read_text_file",
    ]
    assert [transition.current for transition in transitions] == [
        RuntimeState.PLANNING,
        RuntimeState.EXECUTING_TOOL,
        RuntimeState.PLANNING,
        RuntimeState.DONE,
        RuntimeState.IDLE,
    ]
    assert logs[1].startswith("[STATE] PLANNING -> EXECUTING_TOOL")


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


async def test_approval_state_executes_saved_call_and_submit_stays_busy():
    pending = approval()

    async def approve_action(planner, context):
        assert context is pending.context
        planner.event(PlannerEventType.TOOL_STARTED, 1, "filesystem__write_file")
        planner.event(PlannerEventType.TOOL_FINISHED, 1, "filesystem__write_file")
        planner.event(PlannerEventType.PLANNING_STARTED, 2)
        return Completed("저장 완료")

    planner = ScriptedPlanner([pending], approve_actions=[approve_action])
    controller = RuntimeController(planner)

    assert await controller.submit("저장") is pending
    assert controller.state is RuntimeState.WAITING_APPROVAL
    assert controller.pending is None
    assert controller.pending_approval is pending
    assert await controller.submit("새 명령") == Busy(
        "다른 명령을 처리 중입니다.", RuntimeState.WAITING_APPROVAL
    )
    assert await controller.approve_pending() == Completed("저장 완료")

    assert state_path(controller) == [
        RuntimeState.IDLE,
        RuntimeState.PLANNING,
        RuntimeState.WAITING_APPROVAL,
        RuntimeState.RESUMING_PLAN,
        RuntimeState.EXECUTING_TOOL,
        RuntimeState.PLANNING,
        RuntimeState.DONE,
        RuntimeState.IDLE,
    ]
    assert planner.run_inputs == ["저장"]
    assert len(planner.approve_inputs) == 1


async def test_reject_pending_finishes_without_approval_execution():
    pending = approval()
    planner = ScriptedPlanner([pending])
    controller = RuntimeController(planner)

    assert await controller.submit("저장") is pending
    outcome = await controller.reject_pending()

    assert isinstance(outcome, Completed)
    assert "거절" in outcome.text
    assert planner.approve_inputs == []
    assert planner.reject_inputs == [pending.context]
    assert state_path(controller)[-3:] == [
        RuntimeState.WAITING_APPROVAL,
        RuntimeState.DONE,
        RuntimeState.IDLE,
    ]


async def test_approval_can_pause_again_for_second_write():
    first, second = approval("/allowed/one.txt"), approval("/allowed/two.txt")
    planner = ScriptedPlanner([first], approve_actions=[second, Completed("완료")])
    controller = RuntimeController(planner)

    assert await controller.submit("두 파일") is first
    assert await controller.approve_pending() is second
    assert controller.state is RuntimeState.WAITING_APPROVAL
    assert await controller.approve_pending() == Completed("완료")
    assert len(planner.approve_inputs) == 2


async def test_undo_from_idle_uses_done_transition_without_planning():
    planner = ScriptedPlanner([], undo_actions=[Completed("생성한 파일을 되돌렸습니다.")])
    controller = RuntimeController(planner)

    outcome = await controller.undo_last_write()

    assert outcome == Completed("생성한 파일을 되돌렸습니다.")
    assert planner.undo_count == 1
    assert state_path(controller) == [
        RuntimeState.IDLE,
        RuntimeState.DONE,
        RuntimeState.IDLE,
    ]


async def test_undo_conflict_uses_error_transition_without_planning():
    planner = ScriptedPlanner(
        [], undo_actions=[Error("파일이 write 후 변경되어 되돌리지 않았습니다.")]
    )
    controller = RuntimeController(planner)

    outcome = await controller.undo_last_write()

    assert isinstance(outcome, Error)
    assert state_path(controller) == [
        RuntimeState.IDLE,
        RuntimeState.ERROR,
        RuntimeState.IDLE,
    ]


async def test_undo_is_busy_during_active_command():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(_planner, _text):
        entered.set()
        await release.wait()
        return Completed("완료")

    planner = ScriptedPlanner([blocked])
    controller = RuntimeController(planner)
    command = asyncio.create_task(controller.submit("실행"))
    await entered.wait()

    assert await controller.undo_last_write() == Busy(
        "다른 명령을 처리 중입니다.", RuntimeState.PLANNING
    )
    assert planner.undo_count == 0
    release.set()
    await command


@pytest.mark.parametrize("pending", [clarification(), approval()])
async def test_undo_is_busy_during_pending_input(pending):
    planner = ScriptedPlanner([pending])
    controller = RuntimeController(planner)
    await controller.submit("대기")

    outcome = await controller.undo_last_write()

    assert isinstance(outcome, Busy)
    assert planner.undo_count == 0


def test_voice_compatibility_states_are_declared():
    assert {state.name for state in RuntimeState} == {
        "IDLE",
        "RECORDING",
        "TRANSCRIBING",
        "PLANNING",
        "EXECUTING_TOOL",
        "WAITING_USER_INPUT",
        "WAITING_APPROVAL",
        "RESUMING_PLAN",
        "DONE",
        "ERROR",
    }


async def test_narrow_search_takes_free_text_and_n_beon_picks_an_option():
    options = ("/r/newest.txt", "폴더: 자료")
    context = ClarificationContext("임시 읽어줘", "narrow_search", options, 1)
    question = Clarification("범위를 좁혀 주세요.", context.options, context, ("최신", "자료"))
    planner = ScriptedPlanner([question, question], [Completed("A"), Completed("B")])
    controller = RuntimeController(planner)

    await controller.submit("임시 읽어줘")
    assert await controller.submit("2") == Completed("A")  # a bare number is free text here
    await controller.submit("임시 읽어줘")
    assert await controller.submit("1번") == Completed("B")
    assert planner.resume_inputs == [(context, "2"), (context, "/r/newest.txt")]


async def test_a_cancelled_question_and_an_undo_are_not_rateable(tmp_path):
    from voice_action.feedback import FeedbackLoop

    options = ("/r/a.md", "/r/b.md")
    context = ClarificationContext("요약", "file_candidate", options, 1)
    question = Clarification("고르세요", options, context)
    feedback = FeedbackLoop(tmp_path / "fb.sqlite3")
    planner = ScriptedPlanner(
        [question, Completed("답")], undo_actions=[Completed("되돌렸습니다.")]
    )
    controller = RuntimeController(planner, feedback=feedback)

    await controller.submit("요약해줘")
    assert not controller.ratable  # a question is not an answer
    cancelled = await controller.submit("취소")
    assert cancelled == Completed("요청을 취소했습니다. 파일은 변경하지 않았습니다.")
    assert not controller.ratable and not await controller.rate(5)

    await controller.submit("다시 요약해줘")
    assert controller.ratable
    await controller.undo_last_write()
    assert not controller.ratable and not await controller.rate(5)
    feedback.db.close()


class AttachingPlanner(ScriptedPlanner):
    def __init__(self, config, actions):
        super().__init__(actions)
        self.config, self.attachments = config, []

    async def run(self, text, *, attachment=None, **_options):
        self.attachments.append(attachment)
        return await super().run(text)


async def test_an_attachment_belongs_to_the_next_command_only(config):
    from voice_action.attachment import AttachmentError

    root = config.allowed_directories[0]
    target = root / "a b.md"
    target.write_text("x")
    planner = AttachingPlanner(config, [Completed("1"), Completed("2")])
    controller = RuntimeController(planner)

    assert controller.attach(str(target)) == str(target) and controller.attachment == str(target)
    await controller.submit("이 파일 요약해줘")
    await controller.submit("그냥 질문")

    assert planner.attachments == [str(target), None]
    assert controller.attachment is None
    with pytest.raises(AttachmentError):
        controller.attach(str(root.parent / "밖.txt"))
    assert controller.attachment is None


async def test_a_refused_drop_keeps_the_earlier_attachment_and_detach_clears_it(config):
    from voice_action.attachment import AttachmentError

    root = config.allowed_directories[0]
    (root / "a.md").write_text("x")
    controller = RuntimeController(AttachingPlanner(config, []))
    controller.attach(str(root / "a.md"))
    with pytest.raises(AttachmentError):
        controller.attach(str(root))  # a folder
    assert controller.attachment == str(root / "a.md")
    controller.detach()
    assert controller.attachment is None


async def test_attaching_is_refused_while_a_command_is_running_or_waiting(config):
    from voice_action.attachment import AttachmentError

    root = config.allowed_directories[0]
    (root / "a.md").write_text("x")
    options = ("1",)
    context = ClarificationContext("요약", "file_candidate", options, 1)
    controller = RuntimeController(AttachingPlanner(config, [Clarification("?", options, context)]))
    await controller.submit("요약해줘")  # now waiting for an answer
    with pytest.raises(AttachmentError):
        controller.attach(str(root / "a.md"))
