"""Approved, no-clobber file moves, copies and empty directory creation."""

import hashlib
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from voice_action.deletion import MAX_DELETE_BYTES
from voice_action.mcp_client import ToolDefinition, ToolRejectedError, ToolResult
from voice_action.undo import UndoJournalError

MOVE_TOOL = "local__move_file"
COPY_TOOL = "local__copy_file"
MKDIR_TOOL = "local__create_directory"
OPERATIONS = {MOVE_TOOL: "move", COPY_TOOL: "copy", MKDIR_TOOL: "mkdir"}
DEFINITIONS = {
    MOVE_TOOL: ToolDefinition(
        MOVE_TOOL,
        "파일 하나를 이동하거나 이름 변경한다. source는 파일 절대경로, destination은 새 파일 "
        "절대경로 또는 기존 폴더 절대경로. 승인 후 실행하며 Undo 가능. 덮어쓰기·폴더 이동·"
        "다른 파일시스템으로 이동은 지원하지 않는다. 복사 후 삭제로 대신하지 마라.",
        {"type": "object", "properties": {"source": {"type": "string"},
                                         "destination": {"type": "string"}},
         "required": ["source", "destination"], "additionalProperties": False},
    ),
    COPY_TOOL: ToolDefinition(
        COPY_TOOL,
        "파일 하나를 복사한다. source는 파일 절대경로, destination은 새 파일 절대경로 또는 "
        "기존 폴더 절대경로. 원본을 유지하고 승인 후 새 파일 생성. 덮어쓰기 불가, Undo 가능.",
        {"type": "object", "properties": {"source": {"type": "string"},
                                         "destination": {"type": "string"}},
         "required": ["source", "destination"], "additionalProperties": False},
    ),
    MKDIR_TOOL: ToolDefinition(
        MKDIR_TOOL,
        "허용 폴더 안에 새 폴더 하나를 만든다. path는 절대경로, 부모 폴더는 존재해야 한다. "
        "승인 필요. Undo는 만든 폴더가 비어 있고 같은 폴더일 때만 제거한다.",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "required": ["path"], "additionalProperties": False},
    ),
}


