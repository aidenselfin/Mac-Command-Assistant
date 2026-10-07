import asyncio
import gc
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
    Error,
    PlannerError,
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


@pytest.fixture(autouse=True)
def _collect_tk_on_main_thread():
    """A destroyed Tk root can sit in a reference cycle. If the cycle is collected on a worker
    thread, Tcl aborts the whole process; collect it here, on the thread that made it."""
    yield
    gc.collect()


class FakeView:
    def __init__(self):
        self.statuses = []
        self.tools = []
        self.controls = []
        self.outputs = []
        self.close_count = 0
        self.requests = []
        self.approval_enabled = []
        self.choices = ()
        self.rating = (False, None, ())

    def set_choices(self, labels):
        self.choices = labels

    def set_rating(self, visible, score=None, tags=()):
        self.rating = (visible, score, tags)

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
        self.ratings = []

    def rate(self, score, tags):
        self.ratings.append((score, tags))
        return True

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
        view.entry.delete(0, tk.END)
        view.entry.event_generate("<Control-v>")
        assert view.entry.get() == "한글 붙여넣기"
        view.entry.delete(0, tk.END)
        view.entry.insert(0, "앞 교체 뒤")
        view.entry.selection_range(2, 4)
        view.entry.icursor(tk.END)
        view.entry.event_generate("<<Paste>>")
        assert view.entry.get() == "앞 한글 붙여넣기 뒤"
        view.entry.selection_range(0, tk.END)
        root.clipboard_clear()
        view.entry_menu.invoke(0)
        assert view.entry.get() == "앞 한글 붙여넣기 뒤"
        root.clipboard_append("덮어쓰면 안 됨")
        for state in ("readonly", "disabled"):
            view.entry.configure(state=state)
            view.entry_menu.invoke(0)
            assert view.entry.get() == "앞 한글 붙여넣기 뒤"
        assert view.entry.bind("<Button-3>")
        if ui_module.sys.platform == "darwin":
            assert view.entry.bind("<Button-2>")
            assert view.entry.bind("<Control-Button-1>")
    finally:
        root.update()  # flush pending Tk events before destroy (macOS Tk)
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
    approve_presenter, approve_adapter, _view = presenter_at(RuntimeState.WAITING_APPROVAL)
    reject_presenter, reject_adapter, _view = presenter_at(RuntimeState.WAITING_APPROVAL)

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


class PlannerFailingModel:
    async def generate(self, *_args, **_kwargs):
        raise PlannerError("LLM API 요청 실패 (HTTP 404)")


def test_worker_planner_error_is_reported_and_keeps_window_open(config):
    messages = queue.Queue()
    manager, client = FakeManager(), FakeClient()
    worker = AsyncRuntimeWorker(
        config,
        messages,
        manager_factory=lambda _config: manager,
        client_factory=lambda _config: client,
        model_factory=lambda _client, _model_name: PlannerFailingModel(),
    )
    worker.start()
    wait_for_message(messages, UIMessageType.STATE)

    for _ in range(2):  # still accepts commands after the first failure
        assert worker.submit("실패 요청")
        outcome, seen = wait_for_message(messages, UIMessageType.OUTCOME)
        assert outcome.value == Error("LLM API 요청 실패 (HTTP 404)")
        assert not [m for m in seen if m.type in {UIMessageType.FAILURE, UIMessageType.STOPPED}]
    assert not manager.closed.is_set()
    worker.shutdown()


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


