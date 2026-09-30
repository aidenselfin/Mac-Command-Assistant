import asyncio
import queue
import threading
from contextlib import asynccontextmanager

import pytest

import voice_action.ui as ui_module
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    ClarificationContext,
    Completed,
    PlannerEvent,
    PlannerEventType,
)
from voice_action.runtime import RuntimeState
from voice_action.ui import (
    UI_STATE_LABELS,
    AsyncRuntimeWorker,
    UIMessage,
    UIMessageType,
    UIPresenter,
)


class FakeView:
    def __init__(self):
        self.statuses = []
        self.tools = []
        self.controls = []
        self.outputs = []
        self.close_count = 0
        self.requests = []
        self.approval_enabled = []

    def set_status(self, value):
        self.statuses.append(value)

    def set_tool(self, value):
        self.tools.append(value)

    def set_controls(self, **values):
        self.controls.append(values)

    def show_output(self, value):
        self.outputs.append(value)

    def show_outcome(self, outcome):
        self.show_output(ui_module.render_outcome(outcome))

    def show_request(self, text):
        self.requests.append(text)

    def set_approval_enabled(self, enabled):
        self.approval_enabled.append(enabled)

    def close(self):
        self.close_count += 1


class FakeAdapter:
    def __init__(self):
        self.submissions = []
        self.approvals = 0
        self.rejections = 0
        self.undos = 0
        self.cancellations = 0
        self.shutdowns = 0

    def submit(self, text):
        self.submissions.append(text)
        return True

    def approve_pending(self):
        self.approvals += 1
        return True

    def reject_pending(self):
        self.rejections += 1
        return True

    def undo_last_write(self):
        self.undos += 1
        return True

    def cancel_active(self):
        self.cancellations += 1
        return True

    def shutdown(self):
        self.shutdowns += 1


def presenter_at(state):
    adapter, view = FakeAdapter(), FakeView()
    presenter = UIPresenter(adapter, view)
    presenter.handle(UIMessage(UIMessageType.STATE, state))
    return presenter, adapter, view


def test_tk_entry_pastes_from_shortcut_and_context_menu():
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    try:
        view = ui_module.TkView(root)
        root.update()
        view.entry.focus_force()
        root.update()
        root.clipboard_clear()
        root.clipboard_append("한글 붙여넣기")
        key = "<Command-v>" if ui_module.sys.platform == "darwin" else "<Control-v>"
        view.entry.event_generate(key)
        root.update()
        assert view.entry.get() == "한글 붙여넣기"
        view.entry.delete(0, tk.END)
        view.entry_menu.invoke(0)
        assert view.entry.get() == "한글 붙여넣기"
        view.entry.selection_range(0, tk.END)
        view.entry_menu.invoke(0)
        assert view.entry.get() == "한글 붙여넣기"
        assert view.entry.bind("<Button-3>")
        if ui_module.sys.platform == "darwin":
            assert view.entry.bind("<Button-2>")
            assert view.entry.bind("<Control-Button-1>")
    finally:
        root.destroy()


def test_manager_factory_registers_macos_only_on_darwin(config, monkeypatch):
    created = []
    filesystem, web, macos = object(), object(), object()

    monkeypatch.setattr(ui_module, "MCPClient", lambda *_args, **_kwargs: filesystem)
    monkeypatch.setattr(ui_module, "PlaywrightMCPClient", lambda _config: web)
    monkeypatch.setattr(
        ui_module,
        "MacOSAXClient",
        lambda: (created.append("macos"), macos)[1],
    )
    monkeypatch.setattr(ui_module, "MCPManager", lambda clients: clients)

    monkeypatch.setattr(ui_module.sys, "platform", "linux")
    assert ui_module._manager_factory(config) == {
        "filesystem": filesystem,
        "web": web,
    }
    assert created == []

    monkeypatch.setattr(ui_module.sys, "platform", "darwin")
    assert ui_module._manager_factory(config) == {
        "filesystem": filesystem,
        "web": web,
        "macos": macos,
    }
    assert created == ["macos"]


