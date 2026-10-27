import asyncio

from tests.test_feedback import LESSON, FakeAnalyzer, make_loop
from voice_action.autonomy import check_diff, decide, summarize


def run(**tasks):
    """tasks: name=(valid passes, valid failures)."""
    return {"tasks": {k: list(v) for k, v in tasks.items()}, "invalid": 0,
            "passed": sum(p for p, _ in tasks.values())}


def test_summarize_counts_invalid_apart_from_failures():
    trials = [
        {"task": "a", "valid": True, "passed": True},
        {"task": "a", "valid": True, "passed": False},
        {"task": "a", "valid": False, "passed": False},
    ]
    assert summarize(trials) == {"tasks": {"a": [1, 1]}, "invalid": 1, "passed": 1}


def test_decide_needs_a_clear_win_over_the_best_baseline():
    base = [run(a=(3, 0), b=(1, 2)), run(a=(3, 0), b=(2, 1))]
    assert decide(base, run(a=(3, 0), b=(3, 0))) == "adopt"
    assert decide(base, run(a=(3, 0), b=(2, 1))) == "hold"  # inside the baselines' spread
    assert decide(base, run(a=(2, 1), b=(3, 0))) == "reject"  # a never-failing task failed
    assert decide(base, {**run(a=(3, 0), b=(3, 0)), "invalid": 5}) == "hold"


def test_patches_stay_off_safety_files_and_existing_tests():
    ok = ["3\t1\tvoice_action/planner.py", "20\t0\ttests/test_new.py", "5\t0\tdocs/status.md"]
    assert check_diff(ok, {"tests/test_new.py"}) == []
    bad = [
        "1\t1\tvoice_action/risk_guard.py", "0\t4\ttests/test_planner.py",
        "1\t0\tevals/tasks/x.json", "1\t0\tmain.py", "1\t0\tconfig.json",
    ]
    assert len(check_diff(bad, set())) == 5
    assert check_diff(["2\t0\tevals/tasks/new.json"], {"evals/tasks/new.json"}) == []


async def test_analysis_starts_workers_within_budget(tmp_path):
    started = []
    loop, _ = make_loop(tmp_path, FakeAnalyzer(LESSON), launch=lambda *a: started.append(a))
    loop._queue("R4", "reject:a")
    await asyncio.gather(*loop._tasks)
    (lesson_id, _), = loop.candidate_lessons()
    assert started == [("lesson", lesson_id)]

    fix = {"root_cause": "버그", "action": "code_fix", "fix_request": "고쳐줘", "confidence": 0.9}
    loop.analyzer = FakeAnalyzer(fix)
    for sig in ("x", "y", "z", "w"):
        loop._queue("R1", f"error:{sig}")
        await asyncio.gather(*loop._tasks)
    loop._start_worker("fix", 99, "error:x")  # same signature again
    kinds = [kind for kind, _ in started]
    assert kinds.count("fix") == 3  # the daily limit cuts the fourth
    assert loop.recent_incidents()[0][4]["action"] == "code_fix"


async def test_without_a_launcher_nothing_is_started(tmp_path):
    loop, _ = make_loop(tmp_path, FakeAnalyzer(LESSON))
    loop._queue("R4", "reject:a")
    await asyncio.gather(*loop._tasks)
    assert len(loop.candidate_lessons()) == 1
    sql = "SELECT COUNT(*) FROM events WHERE kind='autonomy_start'"
    assert loop.db.execute(sql).fetchone() == (0,)
