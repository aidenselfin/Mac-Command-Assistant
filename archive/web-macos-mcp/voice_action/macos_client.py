"""Minimal in-process macOS client for the frontmost application name."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Protocol

from voice_action.mcp_client import (
    MCPConnectionError,
    MCPToolNotFoundError,
    ToolDefinition,
    ToolRejectedError,
    ToolResult,
)

GET_ACTIVE_APP_TOOL = "get_active_app"
MAX_APP_NAME_CHARS = 200
EMPTY_OBJECT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


class MacOSAXAdapter(Protocol):
    def is_trusted(self) -> bool: ...

    def get_active_app_name(self) -> str: ...


class PyObjCMacOSAXAdapter:
    """Load PyObjC only when a macOS AX tool call actually runs."""

    def is_trusted(self) -> bool:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())

    def get_active_app_name(self) -> str:
        from AppKit import NSWorkspace

        application = NSWorkspace.sharedWorkspace().frontmostApplication()
        if application is None:
            return ""
        return application.localizedName() or ""


def _tool_error(message: str) -> ToolResult:
    return ToolResult(False, message, error=message)


class MacOSAXClient:
    """Expose only a bounded, read-only active-application name lookup."""

    def __init__(self, adapter: MacOSAXAdapter | None = None):
        self._adapter = adapter or PyObjCMacOSAXAdapter()
        self._connected = False
        self._call_lock = asyncio.Lock()
        self.tools: list[ToolDefinition] = []

    @asynccontextmanager
    async def connect(self):
        if self._connected:
            raise MCPConnectionError("macOS AX client가 이미 연결되어 있습니다.")
        self._connected = True
        self.tools = [
            ToolDefinition(
                GET_ACTIVE_APP_TOOL,
                "현재 활성 앱의 이름을 조회합니다.",
                deepcopy(EMPTY_OBJECT_SCHEMA),
            )
        ]
        try:
            yield self
        finally:
            self.tools = []
            self._connected = False

    async def list_tools(self) -> list[ToolDefinition]:
        if not self._connected:
            raise MCPConnectionError("macOS AX client가 연결되어 있지 않습니다.")
        return deepcopy(self.tools)

    async def normalize_tool_arguments(self, name: str, arguments: dict) -> dict:
        if not self._connected:
            raise MCPConnectionError("macOS AX client가 연결되어 있지 않습니다.")
        if name != GET_ACTIVE_APP_TOOL:
            raise MCPToolNotFoundError("허용되지 않은 macOS AX 도구입니다.")
        if not isinstance(arguments, dict) or arguments:
            raise ToolRejectedError("get_active_app은 빈 object 인자만 허용합니다.")
        return {}

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        async with self._call_lock:
            await self.normalize_tool_arguments(name, arguments)
            try:
                trusted = await asyncio.to_thread(self._adapter.is_trusted)
            except Exception:
                return _tool_error("macOS 접근성 권한 상태를 확인하지 못했습니다.")
            if not trusted:
                return _tool_error(
                    "시스템 설정의 개인정보 보호 및 보안 > 손쉬운 사용에서 "
                    "접근성 권한을 허용해 주세요."
                )
            try:
                raw_name = await asyncio.to_thread(self._adapter.get_active_app_name)
            except Exception:
                return _tool_error("현재 활성 앱 이름을 조회하지 못했습니다.")
            if not isinstance(raw_name, str) or not raw_name.strip():
                return _tool_error("현재 활성 앱 이름을 확인할 수 없습니다.")
            name = raw_name.strip()
            truncated = len(name) > MAX_APP_NAME_CHARS
            return ToolResult(
                True,
                name[:MAX_APP_NAME_CHARS],
                truncated=truncated,
            )