def test_runtime_state_mapping_is_exact():
    assert UI_STATE_LABELS[RuntimeState.IDLE] == "READY"
    assert UI_STATE_LABELS[RuntimeState.PLANNING] == "PLANNING"
    assert UI_STATE_LABELS[RuntimeState.EXECUTING_TOOL] == "EXECUTING_TOOL"
    assert UI_STATE_LABELS[RuntimeState.WAITING_USER_INPUT] == "WAITING_USER_INPUT"
    assert UI_STATE_LABELS[RuntimeState.WAITING_APPROVAL] == "WAITING_APPROVAL"
    assert UI_STATE_LABELS[RuntimeState.DONE] == "DONE"
    assert UI_STATE_LABELS[RuntimeState.ERROR] == "ERROR"
    assert UI_STATE_LABELS[RuntimeState.RESUMING_PLAN] == "PLANNING"


def test_namespaced_tool_started_event_is_displayed_without_log_parsing():
    presenter, _adapter, view = presenter_at(RuntimeState.PLANNING)

    presenter.handle(
        UIMessage(
            UIMessageType.PLANNER_EVENT,
            PlannerEvent(
                PlannerEventType.TOOL_STARTED,
                1,
                "filesystem__read_text_file",
            ),
        )
    )

    assert view.tools[-1] == "filesystem__read_text_file"


def test_ready_executes_once_and_blocks_second_request_before_state_update():
    presenter, adapter, _view = presenter_at(RuntimeState.IDLE)

    assert presenter.execute("  파일 목록  ")
    assert not presenter.execute("두 번째")

    assert adapter.submissions == ["파일 목록"]


def test_clarification_uses_existing_submit_path():
    presenter, adapter, _view = presenter_at(RuntimeState.WAITING_USER_INPUT)

    assert presenter.execute("2번")

    assert adapter.submissions == ["2번"]


def test_approval_display_is_bounded_and_has_no_opaque_context():
    presenter, _adapter, view = presenter_at(RuntimeState.WAITING_APPROVAL)
    full_content = "x" * 200 + "SECRET-TAIL"
    outcome = ApprovalRequired(
        "filesystem__write_file",
        "/allowed/summary.txt",
        "overwrite",
        len(full_content),
        full_content[:200],
        True,
        {"digest": "DIGEST-SECRET", "nonce": "NONCE-SECRET"},
    )

    presenter.handle(UIMessage(UIMessageType.OUTCOME, outcome))

    rendered = view.outputs[-1]
    assert "[APPROVAL REQUIRED]" in rendered
    assert "filesystem__write_file" in rendered
    assert "/allowed/summary.txt" in rendered
    assert "overwrite" in rendered
    assert "x" * 200 in rendered
    assert "SECRET-TAIL" not in rendered
    assert "DIGEST-SECRET" not in rendered
    assert "NONCE-SECRET" not in rendered


def test_approval_buttons_call_only_typed_pending_apis():
    approve_presenter, approve_adapter, _view = presenter_at(
        RuntimeState.WAITING_APPROVAL
    )
    reject_presenter, reject_adapter, _view = presenter_at(
        RuntimeState.WAITING_APPROVAL
    )

    assert approve_presenter.approve()
    assert reject_presenter.reject()

    assert approve_adapter.approvals == 1
    assert approve_adapter.rejections == 0
    assert approve_adapter.submissions == []
    assert reject_adapter.rejections == 1
    assert reject_adapter.approvals == 0
    assert reject_adapter.submissions == []


def test_waiting_approval_blocks_general_commands_and_cancel_rejects():
    presenter, adapter, _view = presenter_at(RuntimeState.WAITING_APPROVAL)

    assert not presenter.execute("새 명령")
    assert presenter.cancel()

    assert adapter.submissions == []
    assert adapter.rejections == 1
    assert adapter.cancellations == 0


def test_active_cancel_and_window_close_use_orderly_adapter_paths():
    presenter, adapter, view = presenter_at(RuntimeState.EXECUTING_TOOL)

    assert presenter.cancel()
    presenter.shutdown()

    assert adapter.cancellations == 1
    assert adapter.shutdowns == 1
    assert view.close_count == 1


def test_undo_button_calls_only_typed_api_and_disables_duplicate_click():
    presenter, adapter, view = presenter_at(RuntimeState.IDLE)

    assert view.controls[-1]["undo_enabled"] is True
    assert presenter.undo()
    assert not presenter.undo()

    assert adapter.undos == 1
    assert adapter.submissions == []
    assert adapter.approvals == 0
    assert adapter.rejections == 0
    assert view.controls[-1] == {
        "run_enabled": False,
        "cancel_enabled": False,
        "approval_visible": False,
        "undo_enabled": False,
    }


