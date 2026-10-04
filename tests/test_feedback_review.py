"""Regressions for docs/reviews/2026-10-04-claude-code-review.md (expected behavior after fixes)."""

import asyncio
import json
import os
import sqlite3
import stat
import time
from dataclasses import replace

import pytest

from tests.test_feedback import LESSON, FakeAnalyzer, FakeModel, make_loop
from voice_action.feedback import FeedbackLoop, build_feedback, scrub
from voice_action.planner import Completed, Error, ModelTurn, PlannerError, ToolCall
from voice_action.runtime import RuntimeController

SECRET = 'syn"the\\tic\nsecret-key'  # quotes, backslash and newline survive JSON escaping
BODY = "PRIVATE_BODY_MARKER"


def all_stored_text(loop) -> str:
    rows = [
        *loop.db.execute("SELECT signature, payload FROM events"),
        *loop.db.execute("SELECT signature, rule FROM incidents"),
        *loop.db.execute("SELECT signature, text FROM lessons"),
    ]
    report = loop.report_path.read_text() if loop.report_path.exists() else ""
    return "\n".join(str(v) for row in rows for v in row) + report


# ---- F01/F02 safe recording --------------------------------------------------------------


async def test_secrets_and_file_bodies_are_not_stored_or_sent(tmp_path):
    loop, emitted = make_loop(tmp_path, secrets=(SECRET,))
    loop.user_input(f"{SECRET} Bearer abc.def-ghi 저장해줘", new_run=True)
    arguments = json.dumps({"path": "/a/b.txt", "content": BODY})
    write = ToolCall("1", "filesystem__write_file", arguments)
    model = loop.wrap(FakeModel([ModelTurn("x" * 400 + BODY, [write])]))
    await model.generate(
        [
            {"role": "user", "content": SECRET},
            {"role": "tool", "content": BODY + "y" * 600},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {"name": "filesystem__write_file", "arguments": arguments},
                    }
                ],
            },
        ],
        [],
        allow_tools=True,
    )
    loop.outcome(Completed("z" * 400 + BODY))
    stored = all_stored_text(loop) + loop._bundle("sig", (loop.run_id,))
    for marker in (SECRET, "abc.def-ghi", BODY):
        assert marker not in stored, marker
    for (payload,) in loop.db.execute("SELECT payload FROM events"):
        json.loads(payload)  # never corrupted by redaction
    assert all(SECRET not in m for m in emitted)


def test_bearer_redaction_keeps_json_valid():
    for payload in (
        {"text": "Bearer tok"},
        {"text": "a Bearer tok b"},
        {"messages": [{"content": "Bearer tok"}]},
    ):
        text = json.dumps(scrub(payload, ()), ensure_ascii=False)
        assert "tok" not in text and json.loads(text)


def test_corrupted_row_does_not_hide_valid_detection(tmp_path):
    loop, _ = make_loop(tmp_path)
    for _ in range(3):
        loop.clock.now += 700
        loop.user_input("목록", new_run=True)
        loop.db.execute(
            "INSERT INTO events (ts, session_id, run_id, kind, signature, payload) "
            "VALUES (?, 's', ?, 'llm_response', NULL, '{\"broken')",
            (loop.clock.now, loop.run_id),
        )
        loop.outcome(Error("같은 실패"))
        found = loop.run_finished()
    assert [rule for rule, *_ in found] == ["R1"]


async def test_analysis_result_is_sanitized_once_for_every_sink(tmp_path):
    result = {
        **LESSON,
        "lesson": f"키 {SECRET}를 쓰세요",
        "user_hint": f"힌트 {SECRET}",
        "root_cause": f"원인 {SECRET}",
    }
    loop, emitted = make_loop(tmp_path, FakeAnalyzer(result), secrets=(SECRET,))
    loop.user_input("x", new_run=True)
    loop._queue("R4", "reject:tool")
    await asyncio.gather(*loop._tasks)
    assert loop.active_lessons() == []  # a lesson that carried a credential is refused
    assert SECRET not in all_stored_text(loop)
    assert all(SECRET not in m for m in emitted)


# ---- F03 isolation, F07 lock, F08 symlinks -----------------------------------------------


def broken_emit(_text):
    raise RuntimeError("output failure")


class OkPlanner:
    def set_event_observer(self, observer):
        pass

    async def run(self, text):
        return Completed("ok")


async def test_feedback_failure_never_breaks_commands(tmp_path):
    loop = FeedbackLoop(tmp_path / "fb.sqlite3", emit=broken_emit)
    loop.db.close()  # every write now fails, and so does the warning output
    controller = RuntimeController(OkPlanner(), feedback=loop)
    assert await controller.submit("first") == Completed("ok")
    assert await controller.submit("second") == Completed("ok")


async def test_recording_failure_does_not_hide_model_result_or_error(tmp_path):
    loop = FeedbackLoop(tmp_path / "fb.sqlite3", emit=broken_emit)
    unsummarizable = [object()]  # summarizing this raises inside the feedback code
    turn = ModelTurn("ok", [])
    assert await loop.wrap(FakeModel([turn])).generate(unsummarizable, []) is turn
    with pytest.raises(PlannerError):
        await loop.wrap(FakeModel([PlannerError("LLM 인증 실패")])).generate([], [])


