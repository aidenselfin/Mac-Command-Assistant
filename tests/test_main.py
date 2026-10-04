import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import main
from voice_action.config import ConfigError
from voice_action.macos_client import MacOSAXClient
from voice_action.mcp_client import SUPPORTED_TOOLS, ToolDefinition, ToolResult
from voice_action.mcp_manager import MCPManager
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


class ErrorAXAdapter:
    def __init__(self, failure_stage, secret):
        self.failure_stage = failure_stage
        self.secret = secret

    def is_trusted(self):
        if self.failure_stage == "permission":
            raise RuntimeError(self.secret)
        return True

    def get_active_app_name(self):
        raise RuntimeError(self.secret)


def test_build_manager_registers_full_filesystem_and_web_clients(config, monkeypatch):
    seen = {}
    filesystem = object()
    web = object()
    manager = object()

    def filesystem_client(actual_config, *, allowed_tools):
        seen["filesystem"] = (actual_config, allowed_tools)
        return filesystem

    def web_client(actual_config):
        seen["web_config"] = actual_config
        return web

    def manager_factory(clients):
        seen["clients"] = clients
        return manager

    monkeypatch.setattr(main.sys, "platform", "linux")
    monkeypatch.setattr(main, "MCPClient", filesystem_client)
    monkeypatch.setattr(main, "PlaywrightMCPClient", web_client)
    monkeypatch.setattr(
        main,
        "MacOSAXClient",
        lambda: pytest.fail("non-macOS에서 AX client를 생성하면 안 됩니다."),
    )
    monkeypatch.setattr(main, "MCPManager", manager_factory)

    assert main._build_mcp_manager(config) is manager
    assert seen["filesystem"] == (config, SUPPORTED_TOOLS)
    assert seen["web_config"] is config
    assert seen["clients"] == {"filesystem": filesystem, "web": web}


def test_build_manager_registers_macos_client_only_on_darwin(config, monkeypatch):
    filesystem, web, macos = object(), object(), object()
    seen = {}

    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setattr(main, "MCPClient", lambda *_args, **_kwargs: filesystem)
    monkeypatch.setattr(main, "PlaywrightMCPClient", lambda _config: web)
    monkeypatch.setattr(main, "MacOSAXClient", lambda: macos)
    monkeypatch.setattr(
        main,
        "MCPManager",
        lambda clients: seen.setdefault("clients", clients),
    )

    assert main._build_mcp_manager(config) == {
        "filesystem": filesystem,
        "web": web,
        "macos": macos,
    }
    assert seen["clients"] == {
        "filesystem": filesystem,
        "web": web,
        "macos": macos,
    }


def test_cli_missing_key(monkeypatch, capsys):
    def missing(_):
        raise ConfigError("OPENAI_API_KEY 환경변수가 필요합니다.")

    monkeypatch.setattr(main, "load_config", missing)
    assert main.main(["--text", "파일 목록"]) == 1
    assert "OPENAI_API_KEY" in capsys.readouterr().out


def test_cli_dispatch(config, monkeypatch, capsys):
    seen = []
    undo_journal = object()
    monkeypatch.setattr(main, "load_config", lambda _: config)
    monkeypatch.setattr(main, "_build_undo_journal", lambda _: undo_journal)

    async def run(actual_config, text, console, *, undo_journal):
        seen.append((actual_config, text, undo_journal))
        console.print("[DONE] 한글 메모.txt", markup=False)
        return main.ExecutionTiming()

    monkeypatch.setattr(main, "run_text", run)
    assert main.main(["--text", "voice-action-test 폴더 파일 목록 보여줘"]) == 0
    assert seen == [
        (config, "voice-action-test 폴더 파일 목록 보여줘", undo_journal)
    ]
    assert "한글 메모.txt" in capsys.readouterr().out


def test_cli_interactive_dispatch(config, monkeypatch):
    seen = []
    undo_journal = object()
    monkeypatch.setattr(main, "load_config", lambda _: config)
    monkeypatch.setattr(main, "_build_undo_journal", lambda _: undo_journal)

    async def run(actual_config, console, actual_journal):
        seen.append((actual_config, actual_journal))

    monkeypatch.setattr(main, "run_interactive", run)
    assert main.main(["--interactive"]) == 0
    assert seen == [(config, undo_journal)]