def test_undo_is_disabled_outside_idle():
    for state in (
        RuntimeState.PLANNING,
        RuntimeState.EXECUTING_TOOL,
        RuntimeState.WAITING_USER_INPUT,
        RuntimeState.WAITING_APPROVAL,
        RuntimeState.RESUMING_PLAN,
    ):
        presenter, adapter, view = presenter_at(state)
        assert not presenter.undo()
        assert adapter.undos == 0
        assert view.controls[-1]["undo_enabled"] is False


class FakeManager:
    def __init__(self):
        self.entered = threading.Event()
        self.closed = threading.Event()

    @asynccontextmanager
    async def connect(self):
        self.entered.set()
        try:
            yield self
        finally:
            self.closed.set()

    async def list_tools(self):
        return []


class FakeClient:
    def __init__(self):
        self.closed = threading.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed.set()


class BlockingModel:
    def __init__(self):
        self.entered = threading.Event()
        self.cancelled = threading.Event()

    async def generate(self, *_args, **_kwargs):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class FailingModel:
    async def generate(self, *_args, **_kwargs):
        raise RuntimeError("raw-provider-secret")


def wait_for_message(messages, message_type, timeout=5):
    seen = []
    while True:
        message = messages.get(timeout=timeout)
        seen.append(message)
        if message.type is message_type:
            return message, seen


def test_worker_active_cancel_closes_owned_manager_and_http_client(config):
    messages = queue.Queue()
    manager, client, model = FakeManager(), FakeClient(), BlockingModel()
    worker = AsyncRuntimeWorker(
        config,
        messages,
        manager_factory=lambda _config: manager,
        client_factory=lambda _config: client,
        model_factory=lambda _client, _model_name: model,
    )
    worker.start()
    ready, _ = wait_for_message(messages, UIMessageType.STATE)
    assert ready.value is RuntimeState.IDLE

    assert worker.submit("파일 목록")
    assert model.entered.wait(5)
    assert worker.cancel_active()
    stopped, seen = wait_for_message(messages, UIMessageType.STOPPED)
    worker.shutdown()

    assert stopped.value is True
    assert model.cancelled.is_set()
    assert manager.closed.is_set()
    assert client.closed.is_set()
    assert worker.thread is not None and not worker.thread.is_alive()
    assert any(
        message.type is UIMessageType.NOTICE
        and message.value == "요청을 중단하고 실행 중인 도구를 정리했습니다."
        for message in seen
    )


def test_worker_normal_shutdown_closes_owned_resources(config):
    messages = queue.Queue()
    manager, client = FakeManager(), FakeClient()
    worker = AsyncRuntimeWorker(
        config,
        messages,
        manager_factory=lambda _config: manager,
        client_factory=lambda _config: client,
        model_factory=lambda _client, _model_name: object(),
    )
    worker.start()
    wait_for_message(messages, UIMessageType.STATE)

    worker.shutdown()

    assert manager.closed.is_set()
    assert client.closed.is_set()
    assert worker.thread is not None and not worker.thread.is_alive()


def test_worker_exception_is_redacted_and_closes_owned_resources(config):
    messages = queue.Queue()
    manager, client = FakeManager(), FakeClient()
    worker = AsyncRuntimeWorker(
        config,
        messages,
        manager_factory=lambda _config: manager,
        client_factory=lambda _config: client,
        model_factory=lambda _client, _model_name: FailingModel(),
    )
    worker.start()
    wait_for_message(messages, UIMessageType.STATE)

    assert worker.submit("실패 요청")
    stopped, seen = wait_for_message(messages, UIMessageType.STOPPED)
    worker.shutdown()

    failures = [message for message in seen if message.type is UIMessageType.FAILURE]
    assert stopped.value is True
    assert len(failures) == 1
    assert "RuntimeError" in failures[0].value
    assert "raw-provider-secret" not in failures[0].value
    assert manager.closed.is_set()
    assert client.closed.is_set()
    assert worker.thread is not None and not worker.thread.is_alive()


def test_outcome_rendering_keeps_clarification_contract():
    presenter, _adapter, view = presenter_at(RuntimeState.WAITING_USER_INPUT)
    context = ClarificationContext("요약", "file_candidate", ("a", "b"), 1)

    presenter.handle(
        UIMessage(
            UIMessageType.OUTCOME,
            Clarification("선택하세요", context.options, context),
        )
    )
    presenter.handle(UIMessage(UIMessageType.OUTCOME, Completed("완료")))

    assert view.outputs == ["[QUESTION] 선택하세요\n  1. a\n  2. b", "[DONE] 완료"]
