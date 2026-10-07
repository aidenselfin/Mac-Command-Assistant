"""Run JSON tasks through the real service path (RuntimeController) in throwaway folders.

uv run python evals/run_eval.py                       # every task, scripted model turns
uv run python evals/run_eval.py --live --repeat 3     # model from config (costs API calls)
uv run python evals/run_eval.py --live --lesson "..." # same, with one candidate lesson

Grading checks state and side effects only (files, approvals, outcome status, required tools
run), not the quality of a summary. Negative "control" tasks validate the grader with
scripted turns and are left out of --live runs.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import error_message  # noqa: E402
from voice_action.config import Config, load_config  # noqa: E402
from voice_action.feedback import FeedbackLoop  # noqa: E402
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient  # noqa: E402
from voice_action.mcp_manager import MCPManager  # noqa: E402
from voice_action.planner import (  # noqa: E402
    ApprovalRequired,
    Clarification,
    ClarificationRequest,
    Completed,
    ModelTurn,
    Planner,
    PlannerEventType,  # noqa: E402
    ToolCall,
)
from voice_action.runtime import RuntimeController  # noqa: E402
from voice_action.undo import UndoJournal  # noqa: E402

TASKS = Path(__file__).with_name("tasks")
RESULTS = Path(__file__).with_name("results")
SKIP = {".eval"}  # harness-owned files inside the trial root


class Scripted:
    """Replays model turns from the task; running out is a harness/task defect, not an agent one."""

    def __init__(self, turns, root: Path):
        self.turns = [json.loads(json.dumps(t).replace("{root}", str(root))) for t in turns]
        self.calls = 0

    async def generate(self, messages, tools, **kwargs):
        self.calls += 1
        if not self.turns:
            raise LookupError("scripted turns exhausted")
        turn = self.turns.pop(0)
        calls = [
            ToolCall(f"c{self.calls}_{i}", c["name"], json.dumps(c["args"]))
            for i, c in enumerate(turn.get("calls", []))
        ]
        ask = turn.get("ask")  # {"question", "options", "kind": file_candidate|edit_criteria}
        clarification = ask and ClarificationRequest(
            ask["question"], tuple(ask["options"]), ask["kind"], f"c{self.calls}"
        )
        return deepcopy(ModelTurn(turn.get("text"), calls, clarification))


class Counting:
    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        return await self.inner.generate(*args, **kwargs)


def manifest(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] not in SKIP
    }


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def expected_manifest(initial: dict, files: dict) -> dict:
    """Initial state plus the task's declared changes; everything else must stay as it was."""
    expected = dict(initial)
    for path, content in files.items():
        if content is None:
            expected.pop(path, None)
        else:
            expected[path] = "*" if content == "*" else sha(content)
    return expected


def diff(expected: dict, actual: dict) -> list[str]:
    problems = []
    for path in sorted(expected.keys() | actual.keys()):
        want, got = expected.get(path), actual.get(path)
        if want == "*" and got is not None:
            continue
        if want != got:
            kind = "missing" if got is None else "unexpected" if want is None else "content"
            problems.append(f"{path}: {kind}")
    return problems


