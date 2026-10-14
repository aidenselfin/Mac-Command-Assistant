import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from voice_action.deletion import DELETE_TOOL
from voice_action.file_operations import COPY_TOOL, MKDIR_TOOL, MOVE_TOOL
from voice_action.mcp_client import ToolRejectedError
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    ClarificationRequest,
    Completed,
    Error,
    ModelTurn,
    Planner,
    ToolCall,
)
from voice_action.risk_guard import ApprovalChangedError, ApprovalInvalidError, RiskGuard
from voice_action.runtime import RuntimeController, RuntimeState
from voice_action.ui import render_outcome
from voice_action.undo import UndoJournal, UndoKind


def setup(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"\xff\x00\xfe")
    source.chmod(0o640)
    folder = root / "보관"
    folder.mkdir()
    journal = UndoJournal(tmp_path / "undo.sqlite3", (root,))
    guard = RiskGuard(AsyncMock(), journal, (root,))
    return source, folder, journal, guard


@pytest.mark.parametrize("tool", [MOVE_TOOL, COPY_TOOL])
async def test_approved_transfer_preserves_binary_mode_and_undo_after_restart(tmp_path, tool):
    source, folder, journal, guard = setup(tmp_path)
    prepared = await guard.prepare_write(tool, {"source": str(source), "destination": str(folder)})
    destination = folder / source.name
    assert prepared.targets == (str(source),) and prepared.target_path == str(destination)
    assert source.exists() and not destination.exists()
    assert (await guard.execute_approved(prepared)).ok
    assert destination.read_bytes() == b"\xff\x00\xfe"
    assert destination.stat().st_mode & 0o777 == 0o640
    assert source.exists() == (tool == COPY_TOOL)
    with pytest.raises(ApprovalInvalidError):
        await guard.execute_approved(prepared)
    reopened = UndoJournal(journal.path, (source.parent,))
    assert reopened.undo_last_write().kind in {UndoKind.OPERATION_UNDONE, UndoKind.CREATE_UNDONE}
    assert source.read_bytes() == b"\xff\x00\xfe" and not destination.exists()


async def test_mkdir_undo_is_latest_and_refuses_nonempty_or_replaced_directory(tmp_path):
    source, folder, journal, guard = setup(tmp_path)
    destination = folder / "새폴더"
    prepared = await guard.prepare_write(MKDIR_TOOL, {"path": str(destination)})
    assert not destination.exists()
    await guard.execute_approved(prepared)
    occupant = destination / "user.txt"
    occupant.write_text("사용자 자료")
    assert journal.undo_last_write().kind is UndoKind.CONFLICT
    assert occupant.read_text() == "사용자 자료"
    occupant.unlink()
    transfer = await guard.prepare_write(MOVE_TOOL,
                                        {"source": str(source), "destination": str(destination)})
    await guard.execute_approved(transfer)
    assert journal.undo_last_write().kind is UndoKind.OPERATION_UNDONE
    assert source.exists() and destination.is_dir()
    assert journal.undo_last_write().kind is UndoKind.OPERATION_UNDONE
    assert not destination.exists()


@pytest.mark.parametrize("tool", [MOVE_TOOL, COPY_TOOL, MKDIR_TOOL])
async def test_rejection_and_missing_journal_never_change_paths(tmp_path, tool):
    source, folder, journal, guard = setup(tmp_path)
    arguments = ({"path": str(folder / "new")} if tool == MKDIR_TOOL
                 else {"source": str(source), "destination": str(folder)})
    prepared = await guard.prepare_write(tool, arguments)
    guard.reject(prepared)
    with pytest.raises(ApprovalInvalidError):
        await guard.execute_approved(prepared)
    assert source.exists() and list(folder.iterdir()) == []
    with pytest.raises(ToolRejectedError):
        await RiskGuard(AsyncMock(), None, (source.parent,)).prepare_write(tool, arguments)


@pytest.mark.parametrize("case", ["outside", "symlink", "parent_link", "directory", "existing"])
async def test_transfer_boundaries_and_no_overwrite(tmp_path, case):
    source, folder, journal, guard = setup(tmp_path)
    destination = folder / "target.bin"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    if case == "outside":
        destination = outside
    elif case == "symlink":
        source.unlink()
        source.symlink_to(outside)
    elif case == "parent_link":
        folder.rmdir()
        folder.symlink_to(tmp_path, target_is_directory=True)
    elif case == "directory":
        source = folder
    else:
        destination.write_bytes(b"existing")
    with pytest.raises((ToolRejectedError, OSError)):
        await guard.prepare_write(MOVE_TOOL, {"source": str(source),
                                              "destination": str(destination)})
    assert outside.read_bytes() == b"outside"
    if case == "existing":
        assert destination.read_bytes() == b"existing"


