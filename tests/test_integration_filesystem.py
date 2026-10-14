import asyncio
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest
from anyio.lowlevel import checkpoint

import voice_action.mcp_client as mcp_module
from voice_action.mcp_client import (
    SUPPORTED_TOOLS,
    MCPClient,
    MCPConnectionError,
    MCPStartupError,
    ToolRejectedError,
)
from voice_action.mcp_manager import MCPManager
from voice_action.planner import Clarification, Completed, ModelTurn, Planner, ToolCall

pytestmark = pytest.mark.integration


@pytest.fixture
async def processes(monkeypatch):
    """Record real OS processes; fail on leaks even if Python state was cleared."""
    spawned = []
    original = anyio.open_process

    async def record(*args, **kwargs):
        process = await original(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(anyio, "open_process", record)
    yield spawned
    leaked = [p for p in spawned if p.returncode is None]
    for process in leaked:
        process.kill()
        await process.wait()
    assert not leaked, "Filesystem MCP subprocess leaked"
    for process in spawned:
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)


async def test_real_mcp_planner_listing_and_root_boundary(config, tmp_path, processes):
    mcp = MCPClient(config)
    manager = MCPManager({"filesystem": mcp})
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(
            None,
            [
                ToolCall(
                    "listing",
                    "filesystem__list_directory",
                    json.dumps(
                        {
                            "path": str(config.allowed_directories[0]),
                        }
                    ),
                )
            ],
        ),
        ModelTurn("조회 완료", []),
    ]
    output = []
    async with manager.connect():
        assert len(processes) == 1
        session = mcp._session
        assert [tool.name for tool in mcp.tools] == ["list_directory"]
        assert await Planner(config, manager, model, output.append).run(
            "파일 목록"
        ) == Completed("조회 완료")
        assert any("한글 메모.txt" in line for line in output)
        with pytest.raises(ToolRejectedError):
            await mcp.call_tool("list_directory", {"path": str(tmp_path)})
        # Independently test the actual server boundary, bypassing app validation.
        denied = await mcp._session.call_tool("list_directory", {"path": str(tmp_path)})
        assert denied.isError
        # The same session serves the next command.
        second = await mcp.call_tool(
            "list_directory",
            {"path": str(config.allowed_directories[0])},
        )
        assert second.ok and "한글 메모.txt" in second.text
        assert mcp._session is session
        assert len(processes) == 1
    assert mcp._session is None


async def test_context_closes_on_exception(config, processes):
    mcp = MCPClient(config)
    with pytest.raises(ExceptionGroup):
        async with mcp.connect():
            raise RuntimeError("command failure")
    assert mcp._session is None
    assert mcp.tools == []


async def test_real_read_write_and_new_file_boundaries(config, tmp_path, processes):
    root = config.allowed_directories[0]
    mcp = MCPClient(config, allowed_tools={"list_directory", "read_text_file", "write_file"})
    async with mcp.connect():
        assert {t.name for t in await mcp.list_tools()} == {
            "list_directory",
            "read_text_file",
            "write_file",
        }
        target = root / "새 파일.txt"
        result = await mcp.call_tool("write_file", {"path": str(target), "content": "안녕 😀"})
        assert result.ok
        assert await asyncio.to_thread(target.read_text) == "안녕 😀"
        read = await mcp.call_tool("read_text_file", {"path": str(target)})
        assert read.ok and "안녕 😀" in read.text
        outside = tmp_path / "outside.txt"
        await asyncio.to_thread(outside.write_text, "unchanged")
        link = root / "escape"
        await asyncio.to_thread(link.symlink_to, tmp_path, target_is_directory=True)
        for path in (outside, root / ".." / "outside.txt", link / "outside.txt", link / "new.txt"):
            with pytest.raises(ToolRejectedError):
                await mcp.call_tool("write_file", {"path": str(path), "content": "bad"})
        with pytest.raises(ToolRejectedError):
            await mcp.call_tool("read_text_file", {"path": str(outside)})
        # Test the server's independent boundary, not only our preflight guard.
        for name, args in (
            ("read_text_file", {"path": str(outside)}),
            ("write_file", {"path": str(tmp_path / "new.txt"), "content": "bad"}),
        ):
            denied = await mcp._session.call_tool(name, args)
            assert denied.isError
        assert await asyncio.to_thread(outside.read_text) == "unchanged"
        assert not await asyncio.to_thread((tmp_path / "new.txt").exists)
        assert len(processes) == 1


