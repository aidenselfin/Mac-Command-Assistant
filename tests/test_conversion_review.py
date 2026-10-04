"""Regressions for docs/reviews/2026-10-04-recent-changes-review.md (expected behavior)."""

import asyncio
import os
import threading
from unittest.mock import AsyncMock, patch

import pytest

from voice_action import conversion
from voice_action.conversion import CONVERT_TOOL
from voice_action.mcp_client import ToolRejectedError
from voice_action.risk_guard import RiskGuard
from voice_action.undo import UndoJournal, UndoKind


@pytest.fixture
def setup(tmp_path):
    root, outside = tmp_path / "allowed", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    root, outside = root.resolve(), outside.resolve()
    (root / "source.csv").write_text("name\nreview\n")
    journal = UndoJournal(tmp_path / "undo.sqlite3", (root,))
    return root, outside, journal, RiskGuard(AsyncMock(), journal, (root,))


def arguments(root, **extra):
    return {"source": str(root / "source.csv"), "target_format": "json", **extra}


# ---- C01 execution boundary ----------------------------------------------------------------


async def test_output_parent_swapped_for_symlink_writes_nothing_outside(setup):
    root, outside, journal, guard = setup
    (root / "out").mkdir()
    prepared = await guard.prepare_write(
        CONVERT_TOOL, arguments(root, output=str(root / "out" / "result.json"))
    )
    (root / "out").rename(root / "moved")
    (root / "out").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolRejectedError):
        await guard.execute_approved(prepared)
    assert list(outside.iterdir()) == []
    assert journal.entry_count(status="active") == 0


async def test_source_parent_swapped_for_symlink_is_refused(setup):
    root, outside, _journal, guard = setup
    (root / "in").mkdir()
    (root / "in" / "data.csv").write_text("a\n1\n")
    prepared = await guard.prepare_write(
        CONVERT_TOOL, {"source": str(root / "in" / "data.csv"), "target_format": "json"}
    )
    (outside / "data.csv").write_text("a\n1\n")
    (root / "in").rename(root / "moved")
    (root / "in").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolRejectedError):
        await guard.execute_approved(prepared)
    assert sorted(p.name for p in outside.iterdir()) == ["data.csv"]


def test_local_write_itself_enforces_the_boundary(setup):
    root, outside, _journal, _guard = setup
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolRejectedError):
        conversion.write_new_file(str(root / "link" / "x.json"), b"[]", (root,))
    with pytest.raises(ToolRejectedError):
        conversion.write_new_file(str(outside / "x.json"), b"[]", (root,))
    assert list(outside.iterdir()) == []


# ---- C02 cancellation and partial writes -------------------------------------------------


async def test_cancel_during_write_keeps_output_undoable(setup):
    root, _outside, journal, guard = setup
    prepared = await guard.prepare_write(CONVERT_TOOL, arguments(root))
    started, release = threading.Event(), threading.Event()
    real_write = conversion.write_new_file

    def held_write(*args):
        started.set()
        assert release.wait(10)
        real_write(*args)

    with patch.object(conversion, "write_new_file", side_effect=held_write):
        task = asyncio.create_task(guard.execute_approved(prepared))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (root / "source.json").exists()
    assert journal.entry_count(status="active") == 1
    assert journal.undo_last_write().kind is UndoKind.CREATE_UNDONE
    assert not (root / "source.json").exists()


async def test_cancel_during_journal_preparation_leaves_no_active_entry(setup):
    root, _outside, journal, guard = setup
    prepared = await guard.prepare_write(CONVERT_TOOL, arguments(root))
    started, release = threading.Event(), threading.Event()
    real_prepare = journal.prepare_write

    def held_prepare(*args):
        started.set()
        assert release.wait(10)
        return real_prepare(*args)

    with patch.object(journal, "prepare_write", side_effect=held_prepare):
        task = asyncio.create_task(guard.execute_approved(prepared))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not (root / "source.json").exists()
    assert journal.entry_count(status="active") == 0


async def test_partial_write_failure_leaves_no_orphan_file(setup):
    root, _outside, journal, guard = setup
    prepared = await guard.prepare_write(CONVERT_TOOL, arguments(root))
    real_write = os.write

    def failing_write(descriptor, data):
        real_write(descriptor, bytes(data[:1]))
        raise OSError("disk full")

    with patch.object(conversion.os, "write", side_effect=failing_write):
        with pytest.raises(OSError):
            await guard.execute_approved(prepared)
    assert not (root / "source.json").exists()
    assert journal.entry_count(status="active") == 0