async def test_source_changes_require_reapproval_and_destination_conflict_stops_move(tmp_path):
    source, folder, journal, guard = setup(tmp_path)
    arguments = {"source": str(source), "destination": str(folder)}
    prepared = await guard.prepare_write(MOVE_TOOL, arguments)
    source.write_bytes(b"new")
    with pytest.raises(ApprovalChangedError) as changed:
        await guard.execute_approved(prepared)
    assert source.exists() and list(folder.iterdir()) == []
    (folder / source.name).write_bytes(b"user file")
    with pytest.raises(ToolRejectedError):
        await guard.execute_approved(changed.value.prepared)
    assert source.read_bytes() == b"new" and (folder / source.name).read_bytes() == b"user file"


@pytest.mark.parametrize("change", ["source_taken", "destination_edited", "destination_replaced"])
async def test_move_undo_never_overwrites_user_changes(tmp_path, change):
    source, folder, journal, guard = setup(tmp_path)
    prepared = await guard.prepare_write(
        MOVE_TOOL, {"source": str(source), "destination": str(folder)}
    )
    await guard.execute_approved(prepared)
    destination = folder / source.name
    if change == "source_taken":
        source.write_bytes(b"new source")
    elif change == "destination_edited":
        destination.write_bytes(b"edited")
    else:
        destination.unlink()
        destination.write_bytes(b"replacement")
    assert journal.undo_last_write().kind is UndoKind.CONFLICT
    assert destination.exists()
    if change == "source_taken":
        assert source.read_bytes() == b"new source"


async def test_move_cancel_waits_for_thread_and_can_undo(tmp_path, monkeypatch):
    import threading

    source, folder, journal, guard = setup(tmp_path)
    prepared = await guard.prepare_write(
        MOVE_TOOL, {"source": str(source), "destination": str(folder)}
    )
    started, release = threading.Event(), threading.Event()
    execute = journal.operations.execute

    def slow(*args):
        started.set()
        assert release.wait(5)
        return execute(*args)

    monkeypatch.setattr(journal.operations, "execute", slow)
    task = asyncio.create_task(guard.execute_approved(prepared))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert journal.undo_last_write().kind is UndoKind.OPERATION_UNDONE
    assert source.exists()


def call(name, **arguments):
    return ModelTurn(None, [ToolCall(name, name, json.dumps(arguments))])


async def test_recent_report_move_request_searches_then_approves_without_reading(config, tmp_path):
    root = config.allowed_directories[0]
    source, destination = root / "영어안내문.txt", root / "삭제연습"
    source.write_text("안내문")
    destination.mkdir()
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    gateway.validate_result_path.side_effect = Path
    model.generate.side_effect = [
        call("local__find_files", keywords=["영어안내문"]),
        call(MOVE_TOOL, source=str(source), destination=str(destination)),
        ModelTurn("이동 완료", []),
    ]
    runtime = RuntimeController(Planner(
        config, gateway, model, undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,))
    ))
    outcome = await runtime.submit("영어안내문 파일 삭제연습 폴더로 이동시켜줘.")
    assert isinstance(outcome, ApprovalRequired) and outcome.operation == "move"
    assert runtime.state is RuntimeState.WAITING_APPROVAL and source.exists()
    assert MOVE_TOOL in [t.name for t in model.generate.await_args_list[0].args[1]]
    assert "파일 삭제, 이동" in model.generate.await_args_list[0].args[0][0]["content"]
    assert str(source) in render_outcome(outcome) and str(destination) in render_outcome(outcome)
    done = await runtime.approve_pending()
    assert isinstance(done, Completed) and done.changed_paths == (str(destination / source.name),)
    gateway.call_tool.assert_not_awaited()
    assert not source.exists()
    assert isinstance(await runtime.undo_last_write(), Completed) and source.exists()


