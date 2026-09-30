"""Local, approved file batches with durable backups in the existing journal."""

import hashlib
import json
import os
import stat
import tempfile
import time
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from voice_action.mcp_client import ToolDefinition, ToolRejectedError, ToolResult
from voice_action.undo import UndoJournalError, _fsync_directory

DELETE_TOOL = "local__delete_files"
MAX_DELETE_FILES = 100
MAX_DELETE_BYTES = 50 * 1024 * 1024
DELETE_DEFINITION = ToolDefinition(
    DELETE_TOOL,
    "사용자가 명시적으로 요청한 파일 삭제. paths로 단일/여러 절대경로를 지정하거나 "
    "directory와 pattern(예: *.txt, *)으로 해당 폴더 바로 아래 파일들을 선택한다. "
    "폴더/심볼릭 링크/재귀 삭제는 지원하지 않는다. 확정된 전체 목록을 승인받고 "
    "백업 후 삭제하며 Undo로 복원한다. 최대 100개, 합계 50 MiB.",
    {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "minItems": 1, "maxItems": MAX_DELETE_FILES, "uniqueItems": True},
            "directory": {"type": "string"},
            "pattern": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
        "oneOf": [
            {"required": ["paths"], "not": {"anyOf": [
                {"required": ["directory"]}, {"required": ["pattern"]}]}},
            {"required": ["directory", "pattern"], "not": {"required": ["paths"]}},
        ],
    },
)