def _identity(metadata) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _file_state(parent: int, name: str) -> str:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ToolRejectedError("일반 파일만 이동·복사할 수 있습니다.")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            if size > MAX_DELETE_BYTES:
                raise ToolRejectedError("파일 작업은 최대 50 MiB입니다.")
            digest.update(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            _identity(current) != _identity(before)
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise ToolRejectedError("파일이 검사 중 변경되었습니다.")
        return f"{before.st_dev}:{before.st_ino}:{digest.hexdigest()}"
    finally:
        os.close(descriptor)


class FileOperationJournal:
    def __init__(self, journal):
        self.journal = journal
        self._roots = {root: _identity(root.stat()) for root in journal._roots}
        with journal._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS path_operations (
                    id TEXT PRIMARY KEY, tool TEXT NOT NULL, source TEXT NOT NULL,
                    destination TEXT NOT NULL, expected TEXT NOT NULL, parents TEXT NOT NULL,
                    status TEXT NOT NULL, created_at INTEGER NOT NULL
                )
            """)

    @contextmanager
    def _parent(self, path: Path):
        root = next(root for root in self._roots if path.is_relative_to(root))
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _identity(os.fstat(descriptor)) != self._roots[root]:
                raise ToolRejectedError("허용 폴더가 교체되어 작업을 중단했습니다.")
            for part in path.relative_to(root).parent.parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def prepare(self, tool: str, arguments: dict) -> tuple[dict, tuple]:
        try:
            Draft202012Validator(DEFINITIONS[tool].schema).validate(arguments)
        except ValidationError as exc:
            raise ToolRejectedError("잘못된 파일 작업 인자입니다.") from exc
        source, snapshot = None, ()
        if tool != MKDIR_TOOL:
            source = self.journal.deletions._path(arguments["source"])
            _, snapshot = self.journal.deletions._read(str(source))
        destination = self.journal.deletions._path(
            arguments["path" if tool == MKDIR_TOOL else "destination"], missing=True
        )
        if source is not None and destination.is_dir():
            destination /= source.name
            self.journal.deletions._path(str(destination), missing=True)
        if os.path.lexists(destination):
            raise ToolRejectedError("대상 경로가 이미 있습니다. 덮어쓰지 않습니다.")
        with self._parent(destination) as target_parent:
            target_identity = _identity(os.fstat(target_parent))
        source_identity = ()
        if source is not None:
            with self._parent(source) as source_parent:
                source_identity = _identity(os.fstat(source_parent))
            # shortcut: 이동은 같은 파일시스템만 지원, 다른 디스크 이동 수요가 생기면 확장.
            if tool == MOVE_TOOL and source_identity[0] != target_identity[0]:
                raise ToolRejectedError("다른 파일시스템으로 이동은 지원하지 않습니다.")
        normalized = (
            {"path": str(destination)} if source is None
            else {"source": str(source), "destination": str(destination)}
        )
        return normalized, (snapshot, source_identity, target_identity)

    def _status(self, entry: str, status: str, expected: str | None = None) -> None:
        with self.journal._connect() as connection:
            connection.execute(
                "UPDATE path_operations SET status=?, expected=COALESCE(?, expected) WHERE id=?",
                (status, expected, entry),
            )

    def execute(self, tool: str, arguments: dict, snapshot: tuple) -> ToolResult:
        normalized, current = self.prepare(tool, arguments)
        if normalized != arguments or current != snapshot:
            raise ToolRejectedError("승인 후 파일 또는 부모 폴더가 변경되었습니다.")
        destination = Path(arguments.get("destination", arguments.get("path")))
        if tool == COPY_TOOL:
            return self._copy(arguments, destination, snapshot)
        source = Path(arguments["source"]) if tool == MOVE_TOOL else None
        expected = (
            f"{snapshot[0][1]}:{snapshot[0][2]}:{snapshot[0][-1]}" if source is not None else ""
        )
        entry = uuid.uuid4().hex
        with self.journal._connect() as connection:
            connection.execute("INSERT INTO path_operations VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                               (entry, tool, str(source) if source else "", str(destination),
                                expected, json.dumps(snapshot[1:]), time.time_ns()))
        created = False
        try:
            with self._parent(destination) as target_parent:
                if _identity(os.fstat(target_parent)) != snapshot[2]:
                    raise ToolRejectedError("대상 부모 폴더가 변경되었습니다.")
                if source is None:
                    os.mkdir(destination.name, 0o755, dir_fd=target_parent)
                    created = True
                    metadata = os.stat(destination.name, dir_fd=target_parent,
                                       follow_symlinks=False)
                    self._status(entry, "active", f"{metadata.st_dev}:{metadata.st_ino}")
                else:
                    with self._parent(source) as source_parent:
                        if (_identity(os.fstat(source_parent)) != snapshot[1]
                            or _file_state(source_parent, source.name) != expected):
                            raise ToolRejectedError("원본 파일 또는 부모 폴더가 변경되었습니다.")
                        os.link(source.name, destination.name, src_dir_fd=source_parent,
                                dst_dir_fd=target_parent, follow_symlinks=False)
                        created = True
                        os.fsync(target_parent)
                        if (_file_state(source_parent, source.name) != expected
                            or _file_state(target_parent, destination.name) != expected):
                            raise ToolRejectedError(
                                "이동 중 파일이 교체되어 원본을 지우지 않았습니다."
                            )
                        # shortcut: 검사/unlink 사이 외부 교체는 OS 조건부 unlink가 필요하다.
                        os.unlink(source.name, dir_fd=source_parent)
                        os.fsync(source_parent)
                os.fsync(target_parent)
        except (OSError, ToolRejectedError, UndoJournalError):
            if not created:
                self._status(entry, "failed")
            raise
        action = "폴더 생성" if source is None else "이동"
        return ToolResult(True, f"{action} 완료: {destination}. Undo로 되돌릴 수 있습니다.")

    def _copy(self, arguments: dict, destination: Path, snapshot: tuple) -> ToolResult:
        data, current = self.journal.deletions._read(arguments["source"])
        if current != snapshot[0]:
            raise ToolRejectedError("복사 원본이 변경되었습니다.")
        entry = self.journal.prepare_write(str(destination), "create", data)
        try:
            with self._parent(destination) as parent:
                if _identity(os.fstat(parent)) != snapshot[2]:
                    raise ToolRejectedError("대상 부모 폴더가 변경되었습니다.")
                descriptor = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                created = os.fstat(descriptor)
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(descriptor, view)
                        if not written:
                            raise OSError("파일 쓰기를 완료할 수 없습니다.")
                        view = view[written:]
                    os.fchmod(descriptor, snapshot[0][5])
                    os.fsync(descriptor)
                except BaseException:
                    if _identity(os.stat(destination.name, dir_fd=parent,
                                         follow_symlinks=False)) == _identity(created):
                        os.unlink(destination.name, dir_fd=parent)
                    raise
                finally:
                    os.close(descriptor)
                os.fsync(parent)
        except BaseException:
            self.journal.reconcile_uncertain_write(entry)
            raise
        return ToolResult(True, f"복사 완료: {destination}. 원본은 유지됩니다. Undo 가능.")

    def latest(self):
        with self.journal._connect() as connection:
            return connection.execute(
                "SELECT id, created_at FROM path_operations WHERE status='active' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

    def undo(self, entry: str) -> str:
        with self.journal._connect() as connection:
            tool, raw_source, raw_destination, expected, parents = connection.execute(
                "SELECT tool, source, destination, expected, parents FROM path_operations "
                "WHERE id=? AND status='active'", (entry,)
            ).fetchone()
        source_parent_id, target_parent_id = json.loads(parents)
        destination = self.journal.deletions._path(raw_destination, missing=True)
        with self._parent(destination) as target_parent:
            if list(_identity(os.fstat(target_parent))) != target_parent_id:
                raise ToolRejectedError("Undo 대상 부모 폴더가 교체되었습니다.")
            if tool == MKDIR_TOOL:
                if not os.path.lexists(destination):
                    self._status(entry, "undone")
                    return "만든 폴더가 이미 없어 파일을 변경하지 않았습니다."
                metadata = os.stat(destination.name, dir_fd=target_parent, follow_symlinks=False)
                if (not stat.S_ISDIR(metadata.st_mode)
                    or f"{metadata.st_dev}:{metadata.st_ino}" != expected):
                    raise ToolRejectedError("만든 폴더가 교체되어 되돌리지 않았습니다.")
                os.rmdir(destination.name, dir_fd=target_parent)
            else:
                source = self.journal.deletions._path(raw_source, missing=True)
                with self._parent(source) as source_parent:
                    if list(_identity(os.fstat(source_parent))) != source_parent_id:
                        raise ToolRejectedError("Undo 원본 부모 폴더가 교체되었습니다.")
                    if not os.path.lexists(destination):
                        if _file_state(source_parent, source.name) != expected:
                            raise ToolRejectedError("이동 결과를 확인할 수 없습니다.")
                        self._status(entry, "undone")
                        return "원본이 이미 제자리에 있어 파일을 변경하지 않았습니다."
                    if _file_state(target_parent, destination.name) != expected:
                        raise ToolRejectedError("이동한 파일이 변경되어 되돌리지 않았습니다.")
                    if os.path.lexists(source):
                        if _file_state(source_parent, source.name) != expected:
                            raise ToolRejectedError(
                                "원래 위치에 다른 파일이 있어 되돌리지 않았습니다."
                            )
                    else:
                        os.link(destination.name, source.name, src_dir_fd=target_parent,
                                dst_dir_fd=source_parent, follow_symlinks=False)
                        os.fsync(source_parent)
                    if (_file_state(target_parent, destination.name) != expected
                        or _file_state(source_parent, source.name) != expected):
                        raise ToolRejectedError("Undo 중 파일이 변경되었습니다.")
                    os.unlink(destination.name, dir_fd=target_parent)
            os.fsync(target_parent)
        self._status(entry, "undone")
        return "파일 이동을 되돌렸습니다." if tool == MOVE_TOOL else "만든 빈 폴더를 되돌렸습니다."
