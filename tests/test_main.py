import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import main
from tests import runner
from voice_action.mcp_client import SUPPORTED_TOOLS, ToolDefinition, ToolResult
from voice_action.planner import (
    ApprovalRequired,
    Clarification,
    ClarificationContext,
    Completed,
    ModelTurn,
    ToolCall,
)
from voice_action.undo import UndoJournal


class FakeManager:
    def __init__(self, tools=None):
        self.connect_count = 0
        self.closed = False
        self.list_count = 0
        self.calls = []
        self.tools = tools or [
            ToolDefinition("filesystem__list_directory", "List", {"type": "object"}),
            ToolDefinition("web__browser_snapshot", "Snapshot", {"type": "object"}),
        ]

    @asynccontextmanager
    async def connect(self):
        self.connect_count += 1
        try:
            yield self
        finally:
            self.closed = True

    async def list_tools(self):
        self.list_count += 1
        return self.tools

    async def normalize_tool_arguments(self, _name, arguments):
        return dict(arguments)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return ToolResult(True, "ok")


class FakeOpenAI:
    instances = []

    def __init__(self, **_kwargs):
        self.enter_count = 0
        self.closed = False
        self.instances.append(self)

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, *_args):
        self.closed = True


class FakeLifecycleClient:
    def __init__(self, tools, result_text):
        self.tools = tools
        self.result_text = result_text
        self.calls = []
        self.closed = False

    @asynccontextmanager
    async def connect(self):
        try:
            yield self
        finally:
            self.closed = True

    async def list_tools(self):
        return list(self.tools)

    async def normalize_tool_arguments(self, _name, arguments):
        return dict(arguments)

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return ToolResult(True, self.result_text)


def test_build_manager_registers_only_the_filesystem_client(config, monkeypatch):
    seen = {}
    filesystem = object()
    manager = object()

    def filesystem_client(actual_config, *, allowed_tools):
        seen["filesystem"] = (actual_config, allowed_tools)
        return filesystem

    def manager_factory(clients):
        seen["clients"] = clients
        return manager

    monkeypatch.setattr(main, "MCPClient", filesystem_client)
    monkeypatch.setattr(main, "MCPManager", manager_factory)

    assert main._build_mcp_manager(config) is manager
    assert seen["filesystem"] == (config, SUPPORTED_TOOLS)
    assert seen["clients"] == {"filesystem": filesystem}


def test_unexpected_error_redacted():
    assert "secret" not in main.error_message(RuntimeError("secret"))


async def test_print_outcome_renders_question_and_resumes_number(capsys):
    context = ClarificationContext(
        "회의록 요약해줘",
        "file_candidate",
        ("/allowed/회의록-1.md", "/allowed/회의록-2.md"),
        1,
    )
    # Use a small scripted object so the second input can be asserted as the
    # pending answer without constructing network or MCP dependencies.
    class ScriptedSession:
        def __init__(self):
            self.inputs = []

        async def submit(self, text):
            self.inputs.append(text)
            if len(self.inputs) == 1:
                return Clarification(
                    "요약할 회의록을 선택해 주세요.", context.options, context
                )
            return Completed("두 번째 회의록을 요약했습니다.")

    scripted = ScriptedSession()
    console = main.Console()
    for text in ("회의록 요약해줘", "2번"):
        main.print_outcome(console, await scripted.submit(text))

    assert scripted.inputs == ["회의록 요약해줘", "2번"]
    output = capsys.readouterr().out
    assert "[QUESTION] 요약할 회의록을 선택해 주세요." in output
    assert "  2. /allowed/회의록-2.md" in output
    assert "[DONE] 두 번째 회의록을 요약했습니다." in output


async def test_run_text_uses_manager_and_combined_tools(config, monkeypatch):
    manager = FakeManager()
    model = AsyncMock()
    model.generate.return_value = ModelTurn("통합 완료", [])
    FakeOpenAI.instances.clear()

    monkeypatch.setattr(main, "_build_mcp_manager", lambda _: manager)
    monkeypatch.setattr(runner, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(runner, "OpenAICompatibleModel", lambda _client, _model: model)

    await runner.run_text(config, "파일 목록", main.Console())

    assert manager.connect_count == 1
    assert manager.list_count == 1
    assert manager.closed
    assert len(FakeOpenAI.instances) == 1
    assert FakeOpenAI.instances[0].enter_count == 1
    assert FakeOpenAI.instances[0].closed
    assert {tool.name for tool in model.generate.await_args.args[1]} == {
        "filesystem__list_directory",
        "web__browser_snapshot",
        "local__read_document",  # offered for every non-web request
        "local__find_files",
    }


def write_model_turn(config, content="preview"):
    target = config.allowed_directories[0] / "approved.txt"
    return ModelTurn(
        None,
        [
            ToolCall(
                "write",
                "filesystem__write_file",
                json.dumps({"path": str(target), "content": content}),
            )
        ],
    )


async def test_run_text_prints_approval_without_writing(config, monkeypatch, capsys):
    manager = FakeManager(
        [ToolDefinition("filesystem__write_file", "Write", {"type": "object"})]
    )
    model = AsyncMock()
    model.generate.return_value = write_model_turn(config, "bounded preview")
    FakeOpenAI.instances.clear()
    monkeypatch.setattr(main, "_build_mcp_manager", lambda _: manager)
    monkeypatch.setattr(runner, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(runner, "OpenAICompatibleModel", lambda _client, _model: model)

    undo_journal = UndoJournal(
        config.allowed_directories[0] / "undo-text.sqlite3",
        config.allowed_directories,
    )
    await runner.run_text(
        config,
        "approved.txt에 저장해줘",
        main.Console(),
        undo_journal=undo_journal,
    )

    output = capsys.readouterr().out
    assert "[APPROVAL REQUIRED]" in output
    assert "filesystem__write_file" in output
    assert "operation: create" in output
    assert "content_chars: 15" in output
    assert manager.calls == []
    assert manager.closed
    assert undo_journal.entry_count() == 0


def test_approval_output_and_repr_hide_unbounded_content(config, capsys):
    content = "x" * 201 + "SECRET-TAIL"
    outcome = ApprovalRequired(
        "filesystem__write_file",
        str(config.allowed_directories[0] / "safe.txt"),
        "create",
        len(content),
        content[:200],
        True,
        object(),
    )

    main.print_outcome(main.Console(), outcome)

    output = capsys.readouterr().out
    assert "SECRET-TAIL" not in output
    assert content not in repr(outcome)

