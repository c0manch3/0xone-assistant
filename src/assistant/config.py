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


class MemorySettings(BaseSettings):
    """Memory / vault knobs. OAuth-agnostic; no secrets.

    Phase 4 (Q1/Q3/Q4/Q9/Q10): vault lives at `<data_dir>/vault/` by default
    (XDG), FTS5 index sits in `<data_dir>/memory-index.db`, porter+unicode61
    tokenizer handles Russian+English. Synthetic history truncation caps
    the per-tool-result snippet that is injected into the next turn as a
    system-note so the model knows what happened without replaying the
    full tool_result block (phase 2 replay was never implemented — Q1).
    """

    model_config = SettingsConfigDict(
        env_prefix="MEMORY_",
        env_file=(str(_user_env_file()), ".env"),
        extra="ignore",
    )

    vault_dir: Path | None = None  # None → data_dir / "vault"
    index_db_path: Path | None = None  # None → data_dir / "memory-index.db"
    fts_tokenizer: str = "porter unicode61 remove_diacritics 2"
    history_tool_result_truncate_chars: int = 2000
    max_body_bytes: int = 1_048_576  # 1 MB — S4 guard on single note body


class SchedulerSettings(BaseSettings):
    """Scheduler knobs (phase 5).

    Every field is overridable via `SCHEDULER_<NAME>` env var. Defaults are
    chosen for the in-process single-user configuration (plan §7 / wave-2
    N-W2-4 split of the cooldowns).
    """

    model_config = SettingsConfigDict(
        env_prefix="SCHEDULER_",
        env_file=(str(_user_env_file()), ".env"),
        extra="ignore",
    )

    enabled: bool = True
    tick_interval_s: int = 15
    tz_default: str = "UTC"
    catchup_window_s: int = 3600
    dead_attempts_threshold: int = 5
    # B2: `claude.timeout` (300) + 60 s. A scheduler-turn with memory ops
    # can take 60-180 s; this MUST stay above the claude-turn timeout or
    # the runtime sweep reverts an actively-delivering trigger.
    sent_revert_timeout_s: int = 360
    dispatcher_queue_size: int = 64
    max_schedules: int = 64
    # Wave-2 N-W2-4: separate cooldowns for distinct user-facing events.
    loop_crash_cooldown_s: int = 86400  # 24 h for `scheduler_loop_fatal` notify
    catchup_recap_cooldown_s: int = 3600  # 1 h for the "pending N missed" recap
    # Wave-2 G-W2-10: heartbeat staleness threshold multiplier
    # (tick_interval_s * heartbeat_stale_multiplier = stale threshold).
    heartbeat_stale_multiplier: int = 10


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
    memory: MemorySettings = Field(default_factory=MemorySettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"

    @property
    def vault_dir(self) -> Path:
        return self.memory.vault_dir or (self.data_dir / "vault")

    @property
    def memory_index_path(self) -> Path:
        return self.memory.index_db_path or (self.data_dir / "memory-index.db")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
