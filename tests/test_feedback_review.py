"""Regressions for docs/reviews/2026-10-04-claude-code-review.md (expected behavior after fixes)."""

import asyncio
import json

from tests.test_feedback import LESSON, FakeAnalyzer, FakeModel, make_loop
from voice_action.feedback import scrub
from voice_action.planner import Completed, Error, ModelTurn, ToolCall

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
