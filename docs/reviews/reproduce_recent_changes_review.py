"""Evidence for review of a4e44f1; defect assertions are not regression expectations.

Run from repo root with .venv/bin/python -u docs/reviews/reproduce_recent_changes_review.py.
Synthetic temp files only, no real API or UI. Application acceptance uses --interactive,
real stdin and Filesystem MCP, scripted model. Unit probes isolate boundary/race defects.
"""

import asyncio
import io
import json
import os
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import main  # noqa: E402
from voice_action import conversion  # noqa: E402
from voice_action.config import Config  # noqa: E402
from voice_action.conversion import CONVERT_TOOL, READ_DOCUMENT_TOOL  # noqa: E402
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient, ToolDefinition  # noqa: E402
from voice_action.mcp_manager import MCPManager  # noqa: E402
from voice_action.planner import ApprovalRequired, ModelTurn, Planner, ToolCall  # noqa: E402
from voice_action.risk_guard import RiskGuard  # noqa: E402
from voice_action.undo import UndoJournal, UndoKind  # noqa: E402


def observed(message):
    print("OBSERVED " + message)


def call(name, **arguments):
    return ModelTurn(None, [ToolCall("review", name, json.dumps(arguments))])


class Model:
    def __init__(self, turns):
        self.turns, self.seen, self.offered = list(turns), [], []

    async def generate(self, messages, tools, **kwargs):
        self.seen.append(deepcopy(messages))
        self.offered.append({tool.name for tool in tools})
        return self.turns.pop(0)


async def probes(base):
    root, outside = base / "allowed", base / "outside"
    root.mkdir()
    outside.mkdir()
    source = root / "source.csv"
    source.write_text("name\nreview\n")
    journal = UndoJournal(base / "undo.sqlite3", (root,))
    guard = RiskGuard(AsyncMock(), journal, (root,))

    destination = root / "destination"
    destination.mkdir()
    prepared = await guard.prepare_write(CONVERT_TOOL, {
        "source": str(source), "target_format": "json",
        "output": str(destination / "output.json"),
    })
    destination.rename(root / "original-destination")
    destination.symlink_to(outside, target_is_directory=True)
    result = await guard.execute_approved(prepared)
    assert result.ok and (outside / "output.json").exists()
    assert journal.undo_last_write().kind is UndoKind.BOUNDARY_ERROR
    observed("C01 approved conversion follows changed parent symlink outside allowed root")

    prepared = await guard.prepare_write(CONVERT_TOOL, {
        "source": str(source), "target_format": "json", "output": str(root / "cancelled.json"),
    })
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_write = conversion.write_new_file

    def delayed_write(raw, data):
        started.set()
        try:
            if not release.wait(timeout=10):
                raise RuntimeError("review thread release timed out")
            real_write(raw, data)
        finally:
            finished.set()

    with patch.object(conversion, "write_new_file", side_effect=delayed_write):
        task = asyncio.create_task(guard.execute_approved(prepared))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("expected cancellation")
            assert journal.entry_count(status="failed") == 1
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
    assert (root / "cancelled.json").exists()
    assert journal.entry_count(status="active") == 0
    assert journal.undo_last_write().kind is UndoKind.NOTHING_TO_UNDO
    observed("C02 cancelled writer finishes after journal marked failed; output has no Undo")

    config = Config("openai", "scripted", "synthetic-key", (root,), feedback_enabled=False)
    gateway = AsyncMock()
    gateway.list_tools.return_value = [
        ToolDefinition("web__browser_navigate", "review", {"type": "object"}),
        ToolDefinition("web__browser_snapshot", "review", {"type": "object"}),
    ]
    model = Model([call(CONVERT_TOOL, source=str(source), target_format="json")])
    planner = Planner(config, gateway, model, undo_journal=journal)
    outcome = await planner.run("https://example.com 페이지를 읽고 요약해줘")
    assert model.offered[0] == {"web__browser_navigate"}
    assert CONVERT_TOOL not in model.offered[0]
    assert isinstance(outcome, ApprovalRequired)
    planner.reject(outcome.context)
    observed("C03 unexposed conversion in web/read request reaches approval without navigation")

    duplicate = root / "duplicate.csv"
    duplicate.write_text("name,name\nfirst,second\n")
    converted = conversion.prepare_conversion({
        "source": str(duplicate), "target_format": "json",
    }, (root,))
    assert json.loads(converted.data) == [{"name": "second"}]
    observed("C05 duplicate CSV headers silently discard the first cell")
    with patch.object(conversion, "MAX_OUTPUT_BYTES", 1):
        converted = conversion.prepare_conversion({
            "source": str(source), "target_format": "json",
        }, (root,))
    assert len(converted.data) > 1
    observed("C06 csv/json early return bypasses output size limit (reduced test threshold)")

    with patch.object(conversion, "_run", return_value=b""):
        try:
            conversion.convert(source, b"review document", "txt", "docx")
        except FileNotFoundError:
            observed("C07 successful converter exit without artifact raises raw FileNotFoundError")
        else:
            raise AssertionError("expected missing converter artifact")


