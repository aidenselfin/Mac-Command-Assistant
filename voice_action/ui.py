"""Minimal Tkinter UI over the existing runtime controller."""

import asyncio
import queue
import re
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

    def show_outcome(self, outcome: RuntimeResult) -> None: ...

    def show_request(self, text: str) -> None: ...

    def set_approval_enabled(self, enabled: bool) -> None: ...

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
        if outcome.operation == "delete":
            import json

            targets = "\n".join(json.dumps(path, ensure_ascii=False) for path in outcome.targets)
            return (
                f"[APPROVAL REQUIRED]\noperation: delete\n"
                f"삭제 대상: {len(outcome.targets)}개 파일\n{targets}\n"
                "승인하면 위 파일들을 백업 후 삭제합니다. Undo로 일괄 복원할 수 있습니다."
            )
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
            if isinstance(outcome, (Completed, Error, Clarification, ApprovalRequired, Busy)):
                self._dispatch_pending = False
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
        if message.type is UIMessageType.STOPPED and bool(message.value):
            self.view.close()

    def execute(self, text: str) -> bool:
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
            self.view.show_request(normalized)
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
        return controller is not None and self._schedule(self._dispatch(controller.approve_pending))

    def reject_pending(self) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(self._dispatch(controller.reject_pending))

    def undo_last_write(self) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(self._dispatch(controller.undo_last_write))

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


def output_blocks(value: str):
    """Yield plain text or (headers, rows, original Markdown) table blocks."""
    lines = value.splitlines(keepends=True)
    text = []
    fence = None
    index = 0

    def cells(line):
        # ponytail: pipe tables only; use a Markdown parser if richer syntax is needed.
        parts = re.split(r"(?<!\\)\|", line.strip())
        if parts[0] == "":
            parts.pop(0)
        if parts and parts[-1] == "":
            parts.pop()
        return [part.strip().replace(r"\|", "|") for part in parts]

    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker[1]
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not line.strip()[len(run):]:
                fence = None
            text.append(line)
            index += 1
            continue
        if fence is None and "|" in line and index + 1 < len(lines):
            headers = cells(line)
            separators = cells(lines[index + 1])
            if (
                len(headers) == len(separators)
                and len(headers) > 1
                and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separators)
            ):
                end = index + 2
                rows = []
                valid = True
                while end < len(lines) and "|" in lines[end] and lines[end].strip():
                    row = cells(lines[end])
                    valid = valid and len(row) == len(headers)
                    rows.append(row)
                    end += 1
                original = "".join(lines[index:end])
                if valid:
                    if text:
                        yield "".join(text)
                        text = []
                    yield headers, rows, original
                else:
                    text.append(original)
                index = end
                continue
        text.append(line)
        index += 1
    if text:
        yield "".join(text)


