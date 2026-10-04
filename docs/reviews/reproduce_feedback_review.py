"""Review evidence, not regression tests: assert the reviewed defects still exist.

Run from the repository root: .venv/bin/python docs/reviews/reproduce_feedback_review.py
Only synthetic data and temporary files are used. No external LLM request or UI.
"""

import asyncio
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import main  # noqa: E402
from voice_action.config import Config  # noqa: E402
from voice_action.feedback import FeedbackLoop  # noqa: E402
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient, ToolResult  # noqa: E402
from voice_action.mcp_manager import MCPManager  # noqa: E402
from voice_action.planner import Completed, Error, ModelTurn, PlannerError, ToolCall  # noqa: E402
from voice_action.risk_guard import RiskGuard  # noqa: E402
from voice_action.runtime import Busy, RuntimeController  # noqa: E402
from voice_action.undo import UndoJournal  # noqa: E402


class Model:
    def __init__(self, turns):
        self.turns, self.seen = list(turns), []

    async def generate(self, messages, tools, **kwargs):
        self.seen.append(deepcopy(messages))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class Analyzer:
    def __init__(self):
        self.bundles = []

    async def analyze(self, rule, bundle):
        self.bundles.append(bundle)
        return {"action": "dev_report", "root_cause": "synthetic review", "category": "code_bug",
                "confidence": 0.9}

    async def close(self):
        pass


def observed(name):
    print(f"OBSERVED {name}")


