"""Regressions for docs/reviews/2026-10-04-recent-changes-review.md (expected behavior)."""

import asyncio
import json
import os
import threading
from pathlib import Path
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


# ---- C05 CSV structure, C06 output limits, C07 converter and input errors ------------------


def csv_to_json(root, text: bytes | str):
    path = root / "data.csv"
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return conversion.prepare_conversion({"source": str(path), "target_format": "json"}, (root,))


@pytest.mark.parametrize(
    "text, message",
    [
        ("name,name\nfirst,second\n", "헤더"),
        ("name,\nfirst,second\n", "헤더"),
        ("a,b\n1\n", "열 수"),
        ("a,b\n1,2,3\n", "열 수"),
        ("", "비어"),
        ('a,b\n"open,2\n', "CSV"),
        (b"a,b\n\xff\xfe,1\n", "UTF-8"),
    ],
    ids=["duplicate", "empty-header", "short-row", "long-row", "empty", "bad-quote", "not-utf8"],
)
def test_csv_that_would_lose_or_garble_data_is_refused(setup, text, message):
    root = setup[0]
    with pytest.raises(ToolRejectedError, match=message):
        csv_to_json(root, text)


def test_csv_keeps_strings_bom_and_quoted_newlines(setup):
    root = setup[0]
    converted = csv_to_json(root, '\ufeff이름,메모\n점심,"줄1\n줄2"\n\n저녁,\n')
    assert json.loads(converted.data) == [
        {"이름": "점심", "메모": "줄1\n줄2"},
        {"이름": "저녁", "메모": ""},
    ]


def test_json_scalars_have_a_documented_csv_form(setup):
    root = setup[0]
    path = root / "data.json"
    path.write_text('[{"a": null, "b": 1.5, "c": true, "d": "x"}]')
    converted = conversion.prepare_conversion({"source": str(path), "target_format": "csv"},
                                              (root,))
    assert converted.data.decode() == "a,b,c,d\n,1.5,true,x\n"


@pytest.mark.parametrize("source, target, content", [
    ("data.csv", "json", "a\n1\n"),
    ("data.json", "csv", '[{"a": 1}]'),
])
def test_every_converter_respects_the_output_limit(setup, source, target, content):
    root = setup[0]
    (root / source).write_text(content)
    with patch.object(conversion, "MAX_OUTPUT_BYTES", 1):
        with pytest.raises(ToolRejectedError, match="50 MiB|크기"):
            conversion.prepare_conversion({"source": str(root / source),
                                           "target_format": target}, (root,))


def test_source_read_is_bounded(setup):
    root = setup[0]
    (root / "big.csv").write_text("a\n" + "1\n" * 100)
    with patch.object(conversion, "MAX_SOURCE_BYTES", 10):
        with pytest.raises(ToolRejectedError, match="20 MiB|크기"):
            conversion.prepare_conversion({"source": str(root / "big.csv"),
                                           "target_format": "json"}, (root,))


def test_converter_success_without_an_artifact_is_a_safe_refusal(setup):
    root = setup[0]
    with patch.object(conversion, "_run", return_value=b""):
        with pytest.raises(ToolRejectedError, match="결과 파일"):
            conversion.convert(root / "x.txt", b"review document", "txt", "docx")


def test_oversized_converter_artifact_is_refused_before_reading(setup):
    root = setup[0]

    def fake_run(command):
        Path(command[command.index("-output") + 1]).write_bytes(b"x" * 100)
        return b""

    with patch.object(conversion, "_run", side_effect=fake_run), \
            patch.object(conversion, "MAX_OUTPUT_BYTES", 10):
        with pytest.raises(ToolRejectedError, match="50 MiB|크기"):
            conversion.convert(root / "x.txt", b"review document", "txt", "docx")
