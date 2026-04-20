from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # min_length=10 отсекает пустую строку и обрезки токена. Реальные
    # Telegram-токены существенно длиннее; 10 — дешёвый нижний порог.
    telegram_bot_token: str = Field(min_length=10)
    # gt=0: пустой env var не парсится в int вообще, но явный порог
    # даёт понятное сообщение если owner впишет 0.
    owner_chat_id: int = Field(gt=0)
    data_dir: Path = Path("./data")
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