async def defects(root):
    secret = "syntheticsecretletters"
    loop = FeedbackLoop(root / "privacy.sqlite3", secrets=(secret,))
    loop.user_input(secret, new_run=True)
    signature, payload = loop.db.execute("SELECT signature, payload FROM events").fetchone()
    assert secret in signature and secret not in payload
    observed("F01 signature bypasses secret redaction")
    marker = "PRIVATE_FILE_MARKER"
    model = loop.wrap(Model([ModelTurn(None, [ToolCall("c", "filesystem__write_file",
                                json.dumps({"path": "/tmp/out.txt", "content": marker}))])]))
    await model.generate([{"role": "tool", "content": marker}], [])
    assert marker in loop._bundle()
    observed("F01 file body and write content enter analysis bundle")
    loop.user_input("Bearer synthetictoken", new_run=True)
    (payload,) = loop.db.execute("SELECT payload FROM events ORDER BY id DESC LIMIT 1").fetchone()
    try:
        json.loads(payload)
    except json.JSONDecodeError:
        observed("F02 Bearer redaction corrupts JSON")
    else:
        raise AssertionError("Expected malformed JSON")
    assert loop.run_finished() == []
    await loop.close()

    emitted = []
    loop = FeedbackLoop(root / "lesson.sqlite3", secrets=(secret,), emit=emitted.append)
    loop._apply("synthetic", {"action": "lesson", "lesson": f"참고 문자열 {secret}",
                "user_hint": secret, "confidence": 0.9, "root_cause": "synthetic"})
    assert secret in loop.active_lessons()[0][1] and any(secret in x for x in emitted)
    observed("F01 analyzer output leaks via lesson and hint")
    loop.user_input("current run", new_run=True)
    model = Model([ModelTurn("a", []), ModelTurn("b", [])])
    wrapped = loop.wrap(model)
    messages = [{"role": "system", "content": "base"}]
    await wrapped.generate(messages, [])
    loop._add_lesson("new", "MID_RUN_LESSON")
    await wrapped.generate(messages, [])
    assert "MID_RUN_LESSON" not in model.seen[0][0]["content"]
    assert "MID_RUN_LESSON" in model.seen[1][0]["content"]
    observed("F05 lessons change inside one run")
    loop._add_lesson("never_applied", "NEVER_APPLIED_LESSON")
    for _ in range(3):
        loop._queue("R1", "never_applied")
    assert all("NEVER_APPLIED_LESSON" != t for _, t in loop.active_lessons())
    observed("F06 recurrence retires a lesson never sent to the model")
    await loop.close()

    analyzer = Analyzer()
    loop = FeedbackLoop(root / "context.sqlite3", analyzer=analyzer)
    loop.user_input("ORIGIN_RUN", new_run=True)
    original = loop.run_id
    loop._queue("R1", "error:origin")
    loop.user_input("UNRELATED_NEXT_RUN", new_run=True)
    next_run = loop.run_id
    await asyncio.gather(*loop._tasks)
    (recorded,) = loop.db.execute("SELECT run_id FROM events WHERE kind='feedback'").fetchone()
    assert original != next_run == recorded
    assert "UNRELATED_NEXT_RUN" in analyzer.bundles[0]
    assert next_run in loop.report_path.read_text()
    observed("F04 analysis event/report use next run and bundle includes next input")
    await loop.close()

    path = root / "sessions.sqlite3"
    for index in range(3):
        loop = FeedbackLoop(path)
        loop.user_input(f"SESSION_{index}", new_run=True)
        loop.outcome(Error("repeated synthetic failure"))
        found = loop._detect()
        if index == 2:
            assert any(rule == "R1" for rule, _ in found)
            assert "SESSION_0" not in loop._bundle() and "SESSION_1" not in loop._bundle()
            observed("F04 cross-session detection omits earlier incident evidence")
        await loop.close()

    def broken_emit(_):
        raise RuntimeError("synthetic output failure")

    class Planner:
        def set_event_observer(self, observer):
            pass

        async def run(self, text):
            return Completed("ok")

    loop = FeedbackLoop(root / "isolation.sqlite3", emit=broken_emit)
    loop.db.close()
    controller = RuntimeController(Planner(), feedback=loop)
    try:
        await controller.submit("first")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected output failure to escape feedback")
    assert isinstance(await controller.submit("second"), Busy)
    observed("F03 feedback failure before try leaves runtime permanently Busy")

    loop = FeedbackLoop(root / "lock.sqlite3")
    loop.db.execute("PRAGMA busy_timeout=150")
    other = sqlite3.connect(root / "lock.sqlite3")
    other.execute("BEGIN IMMEDIATE")
    started = time.perf_counter()
    loop.record("synthetic")
    assert time.perf_counter() - started >= 0.1
    other.rollback()
    other.close()
    observed("F07 synchronous feedback DB lock blocks event loop")
    await loop.close()

    target = root / "outside-report.txt"
    target.write_text("original")
    report_dir = root / "links"
    report_dir.mkdir()
    (report_dir / "feedback-report.md").symlink_to(target)
    loop = FeedbackLoop(report_dir / "feedback.sqlite3")
    loop._report("synthetic", "cause", "detail")
    assert "synthetic" in target.read_text()
    observed("F08 report follows symlink and modifies another file")
    await loop.close()

    class Gateway:
        async def normalize_tool_arguments(self, name, arguments):
            return deepcopy(arguments)

        async def call_tool(self, name, arguments):
            await asyncio.to_thread(Path(arguments["path"]).write_text, arguments["content"])
            return ToolResult(True, "ok")

    target = root / "changed.txt"
    target.write_text("approved old version")
    guard = RiskGuard(Gateway(), UndoJournal(root / "undo.sqlite3", (root,)))
    prepared = await guard.prepare_write("filesystem__write_file",
                                         {"path": str(target), "content": "planned output"})
    target.write_text("external new version")
    await guard.execute_approved(prepared)
    assert target.read_text() == "planned output"
    observed("F09 overwrite content changed during approval without reapproval")

    class SlowAnalyzer(Analyzer):
        async def analyze(self, rule, bundle):
            self.bundles.append(bundle)
            await asyncio.Event().wait()

    analyzer = SlowAnalyzer()
    loop = FeedbackLoop(root / "budget.sqlite3", analyzer=analyzer, daily_limit=1)
    loop.user_input("budget origin", new_run=True)
    loop._queue("R1", "error:first")
    await asyncio.sleep(0)
    assert len(analyzer.bundles) == 1
    await loop.close(wait_seconds=0)
    loop = FeedbackLoop(root / "budget.sqlite3", analyzer=analyzer, daily_limit=1)
    loop.user_input("budget next", new_run=True)
    loop._queue("R1", "error:second")
    await asyncio.sleep(0)
    assert len(analyzer.bundles) == 2
    observed("F10 cancelled analysis frees daily budget despite started requests")
    await loop.close(wait_seconds=0)


