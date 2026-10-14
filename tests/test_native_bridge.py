"""Native UI cannot bypass runtime state or replay a stale approval."""

import json
import queue
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest

from voice_action.native_bridge import NativeBridge
from voice_action.planner import (
    ApprovalRequired,
    ClarificationRequest,
    ModelTurn,
    OpenAICompatibleModel,
    ToolCall,
)
from voice_action.runtime import RuntimeState
from voice_action.ui import AsyncRuntimeWorker, UIMessage, UIMessageType


def bridge():
    worker = Mock()
    events = []
    transport = NativeBridge(worker, events.append)
    transport.message(UIMessage(UIMessageType.STATE, RuntimeState.IDLE))
    return transport, worker, events


def approval():
    return ApprovalRequired("filesystem__write_file", "/allowed/out.txt", "create", 1,
                            "x", False, {"nonce": "DO_NOT_SEND", "content": "DO_NOT_SEND"})


def test_approval_is_private_one_shot_and_old_ids_cannot_authorize_a_new_write():
    transport, worker, events = bridge()
    transport.message(UIMessage(UIMessageType.STATE, RuntimeState.WAITING_APPROVAL))
    transport.message(UIMessage(UIMessageType.OUTCOME, approval()))
    first = events[-1]["approval_id"]
    assert "DO_NOT_SEND" not in json.dumps(events)
    transport.command({"action": "approve", "approval_id": "wrong"})
    worker.approve_pending.assert_not_called()
    transport.command({"action": "approve", "approval_id": first})
    transport.command({"action": "approve", "approval_id": first})
    worker.approve_pending.assert_called_once()
    transport.message(UIMessage(UIMessageType.OUTCOME, approval()))
    assert events[-1]["approval_id"] != first
    transport.command({"action": "approve", "approval_id": first})
    worker.approve_pending.assert_called_once()


def test_busy_unready_and_approval_states_refuse_new_commands():
    transport, worker, events = bridge()
    transport.state = None
    transport.command({"action": "submit", "text": "x"})
    transport.state = RuntimeState.WAITING_APPROVAL
    transport.command({"action": "submit", "text": "x"})
    worker.submit.assert_not_called()
    transport.state = RuntimeState.IDLE
    transport.command({"action": "submit", "text": "x"})
    transport.command({"action": "undo"})
    worker.submit.assert_called_once_with("x")
    worker.undo_last_write.assert_not_called()
    assert events[-1]["type"] == "rejected"


def test_clarification_and_cancel_use_original_runtime_contract():
    transport, worker, _ = bridge()
    transport.state = RuntimeState.WAITING_USER_INPUT
    transport.command({"action": "submit", "text": "2번"})
    worker.submit.assert_called_once_with("2번")
    transport.in_flight = False
    transport.command({"action": "cancel"})
    assert worker.submit.call_args.args == ("취소",)
    transport.state = RuntimeState.WAITING_APPROVAL
    transport.in_flight = False
    transport.approval_id = "current"
    transport.command({"action": "cancel"})
    worker.reject_pending.assert_called_once()


@pytest.mark.parametrize("payload", [None, [], {"action": "submit", "text": []},
                                    {"action": "rate", "score": True},
                                    {"action": "rate", "score": 1, "tags": ["bad"]}])
def test_malformed_commands_do_not_reach_worker(payload):
    transport, worker, events = bridge()
    transport.command(payload)
    assert events[-1]["type"] == "rejected"
    assert not worker.mock_calls


@pytest.mark.integration
def test_native_transport_real_mcp_approval_write_and_undo(config, tmp_path):
    from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient
    from voice_action.mcp_manager import MCPManager

    target = config.allowed_directories[0] / "native.txt"
    turns = iter([
        ModelTurn(None, [], ClarificationRequest(
            "저장할 내용은 무엇인가요?", (), "free_text", "question"
        )),
        ModelTurn(None, [ToolCall("write", "filesystem__write_file", json.dumps(
            {"path": str(target), "content": "native backend test"}
        ))]),
        ModelTurn("저장했습니다.", []),
    ])
    model = Mock(spec=OpenAICompatibleModel)

    async def generate(*args, **kwargs):
        return next(turns)

    model.generate = generate

    @asynccontextmanager
    async def client(_config):
        yield None

    messages = queue.Queue()
    worker = AsyncRuntimeWorker(
        config, messages,
        manager_factory=lambda cfg: MCPManager({"filesystem": MCPClient(
            cfg, allowed_tools=SUPPORTED_TOOLS
        )}),
        client_factory=client, model_factory=lambda *_: model,
        feedback_factory=lambda *_: None,
    )
    from voice_action.undo import UndoJournal

    worker._build_undo_journal = lambda: UndoJournal(
        tmp_path / "native-undo.sqlite3", config.allowed_directories
    )
    events = []
    transport = NativeBridge(worker, events.append)

    def until(kind):
        for _ in range(100):
            transport.message(messages.get(timeout=10))
            if events and events[-1]["type"] == kind:
                return events[-1]
        pytest.fail(f"missing event: {kind}")

    worker.start()
    try:
        until("state")
        outside = tmp_path / "outside.txt"
        outside.write_text("not allowed")
        transport.command({"action": "attach", "path": str(outside)})
        refused = until("attachment")
        assert refused["path"] is None and refused["text"]
        transport.command({"action": "submit", "text": "native.txt에 저장해줘"})
        question = until("clarification")
        assert question["kind"] == "free_text" and question["choices"] == []
        transport.command({"action": "submit", "text": "native backend test"})
        pending = until("approval")
        assert not target.exists()
        transport.command({"action": "approve", "approval_id": "stale"})
        assert not target.exists()
        transport.command({"action": "approve", "approval_id": pending["approval_id"]})
        until("completed")
        assert target.read_text() == "native backend test"
        transport.command({"action": "undo"})
        until("completed")
        assert not target.exists()
    finally:
        worker.shutdown()


def test_json_stream_rejects_malformed_input_and_closes_worker_on_shutdown(
    config, monkeypatch, capsys
):
    import io
    import os

    from voice_action import native_bridge

    worker = Mock()
    monkeypatch.setattr(native_bridge, "load_config", lambda: config)
    monkeypatch.setattr(native_bridge, "AsyncRuntimeWorker", lambda *_: worker)
    monkeypatch.setattr(native_bridge.signal, "signal", lambda *_: None)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'not json\n{"action":"shutdown"}\n')
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as stream:
        monkeypatch.setattr(native_bridge.sys, "stdin", io.TextIOWrapper(stream))
        assert native_bridge.main() == 0
    worker.shutdown.assert_called_once()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["type"] for event in events] == ["config", "rejected"]
    assert config.api_key not in json.dumps(events)
