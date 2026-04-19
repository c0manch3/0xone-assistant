from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import signal
import sys
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

from assistant.adapters.dispatch_reply import _DedupLedger
from assistant.adapters.telegram import TelegramAdapter
from assistant.bridge.bootstrap import ensure_skills_symlink
from assistant.bridge.claude import ClaudeBridge
from assistant.config import Settings, get_settings
from assistant.handlers.message import ClaudeHandler
from assistant.logger import get_logger, setup_logging
from assistant.media.paths import ensure_media_dirs
from assistant.media.sweeper import media_sweeper_loop
from assistant.scheduler.dispatcher import ScheduledTrigger, SchedulerDispatcher
from assistant.scheduler.loop import SchedulerLoop
from assistant.scheduler.store import SchedulerStore
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore
from assistant.subagent.definitions import build_agents
from assistant.subagent.hooks import make_subagent_hooks
from assistant.subagent.picker import SubagentRequestPicker
from assistant.subagent.store import SubagentStore

# Phase 6 B-W2-2: loudly log SDK version drift. We keep the runtime-only
# `last_assistant_message` field as the primary result carrier; the JSONL
# fallback inside `subagent/hooks.py` covers a future SDK that drops it.
_EXPECTED_SDK_VERSION = "0.1.59"

AUTH_PREFLIGHT_FAIL_EXIT = 3

# Phase-3 TTLs (Q7).
_TMP_TTL_S = 3600  # 1 hour — aborted fetches
_INSTALLER_CACHE_TTL_S = 7 * 86400  # 7 days — preview -> install window
_BOOTSTRAP_TIMEOUT_S = 120.0
_BOOTSTRAP_NOTIFY_COOLDOWN_S = 7 * 86400  # re-notify if marker older than 7 d
_STOP_DRAIN_TIMEOUT_S = 5.0  # review fix #10