async def run_trial(task: dict, config: Config | None, lessons: list[str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="va-eval-") as temporary:
        root = Path(temporary).resolve()  # noqa: ASYNC240 - trials run one at a time
        for path, content in task["files"].items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(content, encoding="utf-8")
        initial = manifest(root)
        client = None
        if config is None:
            trial_config = Config("openai", "scripted", "unused", (root,))
            model = Scripted(task["script"], root)
        else:
            trial_config = replace(config, allowed_directories=(root,), feedback_enabled=False)
            inner, client = live_model(trial_config)  # closed in finally, once per trial
            model = Counting(inner)
        feedback = None
        if lessons:  # throwaway store: trial lessons never reach ~/.voice-action
            feedback = FeedbackLoop(root / ".eval" / "feedback.sqlite3")
            with feedback.db:
                feedback.db.executemany(
                    "INSERT INTO lessons (signature, text, created_ts, last_seen_ts) "
                    "VALUES ('eval', ?, 0, 1e12)",
                    [(t,) for t in lessons],
                )
        undo = UndoJournal(root / ".eval" / "undo.sqlite3", (root,))
        manager = MCPManager({"filesystem": MCPClient(trial_config, allowed_tools=SUPPORTED_TOOLS)})
        user = task.get("user", {})
        answers, decisions = list(user.get("clarify", [])), list(user.get("approve", []))
        violations, status, text, tools_run = [], None, "", []
        changed: list[str] = []  # what the final Completed reports as written
        started = time.monotonic()
        try:
            async with manager.connect():
                planner = Planner(
                    trial_config,
                    manager,
                    feedback.wrap(model) if feedback else model,
                    tools=await manager.list_tools(),
                    undo_journal=undo,
                )
                controller = RuntimeController(
                    planner,
                    feedback=feedback,
                    planner_event_observer=lambda event: tools_run.append(event.tool_name)
                    if event.type is PlannerEventType.TOOL_STARTED
                    else None,
                )

                async def converse():
                    if task.get("attach"):  # a file dropped on the window before the command
                        controller.attach(str(root / task["attach"]))
                    outcome = await controller.submit(task["request"])
                    while True:
                        if isinstance(outcome, Clarification):
                            if not answers:
                                return (
                                    "simulator_failure",
                                    f"unscripted question: {outcome.question}",
                                )
                            outcome = await controller.submit(answers.pop(0))
                        elif isinstance(outcome, ApprovalRequired):
                            if manifest(root) != initial:
                                violations.append("changed before approval")
                            if not decisions:
                                return (
                                    "simulator_failure",
                                    f"unscripted approval: {outcome.target_path}",
                                )
                            outcome = await (
                                controller.approve_pending()
                                if decisions.pop(0)
                                else controller.reject_pending()
                            )
                        else:
                            break
                    if isinstance(outcome, Completed):
                        changed.extend(
                            Path(path).relative_to(root).as_posix()
                            for path in outcome.changed_paths
                        )
                    if isinstance(outcome, Completed) and task["expect"].get("undo"):
                        outcome = await controller.undo_last_write()
                    return (
                        "completed" if isinstance(outcome, Completed) else "error",
                        getattr(outcome, "text", None) or getattr(outcome, "message", ""),
                    )

                status, text = await asyncio.wait_for(converse(), task.get("timeout", 120))
        except TimeoutError:
            status = "timeout"
        except Exception as exc:  # MCP's TaskGroup wraps errors in an ExceptionGroup
            inner = exc
            while isinstance(inner, ExceptionGroup):
                inner = inner.exceptions[0]
            # Exhausted script = broken task/harness; anything else here is provider/network.
            status = "harness_failure" if isinstance(inner, LookupError) else "provider_failure"
            # Same redaction as the app: SDK errors can carry request bodies and headers.
            text = error_message(inner)
        finally:
            if feedback is not None:
                await feedback.close()
            if client is not None:
                await client.close()
        expect = task["expect"]
        files = {} if expect.get("undo") else expect.get("files", {})
        problems = diff(expected_manifest(initial, files), manifest(root))
        problems += [f"{name}: not run" for name in expect.get("must_run", [])
                     if name not in tools_run]
        if "changed_paths" in expect and sorted(changed) != sorted(expect["changed_paths"]):
            problems.append(f"changed_paths: {sorted(changed)} != {sorted(expect['changed_paths'])}")
        valid = status in {"completed", "error"}
        passed = (
            valid
            and not problems
            and not violations
            and status == expect.get("status", "completed")
        )
        if task.get("negative"):  # deliberately wrong run: the grader must reject it
            passed = valid and not passed
        return {
            "task": task["id"],
            "family": task.get("family"),
            "status": status,
            "passed": passed,
            "valid": valid,
            "problems": problems,
            "violations": violations,
            "model_calls": model.calls,
            "tools_run": tools_run,
            "graded": "state_and_side_effects",
            "seconds": round(time.monotonic() - started, 2),
            "reply": text[:200],
        }


def live_model(config: Config):
    from openai import AsyncOpenAI

    from voice_action.planner import OpenAICompatibleModel

    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.llm_timeout_seconds,
        max_retries=0,
    )
    return OpenAICompatibleModel(client, config.model), client


def code_files() -> list[Path]:
    """Everything that decides a trial's behavior, for the results' code_hash."""
    return sorted(
        [*(ROOT / "voice_action").glob("*.py"), ROOT / "main.py", Path(__file__).resolve()]
    )


def versions(config: Config | None, lessons: list[str], tasks: list[dict]) -> dict:
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()

    code = hashlib.sha256()
    for path in code_files():
        code.update(path.read_bytes())
    return {
        "commit": git("rev-parse", "--short", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "code_hash": code.hexdigest()[:12],
        "model": config.model if config else "scripted",
        "lessons": [sha(t)[:12] for t in lessons],
        "tasks": {t["id"]: sha(json.dumps(t, sort_keys=True))[:12] for t in tasks},
    }


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", action="append", help="task id (default: all)")
    parser.add_argument("--live", action="store_true", help="use the configured LLM")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--lesson", action="append", default=[])
    args = parser.parse_args(argv)
    tasks = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(TASKS.glob("*.json"))]
    tasks = [t for t in tasks if not args.task or t["id"] in args.task]
    if not args.live:
        tasks = [t for t in tasks if "script" in t]
    else:  # controls pass only when a run goes wrong; they would invert a live score
        tasks = [t for t in tasks if not t.get("negative")]
    config = load_config() if args.live else None
    trials = [await run_trial(t, config, args.lesson) for _ in range(args.repeat) for t in tasks]
    for r in trials:
        mark = "PASS" if r["passed"] else ("FAIL" if r["valid"] else "INVALID")
        print(
            f"{mark:7} {r['task']:28} {r['status']:17} calls={r['model_calls']} "
            f"{'; '.join(r['problems'] + r['violations'])}"
        )
    valid = [r for r in trials if r["valid"]]
    print(
        f"\npassed {sum(r['passed'] for r in trials)}/{len(trials)} "
        f"(invalid {len(trials) - len(valid)})"
    )
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / time.strftime("%Y%m%d-%H%M%S.json")
    out.write_text(
        json.dumps(
            {"versions": versions(config, args.lesson, tasks), "trials": trials},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"saved {out.relative_to(ROOT)}")
    return 0 if all(r["passed"] for r in trials) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
