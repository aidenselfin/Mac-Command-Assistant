"""Interactive acceptance after docs/reviews/2026-10-04-recent-changes-review.md fixes.

Run from the repository root: .venv/bin/python docs/reviews/acceptance_recent_changes.py
Real stdin pipe and Filesystem MCP, real macOS converters, scripted LLM, feedback off,
temporary folders only. No UI, no API key, no external request.
"""

import io
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import main  # noqa: E402
from voice_action.config import Config  # noqa: E402
from voice_action.conversion import CONVERT_TOOL, READ_DOCUMENT_TOOL  # noqa: E402
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient  # noqa: E402
from voice_action.mcp_manager import MCPManager  # noqa: E402
from voice_action.planner import ApprovalRequired, ModelTurn, ToolCall  # noqa: E402
from voice_action.undo import UndoJournal  # noqa: E402


class Model:
    def __init__(self, turns):
        self.turns, self.seen = list(turns), []

    async def generate(self, messages, tools, **kwargs):
        self.seen.append(json.dumps(messages, ensure_ascii=False))
        return deepcopy(self.turns.pop(0))


def call(name, **arguments):
    return ModelTurn(None, [ToolCall("c", name, json.dumps(arguments))])


def run_app(root, journal, model, steps, approvals):
    """Drive main --interactive; each step writes a line, waits for a marker, then acts.

    A step with line None closes stdin (EOF) while the app is waiting.
    """
    config = Config("openai", "scripted", "synthetic-key", (root,), feedback_enabled=False)
    output = io.StringIO()
    read_fd, write_fd = os.pipe()
    real_print = main.print_outcome

    def record(console, outcome):
        if isinstance(outcome, ApprovalRequired):
            path = Path(outcome.target_path)
            approvals.append((outcome.operation, path.name, path.exists()))
        real_print(console, outcome)

    def expect(marker, after):
        deadline = time.monotonic() + 30
        while marker not in output.getvalue()[after:]:
            if time.monotonic() > deadline:
                raise AssertionError(f"missing {marker!r}:\n{output.getvalue()[after:]}")
            time.sleep(0.02)

    def feed():
        for line, marker, action in steps:
            if line is None:
                break
            start = len(output.getvalue())
            os.write(write_fd, (line + "\n").encode())
            if marker:
                expect(marker, start)
            if action:
                action()
        os.close(write_fd)

    feeder = threading.Thread(target=feed, daemon=True)
    with os.fdopen(read_fd, encoding="utf-8") as stdin, patch.object(sys, "stdin", stdin), \
            patch.object(main, "load_config", return_value=config), \
            patch.object(main, "_build_undo_journal", return_value=journal), \
            patch.object(main, "OpenAICompatibleModel", return_value=model), \
            patch.object(main, "print_outcome", side_effect=record), \
            patch.object(main, "_build_mcp_manager", side_effect=lambda c: MCPManager({
                "filesystem": MCPClient(c, allowed_tools=SUPPORTED_TOOLS)})), \
            redirect_stdout(output):
        feeder.start()
        code = main.main(["--interactive"])
    feeder.join(timeout=5)
    return code, output.getvalue()


