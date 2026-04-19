from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_logger = logging.getLogger(__name__)


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


class SubagentSettings(BaseSettings):
    """Phase-6 subagent pool knobs.

    Intentionally small — the SDK manages lifecycle; we only tune the ledger
    retention, notify throttle, and maxTurns per kind. Every field is
    overridable via `ASSISTANT_SUBAGENT_<NAME>` env var.
    """

    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_SUBAGENT_",
        env_file=(str(_user_env_file()), ".env"),
        extra="ignore",
    )

    enabled: bool = True
    # Telegram notify throttle between consecutive subagent notifications for
    # the SAME chat. Keeps us below the 30-msg/sec global cap even if ten
    # subagents complete in the same second.
    notify_throttle_ms: int = 500
    # Max bytes of the subagent's final assistant message used as notify
    # body BEFORE the Telegram chunker splits it. Phase-5 chunker handles
    # >4096 chars fine; the cap prevents a pathological 1 MB output from
    # flooding the UI.
    result_body_max_bytes: int = 32_768
    # maxTurns for each kind. S-6-0 Q1 showed that longer maxTurns extends
    # wall-clock in ways the owner can't observe mid-turn.
    max_turns_general: int = 20
    max_turns_worker: int = 5
    max_turns_researcher: int = 15
    # Grace window for Daemon.stop to drain in-flight subagent notify tasks.
    drain_timeout_s: float = 2.0
    # Picker tick interval for CLI-spawn pickups.
    picker_tick_s: float = 1.0
    # Rows older than this with status='requested' are transitioned to
    # 'dropped' by recover_orphans at Daemon.start (B-W2-7).
    requested_stale_after_s: int = 3600


class MediaSettings(BaseSettings):
    """Phase-7 media pipeline knobs (photo/voice/audio/document/transcribe/
    genimage/extract/render/retention).

    All fields overridable via `MEDIA_<NAME>` env var. Defaults are
    spike-verified (S-0 Q0-3 for 5 MB photo inline cap; S-6 for the
    15 MB voice cap, kept below the 20 MB Bot-API limit).
    """

    model_config = SettingsConfigDict(
        env_prefix="MEDIA_",
        env_file=".env",
        extra="ignore",
    )

    # Photo path
    photo_mode: Literal["inline_base64", "path_tool"] = "inline_base64"  # S-0 PASS default
    photo_max_inline_bytes: int = 5_242_880  # 5 MB (S-0 Q0-3)
    photo_download_max_bytes: int = 10_485_760  # 10 MB

    # Voice / audio
    voice_max_sec: int = 1800
    voice_inline_threshold_sec: int = 30
    voice_max_bytes: int = 15_000_000  # S-6: below 20 MB Bot-API cap
    audio_max_bytes: int = 50_000_000

    # Document
    document_max_bytes: int = 20_971_520

    # Transcribe (HTTP client)
    transcribe_endpoint: str = "http://localhost:9100/transcribe"
    transcribe_language_default: str = "auto"
    transcribe_timeout_s: int = 60
    transcribe_max_input_bytes: int = 25_000_000

    # Genimage (HTTP client + quota)
    genimage_endpoint: str = "http://localhost:9101/generate"
    genimage_daily_cap: int = 1
    genimage_steps_default: int = 8
    genimage_timeout_s: int = 120

    # Extract / Render
    extract_max_input_bytes: int = 20_000_000
    render_max_body_bytes: int = 512_000
    render_max_output_bytes: int = 10_485_760

    # Retention
    retention_inbox_days: int = 14
    retention_outbox_days: int = 7
    retention_total_cap_bytes: int = 2_147_483_648  # 2 GB
    sweep_interval_s: int = 3600


# ---------------------------------------------------------------------------
# Phase-8 GitHub settings
#
# ASCII-only owner/repo slug (SF2 — reject cyrillic, reject `..`).
# Owner segment is GitHub-accurate per SF-C1: <=38 chars in the middle +
# 1 leading/trailing alnum = 39 max, alnum+hyphen, no leading/trailing
# hyphen, no consecutive hyphens (regex implicitly disallows consecutive
# hyphens because the middle class is `[A-Za-z0-9-]{0,37}` — hyphens are
# allowed, but the final char must be `[A-Za-z0-9]`, so a GH-like check
# for leading/trailing hyphens is enforced by anchoring). The repo
# segment follows GitHub's more lenient `[A-Za-z0-9._-]+` rule.
_GH_SSH_URL_RE = re.compile(
    r"^git@github\.com:"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)"
    r"/(?P<repo>[A-Za-z0-9._-]+)\.git$"
)
# Shell metacharacters B6 rejects in ssh key path.
_SSH_KEY_BAD_CHARS = frozenset(" \t\n$;&|<>'\"\\`")