def interactive(base):
    root = base / "interactive"
    root.mkdir()
    source = root / "table.csv"
    source.write_text("name,amount\nlunch,12000\n")
    (root / "notes.txt").write_text("review document")
    selected = root / "회의록-selected.html"
    selected.write_text("<html><body>SELECTED_REVIEW_DOCUMENT</body></html>")
    unrelated = root / "unrelated.html"
    unrelated.write_text("<html><body>UNRELATED_REVIEW_DOCUMENT</body></html>")
    config = Config("openai", "scripted", "synthetic-key", (root,), feedback_enabled=False)
    model = Model([
        call(CONVERT_TOOL, source=str(source), target_format="json"), ModelTurn("converted", []),
        call(CONVERT_TOOL, source=str(source), target_format="json"),
        call(CONVERT_TOOL, source=str(root / "notes.txt"), target_format="pdf"),
        ModelTurn("pdf created", []),
        call(READ_DOCUMENT_TOOL, path=str(root / "notes.pdf")), ModelTurn("pdf read", []),
        call("filesystem__search_files", path=str(root), pattern="**/*회의록*", excludePatterns=[]),
        call(READ_DOCUMENT_TOOL, path=str(selected)), ModelTurn("candidate summarized", []),
        call("filesystem__search_files", path=str(root), pattern="**/*회의록*", excludePatterns=[]),
        call(READ_DOCUMENT_TOOL, path=str(unrelated)), ModelTurn("wrong document summarized", []),
    ])
    commands = ["table.csv를 json으로 변환해줘", "승인", "undo",
                "table.csv를 json으로 변환해줘", "거절",
                "notes.txt를 pdf로 변환해줘", "승인", "notes.pdf를 읽어줘",
                "undo", "회의록 요약해줘", "회의록 요약해줘", "exit"]
    output = io.StringIO()
    journal = UndoJournal(base / "interactive-undo.sqlite3", (root,))
    real_print_outcome = main.print_outcome
    approvals = []

    def check_outcome(console, outcome):
        if isinstance(outcome, ApprovalRequired):
            assert not Path(outcome.target_path).exists()
            approvals.append(outcome.operation)
        real_print_outcome(console, outcome)

    read_fd, write_fd = os.pipe()
    os.write(write_fd, ("\n".join(commands) + "\n").encode())
    os.close(write_fd)
    with os.fdopen(read_fd, encoding="utf-8") as stdin, patch.object(sys, "stdin", stdin), \
            patch.object(main, "load_config", return_value=config), \
            patch.object(main, "_build_undo_journal", return_value=journal), \
            patch.object(main, "OpenAICompatibleModel", return_value=model), \
            patch.object(main, "print_outcome", side_effect=check_outcome), \
            patch.object(main, "_build_mcp_manager", side_effect=lambda c: MCPManager({
                "filesystem": MCPClient(c, allowed_tools=SUPPORTED_TOOLS)})), \
            redirect_stdout(output):
        assert main.main(["--interactive"]) == 0
    assert not model.turns, output.getvalue()
    assert approvals == ["convert", "convert", "convert"]
    assert source.read_text() == "name,amount\nlunch,12000\n"
    assert not (root / "table.json").exists() and not (root / "notes.pdf").exists()
    assert "[DONE] pdf read" in output.getvalue()
    assert output.getvalue().count("검증된 후보 파일을 읽지 않아") == 2
    assert any("SELECTED_REVIEW_DOCUMENT" in str(messages) for messages in model.seen)
    assert any("UNRELATED_REVIEW_DOCUMENT" in str(messages) for messages in model.seen)
    observed("C04 selected document read still returns Error; "
             "unrelated document also reaches model")
    print("PASS interactive: stdin + real Filesystem MCP, csv/json approval/reject/Undo, "
          "real macOS txt/pdf conversion/read/Undo, source unchanged, exit")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="voice-action-recent-review-") as temporary:
        base = Path(temporary).resolve()
        asyncio.run(probes(base))
        interactive(base)
