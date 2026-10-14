import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from voice_action.deletion import DELETE_TOOL, MAX_DELETE_BYTES
from voice_action.mcp_client import ToolRejectedError
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    ClarificationRequest,
    Completed,
    ModelTurn,
    Planner,
    ToolCall,
    _delete_scope_error,
    _destructive_request,
)
from voice_action.risk_guard import ApprovalChangedError, ApprovalInvalidError, RiskGuard
from voice_action.runtime import RuntimeController, RuntimeState
from voice_action.ui import render_outcome
from voice_action.undo import UndoJournal, UndoKind


def setup(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    journal = UndoJournal(tmp_path / "undo.sqlite3", (root,))
    gateway = AsyncMock()
    return root, journal, RiskGuard(gateway, journal), gateway


async def test_batch_approval_binary_backup_mode_and_one_shot_undo(tmp_path):
    root, journal, guard, gateway = setup(tmp_path)
    a, b = root / "a.txt", root / "b.bin"
    a.write_bytes(b"original")
    b.write_bytes(b"\xff\x00")
    a.chmod(0o640)
    prepared = await guard.prepare_write(DELETE_TOOL, {"paths": [str(a), str(b)]})
    assert a.exists() and b.exists()
    assert prepared.targets == (str(a), str(b))
    assert (await guard.execute_approved(prepared)).ok
    assert not a.exists() and not b.exists()
    gateway.call_tool.assert_not_awaited()
    with pytest.raises(ApprovalInvalidError):
        await guard.execute_approved(prepared)
    # 앱을 재시작해도 같은 journal에서 복원할 수 있다.
    reopened = UndoJournal(journal.path, (root,))
    assert reopened.undo_last_write().kind is UndoKind.DELETE_UNDONE
    assert a.read_bytes() == b"original" and b.read_bytes() == b"\xff\x00"
    assert a.stat().st_mode & 0o777 == 0o640
    assert reopened.undo_last_write().kind is UndoKind.NOTHING_TO_UNDO


async def test_reject_leaves_all_files_unchanged(tmp_path):
    root, _journal, guard, _gateway = setup(tmp_path)
    target = root / "a.txt"
    target.write_text("original")
    prepared = await guard.prepare_write(DELETE_TOOL, {"paths": [str(target)]})
    guard.reject(prepared)
    with pytest.raises(ApprovalInvalidError):
        await guard.execute_approved(prepared)
    assert target.read_text() == "original"


async def test_changed_file_requires_new_approval_and_old_nonce_is_consumed(tmp_path):
    root, journal, guard, _gateway = setup(tmp_path)
    target = root / "a.txt"
    target.write_text("original")
    prepared = await guard.prepare_write(DELETE_TOOL, {"paths": [str(target)]})
    target.write_text("changed")
    with pytest.raises(ApprovalChangedError) as changed:
        await guard.execute_approved(prepared)
    assert target.read_text() == "changed"
    with pytest.raises(ApprovalInvalidError):
        await guard.execute_approved(prepared)
    assert (await guard.execute_approved(changed.value.prepared)).ok
    journal.undo_last_write()
    assert target.read_text() == "changed"


async def test_pattern_freezes_list_and_skips_directories(tmp_path):
    root, journal, guard, _gateway = setup(tmp_path)
    target = root / "a.txt"
    target.write_text("a")
    (root / "nested.txt").mkdir()
    (root / "keep.bin").write_bytes(b"keep")
    prepared = await guard.prepare_write(DELETE_TOOL, {"directory": str(root), "pattern": "*.txt"})
    added = root / "added.txt"
    added.write_text("new")
    assert prepared.targets == (str(target),)
    assert (await guard.execute_approved(prepared)).ok
    assert added.exists() and (root / "keep.bin").exists() and (root / "nested.txt").is_dir()
    assert journal.undo_last_write().kind is UndoKind.DELETE_UNDONE


@pytest.mark.parametrize("kind", ["outside", "symlink", "directory", "parent_link", "journal"])
async def test_boundaries_are_rejected_before_deletion(tmp_path, kind):
    root, journal, guard, _gateway = setup(tmp_path)
    target = root / "a.txt"
    target.write_text("keep")
    if kind == "outside":
        target = tmp_path / "outside.txt"
        target.write_text("keep")
    elif kind == "symlink":
        link = root / "link.txt"
        link.symlink_to(target)
        target = link
    elif kind == "directory":
        target = root
    elif kind == "parent_link":
        link = root / "linked"
        link.symlink_to(root, target_is_directory=True)
        target = link / "a.txt"
    else:
        # journal이 허용 폴더 안에 있어도 삭제를 금지한다.
        journal = UndoJournal(root / "undo.sqlite3", (root,))
        guard = RiskGuard(AsyncMock(), journal)
        target = journal.path
    with pytest.raises(ToolRejectedError):
        await guard.prepare_write(DELETE_TOOL, {"paths": [str(target)]})
    assert target.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        {"paths": []},
        {"paths": ["relative.txt"]},
        {"paths": ["x", "x"]},
        {"directory": "ROOT", "pattern": "**/*.txt"},
        {"directory": "ROOT", "pattern": "../*"},
        {"directory": "ROOT", "pattern": "*.missing"},
    ],
)
async def test_invalid_selectors_rejected(tmp_path, arguments):
    root, _journal, guard, _gateway = setup(tmp_path)
    arguments = {key: str(root) if value == "ROOT" else value for key, value in arguments.items()}
    with pytest.raises(ToolRejectedError):
        await guard.prepare_write(DELETE_TOOL, arguments)


