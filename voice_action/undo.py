"""Local SQLite journal for undoing the latest approved filesystem write."""

import hashlib
import os
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

DEFAULT_JOURNAL_PATH = Path(".voice-action/undo.sqlite3")
CREATE_UNDONE_MESSAGE = "생성한 파일을 되돌렸습니다."
OVERWRITE_UNDONE_MESSAGE = "이전 파일 내용을 복원했습니다."
NOTHING_TO_UNDO_MESSAGE = "되돌릴 수 있는 최근 파일 쓰기가 없습니다."
CONFLICT_MESSAGE = "파일이 write 후 변경되어 되돌리지 않았습니다."
BOUNDARY_ERROR_MESSAGE = "Undo 안전 경계를 확인할 수 없어 파일을 변경하지 않았습니다."


class UndoJournalError(RuntimeError):
    pass


class UndoKind(Enum):
    CREATE_UNDONE = auto()
    OVERWRITE_UNDONE = auto()
    DELETE_UNDONE = auto()
    NOTHING_TO_UNDO = auto()
    CONFLICT = auto()
    BOUNDARY_ERROR = auto()


@dataclass(frozen=True)
class UndoResult:
    kind: UndoKind
    message: str


@dataclass(frozen=True)
class JournalEntry:
    command_id: str
    canonical_path: str
    operation: str
    post_fingerprint: str


class _UndoBoundaryError(RuntimeError):
    pass


class _UndoConflict(RuntimeError):
    pass


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular_file(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _UndoBoundaryError from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise _UndoBoundaryError
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), metadata
    except OSError as exc:
        raise _UndoBoundaryError from exc
    finally:
        os.close(descriptor)