async def test_move_asks_for_destination_and_uses_the_users_answer(config, tmp_path):
    root = config.allowed_directories[0]
    source, folder = root / "a.txt", root / "보관"
    source.write_text("a")
    folder.mkdir()
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.side_effect = [
        ModelTurn(None, [], ClarificationRequest("어느 폴더로 옮길까요?", (), "free_text", "q")),
        call(MOVE_TOOL, source=str(source), destination=str(folder)),
    ]
    runtime = RuntimeController(Planner(
        config, gateway, model, undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,))
    ))
    assert isinstance(await runtime.submit("a.txt를 옮겨줘"), Clarification)
    assert isinstance(await runtime.submit("보관 폴더"), ApprovalRequired)
    assert source.exists()


@pytest.mark.parametrize("user_text", ["a.txt 읽어줘", "a.txt 이동하지 말고 읽어줘",
                                     "https://example.com 페이지 읽어줘"])
async def test_transfer_cannot_run_on_read_negated_or_web_requests(config, tmp_path, user_text):
    root = config.allowed_directories[0]
    source = root / "a.txt"
    source.write_text("보존")
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.side_effect = [
        call(MOVE_TOOL, source=str(source), destination=str(root / "b.txt")),
        ModelTurn("실행하지 못했습니다", []), ModelTurn("실패", []), ModelTurn("실패", []),
    ]
    planner = Planner(config, gateway, model,
                      undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,)))
    outcome = await planner.run(user_text)
    assert not isinstance(outcome, ApprovalRequired)
    assert source.read_text() == "보존" and not (root / "b.txt").exists()
    assert MOVE_TOOL not in [t.name for t in model.generate.await_args_list[0].args[1]]


async def test_move_never_reports_completion_without_a_real_operation(config):
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.side_effect = [ModelTurn("이동 도구가 없습니다.", []),
                                 ModelTurn("이동했습니다.", [])]
    outcome = await Planner(config, gateway, model).run("a.txt를 보관 폴더로 이동해줘")
    assert isinstance(outcome, Error) and "실행하지 못했습니다" in outcome.message
    assert model.generate.await_count == 2


async def test_partial_move_is_journaled_and_undo_keeps_the_original(tmp_path, monkeypatch):
    source, folder, journal, guard = setup(tmp_path)
    prepared = await guard.prepare_write(
        MOVE_TOOL, {"source": str(source), "destination": str(folder)}
    )
    unlink = os.unlink

    def fail_source(path, *args, **kwargs):
        if path == source.name:
            raise OSError("injected source unlink failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_source)
    with pytest.raises(ToolRejectedError):
        await guard.execute_approved(prepared)
    assert source.read_bytes() == (folder / source.name).read_bytes()
    monkeypatch.setattr(os, "unlink", unlink)
    assert journal.undo_last_write().kind is UndoKind.OPERATION_UNDONE
    assert source.read_bytes() == b"\xff\x00\xfe" and not (folder / source.name).exists()


async def test_mkdir_undo_does_not_remove_a_replacement(tmp_path):
    source, folder, journal, guard = setup(tmp_path)
    destination = folder / "new"
    prepared = await guard.prepare_write(MKDIR_TOOL, {"path": str(destination)})
    await guard.execute_approved(prepared)
    destination.rename(folder / "original")
    destination.mkdir()
    assert journal.undo_last_write().kind is UndoKind.CONFLICT
    assert destination.is_dir()


async def test_journal_failure_happens_before_any_file_change(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from voice_action.undo import UndoJournalError

    source, folder, journal, guard = setup(tmp_path)
    prepared = await guard.prepare_write(
        MOVE_TOOL, {"source": str(source), "destination": str(folder)}
    )

    @contextmanager
    def broken():
        raise UndoJournalError("injected journal failure")
        yield

    monkeypatch.setattr(journal, "_connect", broken)
    with pytest.raises(ToolRejectedError):
        await guard.execute_approved(prepared)
    assert source.exists() and list(folder.iterdir()) == []


async def test_a_false_claim_that_deletion_is_unavailable_is_corrected(config, tmp_path):
    root = config.allowed_directories[0]
    target = root / "a.txt"
    target.write_text("보존")
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.side_effect = [ModelTurn("파일 삭제 도구가 없습니다.", []),
                                 call(DELETE_TOOL, paths=[str(target)])]
    planner = Planner(config, gateway, model,
                      undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,)))
    pending = await planner.run("a.txt를 삭제해줘")
    assert isinstance(pending, ApprovalRequired) and target.exists()
    assert "현재 지원·제공되는 도구" in model.generate.await_args_list[1].args[0][-1]["content"]


