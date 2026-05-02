from __future__ import annotations

import os
import re
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


_VAULT_SYNC_REPO_URL_RE = re.compile(
    r"^git@[a-z0-9.-]+:[\w.-]+/[\w.-]+\.git$"
)


class VaultSyncSettings(BaseSettings):
    """Phase 8 vault → GitHub push-only periodic sync knobs
    (``VAULT_SYNC_*`` env prefix).

    The subsystem itself is opt-in: ``enabled=False`` (default) keeps
    the daemon observably identical to the phase-6e baseline (AC#5
    parity — no loop spawn, no MCP @tool registration, no audit log
    file, no RSS observer field). Setting ``enabled=True`` requires
    the production-style SSH key + pinned known_hosts to exist on the
    host; otherwise ``startup_check`` force-disables the subsystem
    for the process lifetime and the daemon keeps serving phase
    1..6e traffic (AC#3 / AC#17 / AC#26).

    Validators (W2-C3 / W2-M3 / L-2):

      - ``manual_tool_enabled=True`` requires ``enabled=True`` —
        otherwise the @tool would register but the subsystem itself
        would not be constructed (logically inconsistent). Rejected
        at load time.
      - ``drain_timeout_s >= push_timeout_s`` — a slow but otherwise
        healthy push must always finish within the
        ``Daemon.stop`` drain budget; the inverted invariant would
        force-tear-down a still-legitimate subprocess and orphan
        ``.git/index.lock``.
      - ``repo_url`` required when ``enabled=True``, and (when set)
        matches ``^git@<host>:<owner>/<repo>.git$`` so a typo or
        accidental https URL fails fast.
    """

    model_config = SettingsConfigDict(
        env_prefix="VAULT_SYNC_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    enabled: bool = False
    repo_url: str | None = None
    ssh_key_path: Path | None = None
    ssh_known_hosts_path: Path | None = None
    branch: str = "main"
    cron_interval_s: float = 3600.0
    # Fix-pack F11 (devops CRIT-2): delay before the FIRST tick fires.
    # 60s default protects boot pressure (Telegram polling start, sqlite
    # WAL warm-up, claude preflight) from competing with the vault git
    # pipeline. Owner can override via env var to 0 to restore
    # immediate-first-tick semantics if desired.
    first_tick_delay_s: float = 60.0
    git_user_name: str = "0xone-assistant"
    git_user_email: str = "0xone-assistant@users.noreply.github.com"
    # Fix-pack F4 (devil W3 vault_lock hold): lowered 30 → 10s so the
    # 4-call git pipeline (status / add / diff / commit) at the worst-
    # case ``4 * git_op_timeout_s`` budget stays within
    # ``vault_lock_acquire_timeout_s`` (60s default). A healthy git op
    # completes in <1s; the 10s ceiling triggers only on a wedged
    # filesystem.
    git_op_timeout_s: int = 10
    push_timeout_s: int = 60
    drain_timeout_s: float = 60.0
    # Fix-pack F4: bumped 30 → 60s to cover the worst-case 4-step git
    # pipeline at ``4 * git_op_timeout_s``. Validator below enforces
    # the invariant ``vault_lock_acquire_timeout_s >= 4 * git_op_timeout_s``.
    vault_lock_acquire_timeout_s: float = 60.0
    # Fix-pack F6 (UX): default ``True`` per spec §3 table — owner who
    # flips ``VAULT_SYNC_ENABLED=true`` typically wants the manual @tool
    # available too. The "is the @tool actually visible?" gate is the
    # ``effective_manual_tool_enabled`` computed property below: it
    # returns ``enabled and manual_tool_enabled`` so the validator
    # stays simple (no special-case for owner-set vs default) and the
    # @tool is invisible whenever the subsystem itself is disabled.
    manual_tool_enabled: bool = True
    manual_tool_min_interval_s: float = 60.0
    notify_milestone_failures: tuple[int, ...] = (5, 10, 24)
    audit_log_max_size_mb: int = 10
    # Fix-pack F12 (defense-in-depth parity with .gitignore): the
    # gitignore patterns ``secrets/`` / ``.aws/`` /
    # ``.config/0xone-assistant/`` match RECURSIVELY (a hostile path
    # ``notes/.aws/credentials`` is excluded by the gitignore rule).
    # The daemon-side denylist now matches anywhere on the path via
    # ``(?:^|/)`` so a forced-staged path matching the gitignore would
    # also trip the daemon. The bootstrap script's ``grep -E`` regex is
    # mirrored verbatim from this set (single source of truth).
    secret_denylist_regex: tuple[str, ...] = (
        r"(?:^|/)secrets/",
        r"(?:^|/)\.aws/",
        r"(?:^|/)\.config/0xone-assistant/",
        r"\.env$",
        r"\.key$",
        r"\.pem$",
    )
    # Fix-pack F5 (qa HIGH-4 W2-M2 regression restore): the ``{filenames}``
    # placeholder gives commit-log forensic value (which 3 of N files
    # changed). Devops LOW-4: ASCII ``--`` instead of em-dash so legacy
    # log viewers (older grep / less without UTF-8) render cleanly.
    commit_message_template: str = (
        "vault sync {timestamp} ({reason}) -- {files_changed} files: {filenames}"
    )

    @model_validator(mode="after")
    def _validate_vault_sync_consistency(self) -> VaultSyncSettings:
        """Cross-field validators (W2-C3 + W2-M3 + L-2 + repo_url
        required-when-enabled + F4 vault_lock budget).

        Mirrors the precedent ``_validate_whisper_pair`` at
        ``config.py:293-310`` — pydantic v2 ``mode="after"`` so all
        fields are populated when the check runs.

        Fix-pack F6 (UX): the historic
        ``manual_tool_enabled requires enabled=True`` rule is RELAXED
        for the framework default case — when ``manual_tool_enabled``
        is the default ``True`` and the owner has not explicitly set
        it, ``enabled=False`` simply hides the @tool (via
        :py:attr:`effective_manual_tool_enabled`) without raising. The
        validator only RAISES when the owner explicitly set both
        ``manual_tool_enabled=True`` AND ``enabled=False`` (a logical
        error worth surfacing). Implementation uses
        ``model_fields_set`` (pydantic v2) to distinguish owner-set
        from default. Documented choice: this is the simpler of the
        two paths floated in F6 — no computed-property dance through
        bridge wiring, just a smarter validator that respects the
        principle of least surprise.
        """
        owner_set_manual_tool = (
            "manual_tool_enabled" in self.model_fields_set
        )
        if (
            self.manual_tool_enabled
            and not self.enabled
            and owner_set_manual_tool
        ):
            raise ValueError(
                "manual_tool_enabled=True requires enabled=True; "
                "set VAULT_SYNC_MANUAL_TOOL_ENABLED=false or "
                "VAULT_SYNC_ENABLED=true"
            )
        if self.drain_timeout_s < self.push_timeout_s:
            raise ValueError(
                "drain_timeout_s must be >= push_timeout_s "
                f"(got {self.drain_timeout_s} < {self.push_timeout_s})"
            )
        # F4: the vault_lock budget must cover at least one full
        # 4-step git pipeline (status / add / diff / commit) at the
        # worst-case ``git_op_timeout_s`` ceiling — otherwise a
        # parallel ``memory_write`` racing the cron tick can blow the
        # vault_lock timeout while the pipeline is still legitimately
        # running.
        min_lock_budget = 4 * self.git_op_timeout_s
        if self.vault_lock_acquire_timeout_s < min_lock_budget:
            raise ValueError(
                "vault_lock_acquire_timeout_s must be >= "
                f"4 * git_op_timeout_s ({min_lock_budget}s); "
                f"got {self.vault_lock_acquire_timeout_s}"
            )
        if self.enabled and self.repo_url is None:
            raise ValueError(
                "repo_url required when enabled=True; set "
                "VAULT_SYNC_REPO_URL=git@github.com:<owner>/<repo>.git"
            )
        if self.repo_url is not None and not _VAULT_SYNC_REPO_URL_RE.match(
            self.repo_url
        ):
            raise ValueError(
                "repo_url must match SSH form "
                f"git@host:owner/repo.git (got {self.repo_url!r})"
            )
        return self

    @property
    def effective_manual_tool_enabled(self) -> bool:
        """Phase 8 fix-pack F6 — single source of truth for "should the
        ``vault_push_now`` MCP @tool be registered + visible to the
        model?".

        Returns ``True`` only when the subsystem itself is enabled AND
        ``manual_tool_enabled`` is true. Used by ``Daemon.start`` (gate
        for ``configure_vault``) and by ``ClaudeBridge`` (gate for
        ``mcp_servers["vault"]`` + ``allowed_tools``). When the
        subsystem is disabled this property is ``False`` regardless of
        the env var, so the model never sees the tool — fixing the
        AC#5 violation where the @tool registered even with
        ``enabled=False``.
        """
        return self.enabled and self.manual_tool_enabled


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


class RenderDocSettings(BaseSettings):
    """Phase 9 ``render_doc`` subsystem knobs (``RENDER_DOC_`` env prefix).

    Mounted on :class:`Settings` as ``settings.render_doc``. Mirrors
    :class:`VaultSyncSettings` shape; opt-in via ``enabled`` (default
    ``True`` per spec §2.9 — owner explicitly asked for this feature
    in scope).

    Cross-field validators (W2-MED-3 + LOW-4 + W2-HIGH-1):

      - ``tool_timeout_s`` must accommodate worst-case PDF pipeline
        (``pdf_pandoc_timeout_s + pdf_weasyprint_timeout_s``).
      - All format size caps must be ``<= 20 MiB`` (Telegram cap).
      - ``render_drain_timeout_s`` must accommodate worst-case PDF
        pipeline UNLESS explicitly set to 0 (no-drain opt-out per
        W2-MED-3 NOTE in spec §2.9).
      - ``pandoc_sigterm_grace_s + pandoc_sigkill_grace_s`` must fit
        inside ``render_drain_timeout_s`` (else SIGKILL cleanup races
        ``_bg_tasks`` cancel).
    """

    model_config = SettingsConfigDict(
        env_prefix="RENDER_DOC_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    enabled: bool = True
    artefact_dir: Path | None = None
    artefact_ttl_s: int = 600
    sweep_interval_s: int = 60
    cleanup_threshold_s: int = 86400
    max_input_bytes: int = 1_048_576
    tool_timeout_s: int = 60
    render_max_concurrent: int = 2
    audit_log_max_size_mb: int = 10
    audit_log_keep_last_n: int = 5
    pdf_pandoc_timeout_s: int = 20
    pdf_weasyprint_timeout_s: int = 30
    pdf_max_bytes: int = 20 * 1024 * 1024
    docx_pandoc_timeout_s: int = 15
    docx_max_bytes: int = 10 * 1024 * 1024
    xlsx_max_rows: int = 5000
    xlsx_max_cols: int = 50
    xlsx_max_bytes: int = 10 * 1024 * 1024
    render_drain_timeout_s: float = 20.0
    pandoc_sigterm_grace_s: float = 5.0
    pandoc_sigkill_grace_s: float = 5.0
    audit_field_truncate_chars: int = 256

    @model_validator(mode="after")
    def _validate_render_doc_consistency(self) -> RenderDocSettings:
        if self.tool_timeout_s < (
            self.pdf_pandoc_timeout_s + self.pdf_weasyprint_timeout_s
        ):
            raise ValueError(
                "tool_timeout_s must be >= pdf_pandoc_timeout_s + "
                "pdf_weasyprint_timeout_s; otherwise PDF pipeline "
                "cannot fit worst-case"
            )
        if self.render_max_concurrent < 1:
            raise ValueError("render_max_concurrent must be >= 1")
        for fmt, cap in (
            ("pdf_max_bytes", self.pdf_max_bytes),
            ("docx_max_bytes", self.docx_max_bytes),
            ("xlsx_max_bytes", self.xlsx_max_bytes),
        ):
            if cap > 20 * 1024 * 1024:
                raise ValueError(
                    f"{fmt} must be <= 20 MiB (Telegram send_document "
                    "cap)"
                )
        # W2-MED-3: render_drain must accommodate worst-case PDF
        # pipeline UNLESS explicit-zero opt-out. The DEFAULT drain
        # (20s) is deliberately undersized vs the default PDF pipeline
        # sum (50s) to fit the cumulative ``Daemon.stop`` budget per
        # W2-HIGH-1 honesty paragraph in spec §2.12 (iii); this is an
        # accepted residual risk (orphan WeasyPrint thread on
        # mid-render stop). The validator therefore distinguishes
        # owner-set values (logical-error: reject) from the default
        # (intentional trade-off: accept).
        owner_set_drain = (
            "render_drain_timeout_s" in self.model_fields_set
        )
        if (
            owner_set_drain
            and self.render_drain_timeout_s != 0
            and self.render_drain_timeout_s < (
                self.pdf_pandoc_timeout_s + self.pdf_weasyprint_timeout_s
            )
        ):
            raise ValueError(
                "render_drain_timeout_s must be 0 (explicit no-drain) "
                "or >= pdf_pandoc_timeout_s + pdf_weasyprint_timeout_s "
                "when explicitly set"
            )
        # Pandoc grace must fit inside drain (zero-drain path skips).
        if (
            self.render_drain_timeout_s > 0
            and self.pandoc_sigterm_grace_s + self.pandoc_sigkill_grace_s
            > self.render_drain_timeout_s
        ):
            raise ValueError(
                "pandoc_sigterm_grace_s + pandoc_sigkill_grace_s must "
                "fit inside render_drain_timeout_s"
            )
        return self


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
    vault_sync: VaultSyncSettings = Field(default_factory=VaultSyncSettings)
    render_doc: RenderDocSettings = Field(default_factory=RenderDocSettings)

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
    def artefact_dir(self) -> Path:
        """Phase 9: TTL-managed ephemeral pool for ``render_doc``
        artefacts (PDF/DOCX/XLSX).

        Falls back to ``<data_dir>/artefacts`` when
        ``RENDER_DOC_ARTEFACT_DIR`` is unset; always returns an
        absolute, user-expanded path. Sweeper + boot cleanup walk
        this directory plus its ``.staging/`` subdir.
        """
        base = self.render_doc.artefact_dir or (self.data_dir / "artefacts")
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
