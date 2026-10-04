import asyncio
import threading
import time
from contextlib import asynccontextmanager

import pytest

from voice_action.macos_client import EMPTY_OBJECT_SCHEMA, MacOSAXClient
from voice_action.mcp_client import (
    MCPToolNotFoundError,
    ToolDefinition,
    ToolRejectedError,
    ToolResult,
)
from voice_action.mcp_manager import MCPManager


class FakeAXAdapter:
    def __init__(
        self,
        *,
        trusted=True,
        name="Finder",
        permission_error=None,
        query_error=None,
        query_delay=0,
    ):
        self.trusted = trusted
        self.name = name
        self.permission_error = permission_error
        self.query_error = query_error
        self.query_delay = query_delay
        self.permission_calls = 0
        self.query_calls = 0
        self.active_queries = 0
        self.max_active_queries = 0
        self._lock = threading.Lock()

    def is_trusted(self):
        self.permission_calls += 1
        if self.permission_error is not None:
            raise self.permission_error
        return self.trusted

    def get_active_app_name(self):
        self.query_calls += 1
        if self.query_error is not None:
            raise self.query_error
        with self._lock:
            self.active_queries += 1
            self.max_active_queries = max(
                self.max_active_queries, self.active_queries
            )
        try:
            time.sleep(self.query_delay)
            return self.name
        finally:
            with self._lock:
                self.active_queries -= 1


class FakeManagedClient:
    def __init__(self, tool_name, result_text):
        self.tool_name = tool_name
        self.result_text = result_text
        self.calls = []
        self.connected = False
        self.closed = False

    @asynccontextmanager
    async def connect(self):
        self.connected = True
        try:
            yield self
        finally:
            self.connected = False
            self.closed = True

    async def list_tools(self):
        return [ToolDefinition(self.tool_name, self.tool_name, {"type": "object"})]

    async def normalize_tool_arguments(self, _name, arguments):
        return dict(arguments)

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return ToolResult(True, self.result_text)


async def test_discovery_namespacing_and_sequential_dispatch():
    adapter = FakeAXAdapter(query_delay=0.02)
    client = MacOSAXClient(adapter)
    manager = MCPManager({"macos": client})

    async with manager.connect():
        tools = await manager.list_tools()
        results = await asyncio.gather(
            manager.call_tool("macos__get_active_app", {}),
            manager.call_tool("macos__get_active_app", {}),
        )

    assert [(tool.name, tool.schema) for tool in tools] == [
        ("macos__get_active_app", EMPTY_OBJECT_SCHEMA)
    ]
    assert [result.text for result in results] == ["Finder", "Finder"]
    assert adapter.permission_calls == 2
    assert adapter.query_calls == 2
    assert adapter.max_active_queries == 1


@pytest.mark.parametrize("failure_stage", ["permission", "query"])
async def test_ax_error_does_not_break_filesystem_web_manager_session(failure_stage):
    secret = "raw-ax-error PID=7821 payload=private-window-title"
    adapter = FakeAXAdapter(
        permission_error=RuntimeError(secret)
        if failure_stage == "permission"
        else None,
        query_error=RuntimeError(secret) if failure_stage == "query" else None,
    )
    filesystem = FakeManagedClient("list_directory", "files")
    web = FakeManagedClient("browser_snapshot", "page")
    macos = MacOSAXClient(adapter)
    manager = MCPManager(
        {"filesystem": filesystem, "web": web, "macos": macos}
    )

    async with manager.connect():
        ax_result = await manager.call_tool("macos__get_active_app", {})
        filesystem_result = await manager.call_tool(
            "filesystem__list_directory", {"path": "/allowed"}
        )
        web_result = await manager.call_tool("web__browser_snapshot", {})

        assert not ax_result.ok
        assert ax_result.error == ax_result.text
        assert len(ax_result.text) <= 200
        assert "raw-ax-error" not in ax_result.text
        assert "7821" not in ax_result.text
        assert "private-window-title" not in ax_result.text
        assert filesystem_result.ok
        assert web_result.ok

    assert filesystem.calls == [("list_directory", {"path": "/allowed"})]
    assert web.calls == [("browser_snapshot", {})]
    assert filesystem.closed and web.closed
    assert not macos._connected
    assert macos.tools == []
    assert manager.tools == []


@pytest.mark.parametrize("invalid_arguments", [{"pid": 123}, [], None])
async def test_invalid_tool_or_arguments_are_rejected_before_adapter_call(
    invalid_arguments,
):
    adapter = FakeAXAdapter()
    client = MacOSAXClient(adapter)

    async with client.connect():
        with pytest.raises(MCPToolNotFoundError):
            await client.call_tool("click", {})
        with pytest.raises(ToolRejectedError, match="빈 object"):
            await client.call_tool("get_active_app", invalid_arguments)

    assert adapter.permission_calls == 0
    assert adapter.query_calls == 0


async def test_permission_denial_is_bounded_tool_error_without_query():
    adapter = FakeAXAdapter(trusted=False)
    client = MacOSAXClient(adapter)

    async with client.connect():
        result = await client.call_tool("get_active_app", {})

    assert not result.ok
    assert result.error == result.text
    assert "개인정보 보호 및 보안 > 손쉬운 사용" in result.text
    assert len(result.text) <= 200
    assert adapter.query_calls == 0


@pytest.mark.parametrize("failure_stage", ["permission", "query"])
async def test_framework_and_query_errors_hide_raw_exception_and_payload(failure_stage):
    secret = "PID=9281 AXPayload=private-document-title"
    adapter = FakeAXAdapter(
        permission_error=ImportError(secret) if failure_stage == "permission" else None,
        query_error=RuntimeError(secret) if failure_stage == "query" else None,
    )
    client = MacOSAXClient(adapter)

    async with client.connect():
        result = await client.call_tool("get_active_app", {})

    assert not result.ok
    assert result.error == result.text
    assert len(result.text) <= 200
    assert "9281" not in result.text
    assert "private-document-title" not in result.text
    assert "AXPayload" not in result.text


async def test_success_result_contains_only_bounded_app_name():
    client = MacOSAXClient(FakeAXAdapter(name="A" * 250))

    async with client.connect():
        result = await client.call_tool("get_active_app", {})

    assert result.ok
    assert result.text == "A" * 200
    assert result.structured is None
    assert result.truncated


@pytest.mark.parametrize("empty_name", ["", "   ", None])
async def test_empty_app_name_is_bounded_tool_error(empty_name):
    client = MacOSAXClient(FakeAXAdapter(name=empty_name))

    async with client.connect():
        result = await client.call_tool("get_active_app", {})

    assert not result.ok
    assert result.error == result.text == "현재 활성 앱 이름을 확인할 수 없습니다."