def test_redesigned_tk_view_preserves_approval_and_input_contract():
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    try:
        view = ui_module.TkView(root)
        adapter = FakeAdapter()
        presenter = UIPresenter(adapter, view)
        view.bind_presenter(presenter)
        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.IDLE))
        root.update()
        assert root.minsize() == (720, 520)
        view.entry.insert(0, "파일 목록 보여줘")
        assert adapter.submissions == []
        view._execute()
        assert len(adapter.submissions) == 1
        assert view.entry.get() == ""
        assert "내 요청" in view.output.get("1.0", "end")

        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.WAITING_APPROVAL))
        assert str(view.approve_button.cget("state")) == "disabled"
        targets = tuple(f"/allowed/긴 경로/{i:03}.txt" for i in range(100))
        pending = ApprovalRequired(
            "local__delete_files", targets[0], "delete", 0, "", False, {"nonce": "PRIVATE"}, targets
        )
        presenter.handle(UIMessage(UIMessageType.OUTCOME, pending))
        root.geometry("720x520")
        root.update()
        text = view.approval_output.get("1.0", "end")
        assert all(path in text for path in targets)
        assert "PRIVATE" not in text
        assert view.approval_frame.winfo_ismapped()
        for widget in (view.entry, view.approve_button, view.reject_button, view.undo_button):
            assert widget.winfo_rooty() + widget.winfo_height() <= (
                root.winfo_rooty() + root.winfo_height()
            )
        view.entry.insert(0, "실행하면 안 되는 명령")
        view._enter(None)
        assert adapter.approvals == 0 and len(adapter.submissions) == 1
        view.approve_button.invoke()
        view.approve_button.invoke()
        assert adapter.approvals == 1
        assert str(view.approve_button.cget("state")) == "disabled"
        assert str(view.reject_button.cget("state")) == "disabled"
        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.IDLE))
        presenter.handle(UIMessage(UIMessageType.OUTCOME, Completed("완료")))
        root.update()
        assert not view.approval_frame.winfo_ismapped()

        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.WAITING_APPROVAL))
        pending = ApprovalRequired(
            "filesystem__write_file",
            "/allowed/summary.md",
            "overwrite",
            240,
            "가" * 200 + "SECRET",
            True,
            {},
        )
        presenter.handle(UIMessage(UIMessageType.OUTCOME, pending))
        text = view.approval_output.get("1.0", "end")
        assert "덮어씁니다" in text and "일부 내용만" in text
        assert "SECRET" not in text
        view.reject_button.invoke()
        assert adapter.rejections == 1

        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.IDLE))
        presenter.handle(UIMessage(UIMessageType.OUTCOME, Completed("줄\n" * 150)))
        root.update()
        view.output.yview_moveto(0)
        root.update()
        presenter.handle(UIMessage(UIMessageType.OUTCOME, Completed("새 결과")))
        root.update()
        assert view.output.yview()[0] < 0.1
    finally:
        root.update()  # flush pending Tk events before destroy (macOS Tk)
        root.destroy()


def test_lost_hangul_keystroke_detection():
    real_g = (5 << 24) | ord("ㅎ")  # TkAqua: virtual key 5 ('g') typed with the Korean IME
    commit = (0x80 << 24) | ord("ㅎ")
    assert ui_module.is_lost_hangul_keystroke("ㅎ", real_g, composing=False)
    assert not ui_module.is_lost_hangul_keystroke("ㅎ", real_g, composing=True)
    assert not ui_module.is_lost_hangul_keystroke("ㅎ", commit, composing=False)
    assert not ui_module.is_lost_hangul_keystroke("한", real_g, composing=False)
    assert not ui_module.is_lost_hangul_keystroke("g", (5 << 24) | ord("g"), composing=False)


