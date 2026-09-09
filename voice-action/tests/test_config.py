import os
import tempfile
from pathlib import Path

from config import Config, load_config, save_config


def test_config_defaults():
    cfg = Config()
    assert cfg.whisper_model == "small"
    assert cfg.mcp_server_command == "npx"
    assert len(cfg.allowed_directories) >= 1


def test_config_save_and_load(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg = Config(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        anthropic_api_key="test-key-123",
        allowed_directories=[str(tmp_path)],
    )
    save_config(cfg, cfg_file)
    assert cfg_file.exists()

    # Verify loading
    old_env = os.environ.get("ANTHROPIC_API_KEY")
    try:
        if "ANTHROPIC_API_KEY" in os.environ:
            del os.environ["ANTHROPIC_API_KEY"]
        loaded = load_config()
        # Direct load test
        import json
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["anthropic_api_key"] == "test-key-123"
        assert data["provider"] == "anthropic"
    finally:
        if old_env is not None:
            os.environ["ANTHROPIC_API_KEY"] = old_env
