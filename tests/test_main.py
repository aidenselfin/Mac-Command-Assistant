import main
from voice_action.config import ConfigError
from voice_action.planner import Clarification, ClarificationContext, Completed


def test_cli_missing_key(monkeypatch, capsys):
    def missing(_):
        raise ConfigError("OPENAI_API_KEY 환경변수가 필요합니다.")

    monkeypatch.setattr(main, "load_config", missing)
    assert main.main(["--text", "파일 목록"]) == 1
    assert "OPENAI_API_KEY" in capsys.readouterr().out


def test_cli_dispatch(config, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(main, "load_config", lambda _: config)

    async def run(actual_config, text, console):
        seen.append((actual_config, text))
        console.print("[DONE] 한글 메모.txt", markup=False)
        return main.ExecutionTiming()

    monkeypatch.setattr(main, "run_text", run)
    assert main.main(["--text", "voice-action-test 폴더 파일 목록 보여줘"]) == 0
    assert seen == [(config, "voice-action-test 폴더 파일 목록 보여줘")]
    assert "한글 메모.txt" in capsys.readouterr().out


def test_cli_interactive_dispatch(config, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "load_config", lambda _: config)

    async def run(actual_config, console):
        seen.append(actual_config)

    monkeypatch.setattr(main, "run_interactive", run)
    assert main.main(["--interactive"]) == 0
    assert seen == [config]


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
