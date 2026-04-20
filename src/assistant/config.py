from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_project_root() -> Path:
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
    """Claude-bridge knobs (``CLAUDE_*`` env prefix).

    Notes:
      - ``history_limit`` is a TURN count, not a row count (B6 fix).
      - ``thinking_budget`` defaults to 0 — when > 0, ``effort`` is passed
        alongside ``max_thinking_tokens`` per the R2 spike.
      - OAuth only: no ``api_key`` / token field lives here. The CLI
        session under ``~/.claude/`` is the sole auth path.
    """

    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    timeout: int = 300
    max_turns: int = 20
    max_concurrent: int = 2
    history_limit: int = 20
    thinking_budget: int = 0
    effort: str = "medium"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )

    # min_length=10 отсекает пустую строку и обрезки токена. Реальные
    # Telegram-токены существенно длиннее; 10 — дешёвый нижний порог.
    telegram_bot_token: str = Field(min_length=10)
    # gt=0: пустой env var не парсится в int вообще, но явный порог даёт
    # понятное сообщение если owner впишет 0.
    owner_chat_id: int = Field(gt=0)
    log_level: str = "INFO"
    project_root: Path = Field(default_factory=_default_project_root)
    data_dir: Path = Field(default_factory=_default_data_dir)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)

    @field_validator("project_root", "data_dir", mode="after")
    @classmethod
    def _resolve_absolute(cls, v: Path) -> Path:
        """N2: resolve to absolute so downstream code never sees '.'-relative paths."""
        return v.expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
