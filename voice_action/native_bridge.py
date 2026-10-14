"""Private JSON-lines transport for the native UI over the existing runtime worker."""

import json
import os
import queue
import selectors
import signal
import sys
import uuid

from voice_action.config import load_config
from voice_action.feedback import RATING_TAGS
from voice_action.planner import ApprovalRequired, Clarification, Completed, Error
from voice_action.runtime import Busy, RuntimeState
from voice_action.ui import AsyncRuntimeWorker, UIMessageType, display_path


class NativeBridge:
    def __init__(self, worker, emit):
        self.worker, self.emit = worker, emit
        self.state = None
        self.in_flight = False
        self.approval_id = None

    def message(self, message):
        kind, value = message.type, message.value
        if kind is UIMessageType.STATE:
            self.state = value
            self.emit({"type": "state", "state": value.name})
        elif kind is UIMessageType.OUTCOME:
            self.in_flight = False
            self.approval_id = None
            if isinstance(value, ApprovalRequired):
                self.approval_id = uuid.uuid4().hex
                self.emit({
                    "type": "approval", "approval_id": self.approval_id,
                    "operation": value.operation, "target": display_path(value.target_path),
                    "targets": [display_path(path) for path in value.targets],
                    "preview": value.preview, "truncated": value.preview_truncated,
                    "characters": value.content_chars,
                })
            elif isinstance(value, Clarification):
                self.emit({"type": "clarification", "text": value.question,
                           "kind": value.context.kind, "choices": list(value.shown_options)})
            elif isinstance(value, Completed):
                self.emit({"type": "completed", "text": value.text,
                           "changed_paths": list(value.changed_paths),
                           "ratable": self.worker.can_rate()})
            elif isinstance(value, (Error, Busy)):
                self.emit({"type": "error", "text": value.message})
        elif kind is UIMessageType.PLANNER_EVENT:
            self.emit({"type": "tool", "name": value.tool_name or ""})
        elif kind is UIMessageType.ATTACHMENT:
            path, refusal = value
            self.emit({"type": "attachment", "path": path, "text": refusal})
        elif kind is UIMessageType.RATING_SAVED:
            score, tags = value
            self.emit({"type": "rating_saved", "score": score, "tags": list(tags)})
        elif kind is UIMessageType.FAILURE:
            self.in_flight = False
            self.approval_id = None
            self.emit({"type": "error", "text": str(value)})
        elif kind is UIMessageType.STOPPED:
            self.approval_id = None
            self.emit({"type": "stopped"})
        elif kind is UIMessageType.NOTICE:
            self.emit({"type": "notice", "text": str(value)})

    def command(self, request):
        if not isinstance(request, dict):
            return self.refuse()
        action = request.get("action")
        if action == "shutdown":
            return False
        if action == "cancel":
            if self.state is RuntimeState.WAITING_APPROVAL:
                return self.command({"action": "reject", "approval_id": self.approval_id})
            if self.state is RuntimeState.WAITING_USER_INPUT:
                return self.command({"action": "submit", "text": "취소"})
            if self.worker.cancel_active():
                return True
            return self.refuse()
        if self.in_flight:
            return self.refuse()
        if action in {"approve", "reject"}:
            if (self.state is not RuntimeState.WAITING_APPROVAL or self.approval_id is None
                    or request.get("approval_id") != self.approval_id):
                return self.refuse()
            accepted = (self.worker.approve_pending() if action == "approve"
                        else self.worker.reject_pending())
            if accepted:
                self.approval_id = None
                self.in_flight = True
                return True
            return self.refuse()
        if action == "submit":
            text = request.get("text")
            if (self.state not in {RuntimeState.IDLE, RuntimeState.WAITING_USER_INPUT}
                    or not isinstance(text, str) or not text.strip()):
                return self.refuse()
            if self.worker.submit(text):
                self.in_flight = True
                return True
            return self.refuse()
        if self.state is not RuntimeState.IDLE:
            return self.refuse()
        if action == "undo":
            if self.worker.undo_last_write():
                self.in_flight = True
                return True
        elif action == "attach":
            path = request.get("path")
            if isinstance(path, str) and self.worker.attach(path):
                return True
        elif action == "detach":
            self.worker.detach()
            self.emit({"type": "attachment", "path": None, "text": None})
            return True
        elif action == "rate":
            score, tags = request.get("score"), request.get("tags", [])
            if (type(score) is int and 1 <= score <= 5 and isinstance(tags, list)
                    and all(isinstance(tag, str) and tag in RATING_TAGS for tag in tags)
                    and self.worker.rate(score, tuple(tags))):
                return True
        return self.refuse()

    def refuse(self):
        self.emit({"type": "rejected", "text": "현재 상태에서 처리할 수 없는 요청입니다."})
        return True


def main():
    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)

    try:
        config = load_config()
    except Exception as exc:
        emit({"type": "error", "text": (
            f"설정 로딩 실패 ({type(exc).__name__}). 설정과 키를 확인하세요."
        )})
        return 1
    messages = queue.Queue()
    worker = AsyncRuntimeWorker(config, messages)
    bridge = NativeBridge(worker, emit)
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    emit({"type": "config", "folders": [str(path) for path in config.allowed_directories],
          "finder_reveal": config.finder_reveal})
    worker.start()
    # Read bytes ourselves: buffered readline can leave the next JSON message invisible to select.
    buffered = b""
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin.buffer, selectors.EVENT_READ)
            while not stopped:
                while not messages.empty():
                    message = messages.get_nowait()
                    bridge.message(message)
                    if message.type is UIMessageType.STOPPED:
                        stopped = True
                if stopped:
                    break
                for key, _ in selector.select(timeout=0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        stopped = True
                        break
                    buffered += chunk
                    if len(buffered) > 1024 * 1024:
                        bridge.refuse()
                        stopped = True
                        break
                    while b"\n" in buffered:
                        line, buffered = buffered.split(b"\n", 1)
                        try:
                            request = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            bridge.refuse()
                            continue
                        if not bridge.command(request):
                            stopped = True
                            break
    except (BrokenPipeError, OSError):
        pass
    finally:
        worker.shutdown()  # waits for cancellation and durable deletion/Undo cleanup
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
