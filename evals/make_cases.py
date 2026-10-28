"""Make harder variants of the hand-written tasks and keep only the ones that stay valid.

uv run python evals/make_cases.py            # levels 1-3 into evals/generated/L<n>/
uv run python evals/make_cases.py --level 2

A variant changes the situation, never the answer: the request is reworded, unrelated files pile up,
look-alike decoys appear. The task's scripted solution is replayed on every variant; one that no
longer passes is dropped (the change made the task ambiguous or changed what is correct), so a
kept variant is a case the harness can grade. Run the real model on them with
`run_eval.py --live --dir evals/generated/L2`.
"""

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_eval  # noqa: E402

OUT = Path(__file__).with_name("generated")
NOISE = {1: 0, 2: 20, 3: 200}  # unrelated files per level
DECOYS = {1: False, 2: True, 3: True}
REWORD = (
    lambda r: f"저기, {r}",
    lambda r: r.removesuffix("줘") + "주세요" if r.endswith("해줘") else f"{r} 부탁해",
    lambda r: f"{r} 빨리",
)


def pick(task_id: str, count: int) -> int:
    """Same task, same choice, so regenerating never reshuffles the set."""
    return int(hashlib.sha256(task_id.encode()).hexdigest(), 16) % count


def mutate(task: dict, level: int) -> dict:
    out = json.loads(json.dumps(task))
    out["id"] = f"{task['id']}~L{level}"
    out["base"] = task["id"]
    out["request"] = REWORD[pick(task["id"], len(REWORD))](task["request"])
    for i in range(NOISE[level]):
        out["files"][f"zz_noise/n{i:03d}.txt"] = f"unrelated {i}"
    if DECOYS[level]:
        for name in task["files"]:
            if Path(name).name in task["request"]:
                path = Path(name)
                out["files"][path.with_name(f"{path.stem}_old{path.suffix}").as_posix()] = "decoy"
    return out


async def build(levels: list[int]) -> dict[int, tuple[int, list[str]]]:
    """level -> (kept count, dropped ids); only tasks that replay in a throwaway folder qualify."""
    base = [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_eval.TASKS.glob("*.json"))
    ]
    base = [t for t in base if "script" in t and not t.get("negative")]
    result = {}
    for level in levels:
        folder = OUT / f"L{level}"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        kept, dropped = 0, []
        for task in base:
            variant = mutate(task, level)
            trial = await run_eval.run_trial(variant, None, [])
            if trial["passed"]:
                path = folder / f"{variant['id']}.json"
                path.write_text(
                    json.dumps(variant, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                kept += 1
            else:
                dropped.append(variant["id"])
        result[level] = (kept, dropped)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--level", type=int, choices=sorted(NOISE), action="append")
    args = parser.parse_args(argv)
    for level, (kept, dropped) in asyncio.run(build(args.level or sorted(NOISE))).items():
        print(f"L{level}: kept {kept}, dropped {len(dropped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
