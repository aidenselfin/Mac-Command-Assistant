"""Session-scoped Filesystem MCP transport and application execution boundary."""

import asyncio
import json
from collections.abc import Iterable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import anyio
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from voice_action.config import Config

SERVER_ENTRYPOINT = (
    Path(__file__).resolve().parents[1]
    / "node_modules/@modelcontextprotocol/server-filesystem/dist/index.js"
)
SUPPORTED_TOOLS = frozenset(
    {"list_directory", "search_files", "read_text_file", "write_file"}
)
DEFAULT_ALLOWLIST = frozenset({"list_directory"})
TRUNCATION_MARKER = "\n[TRUNCATED]"


class MCPError(RuntimeError):
    pass


class ToolRejectedError(MCPError):
    pass


class MCPStartupError(MCPError):
    pass


class MCPConnectionError(MCPError):
    pass


class MCPToolNotFoundError(ToolRejectedError):
    pass


class MCPToolTimeoutError(MCPError):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    schema: dict


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    text: str
    structured: dict | None = None
    error: str | None = None
    truncated: bool = False


def _bounded_join(parts: Iterable[str], limit: int) -> tuple[str, bool]:
    """Avoid building an unbounded joined copy of tool content."""
    chunks = []
    remaining = limit
    for part in parts:
        chunks.append(part[:remaining])
        if len(part) > remaining:
            return "".join(chunks), True
        remaining -= len(part)
    return "".join(chunks), False


def normalize_result(result, limit: int) -> ToolResult:
    def text_parts():
        for index, item in enumerate(result.content):
            if index:
                yield "\n"
            yield item.text if item.type == "text" else f"[UNSUPPORTED:{item.type}]"

    text, truncated = _bounded_join(text_parts(), limit)
    structured = result.structuredContent
    if structured is not None:
        encoded, oversized = _bounded_join(
            json.JSONEncoder(ensure_ascii=False).iterencode(structured), limit - len(text)
        )
        if not text:
            text = encoded
        if oversized:
            structured = None
            truncated = True
    if truncated:
        text += TRUNCATION_MARKER
    ok = not result.isError
    return ToolResult(
        ok,
        text,
        structured,
        error=None if ok else (text or "MCP 도구가 오류를 반환했습니다."),
        truncated=truncated,
    )


