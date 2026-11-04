"""Shared UI messages, presenter and runtime worker used by the native bridge."""

import asyncio
import queue
import threading
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from openai import AsyncOpenAI

from voice_action.attachment import AttachmentError
from voice_action.feedback import LOW_RATING, build_feedback
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient
from voice_action.mcp_manager import MCPManager
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    Completed,
    Error,
    FallbackModel,
    OpenAICompatibleModel,
    Planner,
    PlannerError,
    PlannerEvent,
    PlannerEventType,
)
from voice_action.runtime import Busy, RuntimeController, RuntimeResult, RuntimeState
from voice_action.undo import UndoJournal, UndoJournalError

UI_STATE_LABELS = {
    RuntimeState.IDLE: "READY",
    RuntimeState.RECORDING: "RECORDING",
    RuntimeState.TRANSCRIBING: "TRANSCRIBING",
    RuntimeState.PLANNING: "PLANNING",
    RuntimeState.EXECUTING_TOOL: "EXECUTING_TOOL",
    RuntimeState.WAITING_USER_INPUT: "WAITING_USER_INPUT",
    RuntimeState.WAITING_APPROVAL: "WAITING_APPROVAL",
    RuntimeState.RESUMING_PLAN: "PLANNING",
    RuntimeState.DONE: "DONE",
    RuntimeState.ERROR: "ERROR",
}


class UIMessageType(Enum):
    STATE = auto()
    PLANNER_EVENT = auto()
    OUTCOME = auto()
    FAILURE = auto()
    NOTICE = auto()
    ATTACHMENT = auto()  # value: (path or None, refusal text or None)
    RATING_SAVED = auto()  # value: (score, tags)
    STOPPED = auto()


@dataclass(frozen=True)
class UIMessage:
    type: UIMessageType
    value: object = None


class UIView(Protocol):
    def set_status(self, value: str) -> None: ...

    def set_tool(self, value: str) -> None: ...

    def set_controls(
        self,
        *,
        run_enabled: bool,
        cancel_enabled: bool,
        approval_visible: bool,
        undo_enabled: bool,
    ) -> None: ...

    def show_output(self, value: str) -> None: ...

    def show_outcome(self, outcome: RuntimeResult) -> None: ...

    def show_request(self, text: str) -> None: ...

    def set_approval_enabled(self, enabled: bool) -> None: ...

    def set_choices(self, labels: tuple[str, ...]) -> None: ...

    def reveal_files(self, paths: tuple[str, ...]) -> None: ...

    def set_attachment(self, path: str | None) -> None: ...

    def set_rating(
        self, visible: bool, score: int | None = None, tags: tuple[str, ...] = ()
    ) -> None: ...

    def close(self) -> None: ...


class UIAdapter(Protocol):
    def submit(self, text: str) -> bool: ...

    def approve_pending(self) -> bool: ...

    def reject_pending(self) -> bool: ...

    def undo_last_write(self) -> bool: ...

    def rate(self, score: int, tags: tuple[str, ...]) -> bool: ...

    def can_rate(self) -> bool: ...

    def attach(self, path: str) -> bool: ...

    def detach(self) -> None: ...

    def cancel_active(self) -> bool: ...

    def shutdown(self) -> None: ...


def render_outcome(outcome: RuntimeResult) -> str:
    """Render only the bounded public fields of a runtime outcome."""
    if isinstance(outcome, Completed):
        return f"[DONE] {outcome.text}"
    if isinstance(outcome, Error):
        return f"[ERROR] {outcome.message}"
    if isinstance(outcome, Clarification):
        options = "\n".join(
            f"  {index}. {option}" for index, option in enumerate(outcome.shown_options, 1)
        )
        return f"[QUESTION] {outcome.question}\n{options}"
    if isinstance(outcome, ApprovalRequired):
        if outcome.operation == "delete":
            import json

            targets = "\n".join(json.dumps(path, ensure_ascii=False) for path in outcome.targets)
            return (
                f"[APPROVAL REQUIRED]\noperation: delete\n"
                f"삭제 대상: {len(outcome.targets)}개 파일\n{targets}\n"
                "승인하면 위 파일들을 백업 후 삭제합니다. Undo로 일괄 복원할 수 있습니다."
            )
        if outcome.operation in {"move", "copy", "mkdir"}:
            return "\n".join((
                "[APPROVAL REQUIRED]", f"operation: {outcome.operation}",
                *((f"source: {outcome.targets[0]}",) if outcome.targets else ()),
                f"target: {outcome.target_path}",
                "승인 후 실행합니다. 기존 경로를 덮어쓰지 않으며 Undo로 되돌릴 수 있습니다.",
            ))
        preview = outcome.preview[:200]
        suffix = "…" if outcome.preview_truncated else ""
        return "\n".join(
            (
                "[APPROVAL REQUIRED]",
                f"tool: {outcome.tool_name}",
                f"target: {outcome.target_path}",
                f"operation: {outcome.operation}",
                f"content_chars: {outcome.content_chars}",
                f"preview: {preview}{suffix}",
            )
        )
    if isinstance(outcome, Busy):
        return f"[BUSY] {outcome.message}"
    raise TypeError("지원하지 않는 runtime 결과입니다.")