class TkView:
    """Tk widgets; every method is called only by the Tk main thread."""

    def __init__(self, root):
        import tkinter as tk
        from tkinter import font, ttk

        self.root = root
        self._tk = tk
        self.presenter: UIPresenter | None = None
        self.closed = False
        self._approval_ready = False
        self._approval_enabled = False
        self._output_original = []
        self._has_output = False
        self._status = "STARTING"
        bg, surface, ink, muted = "#F5F6F8", "#FFFFFF", "#20242C", "#596273"
        root.title("Voice Action AI")
        root.geometry("880x680")
        root.minsize(720, 520)
        root.configure(bg=bg)
        family = font.nametofont("TkDefaultFont").actual("family")
        root.option_add("*Font", (family, 12))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TButton", padding=(12, 8), foreground=ink, background=surface)
        style.map(
            "TButton", background=[("active", "#E8ECF4")], foreground=[("disabled", "#808897")]
        )
        style.configure("Primary.TButton", foreground=surface, background="#3659D9")
        style.map(
            "Primary.TButton",
            background=[("disabled", "#D9DEE7"), ("active", "#2847BC")],
            foreground=[("disabled", muted)],
        )
        style.configure("Danger.TButton", foreground=surface, background="#B42318")
        style.map(
            "Danger.TButton",
            background=[("disabled", "#D9DEE7"), ("active", "#912018")],
            foreground=[("disabled", muted)],
        )
        style.configure("TEntry", padding=10, fieldbackground=surface)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        header = tk.Frame(root, bg=bg)
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 12))
        tk.Label(header, text="Voice Action AI", bg=bg, fg=ink, font=(family, 18, "bold")).pack(
            side="left"
        )
        ttk.Button(header, text="종료", command=self._shutdown).pack(side="right")
        self.status_var = tk.StringVar(value="● 연결 준비 중")
        self.status_label = tk.Label(
            header, textvariable=self.status_var, bg=bg, fg=muted, font=(family, 10)
        )
        self.status_label.pack(side="right", padx=16)

        workspace = tk.Frame(root, bg=bg)
        workspace.grid(row=1, column=0, sticky="nsew", padx=24)
        workspace.grid_columnconfigure(0, weight=1)
        workspace.grid_rowconfigure(1, weight=1)
        self.welcome = tk.Frame(workspace, bg=bg)
        self.welcome.grid(row=0, column=0, sticky="ew", pady=(8, 16))
        tk.Label(
            self.welcome, text="무엇을 도와드릴까요?", bg=bg, fg=ink, font=(family, 18, "bold")
        ).pack(anchor="w")
        tk.Label(
            self.welcome,
            text="허용된 폴더의 파일을 찾거나 문서를 요약할 수 있어요.",
            bg=bg,
            fg=muted,
            font=(family, 10),
        ).pack(anchor="w", pady=(8, 12))
        examples = tk.Frame(self.welcome, bg=bg)
        examples.pack(anchor="w")
        self.example_buttons = []
        for label, text in (
            ("파일 목록 보기", "허용된 폴더의 파일 목록을 보여줘"),
            ("문서 요약하기", "test1.md를 요약해줘"),
            ("웹 페이지 읽기", "https://example.com 페이지를 읽고 요약해줘"),
        ):
            button = ttk.Button(
                examples, text=label, command=lambda text=text: self._fill_example(text)
            )
            button.pack(side="left", padx=(0, 8))
            self.example_buttons.append(button)

        output_frame = tk.Frame(
            workspace, bg=surface, highlightthickness=1, highlightbackground="#D9DEE7"
        )
        output_frame.grid(row=1, column=0, sticky="nsew")
        self.output = tk.Text(
            output_frame,
            wrap="word",
            state="disabled",
            height=6,
            bg=surface,
            fg=ink,
            bd=0,
            padx=16,
            pady=16,
            spacing1=4,
            spacing3=8,
            selectbackground="#DCE5FF",
        )
        self.output.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(output_frame, command=self.output.yview)
        scrollbar.pack(side="right", fill="y")
        self.output.configure(yscrollcommand=scrollbar.set)
        for tag, color in (
            ("request", "#3659D9"),
            ("result", ink),
            ("error", "#B42318"),
            ("notice", muted),
            ("question", "#805B10"),
        ):
            self.output.tag_configure(tag, foreground=color)
            self.output.tag_configure(
                tag + "_title", foreground=color, font=(family, 11, "bold"), spacing1=14
            )

        self.tool_var = tk.StringVar(value="")
        tk.Label(
            workspace, textvariable=self.tool_var, bg=bg, fg=muted, anchor="w", font=(family, 10)
        ).grid(row=2, column=0, sticky="ew", pady=(8, 4))
        self.approval_frame = tk.Frame(
            workspace,
            bg="#FFF8E8",
            padx=16,
            pady=12,
            highlightthickness=1,
            highlightbackground="#E6D3A7",
        )
        self.approval_title = tk.StringVar()
        tk.Label(
            self.approval_frame,
            textvariable=self.approval_title,
            bg="#FFF8E8",
            fg=ink,
            font=(family, 12, "bold"),
        ).pack(anchor="w")
        details = tk.Frame(self.approval_frame, bg="#FFF8E8")
        details.pack(fill="both", expand=True, pady=(8, 8))
        self.approval_output = tk.Text(
            details,
            height=5,
            wrap="word",
            state="disabled",
            bg="#FFF8E8",
            fg=ink,
            bd=0,
            highlightthickness=0,
        )
        self.approval_output.pack(side="left", fill="both", expand=True)
        approval_scroll = ttk.Scrollbar(details, command=self.approval_output.yview)
        approval_scroll.pack(side="right", fill="y")
        self.approval_output.configure(yscrollcommand=approval_scroll.set)
        actions = tk.Frame(self.approval_frame, bg="#FFF8E8")
        actions.pack(fill="x")
        self.approve_button = ttk.Button(
            actions,
            text="승인하고 실행",
            takefocus=False,
            style="Primary.TButton",
            command=self._approve,
            state="disabled",
        )
        self.approve_button.pack(side="right")
        self.reject_button = ttk.Button(
            actions, text="거절", command=self._reject, state="disabled"
        )
        self.reject_button.pack(side="right", padx=8)

        bottom = tk.Frame(root, bg=bg)
        bottom.grid(row=2, column=0, sticky="ew", padx=24, pady=(12, 20))
        command_row = tk.Frame(bottom, bg=bg)
        command_row.pack(fill="x")
        self.entry = ttk.Entry(command_row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.run_button = ttk.Button(
            command_row, text="실행", style="Primary.TButton", command=self._execute
        )
        self.run_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(command_row, text="취소", command=self._cancel)
        self.input_hint = tk.StringVar(value="명령을 입력하세요 · Enter로 실행")
        tk.Label(
            bottom,
            textvariable=self.input_hint,
            bg=bg,
            fg=muted,
            font=(family, 10),
            anchor="w",
            wraplength=650,
        ).pack(fill="x", pady=(8, 4))
        self.undo_button = ttk.Button(bottom, text="최근 변경 되돌리기", command=self._undo)
        self.undo_button.pack(anchor="e")
        ttk.Button(bottom, text="결과 전체 복사", command=self._copy_output).pack(anchor="e")
        self.entry.bind("<Return>", self._enter)
        self.entry.bind("<<Paste>>", self._paste)
        self.entry.bind("<Control-v>", self._paste)
        if sys.platform == "darwin":
            self.entry.bind("<Command-v>", self._paste)
        self.entry_menu = tk.Menu(self.entry, tearoff=False)
        self.entry_menu.add_command(label="붙여넣기", command=self._paste)
        self.entry.bind("<Button-3>", self._show_entry_menu)
        if sys.platform == "darwin":
            self.entry.bind("<Button-2>", self._show_entry_menu)
            self.entry.bind("<Control-Button-1>", self._show_entry_menu)
        root.protocol("WM_DELETE_WINDOW", self._shutdown)
        self.entry.focus_set()

    def bind_presenter(self, presenter: UIPresenter) -> None:
        self.presenter = presenter

    def set_status(self, value: str) -> None:
        self._status = value
        labels = {
            "STARTING": "연결 준비 중",
            "READY": "준비됨",
            "PLANNING": "요청 분석 중",
            "EXECUTING_TOOL": "작업 실행 중",
            "WAITING_USER_INPUT": "답변이 필요해요",
            "WAITING_APPROVAL": "승인이 필요해요",
            "DONE": "완료",
            "ERROR": "오류 발생",
            "RECORDING": "녹음 중",
            "TRANSCRIBING": "음성 변환 중",
        }
        self.status_var.set("● " + labels.get(value, value))
        color = "#B42318" if value == "ERROR" else "#596273"
        if value == "WAITING_APPROVAL":
            color = "#805B10"
        self.status_label.configure(fg=color)
        if value in {"READY", "DONE", "ERROR"}:
            self.tool_var.set("")
        if value == "WAITING_USER_INPUT":
            self.input_hint.set("후보 번호를 입력하세요 · 취소를 입력하면 요청을 취소합니다")
            self.entry.focus_set()
        elif value == "WAITING_APPROVAL":
            self.input_hint.set("대상과 미리보기를 확인한 뒤 승인 또는 거절을 선택하세요")
        elif value in {"PLANNING", "EXECUTING_TOOL"}:
            self.input_hint.set("취소하면 연결을 정리하고 창을 닫습니다")
        else:
            self.input_hint.set("명령을 입력하세요 · Enter로 실행")

    def set_tool(self, value: str) -> None:
        self.tool_var.set("현재 도구 · " + value if value != "없음" else "")

    def set_controls(
        self,
        *,
        run_enabled: bool,
        cancel_enabled: bool,
        approval_visible: bool,
        undo_enabled: bool,
    ) -> None:
        self.run_button.configure(state="normal" if run_enabled else "disabled")
        self.cancel_button.configure(state="normal" if cancel_enabled else "disabled")
        self.undo_button.configure(state="normal" if undo_enabled else "disabled")
        for button in self.example_buttons:
            button.configure(
                state="normal" if run_enabled and self._status == "READY" else "disabled"
            )
        if cancel_enabled and not approval_visible:
            self.cancel_button.pack(side="left", padx=(8, 0))
        else:
            self.cancel_button.pack_forget()
        if approval_visible:
            self.approval_frame.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        else:
            self.approval_frame.grid_remove()
            self._approval_ready = False
        self.set_approval_enabled(False)

    def set_approval_enabled(self, enabled: bool) -> None:
        self._approval_enabled = enabled
        state = "normal" if enabled and self._approval_ready else "disabled"
        self.approve_button.configure(state=state)
        self.reject_button.configure(state=state)

    def _append(self, title: str, body: str, tag: str) -> None:
        if not self._has_output:
            self.welcome.grid_remove()
            self._has_output = True
        at_bottom = self.output.yview()[1] >= 0.99
        self.output.configure(state="normal")
        self.output.insert("end", title + "\n", tag + "_title")
        self._output_original.append(title + "\n" + body)
        for block in output_blocks(body) if tag == "result" else (body,):
            if isinstance(block, str):
                self.output.insert("end", block, tag)
            else:
                self._insert_table(*block)
        self.output.insert("end", "\n\n", tag)
        self.output.configure(state="disabled")
        if at_bottom:
            self.output.see("end")

    def show_request(self, text: str) -> None:
        self._append("내 요청", text, "request")

    def show_output(self, value: str) -> None:
        self._append("안내", value, "notice")

    def show_outcome(self, outcome: RuntimeResult) -> None:
        if isinstance(outcome, ApprovalRequired):
            operation = {
                "create": "새 파일 저장",
                "overwrite": "기존 파일 덮어쓰기",
                "delete": "파일 삭제",
            }.get(outcome.operation, outcome.operation)
            self.approval_title.set("승인 필요 · " + operation)
            if outcome.operation == "delete":
                import json

                targets = "\n".join(
                    json.dumps(path, ensure_ascii=False) for path in outcome.targets
                )
                body = (
                    f"삭제 대상: {len(outcome.targets)}개 파일\n{targets}\n\n"
                    "승인하면 백업 후 삭제합니다. 최근 변경 되돌리기로 일괄 복원할 수 있습니다."
                )
                button_text, button_style = "승인하고 삭제", "Danger.TButton"
            else:
                preview = outcome.preview[:200]
                truncated = outcome.preview_truncated or len(outcome.preview) > 200
                warning = (
                    "기존 파일 내용을 덮어씁니다.\n" if outcome.operation == "overwrite" else ""
                )
                body = (
                    f"대상: {outcome.target_path}\n{warning}내용: {outcome.content_chars}자\n"
                    f"\n미리보기\n{preview}" + ("\n일부 내용만 표시됩니다." if truncated else "")
                )
                button_text = (
                    "승인하고 덮어쓰기" if outcome.operation == "overwrite" else "승인하고 저장"
                )
                button_style = "Primary.TButton"
            self.approval_output.configure(state="normal")
            self.approval_output.delete("1.0", "end")
            self.approval_output.insert("end", body)
            self.approval_output.configure(state="disabled")
            self.approval_output.yview_moveto(0)
            self.approve_button.configure(text=button_text, style=button_style)
            self._approval_ready = True
            self._append("승인 요청 · " + operation, body, "question")
        elif isinstance(outcome, Clarification):
            options = "\n".join(f"{i}. {option}" for i, option in enumerate(outcome.options, 1))
            self._append("선택이 필요해요", outcome.question + "\n" + options, "question")
        elif isinstance(outcome, Completed):
            self._append("결과", outcome.text, "result")
        elif isinstance(outcome, Error):
            self._append("오류 발생", outcome.message, "error")
        elif isinstance(outcome, Busy):
            self._append("작업 중", outcome.message, "notice")

    def _insert_table(self, headers, rows, original) -> None:
        from tkinter import ttk

        frame = ttk.Frame(self.output)
        columns = tuple(str(index) for index in range(len(headers)))
        table = ttk.Treeview(
            frame, columns=columns, show="headings", height=min(10, max(1, len(rows)))
        )
        for column, header in zip(columns, headers, strict=True):
            table.heading(column, text=header)
            table.column(column, width=160, minwidth=80, stretch=False)
        for row in rows:
            table.insert("", "end", values=row)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=table.xview)
        table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.configure(width=max(100, self.output.winfo_width() - 20))
        self.output.bind(
            "<Configure>",
            lambda event: frame.configure(width=max(100, event.width - 20)),
            add="+",
        )
        frame.grid_propagate(False)
        frame.configure(height=table.winfo_reqheight() + horizontal.winfo_reqheight())
        for shortcut in ("<Control-c>", "<Command-c>"):
            table.bind(shortcut, lambda event, text=original: self._copy_text(text))
        self.output.window_create(self._tk.END, window=frame)
        self.output.insert(self._tk.END, "\n")

    def _copy_text(self, text) -> str:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        return "break"

    def _copy_output(self, event=None) -> str:
        if event is not None and self.output.tag_ranges("sel"):
            return self._copy_text(self.output.get("sel.first", "sel.last"))
        return self._copy_text("\n".join(self._output_original))

    def _fill_example(self, text: str) -> None:
        if self.presenter is not None and self.presenter.state is RuntimeState.IDLE:
            self.entry.delete(0, "end")
            self.entry.insert(0, text)
            self.entry.focus_set()

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

    def _paste(self, _event=None) -> str:
        if self.entry.instate(("disabled",)) or self.entry.instate(("readonly",)):
            return "break"
        try:
            text = self.entry.clipboard_get()
        except self._tk.TclError:
            return "break"
        if text:
            if self.entry.selection_present():
                start = self.entry.index("sel.first")
                self.entry.delete(start, "sel.last")
                self.entry.icursor(start)
            self.entry.insert("insert", text)
            self.entry.focus_set()
        return "break"

    def _show_entry_menu(self, event) -> str:
        self.entry.focus_set()
        try:
            self.entry_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.entry_menu.grab_release()
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