async def test_limits_rejected_without_deleting(tmp_path):
    root, _journal, guard, _gateway = setup(tmp_path)
    for index in range(101):
        (root / f"{index}.txt").write_text("x")
    with pytest.raises(ToolRejectedError):
        await guard.prepare_write(DELETE_TOOL, {"directory": str(root), "pattern": "*.txt"})
    huge = root / "huge.bin"
    with huge.open("wb") as stream:
        stream.truncate(MAX_DELETE_BYTES + 1)
    with pytest.raises(ToolRejectedError):
        await guard.prepare_write(DELETE_TOOL, {"paths": [str(huge)]})
    assert len(list(root.iterdir())) == 102


async def test_batch_undo_conflict_prevents_overwriting_or_partial_restore(tmp_path):
    root, journal, guard, _gateway = setup(tmp_path)
    a, b = root / "a.txt", root / "b.txt"
    for path in (a, b):
        path.write_text("original")
    prepared = await guard.prepare_write(DELETE_TOOL, {"paths": [str(a), str(b)]})
    await guard.execute_approved(prepared)
    b.write_text("new file")
    assert journal.undo_last_write().kind is UndoKind.CONFLICT
    assert not a.exists() and b.read_text() == "new file"
    b.unlink()
    assert journal.undo_last_write().kind is UndoKind.DELETE_UNDONE
    assert a.read_text() == b.read_text() == "original"


async def test_existing_write_entries_and_delete_undo_follow_time_order(tmp_path):
    root, journal, guard, _gateway = setup(tmp_path)
    old, target, new = root / "old.txt", root / "target.txt", root / "new.txt"
    journal.prepare_write(str(old), "create", "old")
    old.write_text("old")
    target.write_text("target")
    pending = await guard.prepare_write(DELETE_TOOL, {"paths": [str(target)]})
    await guard.execute_approved(pending)
    journal.prepare_write(str(new), "create", "new")
    new.write_text("new")
    reopened = UndoJournal(journal.path, (root,))
    assert reopened.undo_last_write().kind is UndoKind.CREATE_UNDONE
    assert reopened.undo_last_write().kind is UndoKind.DELETE_UNDONE
    assert reopened.undo_last_write().kind is UndoKind.CREATE_UNDONE
    assert target.read_text() == "target" and not old.exists() and not new.exists()


