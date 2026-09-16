"""Single-flight runtime state machine for text and future voice commands."""

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum, auto

from voice_action.planner import (
    MAX_CLARIFICATIONS,
    Clarification,
    Completed,
    Error,
    Planner,
    PlannerEvent,
    PlannerEventType,
    PlannerOutcome,
)


class RuntimeState(Enum):
    IDLE = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    PLANNING = auto()
    EXECUTING_TOOL = auto()
    WAITING_USER_INPUT = auto()
    RESUMING_PLAN = auto()
    DONE = auto()
    ERROR = auto()


STATE_TRANSITIONS: dict[RuntimeState, frozenset[RuntimeState]] = {
    RuntimeState.IDLE: frozenset(
        {RuntimeState.RECORDING, RuntimeState.PLANNING, RuntimeState.ERROR}
    ),
    RuntimeState.RECORDING: frozenset(
        {RuntimeState.TRANSCRIBING, RuntimeState.IDLE, RuntimeState.ERROR}
    ),
    RuntimeState.TRANSCRIBING: frozenset(
        {RuntimeState.PLANNING, RuntimeState.ERROR}
    ),
    RuntimeState.PLANNING: frozenset(
        {
            RuntimeState.EXECUTING_TOOL,
            RuntimeState.WAITING_USER_INPUT,
            RuntimeState.DONE,
            RuntimeState.ERROR,
        }
    ),
    RuntimeState.EXECUTING_TOOL: frozenset(
        {RuntimeState.PLANNING, RuntimeState.ERROR}
    ),
    RuntimeState.WAITING_USER_INPUT: frozenset(
        {RuntimeState.RESUMING_PLAN, RuntimeState.DONE, RuntimeState.ERROR}
    ),
    RuntimeState.RESUMING_PLAN: frozenset(
        {
            RuntimeState.PLANNING,
            RuntimeState.WAITING_USER_INPUT,
            RuntimeState.DONE,
            RuntimeState.ERROR,
        }
    ),
    RuntimeState.DONE: frozenset({RuntimeState.IDLE, RuntimeState.ERROR}),
    RuntimeState.ERROR: frozenset({RuntimeState.IDLE}),
}


@dataclass(frozen=True)
class StateTransition:
    previous: RuntimeState
    current: RuntimeState
    reason: str


@dataclass(frozen=True)
class Busy:
    message: str
    state: RuntimeState


RuntimeResult = PlannerOutcome | Busy
RuntimeLog = Callable[[str], None]


class RuntimeTransitionError(RuntimeError):
    pass


class RuntimeController:
    """Own command state, pending clarification, and single-flight execution."""

    def __init__(self, planner: Planner, emit: RuntimeLog | None = None):
        self.planner = planner
        self.emit = emit or (lambda _: None)
        self.state = RuntimeState.IDLE
        self._history: list[StateTransition] = []
        self._pending: Clarification | None = None
        self._in_flight = False
        self.planner.set_event_observer(self._on_planner_event)

    @property
    def transition_history(self) -> tuple[StateTransition, ...]:
        return tuple(self._history)

    @property
    def pending(self) -> Clarification | None:
        return self._pending

    async def submit(self, text: str) -> RuntimeResult:
        if self._in_flight or self.state not in {
            RuntimeState.IDLE,
            RuntimeState.WAITING_USER_INPUT,
        }:
            return Busy("다른 명령을 처리 중입니다.", self.state)

        self._in_flight = True
        try:
            if self.state is RuntimeState.WAITING_USER_INPUT:
                return await self._submit_clarification_answer(text)
            self._transition(RuntimeState.PLANNING, "command_started")
            outcome = await self.planner.run(text)
            return self._finish(outcome)
        except BaseException:
            self._pending = None
            if self.state is not RuntimeState.ERROR:
                self._transition(RuntimeState.ERROR, "exception")
            self._transition(RuntimeState.IDLE, "error_observed")
            raise
        finally:
            self._in_flight = False

    async def _submit_clarification_answer(self, answer: str) -> PlannerOutcome:
        pending = self._pending
        if pending is None:
            raise RuntimeTransitionError("pending clarification이 없습니다.")

        normalized = answer.strip()
        if normalized.casefold() in {"취소", "cancel", "c"}:
            return self._finish(Completed("요청을 취소했습니다. 파일은 변경하지 않았습니다."))

        match = re.fullmatch(r"(\d+)\s*번?", normalized)
        index = int(match.group(1)) - 1 if match else -1
        if not 0 <= index < len(pending.options):
            count = pending.context.clarification_count + 1
            if count > MAX_CLARIFICATIONS:
                return self._finish(
                    Error("clarification 최대 횟수를 초과해 파일을 변경하지 않았습니다.")
                )
            context = replace(pending.context, clarification_count=count)
            retry = Clarification(
                "올바른 선택지 번호를 입력하거나 취소를 입력해 주세요.",
                pending.options,
                context,
            )
            self._pending = retry
            return retry

        selection = pending.options[index]
        self._transition(RuntimeState.RESUMING_PLAN, "clarification_selected")
        outcome = await self.planner.resume(pending.context, selection)
        return self._finish(outcome)

    def _finish(self, outcome: PlannerOutcome) -> PlannerOutcome:
        if isinstance(outcome, Clarification):
            self._pending = outcome
            self._transition(RuntimeState.WAITING_USER_INPUT, "clarification_required")
            return outcome

        self._pending = None
        if isinstance(outcome, Completed):
            self._transition(RuntimeState.DONE, "completed")
            self._transition(RuntimeState.IDLE, "result_observed")
            return outcome

        self._transition(RuntimeState.ERROR, "planner_error")
        self._transition(RuntimeState.IDLE, "error_observed")
        return outcome

    def _on_planner_event(self, event: PlannerEvent) -> None:
        if event.type is PlannerEventType.PLANNING_STARTED:
            if self.state in {RuntimeState.EXECUTING_TOOL, RuntimeState.RESUMING_PLAN}:
                self._transition(RuntimeState.PLANNING, f"planning_iteration_{event.iteration}")
            return
        if event.type is PlannerEventType.TOOL_STARTED:
            self._transition(
                RuntimeState.EXECUTING_TOOL,
                f"tool_started:{event.tool_name or 'unknown'}",
            )
            return
        if event.type is PlannerEventType.TOOL_FINISHED:
            self._transition(
                RuntimeState.PLANNING,
                f"tool_finished:{event.tool_name or 'unknown'}",
            )

    def _transition(self, target: RuntimeState, reason: str) -> None:
        if target is self.state:
            return
        if target not in STATE_TRANSITIONS[self.state]:
            raise RuntimeTransitionError(
                f"허용되지 않은 상태 전이: {self.state.name} -> {target.name}"
            )
        transition = StateTransition(self.state, target, reason)
        self.state = target
        self._history.append(transition)
        self.emit(
            f"[STATE] {transition.previous.name} -> {transition.current.name} "
            f"reason={transition.reason}"
        )