async def test_rename_reuses_move_and_reports_only_the_new_path(config, tmp_path):
    root = config.allowed_directories[0]
    source, destination = root / "a.txt", root / "b.txt"
    source.write_text("내용")
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.side_effect = [call(MOVE_TOOL, source=str(source), destination=str(destination)),
                                 ModelTurn("이름 변경 완료", [])]
    runtime = RuntimeController(Planner(
        config, gateway, model, undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,))
    ))
    pending = await runtime.submit("a.txt 이름을 b.txt로 바꿔줘")
    assert isinstance(pending, ApprovalRequired)
    assert (await runtime.approve_pending()).changed_paths == (str(destination),)
    assert not source.exists() and destination.read_text() == "내용"


async def test_mkdir_then_move_has_separate_approvals_and_undos(config, tmp_path):
    root = config.allowed_directories[0]
    source, folder = root / "a.txt", root / "보관"
    source.write_text("원본")
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.side_effect = [ModelTurn(None, [
        ToolCall("mkdir", MKDIR_TOOL, json.dumps({"path": str(folder)})),
        ToolCall("move", MOVE_TOOL, json.dumps(
            {"source": str(source), "destination": str(folder)}
        )),
    ]), ModelTurn("이동 완료", [])]
    runtime = RuntimeController(Planner(
        config, gateway, model, undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,))
    ))
    first = await runtime.submit("a.txt를 새 보관 폴더로 이동해줘")
    assert isinstance(first, ApprovalRequired) and first.operation == "mkdir"
    assert source.exists() and not folder.exists()
    second = await runtime.approve_pending()
    assert isinstance(second, ApprovalRequired) and second.operation == "move"
    assert source.exists() and folder.is_dir()
    assert isinstance(await runtime.approve_pending(), Completed)
    assert not source.exists() and (folder / source.name).read_text() == "원본"
    assert isinstance(await runtime.undo_last_write(), Completed) and source.exists()
    assert isinstance(await runtime.undo_last_write(), Completed) and not folder.exists()


async def test_rejecting_move_keeps_and_reports_an_already_approved_directory(config, tmp_path):
    root = config.allowed_directories[0]
    source, folder = root / "a.txt", root / "보관"
    source.write_text("원본")
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.return_value = ModelTurn(None, [
        ToolCall("mkdir", MKDIR_TOOL, json.dumps({"path": str(folder)})),
        ToolCall("move", MOVE_TOOL, json.dumps(
            {"source": str(source), "destination": str(folder)}
        )),
    ])
    runtime = RuntimeController(Planner(
        config, gateway, model, undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,))
    ))
    assert (await runtime.submit("a.txt를 새 보관 폴더로 이동해줘")).operation == "mkdir"
    assert (await runtime.approve_pending()).operation == "move"
    outcome = await runtime.reject_pending()
    assert "앞서 승인한 변경은 유지" in outcome.text and outcome.changed_paths == (str(folder),)
    assert source.exists() and folder.is_dir()
    assert isinstance(await runtime.undo_last_write(), Completed) and not folder.exists()


@pytest.mark.parametrize("text", ["복사본.txt 읽어줘", "이동계획.md 요약해줘",
                                 "move.txt 읽어줘", "a.txt 이름 변경하지 말고 읽어줘"])
async def test_operation_words_in_filenames_and_negations_do_not_enable_tools(config, text):
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.return_value = ModelTurn("답", [])
    assert isinstance(await Planner(config, gateway, model).run(text), Completed)
    offered = {tool.name for tool in model.generate.await_args.args[1]}
    assert MOVE_TOOL not in offered and COPY_TOOL not in offered and MKDIR_TOOL not in offered


async def test_copy_checks_destination_and_keeps_source(config, tmp_path):
    root = config.allowed_directories[0]
    source, folder = root / "a.txt", root / "보관"
    source.write_text("원본")
    folder.mkdir()
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.side_effect = [
        call(COPY_TOOL, source=str(source), destination=str(root / "wrong.txt")),
        call(COPY_TOOL, source=str(source), destination=str(folder)),
        ModelTurn("복사 완료", []),
    ]
    runtime = RuntimeController(Planner(
        config, gateway, model, undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,))
    ))
    pending = await runtime.submit("a.txt를 보관 폴더로 복사해줘")
    assert isinstance(pending, ApprovalRequired) and pending.operation == "copy"
    assert "명시되지" in model.generate.await_args_list[1].args[0][-1]["content"]
    await runtime.approve_pending()
    assert source.read_text() == (folder / source.name).read_text() == "원본"
    await runtime.undo_last_write()
    assert source.exists() and not (folder / source.name).exists()
