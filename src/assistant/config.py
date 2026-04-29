from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
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


class MemorySettings(BaseSettings):
    """Memory subsystem knobs (``MEMORY_*`` env prefix).

    ``vault_dir`` / ``index_db_path`` default to ``None`` so the
    parent ``Settings`` can derive them from ``data_dir`` at access
    time — owner can still override via env vars.
    """

    model_config = SettingsConfigDict(
        env_prefix="MEMORY_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    vault_dir: Path | None = None
    index_db_path: Path | None = None
    max_body_bytes: int = 1_048_576


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
    # Phase 6e: dedicated semaphore size for the bg audio bridge. The
    # owner-text bridge keeps ``max_concurrent`` (2) so a long-running
    # audio job never blocks owner text. Mac whisper-server holds a
    # hard ``Semaphore(1)`` for GPU memory reasons (see
    # plan/phase6e/description.md §RQ3, "CLOSED-NEGATIVE"); raising
    # this above 1 is pointless until the sidecar is rearchitected
    # multi-instance.
    audio_max_concurrent: int = 1
    history_limit: int = 20
    thinking_budget: int = 0
    effort: str = "medium"


class SchedulerSettings(BaseSettings):
    """Scheduler knobs (``SCHEDULER_*`` env prefix).

    All defaults chosen per plan §G.3. Notable:
      - ``sent_revert_timeout_s`` MUST exceed ``claude.timeout`` — a
        running handler that hasn't yet ack'd a trigger shouldn't be
        declared "expired" and reverted by the CR2.1 sweep. The parent
        ``Settings.__init__`` logs a warning if the invariant is
        violated (see below).
      - ``enabled=False`` disables the background loop + dispatcher
        only; the ``@tool`` handlers stay accessible so the model can
        inspect what was scheduled. Fires during the window are LOST
        (devil M-5 — documented trade-off).
    """

    model_config = SettingsConfigDict(
        env_prefix="SCHEDULER_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    enabled: bool = True
    tick_interval_s: int = 15
    tz_default: str = "UTC"
    catchup_window_s: int = 3600
    dead_attempts_threshold: int = 5
    sent_revert_timeout_s: int = 360  # claude.timeout (300) + 60s margin
    dispatcher_queue_size: int = 64
    max_schedules: int = 64
    missed_notify_cooldown_s: int = 86400
    min_recap_threshold: int = 2
    clean_exit_window_s: int = 120
    # Fix 2 / CR-2: threshold in seconds for the orphan-reclaim filter
    # (``reclaim_pending_not_queued``). Only queue-saturated rows whose
    # ``scheduled_for`` is more than this many seconds behind wall-clock
    # are eligible. Must be > ``tick_interval_s`` so a row does not
    # oscillate between materialisation and reclaim inside one tick.
    reclaim_older_than_s: int = 30


class SubagentSettings(BaseSettings):
    """Phase-6 subagent pool knobs (``ASSISTANT_SUBAGENT_*`` env prefix).

    Intentionally small — SDK manages subagent lifecycle through the
    native ``AgentDefinition`` registry; we tune only the ledger
    cadence, notify behaviour, and per-kind ``maxTurns`` ceilings.

    ``picker_tick_s`` is how often :class:`SubagentRequestPicker`
    polls the ``subagent_jobs`` ledger for ``status='requested'``
    rows. ``orphan_stale_s`` is the bucket for the recover_orphans
    Branch 3 (drop ``requested`` rows older than this on boot — see
    research RQ4). ``max_depth=1`` is defensive; the real cap is
    enforced by :func:`build_agents` omitting ``"Task"`` from each
    agent's tool list.
    """

    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_SUBAGENT_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    enabled: bool = True
    picker_tick_s: float = 1.0
    orphan_stale_s: int = 3600
    max_depth: int = 1
    notify_throttle_ms: int = 500
    result_body_max_bytes: int = 32_768
    max_turns_general: int = 20
    max_turns_worker: int = 5
    max_turns_researcher: int = 15
    # Fix-pack F8 (devops MEDIUM): bumped from 2.0 → 5.0 so a
    # SubagentStop notify whose Telegram chunked-send touches the
    # 4096-char body limit (HTTP RTT + chunk reassembly + retries) has
    # room to land before ``Daemon.stop`` cancels the gather. 2s was
    # tight; CI-recorded p95 ranges up to 3.4s on a busy VPS.
    drain_timeout_s: float = 5.0
    # Picker dispatches use this ceiling via bridge.ask(timeout_override=...).
    # Mirrors phase 6c ``claude_voice_timeout`` but kept distinct so future
    # tuning of one path does not collaterally move the other.
    claude_subagent_timeout: int = 900


class AudioBgSettings(BaseSettings):
    """Phase 6e bg-audio-task knobs (``ASSISTANT_AUDIO_BG_*`` env prefix).

    Single knob today — the drain budget for ``Daemon._audio_persist_pending``
    inside ``Daemon.stop``. Mirrors the phase-6 ``subagent.drain_timeout_s``
    pattern so a fired-but-not-yet-flushed persist task can finish before
    ``conn.close()`` slams the door (see spec §6 / researcher RQ2).

    On drain timeout the turn stays ``pending`` in the DB and the boot
    reaper picks it up on the next start.
    """

    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_AUDIO_BG_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    drain_timeout_s: float = 5.0


class ObservabilitySettings(BaseSettings):
    """Phase 6e fix-pack-2 observability knobs (``ASSISTANT_OBSERVABILITY_*``
    env prefix).

    Single knob today — the cadence of the in-process RSS observer.
    The observer reads ``/proc/self/status`` once per ``rss_interval_s``
    and emits a structured ``daemon_rss`` log line so the owner can
    spot drift over weeks before an OOM-restart surprises them.

    The observer exits silently on hosts without ``/proc/self/status``
    (macOS dev box) — see :meth:`Daemon._rss_observer`.
    """

    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_OBSERVABILITY_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    rss_interval_s: float = 60.0


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
    memory: MemorySettings = Field(default_factory=MemorySettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    subagent: SubagentSettings = Field(default_factory=SubagentSettings)
    audio_bg: AudioBgSettings = Field(default_factory=AudioBgSettings)
    observability: ObservabilitySettings = Field(
        default_factory=ObservabilitySettings
    )

    # ------------------------------------------------------------------
    # Phase 6c: voice / audio / URL transcription via Mac mini Whisper.
    #
    # Both ``whisper_api_url`` and ``whisper_api_token`` MUST be set
    # together — see :meth:`_validate_whisper_pair`. When both are
    # ``None`` the bot's audio handlers reply "Mac sidecar offline" and
    # the transcription path stays disabled.
    # ------------------------------------------------------------------
    whisper_api_url: str | None = None
    whisper_api_token: str | None = None
    whisper_timeout: int = 3600
    yt_dlp_timeout: int = 600
    voice_vault_threshold_seconds: int = 120
    voice_meeting_default_area: str = "inbox"
    # C3 closure: a separate timeout for voice/url Claude turns. Default
    # 900s (15 min) is enough for the auto-summary turn that follows a
    # 1-hour transcript; the standard ``claude.timeout`` (300s) stays
    # the default for text + photo + file paths.
    claude_voice_timeout: int = 900

    @field_validator("project_root", "data_dir", mode="after")
    @classmethod
    def _resolve_absolute(cls, v: Path) -> Path:
        """N2: resolve to absolute so downstream code never sees '.'-relative paths."""
        return v.expanduser().resolve()

    @field_validator("whisper_api_token", mode="after")
    @classmethod
    def _validate_whisper_token(cls, v: str | None) -> str | None:
        """F14 (fix-pack): mirror the Mac-side 32-char minimum.

        The sidecar's ``WhisperSettings`` rejects tokens shorter than
        32 chars at boot. If the bot side accepts a typo / truncated
        copy-paste, every transcribe call would fail with a confusing
        401 in production. Reject early with a useful hint.
        """
        if v is None or v == "":
            return v
        if len(v) < 32:
            raise ValueError(
                "WHISPER_API_TOKEN must be at least 32 chars; "
                "regenerate via `python -c 'import secrets; "
                "print(secrets.token_urlsafe(32))'`"
            )
        return v

    @field_validator("whisper_api_url", mode="after")
    @classmethod
    def _validate_whisper_url(cls, v: str | None) -> str | None:
        """F14 (fix-pack): require ``http://`` or ``https://`` scheme.

        Empty / None passes through (sidecar simply disabled). A bare
        hostname like ``host.docker.internal:9000`` (the canonical
        SSH-tunnel value) would otherwise be silently accepted and
        every request would fail with a confusing httpx
        ``UnsupportedProtocol``. Catch at boot.
        """
        if v is None or v == "":
            return v
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                "WHISPER_API_URL must start with http:// or https:// "
                f"(got: {v[:60]!r})"
            )
        return v

    @model_validator(mode="after")
    def _validate_whisper_pair(self) -> Settings:
        """Phase 6c: WHISPER_API_URL and WHISPER_API_TOKEN must move
        together.

        A half-configured pair (URL set without a token, or vice versa)
        would either send unauthenticated requests or send authenticated
        requests to nowhere — both fail in the dark. Surface as a hard
        configuration error at boot.
        """
        url_set = bool(self.whisper_api_url)
        tok_set = bool(self.whisper_api_token)
        if url_set != tok_set:
            raise ValueError(
                "WHISPER_API_URL and WHISPER_API_TOKEN must both be set "
                "or both unset"
            )
        return self

    def model_post_init(self, __context: object) -> None:
        """Warn (not fail) if ``scheduler.sent_revert_timeout_s`` is
        tight vs ``claude.timeout``.

        Risk #13 (description §J): a too-low sent-revert timeout
        reverts a still-running handler before it finishes, causing a
        double-fire on the next tick. The margin is a SOFT requirement
        — owner may deliberately reduce it for test runs — so we log
        rather than refuse to boot.
        """
        if self.scheduler.sent_revert_timeout_s < self.claude.timeout:
            import structlog

            structlog.get_logger("config").warning(
                "sent_revert_timeout_too_low",
                sent_revert_timeout_s=self.scheduler.sent_revert_timeout_s,
                claude_timeout=self.claude.timeout,
                hint=(
                    "SCHEDULER_SENT_REVERT_TIMEOUT_S < CLAUDE_TIMEOUT: "
                    "an in-flight handler may be reverted prematurely, "
                    "causing double-fires on the next tick."
                ),
            )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"

    @property
    def vault_dir(self) -> Path:
        """Resolve the long-term-memory vault directory.

        Falls back to ``<data_dir>/vault`` when ``MEMORY_VAULT_DIR`` is
        unset; always returns an absolute, user-expanded path.
        """
        base = self.memory.vault_dir or (self.data_dir / "vault")
        return base.expanduser().resolve()

    @property
    def memory_index_path(self) -> Path:
        """Resolve the long-term-memory FTS5 index DB path.

        Falls back to ``<data_dir>/memory-index.db`` when
        ``MEMORY_INDEX_DB_PATH`` is unset.
        """
        base = self.memory.index_db_path or (self.data_dir / "memory-index.db")
        return base.expanduser().resolve()

    @property
    def uploads_dir(self) -> Path:
        """Phase 6a: tmp dir for downloaded Telegram attachments.

        - Container (``project_root == /app``): ``/app/.uploads``. The
          file-tool hook in ``bridge/hooks.py`` constrains every ``Read``
          / ``Write`` / ``Edit`` to ``project_root``; placing the tmp dir
          inside ``/app`` keeps the hook surface single-arg (Option 1
          from RQ1 spike).
        - Mac dev (any other ``project_root``): ``<data_dir>/uploads``.
          Mirrors the convention used by ``vault_dir`` and
          ``memory_index_path`` so the on-disk layout for ephemeral and
          persistent state is consistent. Living under ``data_dir``
          (typically ``~/.local/share/0xone-assistant``) keeps tmp data
          OUT of the working tree, so a routine ``git clean -fd`` cannot
          wipe quarantined ``.failed/`` forensics.
        """
        if self.project_root == Path("/app"):
            return Path("/app/.uploads")
        return (self.data_dir / "uploads").expanduser().resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
