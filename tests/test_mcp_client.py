import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from voice_action.mcp_client import (
    MCPClient,
    MCPConnectionError,
    MCPError,
    MCPStartupError,
    ToolDefinition,
    ToolRejectedError,
    normalize_result,
)


def client(config):
    mcp = MCPClient(config)
    mcp.tools = [
        ToolDefinition(
            "list_directory",
            "List",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )
    ]
    mcp._session = AsyncMock()
    mcp._roots = config.allowed_directories
    return mcp


def write_client(config):
    mcp = MCPClient(config, allowed_tools={"write_file"})
    mcp.tools = [
        ToolDefinition(
            "write_file",
            "Write",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )
    ]
    mcp._session = AsyncMock()
    mcp._roots = config.allowed_directories
    return mcp


@pytest.mark.parametrize(
    "name,args",
    [
        ("delete_file", {}),
        ("move_file", {}),
        ("write_file", {}),
        ("list_directory", {}),
        ("list_directory", {"path": 12}),
    ],
)
async def test_rejected_before_mcp(config, name, args):
    mcp = client(config)
    with pytest.raises(ToolRejectedError):
        await mcp.call_tool(name, args)
    mcp._session.call_tool.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "",
        lambda config: f" {config.allowed_directories[0]} ",
    ],
)
async def test_relative_empty_and_whitespace_wrapped_paths_are_rejected_before_mcp(
    config, path
):
    mcp = client(config)
    path = path(config) if callable(path) else path

    with pytest.raises(ToolRejectedError, match="절대경로"):
        await mcp.call_tool("list_directory", {"path": path})

    mcp._session.call_tool.assert_not_awaited()


async def test_symlink_escape(config, tmp_path):
    link = config.allowed_directories[0] / "escape"
    link.symlink_to(tmp_path, target_is_directory=True)
    mcp = client(config)
    with pytest.raises(ToolRejectedError, match="허용 경로 밖"):
        await mcp.call_tool("list_directory", {"path": str(link)})
    mcp._session.call_tool.assert_not_awaited()


async def test_write_normalization_reuses_schema_and_canonical_root_boundary(
    config, tmp_path
):
    mcp = write_client(config)
    root = config.allowed_directories[0]
    nested = root / "nested"
    nested.mkdir()
    target = nested / ".." / "new.txt"

    normalized = await mcp.normalize_tool_arguments(
        "write_file", {"path": str(target), "content": "exact"}
    )

    assert normalized == {"path": str(root / "new.txt"), "content": "exact"}
    with pytest.raises(ToolRejectedError):
        await mcp.normalize_tool_arguments(
            "write_file", {"path": str(tmp_path / "outside.txt"), "content": "x"}
        )
    with pytest.raises(ToolRejectedError, match="regular file"):
        await mcp.normalize_tool_arguments(
            "write_file", {"path": str(root), "content": "x"}
        )
    with pytest.raises(ToolRejectedError, match="부모"):
        await mcp.normalize_tool_arguments(
            "write_file",
            {"path": str(root / "missing" / "new.txt"), "content": "x"},
        )
    mcp._session.call_tool.assert_not_awaited()


async def test_result_normalization(config):
    mcp = client(config)
    mcp._session.call_tool.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="abc"),
            SimpleNamespace(type="text", text="def"),
        ],
        isError=True,
        structuredContent={"error": "denied"},
    )
    result = await mcp.call_tool("list_directory", {"path": str(config.allowed_directories[0])})
    assert not result.ok
    assert result.text == "abc\ndef"
    assert result.structured == {"error": "denied"}
    assert result.error == "abc\ndef"
    assert not result.truncated


async def test_timeout_no_retry(config):
    mcp = client(replace(config, mcp_timeout_seconds=0.01))

    async def slow(*args):
        await asyncio.sleep(1)

    mcp._session.call_tool.side_effect = slow
    with pytest.raises(MCPError, match="시간 초과"):
        await mcp.call_tool("list_directory", {"path": str(config.allowed_directories[0])})
    assert mcp._session.call_tool.await_count == 1
    with pytest.raises(MCPConnectionError):
        await mcp.call_tool("list_directory", {"path": str(config.allowed_directories[0])})
    assert mcp._session.call_tool.await_count == 1


@pytest.mark.parametrize(
    "text,limit,expected",
    [
        ("abc", 3, "abc"),
        ("abcd", 3, "abc\n[TRUNCATED]"),
        ("한글😀이름", 3, "한글😀\n[TRUNCATED]"),
    ],
)
def test_text_truncation(text, limit, expected):
    result = normalize_result(
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            structuredContent=None,
            isError=False,
        ),
        limit,
    )
    assert result.text == expected
    assert result.truncated == (len(text) > limit)


def test_structured_payload_cannot_bypass_limit():
    result = normalize_result(
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            structuredContent={"data": "secret" * 1000},
            isError=False,
        ),
        20,
    )
    assert result.structured is None
    assert result.text == "ok\n[TRUNCATED]"
    assert result.truncated


