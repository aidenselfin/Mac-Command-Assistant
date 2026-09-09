"""
LLM Planner Module
Orchestrates single-LLM tool calling loop with MCP Client.
Supports OpenAI / OpenRouter (via openai SDK) and Anthropic (via anthropic SDK).
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from config import Config
from mcp_client import MCPClient


@dataclass
class ToolExecutionRecord:
    tool_name: str
    arguments: Dict[str, Any]
    result: str
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


@dataclass
class PlannerResponse:
    user_query: str
    final_text: str
    actions: List[ToolExecutionRecord]
    raw_turns: int
    success: bool = True
    error_message: Optional[str] = None


SYSTEM_PROMPT = """You are Voice-Action AI, an intelligent macOS filesystem assistant.
Your job is to understand user natural language voice commands and execute appropriate filesystem operations using the provided MCP tools.

Guidelines:
1. Always resolve paths carefully. Allowed directories are specified in your tool environment.
2. For reading files, use 'read_text_file' (or 'read_file').
3. For listing files, use 'list_directory' or 'search_files'.
4. For creating/writing files, use 'write_file'.
5. When creating notes or files with current date/time, use the system time provided.
6. After executing the tool(s), respond concisely to the user in Korean explaining what action was performed and summarizing the result.
7. Be precise, polite, and helpful. Current system time: {current_time}.
"""


class LLMPlanner:
    def __init__(
        self,
        config: Config,
        mcp_client: MCPClient,
        on_tool_start: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_tool_end: Optional[Callable[[str, str], None]] = None,
    ):
        self.config = config
        self.mcp_client = mcp_client
        self.on_tool_start = on_tool_start
        self.on_tool_end = on_tool_end

    async def execute(self, user_query: str, max_turns: int = 5) -> PlannerResponse:
        """Executes the tool-calling loop for a given user query."""
        provider = self.config.provider.lower()
        if provider in ("openai", "openrouter"):
            return await self._execute_openai(user_query, max_turns=max_turns)
        elif provider == "anthropic":
            return await self._execute_anthropic(user_query, max_turns=max_turns)
        else:
            raise ValueError(f"Unsupported LLM provider: {self.config.provider}")

    async def _execute_openai(self, user_query: str, max_turns: int = 5) -> PlannerResponse:
        import openai

        api_key = self.config.openai_api_key or self.config.openrouter_api_key
        base_url = self.config.api_base_url
        if self.config.provider == "openrouter" and not base_url:
            base_url = "https://openrouter.ai/api/v1"

        client = openai.AsyncOpenAI(
            api_key=api_key or "sk-placeholder",
            base_url=base_url,
        )

        tools = self.mcp_client.get_openai_tools()
        system_content = SYSTEM_PROMPT.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        if self.config.allowed_directories:
            system_content += f"\nAllowed Directories:\n" + "\n".join(f"- {d}" for d in self.config.allowed_directories)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_query},
        ]

        actions: List[ToolExecutionRecord] = []
        final_text = ""
        turns = 0

        try:
            for turn in range(max_turns):
                turns += 1
                kwargs: Dict[str, Any] = {
                    "model": self.config.model,
                    "messages": messages,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                response = await client.chat.completions.create(**kwargs)
                choice = response.choices[0]
                message = choice.message

                # Add assistant message to history
                assistant_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": message.content or "",
                }
                if message.tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ]
                messages.append(assistant_msg)

                # Check if tool calls were requested
                if not message.tool_calls:
                    final_text = message.content or ""
                    break

                # Execute each tool call
                for tc in message.tool_calls:
                    func_name = tc.function.name
                    raw_args = tc.function.arguments
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception:
                        args = {}

                    if self.on_tool_start:
                        self.on_tool_start(func_name, args)

                    result_str = await self.mcp_client.call_tool(func_name, args)

                    if self.on_tool_end:
                        self.on_tool_end(func_name, result_str)

                    actions.append(ToolExecutionRecord(
                        tool_name=func_name,
                        arguments=args,
                        result=result_str,
                    ))

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": func_name,
                        "content": result_str,
                    })

            return PlannerResponse(
                user_query=user_query,
                final_text=final_text,
                actions=actions,
                raw_turns=turns,
                success=True,
            )
        except Exception as e:
            return PlannerResponse(
                user_query=user_query,
                final_text="",
                actions=actions,
                raw_turns=turns,
                success=False,
                error_message=f"{type(e).__name__}: {str(e)}",
            )

    async def _execute_anthropic(self, user_query: str, max_turns: int = 5) -> PlannerResponse:
        import anthropic

        client = anthropic.AsyncAnthropic(
            api_key=self.config.anthropic_api_key or "sk-placeholder",
            base_url=self.config.api_base_url,
        )

        tools = self.mcp_client.get_anthropic_tools()
        system_content = SYSTEM_PROMPT.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        if self.config.allowed_directories:
            system_content += f"\nAllowed Directories:\n" + "\n".join(f"- {d}" for d in self.config.allowed_directories)

        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": user_query},
        ]

        actions: List[ToolExecutionRecord] = []
        final_text = ""
        turns = 0

        try:
            for turn in range(max_turns):
                turns += 1
                kwargs: Dict[str, Any] = {
                    "model": self.config.model,
                    "system": system_content,
                    "max_tokens": 2048,
                    "messages": messages,
                }
                if tools:
                    kwargs["tools"] = tools

                response = await client.messages.create(**kwargs)
                
                # Extract text content and tool use blocks
                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
                text_blocks = [b for b in response.content if b.type == "text"]

                # Append assistant turn to history
                messages.append({
                    "role": "assistant",
                    "content": response.content,
                })

                if not tool_use_blocks:
                    final_text = "".join(b.text for b in text_blocks)
                    break

                # Process tool calls
                tool_results_content = []
                for tu in tool_use_blocks:
                    func_name = tu.name
                    args = tu.input or {}

                    if self.on_tool_start:
                        self.on_tool_start(func_name, args)

                    result_str = await self.mcp_client.call_tool(func_name, args)

                    if self.on_tool_end:
                        self.on_tool_end(func_name, result_str)

                    actions.append(ToolExecutionRecord(
                        tool_name=func_name,
                        arguments=args,
                        result=result_str,
                    ))

                    tool_results_content.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_str,
                    })

                messages.append({
                    "role": "user",
                    "content": tool_results_content,
                })

            return PlannerResponse(
                user_query=user_query,
                final_text=final_text,
                actions=actions,
                raw_turns=turns,
                success=True,
            )
        except Exception as e:
            return PlannerResponse(
                user_query=user_query,
                final_text="",
                actions=actions,
                raw_turns=turns,
                success=False,
                error_message=f"{type(e).__name__}: {str(e)}",
            )