def interactive(root):
    root.mkdir()
    (root / "source.txt").write_text("source")
    (root / "overwrite.txt").write_text("before")
    (root / "delete.txt").write_text("restore me")
    config = Config("openai", "scripted-review", "synthetic-key", (root,))
    journal = UndoJournal(root / "undo.sqlite3", (root,))
    analyzer = Analyzer()
    loops = []

    def feedback(config, emit):
        loop = FeedbackLoop(root / "feedback.sqlite3", analyzer=analyzer, emit=emit)
        loops.append(loop)
        return loop

    def call(name, **args):
        return ModelTurn(None, [ToolCall("call", name, json.dumps(args))])

    turns = [
        call("filesystem__list_directory", path=str(root)), ModelTurn("listed", []),
        call("filesystem__read_text_file", path=str(root / "source.txt")),
        call("filesystem__write_file", path=str(root / "create.txt"), content="summary"),
        ModelTurn("created", []),
        call("filesystem__write_file", path=str(root / "reject.txt"), content="reject"),
        call("filesystem__write_file", path=str(root / "overwrite.txt"), content="after"),
        ModelTurn("overwritten", []),
        call("local__delete_files", paths=[str(root / "delete.txt")]),
        PlannerError("synthetic repeated failure"), PlannerError("synthetic repeated failure"),
        PlannerError("synthetic repeated failure"),
    ]
    model = Model(turns)
    commands = ["파일 목록 보여줘", "source.txt를 요약해 create.txt에 저장해줘", "승인",
                "reject.txt에 reject라고 써줘", "거절", "overwrite.txt에 after라고 써줘",
                "승인", "undo", "delete.txt 파일을 삭제해줘", "승인", "undo",
                "실패 재현", "실패 재현", "실패 재현", "exit"]
    read_fd, write_fd = os.pipe()
    os.write(write_fd, ("\n".join(commands) + "\n").encode())
    os.close(write_fd)
    output = io.StringIO()
    with os.fdopen(read_fd, encoding="utf-8") as stdin, patch.object(sys, "stdin", stdin), \
            patch.object(main, "load_config", return_value=config), \
            patch.object(main, "_build_undo_journal", return_value=journal), \
            patch.object(main, "build_feedback", side_effect=feedback), \
            patch.object(main, "OpenAICompatibleModel", return_value=model), \
            patch.object(main, "_build_mcp_manager", side_effect=lambda c: MCPManager({
                "filesystem": MCPClient(c, allowed_tools=SUPPORTED_TOOLS)})), \
            redirect_stdout(output):
        assert main.main(["--interactive"]) == 0
    assert not model.turns, output.getvalue()
    assert (root / "create.txt").read_text() == "summary"
    assert not (root / "reject.txt").exists()
    assert (root / "overwrite.txt").read_text() == "before"
    assert (root / "delete.txt").read_text() == "restore me"
    assert "[APPROVAL REQUIRED]" in output.getvalue()
    assert analyzer.bundles
    db = sqlite3.connect(root / "feedback.sqlite3")
    assert db.execute("SELECT COUNT(*) FROM incidents WHERE status='analyzed'").fetchone()[0] > 0
    db.close()
    print("PASS interactive: real stdin pipe, real Filesystem MCP, create/approve, reject, "
          "overwrite/Undo, delete/Undo, repeated error analysis, exit")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="voice-action-review-") as temporary:
        root = Path(temporary).resolve()
        asyncio.run(defects(root))
        interactive(root / "interactive")