class UIPresenter:
    """Main-thread presentation logic, independently testable without Tk()."""

    def __init__(self, adapter: UIAdapter, view: UIView):
        self.adapter = adapter
        self.view = view
        self.state: RuntimeState | None = None
        self._dispatch_pending = False
        self._choices: tuple[str, ...] = ()  # button labels while a question is open
        self._rating_open = False  # the last answer can still be rated
        self._score: int | None = None
        self._tags: tuple[str, ...] = ()
        self._attached: str | None = None  # a dropped file waiting for the next command
        self.view.set_status("STARTING")
        self.view.set_tool("없음")
        self.view.set_controls(
            run_enabled=False,
            cancel_enabled=False,
            approval_visible=False,
            undo_enabled=False,
        )

    def handle(self, message: UIMessage) -> None:
        if message.type is UIMessageType.STATE:
            self._set_state(message.value)
            return
        if message.type is UIMessageType.PLANNER_EVENT:
            event = message.value
            if (
                isinstance(event, PlannerEvent)
                and event.type is PlannerEventType.TOOL_STARTED
                and event.tool_name is not None
            ):
                self.view.set_tool(event.tool_name)
            return
        if message.type is UIMessageType.OUTCOME:
            outcome = message.value
            if isinstance(outcome, (Completed, Error, Clarification, ApprovalRequired, Busy)):
                self._dispatch_pending = False
                if isinstance(outcome, Completed) and outcome.changed_paths:
                    self.view.reveal_files(outcome.changed_paths)
                if not isinstance(outcome, Busy):
                    self._choices = (
                        outcome.shown_options if isinstance(outcome, Clarification) else ()
                    )
                    self._rating_open = isinstance(outcome, Completed) and self.adapter.can_rate()
                    self._score, self._tags = None, ()
                self.view.show_outcome(outcome)
                self._refresh_controls()
            return
        if message.type is UIMessageType.FAILURE:
            self._dispatch_pending = False
            self.view.set_status("ERROR")
            self.view.set_controls(
                run_enabled=False,
                cancel_enabled=False,
                approval_visible=False,
                undo_enabled=False,
            )
            self.view.show_output(f"[ERROR] {message.value}")
            return
        if message.type is UIMessageType.NOTICE:
            self.view.show_output(str(message.value))
            return
        if message.type is UIMessageType.RATING_SAVED:
            score, tags = message.value
            if (
                self._can_rate()
                and (score, tags) == (self._score, self._tags)
                and (score > LOW_RATING or tags)
            ):
                self._rating_open = False
                self._refresh_controls()
            return
        if message.type is UIMessageType.ATTACHMENT:
            path, refusal = message.value
            if path is not None:
                self._attached = path
                self.view.set_attachment(path)
            if refusal:
                self.view.show_output(refusal)
            return
        if message.type is UIMessageType.STOPPED and bool(message.value):
            self.view.close()

    def execute(self, text: str, shown: str | None = None) -> bool:
        normalized = text.strip()
        if (
            not normalized
            or self._dispatch_pending
            or self.state not in {RuntimeState.IDLE, RuntimeState.WAITING_USER_INPUT}
        ):
            return False
        self._dispatch_pending = True
        self.view.set_controls(
            run_enabled=False,
            cancel_enabled=True,
            approval_visible=False,
            undo_enabled=False,
        )
        if self.adapter.submit(normalized):
            self._choices, self._rating_open = (), False
            self.view.set_choices(())
            self.view.set_rating(False)
            if self.state is RuntimeState.IDLE and self._attached is not None:
                self._attached = None  # it went with this command
                self.view.set_attachment(None)
            self.view.show_request(shown or normalized)
            return True
        self._dispatch_pending = False
        self._refresh_controls()
        return False

    def attach(self, paths: tuple[str, ...]) -> bool:
        """A file dropped on the window. One file, only while idle; the app checks the path."""
        if len(paths) != 1:
            self.view.show_output("파일은 하나만 놓을 수 있어요. 요청을 거부했습니다.")
            return False
        if self.state is not RuntimeState.IDLE or self._dispatch_pending:
            self.view.show_output("작업이 끝난 뒤에 파일을 놓아 주세요.")
            return False
        return self.adapter.attach(paths[0])

    def detach(self) -> None:
        self.adapter.detach()
        self._attached = None
        self.view.set_attachment(None)

    def choose(self, number: int) -> bool:
        """Answer the open question with its numbered button (1-based). `N번` is the form
        both question kinds accept, so a number is never mistaken for free text."""
        asking = self.state is RuntimeState.WAITING_USER_INPUT
        if not asking or not 1 <= number <= len(self._choices):
            return False
        return self.execute(f"{number}번", shown=f"{number}. {self._choices[number - 1]}")

    def rate(self, score: int) -> bool:
        if not self._can_rate() or score not in range(1, 6):
            return False
        # Low scores keep the chosen causes; a better score drops them.
        tags = self._tags if score <= LOW_RATING else ()
        if not self.adapter.rate(score, tags):
            return False
        self._score, self._tags = score, tags
        self._refresh_controls()
        return True

    def toggle_tag(self, tag: str) -> bool:
        """Pick or drop a cause for a low rating; the rating is sent again with it."""
        if not self._can_rate() or self._score is None or self._score > LOW_RATING:
            return False
        tags = tuple(t for t in self._tags if t != tag)
        if len(tags) == len(self._tags):
            tags = (*self._tags, tag)
        if not self.adapter.rate(self._score, tags):
            return False
        self._tags = tags
        self._refresh_controls()
        return True

    def _can_rate(self) -> bool:
        return self._rating_open and self.state is RuntimeState.IDLE and not self._dispatch_pending

    def approve(self) -> bool:
        if self.state is not RuntimeState.WAITING_APPROVAL or self._dispatch_pending:
            return False
        self._dispatch_pending = True
        self._refresh_controls()
        if self.adapter.approve_pending():
            return True
        self._dispatch_pending = False
        self._refresh_controls()
        return False

    def reject(self) -> bool:
        if self.state is not RuntimeState.WAITING_APPROVAL or self._dispatch_pending:
            return False
        self._dispatch_pending = True
        self._refresh_controls()
        if self.adapter.reject_pending():
            return True
        self._dispatch_pending = False
        self._refresh_controls()
        return False

    def cancel(self) -> bool:
        if self.state is RuntimeState.WAITING_APPROVAL:
            return self.reject()
        if self.state in {
            RuntimeState.PLANNING,
            RuntimeState.EXECUTING_TOOL,
            RuntimeState.RESUMING_PLAN,
        }:
            self._dispatch_pending = True
            self._refresh_controls()
            return self.adapter.cancel_active()
        return False

    def undo(self) -> bool:
        if self.state is not RuntimeState.IDLE or self._dispatch_pending:
            return False
        self._dispatch_pending = True
        self._refresh_controls()
        if self.adapter.undo_last_write():
            return True
        self._dispatch_pending = False
        self._refresh_controls()
        return False

    def shutdown(self) -> None:
        self.adapter.shutdown()
        self.view.close()

    def _set_state(self, value: object) -> None:
        if not isinstance(value, RuntimeState):
            raise TypeError("RuntimeState가 필요합니다.")
        self.state = value
        self.view.set_status(UI_STATE_LABELS[value])
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        run_enabled = not self._dispatch_pending and self.state in {
            RuntimeState.IDLE,
            RuntimeState.WAITING_USER_INPUT,
        }
        approval_visible = self.state is RuntimeState.WAITING_APPROVAL
        undo_enabled = not self._dispatch_pending and self.state is RuntimeState.IDLE
        cancel_enabled = self.state in {
            RuntimeState.PLANNING,
            RuntimeState.EXECUTING_TOOL,
            RuntimeState.RESUMING_PLAN,
            RuntimeState.WAITING_APPROVAL,
        }
        self.view.set_controls(
            run_enabled=run_enabled,
            cancel_enabled=cancel_enabled,
            approval_visible=approval_visible,
            undo_enabled=undo_enabled,
        )

        self.view.set_approval_enabled(approval_visible and not self._dispatch_pending)
        asking = self.state is RuntimeState.WAITING_USER_INPUT and not self._dispatch_pending
        self.view.set_choices(self._choices if asking else ())
        self.view.set_rating(self._can_rate(), self._score, self._tags)


