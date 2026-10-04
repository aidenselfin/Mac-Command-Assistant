"""Interactive acceptance after the 2026-10-04 review fixes (review section 6, step 5).

Run from the repository root: .venv/bin/python docs/reviews/acceptance_after_fixes.py
Real stdin pipe and real Filesystem MCP; scripted LLM and a slow fake analyzer. Only temporary
files are used, no API key is read and no external LLM request is made. No UI.
"""

import asyncio
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import main  # noqa: E402
from voice_action.config import Config  # noqa: E402
from voice_action.feedback import FeedbackLoop  # noqa: E402
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient  # noqa: E402
from voice_action.mcp_manager import MCPManager  # noqa: E402
from voice_action.planner import ModelTurn, PlannerError, ToolCall  # noqa: E402
from voice_action.undo import UndoJournal  # noqa: E402

SOURCE_BODY = "SOURCE_BODY_MARKER"


class Model:
    def __init__(self, turns):
        self.turns = list(turns)

    async def generate(self, messages, tools, **kwargs):
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return deepcopy(turn)


class SlowAnalyzer:
    def __init__(self):
        self.bundles = []

    async def analyze(self, rule, bundle):
        self.bundles.append(bundle)
        await asyncio.sleep(2)  # long enough for the next command to run meanwhile
        return {"action": "dev_report", "root_cause": "scripted", "category": "provider",
                "confidence": 0.9}

    async def close(self):
        pass


def call(name, **args):
    return ModelTurn(None, [ToolCall("call", name, json.dumps(args))])


def run(root: Path) -> None:
    for name, text in {
        "source.txt": SOURCE_BODY, "overwrite.txt": "before", "delete.txt": "restore me",
        "conflict.txt": "reviewed",
    }.items():
        (root / name).write_text(text)
    config = Config("openai", "scripted", "synthetic-key", (root,))
    analyzer, loops = SlowAnalyzer(), []

    def feedback(_config, emit):
        loops.append(FeedbackLoop(root / ".fb" / "feedback.sqlite3", analyzer=analyzer, emit=emit))
        return loops[-1]

    model = Model([
        call("filesystem__list_directory", path=str(root)), ModelTurn("listed", []),
        call("filesystem__read_text_file", path=str(root / "source.txt")),
        call("filesystem__write_file", path=str(root / "create.txt"), content="summary"),
        ModelTurn("created", []),
        call("filesystem__write_file", path=str(root / "reject.txt"), content="reject"),
        call("filesystem__write_file", path=str(root / "overwrite.txt"), content="after"),
        ModelTurn("overwritten", []),
        call("local__delete_files", paths=[str(root / "delete.txt")]),
        call("filesystem__write_file", path=str(root / "conflict.txt"), content="planned"),
        ModelTurn("conflict written", []),
        PlannerError("synthetic repeated failure"), PlannerError("synthetic repeated failure"),
        PlannerError("synthetic repeated failure"),
        call("filesystem__list_directory", path=str(root)), ModelTurn("listed again", []),
        call("filesystem__write_file", path=str(root / "pending.txt"), content="pending"),
    ])
    output, checks = io.StringIO(), {}

    def expect(marker, after, timeout=20):
        deadline = time.monotonic() + timeout
        while marker not in output.getvalue()[after:]:
            if time.monotonic() > deadline:
                raise AssertionError(f"missing {marker!r}:\n{output.getvalue()[after:]}")
            time.sleep(0.02)

    read_fd, write_fd = os.pipe()

    def say(line, marker, check=None):
        start = len(output.getvalue())
        os.write(write_fd, (line + "\n").encode())
        expect(marker, start)
        if check:
            check()

    def feed():
        say("파일 목록 보여줘", "[DONE] listed")
        say("source.txt를 요약해 create.txt에 저장해줘", "[APPROVAL REQUIRED]",
            lambda: checks.setdefault("before approval", not (root / "create.txt").exists()))
        say("몰라", "승인 또는 거절 입력이 필요합니다")
        say("승인", "[DONE] created")
        checks["create"] = (root / "create.txt").read_text() == "summary"
        say("undo", "[DONE]")
        checks["create undo"] = not (root / "create.txt").exists()
        say("reject.txt에 reject라고 써줘", "[APPROVAL REQUIRED]")
        say("거절", "[DONE]")
        checks["reject"] = not (root / "reject.txt").exists()
        say("overwrite.txt에 after라고 써줘", "[APPROVAL REQUIRED]")
        say("승인", "[DONE] overwritten")
        checks["overwrite"] = (root / "overwrite.txt").read_text() == "after"
        say("undo", "[DONE]")
        checks["overwrite undo"] = (root / "overwrite.txt").read_text() == "before"
        say("delete.txt 파일을 삭제해줘", "삭제 대상")
        say("승인", "[DONE]")
        checks["delete"] = not (root / "delete.txt").exists()
        say("undo", "[DONE]")
        checks["delete undo"] = (root / "delete.txt").read_text() == "restore me"
        # F09: the reviewed file changes while the approval is pending.
        say("conflict.txt에 planned라고 써줘", "[APPROVAL REQUIRED]")
        (root / "conflict.txt").write_text("external edit")
        say("승인", "[APPROVAL REQUIRED]")
        checks["F09 stale approval wrote nothing"] = (
            (root / "conflict.txt").read_text() == "external edit"
        )
        say("승인", "[DONE] conflict written")
        checks["F09 fresh approval writes"] = (root / "conflict.txt").read_text() == "planned"
        for _ in range(3):
            say("실패 재현", "[ERROR]")
        expect("문제 감지 R1", 0)
        started = time.monotonic()
        say("파일 목록 보여줘", "[DONE] listed again")
        checks["next command during analysis"] = (
            time.monotonic() - started < 2
            and "분석 완료" not in output.getvalue()
        )
        say("pending.txt에 pending이라고 써줘", "[APPROVAL REQUIRED]")
        os.write(write_fd, b"exit\n")
        os.close(write_fd)

    feeder = threading.Thread(target=feed, daemon=True)
    with os.fdopen(read_fd, encoding="utf-8") as stdin, patch.object(sys, "stdin", stdin), \
            patch.object(main, "load_config", return_value=config), \
            patch.object(main, "_build_undo_journal",
                         return_value=UndoJournal(root / "undo.sqlite3", (root,))), \
            patch.object(main, "build_feedback", side_effect=feedback), \
            patch.object(main, "OpenAICompatibleModel", return_value=model), \
            patch.object(main, "_build_mcp_manager", side_effect=lambda c: MCPManager({
                "filesystem": MCPClient(c, allowed_tools=SUPPORTED_TOOLS)})), \
            redirect_stdout(output):
        feeder.start()
        code = main.main(["--interactive"])
    feeder.join(timeout=5)
    checks["exit code 0"] = code == 0
    checks["pending write not executed on exit"] = not (root / "pending.txt").exists()
    checks["all scripted turns used"] = not model.turns
    checks["analysis finished before exit"] = "분석 완료 R1" in output.getvalue()
    r1 = [b for b in analyzer.bundles if b.startswith("signature: error:")]
    checks["R1 evidence excludes the later command"] = bool(r1) and "listed again" not in r1[0]
    db = sqlite3.connect(root / ".fb" / "feedback.sqlite3")
    stored = "\n".join(p for (p,) in db.execute("SELECT payload FROM events"))
    checks["file body not stored"] = SOURCE_BODY not in stored
    db.close()
    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
    if not all(checks.values()):
        print(output.getvalue())
        raise SystemExit(1)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="voice-action-acceptance-") as temporary:
        run(Path(temporary).resolve())
