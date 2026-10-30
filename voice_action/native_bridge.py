"""Private JSON-lines transport for the native UI over the existing runtime worker."""

import json
import os
import queue
import selectors
import signal
import sys
import threading
import uuid

from voice_action.config import load_config
from voice_action.feedback import RATING_TAGS
from voice_action.planner import ApprovalRequired, Clarification, Completed, Error
from voice_action.runtime import Busy, RuntimeState
from voice_action.transcribe import clip_path, purge_stale, transcribe
from voice_action.ui import AsyncRuntimeWorker, UIMessageType, display_path

GIVE_ENDINGS = ("줘요", "줘", "주세요", "줄래요", "줄래")
GIVE_VERBS = ("가져와", "가져다", "꺼내")
PARTICLES = ("만", "도", "을", "를", "은", "는")
FILE_FACTS = (
    "경로", "위치", "이름", "목록", "내용", "요약", "크기", "개수", "정보", "날짜", "수정일",
    "생성일", "용량",
)


def _bare(word):
    """'찾아만' is still 찾아, '경로를' still 경로."""
    while word[-1:] in PARTICLES:
        word = word[:-1]
    return word


def _verb_form(word):
    """'해'·'찾아'·'알려'처럼 받침 없는 아/어 꼴이면 그 뒤 '줘'는 동작을 해 달라는 말이다."""
    code = ord(word[-1]) - 0xAC00
    return 0 <= code < 11172 and code % 28 == 0 and code // 28 % 21 in {0, 1, 4, 6, 9, 10, 14, 15}


def asks_for_file(text):
    """'보고서 파일 줘'처럼 파일 자체를 달라는 말인지. '요약해줘'·'찾아 줘'는 아니다."""
    # ponytail: 말끝 규칙만 본다. "데이터 줘", 이름에 "요약"이 든 파일은 놓친다
    words = [word.strip(".,!?~") for word in text.split()]
    plain = [word for word in words if "." not in word and "_" not in word]  # not file names
    if not words or any(fact in word for word in plain for fact in FILE_FACTS):
        return False  # "경로 좀 가져와": facts about the file, not the file
    last = words[-1]  # what the command finally asks: "가져와서 요약해줘" is a summary
    give = next((ending for ending in GIVE_ENDINGS if last.endswith(ending)), "")
    if not give:
        return last.endswith(GIVE_VERBS)
    before = [*words[:-1], last[: -len(give)]]
    before = [word for word in before if word and word != "좀"]  # "찾아 좀 줘" is still 찾아 줘
    stem = before[-1] if before else ""
    if stem.endswith(GIVE_VERBS):  # "꺼내줘", "가져와 줘"
        return True
    stem = _bare(stem)
    if stem.endswith(("거", "것", "서", "나", "개", "명")):  # 보고서·하나·두 개: nouns, not verb forms
        return True
    return not stem or not _verb_form(stem)


class NativeBridge:
    def __init__(self, worker, emit, voice_key=None):
        self.worker, self.emit, self.voice_key = worker, emit, voice_key
        self.transcribing = False
        self.voice_cancelled = False
        self.voice_lock = threading.Lock()
        self.voice_thread = None
        self.state = None
        self.in_flight = False
        self.approval_id = None
        self.request = ""  # the command a clarification answer belongs to

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
                           "found_paths": list(value.found_paths),
                           "offer_file": asks_for_file(self.request),
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
            with self.voice_lock:
                if self.transcribing:
                    self.voice_cancelled = True
                    return True
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
        if action == "voice":
            clip = clip_path(request.get("path"))
            if (clip is None or self.transcribing or self.state not in
                    {RuntimeState.IDLE, RuntimeState.WAITING_USER_INPUT}):
                if clip is not None:
                    try:
                        clip.unlink(missing_ok=True)
                    except OSError:
                        pass
                return self.refuse()
            self.transcribing, self.voice_cancelled = True, False
            self.voice_thread = threading.Thread(
                target=self.transcribe_clip, args=(clip,), daemon=True)
            self.voice_thread.start()
            return True
        if action == "submit":
            text = request.get("text")
            if (self.state not in {RuntimeState.IDLE, RuntimeState.WAITING_USER_INPUT}
                    or not isinstance(text, str) or not text.strip()):
                return self.refuse()
            if self.worker.submit(text):
                if self.state is not RuntimeState.WAITING_USER_INPUT:
                    self.request = text
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

    def transcribe_clip(self, clip):
        """Worker thread: the text goes back to Swift, which submits it like typed input."""
        text, problem = "", None
        try:
            if not self.voice_key:
                problem = "음성 명령에는 Groq API 키(GROQ_API_KEY)가 필요합니다."
            else:
                text = transcribe(clip, self.voice_key)
                if not text:
                    problem = "음성을 알아듣지 못했어요. 다시 말해 주세요."
        except Exception as exc:
            problem = f"음성을 글로 바꾸지 못했습니다 ({type(exc).__name__})."
        finally:
            try:
                clip.unlink(missing_ok=True)
            except OSError:
                pass
        with self.voice_lock:  # a cancel is either seen here or arrives after the result
            if self.voice_cancelled:
                text, problem = "", "음성 명령을 취소했습니다."
            self.emit({"type": "transcript", "text": text, "error": problem})
            self.transcribing = False

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
    purge_stale()
    bridge = NativeBridge(worker, emit, config.voice_api_key)
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
        if bridge.voice_thread:
            bridge.voice_thread.join(timeout=5)
        worker.shutdown()  # waits for cancellation and durable deletion/Undo cleanup
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