async def test_real_read_truncation(config, processes):
    mcp = MCPClient(
        replace(config, max_tool_result_chars=32),
        allowed_tools={"read_text_file", "write_file"},
    )
    async with mcp.connect():
        path = str(config.allowed_directories[0] / "large.txt")
        await mcp.call_tool("write_file", {"path": path, "content": "한글😀" * 1000})
        result = await mcp.call_tool("read_text_file", {"path": path})
        assert result.ok and result.truncated
        assert result.text.endswith("[TRUNCATED]")
        assert len(result.text) <= 32 + len("\n[TRUNCATED]")
        if result.structured is not None:
            assert len(json.dumps(result.structured, ensure_ascii=False)) <= 32


async def test_cancelled_command_closes_process(config, processes):
    ready = asyncio.Event()
    mcp = MCPClient(config)

    async def owner():
        async with mcp.connect():
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await asyncio.wait_for(ready.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mcp._session is None


async def test_anyio_cancellation_closes_process(config, processes):
    mcp = MCPClient(config)
    with anyio.CancelScope() as scope:
        async with mcp.connect():
            scope.cancel()
            await checkpoint()
    assert mcp._session is None


async def test_startup_failure_closes_process(config, processes, monkeypatch):
    mcp = MCPClient(config)
    monkeypatch.setattr(mcp, "_discover", AsyncMock(side_effect=ValueError("bad schema")))
    with pytest.raises(MCPStartupError):
        async with mcp.connect():
            pytest.fail("startup must not yield")
    assert len(processes) == 1


async def test_startup_timeout_closes_process(config, processes, tmp_path, monkeypatch):
    stalled_server = tmp_path / "stalled.cjs"
    await asyncio.to_thread(stalled_server.write_text, "setInterval(() => {}, 1000);")
    monkeypatch.setattr(mcp_module, "SERVER_ENTRYPOINT", stalled_server)
    with pytest.raises(MCPStartupError):
        async with MCPClient(replace(config, mcp_timeout_seconds=0.1)).connect():
            pytest.fail("startup must time out")
    assert len(processes) == 1


async def test_double_connect_does_not_spawn_second_process(config, processes):
    mcp = MCPClient(config)
    async with mcp.connect():
        with pytest.raises(MCPConnectionError):
            async with mcp.connect():
                pytest.fail("cannot connect twice")
        assert len(processes) == 1
        assert (
            await mcp.call_tool(
                "list_directory",
                {
                    "path": str(config.allowed_directories[0]),
                },
            )
        ).ok


async def test_server_exit_fails_without_respawn(config, processes):
    mcp = MCPClient(replace(config, mcp_timeout_seconds=2))
    async with mcp.connect():
        processes[0].kill()
        await processes[0].wait()
        with pytest.raises(mcp_module.MCPError):
            await mcp.call_tool("list_directory", {"path": str(config.allowed_directories[0])})
        with pytest.raises(MCPConnectionError):
            await mcp.call_tool("list_directory", {"path": str(config.allowed_directories[0])})
        assert len(processes) == 1


@pytest.mark.parametrize("exit_mode", ["sigint", "eof", "exit"])
def test_interactive_process_cleanup(config, exit_mode):
    # Launch the actual CLI in a separate process so SIGINT tests real asyncio.run
    # shutdown while stdin is idle. Record only the MCP PID, never credentials.
    script = """
import anyio
import main
original = anyio.open_process
async def record(*args, **kwargs):
    process = await original(*args, **kwargs)
    print(f"[MCP-PID] {process.pid}", flush=True)
    return process
anyio.open_process = record
raise SystemExit(main.main())
"""
    env = {**os.environ, "OPENAI_API_KEY": "test-only", "VOICE_ACTION_FEEDBACK": "off"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            script,
            "--interactive",
            "--provider",
            "openai",
            "--allowed-directory",
            str(config.allowed_directories[0]),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pids = []
    try:
        output = b""
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 10
            while b"[READY]" not in output and time.monotonic() < deadline:
                if selector.select(timeout=0.2):
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output += chunk
        for line in output.splitlines():
            if line.startswith(b"[MCP-PID]"):
                child_pids.append(int(line.split()[-1]))
        assert b"[READY]" in output and len(child_pids) == 2
        if exit_mode == "sigint":
            process.send_signal(signal.SIGINT)
            tail, errors = process.communicate(timeout=10)
        else:
            tail, errors = process.communicate(
                input=b"exit\n" if exit_mode == "exit" else b"",
                timeout=10,
            )
        assert process.returncode == (130 if exit_mode == "sigint" else 0), errors.decode()
        for child_pid in child_pids:
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        for child_pid in child_pids:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_search_runtime_discovery_allowlist_and_boundaries(config, tmp_path, processes):
    root = config.allowed_directories[0]
    first = root / "회의록-검색.md"
    await asyncio.to_thread(first.write_text, "회의 내용", encoding="utf-8")
    outside = tmp_path / "회의록-외부.md"
    await asyncio.to_thread(outside.write_text, "외부", encoding="utf-8")
    escape = root / "search-escape"
    await asyncio.to_thread(escape.symlink_to, tmp_path, target_is_directory=True)
    mcp = MCPClient(config, allowed_tools=SUPPORTED_TOOLS)

    async with mcp.connect():
        tools = await mcp.list_tools()
        names = {tool.name for tool in tools}
        assert names == SUPPORTED_TOOLS
        assert names.isdisjoint({"delete_file", "move_file"})
        search = next(tool for tool in tools if tool.name == "search_files")
        assert search.schema["required"] == ["path", "pattern"]
        assert search.schema["properties"]["excludePatterns"]["type"] == "array"

        result = await mcp.call_tool(
            "search_files",
                            {"path": str(root), "pattern": "**/*회의록*"},
        )
        assert result.ok and str(first.resolve()) in result.text
        assert await mcp.validate_result_path(str(first)) == first.resolve()
        with pytest.raises(ToolRejectedError):
            await mcp.call_tool(
                "search_files",
                {"path": str(tmp_path), "pattern": "*.md"},
            )
        with pytest.raises(ToolRejectedError):
            await mcp.call_tool(
                "search_files",
                {"path": str(escape), "pattern": "*.md"},
            )
        with pytest.raises(ToolRejectedError):
            await mcp.validate_result_path(str(outside))


async def test_real_meeting_note_glob_is_recursive_and_returns_absolute_safe_paths(
    config, tmp_path, processes
):
    root = config.allowed_directories[0]
    nested = root / "nested"
    await asyncio.to_thread(nested.mkdir)
    expected = (root / "회의록-1.md", root / "회의록-2.txt", nested / "회의록-3.md")
    for path in expected:
        await asyncio.to_thread(path.write_text, "fixture", encoding="utf-8")
    await asyncio.to_thread((root / "result.txt").write_text, "fixture", encoding="utf-8")
    outside = tmp_path / "회의록-외부.md"
    await asyncio.to_thread(outside.write_text, "fixture", encoding="utf-8")
    await asyncio.to_thread((root / "escape").symlink_to, tmp_path, target_is_directory=True)
    mcp = MCPClient(config, allowed_tools={"search_files"})

    async with mcp.connect():
        result = await mcp.call_tool(
            "search_files",
            {
                "path": str(root),
                "pattern": "**/*회의록*",
                "excludePatterns": [],
            },
        )

    assert result.ok
    assert set(result.text.splitlines()) == {str(path.resolve()) for path in expected}
    assert str(root / "result.txt") not in result.text
    assert str(outside) not in result.text


async def test_real_search_read_only_candidate_flow(config, processes):
    root = config.allowed_directories[0]
    candidate = root / "회의록-유일.md"
    await asyncio.to_thread(candidate.write_text, "결정 사항: 금요일 배포", encoding="utf-8")
    mcp = MCPClient(config, allowed_tools={"read_text_file"})
    manager = MCPManager({"filesystem": mcp})
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(
            None,
            [
                ToolCall(
                    "find",
                    "local__find_files",
                    json.dumps({"keywords": ["회의록"]}, ensure_ascii=False),
                )
            ],
        ),
        ModelTurn(
            None,
            [
                ToolCall(
                    "read",
                    "filesystem__read_text_file",
                    json.dumps({"path": str(candidate)}, ensure_ascii=False),
                )
            ],
        ),
        ModelTurn("금요일 배포로 요약했습니다.", []),
    ]

    async with manager.connect():
        names = {tool.name for tool in await manager.list_tools()}
        # File lookup is the app's own local__find_files; the server only reads.
        assert names == {"filesystem__read_text_file"}
        assert names.isdisjoint(
            {
                "filesystem__write_file",
                "filesystem__delete_file",
                "filesystem__move_file",
            }
        )
        outcome = await Planner(config, manager, model).run("회의록 요약해줘")

    assert outcome == Completed("금요일 배포로 요약했습니다.")


async def test_real_multiple_search_candidates_stop_before_read(config, processes):
    root = config.allowed_directories[0]
    for name in ("회의록-1.md", "회의록-2.md"):
        await asyncio.to_thread((root / name).write_text, name, encoding="utf-8")
    mcp = MCPClient(config, allowed_tools={"read_text_file"})
    manager = MCPManager({"filesystem": mcp})
    model = AsyncMock()
    model.generate.return_value = ModelTurn(
        None,
        [
            ToolCall(
                "find",
                "local__find_files",
                json.dumps({"keywords": ["회의록"]}, ensure_ascii=False),
            )
        ],
    )

    async with manager.connect():
        outcome = await Planner(config, manager, model).run("회의록 요약해줘")

    assert isinstance(outcome, Clarification)
    assert len(outcome.options) == 2
    assert model.generate.await_count == 1
