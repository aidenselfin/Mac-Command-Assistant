"""Validated configuration; secrets never come from JSON configuration."""

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    provider: str
    model: str
    api_key: str = field(repr=False)
    allowed_directories: tuple[Path, ...]
    base_url: str | None = None
    max_tool_rounds: int = 5
    llm_timeout_seconds: float = 60
    mcp_timeout_seconds: float = 30
    max_tool_result_chars: int = 50000
    feedback_enabled: bool = True
    feedback_api_key: str | None = field(default=None, repr=False)
    feedback_model: str = "x-ai/grok-4.3"
    feedback_daily_limit: int = 20


def load_config(
    overrides: dict[str, Any] | None = None,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Config:
    cwd, home = cwd or Path.cwd(), home or Path.home()
    # An optional user-owned env file is a convenience for GUI-launched terminals.
    env = dict(dotenv_values(home / ".voice-action/config.env", interpolate=False))
    env.update(dotenv_values(cwd / ".voice-action/config.env", interpolate=False))
    env.update(os.environ if environ is None else environ)
    data: dict[str, Any] = {
        "provider": "groq" if env.get("GROQ_API_KEY") else "openai",
        "allowed_directories": ["./voice-action-test"],
    }
    for file in (home / ".voice-action/config.json", cwd / "config.json"):
        if file.exists():
            try:
                value = json.loads(file.read_text())
            except (OSError, ValueError) as exc:
                raise ConfigError(f"설정 파일을 읽을 수 없습니다: {file}") from exc
            if not isinstance(value, dict):
                raise ConfigError("설정은 JSON object여야 합니다.")
            if file == cwd / "config.json" and any("key" in key.lower() for key in value):
                raise ConfigError("API key는 JSON 대신 환경변수에 설정하세요.")
            # Older global configs stored credentials alongside settings. Never read
            # those credentials; tolerate their presence without modifying the file.
            data.update({key: item for key, item in value.items() if "key" not in key.lower()})
    for key in (
        "provider",
        "model",
        "base_url",
        "allowed_directories",
        "max_tool_rounds",
        "llm_timeout_seconds",
        "mcp_timeout_seconds",
        "max_tool_result_chars",
    ):
        value = env.get(f"VOICE_ACTION_{key.upper()}")
        if value is not None:
            if key == "allowed_directories":
                try:
                    value = json.loads(value)
                except ValueError as exc:
                    raise ConfigError(
                        "VOICE_ACTION_ALLOWED_DIRECTORIES는 JSON 배열이어야 합니다."
                    ) from exc
            data[key] = value
    data.update({k: v for k, v in (overrides or {}).items() if v is not None})
    provider = data["provider"]
    if provider not in ("openai", "groq"):
        raise ConfigError("지원 provider: openai, groq")
    data.setdefault("model", "openai/gpt-oss-20b" if provider == "groq" else "gpt-4o-mini")
    key_name = "GROQ_API_KEY" if provider == "groq" else "OPENAI_API_KEY"
    api_key = env.get(key_name)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ConfigError(f"{key_name} 환경변수가 필요합니다.")
    if not isinstance(data["model"], str) or not data["model"].strip():
        raise ConfigError("model이 필요합니다.")
    roots = data["allowed_directories"]
    if not isinstance(roots, list) or not roots:
        raise ConfigError("allowed_directories는 비어 있지 않은 배열이어야 합니다.")
    resolved = []
    for root in roots:
        if not isinstance(root, str) or not root.strip():
            raise ConfigError("허용 경로는 비어 있지 않은 문자열이어야 합니다.")
        path = Path(root).expanduser()
        try:
            path = (path if path.is_absolute() else cwd / path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ConfigError("허용 디렉터리가 존재하지 않거나 해석할 수 없습니다.") from exc
        if not path.is_dir():
            raise ConfigError(f"디렉터리가 아닙니다: {path}")
        resolved.append(path)
    numbers = {}
    for key, default, kind in (
        ("max_tool_rounds", 5, int),
        ("llm_timeout_seconds", 60, float),
        ("mcp_timeout_seconds", 30, float),
        ("max_tool_result_chars", 50000, int),
    ):
        raw = data.get(key, default)
        try:
            number = kind(raw)
            if isinstance(raw, bool) or not math.isfinite(number) or number <= 0:
                raise ValueError
            if kind is int and float(raw) != number:
                raise ValueError
        except (ValueError, TypeError, OverflowError) as exc:
            raise ConfigError(f"{key}는 양수{' 정수' if kind is int else ''}여야 합니다.") from exc
        numbers[key] = number
    base_url = data.get("base_url")
    if base_url is None and provider == "groq":
        base_url = "https://api.groq.com/openai/v1"
    if base_url is not None:
        if not isinstance(base_url, str):
            raise ConfigError("base_url은 URL 문자열이어야 합니다.")
        parsed = urlparse(base_url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username:
            raise ConfigError("base_url은 인증정보 없는 HTTP(S) URL이어야 합니다.")
    feedback = str(env.get("VOICE_ACTION_FEEDBACK") or "on").strip().casefold()
    if feedback not in ("on", "off"):
        raise ConfigError("VOICE_ACTION_FEEDBACK는 on 또는 off여야 합니다.")
    feedback_model = (env.get("VOICE_ACTION_FEEDBACK_MODEL") or "").strip() or "x-ai/grok-4.3"
    try:
        daily_limit = int(env.get("VOICE_ACTION_FEEDBACK_DAILY_LIMIT") or 20)
        if daily_limit <= 0:
            raise ValueError
    except ValueError as exc:
        raise ConfigError("VOICE_ACTION_FEEDBACK_DAILY_LIMIT는 양의 정수여야 합니다.") from exc
    return Config(
        provider,
        data["model"],
        api_key,
        tuple(resolved),
        base_url,
        **numbers,
        feedback_enabled=feedback == "on",
        feedback_api_key=(env.get("OPENROUTER_API_KEY") or "").strip() or None,
        feedback_model=feedback_model,
        feedback_daily_limit=daily_limit,
    )
