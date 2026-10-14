"""Features that were built apart must still work together: file operations, meaning search and
the native app's transport."""

from unittest.mock import AsyncMock, Mock

from voice_action.file_operations import MOVE_TOOL
from voice_action.file_search import (
    FIND_FILES_DEFINITION,
    FIND_FILES_SEMANTIC_DEFINITION,
    find_files,
)
from voice_action.native_bridge import NativeBridge
from voice_action.planner import ApprovalRequired
from voice_action.risk_guard import RiskGuard
from voice_action.runtime import RuntimeState
from voice_action.semantic_index import SemanticIndex
from voice_action.ui import UIMessage, UIMessageType
from voice_action.undo import UndoJournal


class _Embedder:
    """Every file looks like a cat to it: enough to rank a hit without the real model."""

    def query(self, text):
        return [1.0, 0.0]

    def document(self, text):
        return [1.0, 0.0]

    def image(self, data):
        return [1.0, 0.0]


async def test_a_meaning_search_hit_can_be_moved_with_an_approval(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    photo = root / "IMG_001.jpg"
    photo.write_bytes(b"cat")
    (root / "보관").mkdir()
    index = SemanticIndex((root,), tmp_path / "semantic.sqlite3", lambda: None, "fake")
    index._embedder = _Embedder()
    index.refresh()
    found = find_files({"keywords": ["qqq"], "query": "고양이 사진"}, (root,), index).structured
    assert (found["stage"], found["decision"]) == ("meaning", "choose")
    chosen = found["candidates"][0]["path"]
    guard = RiskGuard(AsyncMock(), UndoJournal(tmp_path / "undo.sqlite3", (root,)), (root,))
    request = {"source": chosen, "destination": str(root / "보관")}
    prepared = await guard.prepare_write(MOVE_TOOL, request)
    assert photo.exists()  # nothing moves before the approval
    assert (await guard.execute_approved(prepared)).ok
    assert (root / "보관" / "IMG_001.jpg").exists() and not photo.exists()


def test_the_native_app_gets_source_and_destination_of_a_move_approval():
    events = []
    transport = NativeBridge(Mock(), events.append)
    transport.message(UIMessage(UIMessageType.STATE, RuntimeState.WAITING_APPROVAL))
    outcome = ApprovalRequired(
        MOVE_TOOL, "/allowed/보관/IMG_001.jpg", "move", 0, "", False, {"nonce": "SECRET"},
        targets=("/allowed/IMG_001.jpg",),
    )
    transport.message(UIMessage(UIMessageType.OUTCOME, outcome))
    event = events[-1]
    assert (event["operation"], event["target"], event["targets"]) == (
        "move", "/allowed/보관/IMG_001.jpg", ["/allowed/IMG_001.jpg"],
    )
    assert "SECRET" not in str(events)


def test_meaning_search_off_leaves_the_tool_definition_as_before():
    assert "query" not in FIND_FILES_DEFINITION.schema["properties"]
    assert "query" in FIND_FILES_SEMANTIC_DEFINITION.schema["properties"]
    assert FIND_FILES_SEMANTIC_DEFINITION.name == FIND_FILES_DEFINITION.name
