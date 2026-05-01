"""Phase 8 §2 — :class:`VaultSyncSubsystem` (the central class).

A daemon-owned object holding two locks and one persisted state file:

  - ``_lock: asyncio.Lock`` — outer lock wrapping the FULL pipeline
    (status / add / commit + push). Serialises the cron loop and the
    ``vault_push_now`` @tool against each other (W2-C2). Constructed in
    ``__init__``; binds on first acquire (Python 3.10+).
  - ``vault_lock(<index_db>.lock, ...)`` from
    :mod:`assistant.tools_sdk._memory_core` — inner SYNCHRONOUS fcntl
    context manager. Wraps only ``status / add / commit`` and is
    released BEFORE ``git push`` so a parallel ``memory_write`` can
    finalise its ``.tmp/.tmp-XXX.md`` rename during the network leg
    (devil C-2).
  - ``_state: VaultSyncState`` — single-line JSON at
    ``<run_dir>/vault_sync_state.json``. Persisted across daemon
    restarts:
      - ``last_state`` ∈ {"ok", "fail"} drives the edge-trigger notify
        machine (W1-H1).
      - ``consecutive_failures`` for milestone notifies (5/10/24).
      - ``last_invocation_at`` (ISO8601 UTC) for the
        ``vault_push_now`` 60s rate-limit (W2-M2 — the rate-limit
        survives a daemon restart).

Pipeline (run_once):

    async with self._lock:                       # outer asyncio
        try:
            with vault_lock(...):                # inner fcntl (SYNC!)
                porcelain = await git_status_porcelain(...)
                if porcelain is empty:
                    audit "noop"; return
                await git_add_all(...)
                staged = await git_diff_cached_names(...)
                if validate_no_secrets(staged) → matches:
                    audit "failed"; notify; return
                sha = await git_commit(...)
        except TimeoutError:
            audit "lock_contention"; return       # NOT a failure!
        await git_push(...)                       # NO vault_lock here

The ``lock_contention`` branch (W2-C1) is the most important nuance:
``vault_lock`` raising ``TimeoutError`` after
``vault_lock_acquire_timeout_s`` is a within-bot timing phenomenon, NOT
a remote failure. It writes an audit row, emits a structured log, and
DOES NOT bump ``consecutive_failures`` or transition the edge-trigger
state machine.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assistant.adapters.base import MessengerAdapter
from assistant.config import VaultSyncSettings
from assistant.logger import get_logger
from assistant.tools_sdk._memory_core import vault_lock
from assistant.vault_sync._validate_paths import validate_no_secrets
from assistant.vault_sync.audit import write_audit_row
from assistant.vault_sync.git_ops import (
    GitOpError,
    git_add_all,
    git_commit,
    git_diff_cached_names,
    git_push,
    git_status_porcelain,
)
from assistant.vault_sync.notify import (
    notify_failure,
    notify_milestone,
    notify_recovery,
)

log = get_logger("vault_sync.subsystem")


# ---------------------------------------------------------------------------
# Filename safety (W2-L3)
# ---------------------------------------------------------------------------
_CTRL_CHARS = set(range(0x00, 0x20)) | {0x7F}


def _sanitize_filename(name: str) -> str:
    """Strip ASCII control chars + newlines from a vault-relative path
    before substitution into the commit-message template (W2-L3).

    A hostile filename containing newlines or control chars could
    otherwise break the commit-message format or smuggle a forged
    trailer (e.g. ``Signed-off-by: x``).
    """
    return "".join(c for c in name if ord(c) not in _CTRL_CHARS)


def _render_commit_message(
    template: str,
    *,
    timestamp: dt.datetime,
    reason: str,
    files_changed: int,
    filenames: list[str],
) -> str:
    """Render the commit message via str.format with sanitised inputs.

    All five template keys are populated even if the template uses
    only some of them — KeyError surfaces a misconfigured template at
    boot rather than mid-run. Filenames are sanitised against
    ``_CTRL_CHARS`` and truncated to the first 3.
    """
    safe_names = [_sanitize_filename(n) for n in filenames[:3]]
    return template.format(
        timestamp=timestamp.replace(microsecond=0).isoformat(),
        reason=reason,
        files_changed=files_changed,
        filenames=", ".join(safe_names),
    )


# ---------------------------------------------------------------------------
# Persisted state
# ---------------------------------------------------------------------------
@dataclass
class VaultSyncState:
    """Persisted state at ``<run_dir>/vault_sync_state.json``.

    Schema is intentionally narrow — three fields, single-line JSON.
    A corrupted file is recoverable by deleting it (next load returns
    fresh defaults).
    """

    last_state: str = "ok"  # "ok" | "fail"
    consecutive_failures: int = 0
    last_invocation_at: str | None = None  # ISO8601 UTC

    def to_json(self) -> str:
        return json.dumps(
            {
                "last_state": self.last_state,
                "consecutive_failures": self.consecutive_failures,
                "last_invocation_at": self.last_invocation_at,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_path(cls, path: Path) -> VaultSyncState:
        if not path.exists():
            return cls()
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                return cls()
            data = json.loads(raw)
            return cls(
                last_state=str(data.get("last_state", "ok")),
                consecutive_failures=int(
                    data.get("consecutive_failures", 0)
                ),
                last_invocation_at=(
                    str(data["last_invocation_at"])
                    if data.get("last_invocation_at")
                    else None
                ),
            )
        except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
            log.warning(
                "vault_sync_state_corrupted",
                path=str(path),
                error=repr(exc),
            )
            return cls()

    def save(self, path: Path) -> None:
        """Atomic write — tmp + rename. A corrupted state file (e.g.
        SIGKILL during write) leaves the prior good content intact."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_json() + "\n", encoding="utf-8")
        tmp.replace(path)


