import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".voice-action" / "config.json"


@dataclass
class Config:
    anthropic_api_key: str = ""
    default_scan_dirs: list = field(default_factory=lambda: [
        "~/Documents", "~/Downloads", "~/Desktop"
    ])
    whisper_model: str = "small"


def load_config() -> Config:
    config = Config()
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            config.anthropic_api_key = data.get("anthropic_api_key", "")
            config.default_scan_dirs = data.get("default_scan_dirs", config.default_scan_dirs)
            config.whisper_model = data.get("whisper_model", config.whisper_model)
        except (json.JSONDecodeError, OSError):
            pass

    # 파일에 키가 없을 때만 환경변수를 폴백으로 사용
    if not config.anthropic_api_key:
        config.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    return config


def save_config(config: Config) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(asdict(config), f, ensure_ascii=False, indent=2)