def test_render_markdown_turns_symbols_into_styles():
    def text(segments):
        return "".join(t for t, _ in segments)

    def tags_of(segments, needle):
        return next(tags for t, tags in segments if needle in t)

    rendered = ui_module.render_markdown(
        "## 요약\n"
        "**굵게**와 *기울임*, `code.py`\n"
        "- 첫째\n"
        "  - 둘째\n"
        "1. 번호 유지\n"
        "[문서](https://example.com)\n"
        "> 인용\n"
        "---\n"
        "```\n**그대로** `둠`\n```\n"
        "| 파일 | 크기 |\n|---|---|\n| **a.md** | 10 |"
    )
    plain = text(rendered)
    assert "요약\n굵게와 기울임, code.py\n• 첫째\n  • 둘째\n1. 번호 유지\n" in plain
    assert "문서 (https://example.com)\n인용\n────────\n" in plain
    assert "**그대로** `둠`\n" in plain  # code blocks are shown as written
    assert "파일  크기\na.md  10" in plain and "|" not in plain and "---" not in plain
    assert "md_heading" in tags_of(rendered, "요약")
    assert "md_bold" in tags_of(rendered, "굵게")
    assert "md_italic" in tags_of(rendered, "기울임")
    assert "md_code" in tags_of(rendered, "code.py")
    assert "md_table" in tags_of(rendered, "a.md")


def test_render_markdown_leaves_non_markdown_text_alone():
    for text in ("2 * 3 * 4", "**닫히지 않음", "file_name_v2.txt", "a*b", "평범한 문장"):
        assert "".join(t for t, _ in ui_module.render_markdown(text)) == text


def test_tk_view_shows_folder_and_renders_only_model_text():
    import tkinter as tk
    from pathlib import Path

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    try:
        view = ui_module.TkView(root, (Path("/tmp/작업 폴더"),))
        assert view.folder_text == "작업 폴더 · /tmp/작업 폴더"
        view.show_request("**요청**은 그대로")
        view.show_outcome(Completed("## 결과\n**굵게** `코드`"))
        shown = view.output.get("1.0", "end")
        assert "**요청**은 그대로" in shown
        assert "결과\n굵게 코드" in shown and "## " not in shown and "`" not in shown

        context = ClarificationContext("요약", "file_candidate", ("a", "b"), 1)
        view.show_outcome(
            ApprovalRequired(
                "filesystem__write_file", "/tmp/a.md", "create", 9, "**원문**", False, context
            )
        )
        assert "**원문**" in view.approval_output.get("1.0", "end")  # what will be written
    finally:
        root.update()  # flush pending Tk events before destroy (macOS Tk)
        root.destroy()


def test_redesigned_view_status_tool_cards_and_delete_panel():
    import tkinter as tk
    from pathlib import Path

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    try:
        view = ui_module.TkView(root, (Path("/tmp/work"),))
        presenter = UIPresenter(FakeAdapter(), view)
        view.bind_presenter(presenter)
        assert "아직 대화 기록이 없어요" in view.output.get("1.0", "end")

        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.PLANNING))
        assert view.status_var.get() == "요청 분석 중"
        assert str(view.hint_label.cget("fg")).upper() == ui_module.RED
        presenter.handle(
            UIMessage(
                UIMessageType.PLANNER_EVENT,
                PlannerEvent(PlannerEventType.TOOL_STARTED, 1, "filesystem__read_text_file"),
            )
        )
        root.update()
        assert view.tool_line.winfo_ismapped()
        assert view.tool_var.get() == "filesystem__read_text_file"
        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.IDLE))
        root.update()
        assert view.status_var.get() == "준비됨" and not view.tool_line.winfo_ismapped()
        assert str(view.hint_label.cget("fg")).upper() == ui_module.MUTED

        view.show_request("목록 보여줘")
        shown = view.output.get("1.0", "end")
        assert "아직 대화 기록이 없어요" not in shown and "내 요청\n목록 보여줘" in shown
        assert int(view.output.tag_cget("request", "lmargin1")) > int(
            view.output.tag_cget("result", "lmargin1")
        )

        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.WAITING_APPROVAL))
        targets = ("/tmp/work/a.log", "/tmp/work/b.log")
        presenter.handle(
            UIMessage(
                UIMessageType.OUTCOME,
                ApprovalRequired("local__delete_files", targets[0], "delete", 0, "", False,
                                 {}, targets),
            )
        )
        details = view.approval_output.get("1.0", "end")
        assert "삭제 대상 2개" in details and "1  /tmp/work/a.log" in details
        assert view.approve_button.cget("style") == "Danger.TButton"
        assert str(view.approval_frame.cget("bg")).upper() == ui_module.RED_SOFT
    finally:
        root.update()  # flush pending Tk events before destroy (macOS Tk)
        root.destroy()