class DeletionJournal:
    def __init__(self, journal):
        self.journal = journal
        with journal._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS delete_batches (
                    batch_id TEXT PRIMARY KEY, created_at INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'undone'))
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS delete_files (
                    batch_id TEXT NOT NULL, path TEXT NOT NULL, original BLOB NOT NULL,
                    mode INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('planned', 'deleting', 'deleted', 'restored')
                    ), PRIMARY KEY (batch_id, path)
                )
            """)

    def _path(self, raw, *, missing=False):
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            raise ToolRejectedError("삭제 경로가 올바르지 않습니다.")
        path = Path(raw)
        try:
            if (
                not path.is_absolute()
                or not any(path.is_relative_to(root) for root in self.journal._roots)
                or path.resolve(strict=not missing) != path
                or path.is_symlink()
                or path in {self.journal.path, Path(str(self.journal.path) + "-wal"),
                            Path(str(self.journal.path) + "-shm")}
            ):
                raise ToolRejectedError("삭제 대상의 허용 경로/링크/Undo 경계를 위반했습니다.")
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolRejectedError("삭제 경로를 확인할 수 없습니다.") from exc
        return path

    def _read(self, raw):
        path = self._path(raw)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DELETE_BYTES:
                raise ToolRejectedError("일반 파일만 삭제할 수 있고 백업은 최대 50 MiB입니다.")
            chunks, size = [], 0
            while chunk := os.read(descriptor, min(1024 * 1024, MAX_DELETE_BYTES + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_DELETE_BYTES:
                    raise ToolRejectedError("삭제 백업 크기는 최대 50 MiB입니다.")
            data = b"".join(chunks)
            current = os.lstat(path)
            after = os.fstat(descriptor)
            if (
                self._path(raw) != path
                or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
                or (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise ToolRejectedError("삭제 대상이 검사 중 변경되었습니다.")
            snapshot = (str(path), metadata.st_dev, metadata.st_ino,
                        metadata.st_mtime_ns, metadata.st_ctime_ns,
                        stat.S_IMODE(metadata.st_mode), hashlib.sha256(data).hexdigest())
            return data, snapshot
        finally:
            os.close(descriptor)

    def prepare(self, arguments):
        try:
            Draft202012Validator(DELETE_DEFINITION.schema).validate(arguments)
        except ValidationError as exc:
            raise ToolRejectedError("잘못된 삭제 도구 인자입니다.") from exc
        if "paths" in arguments:
            paths = arguments["paths"]
        else:
            directory = self._path(arguments["directory"])
            pattern = arguments["pattern"]
            if not directory.is_dir() or "/" in pattern or "\\" in pattern or "**" in pattern:
                raise ToolRejectedError("삭제 패턴은 폴더 바로 아래 파일만 선택할 수 있습니다.")
            paths = []
            for path in directory.glob(pattern):
                if path.is_symlink() or not path.is_dir():
                    paths.append(str(path))
                if len(paths) > MAX_DELETE_FILES:
                    raise ToolRejectedError("한 번에 최대 100개 파일을 삭제할 수 있습니다.")
        if not paths:
            raise ToolRejectedError("삭제 조건에 맞는 파일이 없습니다.")
        snapshots, total = [], 0
        for path in sorted(paths):
            data, snapshot = self._read(path)
            total += len(data)
            if total > MAX_DELETE_BYTES:
                raise ToolRejectedError("삭제 파일의 전체 백업 크기는 최대 50 MiB입니다.")
            snapshots.append(snapshot)
        return {"paths": [item[0] for item in snapshots]}, tuple(snapshots)

    def execute(self, snapshots):
        backups = []
        for expected in snapshots:
            data, current = self._read(expected[0])
            if current != expected:
                raise ToolRejectedError("승인 후 삭제 대상이 변경되어 삭제하지 않았습니다.")
            backups.append((data, current))
        batch = str(uuid.uuid4())
        with self.journal._connect() as connection:
            connection.execute("INSERT INTO delete_batches VALUES (?, ?, 'active')",
                               (batch, time.time_ns()))
            connection.executemany(
                "INSERT INTO delete_files VALUES (?, ?, ?, ?, ?, 'planned')",
                [(batch, item[0], data, item[5], item[6]) for data, item in backups],
            )
        deleted = 0
        try:
            for _data, expected in backups:
                _current_data, current = self._read(expected[0])
                if current != expected:
                    raise ToolRejectedError("삭제 실행 중 대상 파일이 변경되었습니다.")
                path = self._path(expected[0])
                parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    metadata = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    if (metadata.st_dev, metadata.st_ino) != expected[1:3]:
                        raise ToolRejectedError("삭제 실행 중 대상 파일이 교체되었습니다.")
                    self._status(batch, str(path), "deleting")
                    # ponytail: stat/unlink 사이 외부 변경은 OS 조건부 unlink가 필요하다.
                    os.unlink(path.name, dir_fd=parent)
                    deleted += 1
                    os.fsync(parent)
                    self._status(batch, str(path), "deleted")
                finally:
                    os.close(parent)
        except (OSError, ToolRejectedError, UndoJournalError) as exc:
            return ToolResult(False, "", error=(
                f"일괄 삭제 중단: {deleted}/{len(backups)}개 삭제됨. "
                f"삭제된 파일은 Undo로 복원할 수 있습니다. ({type(exc).__name__})"
            ))
        return ToolResult(True, f"{deleted}개 파일을 삭제했습니다. Undo로 복원할 수 있습니다.")

    def _status(self, batch, path, status):
        with self.journal._connect() as connection:
            connection.execute("UPDATE delete_files SET status=? WHERE batch_id=? AND path=?",
                               (status, batch, path))

    def latest(self):
        with self.journal._connect() as connection:
            return connection.execute(
                "SELECT batch_id, created_at FROM delete_batches "
                "WHERE status='active' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

    def undo(self, batch):
        with self.journal._connect() as connection:
            rows = connection.execute(
                "SELECT path, original, mode, fingerprint, status FROM delete_files "
                "WHERE batch_id=? AND status IN ('deleting', 'deleted') ORDER BY path",
                (batch,),
            ).fetchall()
        pending = []
        for raw, original, mode, fingerprint, status in rows:
            path = self._path(raw, missing=True)
            if hashlib.sha256(original).hexdigest() != fingerprint or not path.parent.is_dir():
                raise ToolRejectedError("삭제 백업 또는 복원 경계를 확인할 수 없습니다.")
            if os.path.lexists(path):
                if status == "deleting" and self._read(raw)[0] == original:
                    continue  # unlink가 실행되지 않았음.
                raise ToolRejectedError("삭제 후 같은 경로에 파일이 생겨 복원하지 않았습니다.")
            pending.append((path, original, mode))
        restored = 0
        for path, original, mode in pending:
            descriptor, temporary = tempfile.mkstemp(prefix=".voice-action-undo-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    os.fchmod(stream.fileno(), mode)
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._path(str(path), missing=True)
                os.link(temporary, path)  # 새 파일이 생겼으면 덮어쓰지 않고 실패.
                _fsync_directory(path.parent)
                self._status(batch, str(path), "restored")
                restored += 1
            except (OSError, ToolRejectedError, UndoJournalError) as exc:
                raise UndoJournalError(
                    f"{restored}개 복원 후 중단했습니다. 복원된 파일은 유지됩니다. Undo로 재시도하세요."
                ) from exc
            finally:
                os.unlink(temporary)
        with self.journal._connect() as connection:
            connection.execute("UPDATE delete_batches SET status='undone' WHERE batch_id=?", (batch,))
        return f"삭제한 파일 {restored}개를 복원했습니다."
