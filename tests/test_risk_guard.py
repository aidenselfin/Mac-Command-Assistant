from copy import deepcopy

import pytest

from voice_action.mcp_client import ToolResult
from voice_action.risk_guard import (
    READ_ONLY_TOOLS,
    ApprovalInvalidError,
    ForbiddenToolError,
    RiskGuard,
    ToolRisk,
    classify_tool,
)
from voice_action.undo import UndoJournal


class Gateway:
    def __init__(self):
        self.calls = []

    async def normalize_tool_arguments(self, _name, arguments):
        return deepcopy(arguments)

    async def call_tool(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        return ToolResult(True, "ok")


def test_fixed_policy_does_not_depend_on_mcp_annotations():
    assert all(classify_tool(name) is ToolRisk.READ_ONLY for name in READ_ONLY_TOOLS)
    assert classify_tool("filesystem__write_file") is ToolRisk.WRITE
    assert classify_tool("web__browser_click") is ToolRisk.FORBIDDEN
    assert classify_tool("filesystem__delete_file") is ToolRisk.FORBIDDEN


async def test_canonical_digest_is_stable_and_nonce_is_one_shot(tmp_path):
    gateway = Gateway()
    guard = RiskGuard(gateway, UndoJournal(tmp_path / "undo.sqlite3", (tmp_path,)))
    target = tmp_path / "new.txt"
    content = "한" * 201 + "SECRET-TAIL"
    first = await guard.prepare_write(
        "filesystem__write_file",
        {"content": content, "path": str(target)},
    )
    second = await guard.prepare_write(
        "filesystem__write_file",
        {"path": str(target), "content": content},
    )

    assert first.digest == second.digest
    assert first.nonce != second.nonce
    assert content not in repr(first)
    assert "SECRET-TAIL" not in repr(first)
    await guard.execute_approved(first)
    with pytest.raises(ApprovalInvalidError):
        await guard.execute_approved(first)
    assert gateway.calls == [
        (
            "filesystem__write_file",
            {"content": content, "path": str(target)},
        )
    ]


async def test_rejected_nonce_cannot_be_reused(tmp_path):
    gateway = Gateway()
    guard = RiskGuard(gateway, UndoJournal(tmp_path / "undo.sqlite3", (tmp_path,)))
    prepared = await guard.prepare_write(
        "filesystem__write_file",
        {"path": str(tmp_path / "no.txt"), "content": "no"},
    )

    guard.reject(prepared)

    with pytest.raises(ApprovalInvalidError):
        await guard.execute_approved(prepared)
    assert gateway.calls == []


async def test_forbidden_tool_has_no_approval_or_gateway_call():
    gateway = Gateway()
    guard = RiskGuard(gateway)

    with pytest.raises(ForbiddenToolError):
        await guard.execute_read_only("web__browser_click", {"ref": "button"})
    with pytest.raises(ForbiddenToolError):
        await guard.prepare_write("filesystem__delete_file", {"path": "/tmp/x"})
    assert gateway.calls == []
