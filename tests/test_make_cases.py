"""evals/make_cases.py: variants change the situation, never the expected answer."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
spec = importlib.util.spec_from_file_location("make_cases", ROOT / "evals" / "make_cases.py")
make_cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_cases)

TASK = {
    "id": "t",
    "files": {"a.txt": "x"},
    "request": "a.txt 지워줘",
    "expect": {"files": {"a.txt": None}},
    "script": [],
}


def test_level_1_only_rewords():
    out = make_cases.mutate(TASK, 1)
    assert out["files"] == TASK["files"] and out["request"] != TASK["request"]
    assert out["expect"] == TASK["expect"] and out["base"] == "t"


def test_level_3_adds_noise_and_decoy_without_touching_the_original():
    out = make_cases.mutate(TASK, 3)
    assert len(out["files"]) == 1 + 200 + 1 and "a_old.txt" in out["files"]
    assert out["expect"] == TASK["expect"] and TASK["files"] == {"a.txt": "x"}


def test_same_task_same_variant():
    assert make_cases.mutate(TASK, 2) == make_cases.mutate(TASK, 2)