def run(base: Path) -> None:
    root = base / "work"
    root.mkdir()
    root = root.resolve()
    table = "name,amount\nlunch,12000\n"
    (root / "table.csv").write_text(table)
    (root / "notes.txt").write_text("FIRST VERSION")
    selected = root / "회의록-selected.html"
    selected.write_text("<html><body>SELECTED_REVIEW_DOCUMENT</body></html>")
    (root / "unrelated.html").write_text("<html><body>UNRELATED_REVIEW_DOCUMENT</body></html>")
    journal = UndoJournal(base / "undo.sqlite3", (root,))
    checks, approvals = {}, []
    csv_to_json = call(CONVERT_TOOL, source=str(root / "table.csv"), target_format="json")
    search = call("filesystem__search_files", path=str(root), pattern="**/*회의록*",
                  excludePatterns=[])
    model = Model([
        csv_to_json, ModelTurn("converted", []),
        csv_to_json, ModelTurn("converted again", []),
        csv_to_json,
        call(CONVERT_TOOL, source=str(root / "table.csv"), target_format="json",
             output=str(root / "table.json")),
        ModelTurn("output exists", []),
        call(CONVERT_TOOL, source=str(root / "notes.txt"), target_format="pdf"),
        ModelTurn("pdf created", []),
        call(READ_DOCUMENT_TOOL, path=str(root / "notes.pdf")), ModelTurn("pdf read", []),
        search, call(READ_DOCUMENT_TOOL, path=str(selected)), ModelTurn("candidate read", []),
        search, call(READ_DOCUMENT_TOOL, path=str(root / "unrelated.html")),
        call(READ_DOCUMENT_TOOL, path=str(selected)), ModelTurn("recovered", []),
        call(CONVERT_TOOL, source=str(root / "notes.txt"), target_format="html"),
        ModelTurn("html created", []),
        csv_to_json,
    ])

    def note(name, ok):
        return lambda: checks.update({name: ok()})

    steps = [
        ("table.csv를 json으로 변환해줘", "[APPROVAL REQUIRED]", None),
        ("몰라", "승인 또는 거절 입력이 필요합니다", None),
        ("승인", "[DONE] converted", note("csv→json created after approval",
                                          lambda: (root / "table.json").exists())),
        ("undo", "[DONE]", note("csv→json Undo", lambda: not (root / "table.json").exists())),
        ("table.csv를 json으로 변환해줘", "[APPROVAL REQUIRED]", None),
        ("승인", "[DONE] converted again", None),
        ("table.csv를 json으로 변환해줘", "[APPROVAL REQUIRED]", None),
        ("거절", "[DONE]", note("reject leaves no file",
                               lambda: not (root / "table (1).json").exists())),
        ("table.csv를 table.json으로 변환해줘", "[DONE] output exists", None),
        ("notes.txt를 pdf로 변환해줘", "[APPROVAL REQUIRED]",
         lambda: (root / "notes.txt").write_text("SECOND VERSION")),
        ("승인", "[APPROVAL REQUIRED]", note("changed source asks again",
                                             lambda: not (root / "notes.pdf").exists())),
        ("승인", "[DONE] pdf created", None),
        ("notes.pdf를 읽어줘", "[DONE] pdf read", None),
        ("회의록 요약해줘", "[DONE] candidate read", None),
        ("회의록 요약해줘", "[DONE] recovered", None),
        ("undo", "[DONE]", note("binary pdf Undo", lambda: not (root / "notes.pdf").exists())),
        ("notes.txt를 html로 변환해줘", "[APPROVAL REQUIRED]", None),
        ("승인", "[DONE] html created", None),
        ("table.csv를 json으로 변환해줘", "[APPROVAL REQUIRED]", None),
        (None, "", None),  # EOF with an approval pending
    ]
    code, shown = run_app(root, journal, model, steps, approvals)
    seen = "\n".join(model.seen)
    checks["exit code 0 at EOF"] = code == 0
    checks["all scripted turns used"] = not model.turns
    checks["approval shown before any file"] = all(not exists for *_, exists in approvals)
    checks["name collision gets (1)"] = ("convert", "table (1).json", False) in approvals
    checks["explicit output conflict refused"] = (
        "출력 파일이 이미 있습니다" in seen and (root / "table.json").read_text().startswith("[")
    )
    checks["re-approval converted the new source"] = "SECOND VERSION" in seen
    checks["unrelated document never read"] = "UNRELATED_REVIEW_DOCUMENT" not in seen
    checks["selected document read"] = "SELECTED_REVIEW_DOCUMENT" in seen
    checks["no candidate error"] = "검증된 후보 파일을 읽지 않아" not in shown
    checks["EOF pending conversion not written"] = not (root / "table (1).json").exists()
    checks["source table unchanged"] = (root / "table.csv").read_text() == table
    # A new run keeps the journal: Undo removes the html made in the previous run.
    code, _ = run_app(root, journal, Model([]), [("undo", "[DONE]", None), ("exit", "", None)],
                      approvals)
    checks["next run Undo removes last conversion"] = (
        code == 0 and not (root / "notes.html").exists()
    )
    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
    if not all(checks.values()):
        print(shown)
        raise SystemExit(1)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="voice-action-recent-acceptance-") as temporary:
        run(Path(temporary).resolve())