def _same_file(path: Path, expected: os.stat_result) -> bool:
    try:
        current = os.lstat(path)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(current.st_mode)
        and stat.S_ISREG(current.st_mode)
        and current.st_dev == expected.st_dev
        and current.st_ino == expected.st_ino
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class UndoJournal:
    """Durable write-ahead records and one-at-a-time filesystem restoration."""

    def __init__(self, path: Path, allowed_roots: tuple[Path, ...]):
        raw_path = Path(path).absolute()
        try:
            self._roots = tuple(root.resolve(strict=True) for root in allowed_roots)
        except (OSError, RuntimeError) as exc:
            raise UndoJournalError("Undo journal의 허용 경로를 준비할 수 없습니다.") from exc
        if not self._roots or any(not root.is_dir() for root in self._roots):
            raise UndoJournalError("Undo journal에 유효한 허용 경로가 필요합니다.")
        try:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            self.path = raw_path.parent.resolve(strict=True) / raw_path.name
            self._verify_journal_path()
            with self._connect() as connection:
                mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).casefold() != "wal":
                    raise UndoJournalError("Undo journal WAL mode를 활성화할 수 없습니다.")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS undo_entries (
                        command_id TEXT PRIMARY KEY,
                        canonical_path TEXT NOT NULL,
                        operation TEXT NOT NULL CHECK (operation IN ('create', 'overwrite')),
                        original_bytes BLOB,
                        post_fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('active', 'undoing', 'undone', 'conflict', 'failed')
                        ),
                        created_at INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS undo_entries_latest
                    ON undo_entries(status, created_at DESC)
                    """
                )
            from voice_action.deletion import DeletionJournal

            self.deletions = DeletionJournal(self)
        except UndoJournalError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise UndoJournalError("Undo journal을 초기화할 수 없습니다.") from exc

    @classmethod
    def default(cls, allowed_roots: tuple[Path, ...]) -> "UndoJournal":
        return cls(Path.cwd() / DEFAULT_JOURNAL_PATH, allowed_roots)

    @contextmanager
    def _connect(self):
        self._verify_journal_path()
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _verify_journal_path(self) -> None:
        try:
            metadata = os.lstat(self.path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise UndoJournalError("Undo journal 경계를 확인할 수 없습니다.") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise UndoJournalError("Undo journal 경계를 확인할 수 없습니다.")

    def prepare_write(
        self, canonical_path: str, operation: str, content: str
    ) -> JournalEntry:
        """Persist the recovery data before the approved transport call."""
        path = Path(canonical_path)
        if not path.is_absolute() or operation not in {"create", "overwrite"}:
            raise UndoJournalError("Undo journal에 안전한 write 정보를 기록할 수 없습니다.")
        if not isinstance(content, str):
            raise UndoJournalError("Undo journal은 텍스트 write만 지원합니다.")
        try:
            if operation == "overwrite":
                original, _metadata = _read_regular_file(path)
                original.decode("utf-8")
            else:
                try:
                    os.lstat(path)
                except FileNotFoundError:
                    pass
                else:
                    raise UndoJournalError(
                        "Undo journal 준비 중 write 대상 상태가 변경되었습니다."
                    )
                original = None
            expected = _fingerprint(content.encode("utf-8"))
            entry = JournalEntry(
                str(uuid.uuid4()), canonical_path, operation, expected
            )
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO undo_entries (
                        command_id, canonical_path, operation, original_bytes,
                        post_fingerprint, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        entry.command_id,
                        entry.canonical_path,
                        entry.operation,
                        original,
                        entry.post_fingerprint,
                        time.time_ns(),
                    ),
                )
            return entry
        except UnicodeDecodeError as exc:
            raise UndoJournalError(
                "Undo는 UTF-8 텍스트 파일 overwrite만 지원합니다."
            ) from exc
        except UndoJournalError:
            raise
        except _UndoBoundaryError as exc:
            raise UndoJournalError(
                "Undo journal을 준비하지 못해 파일을 쓰지 않았습니다."
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            raise UndoJournalError(
                "Undo journal을 기록하지 못해 파일을 쓰지 않았습니다."
            ) from exc

    def mark_write_failed(self, entry: JournalEntry) -> None:
        self._set_status(entry.command_id, "failed", expected="active")

    def reconcile_uncertain_write(self, entry: JournalEntry) -> None:
        """Keep an uncertain write undoable only when exact output is present."""
        matches = False
        try:
            current, _metadata = _read_regular_file(Path(entry.canonical_path))
            matches = _fingerprint(current) == entry.post_fingerprint
        except _UndoBoundaryError:
            pass
        if not matches:
            self._set_status(entry.command_id, "failed", expected="active")

    def undo_last_write(self) -> UndoResult:
        try:
            latest_delete = self.deletions.latest()
            with self._connect() as connection:
                latest_write = connection.execute(
                    "SELECT MAX(created_at) FROM undo_entries WHERE status='active'"
                ).fetchone()[0]
            if latest_delete and (latest_write is None or latest_delete[1] > latest_write):
                from voice_action.mcp_client import ToolRejectedError

                try:
                    message = self.deletions.undo(latest_delete[0])
                except ToolRejectedError as exc:
                    return UndoResult(UndoKind.CONFLICT, str(exc))
                return UndoResult(UndoKind.DELETE_UNDONE, message)
        except (OSError, sqlite3.Error) as exc:
            raise UndoJournalError("삭제 Undo journal을 처리할 수 없습니다.") from exc
        row = self._claim_latest()
        if row is None:
            return UndoResult(UndoKind.NOTHING_TO_UNDO, NOTHING_TO_UNDO_MESSAGE)
        command_id, raw_path, operation, original, expected = row
        try:
            path, _current, metadata = self._validated_current(
                raw_path, operation, original, expected
            )
            if operation == "create":
                if not _same_file(path, metadata):
                    raise _UndoConflict
                os.unlink(path)
                _fsync_directory(path.parent)
                kind, message = UndoKind.CREATE_UNDONE, CREATE_UNDONE_MESSAGE
            else:
                self._atomic_restore(path, original, metadata)
                kind, message = (
                    UndoKind.OVERWRITE_UNDONE,
                    OVERWRITE_UNDONE_MESSAGE,
                )
            self._set_status(command_id, "undone", expected="undoing")
            return UndoResult(kind, message)
        except _UndoConflict:
            self._set_status(command_id, "conflict", expected="undoing")
            return UndoResult(UndoKind.CONFLICT, CONFLICT_MESSAGE)
        except _UndoBoundaryError:
            self._set_status(command_id, "conflict", expected="undoing")
            return UndoResult(UndoKind.BOUNDARY_ERROR, BOUNDARY_ERROR_MESSAGE)
        except (OSError, sqlite3.Error) as exc:
            try:
                self._set_status(command_id, "conflict", expected="undoing")
            except UndoJournalError:
                pass
            raise UndoJournalError(
                "Undo 처리 중 안전하게 파일을 복구할 수 없었습니다."
            ) from exc

    def _claim_latest(self):
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT command_id, canonical_path, operation, original_bytes,
                           post_fingerprint
                    FROM undo_entries
                    WHERE status = 'active'
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None
                changed = connection.execute(
                    """
                    UPDATE undo_entries SET status = 'undoing'
                    WHERE command_id = ? AND status = 'active'
                    """,
                    (row[0],),
                ).rowcount
                if changed != 1:
                    raise UndoJournalError("Undo journal entry를 사용할 수 없습니다.")
                return row
        except UndoJournalError:
            raise
        except sqlite3.Error as exc:
            raise UndoJournalError("Undo journal을 읽을 수 없습니다.") from exc

    def _validated_current(self, raw_path, operation, original, expected):
        if (
            not isinstance(raw_path, str)
            or operation not in {"create", "overwrite"}
            or not isinstance(expected, str)
            or len(expected) != 64
            or (operation == "create" and original is not None)
            or (operation == "overwrite" and not isinstance(original, bytes))
        ):
            raise _UndoBoundaryError
        path = Path(raw_path)
        if not path.is_absolute() or not any(
            path.is_relative_to(root) for root in self._roots
        ):
            raise _UndoBoundaryError
        try:
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise _UndoBoundaryError
            if path.resolve(strict=True) != path:
                raise _UndoBoundaryError
            current, opened_metadata = _read_regular_file(path)
        except (OSError, RuntimeError) as exc:
            raise _UndoBoundaryError from exc
        if (
            metadata.st_dev != opened_metadata.st_dev
            or metadata.st_ino != opened_metadata.st_ino
        ):
            raise _UndoConflict
        if _fingerprint(current) != expected:
            raise _UndoConflict
        return path, current, opened_metadata

    @staticmethod
    def _atomic_restore(
        path: Path, original: bytes, expected_current: os.stat_result
    ) -> None:
        descriptor = -1
        temporary_path = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".voice-action-undo-", dir=path.parent
            )
            os.fchmod(descriptor, stat.S_IMODE(expected_current.st_mode))
            view = memoryview(original)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("temporary restore write failed")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if not _same_file(path, expected_current):
                raise _UndoConflict
            os.replace(temporary_path, path)
            temporary_path = None
            _fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def _set_status(self, command_id: str, status: str, *, expected: str) -> None:
        try:
            with self._connect() as connection:
                changed = connection.execute(
                    """
                    UPDATE undo_entries SET status = ?
                    WHERE command_id = ? AND status = ?
                    """,
                    (status, command_id, expected),
                ).rowcount
                if changed != 1:
                    raise UndoJournalError("Undo journal 상태를 갱신할 수 없습니다.")
        except UndoJournalError:
            raise
        except sqlite3.Error as exc:
            raise UndoJournalError("Undo journal 상태를 갱신할 수 없습니다.") from exc

    def entry_count(self, *, status: str | None = None) -> int:
        """Return a count without exposing journal payloads."""
        query = "SELECT COUNT(*) FROM undo_entries"
        arguments = ()
        if status is not None:
            query += " WHERE status = ?"
            arguments = (status,)
        try:
            with self._connect() as connection:
                return int(connection.execute(query, arguments).fetchone()[0])
        except sqlite3.Error as exc:
            raise UndoJournalError("Undo journal 상태를 읽을 수 없습니다.") from exc
