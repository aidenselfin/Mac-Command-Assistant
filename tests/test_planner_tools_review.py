"""C03/C04 regressions: exposed tools are the only executable tools; the document reader
follows the selected-candidate contract. Real stdin + Filesystem MCP for the candidate flow."""

import json
import shutil
import sys
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from voice_action import conversion
from voice_action.config import Config
from voice_action.conversion import CONVERT_TOOL, READ_DOCUMENT_TOOL
from voice_action.mcp_client import SUPPORTED_TOOLS, MCPClient, ToolDefinition, ToolResult
from voice_action.mcp_manager import MCPManager
from voice_action.planner import ApprovalRequired, ModelTurn, Planner, ToolCall
from voice_action.undo import UndoJournal


class Model:
    def __init__(self, turns):
        self.turns, self.seen, self.offered = list(turns), [], []

    async def generate(self, messages, tools, **kwargs):
        self.seen.append(deepcopy(messages))
        self.offered.append({tool.name for tool in tools})
        # Planner may ask again (e.g. to correct a web flow); keep answering "끝".
        return self.turns.pop(0) if self.turns else ModelTurn("끝", [])


def calls(*pairs):
    return ModelTurn(
        None, [ToolCall(f"c{i}", name, json.dumps(args)) for i, (name, args) in enumerate(pairs)]
    )


def tool_messages(model):
    return [m for m in model.seen[-1] if m.get("role") == "tool"]


@pytest.fixture
def root(config):
    folder = config.allowed_directories[0]
    (folder / "표.csv").write_text("a\n1\n")
    return folder


def no_conversion(*_args, **_kwargs):
    raise AssertionError("an unexposed conversion must never run")


@pytest.mark.parametrize(
    "request_text, tools",
    [
        ("https://example.com 페이지를 읽고 요약해줘", ["web__browser_navigate"]),
        ("폴더 파일 목록 보여줘", ["filesystem__list_directory"]),
    ],
    ids=["web-read", "listing"],
)
async def test_hidden_conversion_call_is_refused_without_approval(
    config, root, request_text, tools
):
    gateway = AsyncMock()
    gateway.list_tools.return_value = [ToolDefinition(n, "t", {"type": "object"}) for n in tools]
    convert = {"source": str(root / "표.csv"), "target_format": "json"}
    model = Model([calls((CONVERT_TOOL, convert)), ModelTurn("끝", [])])
    with patch.object(conversion, "prepare_conversion", side_effect=no_conversion):
        outcome = await Planner(config, gateway, model).run(request_text)
    assert not isinstance(outcome, ApprovalRequired)
    assert CONVERT_TOOL not in model.offered[0]
    refused = [m for m in model.seen[1] if m.get("role") == "tool"]
    assert refused[0]["content"].startswith("tool_error:")  # policy or not-offered refusal
    assert not (root / "표.json").exists()
    gateway.call_tool.assert_not_called()


async def test_hidden_call_beside_an_exposed_one_only_runs_the_exposed_one(config, root):
    gateway = AsyncMock()
    gateway.list_tools.return_value = [
        ToolDefinition("filesystem__list_directory", "t", {"type": "object"})
    ]
    gateway.call_tool.return_value = ToolResult(True, "[FILE] 표.csv")
    convert = {"source": str(root / "표.csv"), "target_format": "json"}
    model = Model([
        calls(("filesystem__list_directory", {"path": str(root)}), (CONVERT_TOOL, convert)),
        ModelTurn("목록입니다", []),
    ])
    with patch.object(conversion, "prepare_conversion", side_effect=no_conversion):
        outcome = await Planner(config, gateway, model).run("폴더 파일 목록 보여줘")
    assert not isinstance(outcome, ApprovalRequired)
    assert gateway.call_tool.await_count == 1
    contents = [m["content"] for m in tool_messages(model)]
    assert contents[0] == "[FILE] 표.csv" and "제공되지 않은 도구" in contents[1]


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("textutil"), reason="macOS")
async def test_document_reader_follows_the_selected_candidate(tmp_path):
    root = (tmp_path / "work")
    root.mkdir()
    root = root.resolve()
    selected = root / "회의록-selected.html"
    selected.write_text("<html><body>SELECTED_REVIEW_DOCUMENT</body></html>")
    unrelated = root / "unrelated.html"
    unrelated.write_text("<html><body>UNRELATED_REVIEW_DOCUMENT</body></html>")
    config = Config("openai", "scripted", "synthetic-key", (root,), feedback_enabled=False)
    search = ("local__find_files", {"keywords": ["회의록"]})
    model = Model([
        calls(search), calls((READ_DOCUMENT_TOOL, {"path": str(selected)})),
        ModelTurn("candidate summarized", []),
        calls(search), calls((READ_DOCUMENT_TOOL, {"path": str(unrelated)})),
        calls((READ_DOCUMENT_TOOL, {"path": str(selected)})), ModelTurn("recovered", []),
    ])
    manager = MCPManager({"filesystem": MCPClient(config, allowed_tools=SUPPORTED_TOOLS)})
    shown = []
    async with manager.connect():
        planner = Planner(
            config, manager, model, tools=await manager.list_tools(),
            undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,)),
        )
        for _ in range(2):
            shown.append((await planner.run("회의록 요약해줘")).text)
    shown = " ".join(shown)
    assert not model.turns, shown
    assert "candidate summarized" in shown and "recovered" in shown
    assert "검증된 후보 파일을 읽지 않아" not in shown
    assert not any("UNRELATED_REVIEW_DOCUMENT" in str(messages) for messages in model.seen)
