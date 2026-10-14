"""Fixed application risk policy and one-shot filesystem write approvals."""

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from voice_action import conversion, file_search
from voice_action.conversion import CONVERT_TOOL, READ_DOCUMENT_TOOL
from voice_action.deletion import DELETE_TOOL
from voice_action.file_search import FIND_FILES_TOOL
from voice_action.mcp_client import TRUNCATION_MARKER, ToolRejectedError, ToolResult
from voice_action.undo import (
    UndoJournal,
    UndoJournalError,
    _fingerprint,
    _read_regular_file,
    _UndoBoundaryError,
)

READ_ONLY_TOOLS = frozenset(
    {
        "filesystem__list_directory",
        "filesystem__search_files",
        "filesystem__read_text_file",
        "web__browser_navigate",
        "web__browser_snapshot",
        "macos__get_active_app",
        READ_DOCUMENT_TOOL,
        FIND_FILES_TOOL,
    }
)
WRITE_TOOLS = frozenset({"filesystem__write_file", DELETE_TOOL, CONVERT_TOOL})
PREVIEW_LIMIT = 200
# A character is up to a few UTF-8 bytes; files past this many bytes per allowed character are
# read from their start only.
READ_BYTES_FACTOR = 4


class ToolRisk(Enum):
    READ_ONLY = "read-only"
    WRITE = "write"
    FORBIDDEN = "forbidden"


class RiskGuardError(ToolRejectedError):
    pass


class ForbiddenToolError(RiskGuardError):
    pass


class ApprovalInvalidError(RiskGuardError):
    pass


class ApprovalChangedError(RiskGuardError):
    def __init__(self, prepared: "PreparedWrite"):
        super().__init__("쓰기 대상 또는 상태가 변경되어 재승인이 필요합니다.")
        self.prepared = prepared


class GuardedGateway(Protocol):
    async def call_tool(self, name: str, arguments: dict) -> ToolResult: ...

    async def normalize_tool_arguments(self, name: str, arguments: dict) -> dict: ...


@dataclass(frozen=True)
class PreparedWrite:
    tool_name: str
    target_path: str
    operation: str
    content_chars: int
    preview: str
    preview_truncated: bool
    canonical_arguments: str = field(repr=False)
    digest: str = field(repr=False)
    nonce: str = field(repr=False)
    targets: tuple[str, ...] = ()
    snapshots: tuple[tuple, ...] = field(default=(), repr=False)
    file_state: str = field(default="", repr=False)
    payload: bytes = field(default=b"", repr=False)  # a conversion's output, made before approval


def classify_tool(name: str) -> ToolRisk:
    if name in READ_ONLY_TOOLS:
        return ToolRisk.READ_ONLY
    if name in WRITE_TOOLS:
        return ToolRisk.WRITE
    return ToolRisk.FORBIDDEN


