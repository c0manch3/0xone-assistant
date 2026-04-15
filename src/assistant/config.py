from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    owner_chat_id: int
    data_dir: Path = Path("./data")
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
