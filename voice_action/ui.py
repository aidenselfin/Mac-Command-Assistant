"""Minimal Tkinter UI over the existing runtime controller."""

import asyncio
import os
import queue
import re
import sys
import threading
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Protocol

from openai import AsyncOpenAI

from voice_action.feedback import LOW_RATING, build_feedback
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
    PlannerError,
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

    def set_choices(self, labels: tuple[str, ...]) -> None: ...

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
                if not isinstance(outcome, Busy):
                    self._choices = (
                        outcome.shown_options if isinstance(outcome, Clarification) else ()
                    )
                    self._rating_open = isinstance(outcome, Completed)
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
            self.view.show_request(shown or normalized)
            return True
        self._dispatch_pending = False
        self._refresh_controls()
        return False

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

    def rate(self, score: int, tags: tuple[str, ...]) -> bool:
        controller = self._controller
        return controller is not None and self._schedule(controller.rate(score, tags))

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
                async with self._client_factory(self.config) as client:
                    tools = await manager.list_tools()
                    model = self._model_factory(client, self.config.model)
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


MarkdownSegment = tuple[str, tuple[str, ...]]
MARKDOWN_INLINE = re.compile(
    r"`([^`\n]+)`"  # code
    r"|\*\*(?=\S)(.+?)(?<=\S)\*\*"  # bold
    r"|\*(?=\S)([^*\n]+?)(?<=\S)\*"  # italic; "2 * 3 * 4" has spaces, so it stays as typed
    r"|\[([^\]\n]+)\]\((\S+?)\)"  # link
)


def _markdown_inline(text: str, tags: tuple[str, ...]) -> list[MarkdownSegment]:
    segments, position = [], 0
    for match in MARKDOWN_INLINE.finditer(text):
        segments.append((text[position:match.start()], tags))
        code, bold, italic, label, url = match.groups()
        if code is not None:
            segments.append((code, (*tags, "md_code")))
        elif bold is not None:
            segments.extend(_markdown_inline(bold, (*tags, "md_bold")))
        elif italic is not None:
            segments.append((italic, (*tags, "md_italic")))
        else:
            segments.append((f"{label} ({url})", tags))
        position = match.end()
    segments.append((text[position:], tags))
    return [segment for segment in segments if segment[0]]


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _markdown_table(lines: list[str]) -> list[MarkdownSegment]:
    rows = [
        [
            "".join(t for t, _ in _markdown_inline(cell.strip(), ()))
            for cell in line.strip().strip("|").split("|")
        ]
        for line in lines
        if not re.fullmatch(r"[\s|:-]+", line)  # the |---|---| separator row
    ]
    if not rows:
        return []
    widths = [
        max(_display_width(row[c]) if c < len(row) else 0 for row in rows)
        for c in range(max(len(row) for row in rows))
    ]
    segments = []
    for index, row in enumerate(rows):
        cells = [*row, *[""] * (len(widths) - len(row))]
        line = "  ".join(
            cell + " " * (width - _display_width(cell))
            for cell, width in zip(cells, widths, strict=True)
        )
        tags = ("md_table", "md_bold") if index == 0 else ("md_table",)
        segments.append((line.rstrip() + "\n", tags))
    return segments


