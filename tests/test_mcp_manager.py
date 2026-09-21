from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from voice_action.mcp_client import (
    MCPClient,
    MCPStartupError,
    MCPToolNotFoundError,
    ToolDefinition,
    ToolResult,
)
from voice_action.mcp_manager import MCPManager


class FakeMCPClient:
    def __init__(self, tools):
        self._tools = tools
        self.calls = []
        self.validated_paths = []
        self.normalized_calls = []
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
        return list(self._tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return ToolResult(True, f"{name} completed")

    async def validate_result_path(self, raw_path):
        self.validated_paths.append(raw_path)
        return Path(raw_path)

    async def normalize_tool_arguments(self, name, arguments):
        self.normalized_calls.append((name, arguments))
        return {**arguments, "normalized": True}


def tool(name, description):
    return ToolDefinition(
        name,
        description,
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )


@pytest.fixture
def clients():
    filesystem = FakeMCPClient([tool("list_directory", "List a directory")])
    fake = FakeMCPClient([tool("example_tool", "Example tool")])
    return filesystem, fake


@pytest.mark.integration
async def test_real_filesystem_and_fake_client_share_manager(config):
    filesystem = MCPClient(config)
    fake = FakeMCPClient([tool("example_tool", "Example tool")])
    manager = MCPManager({"filesystem": filesystem, "fake": fake})

    async with manager.connect():
        assert {item.name for item in await manager.list_tools()} == {
            "filesystem__list_directory",
            "fake__example_tool",
        }
        listing = await manager.call_tool(
            "filesystem__list_directory",
            {"path": str(config.allowed_directories[0])},
        )
        example = await manager.call_tool("fake__example_tool", {"path": "/example"})

        assert listing.ok and "한글 메모.txt" in listing.text
        assert example.ok

    assert filesystem._session is None
    assert fake.closed


async def test_namespaced_tools_dispatch_and_lifecycle(clients):
    filesystem, fake = clients
    manager = MCPManager()
    manager.register("filesystem", filesystem)
    manager.register("fake", fake)

    async with manager.connect():
        tools = await manager.list_tools()
        assert [item.name for item in tools] == [
            "filesystem__list_directory",
            "fake__example_tool",
        ]
        assert tools[0].description == "List a directory"
        assert tools[0].schema == filesystem._tools[0].schema

        filesystem_result = await manager.call_tool(
            "filesystem__list_directory", {"path": "/allowed"}
        )
        fake_result = await manager.call_tool("fake__example_tool", {"path": "/example"})

        assert filesystem_result.ok
        assert fake_result.ok
        assert filesystem.calls == [("list_directory", {"path": "/allowed"})]
        assert fake.calls == [("example_tool", {"path": "/example"})]
        assert filesystem.connected and fake.connected

    assert filesystem.closed and fake.closed
    assert manager.tools == []


async def test_manager_closes_all_clients_on_exception(clients):
    filesystem, fake = clients
    manager = MCPManager({"filesystem": filesystem, "fake": fake})

    with pytest.raises(RuntimeError, match="command failed"):
        async with manager.connect():
            raise RuntimeError("command failed")

    assert filesystem.closed and fake.closed


async def test_manager_delegates_candidate_validation_to_filesystem(clients):
    filesystem, fake = clients
    manager = MCPManager({"filesystem": filesystem, "fake": fake})

    async with manager.connect():
        result = await manager.validate_result_path("/allowed/candidate.md")

    assert result == Path("/allowed/candidate.md")
    assert filesystem.validated_paths == ["/allowed/candidate.md"]
    assert fake.validated_paths == []


async def test_manager_delegates_namespaced_argument_normalization(clients):
    filesystem, fake = clients
    manager = MCPManager({"filesystem": filesystem, "fake": fake})

    async with manager.connect():
        result = await manager.normalize_tool_arguments(
            "filesystem__list_directory", {"path": "/allowed"}
        )

    assert result == {"path": "/allowed", "normalized": True}
    assert filesystem.normalized_calls == [
        ("list_directory", {"path": "/allowed"})
    ]
    assert fake.normalized_calls == []


async def test_second_client_startup_failure_closes_first_client(clients):
    filesystem, _ = clients

    class FailingClient(FakeMCPClient):
        @asynccontextmanager
        async def connect(self):
            raise MCPStartupError("fake startup failed")
            yield self

    failing = FailingClient([tool("example_tool", "Example tool")])
    manager = MCPManager({"filesystem": filesystem, "web": failing})

    with pytest.raises(MCPStartupError, match="startup failed"):
        async with manager.connect():
            pytest.fail("manager must not yield after partial startup failure")

    assert filesystem.closed


async def test_namespaced_tool_collision_fails_startup_and_closes_clients(clients):
    filesystem, fake = clients
    filesystem._tools.append(tool("list_directory", "Duplicate"))
    manager = MCPManager({"filesystem": filesystem, "fake": fake})

    with pytest.raises(MCPStartupError, match="충돌"):
        async with manager.connect():
            pytest.fail("manager must not yield when tool names collide")

    assert filesystem.closed and fake.closed


@pytest.mark.parametrize(
    "name",
    ["list_directory", "unknown__list_directory", "filesystem__missing_tool"],
)
async def test_invalid_namespaced_tool_is_rejected_before_client_call(clients, name):
    filesystem, fake = clients
    manager = MCPManager({"filesystem": filesystem, "fake": fake})

    async with manager.connect():
        with pytest.raises(MCPToolNotFoundError):
            await manager.call_tool(name, {"path": "/allowed"})

    assert filesystem.calls == []
    assert fake.calls == []
