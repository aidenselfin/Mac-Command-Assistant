"""Features that were built apart must still work together: the native app's transport."""

from unittest.mock import Mock

from voice_action.file_operations import MOVE_TOOL
from voice_action.native_bridge import NativeBridge
from voice_action.planner import ApprovalRequired
from voice_action.runtime import RuntimeState
from voice_action.ui import UIMessage, UIMessageType


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
