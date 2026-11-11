"""The Python bridge, the Swift receiver and docs/native-protocol.md must list the same messages."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = (ROOT / "voice_action/native_bridge.py").read_text()
SWIFT = (ROOT / "native-preview/Sources/FinderChatPreview/Backend.swift").read_text()
DOC = (ROOT / "docs/native-protocol.md").read_text()


def test_every_event_the_bridge_sends_is_handled_by_swift_and_documented():
    sent = set(re.findall(r'"type": "([a-z_]+)"', BRIDGE))
    handled = set(re.findall(r'case ((?:"[a-z_]+"(?:, )?)+):', SWIFT))
    handled = {name for group in handled for name in re.findall(r'"([a-z_]+)"', group)}
    assert sent, "no events found: the pattern needs updating"
    assert sent <= handled, f"Swift ignores: {sorted(sent - handled)}"
    assert all(f"`{name}`" in DOC for name in sent), "docs/native-protocol.md misses an event"


def test_every_command_the_bridge_accepts_is_documented():
    actions = set(re.findall(r'action (?:==|in \{)\s*"([a-z_]+)"', BRIDGE))
    groups = re.findall(r"action in \{([^}]*)\}", BRIDGE)
    actions |= set(re.findall(r'"([a-z_]+)"', " ".join(groups)))
    known = {"submit", "approve", "reject", "cancel", "undo", "attach", "detach"}
    known |= {"shutdown", "voice", "reset", "quick_list"}
    assert known <= actions
    assert all(f"`{name}`" in DOC for name in actions), "docs/native-protocol.md misses a command"


def test_every_approval_field_the_bridge_sends_is_decoded_by_swift_and_documented():
    block = BRIDGE[BRIDGE.index('"type": "approval"'):]
    block = block[:block.index("})")]
    fields = set(re.findall(r'"([a-z_]+)":', block)) - {"type"}
    struct = SWIFT[SWIFT.index("struct Approval"):]
    decoded = set(re.findall(r"let ([a-z_]+):", struct[:struct.index("}")]))
    assert "previews" in fields
    assert fields <= decoded, f"Swift Approval misses: {sorted(fields - decoded)}"
    row = next(line for line in DOC.splitlines() if line.startswith("| `approval`"))
    assert all(f"`{name}`" in row for name in fields), "approval row misses a field"