class GitHubSettings(BaseSettings):
    """Phase-8 GitHub operations + vault auto-commit knobs.

    All fields overridable via ``GH_<NAME>`` env var. Defaults are
    spike-verified (R-1/R-9/R-10/R-12). Nested under ``Settings.github``.

    v2 note on ``allowed_repos``: the annotation uses
    ``Annotated[tuple[str, ...], NoDecode]`` because pydantic-settings 2.3+
    eagerly JSON-decodes any typed tuple/list/dict env value BEFORE the
    field validator runs. Without ``NoDecode``, ``GH_ALLOWED_REPOS="a/b,c/d"``
    would raise ``SettingsError`` (blocker B-A1). With ``NoDecode``, the
    framework delivers the raw string to our ``mode="before"`` validator
    which splits on commas. Confirmed against pydantic-settings 2.13.1.

    Q4 auto-disable: if ``vault_remote_url`` is empty but
    ``auto_commit_enabled`` is ``True``, the ``model_validator`` flips
    ``auto_commit_enabled`` to ``False`` (no ValidationError). The
    downstream scheduler can then silently skip the cron registration
    for a freshly-installed box that has not yet configured a remote.
    """

    model_config = SettingsConfigDict(
        env_prefix="GH_",
        env_file=(str(_user_env_file()), ".env"),
        extra="ignore",
    )

    vault_remote_url: str = ""  # empty -> auto_commit_enabled auto-disabled (Q4)
    vault_ssh_key_path: Path = Field(
        default_factory=lambda: Path.home() / ".ssh" / "id_vault"
    )
    vault_remote_name: str = "vault-backup"  # Q5
    vault_branch: str = "main"
    auto_commit_enabled: bool = True
    auto_commit_cron: str = "0 3 * * *"
    auto_commit_tz: str = "Europe/Moscow"  # Q6
    commit_message_template: str = "vault sync {date}"
    commit_author_email: str = "vaultbot@localhost"  # Q7
    # SF-F3: field name ends in `_path` to match env var GH_VAULT_SSH_KEY_PATH.
    # A mode="before" validator expands `~` (see `_expand_ssh_key_path`).
    # B-A1: NoDecode forces the raw string to reach `_parse_allowed_repos`;
    # without it pydantic-settings 2.13.1 tries JSON-decoding first and fails
    # on `"a/b,c/d"` before any validator runs.
    allowed_repos: Annotated[tuple[str, ...], NoDecode] = ()

    # ------------------------------------------------------------------
    # Validators

    @field_validator("vault_remote_url")
    @classmethod
    def _validate_remote_url(cls, v: str) -> str:
        if not v:
            # Empty allowed; model_validator flips auto_commit_enabled off.
            return v
        match = _GH_SSH_URL_RE.match(v)
        if match is None:
            raise ValueError(
                "vault_remote_url must match git@github.com:OWNER/REPO.git "
                f"(ASCII alnum+._-); got: {v!r}"
            )
        owner, repo = match.group("owner"), match.group("repo")
        for segment, name in ((owner, "owner"), (repo, "repo")):
            if ".." in segment or segment.startswith(".") or segment.endswith("."):
                raise ValueError(
                    f"vault_remote_url {name} segment {segment!r} has dangerous dots"
                )
        return v

    @field_validator("vault_ssh_key_path", mode="before")
    @classmethod
    def _expand_ssh_key_path(cls, v: object) -> object:
        """SF-F3: expand ``~`` in paths coming from env BEFORE other checks.

        ``env_file`` may contain ``GH_VAULT_SSH_KEY_PATH=~/.ssh/id_vault``;
        without expansion pydantic stores the literal ``~/.ssh/id_vault``
        which later fails ``is_file()`` in the command handler.
        """
        if isinstance(v, str):
            return Path(v).expanduser()
        if isinstance(v, Path):
            return v.expanduser()
        return v

    @field_validator("vault_ssh_key_path")
    @classmethod
    def _validate_ssh_key_path(cls, v: Path) -> Path:
        s = str(v)
        for ch in _SSH_KEY_BAD_CHARS:
            if ch in s:
                raise ValueError(
                    "vault_ssh_key_path must not contain metacharacter "
                    f"{ch!r}; got: {s!r}"
                )
        # Paranoid defence against a clever bypass that embeds an OpenSSH
        # `-o` option flag inside the path string.
        if " -o " in s:
            raise ValueError(
                f"vault_ssh_key_path contains ' -o ' substring; got: {s!r}"
            )
        return v

    @field_validator("auto_commit_tz")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {v!r}") from exc
        return v

    @field_validator("auto_commit_cron")
    @classmethod
    def _validate_cron(cls, v: str) -> str:
        # Lazy import to avoid coupling config -> scheduler at module load.
        from assistant.scheduler.cron import parse_cron

        try:
            parse_cron(v)
        except Exception as exc:
            # Re-raise as ValueError so pydantic wraps it into a
            # ValidationError consistent with the other validators.
            raise ValueError(f"invalid cron expression {v!r}: {exc}") from exc
        return v

    @field_validator("allowed_repos", mode="before")
    @classmethod
    def _parse_allowed_repos(cls, v: object) -> tuple[str, ...]:
        """B-A1: accepts raw string thanks to ``NoDecode`` annotation.

        Input shapes:
        - ``"a/b,c/d"`` from env (most common after NoDecode)
        - ``("a/b", "c/d")`` from programmatic instantiation (tests)
        - ``[]`` / ``()`` / ``None`` / ``""`` all collapse to ``()``
        """
        if v is None or v == "":
            return ()
        if isinstance(v, str):
            return tuple(s.strip() for s in v.split(",") if s.strip())
        if isinstance(v, (list, tuple)):
            return tuple(str(s).strip() for s in v if str(s).strip())
        return ()

    @model_validator(mode="after")
    def _auto_disable_on_empty_url(self) -> GitHubSettings:
        if self.auto_commit_enabled and not self.vault_remote_url:
            # BaseSettings instances are mutable, but pydantic v2's model
            # validator contract forbids normal attribute assignment under
            # `mode="after"` on frozen models. `object.__setattr__` is the
            # escape hatch used throughout pydantic's own test suite.
            object.__setattr__(self, "auto_commit_enabled", False)
            _logger.warning(
                "GitHubSettings: vault_remote_url empty; auto_commit_enabled "
                "forced to False (Q4 auto-disable)."
            )
        return self


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
    subagent: SubagentSettings = Field(default_factory=SubagentSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)

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