def test_convert_approval_panel_shows_source_output_and_preview():
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    try:
        view = ui_module.TkView(root)
        presenter = UIPresenter(FakeAdapter(), view)
        view.bind_presenter(presenter)
        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.WAITING_APPROVAL))
        pending = ApprovalRequired(
            "local__convert_file", "/w/사진.jpg", "convert", 2048, "", False, {}, ("/w/사진.heic",)
        )
        presenter.handle(UIMessage(UIMessageType.OUTCOME, pending))
        details = view.approval_output.get("1.0", "end")
        assert "원본  /w/사진.heic" in details and "출력  /w/사진.jpg" in details
        assert "2,048 bytes" in details and "미리보기가 없습니다" in details
        assert view.approve_button.cget("text") == "승인하고 변환"
    finally:
        root.update()  # flush pending Tk events before destroy (macOS Tk)
        root.destroy()


def pick_question(presenter, kind="file_candidate"):
    options = ("/r/a.md", "/r/b.md")
    context = ClarificationContext("요약", kind, options, 1)
    presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.WAITING_USER_INPUT))
    presenter.handle(
        UIMessage(
            UIMessageType.OUTCOME,
            Clarification("고르세요", options, context, ("a.md · 자료", "b.md · 자료")),
        )
    )


def test_question_shows_labelled_buttons_and_a_click_answers_with_n_beon():
    presenter, adapter, view = presenter_at(RuntimeState.IDLE)
    pick_question(presenter)
    assert view.choices == ("a.md · 자료", "b.md · 자료")
    assert not presenter.choose(3) and not presenter.choose(0)
    assert presenter.choose(2)
    assert adapter.submissions == ["2번"]
    assert view.requests == ["2. b.md · 자료"]
    assert view.choices == ()  # gone as soon as the answer is on its way


def test_choices_hide_when_the_question_is_no_longer_open():
    presenter, adapter, view = presenter_at(RuntimeState.IDLE)
    pick_question(presenter, "narrow_search")
    presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.PLANNING))
    assert view.choices == ()
    assert not presenter.choose(1) and adapter.submissions == []


def test_free_text_still_answers_a_question_with_buttons():
    presenter, adapter, view = presenter_at(RuntimeState.IDLE)
    pick_question(presenter, "narrow_search")
    assert presenter.execute("임시-12번")
    assert adapter.submissions == ["임시-12번"] and view.choices == ()


def test_completed_answer_opens_rating_and_next_command_closes_it():
    presenter, adapter, view = presenter_at(RuntimeState.IDLE)
    assert view.rating[0] is False
    presenter.handle(UIMessage(UIMessageType.OUTCOME, Completed("답")))
    assert view.rating == (True, None, ())
    assert presenter.rate(5) and adapter.ratings == [(5, ())]
    assert view.rating == (True, 5, ())
    assert not presenter.rate(0) and not presenter.rate(6)
    assert presenter.execute("다음")
    assert view.rating[0] is False and not presenter.rate(4)


def test_errors_and_questions_cannot_be_rated():
    presenter, adapter, view = presenter_at(RuntimeState.IDLE)
    presenter.handle(UIMessage(UIMessageType.OUTCOME, Error("실패")))
    assert not presenter.rate(5) and adapter.ratings == [] and view.rating[0] is False


