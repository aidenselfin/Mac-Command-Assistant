"""Minimal lifecycle and namespaced dispatch for multiple MCP clients."""

import asyncio
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Protocol

from voice_action.mcp_client import (
    MCPConnectionError,
    MCPStartupError,
    MCPToolNotFoundError,
    ToolDefinition,
    ToolResult,
)


class _ManagedMCPClient(Protocol):
    def connect(self) -> AbstractAsyncContextManager["_ManagedMCPClient"]: ...

    async def list_tools(self) -> list[ToolDefinition]: ...

    async def call_tool(self, name: str, arguments: dict) -> ToolResult: ...

    async def normalize_tool_arguments(self, name: str, arguments: dict) -> dict: ...


class MCPManager:
    def __init__(self, clients: Mapping[str, _ManagedMCPClient] | None = None):
        self._clients: dict[str, _ManagedMCPClient] = {}
        self._connected_clients: dict[str, _ManagedMCPClient] = {}
        self._routes: dict[str, tuple[_ManagedMCPClient, str]] = {}
        self._connecting = False
        self._call_lock = asyncio.Lock()
        self.tools: list[ToolDefinition] = []
        for name, client in (clients or {}).items():
            self.register(name, client)

    def register(self, name: str, client: _ManagedMCPClient) -> None:
        if self._connecting:
            raise MCPConnectionError("연결 중에는 MCP를 등록할 수 없습니다.")
        if not isinstance(name, str) or not name or "__" in name:
            raise ValueError("MCP 이름은 비어 있지 않고 '__'를 포함하지 않아야 합니다.")
        if name in self._clients:
            raise ValueError(f"MCP 이름이 중복되었습니다: {name}")
        self._clients[name] = client

    @asynccontextmanager
    async def connect(self):
        if self._connecting:
            raise MCPConnectionError("MCP manager가 이미 연결 중이거나 연결되어 있습니다.")
        self._connecting = True
        try:
            async with AsyncExitStack() as stack:
                connected = {}
                for name, client in self._clients.items():
                    connected[name] = await stack.enter_async_context(client.connect())

                tools = []
                routes = {}
                for namespace, client in connected.items():
                    for tool in await client.list_tools():
                        namespaced_name = f"{namespace}__{tool.name}"
                        if namespaced_name in routes:
                            raise MCPStartupError(
                                f"namespaced MCP 도구 이름이 충돌했습니다: {namespaced_name}"
                            )
                        routes[namespaced_name] = (client, tool.name)
                        tools.append(
                            ToolDefinition(
                                namespaced_name,
                                tool.description,
                                deepcopy(tool.schema),
                            )
                        )

                self._connected_clients = connected
                self._routes = routes
                self.tools = tools
                yield self
        finally:
            self._connected_clients = {}
            self._routes = {}
            self.tools = []
            self._connecting = False

    async def list_tools(self) -> list[ToolDefinition]:
        if not self._connected_clients:
            raise MCPConnectionError("MCP manager가 연결되어 있지 않습니다.")
        return deepcopy(self.tools)

    async def validate_result_path(self, raw_path: str) -> Path:
        """Delegate search-candidate validation to the Filesystem MCP boundary."""
        if not self._connected_clients:
            raise MCPConnectionError("MCP manager가 연결되어 있지 않습니다.")
        filesystem = self._connected_clients.get("filesystem")
        validator = getattr(filesystem, "validate_result_path", None)
        if validator is None:
            raise MCPConnectionError("filesystem MCP가 경로 재검증을 지원하지 않습니다.")
        return await validator(raw_path)

    async def normalize_tool_arguments(
        self, namespaced_name: str, arguments: dict
    ) -> dict:
        """Delegate non-executing argument validation to the routed MCP client."""
        if not self._connected_clients:
            raise MCPConnectionError("MCP manager가 연결되어 있지 않습니다.")
        route = self._routes.get(namespaced_name)
        if route is None:
            raise MCPToolNotFoundError(
                f"MCP namespace에 존재하지 않는 도구입니다: {namespaced_name}"
            )
        client, original_name = route
        normalizer = getattr(client, "normalize_tool_arguments", None)
        if normalizer is None:
            raise MCPConnectionError("MCP client가 인자 정규화를 지원하지 않습니다.")
        return await normalizer(original_name, arguments)

    async def call_tool(self, namespaced_name: str, arguments: dict) -> ToolResult:
        async with self._call_lock:
            if not self._connected_clients:
                raise MCPConnectionError("MCP manager가 연결되어 있지 않습니다.")
            namespace, separator, tool_name = namespaced_name.partition("__")
            if not separator or not namespace or not tool_name:
                raise MCPToolNotFoundError("MCP namespace가 없는 도구 이름입니다.")
            if namespace not in self._connected_clients:
                raise MCPToolNotFoundError(
                    f"등록되지 않은 MCP namespace입니다: {namespace}"
                )
            route = self._routes.get(namespaced_name)
            if route is None:
                raise MCPToolNotFoundError(
                    f"MCP namespace에 존재하지 않는 도구입니다: {namespaced_name}"
                )
            client, original_name = route
            return await client.call_tool(original_name, arguments)