def test_cli_ui_dispatch_uses_loaded_config(config, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "load_config", lambda options: (seen.append(options), config)[1])
    monkeypatch.setattr(main, "run_ui", seen.append)

    assert main.main(["--ui"]) == 0

    assert seen == [
        {
            "provider": None,
            "model": None,
            "base_url": None,
            "allowed_directories": None,
        },
        config,
    ]


def test_unexpected_error_redacted():
    assert "secret" not in main.error_message(RuntimeError("secret"))


def test_execution_timing_output_order(capsys):
    timing = main.ExecutionTiming(
        mcp_startup_ms=10.0,
        planner=main.PlannerTiming([20.0, 30.0, 40.0], 2.0, 3.0),
        total_ms=105.0,
    )
    main.print_timing(main.Console(), timing)
    assert capsys.readouterr().out.splitlines() == [
        "[TIMING]",
        "mcp_startup_ms=10.00",
        "llm_round_1_ms=20.00",
        "mcp_read_ms=2.00",
        "llm_round_2_ms=30.00",
        "mcp_write_ms=3.00",
        "llm_round_3_ms=40.00",
        "total_ms=105.00",
    ]


async def test_run_command_renders_question_and_resumes_number(capsys):
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
    await main.run_command(scripted, "회의록 요약해줘", console)
    await main.run_command(scripted, "2번", console)

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
    monkeypatch.setattr(main, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(main, "OpenAICompatibleModel", lambda _client, _model: model)

    await main.run_text(config, "파일 목록", main.Console())

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
    }


@pytest.mark.parametrize("failure_stage", ["permission", "query"])
async def test_run_text_continues_filesystem_after_bounded_ax_error(
    config, monkeypatch, capsys, failure_stage
):
    secret = "raw-ax-error PID=5137 payload=private-window-title"
    filesystem = FakeLifecycleClient(
        [ToolDefinition("list_directory", "List", {"type": "object"})],
        "[FILE] 한글 메모.txt",
    )
    web = FakeLifecycleClient(
        [ToolDefinition("browser_snapshot", "Snapshot", {"type": "object"})],
        "page snapshot",
    )
    macos = MacOSAXClient(ErrorAXAdapter(failure_stage, secret))
    manager = MCPManager(
        {"filesystem": filesystem, "web": web, "macos": macos}
    )
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn(
            None,
            [ToolCall("active", "macos__get_active_app", "{}")],
        ),
        ModelTurn(
            None,
            [
                ToolCall(
                    "files",
                    "filesystem__list_directory",
                    json.dumps({"path": str(config.allowed_directories[0])}),
                )
            ],
        ),
        ModelTurn("AX 오류 뒤 파일 목록 조회 완료", []),
    ]
    FakeOpenAI.instances.clear()
    monkeypatch.setattr(main, "_build_mcp_manager", lambda _config: manager)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(main, "OpenAICompatibleModel", lambda _client, _model: model)

    await main.run_text(config, "활성 앱 확인 후 파일 목록", main.Console())

    ax_message = model.generate.await_args_list[1].args[0][-1]
    assert ax_message["role"] == "tool"
    assert ax_message["tool_call_id"] == "active"
    assert ax_message["content"].startswith("tool_error:")
    assert secret not in ax_message["content"]
    assert filesystem.calls == [
        (
            "list_directory",
            {"path": str(config.allowed_directories[0])},
        )
    ]
    assert web.calls == []
    assert filesystem.closed and web.closed
    assert not macos._connected
    assert manager.tools == []
    assert len(FakeOpenAI.instances) == 1
    assert FakeOpenAI.instances[0].closed
    output = capsys.readouterr().out
    assert "[TOOL-ERROR] iteration=1 name=macos__get_active_app" in output
    assert "[DONE] AX 오류 뒤 파일 목록 조회 완료" in output
    assert secret not in output
    assert "PID=5137" not in output
    assert "private-window-title" not in output


