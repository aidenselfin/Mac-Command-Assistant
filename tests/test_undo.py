import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from voice_action.mcp_client import ToolDefinition, ToolResult
from voice_action.planner import ApprovalRequired, Completed, ModelTurn, Planner, ToolCall
from voice_action.risk_guard import RiskGuard, RiskGuardError
from voice_action.undo import (
    BOUNDARY_ERROR_MESSAGE,
    CONFLICT_MESSAGE,
    CREATE_UNDONE_MESSAGE,
    NOTHING_TO_UNDO_MESSAGE,
    OVERWRITE_UNDONE_MESSAGE,
    UndoJournal,
    UndoJournalError,
    UndoKind,
)


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def journal(tmp_path, root=None):
    root = root or tmp_path / "allowed"
    root.mkdir(exist_ok=True)
    return UndoJournal(tmp_path / "journal.sqlite3", (root,)), root


def inject_entry(
    undo_journal,
    *,
    path,
    operation="create",
    original=None,
    expected=None,
    status="active",
):
    expected = expected or fingerprint(b"written")
    with sqlite3.connect(undo_journal.path) as connection:
        connection.execute(
            """
            INSERT INTO undo_entries (
                command_id, canonical_path, operation, original_bytes,
                post_fingerprint, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                str(path),
                operation,
                original,
                expected,
                status,
                time.time_ns(),
            ),
        )


def test_create_write_then_undo_removes_only_created_file(tmp_path):
    undo_journal, root = journal(tmp_path)
    target = root / "created.txt"
    undo_journal.prepare_write(str(target), "create", "written")
    target.write_text("written", encoding="utf-8")

    result = undo_journal.undo_last_write()

    assert result.kind is UndoKind.CREATE_UNDONE
    assert result.message == CREATE_UNDONE_MESSAGE
    assert not target.exists()
    assert undo_journal.entry_count(status="undone") == 1


def test_overwrite_then_undo_atomically_restores_original_bytes(tmp_path):
    undo_journal, root = journal(tmp_path)
    target = root / "existing.txt"
    original = "원본 😀\n".encode()
    target.write_bytes(original)
    undo_journal.prepare_write(str(target), "overwrite", "새 내용")
    target.write_text("새 내용", encoding="utf-8")

    result = undo_journal.undo_last_write()

    assert result.kind is UndoKind.OVERWRITE_UNDONE
    assert result.message == OVERWRITE_UNDONE_MESSAGE
    assert target.read_bytes() == original
    assert list(root.glob(".voice-action-undo-*")) == []


def test_external_change_conflicts_and_preserves_changed_file(tmp_path):
    undo_journal, root = journal(tmp_path)
    target = root / "changed.txt"
    undo_journal.prepare_write(str(target), "create", "written")
    target.write_text("externally changed", encoding="utf-8")

    result = undo_journal.undo_last_write()

    assert result.kind is UndoKind.CONFLICT
    assert result.message == CONFLICT_MESSAGE
    assert target.read_text(encoding="utf-8") == "externally changed"
    assert undo_journal.entry_count(status="conflict") == 1
    assert undo_journal.undo_last_write().message == NOTHING_TO_UNDO_MESSAGE


def test_injected_outside_root_entry_is_rejected_without_file_change(tmp_path):
    undo_journal, _root = journal(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"written")
    inject_entry(undo_journal, path=outside)

    result = undo_journal.undo_last_write()

    assert result.kind is UndoKind.BOUNDARY_ERROR
    assert result.message == BOUNDARY_ERROR_MESSAGE
    assert outside.read_bytes() == b"written"
    assert undo_journal.entry_count(status="conflict") == 1


def test_symlink_target_and_symlink_journal_are_rejected(tmp_path):
    undo_journal, root = journal(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"written")
    link = root / "link.txt"
    link.symlink_to(outside)
    inject_entry(undo_journal, path=link)

    result = undo_journal.undo_last_write()

    assert result.kind is UndoKind.BOUNDARY_ERROR
    assert outside.read_bytes() == b"written"

    real_journal = tmp_path / "real.sqlite3"
    real_journal.write_bytes(b"not a journal")
    journal_link = tmp_path / "linked.sqlite3"
    journal_link.symlink_to(real_journal)
    with pytest.raises(UndoJournalError, match="경계"):
        UndoJournal(journal_link, (root,))


def test_non_regular_target_is_rejected_without_removal(tmp_path):
    undo_journal, root = journal(tmp_path)
    target = root / "directory"
    target.mkdir()
    inject_entry(undo_journal, path=target)

    result = undo_journal.undo_last_write()

    assert result.kind is UndoKind.BOUNDARY_ERROR
    assert target.is_dir()
    assert undo_journal.entry_count(status="conflict") == 1


def test_second_undo_is_noop_and_does_not_change_another_file(tmp_path):
    undo_journal, root = journal(tmp_path)
    target = root / "created.txt"
    untouched = root / "untouched.txt"
    untouched.write_text("keep", encoding="utf-8")
    undo_journal.prepare_write(str(target), "create", "written")
    target.write_text("written", encoding="utf-8")

    assert undo_journal.undo_last_write().message == CREATE_UNDONE_MESSAGE
    assert undo_journal.undo_last_write().message == NOTHING_TO_UNDO_MESSAGE

    assert not target.exists()
    assert untouched.read_text(encoding="utf-8") == "keep"


def test_latest_active_write_is_undone_first(tmp_path):
    undo_journal, root = journal(tmp_path)
    first, second = root / "first.txt", root / "second.txt"
    undo_journal.prepare_write(str(first), "create", "first")
    first.write_text("first", encoding="utf-8")
    undo_journal.prepare_write(str(second), "create", "second")
    second.write_text("second", encoding="utf-8")

    assert undo_journal.undo_last_write().message == CREATE_UNDONE_MESSAGE
    assert first.exists() and not second.exists()
    assert undo_journal.undo_last_write().message == CREATE_UNDONE_MESSAGE
    assert not first.exists()


def test_non_utf8_overwrite_is_not_journaled(tmp_path):
    undo_journal, root = journal(tmp_path)
    target = root / "binary.txt"
    target.write_bytes(b"\xff\xfe")

    with pytest.raises(UndoJournalError, match="UTF-8"):
        undo_journal.prepare_write(str(target), "overwrite", "new")

    assert undo_journal.entry_count() == 0


def test_sqlite_uses_wal_and_stores_only_required_write_fields(tmp_path):
    undo_journal, root = journal(tmp_path)
    target = root / "created.txt"
    entry = undo_journal.prepare_write(str(target), "create", "written")

    with sqlite3.connect(undo_journal.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        row = connection.execute(
            """
            SELECT command_id, canonical_path, operation, original_bytes,
                   post_fingerprint, status, created_at
            FROM undo_entries
            """
        ).fetchone()

    assert row[:6] == (
        entry.command_id,
        str(target),
        "create",
        None,
        fingerprint(b"written"),
        "active",
    )
    uuid.UUID(row[0])
    assert isinstance(row[6], int)


class WritingGateway:
    def __init__(self):
        self.calls = []

    async def normalize_tool_arguments(self, _name, arguments):
        normalized = dict(arguments)
        normalized["path"] = str(
            await asyncio.to_thread(Path(arguments["path"]).resolve, strict=False)
        )
        return normalized

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "filesystem__write_file":
            await asyncio.to_thread(
                Path(arguments["path"]).write_text,
                arguments["content"],
                encoding="utf-8",
            )
        return ToolResult(True, "ok")


class FailingJournal:
    def prepare_write(self, *_args):
        raise UndoJournalError("database secret")


class FailedResultGateway(WritingGateway):
    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return ToolResult(False, "failed", error="failed")


class UncertainGateway(WritingGateway):
    def __init__(self, *, written_content):
        super().__init__()
        self.written_content = written_content

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.written_content is not None:
            await asyncio.to_thread(
                Path(arguments["path"]).write_text,
                self.written_content,
                encoding="utf-8",
            )
        raise RuntimeError("uncertain transport")


async def test_journal_prepare_failure_blocks_underlying_write(tmp_path):
    gateway = WritingGateway()
    guard = RiskGuard(gateway, FailingJournal())
    target = tmp_path / "blocked.txt"
    prepared = await guard.prepare_write(
        "filesystem__write_file",
        {"path": str(target), "content": "blocked"},
    )

    with pytest.raises(RiskGuardError, match="journal") as error:
        await guard.execute_approved(prepared)

    assert "database secret" not in str(error.value)
    assert gateway.calls == []
    assert not target.exists()


async def test_only_approved_write_creates_entry_and_rejection_read_do_not(tmp_path):
    undo_journal, root = journal(tmp_path)
    gateway = WritingGateway()
    guard = RiskGuard(gateway, undo_journal)
    target = root / "approved.txt"
    rejected = await guard.prepare_write(
        "filesystem__write_file",
        {"path": str(target), "content": "no"},
    )
    guard.reject(rejected)
    await guard.execute_read_only(
        "filesystem__list_directory", {"path": str(root)}
    )
    approved = await guard.prepare_write(
        "filesystem__write_file",
        {"path": str(target), "content": "yes"},
    )

    assert undo_journal.entry_count() == 0
    await guard.execute_approved(approved)

    assert undo_journal.entry_count(status="active") == 1
    assert target.read_text(encoding="utf-8") == "yes"


async def test_clear_write_failure_is_excluded_from_undo_candidates(tmp_path):
    undo_journal, root = journal(tmp_path)
    gateway = FailedResultGateway()
    guard = RiskGuard(gateway, undo_journal)
    target = root / "failed.txt"
    prepared = await guard.prepare_write(
        "filesystem__write_file",
        {"path": str(target), "content": "failed"},
    )

    result = await guard.execute_approved(prepared)

    assert not result.ok
    assert undo_journal.entry_count(status="failed") == 1
    assert undo_journal.undo_last_write().message == NOTHING_TO_UNDO_MESSAGE


@pytest.mark.parametrize(
    ("written_content", "expected_status"),
    [(None, "failed"), ("expected", "active"), ("different", "failed")],
)
async def test_uncertain_transport_is_active_only_for_exact_fingerprint(
    tmp_path, written_content, expected_status
):
    undo_journal, root = journal(tmp_path)
    gateway = UncertainGateway(written_content=written_content)
    guard = RiskGuard(gateway, undo_journal)
    target = root / f"uncertain-{expected_status}-{written_content}.txt"
    prepared = await guard.prepare_write(
        "filesystem__write_file",
        {"path": str(target), "content": "expected"},
    )

    with pytest.raises(RuntimeError, match="uncertain transport"):
        await guard.execute_approved(prepared)

    assert undo_journal.entry_count(status=expected_status) == 1


async def test_planner_without_journal_never_executes_approved_write(config):
    gateway = WritingGateway()
    target = config.allowed_directories[0] / "missing-journal.txt"
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(
            None,
            [
                ToolCall(
                    "write",
                    "filesystem__write_file",
                    json.dumps({"path": str(target), "content": "blocked"}),
                )
            ],
        ),
        ModelTurn("journal 오류를 확인했습니다.", []),
    ]
    planner = Planner(
        config,
        gateway,
        model,
        tools=[ToolDefinition("filesystem__write_file", "Write", {"type": "object"})],
    )

    pending = await planner.run("missing-journal.txt에 저장해줘")
    assert isinstance(pending, ApprovalRequired)
    outcome = await planner.approve(pending.context)

    assert outcome == Completed("journal 오류를 확인했습니다.")
    assert gateway.calls == []
    assert not target.exists()
    assert "Undo journal" in model.generate.await_args_list[1].args[0][-1]["content"]
