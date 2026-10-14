"""Stage 3 of docs/self-improvement-guide.md: analysis lessons are candidates until adopted."""

import asyncio
import sqlite3

import pytest

import main
from tests.test_feedback import LESSON, FakeAnalyzer, FakeModel, make_loop
from voice_action.feedback import LESSON_LIMIT, FeedbackLoop
from voice_action.planner import ModelTurn


async def system_prompt_for(loop, text="명령"):
    model = FakeModel([ModelTurn("ok", [])])
    loop.user_input(text, new_run=True)
    await loop.wrap(model).generate([{"role": "system", "content": "base"}], [])
    return model.seen[0][0]["content"]


async def test_analysis_lesson_is_only_a_candidate(tmp_path):
    loop, emitted = make_loop(tmp_path, FakeAnalyzer(LESSON))
    loop.user_input("x", new_run=True)
    loop._queue("R4", "reject:tool")
    await asyncio.gather(*loop._tasks)
    assert loop.active_lessons() == []
    assert [text for _, text in loop.candidate_lessons()] == [LESSON["lesson"]]
    assert any("후보 lesson" in m and "--lessons" in m for m in emitted)
    assert LESSON["lesson"] not in await system_prompt_for(loop)


async def test_adopted_lesson_is_injected_from_the_next_command(tmp_path):
    loop, _ = make_loop(tmp_path)
    loop._add_lesson("sig", "OLD")
    (old,) = [i for i, _ in loop.candidate_lessons()]
    assert loop.activate_lesson(old)
    loop._add_lesson("sig", "NEW")
    (new,) = [i for i, _ in loop.candidate_lessons()]
    assert loop.activate_lesson(new)
    assert [text for _, text in loop.active_lessons()] == ["NEW"]  # same signature replaced
    assert "NEW" in await system_prompt_for(loop)
    assert not loop.activate_lesson(old)  # an inactive one can't come back silently
    assert not loop.activate_lesson(999)


def test_activation_keeps_the_lesson_limit(tmp_path):
    loop, _ = make_loop(tmp_path)
    for index in range(LESSON_LIMIT + 1):
        loop._add_lesson(f"sig{index}", f"lesson {index}")
        (candidate,) = [i for i, _ in loop.candidate_lessons()]
        loop.activate_lesson(candidate)
    texts = [text for _, text in loop.active_lessons()]
    assert len(texts) == LESSON_LIMIT and "lesson 0" not in texts


def test_rejected_candidate_is_gone(tmp_path):
    loop, _ = make_loop(tmp_path)
    loop._add_lesson("sig", "maybe")
    (candidate,) = [i for i, _ in loop.candidate_lessons()]
    assert loop.deactivate_lesson(candidate)
    assert loop.candidate_lessons() == [] and loop.active_lessons() == []


def test_existing_store_keeps_its_active_lessons(tmp_path):
    path = tmp_path / "fb.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE lessons (id INTEGER PRIMARY KEY, signature TEXT NOT NULL, text TEXT NOT NULL,"
        " created_ts REAL NOT NULL, last_seen_ts REAL NOT NULL,"
        " recurrences INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);"
        "INSERT INTO lessons (signature, text, created_ts, last_seen_ts)"
        " VALUES ('s', 'kept', 1, 9e12);"
    )
    db.commit()
    db.close()
    loop = FeedbackLoop(path)
    assert [text for _, text in loop.active_lessons()] == ["kept"]
    assert loop.candidate_lessons() == []
    loop.db.close()


def test_cli_lists_candidates_and_adopts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "DEFAULT_DB_PATH", tmp_path / "fb.sqlite3")
    loop = FeedbackLoop(tmp_path / "fb.sqlite3")
    loop._add_lesson("sig", "후보 문장")
    (candidate,) = [i for i, _ in loop.candidate_lessons()]
    loop.db.close()
    assert main.main(["--lessons"]) == 0
    listed = capsys.readouterr().out
    assert "[후보 lesson]" in listed and f"#{candidate} 후보 문장" in listed
    assert main.main(["--lesson-on", str(candidate)]) == 0
    assert main.main(["--lesson-on", "999"]) == 1
    loop = FeedbackLoop(tmp_path / "fb.sqlite3")
    assert [text for _, text in loop.active_lessons()] == ["후보 문장"]
    loop.db.close()


@pytest.mark.parametrize("flag", ["--lesson-on", "--lesson-off"])
def test_lesson_flags_need_an_id(flag):
    with pytest.raises(SystemExit):
        main.main([flag])
