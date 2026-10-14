"""현재 결함을 단언하는 리뷰 증거. 수정 후 실패하는 것이 정상이다.

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python docs/reviews/reproduce_2026_10_07_search_rating.py
API·운영 DB·사용자 파일을 사용하지 않는다.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from voice_action.config import load_config
from voice_action.feedback import FeedbackLoop
from voice_action.file_search import _day, find_files
from voice_action.mcp_client import ToolDefinition, ToolResult
from voice_action.planner import Clarification, Completed, ModelTurn, Planner, ToolCall


async def check(base):
    base = base.resolve()
    root = base / "voice-action-test"
    root.mkdir()
    config = load_config(cwd=base, home=base, environ={"OPENAI_API_KEY": "test-secret"})
    start = datetime.fromtimestamp(_day("2026-10-07", end=False))
    assert (start.hour, start.minute) == (23, 59)
    print("F1 confirmed: modified_after starts at 23:59:00")

    (root / "budget.md").write_text("x")
    found = find_files({"keywords": ["budget"], "extensions": ["pdf"]}, (root,)).structured
    assert found["decision"] == "single" and found["filters_relaxed"]
    print("F2 confirmed: explicit PDF filter relaxed to a single MD file")

    many = Planner._candidate_search_outcome(
        "회의록 요약해줘", [f"/x/회의록-{i}.txt" for i in range(30)], 0
    )
    assert isinstance(many, Clarification) and len(many.options) == 30
    print("F3 confirmed: legacy meeting flow lists all 30 candidates")

    (root / "long.txt").write_text("x" * (64 * 1024) + "unique_topic")
    found = find_files({"keywords": ["unique_topic"]}, (root,)).structured
    assert found["decision"] == "none" and not found["truncated"]
    print("F4 confirmed: unsearched large-file suffix reported as complete no-match")

    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(None, [ToolCall("s", "local__find_files", '{"keywords":["budget"]}')]),
        ModelTurn(None, [ToolCall(
            "r", "filesystem__read_text_file", '{"path":"' + str(root / "long.txt") + '"}'
        )]),
        ModelTurn("요약", []),
    ]
    gateway = AsyncMock()
    gateway.list_tools.return_value = [
        ToolDefinition("filesystem__read_text_file", "read", {"type": "object"})
    ]
    gateway.call_tool.return_value = ToolResult(True, "다른 파일 내용")
    planner = Planner(config, gateway, model)
    assert await planner.run("budget 문서 요약해줘") == Completed("요약")
    assert gateway.call_tool.await_args.args[1]["path"].endswith("long.txt")
    print("F5 confirmed: single search match does not bind subsequent read target")

    loop = FeedbackLoop(base / "review-feedback.sqlite3")
    loop.user_input("first", new_run=True)
    assert loop.rating(2, ("wrong_file",))
    assert loop.rating(5)  # UI clears cause tags when switching to a high score.
    loop.user_input("second", new_run=True)
    assert loop.rating(2, ("wrong_file",))
    assert loop.db.execute("SELECT COUNT(*) FROM incidents WHERE rule='R7'").fetchone()[0] == 1
    print("F6 confirmed: superseded low rating still triggers R7 after tag change")
    await loop.close()

    inside = root / "nested"
    inside.mkdir()
    (inside / "memo.txt").write_text("safe")
    outside = base / "outside"
    outside.mkdir()
    (outside / "memo.txt").write_text("outside_unique")
    from voice_action import file_search

    original = file_search._read_head

    def swapped(path):
        if path.parent == inside:
            inside.rename(root / "nested-original")
            inside.symlink_to(outside, target_is_directory=True)
        return original(path)

    with patch.object(file_search, "_read_head", side_effect=swapped):
        found = find_files({"keywords": ["outside_unique"]}, (root,)).structured
    assert found["decision"] == "single"
    assert Path(found["candidates"][0]["path"]).resolve().is_relative_to(outside)  # noqa: ASYNC240
    print("F7 confirmed: ancestor symlink swap permits out-of-root content search")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="voice-action-review-") as directory:
        asyncio.run(check(Path(directory)))