def test_low_score_collects_causes_and_a_better_score_drops_them():
    presenter, adapter, view = presenter_at(RuntimeState.IDLE)
    presenter.handle(UIMessage(UIMessageType.OUTCOME, Completed("답")))
    assert not presenter.toggle_tag("wrong_file")  # nothing rated yet
    presenter.rate(2)
    assert presenter.toggle_tag("wrong_file") and presenter.toggle_tag("too_slow")
    assert presenter.toggle_tag("wrong_file")  # toggled off
    assert adapter.ratings[-1] == (2, ("too_slow",))
    assert view.rating == (True, 2, ("too_slow",))
    presenter.rate(4)
    assert adapter.ratings[-1] == (4, ()) and not presenter.toggle_tag("other")
    presenter.rate(1)  # a new low score starts without the old causes
    assert adapter.ratings[-1] == (1, ())


def test_rating_hides_outside_idle():
    presenter, adapter, view = presenter_at(RuntimeState.IDLE)
    presenter.handle(UIMessage(UIMessageType.OUTCOME, Completed("답")))
    presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.PLANNING))
    assert view.rating[0] is False and not presenter.rate(5)


def test_tk_choice_and_rating_buttons_and_digit_shortcuts():
    import tkinter as tk
    from pathlib import Path

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    try:
        adapter = FakeAdapter()
        view = ui_module.TkView(root, (Path("/tmp/work"),))
        presenter = UIPresenter(adapter, view)
        view.bind_presenter(presenter)

        class Key:
            def __init__(self, char):
                self.char = char

        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.IDLE))
        assert view._on_digit(Key("3")) is None  # nothing to pick or rate yet

        pick_question(presenter)
        root.update()
        buttons = view.choice_frame.winfo_children()
        assert [b.cget("text") for b in buttons] == ["1   a.md · 자료", "2   b.md · 자료"]
        view.entry.insert(0, "12")
        assert view._on_digit(Key("1")) is None  # typing a number into a command
        view.entry.delete(0, "end")
        assert view._on_digit(Key("9")) is None and view._on_digit(Key("a")) is None
        assert view._on_digit(Key("2")) == "break" and adapter.submissions == ["2번"]
        root.update()
        assert not view.choice_frame.winfo_ismapped()

        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.IDLE))
        presenter.handle(UIMessage(UIMessageType.OUTCOME, Completed("답")))
        root.update()
        assert view.rating_frame.winfo_ismapped() and not view.tag_row.winfo_ismapped()
        assert view._on_digit(Key("2")) == "break" and adapter.ratings[-1] == (2, ())
        root.update()
        assert view.tag_row.winfo_ismapped()
        view.tag_buttons["wrong_file"].invoke()
        assert adapter.ratings[-1] == (2, ("wrong_file",))
        view.rating_buttons[5].invoke()
        root.update()
        assert adapter.ratings[-1] == (5, ()) and not view.tag_row.winfo_ismapped()
    finally:
        root.update()
        root.destroy()


class AnsweringModel:
    async def generate(self, *_args, **_kwargs):
        from voice_action.planner import ModelTurn

        return ModelTurn("답", [])


def test_worker_records_a_rating_for_the_answered_run(config, tmp_path):
    import sqlite3

    from voice_action.feedback import FeedbackLoop

    messages = queue.Queue()
    manager, client = FakeManager(), FakeClient()
    store = tmp_path / "ui-rating.sqlite3"
    worker = AsyncRuntimeWorker(
        config,
        messages,
        manager_factory=lambda _config: manager,
        client_factory=lambda _config: client,
        model_factory=lambda _client, _model_name: AnsweringModel(),
        feedback_factory=lambda _config, _emit: FeedbackLoop(store),  # built on the worker thread
    )
    worker.start()
    wait_for_message(messages, UIMessageType.STATE)
    assert worker.submit("질문")
    done, _ = wait_for_message(messages, UIMessageType.OUTCOME)
    assert done.value == Completed("답")

    assert worker.rate(2, ("wrong_file",))
    worker.shutdown()

    rows = sqlite3.connect(store).execute(
        "SELECT json_extract(payload, '$.score'), json_extract(payload, '$.tags') "
        "FROM events WHERE kind = 'rating'"
    ).fetchall()
    assert rows == [(2, '["wrong_file"]')]