async def test_partial_failure_preserves_backup_and_undo_only_deleted_files(tmp_path, monkeypatch):
    root, journal, guard, _gateway = setup(tmp_path)
    a, b = root / "a.txt", root / "b.txt"
    for path in (a, b):
        path.write_text("original")
    pending = await guard.prepare_write(DELETE_TOOL, {"paths": [str(a), str(b)]})
    original_unlink = os.unlink

    def fail_second(path, *args, **kwargs):
        if path == "b.txt":
            raise PermissionError("injected")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_second)
    result = await guard.execute_approved(pending)
    assert not result.ok and "1/2" in result.error
    assert not a.exists() and b.exists()
    assert journal.undo_last_write().kind is UndoKind.DELETE_UNDONE
    assert a.read_text() == b.read_text() == "original"


async def test_cancel_waits_for_durable_delete_worker(tmp_path, monkeypatch):
    import threading

    root, journal, guard, _gateway = setup(tmp_path)
    target = root / "a.txt"
    target.write_text("original")
    pending = await guard.prepare_write(DELETE_TOOL, {"paths": [str(target)]})
    started, release = threading.Event(), threading.Event()
    execute = journal.deletions.execute

    def delayed(snapshots):
        started.set()
        release.wait(5)
        return execute(snapshots)

    monkeypatch.setattr(journal.deletions, "execute", delayed)
    task = asyncio.create_task(guard.execute_approved(pending))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not target.exists()
    assert journal.undo_last_write().kind is UndoKind.DELETE_UNDONE


@pytest.mark.parametrize(
    "text",
    [
        "a.txt 삭제하지 말고 읽어줘",
        "delete.txt 읽어줘",
        "don't delete a.txt",
        '"a.txt 삭제해줘"라는 문장을 읽어줘',
    ],
)
def test_non_delete_intent_does_not_enable_deletion(text):
    assert not _destructive_request(text)


def test_delete_scope_requires_user_intent_folder_and_pattern(tmp_path):
    root = tmp_path / "allowed"
    args = {"directory": str(root), "pattern": "*.txt"}
    assert _delete_scope_error(args, "allowed 폴더의 *.txt 파일을 삭제해줘", (root,)) is None
    assert _delete_scope_error(args, "다른 폴더의 *.txt 파일을 삭제해줘", (root,))
    assert _delete_scope_error(args, "allowed 폴더의 *.bin 파일을 삭제해줘", (root,))
    assert _delete_scope_error(args, "allowed 폴더의 *.txt 파일을 읽어줘", (root,))
    assert _delete_scope_error(
        {"directory": str(root), "pattern": "*"}, "allowed 폴더 파일을 삭제해줘", (root,)
    )
    assert _delete_scope_error({"paths": [str(root / "b.txt")]}, "a.txt 삭제해줘", (root,))
    assert _delete_scope_error(args, "allowed 폴더의 *.txt 중 a.txt 제외하고 삭제해줘", (root,))
    assert _delete_scope_error(
        {"paths": [str(root / "a.txt")]}, '"a.txt 삭제해줘"라는 문장 말고 b.txt 삭제해줘', (root,)
    )


