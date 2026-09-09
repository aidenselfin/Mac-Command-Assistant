import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

LOCAL_CONFIG_PATH = Path("config.json")
GLOBAL_CONFIG_PATH = Path.home() / ".voice-action" / "config.json"


@dataclass
class Config:
    # LLM Provider & Model settings
    provider: str = "openai"  # "openai", "anthropic", or "openrouter"
    model: str = "gpt-4o-mini"  # e.g., "gpt-4o-mini", "claude-3-5-sonnet-20241022"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    api_base_url: Optional[str] = None

    # MCP Filesystem Server settings
    mcp_server_command: str = "npx"
    mcp_server_args: List[str] = field(default_factory=lambda: [
        "-y", "@modelcontextprotocol/server-filesystem"
    ])
    allowed_directories: List[str] = field(default_factory=lambda: [
        str(Path.home() / "Desktop"),
        str(Path.home() / "Documents" / "test_workspace")
    ])

    # Audio & STT Settings
    whisper_model: str = "small"  # "tiny", "base", "small", "medium"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    sample_rate: int = 16000
    channels: int = 1
    hotkey: str = "cmd_r"


def load_config() -> Config:
    """Loads configuration prioritizing local config.json, global config, then env vars."""
    config = Config()

    # 1. Load from file
    target_path = LOCAL_CONFIG_PATH if LOCAL_CONFIG_PATH.exists() else GLOBAL_CONFIG_PATH
    if target_path.exists():
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if hasattr(config, k):
                    setattr(config, k, v)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[Warning] Failed to load config from {target_path}: {e}")

    # 2. Environment variables fallback
    if os.environ.get("OPENAI_API_KEY") and not config.openai_api_key:
        config.openai_api_key = os.environ["OPENAI_API_KEY"]
    if os.environ.get("ANTHROPIC_API_KEY") and not config.anthropic_api_key:
        config.anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("OPENROUTER_API_KEY") and not config.openrouter_api_key:
        config.openrouter_api_key = os.environ["OPENROUTER_API_KEY"]
    if os.environ.get("VOICE_ACTION_PROVIDER"):
        config.provider = os.environ["VOICE_ACTION_PROVIDER"]
    if os.environ.get("VOICE_ACTION_MODEL"):
        config.model = os.environ["VOICE_ACTION_MODEL"]

    # Normalize allowed directories (expand ~)
    config.allowed_directories = [
        str(Path(d).expanduser().resolve()) for d in config.allowed_directories
    ]

    return config


def save_config(config: Config, path: Optional[Path] = None) -> Path:
    """Saves configuration to JSON."""
    target_path = path or LOCAL_CONFIG_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(asdict(config), f, ensure_ascii=False, indent=2)
    return target_path