class MCPClient:
    def __init__(self, config: Config, *, allowed_tools: Iterable[str] = DEFAULT_ALLOWLIST):
        self.config = config
        self._allowlist = frozenset(allowed_tools)
        if not self._allowlist or not self._allowlist <= SUPPORTED_TOOLS:
            raise ToolRejectedError("지원하지 않는 application allowlist입니다.")
        self.tools: list[ToolDefinition] = []
        self._session: ClientSession | None = None
        self._connecting = False
        self._failed = False
        self._roots: tuple[Path, ...] = ()
        self._call_lock = asyncio.Lock()

    def _resolve_roots(self) -> tuple[Path, ...]:
        roots = tuple(p.expanduser().resolve(strict=True) for p in self.config.allowed_directories)
        if not roots or any(not root.is_dir() for root in roots):
            raise MCPStartupError("유효한 allowed directory가 필요합니다.")
        return roots

    async def _discover(self, session: ClientSession) -> list[ToolDefinition]:
        tools = {}
        cursor, seen = None, set()
        while True:
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                if tool.name not in self._allowlist:
                    continue
                if tool.name in tools:
                    raise MCPStartupError("MCP 도구 정의가 중복되었습니다.")
                schema = tool.inputSchema
                Draft202012Validator.check_schema(schema)
                if (
                    schema.get("type") != "object"
                    or schema.get("properties", {}).get("path", {}).get("type") != "string"
                    or "path" not in schema.get("required", [])
                ):
                    raise MCPStartupError("지원하지 않는 filesystem 도구 schema입니다.")
                tools[tool.name] = ToolDefinition(
                    tool.name, tool.description or "", deepcopy(schema)
                )
            cursor = page.nextCursor
            if not cursor:
                break
            if cursor in seen:
                raise MCPStartupError("MCP 도구 페이지가 반복됩니다.")
            seen.add(cursor)
        if tools.keys() != self._allowlist:
            raise MCPStartupError("허용 목록의 필수 MCP capability가 없습니다.")
        return list(tools.values())

    async def list_tools(self) -> list[ToolDefinition]:
        """Return only the allowlisted startup snapshot; no per-command discovery."""
        if self._session is None or self._failed:
            raise MCPConnectionError("MCP가 연결되어 있지 않습니다.")
        return deepcopy(self.tools)

    @asynccontextmanager
    async def connect(self):
        if self._connecting:
            raise MCPConnectionError("MCP가 이미 연결 중이거나 연결되어 있습니다.")
        self._connecting = True
        ready = False
        try:
            self._roots = await asyncio.to_thread(self._resolve_roots)
            if not await asyncio.to_thread(SERVER_ENTRYPOINT.is_file):
                raise MCPStartupError(
                    "Filesystem MCP가 설치되어 있지 않습니다. npm ci를 실행하세요."
                )
            params = StdioServerParameters(
                command="node",
                args=[str(SERVER_ENTRYPOINT), *map(str, self._roots)],
            )
            # SDK context managers must be entered/exited by the same task. Shield
            # teardown from enclosing AnyIO cancellation, never command execution.
            with anyio.CancelScope() as lifetime:
                async with stdio_client(params) as (read, write):
                    try:
                        async with ClientSession(
                            read,
                            write,
                            read_timeout_seconds=timedelta(seconds=self.config.mcp_timeout_seconds),
                        ) as session:
                            try:
                                async with asyncio.timeout(self.config.mcp_timeout_seconds):
                                    await session.initialize()
                                    self.tools = await self._discover(session)
                                self._session, self._failed, ready = session, False, True
                                yield self
                            finally:
                                lifetime.shield = True
                    finally:
                        lifetime.shield = True
        except MCPStartupError:
            raise
        except Exception as exc:
            if not ready:
                raise MCPStartupError("MCP 시작/초기화/도구 발견 실패") from exc
            raise
        finally:
            self._session = None
            self.tools = []
            self._roots = ()
            self._connecting = False

    def _validate_path(self, raw_path: str, *, creating: bool) -> Path:
        if not isinstance(raw_path, str) or not raw_path or not Path(raw_path).is_absolute():
            raise ToolRejectedError("도구 경로는 절대경로여야 합니다.")
        try:
            path = Path(raw_path).resolve(strict=not creating)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolRejectedError("도구 경로를 해석할 수 없습니다.") from exc
        if not any(path.is_relative_to(root) for root in self._roots):
            raise ToolRejectedError("허용 경로 밖 접근입니다.")
        return path

    def _normalize_arguments(self, name: str, arguments: dict) -> dict:
        tool = next((tool for tool in self.tools if tool.name == name), None)
        if name not in self._allowlist or tool is None:
            raise MCPToolNotFoundError("허용되지 않은 도구입니다.")
        if self._session is None or self._failed:
            raise MCPConnectionError("MCP 연결이 종료되었거나 사용할 수 없습니다.")
        if name == "read_text_file" and isinstance(arguments, dict):
            arguments = {
                key: value for key, value in arguments.items()
                if key not in {"head", "tail"} or value is not None
            }
        try:
            Draft202012Validator(tool.schema).validate(arguments)
        except ValidationError as exc:
            raise ToolRejectedError("잘못된 도구 인자입니다.") from exc

        creating = name == "write_file"
        path = self._validate_path(arguments.get("path"), creating=creating)
        if creating:
            content = arguments.get("content")
            if not isinstance(content, str):
                raise ToolRejectedError("write_file content는 문자열이어야 합니다.")
            if path.exists():
                if not path.is_file():
                    raise ToolRejectedError("쓰기 대상은 regular file이어야 합니다.")
            else:
                try:
                    parent = path.parent.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise ToolRejectedError(
                        "쓰기 대상의 부모 디렉터리가 존재하지 않습니다."
                    ) from exc
                if not parent.is_dir() or not any(
                    parent.is_relative_to(root) for root in self._roots
                ):
                    raise ToolRejectedError("쓰기 대상의 부모가 허용 디렉터리가 아닙니다.")
        return {**deepcopy(arguments), "path": str(path)}

    async def normalize_tool_arguments(self, name: str, arguments: dict) -> dict:
        """Validate and canonicalize arguments without executing the MCP tool."""
        return await asyncio.to_thread(self._normalize_arguments, name, arguments)

    async def validate_result_path(self, raw_path: str) -> Path:
        """Revalidate a server-returned candidate as a file inside an allowed root."""
        if self._session is None or self._failed:
            raise MCPConnectionError("MCP가 연결되어 있지 않습니다.")
        path = await asyncio.to_thread(self._validate_path, raw_path, creating=False)
        if not await asyncio.to_thread(path.is_file):
            raise ToolRejectedError("검색 후보가 읽을 수 있는 파일이 아닙니다.")
        return path

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        async with self._call_lock:
            normalized = await self.normalize_tool_arguments(name, arguments)
            try:
                async with asyncio.timeout(self.config.mcp_timeout_seconds):
                    result = await self._session.call_tool(name, normalized)
            except TimeoutError as exc:
                self._failed = True
                raise MCPToolTimeoutError(
                    "MCP 도구 실행 시간 초과; 자동 재실행하지 않습니다."
                ) from exc
            except asyncio.CancelledError:
                self._failed = True
                raise
            except McpError as exc:
                self._failed = True
                if exc.error.code == 408:
                    raise MCPToolTimeoutError(
                        "MCP 도구 실행 시간 초과; 자동 재실행하지 않습니다."
                    ) from exc
                raise MCPConnectionError("MCP protocol 요청 실패") from exc
            except Exception as exc:
                self._failed = True
                raise MCPConnectionError("MCP 도구 연결 또는 실행 실패") from exc
            return normalize_result(result, self.config.max_tool_result_chars)
