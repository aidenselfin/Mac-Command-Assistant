"""
MCP Client Module
Connects to standard Model Context Protocol (MCP) servers via stdio transport.
Exposes tools in standard formats for OpenAI and Anthropic LLMs.
"""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool


class MCPClient:
    """Manages an active stdio connection to an MCP server."""

    def __init__(
        self,
        command: str = "npx",
        args: Optional[List[str]] = None,
        allowed_directories: Optional[List[str]] = None,
    ):
        self.command = command
        self.args = args or ["-y", "@modelcontextprotocol/server-filesystem"]
        self.allowed_directories = allowed_directories or [
            str(Path.home() / "Desktop"),
            str(Path.home() / "Documents" / "test_workspace"),
        ]
        self._exit_stack = AsyncExitStack()
        self._session: Optional[ClientSession] = None
        self._tools: List[Tool] = []

    async def connect(self) -> None:
        """Starts the MCP server process and initializes the ClientSession."""
        # Ensure allowed directories exist on disk
        for d in self.allowed_directories:
            Path(d).mkdir(parents=True, exist_ok=True)

        full_args = list(self.args) + self.allowed_directories
        server_params = StdioServerParameters(
            command=self.command,
            args=full_args,
        )

        read, write = await self._exit_stack.enter_async_context(stdio_client(server_params))
        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

        # Fetch tools once connected
        tools_result = await self._session.list_tools()
        self._tools = tools_result.tools

    async def disconnect(self) -> None:
        """Cleanly terminates the MCP server connection."""
        self._tools = []
        self._session = None
        await self._exit_stack.aclose()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    @property
    def tools(self) -> List[Tool]:
        return self._tools

    def get_openai_tools(self) -> List[Dict[str, Any]]:
        """Converts MCP tools into OpenAI / OpenRouter function calling format."""
        openai_tools = []
        for tool in self._tools:
            schema = tool.inputSchema if hasattr(tool, "inputSchema") else getattr(tool, "input_schema", {})
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": schema,
                },
            })
        return openai_tools

    def get_anthropic_tools(self) -> List[Dict[str, Any]]:
        """Converts MCP tools into Anthropic tool format."""
        anthropic_tools = []
        for tool in self._tools:
            schema = tool.inputSchema if hasattr(tool, "inputSchema") else getattr(tool, "input_schema", {})
            anthropic_tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": schema,
            })
        return anthropic_tools

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Executes a tool on the MCP server and returns the text result."""
        if not self._session:
            raise RuntimeError("MCP Client is not connected. Call connect() first.")

        try:
            result = await self._session.call_tool(tool_name, arguments=arguments)
            output_parts = []
            if hasattr(result, "content") and result.content:
                for item in result.content:
                    if hasattr(item, "text"):
                        output_parts.append(item.text)
                    else:
                        output_parts.append(str(item))
            
            is_err = getattr(result, "is_error", False) or getattr(result, "isError", False)
            if is_err:
                err_msg = "\n".join(output_parts) or "Tool execution reported an error."
                return f"[MCP Error] {err_msg}"
            return "\n".join(output_parts) if output_parts else "Success (no output content)."
        except Exception as e:
            return f"[MCP Call Error] {type(e).__name__}: {str(e)}"
