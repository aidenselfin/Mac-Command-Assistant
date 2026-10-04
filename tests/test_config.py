import json

import pytest

from voice_action.config import ConfigError, load_config


def test_defaults_and_secret_repr(config):
    assert config.allowed_directories[0].is_absolute()
    assert config.model == "gpt-4o-mini"
    assert "test-secret" not in repr(config)


def test_precedence(tmp_path):
    (tmp_path / "voice-action-test").mkdir()
    home = tmp_path / "home"
    (home / ".voice-action").mkdir(parents=True)
    (home / ".voice-action/config.json").write_text('{"model":"home"}')
    local = tmp_path / "config.json"
    local.write_text('{"model":"local"}')
    env = {"OPENAI_API_KEY": "test", "VOICE_ACTION_MODEL": "environment"}
    assert load_config({"model": "cli"}, cwd=tmp_path, home=home, environ=env).model == "cli"
    assert load_config(cwd=tmp_path, home=home, environ=env).model == "environment"
    del env["VOICE_ACTION_MODEL"]
    assert load_config(cwd=tmp_path, home=home, environ=env).model == "local"
    local.unlink()
    assert load_config(cwd=tmp_path, home=home, environ=env).model == "home"


def test_local_groq_env(tmp_path):
    (tmp_path / "voice-action-test").mkdir()
    (tmp_path / ".voice-action").mkdir()
    (tmp_path / ".voice-action/config.env").write_text("GROQ_API_KEY=local-secret\n")
    config = load_config(cwd=tmp_path, home=tmp_path / "home", environ={})
    assert config.provider == "groq"
    assert config.model == "openai/gpt-oss-20b"
    assert config.base_url == "https://api.groq.com/openai/v1"
    assert config.api_key == "local-secret"


def test_openrouter_provider(tmp_path):
    (tmp_path / "voice-action-test").mkdir()
    config = load_config(
        {"provider": "openrouter"},
        cwd=tmp_path,
        home=tmp_path / "home",
        environ={"OPENROUTER_API_KEY": "router-secret", "GROQ_API_KEY": "unused"},
    )
    assert config.model == "anthropic/claude-sonnet-5.5"
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.api_key == "router-secret"


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "unsupported"},
        {"allowed_directories": []},
        {"allowed_directories": ["missing"]},
        {"max_tool_rounds": 0},
        {"max_tool_rounds": 1.5},
        {"max_tool_rounds": True},
        {"mcp_timeout_seconds": "nan"},
        {"llm_timeout_seconds": -1},
        {"base_url": "https://secret@example.com"},
    ],
)
def test_invalid_config(tmp_path, override):
    (tmp_path / "voice-action-test").mkdir()
    with pytest.raises(ConfigError):
        load_config(override, cwd=tmp_path, home=tmp_path, environ={"OPENAI_API_KEY": "test"})


def test_missing_key(tmp_path):
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_config(cwd=tmp_path, home=tmp_path, environ={})


def test_json_secret_rejected(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"api_key": "secret"}))
    with pytest.raises(ConfigError, match="환경변수"):
        load_config(cwd=tmp_path, home=tmp_path, environ={})


def test_legacy_global_secrets_are_ignored(tmp_path):
    (tmp_path / "voice-action-test").mkdir()
    home = tmp_path / "home"
    (home / ".voice-action").mkdir(parents=True)
    legacy = home / ".voice-action/config.json"
    original = '{"openrouter_api_key":"old-secret","whisper_model":"base"}'
    legacy.write_text(original)
    config = load_config(
        cwd=tmp_path,
        home=home,
        environ={"GROQ_API_KEY": "current-secret"},
    )
    assert config.api_key == "current-secret"
    assert legacy.read_text() == original
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_config(cwd=tmp_path, home=home, environ={})
