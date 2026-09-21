"""Fixed application risk policy and one-shot filesystem write approvals."""

import asyncio
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from voice_action.mcp_client import ToolRejectedError, ToolResult
from voice_action.undo import UndoJournal, UndoJournalError

READ_ONLY_TOOLS = frozenset(
    {
        "filesystem__list_directory",
        "filesystem__search_files",
        "filesystem__read_text_file",
        "web__browser_navigate",
        "web__browser_snapshot",
        "macos__get_active_app",
    }
)
WRITE_TOOLS = frozenset({"filesystem__write_file"})
PREVIEW_LIMIT = 200


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


def _digest(tool_name: str, canonical_arguments: str) -> str:
    payload = f"{tool_name}\n{canonical_arguments}".encode()
    return hashlib.sha256(payload).hexdigest()


class RiskGuard:
    """Permit fixed read tools and bind each write to one in-memory approval."""

    def __init__(
        self, gateway: GuardedGateway, undo_journal: UndoJournal | None = None
    ):
        self._gateway = gateway
        self._undo_journal = undo_journal
        self._pending: dict[str, str] = {}

    async def execute_read_only(self, name: str, arguments: dict) -> ToolResult:
        risk = classify_tool(name)
        if risk is not ToolRisk.READ_ONLY:
            if risk is ToolRisk.WRITE:
                raise RiskGuardError("파일 쓰기는 호출별 승인이 필요합니다.")
            raise ForbiddenToolError(f"금지된 도구입니다: {name}")
        return await self._gateway.call_tool(name, arguments)

    async def prepare_write(self, name: str, arguments: dict) -> PreparedWrite:
        risk = classify_tool(name)
        if risk is ToolRisk.FORBIDDEN:
            raise ForbiddenToolError(f"금지된 도구입니다: {name}")
        if risk is not ToolRisk.WRITE:
            raise RiskGuardError("쓰기 승인은 write 도구에만 사용할 수 있습니다.")
        normalized = await self._gateway.normalize_tool_arguments(name, arguments)
        return self._new_prepared(name, normalized)

    def _new_prepared(self, name: str, normalized: dict) -> PreparedWrite:
        path = normalized.get("path")
        content = normalized.get("content")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise RiskGuardError("정규화된 write 경로가 절대경로가 아닙니다.")
        if not isinstance(content, str):
            raise RiskGuardError("write_file content는 문자열이어야 합니다.")
        target = Path(path)
        if target.exists():
            if not target.is_file():
                raise RiskGuardError("쓰기 대상은 regular file이어야 합니다.")
            operation = "overwrite"
        else:
            if not target.parent.is_dir():
                raise RiskGuardError("쓰기 대상의 부모 디렉터리가 존재하지 않습니다.")
            operation = "create"
        canonical = _canonical_json(normalized)
        digest = _digest(name, canonical)
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

        arguments = json.loads(prepared.canonical_arguments)
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
        canonical = _canonical_json(normalized)
        return PreparedWrite(
            name,
            path,
            operation,
            len(content),
            content[:PREVIEW_LIMIT],
            len(content) > PREVIEW_LIMIT,
            canonical,
            _digest(name, canonical),
            "",
        )
