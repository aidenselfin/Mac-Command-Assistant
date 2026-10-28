"""Read-only-minimal Playwright MCP client for navigation and page snapshots."""

import asyncio
from collections.abc import Iterable
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import anyio
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from voice_action.config import Config
from voice_action.mcp_client import (
    MCPConnectionError,
    MCPStartupError,
    MCPToolNotFoundError,
    MCPToolTimeoutError,
    ToolDefinition,
    ToolRejectedError,
    ToolResult,
    normalize_result,
)

PLAYWRIGHT_SERVER_ENTRYPOINT = (
    Path(__file__).resolve().parents[1] / "node_modules/@playwright/mcp/cli.js"
)
PLAYWRIGHT_SERVER_ARGS = (
    "--headless",
    "--isolated",
    "--image-responses",
    "omit",
    "--codegen",
    "none",
)
PLAYWRIGHT_TOOLS = frozenset({"browser_navigate", "browser_snapshot"})
EMPTY_SNAPSHOT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


class PlaywrightMCPClient:
    def __init__(
        self,
        config: Config,
        *,
        allowed_tools: Iterable[str] = PLAYWRIGHT_TOOLS,
    ):
        self.config = config
        self._allowlist = frozenset(allowed_tools)
        if not self._allowlist or not self._allowlist <= PLAYWRIGHT_TOOLS:
            raise ToolRejectedError("지원하지 않는 Playwright application allowlist입니다.")
        self.tools: list[ToolDefinition] = []
        self._session: ClientSession | None = None
        self._connecting = False
        self._failed = False
        self._call_lock = asyncio.Lock()

    async def _discover(self, session: ClientSession) -> list[ToolDefinition]:
        tools = {}
        cursor, seen = None, set()
        while True:
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                if tool.name not in self._allowlist:
                    continue
                if tool.name in tools:
                    raise MCPStartupError("Playwright MCP 도구 정의가 중복되었습니다.")
                schema = tool.inputSchema
                Draft202012Validator.check_schema(schema)
                if tool.name == "browser_snapshot":
                    try:
                        Draft202012Validator(schema).validate({})
                    except ValidationError as exc:
                        raise MCPStartupError(
                            "Playwright browser_snapshot이 빈 인자를 지원하지 않습니다."
                        ) from exc
                    schema = EMPTY_SNAPSHOT_SCHEMA
                tools[tool.name] = ToolDefinition(
                    tool.name,
                    tool.description or "",
                    deepcopy(schema),
                )
            cursor = page.nextCursor
            if not cursor:
                break
            if cursor in seen:
                raise MCPStartupError("Playwright MCP 도구 페이지가 반복됩니다.")
            seen.add(cursor)
        if tools.keys() != self._allowlist:
            raise MCPStartupError("필수 Playwright MCP capability가 없습니다.")
        return list(tools.values())

    async def list_tools(self) -> list[ToolDefinition]:
        if self._session is None or self._failed:
            raise MCPConnectionError("Playwright MCP가 연결되어 있지 않습니다.")
        return deepcopy(self.tools)

    @asynccontextmanager
    async def connect(self):
        if self._connecting:
            raise MCPConnectionError("Playwright MCP가 이미 연결 중이거나 연결되어 있습니다.")
        self._connecting = True
        ready = False
        try:
            if not await asyncio.to_thread(PLAYWRIGHT_SERVER_ENTRYPOINT.is_file):
                raise MCPStartupError(
                    "Playwright MCP가 설치되어 있지 않습니다. npm ci를 실행하세요."
                )
            params = StdioServerParameters(
                command="node",
                args=[str(PLAYWRIGHT_SERVER_ENTRYPOINT), *PLAYWRIGHT_SERVER_ARGS],
            )
            with anyio.CancelScope() as lifetime:
                async with stdio_client(params) as (read, write):
                    try:
                        async with ClientSession(
                            read,
                            write,
                            read_timeout_seconds=timedelta(
                                seconds=self.config.mcp_timeout_seconds
                            ),
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
                raise MCPStartupError("Playwright MCP 시작/초기화/도구 발견 실패") from exc
            raise
        finally:
            self._session = None
            self.tools = []
            self._connecting = False

    async def normalize_tool_arguments(self, name: str, arguments: dict) -> dict:
        tool = next((tool for tool in self.tools if tool.name == name), None)
        if name not in self._allowlist or tool is None:
            raise MCPToolNotFoundError("허용되지 않은 Playwright 도구입니다.")
        if self._session is None or self._failed:
            raise MCPConnectionError(
                "Playwright MCP 연결이 종료되었거나 사용할 수 없습니다."
            )
        if name == "browser_snapshot" and arguments:
            raise ToolRejectedError(
                "읽기 전용 web snapshot은 빈 object 인자만 허용합니다."
            )
        try:
            Draft202012Validator(tool.schema).validate(arguments)
        except ValidationError as exc:
            raise ToolRejectedError("잘못된 Playwright 도구 인자입니다.") from exc
        return deepcopy(arguments)

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        async with self._call_lock:
            normalized = await self.normalize_tool_arguments(name, arguments)
            try:
                async with asyncio.timeout(self.config.mcp_timeout_seconds):
                    result = await self._session.call_tool(name, normalized)
            except TimeoutError as exc:
                self._failed = True
                raise MCPToolTimeoutError(
                    "Playwright MCP 도구 실행 시간 초과; 자동 재실행하지 않습니다."
                ) from exc
            except asyncio.CancelledError:
                self._failed = True
                raise
            except McpError as exc:
                self._failed = True
                if exc.error.code == 408:
                    raise MCPToolTimeoutError(
                        "Playwright MCP 도구 실행 시간 초과; 자동 재실행하지 않습니다."
                    ) from exc
                raise MCPConnectionError("Playwright MCP protocol 요청 실패") from exc
            except Exception as exc:
                self._failed = True
                raise MCPConnectionError("Playwright MCP 도구 연결 또는 실행 실패") from exc
            return normalize_result(result, self.config.max_tool_result_chars)