async def test_close_releases_resources_even_when_output_fails(tmp_path):
    class SlowAnalyzer:
        closed = False

        async def analyze(self, rule, bundle):
            await asyncio.Event().wait()

        async def close(self):
            SlowAnalyzer.closed = True

    loop = FeedbackLoop(tmp_path / "fb.sqlite3", analyzer=SlowAnalyzer(), emit=broken_emit)
    loop.user_input("x", new_run=True)
    loop._queue("R4", "reject:tool")
    await loop.close(wait_seconds=0)
    assert SlowAnalyzer.closed and not loop._tasks
    with pytest.raises(sqlite3.ProgrammingError):
        loop.db.execute("SELECT 1")


def test_analyzer_construction_failure_keeps_recording(config, monkeypatch, tmp_path):
    def fail(*_args, **_kwargs):
        raise RuntimeError("bad client")

    monkeypatch.setattr("voice_action.feedback.OpenRouterAnalyzer", fail)
    loop = build_feedback(replace(config, feedback_api_key="k"), lambda _: None)
    assert loop is not None and loop.analyzer is None
    loop.db.close()


def test_locked_database_does_not_block_recording(tmp_path):
    emitted = []
    loop = FeedbackLoop(tmp_path / "fb.sqlite3", emit=emitted.append)
    other = sqlite3.connect(tmp_path / "fb.sqlite3")
    other.execute("BEGIN IMMEDIATE")
    started = time.perf_counter()
    loop.record("a")
    loop.record("b")
    assert time.perf_counter() - started < 0.5
    assert len(emitted) == 1  # one warning that records were dropped
    other.rollback()
    other.close()
    loop.db.close()


def test_store_refuses_symlinks_and_keeps_logs_private(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    store = tmp_path / "store"
    store.mkdir()
    (store / "feedback.sqlite3").symlink_to(outside)
    with pytest.raises(OSError):
        FeedbackLoop(store / "feedback.sqlite3")
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(tmp_path / "real", target_is_directory=True)
    (tmp_path / "real").mkdir()
    with pytest.raises(OSError):
        FeedbackLoop(linked_parent / "feedback.sqlite3")

    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "feedback-report.md").symlink_to(outside)
    loop = FeedbackLoop(clean / "feedback.sqlite3")
    loop._report("sig", "cause", "detail")  # refused, and the command path must not see it
    assert outside.read_text() == "original"
    assert stat.S_IMODE(os.stat(clean / "feedback.sqlite3").st_mode) == 0o600
    loop.db.close()
    loop = FeedbackLoop(tmp_path / "fresh" / "feedback.sqlite3")
    loop._report("sig", "cause", "detail")
    assert stat.S_IMODE(os.stat(loop.report_path).st_mode) == 0o600
    loop.db.close()


# ---- F04 incident context ----------------------------------------------------------------


class RecordingAnalyzer:
    def __init__(self, result=None):
        self.bundles = []
        self.result = result or {
            "action": "dev_report", "root_cause": "r", "category": "code_bug", "confidence": 0.9
        }

    async def analyze(self, rule, bundle):
        self.bundles.append(bundle)
        return self.result

    async def close(self):
        pass


async def test_analysis_stays_with_its_origin_run(tmp_path):
    analyzer = RecordingAnalyzer()
    loop, _ = make_loop(tmp_path, analyzer)
    loop.user_input("ORIGIN_RUN", new_run=True)
    origin = loop.run_id
    loop._queue("R4", "reject:tool", (origin,))
    loop.user_input("UNRELATED_NEXT_RUN", new_run=True)  # starts before the task runs
    await asyncio.gather(*loop._tasks)
    assert "ORIGIN_RUN" in analyzer.bundles[0]
    assert "UNRELATED_NEXT_RUN" not in analyzer.bundles[0]
    assert "signature: reject:tool" in analyzer.bundles[0]
    (run,) = loop.db.execute("SELECT run_id FROM events WHERE kind='feedback'").fetchone()
    assert run == origin and origin in loop.report_path.read_text()


async def test_repeated_error_across_sessions_keeps_all_evidence(tmp_path):
    analyzer = RecordingAnalyzer()
    for index in range(3):
        loop = FeedbackLoop(tmp_path / "fb.sqlite3", analyzer=analyzer)
        loop.user_input(f"SESSION_{index}", new_run=True)
        loop.outcome(Error("repeated synthetic failure"))
        found = loop.run_finished()
        await loop.close()
    assert "R1" in [rule for rule, _ in found]
    (r1_bundle,) = [b for b in analyzer.bundles if b.startswith("signature: error:")]
    assert all(f"SESSION_{index}" in r1_bundle for index in range(3))


def test_bundle_is_cut_on_whole_records(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_action.feedback.BUNDLE_CHARS", 300)
    loop, _ = make_loop(tmp_path)
    loop.user_input("x", new_run=True)
    for index in range(20):
        loop.record("outcome", text=f"line {index}")
    bundle = loop._bundle("sig", (loop.run_id,))
    lines = bundle.split("\n\n")[0].splitlines()[2:]
    assert lines and all(json.loads(line) for line in lines)
    assert "line 19" in bundle and "line 0\"" not in bundle
    assert "뺀 오래된 기록" in bundle
    loop.db.close()