def render_markdown(text: str) -> list[MarkdownSegment]:
    """Model replies as (text, tags): markdown symbols become Tk text styles, not characters.

    Covers what LLM replies use: headings, bold/italic, inline and fenced code, lists, links,
    tables, quotes and rules. Anything else is shown as written.
    """
    lines, segments, index, in_code = text.split("\n"), [], 0, False
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            segments.append((line + "\n", ("md_code",)))
            continue
        if line.lstrip().startswith("|"):
            block = [line]
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                block.append(lines[index])
                index += 1
            segments.extend(_markdown_table(block))
            continue
        if re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
            segments.append(("────────", ()))
        elif heading := re.match(r"\s*#{1,6}\s+(.*)", line):
            segments.extend(_markdown_inline(heading.group(1), ("md_heading",)))
        elif item := re.match(r"(\s*)[-*+]\s+(.*)", line):
            segments.append(("  " * (len(item.group(1)) // 2) + "• ", ()))
            segments.extend(_markdown_inline(item.group(2), ()))
        elif quote := re.match(r"\s*>\s?(.*)", line):
            segments.extend(_markdown_inline(quote.group(1), ()))
        else:
            segments.extend(_markdown_inline(line, ()))
        segments.append(("\n", ()))
    while segments and segments[-1][0] == "\n":
        segments.pop()
    return segments


TK_IME_COMMIT_KEY = 0x80  # TkAqua keycode high byte for text committed by an input method
SHIFT_STATE = 0x1


def is_lost_hangul_keystroke(char: str, keycode: int, composing: bool) -> bool:
    """Apple's Korean IME commits the first key after focus or a 한/영 switch as a lone jamo.

    A healthy IME sends Hangul keys as marked text (composing) or as commits (0x80 keycode),
    so a lone compatibility jamo (U+3131-U+318E) from a real key outside composition is the bug.
    """
    return (
        not composing
        and len(char) == 1
        and "\u3131" <= char <= "\u318e"
        and (keycode >> 24) & 0xFF != TK_IME_COMMIT_KEY
    )


def fix_hangul_first_keystroke(widget) -> None:
    """Replay the lost first keystroke so Apple's Korean IME composes it (macOS only).

    Same idea as cmux PR #11806, which re-interprets the key inside keyDown. Tk runs bindings
    after keyDown, where re-interpreting still commits a lone jamo, so the key is posted to this
    process again and goes through Tk's normal keyDown path once the IME is ready.
    """
    # The keycode layout (virtual key << 24 | char) used below is TkAqua 9's.
    if float(widget.tk.call("info", "tclversion")) < 9.0:
        return
    try:
        import Quartz
    except ImportError:
        return
    composing = False
    replayed = False

    def set_composing(value: bool) -> None:
        nonlocal composing
        composing = value

    def on_key(event) -> str | None:
        nonlocal replayed
        if replayed or not is_lost_hangul_keystroke(event.char, event.keycode, composing):
            replayed = False  # one replay per key: a second loss is inserted as typed
            return None
        # Without permission the post is dropped silently; a split syllable beats a lost key.
        if not Quartz.CGPreflightPostEventAccess():
            return None
        replayed = True
        # ponytail: the replay is queued behind any key already pending, so a key typed within
        # a few ms of the first one could land before it; never seen at human typing speed.
        pid = os.getpid()
        for down in (True, False):
            key = Quartz.CGEventCreateKeyboardEvent(None, (event.keycode >> 24) & 0xFF, down)
            if event.state & SHIFT_STATE:
                Quartz.CGEventSetFlags(key, Quartz.kCGEventFlagMaskShift)
            Quartz.CGEventPostToPid(pid, key)
        return "break"

    widget.bind("<<TkStartIMEMarkedText>>", lambda _e: set_composing(True), add="+")
    widget.bind("<<TkEndIMEMarkedText>>", lambda _e: set_composing(False), add="+")
    widget.bind("<KeyPress>", on_key, add="+")


INK, MUTED, BORDER, CANVAS = "#20242C", "#596273", "#E5E7EB", "#FFFFFF"
BLUE, BLUE_SOFT = "#3659D9", "#EEF1FC"
AMBER, AMBER_SOFT, AMBER_LINE = "#805B10", "#FBF5E8", "#E6D3A7"
RED, RED_SOFT, RED_LINE = "#B42318", "#FDF0EF", "#EBB3AE"
GRAY_SOFT, SUCCESS = "#F2F3F5", "#1F7A4D"

# Status pill per runtime state: (label, text color, background, dot color).
STATUS_STYLE = {
    "STARTING": ("연결 준비 중", BLUE, BLUE_SOFT, BLUE),
    "READY": ("준비됨", INK, GRAY_SOFT, SUCCESS),
    "PLANNING": ("요청 분석 중", BLUE, BLUE_SOFT, BLUE),
    "EXECUTING_TOOL": ("작업 실행 중", BLUE, BLUE_SOFT, BLUE),
    "WAITING_USER_INPUT": ("답변이 필요해요", AMBER, AMBER_SOFT, AMBER),
    "WAITING_APPROVAL": ("승인이 필요해요", AMBER, AMBER_SOFT, AMBER),
    "DONE": ("완료", SUCCESS, GRAY_SOFT, SUCCESS),
    "ERROR": ("오류 발생", RED, RED_SOFT, RED),
    "RECORDING": ("녹음 중", BLUE, BLUE_SOFT, BLUE),
    "TRANSCRIBING": ("음성 변환 중", BLUE, BLUE_SOFT, BLUE),
}
DEFAULT_HINT = "명령을 입력하세요 · Enter로 실행"
STATUS_HINT = {
    "WAITING_USER_INPUT": "버튼·숫자 키로 고르거나 직접 입력하세요 · 취소 입력 시 요청 취소",
    "WAITING_APPROVAL": "대상과 미리보기를 확인한 뒤 승인 또는 거절을 선택하세요",
    "PLANNING": "취소하면 연결을 정리하고 창을 닫습니다",
    "EXECUTING_TOOL": "취소하면 연결을 정리하고 창을 닫습니다",
}
# Conversation cards: (title color, card background). Requests sit on the right.
CARD_STYLE = {
    "request": (BLUE, BLUE_SOFT),
    "result": (INK, GRAY_SOFT),
    "question": (AMBER, AMBER_SOFT),
    "error": (RED, RED_SOFT),
    "notice": (MUTED, GRAY_SOFT),
}
APPROVAL_STYLE = {
    "create": ("＋  승인 필요 · 새 파일 저장", "승인하고 저장", "Primary.TButton", False),
    "overwrite": (
        "⚠  승인 필요 · 기존 파일 덮어쓰기", "승인하고 덮어쓰기", "Primary.TButton", False
    ),
    "delete": ("🗑  승인 필요 · 파일 삭제", "🗑  승인하고 삭제", "Danger.TButton", True),
    "convert": ("⇄  승인 필요 · 파일 변환", "승인하고 변환", "Primary.TButton", False),
}
APPROVAL_PREVIEW_CHARS = 200
# Shown on the 1-5 rating buttons; the tags are what the feedback loop stores.
RATING_CAPTIONS = {1: "별로", 5: "최고"}
RATING_TAG_LABELS = (
    ("wrong_file", "엉뚱한 파일"),
    ("missing_content", "내용 부족"),
    ("wrong_content", "틀린 내용"),
    ("too_long", "너무 김"),
    ("too_slow", "너무 느림"),
    ("other", "기타"),
)
CARD_SIDE_GAP = 96  # the empty side of a card, like a chat bubble


class TkView:
    """Tk widgets; every method is called only by the Tk main thread.

    Layout follows the Voice Action redesign: header with a status pill, folder bar,
    tinted conversation cards, tool line, approval panel, input bar and footer.
    """

    def __init__(self, root, folders: tuple[Path, ...] = ()):
        import tkinter as tk
        from tkinter import font, ttk

        self.root = root
        self._tk = tk
        self._ttk = ttk
        self.presenter: UIPresenter | None = None
        self.closed = False
        self._approval_ready = False
        self._approval_enabled = False
        self._has_cards = False
        self._status = "STARTING"
        root.title("Voice Action")
        root.geometry("880x680")
        root.minsize(720, 520)
        root.configure(bg=CANVAS)
        family = font.nametofont("TkDefaultFont").actual("family")
        mono = font.nametofont("TkFixedFont").actual("family")
        self._family = family
        root.option_add("*Font", (family, 12))

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(
            "TButton", padding=(12, 7), foreground=INK, background=CANVAS, bordercolor=BORDER,
            lightcolor=CANVAS, darkcolor=CANVAS, focuscolor=BLUE_SOFT,
        )
        style.map(
            "TButton", background=[("active", GRAY_SOFT)], foreground=[("disabled", "#A0A6B1")]
        )
        for name, color, active in (
            ("Primary.TButton", BLUE, "#2847BC"),
            ("Danger.TButton", RED, "#912018"),
        ):
            style.configure(
                name, foreground="#FFFFFF", background=color, bordercolor=color,
                lightcolor=color, darkcolor=color, font=(family, 12, "bold"),
            )
            style.map(
                name,
                background=[("disabled", "#D9DEE7"), ("active", active)],
                bordercolor=[("disabled", "#D9DEE7")],
                lightcolor=[("disabled", "#D9DEE7")],
                darkcolor=[("disabled", "#D9DEE7")],
                foreground=[("disabled", MUTED)],
            )
        style.configure(
            "Cancel.TButton", foreground=RED, background=CANVAS, bordercolor=RED_LINE,
            lightcolor=CANVAS, darkcolor=CANVAS,
        )
        style.map("Cancel.TButton", background=[("active", RED_SOFT)])
        style.configure(
            "Ghost.TButton", foreground=MUTED, background=CANVAS, bordercolor=CANVAS,
            lightcolor=CANVAS, darkcolor=CANVAS, padding=(8, 4), font=(family, 11),
        )
        style.map(
            "Ghost.TButton",
            background=[("active", GRAY_SOFT)],
            bordercolor=[("active", GRAY_SOFT)],
            foreground=[("disabled", "#B5BAC3"), ("active", INK)],
        )
        style.configure(
            "Choice.TButton", anchor="w", padding=(12, 8), font=(family, 12),
        )
        style.configure(
            "Chip.TButton", padding=(8, 3), font=(family, 11),
        )
        style.configure(
            "Mic.TButton", foreground="#A0A6B1", background=CANVAS, bordercolor=BORDER,
            padding=(8, 7),
        )
        style.configure(
            "TEntry", padding=9, fieldbackground=CANVAS, bordercolor=BORDER,
            lightcolor=CANVAS, darkcolor=CANVAS,
        )
        style.map("TEntry", bordercolor=[("focus", BLUE)], lightcolor=[("focus", BLUE)])
        style.configure("Vertical.TScrollbar", background=GRAY_SOFT, troughcolor=CANVAS,
                        bordercolor=CANVAS, arrowcolor=MUTED)

        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(4, weight=1)

        def rule(row):
            tk.Frame(root, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")

        # ---- header: logo, name, status pill, quit --------------------------------------
        header = tk.Frame(root, bg=CANVAS, padx=20, pady=12)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            header, text="♪", bg=INK, fg="#FFFFFF", width=2, font=(family, 13, "bold")
        ).pack(side="left")
        tk.Label(
            header, text="Voice Action", bg=CANVAS, fg=INK, font=(family, 14, "bold")
        ).pack(side="left", padx=(8, 12))
        self.status_pill = tk.Frame(header, bg=BLUE_SOFT, padx=10, pady=3)
        self.status_pill.pack(side="left")
        self.status_dot = tk.Label(
            self.status_pill, text="●", bg=BLUE_SOFT, fg=BLUE, font=(family, 9)
        )
        self.status_dot.pack(side="left", padx=(0, 5))
        self.status_var = tk.StringVar(value="연결 준비 중")
        self.status_label = tk.Label(
            self.status_pill, textvariable=self.status_var, bg=BLUE_SOFT, fg=BLUE,
            font=(family, 11, "bold"),
        )
        self.status_label.pack(side="left")
        ttk.Button(header, text="⏻  종료", style="Ghost.TButton", command=self._shutdown).pack(
            side="right"
        )
        rule(1)

        # ---- folder bar: the folders the assistant may read and write ---------------------
        self.folder_text = "작업 폴더 · " + (
            ", ".join(str(folder) for folder in folders) or "설정되지 않음"
        )
        bar = tk.Frame(root, bg=GRAY_SOFT, padx=20, pady=7)
        bar.grid(row=2, column=0, sticky="ew")
        tk.Label(bar, text="📂  작업 폴더", bg=GRAY_SOFT, fg=MUTED, font=(family, 11, "bold")).pack(
            side="left", anchor="n"
        )
        tk.Label(
            bar,
            text=", ".join(str(folder) for folder in folders) or "설정되지 않음",
            bg=GRAY_SOFT,
            fg=INK,
            font=(mono, 11),
            anchor="w",
            justify="left",
            wraplength=640,
        ).pack(side="left", fill="x", expand=True, padx=(8, 0))
        rule(3)

        # ---- conversation -----------------------------------------------------------------
        log = tk.Frame(root, bg=CANVAS)
        log.grid(row=4, column=0, sticky="nsew")
        self.output = tk.Text(
            log,
            wrap="word",
            state="disabled",
            height=4,
            bg=CANVAS,
            fg=INK,
            bd=0,
            highlightthickness=0,
            padx=20,
            pady=14,
            selectbackground="#DCE5FF",
            cursor="arrow",
        )
        self.output.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log, command=self.output.yview)
        scrollbar.pack(side="right", fill="y")
        self.output.configure(yscrollcommand=scrollbar.set)
        for kind, (title_color, card) in CARD_STYLE.items():
            left = CARD_SIDE_GAP if kind == "request" else 0
            right = 0 if kind == "request" else CARD_SIDE_GAP
            margins = {
                "background": card,
                "lmargin1": left + 14,
                "lmargin2": left + 14,
                "rmargin": right + 14,
                # The margins inside the card share its color; the empty side stays canvas.
                "lmargincolor": card,
                "rmargincolor": card,
            }
            self.output.tag_configure(kind, foreground=INK, **margins)
            self.output.tag_configure(
                kind + "_title", foreground=title_color, font=(family, 11, "bold"), spacing1=10,
                **margins,
            )
            self.output.tag_configure(kind + "_end", font=(family, 5), **margins)
        self.output.tag_configure("gap", font=(family, 6))
        self.output.tag_configure(
            "empty", foreground=MUTED, justify="center", font=(family, 12), spacing1=160
        )
        # Markdown styles, configured after the card tags so their fonts win.
        self.output.tag_configure("md_bold", font=(family, 12, "bold"))
        self.output.tag_configure("md_italic", font=(family, 12, "italic"))
        self.output.tag_configure("md_code", font=(mono, 11), background="#E8EAEE")
        self.output.tag_configure("md_table", font=(mono, 11))
        self.output.tag_configure("md_heading", font=(family, 14, "bold"), spacing1=6)
        self._show_empty()

        # ---- current tool -------------------------------------------------------------------
        self.tool_var = tk.StringVar(value="")
        self.tool_line = tk.Frame(
            root, bg=CANVAS, padx=10, pady=4, highlightthickness=1, highlightbackground=BORDER
        )
        tk.Label(self.tool_line, text="⟳  🔧  현재 도구", bg=CANVAS, fg=MUTED,
                 font=(family, 11)).pack(side="left")
        tk.Label(self.tool_line, textvariable=self.tool_var, bg=CANVAS, fg=INK,
                 font=(mono, 11)).pack(side="left", padx=(8, 0))

        # ---- approval panel -------------------------------------------------------------
        self.approval_frame = tk.Frame(
            root, bg=AMBER_SOFT, highlightthickness=1, highlightbackground=AMBER_LINE
        )
        self.approval_title = tk.StringVar()
        self._approval_title_label = tk.Label(
            self.approval_frame, textvariable=self.approval_title, bg=AMBER_SOFT, fg=AMBER,
            font=(family, 12, "bold"), anchor="w",
        )
        self._approval_title_label.pack(fill="x", padx=14, pady=(10, 6))
        details = tk.Frame(self.approval_frame, bg=CANVAS, highlightthickness=1,
                           highlightbackground=BORDER)
        details.pack(fill="both", expand=True, padx=14)
        self.approval_output = tk.Text(
            details,
            height=6,
            wrap="word",
            state="disabled",
            bg=CANVAS,
            fg=INK,
            bd=0,
            highlightthickness=0,
            padx=10,
            pady=6,
            font=(mono, 11),
        )
        self.approval_output.pack(side="left", fill="both", expand=True)
        approval_scroll = ttk.Scrollbar(details, command=self.approval_output.yview)
        approval_scroll.pack(side="right", fill="y")
        self.approval_output.configure(yscrollcommand=approval_scroll.set)
        self.approval_output.tag_configure("label", foreground=MUTED, font=(family, 11))
        self.approval_output.tag_configure("warn", foreground=AMBER, font=(family, 11, "bold"))
        self.approval_output.tag_configure("note", foreground=MUTED, font=(family, 11))
        self.approval_output.tag_configure("count", font=(family, 12, "bold"))
        self._approval_bg = [self.approval_frame, self._approval_title_label]
        actions = tk.Frame(self.approval_frame, bg=AMBER_SOFT)
        actions.pack(fill="x", padx=14, pady=10)
        self._approval_bg.append(actions)
        # Approve must never take keyboard focus, so Enter or Space can't approve by accident.
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

        # ---- question buttons and answer rating (never visible together with approval) ----
        self.choice_frame = tk.Frame(root, bg=CANVAS)
        self._choices_shown: tuple[str, ...] = ()
        self.rating_frame = tk.Frame(root, bg=CANVAS)
        rating_row = tk.Frame(self.rating_frame, bg=CANVAS)
        rating_row.pack(fill="x")
        tk.Label(
            rating_row, text="이 답변은 어땠나요?", bg=CANVAS, fg=MUTED, font=(family, 11)
        ).pack(side="left", padx=(0, 10))
        self.rating_buttons = {}
        for score in range(1, 6):
            caption = RATING_CAPTIONS.get(score)
            button = ttk.Button(
                rating_row, text=f"{score}" + (f" {caption}" if caption else ""),
                style="Chip.TButton", takefocus=False, width=6 if caption else 3,
                command=lambda score=score: self._rate(score),
            )
            button.pack(side="left", padx=2)
            self.rating_buttons[score] = button
        tk.Label(
            rating_row, text="숫자 키 1~5", bg=CANVAS, fg=MUTED, font=(family, 11)
        ).pack(side="left", padx=(10, 0))
        self.tag_row = tk.Frame(self.rating_frame, bg=CANVAS)
        tk.Label(
            self.tag_row, text="아쉬운 점", bg=CANVAS, fg=MUTED, font=(family, 11)
        ).pack(side="left", padx=(0, 10))
        self.tag_buttons = {}
        for tag, label in RATING_TAG_LABELS:
            button = ttk.Button(
                self.tag_row, text=label, style="Chip.TButton", takefocus=False,
                command=lambda tag=tag: self._toggle_tag(tag),
            )
            button.pack(side="left", padx=2)
            self.tag_buttons[tag] = button
        self._rating_visible = False

        # ---- input bar --------------------------------------------------------------------
        rule(7)
        command_row = tk.Frame(root, bg=CANVAS, padx=20, pady=10)
        command_row.grid(row=8, column=0, sticky="ew")
        ttk.Button(
            command_row, text="🎙", style="Mic.TButton", state="disabled", takefocus=False,
            width=3,
        ).pack(side="left", padx=(0, 8))
        self.entry = ttk.Entry(command_row, font=(family, 12))
        self.entry.pack(side="left", fill="x", expand=True)
        self.run_button = ttk.Button(
            command_row, text="↵  실행", style="Primary.TButton", command=self._execute
        )
        self.run_button.pack(side="right", padx=(8, 0))
        self.cancel_button = ttk.Button(
            command_row, text="✕  취소", style="Cancel.TButton", command=self._cancel
        )
        self._command_row = command_row

        # ---- footer: hint and undo ----------------------------------------------------------
        footer = tk.Frame(root, bg=CANVAS, padx=20)
        footer.grid(row=9, column=0, sticky="ew", pady=(0, 10))
        self.input_hint = tk.StringVar(value=DEFAULT_HINT)
        self.hint_label = tk.Label(
            footer, textvariable=self.input_hint, bg=CANVAS, fg=MUTED, font=(family, 11),
            anchor="w",
        )
        self.hint_label.pack(side="left", fill="x", expand=True)
        self.undo_button = ttk.Button(
            footer, text="↺  최근 변경 되돌리기", style="Ghost.TButton", command=self._undo
        )
        self.undo_button.pack(side="right")

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
            fix_hangul_first_keystroke(self.entry)
        # Digits answer a question or rate an answer, but only while the box is empty, so
        # typing a number into a command is never taken.
        self.entry.bind("<KeyPress>", self._on_digit, add="+")
        root.bind("<KeyPress>", self._on_digit, add="+")
        root.protocol("WM_DELETE_WINDOW", self._shutdown)
        self.entry.focus_set()

    def bind_presenter(self, presenter: UIPresenter) -> None:
        self.presenter = presenter

    def set_status(self, value: str) -> None:
        self._status = value
        label, color, background, dot = STATUS_STYLE.get(value, (value, MUTED, GRAY_SOFT, MUTED))
        self.status_var.set(label)
        self.status_pill.configure(bg=background)
        self.status_dot.configure(bg=background, fg=dot)
        self.status_label.configure(bg=background, fg=color)
        if value in {"READY", "DONE", "ERROR"}:
            self.set_tool("없음")
        hint = STATUS_HINT.get(value, DEFAULT_HINT)
        self.input_hint.set(hint)
        # Cancelling a running command closes the window, so that hint is shown as a warning.
        warn = value in {"PLANNING", "EXECUTING_TOOL"}
        self.hint_label.configure(
            fg=RED if warn else MUTED, font=(self._family, 11, "bold" if warn else "normal")
        )
        if value == "WAITING_USER_INPUT":
            self.entry.focus_set()

    def set_tool(self, value: str) -> None:
        if value == "없음":
            self.tool_var.set("")
            self.tool_line.grid_remove()
        else:
            self.tool_var.set(value)
            self.tool_line.grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 8))

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
        if cancel_enabled and not approval_visible:
            self.cancel_button.pack(side="right", padx=(8, 0), before=self.run_button)
        else:
            self.cancel_button.pack_forget()
        if approval_visible:
            self.approval_frame.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 10))
        else:
            self.approval_frame.grid_remove()
            self._approval_ready = False
        self.set_approval_enabled(False)

    def set_approval_enabled(self, enabled: bool) -> None:
        self._approval_enabled = enabled
        state = "normal" if enabled and self._approval_ready else "disabled"
        self.approve_button.configure(state=state)
        self.reject_button.configure(state=state)

    def set_choices(self, labels: tuple[str, ...]) -> None:
        if labels == self._choices_shown:
            return
        self._choices_shown = labels
        for child in self.choice_frame.winfo_children():
            child.destroy()
        if not labels:
            self.choice_frame.grid_remove()
            return
        for number, label in enumerate(labels, 1):
            self._ttk.Button(
                self.choice_frame, text=f"{number}   {label}", style="Choice.TButton",
                takefocus=False, command=lambda number=number: self._choose(number),
            ).pack(fill="x", pady=2)
        self.choice_frame.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 10))

    def set_rating(
        self, visible: bool, score: int | None = None, tags: tuple[str, ...] = ()
    ) -> None:
        self._rating_visible = visible
        if not visible:
            self.rating_frame.grid_remove()
            return
        self.rating_frame.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 10))
        for number, button in self.rating_buttons.items():
            button.configure(style="Primary.TButton" if number == score else "Chip.TButton")
        if score is not None and score <= LOW_RATING:
            self.tag_row.pack(fill="x", pady=(6, 0))
        else:
            self.tag_row.pack_forget()
        for tag, button in self.tag_buttons.items():
            button.configure(style="Primary.TButton" if tag in tags else "Chip.TButton")

    def _show_empty(self) -> None:
        self.output.configure(state="normal")
        self.output.insert(
            "end", "아직 대화 기록이 없어요. 아래 입력창에 명령을 입력해 보세요.", "empty"
        )
        self.output.configure(state="disabled")

    def _append(self, title: str, body: str, tag: str, *, markdown: bool = False) -> None:
        at_bottom = self.output.yview()[1] >= 0.99
        self.output.configure(state="normal")
        if not self._has_cards:
            self._has_cards = True
            self.output.delete("1.0", "end")
        else:
            self.output.insert("end", "\n", "gap")
        self.output.insert("end", title + "\n", (tag, tag + "_title"))
        # Only model-written text is markdown; requests, errors and previews stay verbatim.
        for text, styles in render_markdown(body) if markdown else [(body, ())]:
            self.output.insert("end", text, (tag, *styles))
        self.output.insert("end", "\n", tag)
        self.output.insert("end", "\n", (tag, tag + "_end"))
        self.output.configure(state="disabled")
        if at_bottom:
            self.output.see("end")

    def show_request(self, text: str) -> None:
        self._append("내 요청", text, "request")

    def show_output(self, value: str) -> None:
        self._append("안내", value, "notice")

    def _approval_details(self, outcome: ApprovalRequired) -> list[tuple[str, tuple[str, ...]]]:
        """Panel text as (text, tags). Paths and the preview are shown exactly as written."""
        if outcome.operation == "delete":
            width = len(str(len(outcome.targets)))
            return [
                ("삭제 대상 ", ("label",)),
                (f"{len(outcome.targets)}개\n", ("count",)),
                *[
                    (f"{index:>{width}}  {display_path(path)}\n", ())
                    for index, path in enumerate(outcome.targets, 1)
                ],
                (
                    "삭제 전에 백업을 만들어요. 승인 후에도 하단의 최근 변경 되돌리기로 "
                    "복구할 수 있어요.",
                    ("note",),
                ),
            ]
        if outcome.operation == "convert":
            segments = [
                ("원본  ", ("label",)),
                (f"{display_path(outcome.targets[0]) if outcome.targets else ''}\n", ()),
                ("출력  ", ("label",)),
                (f"{display_path(outcome.target_path)}\n", ()),
                ("크기  ", ("label",)),
                (f"{outcome.content_chars:,} bytes\n", ()),
                ("원본은 그대로 두고 새 파일을 만듭니다.\n", ("note",)),
            ]
            if outcome.preview:
                more = " · 앞부분만 표시됩니다" if outcome.preview_truncated else ""
                segments += [(f"\n미리보기{more}\n", ("label",)), (outcome.preview, ())]
            else:
                segments.append(("\n이미지·PDF 등은 미리보기가 없습니다.", ("note",)))
            return segments
        preview = outcome.preview[:APPROVAL_PREVIEW_CHARS]
        truncated = outcome.preview_truncated or len(outcome.preview) > APPROVAL_PREVIEW_CHARS
        segments = [
            ("대상  ", ("label",)),
            (f"{display_path(outcome.target_path)}\n", ()),
            ("내용  ", ("label",)),
            (f"{outcome.content_chars}자\n", ()),
        ]
        if outcome.operation == "overwrite":
            segments.append(("⚠ 기존 파일 내용을 덮어씁니다.\n", ("warn",)))
        note = f" · 앞 {APPROVAL_PREVIEW_CHARS}자, 일부 내용만 표시됩니다" if truncated else ""
        segments += [(f"\n미리보기{note}\n", ("label",)), (preview, ())]
        return segments

    def show_outcome(self, outcome: RuntimeResult) -> None:
        if isinstance(outcome, ApprovalRequired):
            title, button_text, button_style, danger = APPROVAL_STYLE.get(
                outcome.operation,
                (f"승인 필요 · {outcome.operation}", "승인하고 실행", "Primary.TButton", False),
            )
            background, line, accent = (
                (RED_SOFT, RED_LINE, RED) if danger else (AMBER_SOFT, AMBER_LINE, AMBER)
            )
            for widget in self._approval_bg:
                widget.configure(bg=background)
            self.approval_frame.configure(highlightbackground=line)
            self._approval_title_label.configure(fg=accent)
            self.approval_title.set(title)
            segments = self._approval_details(outcome)
            self.approval_output.configure(state="normal")
            self.approval_output.delete("1.0", "end")
            for text, tags in segments:
                self.approval_output.insert("end", text, tags)
            self.approval_output.configure(state="disabled")
            self.approval_output.yview_moveto(0)
            self.approve_button.configure(text=button_text, style=button_style)
            self._approval_ready = True
            operation = title.split("· ", 1)[-1]
            self._append(
                "승인 요청 · " + operation, "".join(text for text, _ in segments), "question"
            )
        elif isinstance(outcome, Clarification):
            options = "\n".join(
                f"{i}. {option}" for i, option in enumerate(outcome.shown_options, 1)
            )
            self._append(
                "선택이 필요해요", outcome.question + "\n" + options, "question", markdown=True
            )
        elif isinstance(outcome, Completed):
            self._append("결과", outcome.text, "result", markdown=True)
        elif isinstance(outcome, Error):
            self._append("오류 발생", outcome.message, "error")
        elif isinstance(outcome, Busy):
            self._append("작업 중", outcome.message, "notice")

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

    def _on_digit(self, event) -> str | None:
        if self.presenter is None or event.char not in {"1", "2", "3", "4", "5"}:
            return None
        if self.entry.get():
            return None
        number = int(event.char)
        if self._choices_shown:
            acted = self.presenter.choose(number)
        elif self._rating_visible:
            acted = self.presenter.rate(number)
        else:
            return None
        return "break" if acted else None

    def _choose(self, number: int) -> None:
        if self.presenter is not None:
            self.presenter.choose(number)

    def _rate(self, score: int) -> None:
        if self.presenter is not None:
            self.presenter.rate(score)

    def _toggle_tag(self, tag: str) -> None:
        if self.presenter is not None:
            self.presenter.toggle_tag(tag)

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
    view = TkView(root, config.allowed_directories)
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
