from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_project_root() -> Path:
    # src/assistant/config.py → parents[2] = project root
    return Path(__file__).resolve().parents[2]


def _default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "0xone-assistant"


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "0xone-assistant"


def _user_env_file() -> Path:
    return _default_config_dir() / ".env"


class ClaudeSettings(BaseSettings):
    """Claude SDK knobs. Auth is OAuth via `claude` CLI — no API key here."""

    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_",
        env_file=(str(_user_env_file()), ".env"),
        extra="ignore",
    )

    timeout: int = 300
    max_turns: int = 20
    max_concurrent: int = 2
    history_limit: int = 20
    thinking_budget: int = 0  # 0 = disabled; >0 → max_thinking_tokens
    effort: str = "medium"  # 'low'|'medium'|'high'|'max'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_user_env_file()), ".env"),
        extra="ignore",
    )

    telegram_bot_token: str
    owner_chat_id: int
    log_level: str = "INFO"
    project_root: Path = Field(default_factory=_default_project_root)
    data_dir: Path = Field(default_factory=_default_data_dir)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
