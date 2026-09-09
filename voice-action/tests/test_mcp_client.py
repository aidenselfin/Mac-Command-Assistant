import asyncio
from pathlib import Path
import pytest

from mcp_client import MCPClient


@pytest.mark.asyncio
async def test_mcp_client_connection(tmp_path):
    workspace = str(tmp_path)
    client = MCPClient(allowed_directories=[workspace])

    async with client:
        assert len(client.tools) > 0
        tool_names = [t.name for t in client.tools]
        assert "write_file" in tool_names or "write_text_file" in tool_names or "read_file" in tool_names
        
        # Test OpenAI format
        openai_tools = client.get_openai_tools()
        assert len(openai_tools) == len(client.tools)
        assert openai_tools[0]["type"] == "function"

        # Test Anthropic format
        anthropic_tools = client.get_anthropic_tools()
        assert len(anthropic_tools) == len(client.tools)
        assert "input_schema" in anthropic_tools[0]


@pytest.mark.asyncio
async def test_mcp_file_operations(tmp_path):
    workspace = str(tmp_path)
    client = MCPClient(allowed_directories=[workspace])

    async with client:
        test_file = str(tmp_path / "hello_test.txt")
        # 1. Write file
        res_write = await client.call_tool("write_file", {
            "path": test_file,
            "content": "Automated MCP Test Content\nLine 2"
        })
        assert "Successfully wrote" in res_write or "hello_test.txt" in res_write
        assert Path(test_file).exists()

        # 2. List directory
        res_list = await client.call_tool("list_directory", {
            "path": workspace
        })
        assert "hello_test.txt" in res_list

        # 3. Read file
        res_read = await client.call_tool("read_text_file", {
            "path": test_file
        })
        assert "Automated MCP Test Content" in res_read