def test_structured_only_and_non_text():
    result = normalize_result(
        SimpleNamespace(
            content=[],
            structuredContent={"number": 2},
            isError=False,
        ),
        100,
    )
    assert result.text == '{"number": 2}'
    assert result.structured == {"number": 2}
    result = normalize_result(
        SimpleNamespace(
            content=[SimpleNamespace(type="image", data="not-for-planner")],
            structuredContent=None,
            isError=False,
        ),
        100,
    )
    assert result.text == "[UNSUPPORTED:image]"


async def test_dynamic_paginated_discovery(config):
    mcp = MCPClient(config, allowed_tools={"list_directory", "read_text_file"})
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def tool(name):
        return SimpleNamespace(name=name, description="runtime description", inputSchema=schema)

    session = AsyncMock()
    session.list_tools.side_effect = [
        SimpleNamespace(tools=[tool("move_file"), tool("list_directory")], nextCursor="next"),
        SimpleNamespace(tools=[tool("read_text_file"), tool("unknown_new_tool")], nextCursor=None),
    ]
    mcp.tools = await mcp._discover(session)
    mcp._session = session
    snapshot = await mcp.list_tools()
    assert {t.name for t in snapshot} == {"list_directory", "read_text_file"}
    assert snapshot[0].description == "runtime description"
    snapshot[0].schema.clear()
    assert mcp.tools[0].schema == schema
    assert session.list_tools.await_count == 2
    session.list_tools.assert_awaited_with(cursor="next")


@pytest.mark.parametrize("failure", ["missing", "duplicate", "schema", "pagination"])
async def test_invalid_discovery_fails_closed(config, failure):
    mcp = client(config)
    tool = SimpleNamespace(
        name="list_directory",
        description=None,
        inputSchema=mcp.tools[0].schema,
    )
    page = SimpleNamespace(tools=[tool], nextCursor=None)
    if failure == "missing":
        page.tools = []
    elif failure == "duplicate":
        page.tools.append(tool)
    elif failure == "schema":
        tool.inputSchema = {"type": "object"}
    else:
        page.nextCursor = "same"
        page.tools = []
    mcp._session.list_tools.return_value = page
    with pytest.raises(MCPStartupError):
        await mcp._discover(mcp._session)


def test_unsupported_allowlist(config):
    with pytest.raises(ToolRejectedError):
        MCPClient(config, allowed_tools={"move_file"})


async def test_sdk_timeout_is_not_retried(config):
    from voice_action.mcp_client import MCPToolTimeoutError

    mcp = client(config)
    mcp._session.call_tool.side_effect = McpError(ErrorData(code=408, message="timeout"))
    with pytest.raises(MCPToolTimeoutError):
        await mcp.call_tool("list_directory", {"path": str(config.allowed_directories[0])})
    with pytest.raises(MCPConnectionError):
        await mcp.call_tool("list_directory", {"path": str(config.allowed_directories[0])})
    assert mcp._session.call_tool.await_count == 1


async def test_tool_calls_are_serialized(config):
    mcp = client(config)
    in_flight = 0

    async def execute(*args):
        nonlocal in_flight
        in_flight += 1
        assert in_flight == 1
        await asyncio.sleep(0.01)
        in_flight -= 1
        return SimpleNamespace(content=[], structuredContent=None, isError=False)

    mcp._session.call_tool.side_effect = execute
    await asyncio.gather(
        *(
            mcp.call_tool("list_directory", {"path": str(config.allowed_directories[0])})
            for _ in range(3)
        )
    )
    assert mcp._session.call_tool.await_count == 3


async def test_read_optional_nulls_are_omitted_without_relaxing_validation(config):
    mcp = MCPClient(config, allowed_tools={"read_text_file"})
    mcp.tools = [
        ToolDefinition(
            "read_text_file",
            "Read",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "head": {"type": "integer", "minimum": 0},
                    "tail": {"type": "integer", "minimum": 0},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )
    ]
    mcp._session = AsyncMock()
    mcp._roots = config.allowed_directories
    path = config.allowed_directories[0] / "source.txt"
    path.write_text("first\nsecond\n")
    for options, expected in [
        ({}, {}),
        ({"head": None, "tail": None}, {}),
        ({"head": 1, "tail": None}, {"head": 1}),
    ]:
        args = {"path": str(path), **options}
        original = args.copy()
        assert await mcp.normalize_tool_arguments("read_text_file", args) == {
            "path": str(path),
            **expected,
        }
        assert args == original
    for invalid in ({"head": "1"}, {"tail": -1}, {"unknown": None}, {"path": None}):
        with pytest.raises(ToolRejectedError):
            await mcp.normalize_tool_arguments("read_text_file", {"path": str(path), **invalid})
    mcp._session.call_tool.assert_not_awaited()