# ---------------------------------------------------------------------------
# Result enum (string-typed for audit-log shape)
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    """Outcome of a single ``run_once`` invocation.

    ``result`` ∈ {"pushed", "noop", "rate_limited", "lock_contention",
    "failed"}. ``commit_sha`` is present only for ``"pushed"`` (a noop
    cycle has no commit).
    """

    result: str
    files_changed: int = 0
    commit_sha: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ok": self.result in ("pushed", "noop", "lock_contention"),
            "result": self.result,
            "files_changed": self.files_changed,
        }
        if self.commit_sha is not None:
            d["commit_sha"] = self.commit_sha
        if self.error is not None:
            d["error"] = self.error
        if self.extra:
            d.update(self.extra)
        return d


# ---------------------------------------------------------------------------
# Subsystem class
# ---------------------------------------------------------------------------
class VaultSyncSubsystem:
    """Phase 8 vault → GitHub push-only sync subsystem.

    Constructed once by :meth:`assistant.main.Daemon.start` when
    ``settings.vault_sync.enabled=True``. The owning daemon also
    drains the ``pending_set`` set inside :meth:`Daemon.stop` BEFORE
    cancelling ``_bg_tasks`` (§2.9).
    """

    def __init__(
        self,
        *,
        vault_dir: Path,
        index_db_lock_path: Path,
        settings: VaultSyncSettings,
        adapter: MessengerAdapter | None,
        owner_chat_id: int,
        run_dir: Path,
        pending_set: set[asyncio.Task[Any]],
    ) -> None:
        self._vault_dir = vault_dir
        self._index_db_lock_path = index_db_lock_path
        self._settings = settings
        self._adapter = adapter
        self._owner_chat_id = owner_chat_id
        self._run_dir = run_dir
        self._pending_set = pending_set
        self._lock = asyncio.Lock()
        self._state_path = run_dir / "vault_sync_state.json"
        self._audit_log_path = run_dir / "vault-sync-audit.jsonl"
        self._state = VaultSyncState.from_path(self._state_path)
        # ``True`` after ``startup_check`` proves the host environment
        # cannot satisfy the contract; set ``enabled``-equivalent off
        # for the process lifetime so the loop body refuses to do
        # anything (AC#3 / AC#17 / AC#26).
        self._force_disabled: bool = False
        # AC#3 / AC#26 surface: external probes (tests, RSS observer)
        # can read this without needing access to the private flag.
        self.disabled_reason: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_ssh_paths(
        s: VaultSyncSettings,
    ) -> tuple[Path, Path]:
        """Return ``(ssh_key_path, ssh_known_hosts_path)``, defaulting
        to ``~/.ssh/vault_deploy`` / ``~/.ssh/known_hosts_vault`` on
        the host filesystem when the settings field is ``None``.

        Static helper so the async ``startup_check`` does not invoke
        ``Path.expanduser`` directly (ASYNC240 — pathlib filesystem
        access from async functions).
        """
        key_path = (
            s.ssh_key_path
            if s.ssh_key_path is not None
            else Path("~/.ssh/vault_deploy").expanduser()
        )
        kh_path = (
            s.ssh_known_hosts_path
            if s.ssh_known_hosts_path is not None
            else Path("~/.ssh/known_hosts_vault").expanduser()
        )
        return (key_path, kh_path)

    async def startup_check(self) -> None:
        """Validate ``ssh_key_path`` + ``ssh_known_hosts_path`` (AC#17 +
        AC#26 + W2-H5).

        On any missing / unreadable input, log + force-disable the
        subsystem for the process lifetime. NEVER raise — the daemon
        must keep serving phase-1..6e traffic even when vault sync is
        broken.
        """
        s = self._settings
        if not s.enabled:
            self._force_disabled = True
            self.disabled_reason = "settings_disabled"
            return
        key_path, kh_path = self._resolve_ssh_paths(s)
        if not key_path.exists():
            log.error(
                "vault_sync_ssh_key_missing",
                path=str(key_path),
                hint="run deploy/scripts/vault-bootstrap.sh on the host",
            )
            self._force_disabled = True
            self.disabled_reason = "ssh_key_missing"
            return
        if not kh_path.exists():
            log.error(
                "vault_sync_known_hosts_missing",
                path=str(kh_path),
                hint=(
                    "copy deploy/known_hosts_vault.pinned to "
                    f"{kh_path}"
                ),
            )
            self._force_disabled = True
            self.disabled_reason = "known_hosts_missing"
            return
        try:
            kh_text = kh_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.error(
                "vault_sync_known_hosts_unreadable",
                path=str(kh_path),
                error=repr(exc),
            )
            self._force_disabled = True
            self.disabled_reason = "known_hosts_unreadable"
            return
        # Sanity check: pinned file must mention github.com on at least
        # one line. A wrong fingerprint surfaces at push time as an
        # ssh "Host key verification failed" error — not catchable by a
        # boot-time string match short of running ssh itself, so we
        # keep this check minimal (W2-H5: AC#26 covers the runtime
        # mismatch path).
        if "github.com" not in kh_text:
            log.error(
                "vault_sync_host_key_mismatch",
                path=str(kh_path),
                hint=(
                    "pinned known_hosts has no github.com entry; "
                    "regenerate via gh api meta | jq -r .ssh_keys[]"
                ),
            )
            self._force_disabled = True
            self.disabled_reason = "host_key_mismatch"
            return
        # Memoise resolved paths so loop body can rely on them.
        self._resolved_key_path = key_path
        self._resolved_known_hosts_path = kh_path
        log.info(
            "vault_sync_startup_check_ok",
            ssh_key=str(key_path),
            known_hosts=str(kh_path),
        )

    @property
    def force_disabled(self) -> bool:
        return self._force_disabled

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    async def loop(self) -> None:
        """Supervised asyncio loop body. Fires the FIRST tick
        immediately (W2-H1) so a deploy with ``enabled=True`` produces
        a visible commit within ~60s of the restart, then sleeps
        ``cron_interval_s`` between subsequent ticks.

        Defensive inner ``try / except`` so a single tick failure does
        not crash the loop — the supervisor's respawn budget is
        reserved for genuinely fatal exceptions.
        """
        if self._force_disabled:
            log.info(
                "vault_sync_loop_skipped_force_disabled",
                reason=self.disabled_reason,
            )
            return
        while True:
            try:
                await self._run_once_tracked(reason="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                log.exception("vault_sync_loop_tick_error")
            await asyncio.sleep(self._settings.cron_interval_s)

    async def _run_once_tracked(self, *, reason: str) -> RunResult:
        """Wrap ``_run_once`` so the in-flight task is registered in
        ``pending_set`` for ``Daemon.stop`` drain (§2.9).

        Cron-tick callers go through this helper too — a slow scheduled
        push that overlaps shutdown must drain to the same budget as a
        manual ``vault_push_now`` invocation.
        """
        task = asyncio.current_task()
        if task is not None:
            self._pending_set.add(task)
            task.add_done_callback(self._pending_set.discard)
        return await self._run_once(reason=reason)

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------
    async def _run_once(self, *, reason: str) -> RunResult:
        """Run a single sync cycle end-to-end.

        See class docstring for the full pipeline + locking contract.
        """
        if self._force_disabled:
            return RunResult(
                result="failed", error="force_disabled"
            )
        async with self._lock:
            return await self._run_pipeline(reason=reason)

    async def _run_pipeline(self, *, reason: str) -> RunResult:
        """Inner pipeline (caller holds ``self._lock``)."""
        s = self._settings
        # 1. Acquire INNER fcntl vault_lock (sync ctx mgr — W2-C1).
        try:
            with vault_lock(
                self._index_db_lock_path,
                blocking=True,
                timeout=s.vault_lock_acquire_timeout_s,
            ):
                # 2. Working-tree-affecting ops only.
                porcelain = await git_status_porcelain(
                    self._vault_dir,
                    timeout_s=s.git_op_timeout_s,
                )
                if not porcelain.strip():
                    log.info(
                        "vault_sync_no_changes", reason=reason
                    )
                    result = RunResult(result="noop")
                    self._record_audit(reason=reason, result=result)
                    return result
                await git_add_all(
                    self._vault_dir,
                    timeout_s=s.git_op_timeout_s,
                )
                staged = await git_diff_cached_names(
                    self._vault_dir,
                    timeout_s=s.git_op_timeout_s,
                )
                if not staged:
                    # ``git add -A`` had nothing to stage despite a
                    # non-empty porcelain — could happen if every
                    # changed file matched .gitignore. Treat as noop.
                    log.info(
                        "vault_sync_nothing_staged", reason=reason
                    )
                    result = RunResult(result="noop")
                    self._record_audit(reason=reason, result=result)
                    return result
                # 2.5 W2-H4: secret denylist before commit.
                matches = validate_no_secrets(
                    staged, s.secret_denylist_regex
                )
                if matches:
                    log.error(
                        "vault_sync_denylist_block",
                        reason=reason,
                        matches=matches[:10],
                    )
                    err = (
                        "secret denylist matches: "
                        + ", ".join(matches[:5])
                    )
                    result = RunResult(
                        result="failed", error=err
                    )
                    self._record_audit(reason=reason, result=result)
                    await self._handle_failure_edge(err)
                    return result
                # 3. Commit.
                now = dt.datetime.now(dt.UTC)
                commit_msg = _render_commit_message(
                    s.commit_message_template,
                    timestamp=now,
                    reason=reason,
                    files_changed=len(staged),
                    filenames=staged,
                )
                try:
                    sha = await git_commit(
                        self._vault_dir,
                        message=commit_msg,
                        author_name=s.git_user_name,
                        author_email=s.git_user_email,
                        timeout_s=s.git_op_timeout_s,
                    )
                except GitOpError as exc:
                    log.error(
                        "vault_sync_commit_failed",
                        reason=reason,
                        error=str(exc),
                    )
                    result = RunResult(
                        result="failed", error=str(exc)
                    )
                    self._record_audit(reason=reason, result=result)
                    await self._handle_failure_edge(str(exc))
                    return result
            # 4. INNER vault_lock RELEASED (with-block exited).
            #    memory_write can now resume; concurrent vault_sync
            #    invocations still block on the OUTER asyncio _lock.
        except TimeoutError:
            # W2-C1: lock_contention is NOT a push failure.
            log.warning(
                "vault_sync_lock_contention",
                reason=reason,
                timeout_s=s.vault_lock_acquire_timeout_s,
            )
            result = RunResult(result="lock_contention")
            self._record_audit(reason=reason, result=result)
            return result

        # 5. Push WITHOUT vault_lock — git push only reads
        #    .git/objects/ and the network; never the working tree.
        if not getattr(self, "_resolved_key_path", None) or not getattr(
            self, "_resolved_known_hosts_path", None
        ):
            err = "ssh paths not resolved; startup_check skipped?"
            log.error("vault_sync_push_skipped_no_paths")
            result = RunResult(
                result="failed",
                files_changed=len(staged),
                commit_sha=sha,
                error=err,
            )
            self._record_audit(reason=reason, result=result)
            await self._handle_failure_edge(err)
            return result
        try:
            assert s.repo_url is not None  # validator ensures this
            await git_push(
                self._vault_dir,
                remote=s.repo_url,
                branch=s.branch,
                ssh_key_path=self._resolved_key_path,
                known_hosts_path=self._resolved_known_hosts_path,
                timeout_s=s.push_timeout_s,
            )
        except GitOpError as exc:
            log.error(
                "vault_sync_push_failed",
                reason=reason,
                error=str(exc),
                returncode=exc.returncode,
            )
            result = RunResult(
                result="failed",
                files_changed=len(staged),
                commit_sha=sha,
                error=str(exc),
            )
            self._record_audit(reason=reason, result=result)
            await self._handle_failure_edge(str(exc))
            return result

        log.info(
            "vault_sync_pushed",
            reason=reason,
            files_changed=len(staged),
            commit_sha=sha,
        )
        result = RunResult(
            result="pushed",
            files_changed=len(staged),
            commit_sha=sha,
        )
        self._record_audit(reason=reason, result=result)
        await self._handle_success_edge()
        return result

    # ------------------------------------------------------------------
    # Manual @tool path
    # ------------------------------------------------------------------
    async def push_now(self) -> dict[str, Any]:
        """Manual ``vault_push_now`` MCP @tool body.

        Implements the 60s rate-limit (W2-C4 / W2-M2 — persisted
        across restart via ``last_invocation_at`` in the state file)
        and registers the running task in ``pending_set`` so
        ``Daemon.stop`` can drain it.
        """
        if self._force_disabled:
            return {
                "ok": False,
                "reason": "not_configured",
                "detail": self.disabled_reason or "force_disabled",
            }

        s = self._settings
        now = dt.datetime.now(dt.UTC)

        # Rate-limit check.
        if self._state.last_invocation_at:
            try:
                last = dt.datetime.fromisoformat(
                    self._state.last_invocation_at
                )
                if last.tzinfo is None:
                    last = last.replace(tzinfo=dt.UTC)
                elapsed = (now - last).total_seconds()
                if elapsed < s.manual_tool_min_interval_s:
                    remaining = int(
                        s.manual_tool_min_interval_s - elapsed
                    )
                    log.info(
                        "vault_sync_manual_rate_limited",
                        next_eligible_in_s=remaining,
                    )
                    rl_result = RunResult(
                        result="rate_limited",
                        extra={"next_eligible_in_s": remaining},
                    )
                    self._record_audit(
                        reason="manual", result=rl_result
                    )
                    return {
                        "ok": False,
                        "reason": "rate_limit",
                        "next_eligible_in_s": remaining,
                    }
            except (ValueError, TypeError) as exc:
                log.warning(
                    "vault_sync_state_invocation_unparseable",
                    raw=self._state.last_invocation_at,
                    error=repr(exc),
                )

        # Update timer at INVOCATION time (not completion) so the
        # rate-limit covers the operation duration (§2.8 step 6).
        self._state.last_invocation_at = now.isoformat()
        self._state.save(self._state_path)

        # Run the pipeline as a tracked task so ``Daemon.stop`` drain
        # observes it. ``asyncio.shield`` keeps the surrounding await
        # from cancelling the inner pipeline if the @tool caller's
        # context dies mid-push (defensive).
        task: asyncio.Task[RunResult] = asyncio.create_task(
            self._run_once_tracked(reason="manual"),
            name="vault_push_now",
        )
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Surface cancellation upward; the inner task continues
            # running anchored in pending_set.
            raise
        return result.to_dict()

    # ------------------------------------------------------------------
    # Edge-trigger state machine
    # ------------------------------------------------------------------
    async def _handle_success_edge(self) -> None:
        """Drive the ``ok→ok`` (silent) and ``fail→ok`` (recovery
        notify) transitions."""
        if self._state.last_state == "fail":
            prev_failures = self._state.consecutive_failures
            self._state.last_state = "ok"
            self._state.consecutive_failures = 0
            self._state.save(self._state_path)
            await notify_recovery(
                self._adapter, self._owner_chat_id, prev_failures
            )
            return
        # Already ok; ensure the counter is zero (defensive — should
        # already be).
        if self._state.consecutive_failures != 0:
            self._state.consecutive_failures = 0
            self._state.save(self._state_path)

    async def _handle_failure_edge(self, error: str) -> None:
        """Drive the ``ok→fail`` (notify), ``fail→fail`` (silent +
        milestone) transitions."""
        was_ok = self._state.last_state == "ok"
        self._state.consecutive_failures += 1
        self._state.last_state = "fail"
        self._state.save(self._state_path)
        if was_ok:
            await notify_failure(
                self._adapter, self._owner_chat_id, error
            )
            return
        if self._state.consecutive_failures in (
            self._settings.notify_milestone_failures
        ):
            await notify_milestone(
                self._adapter,
                self._owner_chat_id,
                self._state.consecutive_failures,
            )

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------
    def _record_audit(
        self, *, reason: str, result: RunResult
    ) -> None:
        """Append a single JSONL audit row, with 10 MB rotation
        (W2-H2)."""
        max_bytes = self._settings.audit_log_max_size_mb * 1024 * 1024
        row: dict[str, Any] = {
            "ts": dt.datetime.now(dt.UTC)
            .replace(microsecond=0)
            .isoformat(),
            "reason": reason,
            "result": result.result,
            "files_changed": result.files_changed,
            "commit_sha": result.commit_sha,
            "error": result.error,
        }
        if result.extra:
            row.update(
                {
                    k: v
                    for k, v in result.extra.items()
                    if k not in row
                }
            )
        try:
            write_audit_row(
                self._audit_log_path, row, max_size_bytes=max_bytes
            )
        except OSError as exc:
            log.warning(
                "vault_sync_audit_write_failed",
                path=str(self._audit_log_path),
                error=repr(exc),
            )
