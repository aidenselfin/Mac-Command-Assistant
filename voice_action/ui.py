"""Minimal Tkinter UI over the existing runtime controller."""

import asyncio
import queue
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from openai import AsyncOpenAI

from voice_action.macos_client import MacOSAXClient
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient
from voice_action.mcp_manager import MCPManager
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    Completed,
    Error,
    OpenAICompatibleModel,
    Planner,
    PlannerEvent,
    PlannerEventType,
)
from voice_action.playwright_client import PlaywrightMCPClient
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

    def close(self) -> None: ...


class UIAdapter(Protocol):
    def submit(self, text: str) -> bool: ...

    def approve_pending(self) -> bool: ...

    def reject_pending(self) -> bool: ...

    def undo_last_write(self) -> bool: ...

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
            f"  {index}. {option}" for index, option in enumerate(outcome.options, 1)
        )
        return f"[QUESTION] {outcome.question}\n{options}"
    if isinstance(outcome, ApprovalRequired):
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
            if isinstance(
                outcome, (Completed, Error, Clarification, ApprovalRequired, Busy)
            ):
                self._dispatch_pending = False
                self._refresh_controls()
                self.view.show_output(render_outcome(outcome))
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
        if message.type is UIMessageType.STOPPED and bool(message.value):
            self.view.close()

    def execute(self, text: str) -> bool:
        normalized = text.strip()
        if (
            not normalized
            or self._dispatch_pending
            or self.state
            not in {RuntimeState.IDLE, RuntimeState.WAITING_USER_INPUT}
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
            return True
        self._dispatch_pending = False
        self._refresh_controls()
        return False

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
        run_enabled = (
            not self._dispatch_pending
            and self.state in {RuntimeState.IDLE, RuntimeState.WAITING_USER_INPUT}
        )
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


def _manager_factory(config) -> MCPManager:
    clients = {
        "filesystem": MCPClient(config, allowed_tools=SUPPORTED_TOOLS),
        "web": PlaywrightMCPClient(config),
    }
    if sys.platform == "darwin":
        clients["macos"] = MacOSAXClient()
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
    ):
        self.config = config
        self.messages = messages
        self._allow_local_web = allow_local_web
        self._manager_factory = manager_factory
        self._client_factory = client_factory
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
        return controller is not None and self._schedule(
            self._dispatch(controller.approve_pending)
        )

    def reject_pending(self) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(
            self._dispatch(controller.reject_pending)
        )

    def undo_last_write(self) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(
            self._dispatch(controller.undo_last_write)
        )

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
            future = asyncio.run_coroutine_threadsafe(
                self._request_shutdown(), self._loop
            )
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
        try:
            if self._pre_shutdown.is_set():
                return
            manager = self._manager_factory(self.config)
            async with manager.connect():
                async with self._client_factory(self.config) as client:
                    tools = await manager.list_tools()
                    planner = Planner(
                        self.config,
                        manager,
                        self._model_factory(client, self.config.model),
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


class TkView:
    """Tk widgets; every method is called only by the Tk main thread."""

    def __init__(self, root):
        import tkinter as tk

        self.root = root
        self._tk = tk
        self.presenter: UIPresenter | None = None
        self.closed = False
        root.title("Voice Action AI")
        root.geometry("720x520")

        self.status_var = tk.StringVar(value="STARTING")
        self.tool_var = tk.StringVar(value="없음")
        tk.Label(root, text="상태:").pack(anchor="w", padx=10, pady=(10, 0))
        tk.Label(root, textvariable=self.status_var).pack(anchor="w", padx=10)
        tk.Label(root, text="현재 도구:").pack(anchor="w", padx=10, pady=(8, 0))
        tk.Label(root, textvariable=self.tool_var).pack(anchor="w", padx=10)

        command_row = tk.Frame(root)
        command_row.pack(fill="x", padx=10, pady=10)
        self.entry = tk.Entry(command_row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.run_button = tk.Button(command_row, text="실행", command=self._execute)
        self.run_button.pack(side="left", padx=(6, 0))
        self.cancel_button = tk.Button(command_row, text="취소", command=self._cancel)
        self.cancel_button.pack(side="left", padx=(6, 0))
        self.entry.bind("<Return>", self._enter)

        self.output = tk.Text(root, wrap="word", state="disabled")
        self.output.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.approval_frame = tk.Frame(root)
        self.approve_button = tk.Button(
            self.approval_frame, text="승인", command=self._approve
        )
        self.approve_button.pack(side="left", padx=(0, 6))
        self.reject_button = tk.Button(
            self.approval_frame, text="거절", command=self._reject
        )
        self.reject_button.pack(side="left")

        bottom = tk.Frame(root)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        self.undo_button = tk.Button(bottom, text="Undo", command=self._undo)
        self.undo_button.pack(side="left")
        tk.Button(bottom, text="종료", command=self._shutdown).pack(side="right")
        root.protocol("WM_DELETE_WINDOW", self._shutdown)

    def bind_presenter(self, presenter: UIPresenter) -> None:
        self.presenter = presenter

    def set_status(self, value: str) -> None:
        self.status_var.set(value)

    def set_tool(self, value: str) -> None:
        self.tool_var.set(value)

    def set_controls(
        self,
        *,
        run_enabled: bool,
        cancel_enabled: bool,
        approval_visible: bool,
        undo_enabled: bool,
    ) -> None:
        normal, disabled = self._tk.NORMAL, self._tk.DISABLED
        self.run_button.configure(state=normal if run_enabled else disabled)
        self.cancel_button.configure(state=normal if cancel_enabled else disabled)
        self.undo_button.configure(state=normal if undo_enabled else disabled)
        if approval_visible:
            if not self.approval_frame.winfo_ismapped():
                self.approval_frame.pack(
                    before=self.output, padx=10, pady=(0, 10), anchor="w"
                )
        else:
            self.approval_frame.pack_forget()

    def show_output(self, value: str) -> None:
        self.output.configure(state=self._tk.NORMAL)
        self.output.insert(self._tk.END, f"{value}\n")
        self.output.see(self._tk.END)
        self.output.configure(state=self._tk.DISABLED)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.root.destroy()

    def _execute(self) -> None:
        if self.presenter is not None and self.presenter.execute(self.entry.get()):
            self.entry.delete(0, self._tk.END)

    def _enter(self, _event) -> str:
        self._execute()
        return "break"

    def _approve(self) -> None:
        if self.presenter is not None:
            self.presenter.approve()

    def _reject(self) -> None:
        if self.presenter is not None:
            self.presenter.reject()

    def _cancel(self) -> None:
        if self.presenter is not None:
            self.presenter.cancel()

    def _undo(self) -> None:
        if self.presenter is not None:
            self.presenter.undo()

    def _shutdown(self) -> None:
        if self.presenter is not None:
            self.presenter.shutdown()


def run_ui(config) -> None:
    """Run the Tk main loop and a single long-lived asyncio worker loop."""
    import tkinter as tk

    root = tk.Tk()
    messages: queue.Queue[UIMessage] = queue.Queue()
    worker = AsyncRuntimeWorker(config, messages)
    view = TkView(root)
    presenter = UIPresenter(worker, view)
    view.bind_presenter(presenter)

    def poll_messages() -> None:
        while True:
            try:
                presenter.handle(messages.get_nowait())
            except queue.Empty:
                break
        if not view.closed:
            root.after(50, poll_messages)

    worker.start()
    root.after(0, poll_messages)
    try:
        root.mainloop()
    finally:
        worker.shutdown()