async def test_backup_failure_never_deletes_files(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    root, journal, guard, _gateway = setup(tmp_path)
    target = root / "a.txt"
    target.write_text("original")
    pending = await guard.prepare_write(DELETE_TOOL, {"paths": [str(target)]})

    @contextmanager
    def broken_connection():
        raise sqlite3.OperationalError("injected backup failure")
        yield

    monkeypatch.setattr(journal, "_connect", broken_connection)
    with pytest.raises(ToolRejectedError):
        await guard.execute_approved(pending)
    assert target.read_text() == "original"


async def test_partial_restore_can_be_retried_without_overwriting_restored_files(
    tmp_path, monkeypatch
):
    root, journal, guard, _gateway = setup(tmp_path)
    a, b = root / "a.txt", root / "b.txt"
    for path in (a, b):
        path.write_text("original")
    pending = await guard.prepare_write(DELETE_TOOL, {"paths": [str(a), str(b)]})
    await guard.execute_approved(pending)
    original_link = os.link

    def fail_second(src, dst, *args, **kwargs):
        if dst == b:
            raise PermissionError("injected restore failure")
        return original_link(src, dst, *args, **kwargs)

    from voice_action.undo import UndoJournalError

    monkeypatch.setattr(os, "link", fail_second)
    with pytest.raises(UndoJournalError):
        journal.undo_last_write()
    assert a.read_text() == "original" and not b.exists()
    a.write_text("user edited restored file")
    monkeypatch.setattr(os, "link", original_link)
    assert journal.undo_last_write().kind is UndoKind.DELETE_UNDONE
    assert a.read_text() == "user edited restored file" and b.read_text() == "original"


async def test_runtime_batch_approval_ui_list_and_undo_without_gateway_delete(config, tmp_path):
    root = config.allowed_directories[0]
    a, b = root / "a.txt", root / "b.txt"
    for path in (a, b):
        path.write_text("original")
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.return_value = ModelTurn(
        None, [ToolCall("delete", DELETE_TOOL, json.dumps({"paths": [str(a), str(b)]}))]
    )
    planner = Planner(
        config, gateway, model, undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,))
    )
    runtime = RuntimeController(planner)
    pending = await runtime.submit("a.txt와 b.txt를 삭제해줘")
    assert isinstance(pending, ApprovalRequired)
    assert runtime.state is RuntimeState.WAITING_APPROVAL
    assert str(a) in render_outcome(pending) and str(b) in render_outcome(pending)
    assert a.exists() and b.exists()
    assert isinstance(await runtime.approve_pending(), Completed)
    assert not a.exists() and not b.exists()
    assert isinstance(await runtime.undo_last_write(), Completed)
    assert a.exists() and b.exists()
    gateway.call_tool.assert_not_awaited()
    assert model.generate.await_count == 1


async def test_hallucinated_delete_on_read_request_never_executes(config, tmp_path):
    target = config.allowed_directories[0] / "a.txt"
    target.write_text("keep")
    model, gateway = AsyncMock(), AsyncMock()
    model.generate.side_effect = [
        ModelTurn(None, [ToolCall("delete", DELETE_TOOL, json.dumps({"paths": [str(target)]}))]),
        ModelTurn("읽기 요청입니다.", []),
    ]
    gateway.list_tools.return_value = []
    planner = Planner(
        config,
        gateway,
        model,
        undo_journal=UndoJournal(tmp_path / "undo.sqlite3", config.allowed_directories),
    )
    outcome = await planner.run("a.txt 읽어줘")
    assert isinstance(outcome, Completed) and target.read_text() == "keep"
    assert DELETE_TOOL not in [tool.name for tool in model.generate.await_args_list[0].args[1]]
    gateway.call_tool.assert_not_awaited()