def _canonical_json(arguments: dict) -> str:
    return json.dumps(
        arguments,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _digest(tool_name: str, canonical_arguments: str, file_state: str = "") -> str:
    payload = f"{tool_name}\n{canonical_arguments}\n{file_state}".encode()
    return hashlib.sha256(payload).hexdigest()


def file_state(path: str) -> str:
    """Identity and content hash of the file an overwrite approval was shown for.

    Bound into the approval digest, so an edit, a same-path replacement or a symlink swap
    while the approval is pending forces a new approval. Never the bytes themselves.
    """
    try:
        data, metadata = _read_regular_file(Path(path))
    except (OSError, _UndoBoundaryError):
        return "unreadable"
    return f"{metadata.st_dev}:{metadata.st_ino}:{_fingerprint(data)}"


class RiskGuard:
    """Permit fixed read tools and bind each write to one in-memory approval."""

    def __init__(
        self,
        gateway: GuardedGateway,
        undo_journal: UndoJournal | None = None,
        roots: tuple[Path, ...] = (),
        read_limit: int = 50000,
        semantic=None,
    ):
        self._semantic = semantic
        self._gateway = gateway
        self._undo_journal = undo_journal
        self._roots = tuple(root.resolve() for root in roots)
        self._read_limit = read_limit
        self._pending: dict[str, str] = {}

    async def execute_read_only(self, name: str, arguments: dict) -> ToolResult:
        risk = classify_tool(name)
        if risk is not ToolRisk.READ_ONLY:
            if risk is ToolRisk.WRITE:
                raise RiskGuardError("파일 쓰기는 호출별 승인이 필요합니다.")
            raise ForbiddenToolError(f"금지된 도구입니다: {name}")
        if name == READ_DOCUMENT_TOOL:
            return await asyncio.to_thread(
                conversion.read_document, arguments, self._roots, self._read_limit
            )
        if name == FIND_FILES_TOOL:
            return await asyncio.to_thread(
                file_search.find_files, arguments, self._roots, self._semantic
            )
        if name == "filesystem__read_text_file" and not (
            arguments.get("head") or arguments.get("tail")
        ):
            # The filesystem server reads a whole file before the result is cut, so a huge
            # file is read here, up to a byte bound, instead of being sent through it.
            bounded = await asyncio.to_thread(self._read_bounded, arguments)
            if bounded is not None:
                return bounded
        return await self._gateway.call_tool(name, arguments)

    def _read_bounded(self, arguments: dict) -> ToolResult | None:
        """The start of a file much bigger than the result limit; None for any other file."""
        try:
            path = conversion._inside(arguments.get("path"), self._roots, exists=True)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except (ToolRejectedError, OSError):
            return None  # the filesystem server reports the problem in its own words
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= READ_BYTES_FACTOR * (
                self._read_limit
            ):
                return None
            data = os.read(descriptor, READ_BYTES_FACTOR * self._read_limit)
        finally:
            os.close(descriptor)
        text = data.decode("utf-8", errors="replace")[: self._read_limit]
        return ToolResult(True, text + TRUNCATION_MARKER, truncated=True)

    async def prepare_write(self, name: str, arguments: dict) -> PreparedWrite:
        risk = classify_tool(name)
        if risk is ToolRisk.FORBIDDEN:
            raise ForbiddenToolError(f"금지된 도구입니다: {name}")
        if risk is not ToolRisk.WRITE:
            raise RiskGuardError("쓰기 승인은 write 도구에만 사용할 수 있습니다.")
        if name == DELETE_TOOL:
            return await asyncio.to_thread(self._prepare_delete, arguments)
        if name == CONVERT_TOOL:
            return await asyncio.to_thread(self._prepare_convert, arguments)
        normalized = await self._gateway.normalize_tool_arguments(name, arguments)
        return self._new_prepared(name, normalized)

    def _prepare_delete(self, arguments: dict) -> PreparedWrite:
        if self._undo_journal is None:
            raise RiskGuardError("Undo journal이 없어 파일을 삭제할 수 없습니다.")
        try:
            normalized, snapshots = self._undo_journal.deletions.prepare(arguments)
        except (OSError, sqlite3.Error, UndoJournalError) as exc:
            raise RiskGuardError("삭제 대상/백업을 준비할 수 없습니다.") from exc
        canonical = _canonical_json(normalized)
        digest = _digest(DELETE_TOOL, canonical + _canonical_json({"files": snapshots}))
        nonce = secrets.token_urlsafe(32)
        self._pending[nonce] = digest
        targets = tuple(normalized["paths"])
        return PreparedWrite(
            DELETE_TOOL, targets[0] if len(targets) == 1 else f"{len(targets)}개 파일",
            "delete", 0, "", False, canonical, digest, nonce, targets, snapshots,
        )

    def _prepare_convert(self, arguments: dict) -> PreparedWrite:
        """Convert into memory now, so the approval shows the real result."""
        if not self._roots:
            raise RiskGuardError("허용 폴더가 없어 파일을 변환할 수 없습니다.")
        result = conversion.prepare_conversion(arguments, self._roots)
        canonical = _canonical_json(arguments)
        digest = _digest(
            CONVERT_TOOL,
            canonical + result.output,
            result.source_state + hashlib.sha256(result.data).hexdigest(),
        )
        nonce = secrets.token_urlsafe(32)
        self._pending[nonce] = digest
        return PreparedWrite(
            CONVERT_TOOL,
            result.output,
            "convert",
            len(result.data),
            result.preview,
            result.preview_truncated,
            canonical,
            digest,
            nonce,
            (result.source,),
            file_state=result.source_state,
            payload=result.data,
        )

    async def _execute_convert(
        self,
        prepared: PreparedWrite,
        expected: str,
        on_started: Callable[[], None] | None,
        on_finished: Callable[[], None] | None,
    ) -> ToolResult:
        changed = await asyncio.to_thread(
            conversion.changed_since,
            prepared.targets[0],
            prepared.file_state,
            prepared.target_path,
            self._roots,
        )
        if expected != prepared.digest or changed:
            # The source changed or the output name got taken: convert again, ask again.
            refreshed = await asyncio.to_thread(
                self._prepare_convert, json.loads(prepared.canonical_arguments)
            )
            raise ApprovalChangedError(refreshed)
        if self._undo_journal is None:
            raise RiskGuardError("Undo journal이 없어 변환 파일을 만들지 않았습니다.")
        # A cancelled to_thread keeps running, so each step is shielded and awaited to its end
        # before the journal is settled, as the delete path does.
        journal_task = asyncio.create_task(asyncio.to_thread(
            self._undo_journal.prepare_write, prepared.target_path, "create", prepared.payload
        ))
        try:
            entry = await asyncio.shield(journal_task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.to_thread(self._undo_journal.mark_write_failed, await journal_task)
            raise
        except UndoJournalError as exc:
            raise RiskGuardError(
                "Undo journal을 준비하지 못해 변환 파일을 만들지 않았습니다."
            ) from exc
        if on_started is not None:
            on_started()
        write_task = asyncio.create_task(asyncio.to_thread(
            conversion.write_new_file, prepared.target_path, prepared.payload, self._roots
        ))
        try:
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await write_task  # the file is now either complete or removed
                raise
        except BaseException:
            with suppress(UndoJournalError):
                await asyncio.to_thread(self._undo_journal.reconcile_uncertain_write, entry)
            raise
        finally:
            if on_finished is not None:
                on_finished()
        return ToolResult(
            True, f"변환 완료: {prepared.target_path} ({len(prepared.payload)} bytes)"
        )

    def _new_prepared(self, name: str, normalized: dict) -> PreparedWrite:
        path = normalized.get("path")
        content = normalized.get("content")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise RiskGuardError("정규화된 write 경로가 절대경로가 아닙니다.")
        if not isinstance(content, str):
            raise RiskGuardError("write_file content는 문자열이어야 합니다.")
        target = Path(path)
        state = ""
        if target.exists():
            state = file_state(path)
            if not target.is_file() or state == "unreadable":
                raise RiskGuardError("쓰기 대상은 regular file이어야 합니다.")
            operation = "overwrite"
        else:
            if not target.parent.is_dir():
                raise RiskGuardError("쓰기 대상의 부모 디렉터리가 존재하지 않습니다.")
            operation = "create"
        canonical = _canonical_json(normalized)
        digest = _digest(name, canonical, state)
        nonce = secrets.token_urlsafe(32)
        self._pending[nonce] = digest
        return PreparedWrite(
            name,
            path,
            operation,
            len(content),
            content[:PREVIEW_LIMIT],
            len(content) > PREVIEW_LIMIT,
            canonical,
            digest,
            nonce,
            file_state=state,
        )

    async def execute_approved(
        self,
        prepared: PreparedWrite,
        *,
        on_started: Callable[[], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> ToolResult:
        expected = self._pending.pop(prepared.nonce, None)
        if expected is None:
            raise ApprovalInvalidError("승인이 없거나 이미 사용되었습니다.")

        if prepared.tool_name == CONVERT_TOOL:
            return await self._execute_convert(prepared, expected, on_started, on_finished)
        arguments = json.loads(prepared.canonical_arguments)
        if prepared.tool_name == DELETE_TOOL:
            refreshed = await asyncio.to_thread(self._prepare_delete, arguments)
            self._pending.pop(refreshed.nonce, None)
            if expected != prepared.digest or refreshed.digest != prepared.digest:
                self._pending[refreshed.nonce] = refreshed.digest
                raise ApprovalChangedError(refreshed)
            if on_started is not None:
                on_started()
            task = asyncio.create_task(asyncio.to_thread(
                self._undo_journal.deletions.execute, refreshed.snapshots
            ))
            try:
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task  # 백업/삭제 작업이 끝날 때까지 journal을 닫지 않는다.
                    raise
            except (OSError, sqlite3.Error, UndoJournalError) as exc:
                raise RiskGuardError(
                    "삭제 journal 오류입니다. 삭제 결과를 확인하고 Undo하세요."
                ) from exc
            finally:
                if on_finished is not None:
                    on_finished()
        normalized = await self._gateway.normalize_tool_arguments(
            prepared.tool_name, arguments
        )
        if expected != prepared.digest:
            raise ApprovalChangedError(self._new_prepared(prepared.tool_name, normalized))
        refreshed = self._snapshot(prepared.tool_name, normalized)
        if (
            refreshed.digest != prepared.digest
            or refreshed.target_path != prepared.target_path
            or refreshed.operation != prepared.operation
        ):
            raise ApprovalChangedError(self._new_prepared(prepared.tool_name, normalized))
        if self._undo_journal is None:
            raise RiskGuardError(
                "Undo journal이 없어 승인된 파일 write를 실행하지 않았습니다."
            )
        try:
            journal_entry = await asyncio.to_thread(
                self._undo_journal.prepare_write,
                refreshed.target_path,
                refreshed.operation,
                normalized["content"],
                refreshed.file_state or None,
            )
        except UndoJournalError as exc:
            raise RiskGuardError(
                "Undo journal을 준비하지 못해 파일 write를 실행하지 않았습니다."
            ) from exc
        if on_started is not None:
            on_started()
        try:
            result = await self._gateway.call_tool(prepared.tool_name, normalized)
        except BaseException:
            try:
                await asyncio.to_thread(
                    self._undo_journal.reconcile_uncertain_write, journal_entry
                )
            except UndoJournalError:
                pass
            raise
        else:
            if not result.ok:
                try:
                    await asyncio.to_thread(
                        self._undo_journal.mark_write_failed, journal_entry
                    )
                except UndoJournalError as exc:
                    raise RiskGuardError(
                        "실패한 write의 Undo journal 상태를 갱신하지 못했습니다."
                    ) from exc
            return result
        finally:
            if on_finished is not None:
                on_finished()

    def reject(self, prepared: PreparedWrite) -> None:
        self._pending.pop(prepared.nonce, None)

    @staticmethod
    def _snapshot(name: str, normalized: dict) -> PreparedWrite:
        path = normalized["path"]
        content = normalized["content"]
        operation = "overwrite" if Path(path).is_file() else "create"
        state = file_state(path) if operation == "overwrite" else ""
        canonical = _canonical_json(normalized)
        return PreparedWrite(
            name,
            path,
            operation,
            len(content),
            content[:PREVIEW_LIMIT],
            len(content) > PREVIEW_LIMIT,
            canonical,
            _digest(name, canonical, state),
            "",
            file_state=state,
        )