async def test_interactive_reuses_one_manager_and_llm_client(config, monkeypatch):
    manager = FakeManager()
    model = AsyncMock()
    model.generate.side_effect = [
        ModelTurn("첫 명령 완료", []),
        ModelTurn("둘째 명령 완료", []),
    ]
    FakeOpenAI.instances.clear()

    class Reader:
        def __init__(self):
            self.lines = iter((b"first\n", b"second\n", b"exit\n"))

        async def readline(self):
            return next(self.lines)

    @asynccontextmanager
    async def reader_context():
        yield Reader()

    monkeypatch.setattr(main, "_build_mcp_manager", lambda _: manager)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(main, "OpenAICompatibleModel", lambda _client, _model: model)
    monkeypatch.setattr(main, "input_reader", reader_context)

    await main.run_interactive(config, main.Console())

    assert manager.connect_count == 1
    assert manager.list_count == 1
    assert manager.closed
    assert model.generate.await_count == 2
    assert len(FakeOpenAI.instances) == 1
    assert FakeOpenAI.instances[0].enter_count == 1
    assert FakeOpenAI.instances[0].closed


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
    monkeypatch.setattr(main, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(main, "OpenAICompatibleModel", lambda _client, _model: model)

    undo_journal = UndoJournal(
        config.allowed_directories[0] / "undo-text.sqlite3",
        config.allowed_directories,
    )
    await main.run_text(
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


async def test_interactive_approval_and_rejection_tokens(config, monkeypatch):
    async def run_case(token, should_write):
        manager = FakeManager(
            [ToolDefinition("filesystem__write_file", "Write", {"type": "object"})]
        )
        model = AsyncMock()
        turns = [write_model_turn(config)]
        if should_write:
            turns.append(ModelTurn("완료", []))
        model.generate.side_effect = turns

        class Reader:
            def __init__(self):
                self.lines = iter((b"approved.txt\n", f"{token}\n".encode(), b"exit\n"))

            async def readline(self):
                return next(self.lines)

        @asynccontextmanager
        async def reader_context():
            yield Reader()

        monkeypatch.setattr(main, "_build_mcp_manager", lambda _: manager)
        monkeypatch.setattr(main, "AsyncOpenAI", FakeOpenAI)
        monkeypatch.setattr(main, "OpenAICompatibleModel", lambda _client, _model: model)
        monkeypatch.setattr(main, "input_reader", reader_context)
        undo_journal = UndoJournal(
            config.allowed_directories[0] / f"undo-{token}.sqlite3",
            config.allowed_directories,
        )
        await main.run_interactive(config, main.Console(), undo_journal)
        assert bool(manager.calls) is should_write
        assert model.generate.await_count == (2 if should_write else 1)
        assert undo_journal.entry_count() == (1 if should_write else 0)

    assert main.APPROVE_TOKENS == {"승인", "approve", "yes", "y"}
    assert main.REJECT_TOKENS == {
        "거절",
        "reject",
        "no",
        "n",
        "취소",
        "cancel",
        "c",
    }
    await run_case("승인", True)
    await run_case("reject", False)


async def test_interactive_unknown_and_empty_are_not_approval(
    config, monkeypatch, capsys
):
    manager = FakeManager(
        [ToolDefinition("filesystem__write_file", "Write", {"type": "object"})]
    )
    model = AsyncMock()
    model.generate.side_effect = [write_model_turn(config), ModelTurn("완료", [])]

    class Reader:
        def __init__(self):
            self.lines = iter(
                (
                    b"approved.txt\n",
                    b"\n",
                    b"unknown\n",
                    b"undo\n",
                    b" y \n",
                    b"exit\n",
                )
            )

        async def readline(self):
            return next(self.lines)

    @asynccontextmanager
    async def reader_context():
        yield Reader()

    monkeypatch.setattr(main, "_build_mcp_manager", lambda _: manager)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(main, "OpenAICompatibleModel", lambda _client, _model: model)
    monkeypatch.setattr(main, "input_reader", reader_context)

    undo_journal = UndoJournal(
        config.allowed_directories[0] / "undo-unknown.sqlite3",
        config.allowed_directories,
    )
    await main.run_interactive(config, main.Console(), undo_journal)

    assert len(manager.calls) == 1
    assert model.generate.await_count == 2
    assert capsys.readouterr().out.count("승인 또는 거절 입력이 필요") == 3


async def test_interactive_exit_discards_pending_write_and_closes(config, monkeypatch):
    manager = FakeManager(
        [ToolDefinition("filesystem__write_file", "Write", {"type": "object"})]
    )
    model = AsyncMock()
    model.generate.return_value = write_model_turn(config)

    class Reader:
        def __init__(self):
            self.lines = iter((b"approved.txt\n", b"exit\n"))

        async def readline(self):
            return next(self.lines)

    @asynccontextmanager
    async def reader_context():
        yield Reader()

    monkeypatch.setattr(main, "_build_mcp_manager", lambda _: manager)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(main, "OpenAICompatibleModel", lambda _client, _model: model)
    monkeypatch.setattr(main, "input_reader", reader_context)

    undo_journal = UndoJournal(
        config.allowed_directories[0] / "undo-exit.sqlite3",
        config.allowed_directories,
    )
    await main.run_interactive(config, main.Console(), undo_journal)

    assert manager.calls == []
    assert manager.closed
    assert model.generate.await_count == 1
    assert undo_journal.entry_count() == 0


@pytest.mark.parametrize("token", ["undo", "되돌리기"])
async def test_interactive_undo_tokens_use_typed_api_without_llm(
    config, monkeypatch, capsys, token
):
    manager = FakeManager()
    model = AsyncMock()
    undo_journal = UndoJournal(
        config.allowed_directories[0] / f"undo-command-{token}.sqlite3",
        config.allowed_directories,
    )
    target = config.allowed_directories[0] / f"undo-command-{token}.txt"
    undo_journal.prepare_write(str(target), "create", "written")
    target.write_text("written", encoding="utf-8")

    class Reader:
        def __init__(self):
            self.lines = iter((f"{token}\n".encode(), b"exit\n"))

        async def readline(self):
            return next(self.lines)

    @asynccontextmanager
    async def reader_context():
        yield Reader()

    monkeypatch.setattr(main, "_build_mcp_manager", lambda _: manager)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(main, "OpenAICompatibleModel", lambda _client, _model: model)
    monkeypatch.setattr(main, "input_reader", reader_context)

    await main.run_interactive(config, main.Console(), undo_journal)

    assert not target.exists()
    model.generate.assert_not_awaited()
    assert "생성한 파일을 되돌렸습니다." in capsys.readouterr().out


async def test_interactive_undo_during_clarification_is_pending_answer(
    config, monkeypatch, capsys
):
    manager = FakeManager()
    model = AsyncMock()
    undo_journal = UndoJournal(
        config.allowed_directories[0] / "undo-clarification.sqlite3",
        config.allowed_directories,
    )
    target = config.allowed_directories[0] / "clarification-preserved.txt"
    undo_journal.prepare_write(str(target), "create", "written")
    target.write_text("written", encoding="utf-8")

    class Reader:
        def __init__(self):
            self.lines = iter(
                (
                    "README를 깔끔하게 정리해줘\n".encode(),
                    b"undo\n",
                    "취소\n".encode(),
                    b"exit\n",
                )
            )

        async def readline(self):
            return next(self.lines)

    @asynccontextmanager
    async def reader_context():
        yield Reader()

    monkeypatch.setattr(main, "_build_mcp_manager", lambda _: manager)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(main, "OpenAICompatibleModel", lambda _client, _model: model)
    monkeypatch.setattr(main, "input_reader", reader_context)

    await main.run_interactive(config, main.Console(), undo_journal)

    assert target.read_text(encoding="utf-8") == "written"
    assert undo_journal.entry_count(status="active") == 1
    model.generate.assert_not_awaited()
    output = capsys.readouterr().out
    assert "올바른 선택지 번호" in output
    assert "요청을 취소했습니다" in output


async def test_interactive_non_exact_undo_text_remains_normal_command(
    config, monkeypatch
):
    manager = FakeManager()
    model = AsyncMock()
    model.generate.return_value = ModelTurn("일반 명령 처리", [])

    class Reader:
        def __init__(self):
            self.lines = iter((b"UNDO\n", b"exit\n"))

        async def readline(self):
            return next(self.lines)

    @asynccontextmanager
    async def reader_context():
        yield Reader()

    monkeypatch.setattr(main, "_build_mcp_manager", lambda _: manager)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(main, "OpenAICompatibleModel", lambda _client, _model: model)
    monkeypatch.setattr(main, "input_reader", reader_context)

    await main.run_interactive(config, main.Console())

    model.generate.assert_awaited_once()


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