async def test_missing_journal_blocks_deletion(config):
    model, gateway = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.side_effect = [
        ModelTurn(
            None,
            [
                ToolCall(
                    "delete",
                    DELETE_TOOL,
                    json.dumps({"paths": [str(config.allowed_directories[0] / "한글 메모.txt")]}),
                )
            ],
        ),
        ModelTurn("삭제할 수 없습니다.", []),
    ]
    outcome = await Planner(config, gateway, model).run('"한글 메모.txt" 삭제해줘')
    assert not isinstance(outcome, ApprovalRequired)
    assert (config.allowed_directories[0] / "한글 메모.txt").exists()


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("candidate_count", [1, 2])
@pytest.mark.parametrize("approve", [False, True])
async def test_described_delete_searches_then_selects_and_asks_approval(
    config, tmp_path, retry, candidate_count, approve
):
    root = config.allowed_directories[0]
    target = root / "회의록-2026-10-03.md"
    target.write_text("지울 회의록")
    if candidate_count == 2:
        (root / "회의록-2026-10-01.md").write_text("보존")
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    gateway.validate_result_path.side_effect = Path
    find = ModelTurn(None, [ToolCall("f", "local__find_files",
                                    json.dumps({"keywords": ["회의록"]}))])
    turns = [find]
    if retry:
        turns.insert(0, ModelTurn(None, [], ClarificationRequest("어떤 작업인가요?", (),
                                                               "free_text", "q")))
    model.generate.side_effect = turns
    journal = UndoJournal(tmp_path / "delete.sqlite3", (root,))
    runtime = RuntimeController(Planner(config, gateway, model, undo_journal=journal))
    request = "10월 3일 회의록 삭제해줘."
    choices = await runtime.submit("다시" if retry else request)
    if retry:
        assert isinstance(choices, Clarification)
        choices = await runtime.submit(request)
    assert isinstance(choices, Clarification) and choices.context.kind == "file_candidate"
    assert target.exists()
    wrong = root / "한글 메모.txt"
    model.generate.side_effect = [
        ModelTurn(None, [ToolCall("wrong", DELETE_TOOL, json.dumps({"paths": [str(wrong)]}))]),
        ModelTurn(None, [ToolCall("d", DELETE_TOOL, json.dumps({"paths": [str(target)]}))]),
    ]
    pending = await runtime.submit(f"{choices.options.index(str(target)) + 1}번")
    assert isinstance(pending, ApprovalRequired) and pending.targets == (str(target),)
    assert runtime.state is RuntimeState.WAITING_APPROVAL and target.exists() and wrong.exists()
    messages, tools = model.generate.await_args_list[-2].args
    assert request in messages[1]["content"]
    assert DELETE_TOOL in [tool.name for tool in tools]
    assert isinstance(await (runtime.approve_pending() if approve else runtime.reject_pending()),
                      Completed)
    assert wrong.exists()
    if approve:
        assert not target.exists()
        assert isinstance(await runtime.undo_last_write(), Completed)
    assert target.read_text() == "지울 회의록"


async def test_described_delete_cannot_skip_search_or_candidate_selection(config, tmp_path):
    root = config.allowed_directories[0]
    target = root / "회의록-2026-10-03.md"
    target.write_text("보존")
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    guessed = ModelTurn(None, [ToolCall("d", DELETE_TOOL,
                                       json.dumps({"paths": [str(target)]}))])
    find = ModelTurn(None, [ToolCall("f", "local__find_files",
                                    json.dumps({"keywords": ["회의록"]}))])
    model.generate.side_effect = [guessed, find]
    planner = Planner(config, gateway, model,
                      undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,)))
    choices = await planner.run("10월 3일 회의록 삭제해줘")
    assert isinstance(choices, Clarification) and choices.options == (str(target),)
    assert target.read_text() == "보존"
    assert "사용자 명령에 명시되지" in model.generate.await_args_list[1].args[0][-1]["content"]


async def test_clarification_answer_cannot_turn_a_read_request_into_deletion(config, tmp_path):
    root = config.allowed_directories[0]
    target = root / "a.txt"
    target.write_text("보존")
    gateway, model = AsyncMock(), AsyncMock()
    gateway.list_tools.return_value = []
    model.generate.side_effect = [
        ModelTurn(None, [], ClarificationRequest("어떤 항목을 추출할까요?", (), "free_text", "q")),
        ModelTurn(None, [ToolCall("d", DELETE_TOOL, json.dumps({"paths": [str(target)]}))]),
        ModelTurn("삭제할 수 없습니다.", []),
    ]
    runtime = RuntimeController(Planner(
        config, gateway, model, undo_journal=UndoJournal(tmp_path / "undo.sqlite3", (root,))
    ))
    assert isinstance(await runtime.submit("a.txt에서 필요한 항목을 추출해줘"), Clarification)
    assert isinstance(await runtime.submit("a.txt 삭제해줘"), Completed)
    assert DELETE_TOOL not in [tool.name for tool in model.generate.await_args_list[1].args[1]]
    assert target.read_text() == "보존"
