import asyncio
import subprocess

from tests.test_feedback import LESSON, FakeAnalyzer, make_loop
from voice_action import autonomy
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


# ---- worker flows with the slow parts (paid eval, agent, git, check tool) replaced ----------


def trials(passed, failed):
    return [{"task": "a", "valid": True, "passed": i < passed} for i in range(passed + failed)]


def eval_result(passed, failed, lessons=()):
    return {"versions": {"code_hash": "h", "model": "m", "tasks": {}, "lessons": list(lessons)},
            "trials": trials(passed, failed)}


def test_lesson_job_adopts_rejects_or_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy, "_matching_baselines", lambda versions: [])
    for candidate, baseline, verdict, active in (
        ((3, 0), (2, 1), "adopt", 1), ((2, 1), (3, 0), "reject", 0), ((3, 0), (3, 0), "hold", 0),
    ):
        loop, _ = make_loop(tmp_path / verdict)
        loop._add_lesson("sig", "지침")
        (lesson_id, _), = loop.candidate_lessons()
        monkeypatch.setattr(
            autonomy, "_live_eval",
            lambda lessons, c=candidate, b=baseline: eval_result(*(c if lessons else b)),
        )
        assert autonomy.lesson_job(loop, lesson_id)["status"] == verdict
        assert len(loop.active_lessons()) == active
        assert (len(loop.candidate_lessons()) == 1) == (verdict == "hold")


class FakeGit:
    """Answers the commands fix_job runs. `script` maps a command prefix to (rc, stdout)."""

    def __init__(self, numstat, added="", **script):
        self.calls, self.checks, self.script = [], 0, script
        self.numstat, self.added = numstat, added

    def __call__(self, command, cwd, timeout):
        text = " ".join(map(str, command))
        self.calls.append(text)
        rc, out = 0, ""
        if "vibe.py" in text:
            out = "/tmp/wt\n"
        elif "diff --cached --numstat" in text:
            out = self.numstat
        elif "--diff-filter=A" in text:
            out = self.added
        elif "--abbrev-ref" in text:
            out = "main"
        elif "scripts/check.py" in text:
            self.checks += 1
            rc, out = self.script.get("checks", [(0, "")] * 9)[self.checks - 1]
        elif "merge --ff-only" in text:
            rc = self.script.get("ff", [0] * 9).pop(0)
        elif "merge main" in text:
            rc = self.script.get("pull", 0)
        elif "--is-ancestor" in text:
            rc = self.script.get("ancestor", 1)
        return subprocess.CompletedProcess(command, rc, out, "")


def fix_loop(tmp_path):
    loop, _ = make_loop(tmp_path)
    loop.db.execute("INSERT INTO incidents (id, ts, rule, signature, status, evidence) "
                    "VALUES (1, 0, 'R1', 'error:x', 'analyzed', '[]')")
    loop.record("feedback", incident=1, root_cause="원인", fix_request="고쳐줘")
    loop.db.commit()
    return loop


def run_fix(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(autonomy, "_run", fake)
    monkeypatch.setattr(autonomy, "_agent_command", lambda worktree, prompt: ["agent"])
    return autonomy.fix_job(fix_loop(tmp_path), 1)


GOOD = "3\t0\ttests/test_new.py"


def test_fix_job_merges_after_the_check_tool_passes(tmp_path, monkeypatch):
    fake = FakeGit(GOOD, "tests/test_new.py")
    assert run_fix(tmp_path, monkeypatch, fake)["status"] == "merged"
    assert fake.checks == 1 and any("tag -f autofix-pre-1" in c for c in fake.calls)


def test_fix_job_never_merges_a_protected_or_failing_patch(tmp_path, monkeypatch):
    fake = FakeGit("1\t1\tvoice_action/risk_guard.py")
    assert run_fix(tmp_path / "a", monkeypatch, fake)["status"] == "rejected"
    assert fake.checks == 0 and not any("merge" in c for c in fake.calls)

    fake = FakeGit(GOOD, "tests/test_new.py", checks=[(1, "FAIL  eval replay  x\n")])
    result = run_fix(tmp_path / "b", monkeypatch, fake)
    assert result["status"] == "rejected" and "eval replay" in result["why"]
    assert not any("merge" in c for c in fake.calls)


def test_fix_job_retries_once_after_main_moved(tmp_path, monkeypatch):
    fake = FakeGit(GOOD, "tests/test_new.py", ff=[1, 0])
    assert run_fix(tmp_path / "a", monkeypatch, fake)["status"] == "merged"
    assert fake.checks == 2  # re-checked with main merged in

    fake = FakeGit(GOOD, "tests/test_new.py", ff=[1, 1], ancestor=0)  # not about main moving
    assert run_fix(tmp_path / "b", monkeypatch, fake)["status"] == "branch_kept"
    assert fake.checks == 1

    fake = FakeGit(GOOD, "tests/test_new.py", ff=[1], pull=1)  # main conflicts
    assert run_fix(tmp_path / "c", monkeypatch, fake)["status"] == "branch_kept"
    assert any("merge --abort" in c for c in fake.calls)
