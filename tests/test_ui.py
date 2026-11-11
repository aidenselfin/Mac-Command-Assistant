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
def _isolated_journal(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_action.undo.DEFAULT_JOURNAL_PATH", tmp_path / "undo.sqlite3")


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
        self.revealed = []
        self.attachment = None

    def reveal_files(self, paths):
        self.revealed.append(paths)

    def set_attachment(self, path):
        self.attachment = path

    def set_choices(self, labels):
        self.choices = labels

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
        self.attached, self.detached = [], 0

    def attach(self, path):
        self.attached.append(path)
        return True

    def detach(self):
        self.detached += 1

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


def test_manager_factory_registers_only_the_filesystem_client(config, monkeypatch):
    filesystem = object()

    monkeypatch.setattr(ui_module, "MCPClient", lambda *_args, **_kwargs: filesystem)
    monkeypatch.setattr(ui_module, "MCPManager", lambda clients: clients)

    assert ui_module._manager_factory(config) == {"filesystem": filesystem}


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


def test_worker_cancel_then_shutdown_never_hangs(config):
    # 회귀: cancel_active()가 루프를 끝내는 사이 shutdown()이 실행되지 않을 요청을 기다리며 멈췄다.
    for _ in range(30):
        messages = queue.Queue()
        manager, client, model = FakeManager(), FakeClient(), BlockingModel()
        worker = AsyncRuntimeWorker(
            config,
            messages,
            manager_factory=lambda _config, manager=manager: manager,
            client_factory=lambda _config, client=client: client,
            model_factory=lambda _client, _model_name, model=model: model,
        )
        worker.start()
        wait_for_message(messages, UIMessageType.STATE)
        assert worker.submit("파일 목록")
        assert model.entered.wait(5)
        assert worker.cancel_active()
        wait_for_message(messages, UIMessageType.STOPPED)

        done = threading.Event()
        stopper = threading.Thread(
            target=lambda w=worker, d=done: (w.shutdown(), d.set()), daemon=True
        )
        stopper.start()

        assert done.wait(5), "shutdown()이 멈춘 루프를 기다리며 끝나지 않았습니다."
        assert manager.closed.is_set()
        assert client.closed.is_set()
        assert worker.thread is not None and not worker.thread.is_alive()


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


def test_only_a_completed_result_with_changed_files_reveals_them():
    presenter, _, view = presenter_at(RuntimeState.IDLE)
    presenter.handle(UIMessage(UIMessageType.OUTCOME, Completed("답")))
    presenter.handle(UIMessage(UIMessageType.OUTCOME, Error("실패")))
    assert view.revealed == []  # nothing changed, or it failed
    presenter.handle(UIMessage(UIMessageType.OUTCOME, Completed("저장", ("/r/한글 파일.txt",))))
    assert view.revealed == [("/r/한글 파일.txt",)]


def test_presenter_takes_one_file_only_while_idle_and_shows_refusals():
    presenter, adapter, view = presenter_at(RuntimeState.IDLE)
    assert not presenter.attach(("/r/a.md", "/r/b.md"))
    assert "하나만" in view.outputs[-1] and adapter.attached == []
    assert not presenter.attach(())
    assert presenter.attach(("/r/한글 이름.md",)) and adapter.attached == ["/r/한글 이름.md"]
    presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.PLANNING))
    assert not presenter.attach(("/r/c.md",)) and "끝난 뒤" in view.outputs[-1]


def test_attachment_chip_follows_the_worker_and_goes_away_with_the_command():
    presenter, adapter, view = presenter_at(RuntimeState.IDLE)
    presenter.handle(UIMessage(UIMessageType.ATTACHMENT, ("/r/a.md", None)))
    assert view.attachment == "/r/a.md"
    presenter.handle(UIMessage(UIMessageType.ATTACHMENT, (None, "허용 폴더 밖이에요.")))
    assert view.attachment == "/r/a.md" and view.outputs[-1] == "허용 폴더 밖이에요."
    assert presenter.execute("이 파일 요약해줘")
    assert view.attachment is None  # consumed by that one command
    presenter.handle(UIMessage(UIMessageType.ATTACHMENT, ("/r/b.md", None)))
    presenter.detach()
    assert view.attachment is None and adapter.detached == 1


def test_an_answer_to_a_question_keeps_the_attachment():
    presenter, _, view = presenter_at(RuntimeState.IDLE)
    presenter.handle(UIMessage(UIMessageType.ATTACHMENT, ("/r/a.md", None)))
    pick_question(presenter)
    assert presenter.execute("1번")
    assert view.attachment == "/r/a.md"  # only a new command takes it


def test_worker_attach_checks_the_path_on_the_worker_and_reports_back(config):
    root = config.allowed_directories[0]
    (root / "내 파일.md").write_text("x")
    messages = queue.Queue()
    worker = AsyncRuntimeWorker(
        config,
        messages,
        manager_factory=lambda _config: FakeManager(),
        client_factory=lambda _config: FakeClient(),
        model_factory=lambda _client, _model_name: object(),
    )
    worker.start()
    wait_for_message(messages, UIMessageType.STATE)

    assert worker.attach(str(root / "내 파일.md"))
    accepted, _ = wait_for_message(messages, UIMessageType.ATTACHMENT)
    assert accepted.value == (str(root / "내 파일.md"), None)

    assert worker.attach(str(root.parent / "밖.txt"))
    refused, _ = wait_for_message(messages, UIMessageType.ATTACHMENT)
    worker.shutdown()
    assert refused.value[0] is None and "요청을 거부" in refused.value[1]



def test_display_path_escapes_control_characters_only():
    assert ui_module.display_path('/w/a\nb\t"c".txt') == '/w/a\\nb\\t"c".txt'
    assert ui_module.display_path("/w/회의록 (1).md") == "/w/회의록 (1).md"