async def _preflight_claude_cli(log: structlog.stdlib.BoundLogger) -> None:
    """Fail-fast if the `claude` CLI is missing.

    `claude --version` is cheap (no model turn, no cost) -- it confirms the
    binary is on $PATH. Auth is *not* checked here because every available
    "real" probe (`claude --print ping`, etc.) costs a model call on every
    daemon restart. Authentication errors surface from the first user message
    via `ClaudeBridgeError` with a clear "auth/login" hint in the structured
    log, which is good enough for a single-user bot.

    Exit 3 signals "user action required" (install CLI).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.error(
            "claude_cli_missing",
            hint="Install Claude Code CLI and run `claude login`.",
        )
        sys.exit(AUTH_PREFLIGHT_FAIL_EXIT)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.error(
            "claude_cli_hanging",
            hint="`claude --version` did not respond within 10s.",
        )
        sys.exit(AUTH_PREFLIGHT_FAIL_EXIT)

    if proc.returncode != 0:
        err = (stderr_bytes or b"").decode("utf-8", "replace")
        log.error("claude_cli_failed", stderr=err[:500], rc=proc.returncode)
        sys.exit(AUTH_PREFLIGHT_FAIL_EXIT)

    version = (stdout_bytes or b"").decode("utf-8", "replace").strip()
    log.info("auth_preflight_ok", claude_version=version[:120])


class Daemon:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = get_logger("main")
        self._conn: aiosqlite.Connection | None = None
        self._adapter: TelegramAdapter | None = None
        # Fire-and-forget task handles. `add_done_callback(self._bg_tasks.discard)`
        # keeps refs alive (CPython GCs floating tasks without warnings) and
        # lets `Daemon.stop()` drain them.
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Phase 5 state — populated in start(), consumed in stop() and the
        # health-check background task.
        self._pid_fd: int | None = None
        self._scheduler_loop: SchedulerLoop | None = None
        self._scheduler_dispatcher: SchedulerDispatcher | None = None
        # Fix-pack CRITICAL #3: per-instance latch so the bypass=True
        # heartbeat notify fires at most once per stall episode. Resets
        # when both loop + dispatcher heartbeats become fresh again
        # (HIGH #7 extends the staleness check to both).
        self._heartbeat_notified: bool = False
        # Phase 6 state — populated in start().
        self._sub_store: SubagentStore | None = None
        self._subagent_picker: SubagentRequestPicker | None = None
        self._subagent_pending: set[asyncio.Task[Any]] = set()
        self._picker_bridge: ClaudeBridge | None = None
        # Phase 7 (commit 16) — shared dedup ledger for dispatch_reply.
        # The SAME instance is threaded into the main-turn bridge's
        # adapter path (phase-7 commit 17), the `SchedulerDispatcher`
        # and the `make_subagent_hooks` factory so invariant I-7.5
        # (at-most-once artefact send per `(resolved_path, chat_id)`
        # within the ledger TTL) holds across ALL three call-sites.
        self._dedup_ledger = _DedupLedger()
        # Phase 7 (commit 16) — sweeper lifecycle. `asyncio.Event` is
        # created in `__init__` (not `start`) so `Daemon.stop()` can
        # unconditionally call `.set()` without a None-check even if
        # start() was never invoked (test ergonomics).
        self._media_sweep_stop = asyncio.Event()

    # ------------------------------------------------------------------

    def _spawn_bg(self, coro: Coroutine[Any, Any, None], *, name: str) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _sweep_run_dirs(self) -> None:
        """Remove `run/tmp` entries >1h and `run/installer-cache` entries >7d.

        Fire-and-forget; never fails startup on IOError. Missing directories
        are a no-op (first-run case).
        """
        now = time.time()
        data_dir = self._settings.data_dir
        buckets = (
            (data_dir / "run" / "tmp", _TMP_TTL_S),
            (data_dir / "run" / "installer-cache", _INSTALLER_CACHE_TTL_S),
        )
        removed = 0
        for base, ttl in buckets:
            if not base.is_dir():
                continue
            for entry in base.iterdir():
                try:
                    age = now - entry.stat().st_mtime
                except OSError:
                    continue
                if age <= ttl:
                    continue
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                    removed += 1
                except OSError as exc:
                    self._log.warning(
                        "sweep_failed",
                        entry=str(entry),
                        error=repr(exc),
                    )
        if removed:
            self._log.info("sweep_run_dirs_done", removed=removed)

    async def _bootstrap_skill_creator_bg(self) -> None:
        """Install Anthropic's `skill-creator` from the marketplace on first run.

        Fire-and-forget: `Daemon.start()` creates this task with
        `asyncio.create_task` and returns without awaiting. The internal
        120-second timeout bounds the task itself; the main flow never
        waits on GitHub.

        Failure modes (all log-only; owner notified once via
        `_bootstrap_notify_failure` when a real failure happens — not on
        the predictable `skipped_no_gh` state):

        * skill already present              -> no-op.
        * `gh` not on PATH                   -> `skipped_no_gh` warning.
        * subprocess rc != 0 / timeout / exc -> corresponding warning +
                                                one-shot Telegram notice.
        """
        skill_dir = self._settings.project_root / "skills" / "skill-creator"
        if skill_dir.exists():
            return
        if shutil.which("gh") is None:
            self._log.warning("skill_creator_bootstrap_skipped_no_gh")
            return

        self._log.info("skill_creator_bootstrap_starting")
        installer = self._settings.project_root / "tools" / "skill_installer" / "main.py"
        env = {**os.environ, "ASSISTANT_BOOTSTRAP": "1"}
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(installer),
                "marketplace",
                "install",
                "skill-creator",
                "--confirm",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            self._log.warning("skill_creator_bootstrap_exception", error=repr(exc))
            await self._bootstrap_notify_failure(rc=-1, reason="exception")
            return

        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=_BOOTSTRAP_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            self._log.warning(
                "skill_creator_bootstrap_timeout",
                timeout_s=_BOOTSTRAP_TIMEOUT_S,
            )
            await self._bootstrap_notify_failure(rc=-2, reason="timeout")
            return

        stderr_bytes = b""
        if proc.stderr is not None:
            try:
                stderr_bytes = await proc.stderr.read()
            except Exception:  # pragma: no cover -- pipe already drained
                stderr_bytes = b""
        if rc != 0:
            self._log.warning(
                "skill_creator_bootstrap_failed",
                rc=rc,
                stderr=stderr_bytes.decode("utf-8", "replace")[:500],
            )
            await self._bootstrap_notify_failure(rc=rc, reason="failed")
            return
        # Success — drop the stale notification marker so a future regression
        # is allowed to notify again (review fix #11 auto-reset semantics).
        self._bootstrap_clear_notify_marker()
        self._log.info("skill_creator_bootstrap_ok")

    # ------------------------------------------------------------------
    # Review fix #7 + #11: the marker stores `{"rc", "reason", "ts"}` so we
    # can decide whether to re-notify. Creation uses `O_CREAT | O_EXCL` to
    # close a parallel-start race where two daemons would both find the
    # marker absent and both send the Telegram message.

    def _bootstrap_marker_path(self) -> Path:
        return self._settings.data_dir / "run" / ".bootstrap_notified"

    def _bootstrap_clear_notify_marker(self) -> None:
        marker = self._bootstrap_marker_path()
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover -- best-effort cleanup
            self._log.warning("bootstrap_marker_clear_failed", error=repr(exc))

    def _should_notify_bootstrap(self, rc: int) -> bool:
        """Decide whether a fresh Telegram notification should go out.

        Returns True and unlinks the existing marker (so the subsequent
        O_EXCL create succeeds) when the marker is stale:
          * absent                       — first failure,
          * older than _BOOTSTRAP_NOTIFY_COOLDOWN_S (7 d), OR
          * records a different rc        — regression / new condition.
        Returns False when the marker is fresh *and* the rc matches — a
        predictable repeat that we already told the operator about.

        The race guard stays honest: a parallel daemon between our unlink
        and our caller's `O_EXCL` will cause `FileExistsError` there,
        which the caller swallows (another process already notified).
        """
        marker = self._bootstrap_marker_path()
        if not marker.exists():
            return True
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            last_ts = float(data.get("ts_epoch", 0.0))
            last_rc_raw = data.get("rc")
            last_rc = int(last_rc_raw) if last_rc_raw is not None else None
        except (OSError, ValueError, TypeError):
            # Corrupt marker: drop it so we can rewrite cleanly.
            marker.unlink(missing_ok=True)
            return True
        stale = time.time() - last_ts > _BOOTSTRAP_NOTIFY_COOLDOWN_S or last_rc != rc
        if stale:
            # Make way for the O_EXCL create in _bootstrap_notify_failure.
            marker.unlink(missing_ok=True)
            return True
        return False

    async def _bootstrap_notify_failure(self, *, rc: int, reason: str) -> None:
        """Send a Telegram message to the owner on bootstrap failure.

        Gated by a JSON marker that records `rc`/`reason`/`ts_epoch` so:
        * Parallel daemons can't both send (O_EXCL on marker creation).
        * Persistent failure doesn't re-spam on every restart.
        * A rc-change OR a >7d-old marker unlocks a new notification.
        """
        if self._adapter is None:
            return
        if not self._should_notify_bootstrap(rc):
            return
        marker = self._bootstrap_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                str(marker),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_TRUNC,
                0o644,
            )
        except FileExistsError:
            # Another process beat us to the marker in the split second
            # between `_should_notify_bootstrap` and `os.open`. Honour
            # its decision — don't double-notify.
            return
        payload = {
            "rc": rc,
            "reason": reason,
            "ts_epoch": time.time(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            os.write(fd, json.dumps(payload).encode("utf-8"))
        finally:
            os.close(fd)

        msg = (
            "Автобутстрап skill-creator не удался — marketplace-установка "
            f"временно недоступна (rc={rc}, reason={reason}). "
            "Проверь `gh auth status` и логи."
        )
        try:
            await self._adapter.send_text(self._settings.owner_chat_id, msg)
        except Exception as exc:
            # Marker already written: we'd retry on the next rc-change or
            # after the cooldown expires. Either is acceptable.
            self._log.warning("bootstrap_notify_failed", error=repr(exc))

    # ------------------------------------------------------------------

    def _ensure_vault(self) -> None:
        """Create the vault root (0o700) and log a warning if it is loose.

        The memory CLI also does this on every invocation, but doing it here
        at daemon startup (a) makes the `{vault_dir}` reference in the
        system prompt point to something that exists, and (b) surfaces the
        advisory-flock failure as a Telegram notice (future phase 5) rather
        than hiding it behind a single CLI error.

        We do NOT trigger the S3 lock probe here (it's CLI-local). An
        advisory-flock filesystem will surface via `exit 5` on the first
        `memory write`, which the model is instructed to relay to the owner
        in SKILL.md.
        """
        vault_dir = self._settings.vault_dir
        try:
            vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            (vault_dir / ".tmp").mkdir(exist_ok=True, mode=0o700)
        except OSError as exc:
            self._log.warning("vault_init_failed", error=repr(exc), path=str(vault_dir))
            return
        try:
            mode = vault_dir.stat().st_mode & 0o777
        except OSError:
            return
        if mode & 0o077:
            self._log.warning(
                "vault_dir_permissions_too_open",
                path=str(vault_dir),
                mode=oct(mode),
            )

        # B-CRIT-1 (review wave 3): `memory write --body-file` reads staged
        # body files inside `<project_root>/data/run/memory-stage/`. Pre-
        # create the dir at 0o700 so the model's first Write tool call has
        # a valid landing spot. The stage lives under project_root (inside
        # the phase-2 file-hook allowlist), intentionally not under
        # `<data_dir>/` which is OUTSIDE the project.
        stage_dir = self._settings.project_root / "data" / "run" / "memory-stage"
        try:
            stage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            self._log.warning(
                "memory_stage_init_failed",
                error=repr(exc),
                path=str(stage_dir),
            )

    def _acquire_pid_lock_or_exit(self) -> None:
        """Advisory flock on `<data_dir>/run/daemon.pid` (plan §1.13 / spike S-4).

        On `BlockingIOError` (another daemon holds the lock) we log
        `daemon_already_running` and `sys.exit(0)` — phase-5 B1 blocker
        fix: guarantees single-daemon for the rest of `start()` so
        `sweep_pending` and `clean_slate_sent` can't compete with another
        process's in-flight scheduler turn.

        fd kept on `self._pid_fd` until process exit. Mode 0o600 per
        wave-2 N-W2-5 — the file contains only `${pid}\\n` today but
        staying restrictive costs nothing and defends against curious
        file-watchers.
        """
        pid_dir = self._settings.data_dir / "run"
        pid_dir.mkdir(parents=True, exist_ok=True)
        pid_path = pid_dir / "daemon.pid"
        fd = os.open(str(pid_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._log.warning("daemon_already_running", pid_path=str(pid_path))
            os.close(fd)
            sys.exit(0)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._pid_fd = fd

    async def _notify_with_marker(
        self,
        marker: Path,
        cooldown_s: int,
        msg: str,
        *,
        bypass: bool = False,
    ) -> None:
        """Marker-file-gated Telegram notice. Mirrors `_bootstrap_notify_failure`
        structurally; separated because scheduler failures have their own
        wave-2 N-W2-4 cooldowns (loop-crash vs catchup-recap).

        `bypass=True` skips the cooldown — used by the heartbeat
        health-check (wave-2 G-W2-10) to surface silent loop death
        immediately, since a loop that dies and the notify is on cooldown
        would otherwise leave the operator completely in the dark.
        """
        if self._adapter is None:
            return
        if not bypass and marker.exists():
            try:
                age = time.time() - marker.stat().st_mtime
                if age < cooldown_s:
                    return
            except OSError:
                pass
        # Fix-pack HIGH #6: reserve the cooldown BEFORE the network call.
        # If send_text fails (timeout, flood-wait, crash mid-send), the
        # marker is already written — a quick restart won't re-fire the
        # same "пропущено N напоминаний" recap. A delivery failure from
        # the cooldown's perspective is indistinguishable from a
        # successful delivery that the operator happened to miss, and
        # both are better-handled by the periodic resend pathway.
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            self._log.warning("scheduler_marker_touch_failed", exc_info=True)
        try:
            await self._adapter.send_text(self._settings.owner_chat_id, msg)
        except Exception:
            self._log.warning("scheduler_notify_failed", exc_info=True)

    async def _scheduler_loop_notify(self, msg: str) -> None:
        marker = self._settings.data_dir / "run" / ".scheduler_loop_notified"
        await self._notify_with_marker(
            marker,
            cooldown_s=self._settings.scheduler.loop_crash_cooldown_s,
            msg=msg,
        )

    async def _scheduler_dispatcher_notify(self, msg: str) -> None:
        """Fix-pack HIGH #4: sibling marker for dispatcher fatal crashes.

        We keep the marker distinct from the loop notify so a repeated
        dispatcher crash cooldown doesn't mask a fresh loop crash (and
        vice-versa). Both share the same cooldown setting.
        """
        marker = self._settings.data_dir / "run" / ".scheduler_dispatcher_notified"
        await self._notify_with_marker(
            marker,
            cooldown_s=self._settings.scheduler.loop_crash_cooldown_s,
            msg=msg,
        )

    async def _scheduler_catchup_recap(self, missed: int) -> None:
        """One Russian-language Telegram message (GAP #16). Fires once at
        boot if `count_catchup_misses` sums to > 0."""
        marker = self._settings.data_dir / "run" / ".scheduler_catchup_recap"
        await self._notify_with_marker(
            marker,
            cooldown_s=self._settings.scheduler.catchup_recap_cooldown_s,
            msg=f"пока система спала, пропущено {missed} напоминаний.",
        )

    async def _scheduler_health_check_bg(self) -> None:
        """Heartbeat watchdog (wave-2 G-W2-10 + fix-pack CRITICAL #3/#4/#5,
        HIGH #7).

        Checks every `check_interval_s` (60 s) that BOTH
        `SchedulerLoop.last_tick_at()` AND
        `SchedulerDispatcher.last_tick_at()` are fresher than
        `tick_interval_s * heartbeat_stale_multiplier` (150 s default).
        If EITHER is stale we assume a silent death and send a
        BYPASS-cooldown Telegram notice — the owner must know *now*,
        not 24 h later.

        Fix-pack CRITICAL #3 latch: `bypass=True` defeats the marker
        cooldown, so we track `_heartbeat_notified` per Daemon instance
        and send at most ONE notify per stall episode. The latch resets
        when both heartbeats catch up past the previous stale window,
        allowing a future stall to re-notify.
        """
        check_interval_s = 60.0
        stale_mult = self._settings.scheduler.heartbeat_stale_multiplier
        tick_s = self._settings.scheduler.tick_interval_s
        stale_threshold_s = tick_s * stale_mult

        marker = self._settings.data_dir / "run" / ".scheduler_loop_notified"
        while True:
            try:
                # Cooperative pacing: use the public `stop_event()`
                # accessor (fix-pack CRITICAL #5) instead of `_stop`.
                if self._scheduler_loop is not None:
                    try:
                        await asyncio.wait_for(
                            self._scheduler_loop.stop_event().wait(),
                            timeout=check_interval_s,
                        )
                    except TimeoutError:
                        pass
                    else:
                        return
                else:
                    await asyncio.sleep(check_interval_s)
                if self._scheduler_loop is None:
                    continue
                now_loop = asyncio.get_running_loop().time()
                loop_last = self._scheduler_loop.last_tick_at()
                disp_last = (
                    self._scheduler_dispatcher.last_tick_at()
                    if self._scheduler_dispatcher is not None
                    else 0.0
                )
                # Boot window — ignore zeros (tasks haven't ticked yet).
                if loop_last == 0.0:
                    continue
                loop_age = now_loop - loop_last
                # Consider dispatcher stale only if it has ticked at least
                # once; a freshly-booted dispatcher may legitimately not
                # have drained anything yet.
                disp_age = now_loop - disp_last if disp_last > 0.0 else 0.0
                stale = loop_age > stale_threshold_s or disp_age > stale_threshold_s

                if stale:
                    self._log.error(
                        "scheduler_heartbeat_stale",
                        loop_age_s=loop_age,
                        dispatcher_age_s=disp_age,
                        threshold_s=stale_threshold_s,
                    )
                    if not self._heartbeat_notified:
                        self._heartbeat_notified = True
                        await self._notify_with_marker(
                            marker,
                            cooldown_s=self._settings.scheduler.loop_crash_cooldown_s,
                            msg=(
                                f"scheduler loop heartbeat stale ({int(loop_age)}s since last tick)"
                            ),
                            bypass=True,
                        )
                    else:
                        self._log.info(
                            "scheduler_heartbeat_still_stale",
                            loop_age_s=loop_age,
                            dispatcher_age_s=disp_age,
                        )
                else:
                    # Latch reset: both heartbeats are fresh. The `not
                    # stale` branch already proves `loop_age <= threshold`
                    # AND (disp_age <= threshold OR disp_last == 0), so
                    # the producer/consumer has made progress since the
                    # stall was detected. Reset the latch so a future
                    # stall re-notifies.
                    if self._heartbeat_notified:
                        self._heartbeat_notified = False
                        self._log.info("scheduler_heartbeat_recovered")
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.warning("scheduler_health_check_failed", exc_info=True)

    async def start(self) -> None:
        # Phase 5 B1: pidfile mutex FIRST — before any DB operation.
        # Second daemon exits 0 here and never runs `sweep_pending` /
        # `clean_slate_sent`, so in-flight turns from the first daemon
        # stay intact.
        self._acquire_pid_lock_or_exit()

        await _preflight_claude_cli(self._log)
        ensure_skills_symlink(self._settings.project_root)

        self._settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        (self._settings.data_dir / "run").mkdir(parents=True, exist_ok=True)
        self._ensure_vault()

        # Phase 7 (commit 16) pitfall #14: create the media layout
        # BEFORE ANY background task is spawned. `media_sweeper_loop`
        # scans `<data_dir>/media/{inbox,outbox}` on its first tick;
        # if the dirs don't exist yet, the first sweep logs a spurious
        # `FileNotFoundError` (harmless but noisy) and — worse — the
        # adapter download path would hit `OSError: ENOENT` on the
        # first Telegram file. Ordering is authoritative here: this
        # call MUST precede every `_spawn_bg` below.
        await ensure_media_dirs(self._settings.data_dir)

        self._conn = await connect(self._settings.db_path)
        await apply_schema(self._conn)

        conv = ConversationStore(self._conn)
        turns = TurnStore(self._conn, lock=conv.lock)
        # Wave-2 N-W2-1 verified: ConversationStore.lock property exists
        # (state/conversations.py:25-27). Scheduler shares the same writer
        # lock — spike S-1 confirmed p99 is 3.4 ms.
        sched_store = SchedulerStore(self._conn, lock=conv.lock)

        # A previous process may have crashed mid-turn; promote any orphan
        # 'pending' rows to 'interrupted' so they're excluded from history.
        swept = await turns.sweep_pending()
        if swept:
            self._log.warning("startup_swept_pending_turns", count=swept)

        # Plan §8.2: BEFORE the dispatcher starts accepting, revert every
        # `status='sent'` trigger to `pending`. _inflight is empty at this
        # point, so any 'sent' row is provably orphaned from a prior crash.
        reverted = await sched_store.clean_slate_sent()
        if reverted:
            self._log.info("scheduler_clean_slate_revert", count=reverted)

        if shutil.which("gh") is None:
            self._log.warning(
                "gh_cli_not_found",
                hint=("marketplace features disabled. Install https://cli.github.com/ to enable."),
            )

        # Phase 6 wiring -------------------------------------------------
        # B-W2-2: loud log-only warning on SDK version drift. We do NOT
        # crash — the JSONL fallback inside on_subagent_stop handles a
        # future SDK that changes `last_assistant_message`.
        try:
            import claude_agent_sdk as _sdk

            sdk_version = getattr(_sdk, "__version__", None)
        except Exception:  # pragma: no cover — SDK is a hard dep
            sdk_version = None
        if sdk_version and sdk_version != _EXPECTED_SDK_VERSION:
            self._log.warning(
                "sdk_version_drift_phase6",
                expected=_EXPECTED_SDK_VERSION,
                seen=sdk_version,
                note=(
                    "SubagentStop hook relies on the runtime "
                    "'last_assistant_message' field which is not in the SDK "
                    "TypedDict. If this version drops or changes it, the "
                    "JSONL fallback in subagent/hooks.py picks up."
                ),
            )

        # Subagent ledger — shares ConversationStore.lock (pitfall #8).
        self._sub_store = SubagentStore(self._conn, lock=conv.lock)
        recovered = await self._sub_store.recover_orphans(
            stale_requested_after_s=self._settings.subagent.requested_stale_after_s
        )
        total_recovered = recovered.get("interrupted", 0) + recovered.get("dropped", 0)
        if total_recovered:
            self._log.warning("subagent_orphans_recovered", **recovered)

        # Adapter MUST be built BEFORE the hook factory — the hooks
        # close over it to deliver the notify. Build adapter first, then
        # hooks, then both bridges, then register the handler.
        #
        # Phase 7 fix-pack C1: the adapter receives the SAME
        # `_dedup_ledger` the scheduler dispatcher and subagent Stop
        # hook already share. This closes the I-7.5 gap where main-turn
        # replies emitting an outbox artefact path were sent as raw
        # text — they now route through `dispatch_reply`, deliver
        # photo/document/audio, strip the path token from the cleaned
        # text, and participate in the shared at-most-once key.
        self._adapter = TelegramAdapter(
            self._settings,
            dedup_ledger=self._dedup_ledger,
        )

        # B-W2-7: the split-notify message differentiates "prior daemon
        # crashed mid-subagent" from "pending CLI request sat past the
        # stale window".
        if total_recovered:
            msg_parts: list[str] = []
            if recovered.get("interrupted"):
                msg_parts.append(
                    f"{recovered['interrupted']} подагент(ов) помечено "
                    "interrupted (предыдущий daemon упал в процессе)"
                )
            if recovered.get("dropped"):
                msg_parts.append(
                    f"{recovered['dropped']} отложенных запросов "
                    "отброшено (CLI insert просидел >1ч без pickup)"
                )
            notify_body = "daemon restart: " + "; ".join(msg_parts)
            # Use the adapter directly; bg task keeps start() non-blocking.
            self._spawn_bg(
                self._adapter.send_text(self._settings.owner_chat_id, notify_body),
                name="subagent_orphan_notify",
            )

        sub_hooks = make_subagent_hooks(
            store=self._sub_store,
            adapter=self._adapter,
            settings=self._settings,
            pending_updates=self._subagent_pending,
            dedup_ledger=self._dedup_ledger,
        )
        sub_agents = build_agents(self._settings)

        # B-W2-6: two bridges share hook factory + agents but have
        # INDEPENDENT Semaphores so picker flood cannot starve user turns.
        bridge = ClaudeBridge(
            self._settings,
            extra_hooks=sub_hooks,
            agents=sub_agents,
        )
        self._picker_bridge = ClaudeBridge(
            self._settings,
            extra_hooks=sub_hooks,
            agents=sub_agents,
        )

        # Adapter was built above (phase 6: hooks need it). Just wire
        # the handler and start.
        handler = ClaudeHandler(self._settings, conv, turns, bridge)
        self._adapter.set_handler(handler)
        await self._adapter.start()

        # Scheduler wiring (plan §8.1). Loop and dispatcher share a bounded
        # in-process queue; blocking put() gives natural backpressure.
        queue: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(
            maxsize=self._settings.scheduler.dispatcher_queue_size
        )
        dispatcher = SchedulerDispatcher(
            queue=queue,
            store=sched_store,
            handler=handler,
            adapter=self._adapter,
            owner_chat_id=self._settings.owner_chat_id,
            settings=self._settings,
            notify_fn=self._scheduler_dispatcher_notify,
            dedup_ledger=self._dedup_ledger,
        )
        loop_ = SchedulerLoop(
            queue=queue,
            store=sched_store,
            dispatcher=dispatcher,
            settings=self._settings,
            notify_fn=self._scheduler_loop_notify,
        )
        self._scheduler_dispatcher = dispatcher
        self._scheduler_loop = loop_
        self._spawn_bg(dispatcher.run(), name="scheduler_dispatcher")
        self._spawn_bg(loop_.run(), name="scheduler_loop")
        # Wave-2 G-W2-10: heartbeat watchdog.
        self._spawn_bg(self._scheduler_health_check_bg(), name="scheduler_health")

        # Phase 6 picker. Uses the dedicated `self._picker_bridge` so a
        # picker flood cannot starve user-chat turns (B-W2-6).
        if self._settings.subagent.enabled:
            assert self._picker_bridge is not None
            self._subagent_picker = SubagentRequestPicker(
                self._sub_store,
                self._picker_bridge,
                settings=self._settings,
            )
            self._spawn_bg(self._subagent_picker.run(), name="subagent_picker")

        # Fire-and-forget housekeeping. `Daemon.start()` must not wait on
        # GitHub or `uv sync`; bootstrap has its own 120 s timeout internally.
        self._spawn_bg(self._sweep_run_dirs(), name="sweep_run_dirs")
        self._spawn_bg(self._bootstrap_skill_creator_bg(), name="skill_creator_bootstrap")

        # Phase 7 (commit 16) media-retention sweeper. Pitfall #14:
        # `ensure_media_dirs()` above created `inbox/` and `outbox/`
        # so the first `media_sweeper_loop` tick scans existing dirs.
        # The loop honours `self._media_sweep_stop` for cooperative
        # shutdown; `Daemon.stop()` sets the event before the bg-task
        # drain so the loop exits cleanly instead of being cancelled
        # mid-unlink.
        self._spawn_bg(
            media_sweeper_loop(
                self._settings.data_dir,
                self._settings,
                self._media_sweep_stop,
                self._log,
            ),
            name="media_sweeper_loop",
        )

        # GAP #16 / wave-2 G-W2-4: one-shot "missed N reminders" recap.
        # Counted BEFORE the first tick so the recap number is stable
        # (subsequent ticks may immediately materialise fires inside the
        # catchup window, which is a separate concern).
        try:
            missed = await loop_.count_catchup_misses()
        except Exception:
            self._log.warning("scheduler_catchup_count_failed", exc_info=True)
            missed = 0
        if missed > 0:
            self._spawn_bg(
                self._scheduler_catchup_recap(missed),
                name="scheduler_catchup_recap",
            )

        self._log.info("daemon_started", owner=self._settings.owner_chat_id)

    async def stop(self) -> None:
        """Shutdown sequence (wave-2 B-W2-2 corrected order).

        1. Signal scheduler loop + dispatcher (non-blocking Event.set()) so
           the drain in step 2 can progress.
        2. Drain bg-tasks. In-flight scheduler delivery (up to and including
           the final `adapter.send_text`) completes HERE. Cancelled tasks
           hit the dispatcher's shielded `mark_pending_retry` branch
           (wave-2 B-W2-3) so the DB retry ledger stays honest.
        3. Stop adapter — MUST run AFTER bg-tasks drain. Stopping earlier
           would close the aiogram session while the dispatcher is still
           trying to send, burning a Claude turn on the next boot's
           re-materialisation.
        4. Close DB.
        5. Release pidfile flock.
        """
        self._log.info("daemon_stopping")

        # Step 1.
        if self._scheduler_loop is not None:
            try:
                self._scheduler_loop.stop()
            except Exception:
                self._log.warning("stop_step_failed", step="scheduler_loop", exc_info=True)
        if self._scheduler_dispatcher is not None:
            try:
                self._scheduler_dispatcher.stop()
            except Exception:
                self._log.warning("stop_step_failed", step="scheduler_dispatcher", exc_info=True)
        # Phase 6: signal picker to stop BEFORE bg drain so no new
        # dispatches land mid-drain.
        if self._subagent_picker is not None:
            try:
                self._subagent_picker.request_stop()
            except Exception:
                self._log.warning("stop_step_failed", step="subagent_picker", exc_info=True)
        # Phase 7 (commit 16): signal media sweeper to exit at the next
        # `asyncio.wait_for(stop_event.wait(), timeout=interval_s)`
        # wake-up. Must precede the bg-drain in step 2 so the loop is
        # already draining when `gather(...)` awaits its task.
        self._media_sweep_stop.set()

        # Step 2.
        if self._bg_tasks:
            self._log.info("daemon_draining_bg_tasks", count=len(self._bg_tasks))
            pending = list(self._bg_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=_STOP_DRAIN_TIMEOUT_S,
                )
            except TimeoutError:
                self._log.warning(
                    "daemon_bg_drain_timeout",
                    count=len([t for t in pending if not t.done()]),
                )
                for t in pending:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        # Step 2.5 — fix-pack HIGH #5: drain the dispatcher's shielded
        # `mark_pending_retry` tasks BEFORE closing the DB connection.
        # A shielded coroutine survives its owner task's cancellation
        # but the outer awaiter gets CancelledError; without this
        # drain those shields could still be executing their UPDATE
        # when `conn.close()` runs below, racing a `ProgrammingError:
        # Cannot operate on a closed database`.
        if self._scheduler_dispatcher is not None:
            updates = list(self._scheduler_dispatcher.pending_updates())
            if updates:
                self._log.info("daemon_draining_shield_updates", count=len(updates))
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*updates, return_exceptions=True),
                        timeout=2.0,
                    )
                except TimeoutError:
                    self._log.warning(
                        "daemon_shield_drain_timeout",
                        count=len([t for t in updates if not t.done()]),
                    )

        # Step 2.57 — phase-6 fix-pack C-3 (devil C-3 / CR I-1):
        # drain the picker's in-flight `_dispatch_one` tasks BEFORE
        # the subagent-notify drain AND the DB close. The picker
        # spawns these via `asyncio.create_task(self._dispatch_one(job))`
        # and they outlive `picker.run()` — a bare `_bg_tasks` drain
        # only closes `picker.run()` itself, leaving `_dispatch_one`
        # coroutines racing `conn.close()` (ProgrammingError:
        # Cannot operate on a closed database) and
        # `adapter.stop()` (notify drops because adapter gone).
        #
        # We use a short-ish 5 s timeout — each dispatch is an SDK
        # turn that can legitimately take that long mid-message; on
        # timeout we cancel the stragglers and a final gather lets
        # their CancelledError propagate cleanly.
        if self._subagent_picker is not None:
            dispatches = list(self._subagent_picker.dispatch_tasks())
            if dispatches:
                self._log.info("daemon_draining_picker_dispatches", count=len(dispatches))
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*dispatches, return_exceptions=True),
                        timeout=_STOP_DRAIN_TIMEOUT_S,
                    )
                except TimeoutError:
                    stuck = [t for t in dispatches if not t.done()]
                    self._log.warning(
                        "daemon_picker_dispatch_drain_timeout",
                        count=len(stuck),
                    )
                    for t in stuck:
                        t.cancel()
                    await asyncio.gather(*dispatches, return_exceptions=True)

        # Step 2.6 — phase 6 (GAP #12): drain subagent notify tasks. The
        # Stop hook register shielded `adapter.send_text` tasks onto
        # `_subagent_pending`; we MUST finish them before `adapter.stop()`
        # closes the aiogram session.
        if self._subagent_pending:
            sub_updates = list(self._subagent_pending)
            self._log.info("daemon_draining_subagent_notifies", count=len(sub_updates))
            try:
                await asyncio.wait_for(
                    asyncio.gather(*sub_updates, return_exceptions=True),
                    timeout=self._settings.subagent.drain_timeout_s,
                )
            except TimeoutError:
                self._log.warning(
                    "daemon_subagent_drain_timeout",
                    count=len([t for t in sub_updates if not t.done()]),
                )

        # Step 2.7 — GAP #13: warn-only ps-sweep for orphan `claude` CLI
        # subprocesses from a killed subagent. We do NOT kill; the
        # operator reads the log and decides. Gated on the picker
        # having actually run — no picker → no subagent subprocesses,
        # so no orphan risk worth scanning for. Skipping in this branch
        # also keeps the legacy bootstrap tests (which monkeypatch
        # `create_subprocess_exec` globally with a fixed-count
        # iterator) honest. Detection-only; uses
        # `asyncio.create_subprocess_exec` so `stop()` stays fully
        # cooperative (ASYNC221). Broad except: a failure here must
        # NEVER propagate into stop()'s critical cleanup order
        # (DB close, pidfile release).
        if self._subagent_picker is not None:
            try:
                ps_proc = await asyncio.create_subprocess_exec(
                    "ps",
                    "-Ao",
                    "pid,command",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    ps_out, _ = await asyncio.wait_for(ps_proc.communicate(), timeout=2.0)
                except TimeoutError:
                    ps_proc.kill()
                    await ps_proc.wait()
                    ps_out = b""
                ps_text = ps_out.decode("utf-8", errors="replace")
                my_pid = str(os.getpid())
                claude_lines = [
                    line
                    for line in ps_text.splitlines()
                    if "claude" in line and "grep" not in line and my_pid not in line
                ]
                if claude_lines:
                    self._log.warning(
                        "phase6_possible_orphan_claude_processes",
                        count=len(claude_lines),
                        sample=claude_lines[:3],
                    )
            except Exception:
                self._log.debug("phase6_ps_sweep_skipped", exc_info=True)

        # Step 3.
        if self._adapter is not None:
            try:
                await self._adapter.stop()
            except Exception:
                self._log.warning("stop_step_failed", step="adapter", exc_info=True)

        # Step 4.
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                self._log.warning("stop_step_failed", step="db", exc_info=True)

        # Step 5.
        if self._pid_fd is not None:
            import contextlib

            with contextlib.suppress(OSError):
                os.close(self._pid_fd)
            self._pid_fd = None

        self._log.info("daemon_stopped")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    d = Daemon(settings)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    try:
        await d.start()
        await stop_event.wait()
    finally:
        await d.stop()