def _manager_factory(config) -> MCPManager:
    clients = {
        "filesystem": MCPClient(config, allowed_tools=SUPPORTED_TOOLS),
    }
    return MCPManager(clients)


def _client_factory(config):
    return AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.llm_timeout_seconds,
        max_retries=0,
    )


class AsyncRuntimeWorker:
    """Own one asyncio loop and all MCP/HTTP resources on one non-daemon thread."""

    def __init__(
        self,
        config,
        messages: queue.Queue[UIMessage],
        *,
        allow_local_web: bool = False,
        manager_factory: Callable = _manager_factory,
        client_factory: Callable = _client_factory,
        model_factory: Callable = OpenAICompatibleModel,
        feedback_factory: Callable = build_feedback,
    ):
        self.config = config
        self.messages = messages
        self._allow_local_web = allow_local_web
        self._manager_factory = manager_factory
        self._client_factory = client_factory
        self._feedback_factory = feedback_factory
        self._model_factory = model_factory
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._controller: RuntimeController | None = None
        self._active_task: asyncio.Task | None = None
        self._ready = False
        self._pre_shutdown = threading.Event()
        self._stopped = threading.Event()
        self._close_after_stop = False

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("UI worker가 이미 시작되었습니다.")
        self._thread = threading.Thread(
            target=self._thread_main,
            name="voice-action-async-worker",
            daemon=False,
        )
        self._thread.start()

    def submit(self, text: str) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(
            self._dispatch(lambda: controller.submit(text))
        )

    def approve_pending(self) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(self._dispatch(controller.approve_pending))

    def reject_pending(self) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(self._dispatch(controller.reject_pending))

    def undo_last_write(self) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(self._dispatch(controller.undo_last_write))

    def can_rate(self) -> bool:
        controller = self._controller
        return controller is not None and controller.ratable

    def attach(self, path: str) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(self._attach(controller, path))

    def detach(self) -> None:
        if self._controller is not None:
            self._controller.detach()

    async def _attach(self, controller, path: str) -> None:
        try:
            stored = controller.attach(path)
        except AttachmentError as exc:
            self.messages.put(UIMessage(UIMessageType.ATTACHMENT, (None, str(exc))))
        else:
            self.messages.put(UIMessage(UIMessageType.ATTACHMENT, (stored, None)))

    def rate(self, score: int, tags: tuple[str, ...]) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(self._rate(controller, score, tags))

    async def _rate(self, controller, score: int, tags: tuple[str, ...]) -> None:
        # Scheduling worked; whether the rating was stored is only known here.
        if not await controller.rate(score, tags):
            self.messages.put(
                UIMessage(UIMessageType.NOTICE, "평가를 저장하지 못했어요. 다시 눌러 보세요.")
            )
        else:
            self.messages.put(UIMessage(UIMessageType.RATING_SAVED, (score, tags)))

    def cancel_active(self) -> bool:
        if self._active_task is None:
            return False
        return self._schedule(self._cancel_and_stop())

    def shutdown(self) -> None:
        self._pre_shutdown.set()
        thread = self._thread
        if thread is None:
            return
        if thread.is_alive() and self._loop is not None:
            future = asyncio.run_coroutine_threadsafe(self._request_shutdown(), self._loop)
            with suppress(Exception):
                future.result()
        if threading.current_thread() is not thread:
            thread.join()

    def _schedule(self, coroutine) -> bool:
        loop = self._loop
        if not self._ready or loop is None or loop.is_closed():
            coroutine.close()
            return False
        asyncio.run_coroutine_threadsafe(coroutine, loop)
        return True

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        finally:
            self._stopped.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._lifecycle_task = asyncio.current_task()
        self._shutdown_event = asyncio.Event()
        feedback = None
        try:
            if self._pre_shutdown.is_set():
                return
            manager = self._manager_factory(self.config)
            feedback = self._feedback_factory(
                self.config, lambda text: self.messages.put(UIMessage(UIMessageType.NOTICE, text))
            )
            async with manager.connect():
                async with AsyncExitStack() as stack:
                    client = await stack.enter_async_context(self._client_factory(self.config))
                    tools = await manager.list_tools()
                    model = self._model_factory(client, self.config.model)
                    if self.config.fallback_api_key:
                        spare = await stack.enter_async_context(
                            AsyncOpenAI(
                                api_key=self.config.fallback_api_key,
                                base_url="https://api.openai.com/v1",
                                timeout=self.config.llm_timeout_seconds,
                                max_retries=0,
                            )
                        )
                        model = FallbackModel(
                            model, self._model_factory(spare, self.config.fallback_model)
                        )
                    planner = Planner(
                        self.config,
                        manager,
                        feedback.wrap(model) if feedback else model,
                        tools=tools,
                        allow_local_web=self._allow_local_web,
                        undo_journal=self._build_undo_journal(),
                    )
                    self._controller = RuntimeController(
                        planner,
                        state_observer=lambda transition: self.messages.put(
                            UIMessage(UIMessageType.STATE, transition.current)
                        ),
                        planner_event_observer=lambda event: self.messages.put(
                            UIMessage(UIMessageType.PLANNER_EVENT, event)
                        ),
                        feedback=feedback,
                    )
                    self._ready = True
                    self.messages.put(UIMessage(UIMessageType.STATE, RuntimeState.IDLE))
                    if self._pre_shutdown.is_set():
                        self._shutdown_event.set()
                    await self._shutdown_event.wait()
                    await self._cancel_active_task()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._close_after_stop = True
            self.messages.put(
                UIMessage(
                    UIMessageType.FAILURE,
                    f"실행 실패 ({type(exc).__name__}); 설정과 MCP/네트워크 연결을 확인하세요.",
                )
            )
        finally:
            if feedback is not None:
                await feedback.close(wait_seconds=5)
            self._ready = False
            self._controller = None
            self._active_task = None
            self.messages.put(UIMessage(UIMessageType.STOPPED, self._close_after_stop))

    def _build_undo_journal(self) -> UndoJournal | None:
        try:
            return UndoJournal.default(self.config.allowed_directories)
        except UndoJournalError:
            return None

    async def _dispatch(self, action: Callable[[], object]) -> None:
        if self._active_task is not None:
            return
        task = asyncio.create_task(action())
        self._active_task = task
        try:
            outcome = await task
        except asyncio.CancelledError:
            return
        except PlannerError as exc:
            # Known, already-redacted failures (e.g. the LLM API rejected the request): the
            # controller is back to IDLE, so report it and keep the window usable.
            self.messages.put(UIMessage(UIMessageType.OUTCOME, Error(str(exc))))
        except Exception as exc:
            self._close_after_stop = True
            self.messages.put(
                UIMessage(
                    UIMessageType.FAILURE,
                    f"실행 실패 ({type(exc).__name__}); 설정과 MCP/네트워크 연결을 확인하세요.",
                )
            )
            if self._shutdown_event is not None:
                self._shutdown_event.set()
        else:
            self.messages.put(UIMessage(UIMessageType.OUTCOME, outcome))
        finally:
            if self._active_task is task:
                self._active_task = None

    async def _cancel_active_task(self) -> None:
        task = self._active_task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _cancel_and_stop(self) -> None:
        self._close_after_stop = True
        await self._cancel_active_task()
        self.messages.put(
            UIMessage(
                UIMessageType.NOTICE,
                "요청을 중단하고 실행 중인 도구를 정리했습니다.",
            )
        )
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    async def _request_shutdown(self) -> None:
        await self._cancel_active_task()
        if self._controller is None:
            if self._lifecycle_task is not None:
                self._lifecycle_task.cancel()
            return
        if self._shutdown_event is not None:
            self._shutdown_event.set()


def display_path(path: str) -> str:
    """A path as shown for approval: control characters (newline, tab, ...) are escaped so
    one file name can't look like two rows. Only the display changes, never the path used."""
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in path
    )
