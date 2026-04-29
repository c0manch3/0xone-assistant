from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import shutil as _shutil
import signal
import sqlite3
import sys
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import aiosqlite

from assistant.adapters.telegram import TelegramAdapter
from assistant.bridge.bootstrap import (
    assert_no_custom_claude_settings,
    ensure_skills_symlink,
)
from assistant.bridge.claude import ClaudeBridge
from assistant.config import Settings, get_settings
from assistant.handlers.message import ClaudeHandler
from assistant.logger import get_logger, setup_logging
from assistant.scheduler.dispatcher import SchedulerDispatcher
from assistant.scheduler.loop import (
    RealClock,
    ScheduledTrigger,
    SchedulerLoop,
)
from assistant.scheduler.store import (
    SchedulerStore,
    unlink_clean_exit_marker,
    write_clean_exit_marker,
)
from assistant.services.transcription import TranscriptionService
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.subagent import build_agents
from assistant.subagent.hooks import make_subagent_hooks
from assistant.subagent.picker import SubagentRequestPicker
from assistant.subagent.store import SubagentStore
from assistant.tools_sdk import _installer_core as _core
from assistant.tools_sdk import installer as _installer_mod
from assistant.tools_sdk import memory as _memory_mod
from assistant.tools_sdk import scheduler as _scheduler_mod
from assistant.tools_sdk import subagent as _subagent_mod

log = get_logger("main")

# Phase 6a: how old a file in ``.uploads/.failed/`` must be to be
# pruned at boot. 7 days lines up with the plan's quarantine retention
# (devil H4); a separate ``.failed/`` size-cap is deferred to phase 6e.
_QUARANTINE_RETENTION_S = 7 * 86400


def _boot_sweep_uploads(uploads_dir: Path) -> None:
    """Phase 6a: UNCONDITIONAL boot-sweep of orphaned tmp uploads.

    Every file directly under ``uploads_dir/`` at ``Daemon.start()`` is
    by definition stale — the previous daemon died, its in-flight
    uploads are orphaned. Wipe ALL top-level entries unconditionally;
    for ``.failed/`` quarantine entries, prune those older than 7 days.

    Devil H3: the prior plan used a 1h age-bound at boot; that opens a
    crash-loop disk-fill (a SIGKILL'd daemon restarting in <1h leaves
    the previous boot's tmp file untouched, then the next crash adds a
    new one, etc.). UNCONDITIONAL is the correct policy because there
    is no in-flight turn at process start.

    Errors during sweep are logged and swallowed — boot must succeed
    even if a single file's permissions are surprising. The bound on
    ``uploads_dir`` size is small enough (single-owner + 20 MB
    pre-download cap + 7d retention → ≤ ~1.4 GB worst case) that sync
    iteration in ``Daemon.start()`` is fast (<10 ms typical) and
    finishes before the adapter accepts traffic.
    """
    if not uploads_dir.exists():
        return
    now = time.time()
    try:
        entries = list(uploads_dir.iterdir())
    except OSError as exc:
        log.warning(
            "boot_sweep_iterdir_failed",
            path=str(uploads_dir),
            error=repr(exc),
        )
        return

    wiped_orphans = 0
    pruned_failed = 0
    for entry in entries:
        # ``.failed/`` is the quarantine subdir — keep it; prune by age.
        if entry.name == ".failed":
            if not entry.is_dir():
                # Defensive: a file at .failed/ would block quarantine
                # mkdir on the next ExtractionError. Log and skip; the
                # next mkdir(parents=True, exist_ok=True) raises
                # FileExistsError which the handler logs.
                log.warning(
                    "boot_sweep_failed_path_not_dir",
                    path=str(entry),
                )
                continue
            try:
                failed_entries = list(entry.iterdir())
            except OSError as exc:
                log.warning(
                    "boot_sweep_failed_iterdir_failed",
                    path=str(entry),
                    error=repr(exc),
                )
                continue
            for f in failed_entries:
                try:
                    if now - f.stat().st_mtime > _QUARANTINE_RETENTION_S:
                        f.unlink(missing_ok=True)
                        pruned_failed += 1
                except OSError as exc:
                    log.warning(
                        "boot_sweep_failed_prune_error",
                        path=str(f),
                        error=repr(exc),
                    )
            continue
        # Top-level entry: orphan tmp from a dead daemon. Wipe.
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink(missing_ok=True)
                wiped_orphans += 1
            elif entry.is_dir():
                # Future-proofing: phase 6b might land a peer subdir
                # under uploads_dir/. We don't recurse blindly; just
                # log and skip.
                log.info(
                    "boot_sweep_skipped_unexpected_dir",
                    path=str(entry),
                )
        except OSError as exc:
            log.warning(
                "boot_sweep_orphan_unlink_error",
                path=str(entry),
                error=repr(exc),
            )

    log.info(
        "boot_sweep_uploads_done",
        path=str(uploads_dir),
        wiped_orphans=wiped_orphans,
        pruned_failed=pruned_failed,
    )


def _acquire_singleton_lock(data_dir: Path) -> int:
    """Acquire an exclusive, non-blocking ``fcntl.flock`` on
    ``<data_dir>/.daemon.pid`` and return the open file descriptor.

    Fix 6 / H6-W3: prevents two daemon processes from sharing the same
    vault + index + ``assistant.db``. Advisory fcntl lock on a pidfile
    is the idiomatic POSIX answer — kernel releases the lock on process
    exit (even SIGKILL), so a stale pidfile from a crashed daemon never
    blocks the next restart.

    Raises :class:`SystemExit(3)` with a helpful hint on contention.
    Caller is responsible for closing the returned fd on shutdown.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".daemon.pid"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Read the PID stored by the holder for the hint; best-effort.
        try:
            with open(lock_path, encoding="utf-8") as fh:
                holder = fh.read().strip() or "unknown"
        except OSError:
            holder = "unknown"
        os.close(fd)
        log.error(
            "daemon_singleton_lock_held",
            hint=(
                "another 0xone-assistant daemon is already running "
                f"(pid={holder}). Stop it before starting a new instance."
            ),
        )
        sys.exit(3)
    # Write our PID so the next would-be starter can see who holds the
    # lock. Truncate to avoid stale trailing bytes from a prior run.
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
    except OSError as exc:
        log.warning("daemon_singleton_lock_pid_write_failed", error=repr(exc))
    return fd


class Daemon:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: aiosqlite.Connection | None = None
        self._adapter: TelegramAdapter | None = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._lock_fd: int | None = None
        # Phase 5: scheduler wiring. Instantiated in ``start()``; kept
        # as attributes so ``stop()`` can signal + join gracefully.
        self._sched_store: SchedulerStore | None = None
        self._sched_loop: SchedulerLoop | None = None
        self._sched_dispatcher: SchedulerDispatcher | None = None
        self._sched_queue: asyncio.Queue[ScheduledTrigger] | None = None
        # Phase 6: subagent infrastructure (post-wipe restoration).
        self._sub_store: SubagentStore | None = None
        self._sub_picker: SubagentRequestPicker | None = None
        self._sub_pending_updates: set[asyncio.Task[Any]] = set()
        # Phase 6e: bg audio dispatch state. ``_audio_persist_pending``
        # mirrors ``_sub_pending_updates`` — the bg ``finally`` schedules
        # the persist as a TRACKED task rather than ``asyncio.shield(...)``
        # (researcher RQ2 — orphan-task gotcha). ``Daemon.stop`` drains
        # this set BEFORE ``conn.close()`` with a bounded budget so a
        # fired-but-not-yet-flushed persist task can finish without
        # ``aiosqlite.ProgrammingError``.
        self._audio_persist_pending: set[asyncio.Task[Any]] = set()
        # Single semaphore shared across every audio bg task; enforces
        # the Mac whisper-server's hard ``Semaphore(1)`` from the client
        # side (CLOSED-NEGATIVE per researcher RQ3).
        self._audio_bg_sem: asyncio.Semaphore | None = None

    async def _preflight_claude_auth(self) -> None:
        """Fail-fast if the ``claude`` CLI is missing or unauthenticated.

        Runs once at startup before we accept any Telegram traffic. Without
        this, the first user message would spawn a subprocess that just
        prompts for ``claude login`` on stderr, and the owner would see a
        generic "sdk error" in Telegram with no context.

        Uses ``asyncio.create_subprocess_exec`` (direct argv, no shell),
        so no injection surface — ``claude``/``--print``/``ping`` are all
        static literals.
        """
        plog = get_logger("daemon.preflight")
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "--print",
                "ping",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _out, err = await asyncio.wait_for(proc.communicate(), timeout=45.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                plog.error(
                    "claude_cli_timeout",
                    hint="`claude --print ping` hung for 45s",
                )
                sys.exit(3)
        except FileNotFoundError:
            plog.error(
                "claude_cli_missing",
                hint="Install Claude Code CLI and run `claude login`.",
            )
            sys.exit(3)
        if proc.returncode != 0:
            tail = (err or b"").decode("utf-8", "replace").lower()
            if "auth" in tail or "login" in tail or "not authenticated" in tail:
                plog.error(
                    "claude_cli_not_authenticated",
                    hint="Run `claude login` before starting the bot.",
                )
            else:
                plog.error("claude_cli_failed", stderr=tail[:500])
            sys.exit(3)
        plog.info("auth_preflight_ok")

    async def start(self) -> None:
        # Fix 6 / H6-W3: acquire singleton lock BEFORE any daemon-wide
        # state is touched. Two daemons on the same data_dir would
        # double-write the audit log and race on the vault flock's
        # between-write windows.
        self._lock_fd = _acquire_singleton_lock(self._settings.data_dir)
        await self._preflight_claude_auth()
        # S15: refuse to start if .claude/settings*.json carries hooks/permissions.
        # We pass a plain stdlib logger because the helper is typed against
        # logging.Logger — structlog's BoundLogger is not a direct subtype.
        assert_no_custom_claude_settings(
            self._settings.project_root,
            logging.getLogger("bridge.bootstrap"),
        )
        ensure_skills_symlink(self._settings.project_root)

        # Phase 3: configure installer BEFORE ClaudeBridge is constructed —
        # the bridge imports ``INSTALLER_SERVER`` at module load; the @tool
        # handlers need ``project_root`` / ``data_dir`` resolved at call
        # time through the shared ``_CTX`` dict.
        _installer_mod.configure_installer(
            project_root=self._settings.project_root,
            data_dir=self._settings.data_dir,
        )
        # Phase 4: long-term memory subsystem. configure_memory is
        # idempotent on re-boot; it creates the vault dir + .tmp/
        # subdir, ensures the FTS5 index schema, runs Policy-B
        # auto-reindex (count OR max_mtime_ns mismatch), and never
        # blocks boot on a held lock (non-blocking acquisition, warn
        # on contention).
        #
        # Fix 8 / H5-W3: wrap with a helpful exit. If the owner's
        # vault dir is not writable (read-only FS, quota exceeded,
        # cloud-sync volume rejecting flock) or the index DB is
        # locked by another process that already leaked past the
        # singleton check, we surface a human-readable hint instead
        # of a raw traceback.
        try:
            _memory_mod.configure_memory(
                vault_dir=self._settings.vault_dir,
                index_db_path=self._settings.memory_index_path,
                max_body_bytes=self._settings.memory.max_body_bytes,
            )
        except (OSError, sqlite3.OperationalError) as exc:
            log.error(
                "memory_configure_failed",
                error=repr(exc),
                vault_dir=str(self._settings.vault_dir),
                index_db_path=str(self._settings.memory_index_path),
                hint=(
                    "vault_dir not writable or index_db_path locked? "
                    "Check MEMORY_VAULT_DIR / MEMORY_INDEX_DB_PATH env "
                    "overrides and filesystem permissions."
                ),
            )
            sys.exit(4)

        # Phase 6a: UNCONDITIONAL sweep of orphaned uploads + 7d
        # quarantine prune. Runs synchronously before the adapter
        # accepts traffic so the very first upload turn sees a clean
        # tmp dir. See ``_boot_sweep_uploads`` docstring for the
        # rationale (devil H3 — 1h-bound dropped).
        _boot_sweep_uploads(self._settings.uploads_dir)

        # Sweep stale tmp / cache dirs + crashed-install staging dirs.
        self._spawn_bg(_core.sweep_run_dirs(self._settings.data_dir))
        self._spawn_bg(_core.sweep_legacy_stage_dirs(self._settings.project_root))
        # Bootstrap Anthropic's skill-creator bundle on first boot.
        self._spawn_bg(self._bootstrap_skill_creator_bg())

        # Q2: ensure the XDG data dir + its parent exist before sqlite opens.
        self._settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await connect(self._settings.db_path)
        await apply_schema(self._conn)

        store = ConversationStore(self._conn)
        # S10: clean up any pending turns left behind by a prior crash.
        orphans = await store.cleanup_orphan_pending_turns()
        if orphans:
            log.info("orphan_turns_cleaned", count=orphans)

        # Phase 6: subagent ledger + orphan recovery BEFORE bridge
        # construction so the bridge sees the correct schema state and
        # the picker (started later) inherits a clean ledger.  Recovery
        # is a 3-branch UPDATE — research RQ4 / devil H-7.
        self._sub_store = SubagentStore(self._conn)
        recovery = await self._sub_store.recover_orphans(
            stale_requested_after_s=self._settings.subagent.orphan_stale_s
        )
        if recovery.total > 0:
            log.warning(
                "subagent_orphans_recovered",
                interrupted=recovery.interrupted,
                dropped_no_sdk=recovery.dropped_no_sdk,
                dropped_stale=recovery.dropped_stale,
            )

        # Phase 6: configure the @tool surface BEFORE bridge construction
        # — the bridge imports SUBAGENT_SERVER at module load and the
        # @tool handlers need ``store`` resolved at call time through the
        # shared ``_CTX`` dict (mirror of installer/memory/scheduler).
        _subagent_mod.configure_subagent(
            store=self._sub_store,
            owner_chat_id=self._settings.owner_chat_id,
            settings=self._settings,
        )

        # Phase 6c: transcription service is constructed unconditionally
        # but its ``enabled`` flag is False when no whisper URL/token
        # is configured. The handler routes audio turns through the
        # offline-reject path in that case.
        transcription = TranscriptionService(self._settings)

        # Phase 6: subagent hook factory + AgentDefinition registry.
        # Built BEFORE bridges so both user-chat and picker bridges
        # share the same ``extra_hooks`` dict object (Q6 PASS — single
        # SubagentStop callback fires regardless of which bridge spawned
        # the subagent).
        sub_agents: dict[str, Any] | None = None
        sub_hooks: dict[str, Any] = {}
        if self._settings.subagent.enabled:
            self._adapter = TelegramAdapter(self._settings)
            sub_agents = build_agents(self._settings)
            sub_hooks = make_subagent_hooks(
                store=self._sub_store,
                adapter=self._adapter,
                settings=self._settings,
                pending_updates=self._sub_pending_updates,
            )
        else:
            self._adapter = TelegramAdapter(self._settings)

        bridge = ClaudeBridge(
            self._settings,
            extra_hooks=sub_hooks or None,
            agents=sub_agents,
        )

        # Phase 6e (Alt-C): SEPARATE audio bridge. ``max_concurrent``
        # comes from ``settings.claude.audio_max_concurrent`` (default
        # 1) — the Mac whisper-server hard-caps to 1 concurrent
        # request, so client-side parallelism above 1 is pointless and
        # the lower cap also keeps the SDK subprocess footprint bounded
        # (worst-case 5 procs @ ~150 MB = ~750 MB RSS, well under the
        # 1500m container cap). User + picker bridges are unaffected;
        # they share the user-text ``claude.max_concurrent``.
        # ``agents=None`` because audio jobs do not spawn subagents;
        # ``extra_hooks`` mirrors the user bridge so SubagentStop
        # would still fire correctly if the audio path ever delegated
        # (defensive — costs nothing).
        audio_bridge = ClaudeBridge(
            self._settings,
            extra_hooks=sub_hooks or None,
            agents=None,
            max_concurrent_override=self._settings.claude.audio_max_concurrent,
        )
        self._audio_bg_sem = asyncio.Semaphore(
            self._settings.claude.audio_max_concurrent
        )

        handler = ClaudeHandler(
            self._settings,
            store,
            bridge,
            transcription=transcription,
            audio_bridge=audio_bridge,
            audio_bg_sem=self._audio_bg_sem,
            audio_dispatch=self.spawn_audio_task,
            audio_persist_pending=self._audio_persist_pending,
        )
        self._adapter.set_handler(handler)
        self._adapter.set_transcription(transcription)

        # Phase 6 (research RQ2 + B-W2-6): the picker bridge is a
        # SEPARATE ClaudeBridge instance with its own asyncio.Semaphore.
        # A long picker dispatch holding its slot for 15 minutes cannot
        # starve owner turns running through the user-chat bridge. Both
        # bridges share the same ``sub_hooks`` object so SubagentStop
        # fires through the shared ledger regardless of origin.
        if self._settings.subagent.enabled:
            picker_bridge = ClaudeBridge(
                self._settings,
                extra_hooks=sub_hooks or None,
                agents=sub_agents,
            )
            self._sub_picker = SubagentRequestPicker(
                self._sub_store,
                picker_bridge,
                settings=self._settings,
                # Fix-pack F1: terminal ``'error'`` transitions notify
                # the owner via Telegram. Adapter is already constructed
                # at this point so handing it through is safe.
                adapter=self._adapter,
            )

        # --------------------------------------------------------------
        # Phase 5: scheduler subsystem
        #
        # Order matters: we need to classify the boot BEFORE the loop
        # starts producing triggers, clean-slate any orphan ``sent``
        # rows, and decide whether the catchup recap should fire. The
        # supervised spawn runs after ``adapter.start()`` so the adapter
        # is ready when the very first scheduled trigger lands.
        # --------------------------------------------------------------
        sched_enabled = self._settings.scheduler.enabled
        catchup_missed = 0
        boot_class: str = "unknown"
        if sched_enabled:
            self._sched_store = SchedulerStore(self._conn)
            _scheduler_mod.configure_scheduler(
                data_dir=self._settings.data_dir,
                owner_chat_id=self._settings.owner_chat_id,
                settings=self._settings.scheduler,
                store=self._sched_store,
            )
            marker_path = self._settings.data_dir / ".last_clean_exit"
            boot_class = await self._sched_store.classify_boot(
                marker_path=marker_path,
                max_age_s=self._settings.scheduler.clean_exit_window_s,
            )
            # M2.7: remove the marker AFTER classification so a restart
            # 10 minutes later is still classified as suspend-or-crash
            # rather than spuriously-clean.
            unlink_clean_exit_marker(marker_path)
            log.info(
                "boot_classified",
                cls=boot_class,
                marker=str(marker_path),
            )
            reverted = await self._sched_store.clean_slate_sent()
            if reverted:
                log.info("orphan_sent_reverted", count=reverted)
            if boot_class != "clean-deploy":
                catchup_missed = (
                    await self._sched_store.count_catchup_misses(
                        catchup_window_s=(
                            self._settings.scheduler.catchup_window_s
                        ),
                    )
                )

        await self._adapter.start()
        log.info("daemon_started", owner=self._settings.owner_chat_id)

        # Phase 6e fix-pack-2 (DevOps CRIT-3): periodic RSS sampler.
        # Spawned AFTER ``adapter.start()`` so the daemon is fully up
        # before the first sample fires. Anchored in ``_bg_tasks`` so
        # ``Daemon.stop`` cancels it during shutdown (the inner
        # ``asyncio.sleep`` is cancel-safe). On macOS dev hosts the
        # observer exits silently after the first ``FileNotFoundError``.
        self._spawn_bg(
            self._rss_observer(
                interval_s=self._settings.observability.rss_interval_s,
            )
        )

        # Phase 6e (MED-5): notify the owner if the boot reaper marked
        # any pending turns as interrupted. ``cleanup_orphan_pending_turns``
        # is INDISCRIMINATE — it covers text, photo, audio, file turns
        # — so the wording stays generic. Sent BEFORE the subagent
        # orphan notify so a daemon that crashed mid-bg-audio gets the
        # owner's attention right away.
        if orphans > 0 and self._adapter is not None:
            self._spawn_bg(
                self._adapter.send_text(
                    self._settings.owner_chat_id,
                    f"⚠️ daemon перезапущен: {orphans} turn(s) прерван(ы). "
                    "Если ждал результат — повтори запрос.",
                )
            )

        # Phase 6: notify the owner of any subagent orphans recovered on
        # this boot. Done AFTER adapter.start so send_text actually
        # reaches Telegram. ``dropped_*`` branches are info-only — owner
        # has already moved on.
        if recovery.interrupted > 0 and self._adapter is not None:
            self._spawn_bg(
                self._adapter.send_text(
                    self._settings.owner_chat_id,
                    f"daemon restart: {recovery.interrupted} subagent(s) "
                    "marked interrupted (prior daemon run crashed mid-"
                    "subagent). Respawn manually if needed.",
                )
            )

        # Phase 6: spawn the picker after the adapter is ready so any
        # SubagentStop notify it triggers can actually deliver.
        if self._sub_picker is not None:
            self._spawn_bg(self._sub_picker.run())

        if sched_enabled:
            assert self._sched_store is not None
            self._sched_queue = asyncio.Queue(
                maxsize=self._settings.scheduler.dispatcher_queue_size
            )
            self._sched_dispatcher = SchedulerDispatcher(
                queue=self._sched_queue,
                store=self._sched_store,
                handler=handler,
                adapter=self._adapter,
                owner_chat_id=self._settings.owner_chat_id,
                settings=self._settings,
            )
            self._sched_loop = SchedulerLoop(
                queue=self._sched_queue,
                store=self._sched_store,
                inflight_ref=self._sched_dispatcher.inflight,
                settings=self._settings,
                clock=RealClock(),
            )
            self._spawn_bg_supervised(
                self._sched_dispatcher.run,
                name="scheduler_dispatcher",
            )
            self._spawn_bg_supervised(
                self._sched_loop.run, name="scheduler_loop"
            )
            # Catchup recap: only if we decidedly missed work (H-2).
            if (
                catchup_missed
                >= self._settings.scheduler.min_recap_threshold
                and boot_class != "clean-deploy"
            ):
                top3 = await self._sched_store.top_missed_schedules(
                    limit=3
                )
                recap = (
                    f"пока я спал, пропущено {catchup_missed} "
                    f"напоминаний (top-3: {', '.join(top3) or '-'})."
                )
                self._spawn_bg(
                    self._adapter.send_text(
                        self._settings.owner_chat_id, recap
                    )
                )

    def _spawn_bg(self, coro: Any) -> None:
        """Anchor a background coroutine so it is not GC'd mid-flight
        (NH-5: ``asyncio.create_task`` orphan risk)."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _rss_observer(self, interval_s: float = 60.0) -> None:
        """Phase 6e fix-pack-2 (DevOps CRIT-3): periodic RSS sampler.

        Reads ``/proc/self/status``, parses ``VmRSS:`` (kB), and emits a
        structured ``daemon_rss`` log line every ``interval_s`` seconds
        alongside the current bg-task / persist / subagent set sizes.
        That correlation lets the operator spot whether an RSS bump
        coincides with bg work in flight or is a steady-state leak.

        Runs forever as a ``_bg_tasks``-anchored coroutine; cancelled by
        ``Daemon.stop`` (the ``await asyncio.sleep`` is cancel-safe so
        shutdown is prompt).

        On hosts without ``/proc/self/status`` (macOS dev box) the
        observer exits silently after the first ``FileNotFoundError`` —
        no spam in dev runs. Any other exception while reading is
        logged at ``debug`` and the loop continues; a transient
        permission flake or one-off parse error must not crash the
        daemon's observability path.

        No psutil dep on purpose: ``/proc/self/status`` is a single
        read of an in-kernel pseudo-file and avoids pulling a C-extension
        into the runtime image. The bot already standardised on that
        approach — see :class:`Daemon.healthcheck` (compose) which
        readlinks ``/proc/<pid>/exe``.
        """
        plog = get_logger("daemon.rss_observer")
        while True:
            try:
                # ``/proc/self/status`` is a kernel-synthesised pseudo-file
                # — every read is satisfied directly from in-kernel memory
                # without disk I/O, so ``open`` + small-buffer read does
                # NOT block the event loop in any meaningful sense.
                # ``ASYNC230`` is suppressed for that reason; offloading
                # to ``asyncio.to_thread`` would be more expensive (thread
                # hop) than the read itself.
                with open("/proc/self/status") as f:  # noqa: ASYNC230
                    rss_kb = next(
                        int(line.split()[1])
                        for line in f
                        if line.startswith("VmRSS:")
                    )
            except FileNotFoundError:
                # /proc/self/status absent on macOS dev box — silent
                # one-shot exit; do not emit a log line on every dev
                # boot.
                return
            except Exception as exc:
                plog.debug("rss_read_failed", error=repr(exc))
            else:
                plog.info(
                    "daemon_rss",
                    rss_mb=rss_kb // 1024,
                    bg_tasks=len(self._bg_tasks),
                    audio_persist_pending=len(
                        self._audio_persist_pending
                    ),
                    sub_pending=len(self._sub_pending_updates),
                )
            await asyncio.sleep(interval_s)

    def spawn_audio_task(
        self, coro: Coroutine[Any, Any, None]
    ) -> None:
        """Phase 6e: register a bg audio coroutine in ``_bg_tasks``.

        Wired into ``ClaudeHandler.audio_dispatch``; called once per
        voice / audio / URL turn AFTER the per-chat lock releases. Same
        anchor pattern as :meth:`_spawn_bg` (NH-5 orphan-task risk),
        but kept distinct so future audio-only telemetry / shutdown
        hooks have a single seam to extend.

        On ``Daemon.stop`` the bg task is cancelled by the
        ``_bg_tasks`` drain; the persist tracking lives in the
        SEPARATE ``_audio_persist_pending`` set which is drained
        afterwards (see :meth:`stop`). Cancellation propagates into
        ``ClaudeHandler._run_audio_job`` and out through its outer
        ``finally`` so the persist task is still scheduled before the
        bg task itself terminates.

        Fix-pack F4 (DevOps CRIT-1): wrap the inner coroutine so any
        unhandled exception (AssertionError, OperationalError, etc.)
        becomes a structured ``log.exception`` instead of a silent
        unraisable-hook warning. ``CancelledError`` is preserved so
        ``Daemon.stop`` drain semantics still work. Persist data is
        independently scheduled inside ``_run_audio_job``'s outer
        finally; the wrapper exception path doesn't lose persist
        because the persist task lives in ``_audio_persist_pending``.
        """

        async def _wrapped() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("audio_bg_task_unhandled")

        self._spawn_bg(_wrapped())

    def _spawn_bg_supervised(
        self,
        factory: Callable[[], Coroutine[Any, Any, None]],
        *,
        name: str,
        max_respawn_per_hour: int = 3,
        backoff_s: float = 5.0,
    ) -> None:
        """Spawn a supervised background task (H-1).

        On non-cancellation exceptions the supervisor respawns after
        ``backoff_s`` seconds. After ``max_respawn_per_hour`` crashes
        within a rolling hour it gives up and (best-effort) sends a
        one-shot Telegram notify to the owner. The daemon as a whole
        continues running — a dead scheduler shouldn't kill Telegram
        echo.

        ``factory`` is a zero-arg callable returning a fresh
        coroutine. Callers MUST pass a factory (e.g. ``loop.run``) and
        NOT a pre-instantiated coroutine — a coroutine can only be
        awaited once, so respawning needs a fresh one each time.
        """

        async def _supervisor() -> None:
            crashes: list[float] = []
            while True:
                task: asyncio.Task[None] = asyncio.create_task(factory())
                try:
                    await task
                    return  # clean exit
                except asyncio.CancelledError:
                    # Fix 4 / devil C2: cancelling the supervisor task
                    # does NOT cascade to the child ``task`` created
                    # with ``asyncio.create_task``. Without this, the
                    # child outlives the supervisor, and when
                    # :meth:`Daemon.stop` closes the sqlite connection
                    # the child's next DB call raises
                    # ``sqlite3.ProgrammingError``. Bound the wait to
                    # keep shutdown predictable; :class:`shield` keeps
                    # the inner await from being cancelled twice.
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await asyncio.shield(
                            asyncio.wait({task}, timeout=5.0)
                        )
                    raise
                except Exception as exc:  # supervise all non-cancel
                    now_s = asyncio.get_running_loop().time()
                    crashes = [
                        t for t in crashes if now_s - t < 3600
                    ] + [now_s]
                    log.warning(
                        "bg_task_crashed",
                        name=name,
                        count=len(crashes),
                        error=repr(exc),
                    )
                    if len(crashes) > max_respawn_per_hour:
                        log.error("bg_task_giving_up", name=name)
                        if self._adapter is not None:
                            with contextlib.suppress(Exception):
                                await self._adapter.send_text(
                                    self._settings.owner_chat_id,
                                    f"{name} crashed "
                                    f"{len(crashes)}x in 1h; stopped",
                                )
                        return
                    await asyncio.sleep(backoff_s)

        self._spawn_bg(_supervisor())

    async def _bootstrap_skill_creator_bg(self) -> None:
        """Best-effort first-boot install of Anthropic's ``skill-creator``.

        Runs concurrently with the rest of ``start()`` — a slow GitHub
        fetch must not block Telegram polling. All exceptions are logged
        and swallowed (the daemon still starts without skill-creator;
        the user can re-attempt via ``mcp__installer__marketplace_install``).

        NH-1: the marker file ``.0xone-installed`` is the idempotency
        gate. A partial ``atomic_install`` failure leaves no marker, so
        the next daemon boot retries.
        """
        skills_dir = self._settings.project_root / "skills"
        skill_dir = skills_dir / "skill-creator"
        marker = skill_dir / ".0xone-installed"
        if marker.is_file():
            return
        # B1 fix (wave-3): recover from a previous partial install. If the
        # skill directory exists but the idempotency marker does NOT, a
        # prior bootstrap crashed between ``atomic_install`` rename and the
        # ``.0xone-installed`` touch. Leaving the dir in place makes the
        # next ``atomic_install`` raise ``InstallError`` ("already installed")
        # and the skill is permanently broken until manual cleanup. Clean
        # up the stale directory before re-fetching.
        if skill_dir.exists():
            log.warning(
                "skill_creator_partial_install_detected_cleanup",
                path=str(skill_dir),
            )
            _shutil.rmtree(skill_dir, ignore_errors=True)
        try:
            fetch = _core._fetch_tool()
        except _core.FetchToolMissing:
            log.warning("skill_creator_bootstrap_skipped_no_gh_nor_git")
            return
        log.info("skill_creator_bootstrap_starting", via=fetch)
        tmp = self._settings.data_dir / "run" / "tmp" / "skill-creator-boot"
        if tmp.exists():
            _shutil.rmtree(tmp, ignore_errors=True)
        try:

            async def _run() -> None:
                url = _core.marketplace_tree_url("skill-creator")
                await _core.fetch_bundle_async(url, tmp)
                report = await asyncio.to_thread(_core.validate_bundle, tmp)
                await asyncio.to_thread(
                    _core.atomic_install,
                    tmp,
                    report,
                    project_root=self._settings.project_root,
                )
                sentinel = self._settings.data_dir / "run" / "skills.dirty"
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.touch()

            await asyncio.wait_for(_run(), timeout=120)
            log.info("skill_creator_bootstrap_ok")
        except TimeoutError:
            log.warning("skill_creator_bootstrap_timeout")
        except Exception as exc:  # surface everything, never re-raise
            log.warning("skill_creator_bootstrap_failed", error=str(exc))
        finally:
            if tmp.exists():
                _shutil.rmtree(tmp, ignore_errors=True)

    async def stop(self) -> None:
        log.info("daemon_stopping")
        # H-2: write clean-exit marker BEFORE cancelling bg tasks or
        # closing the DB. If we crashed after writing but before full
        # teardown, the next boot still correctly classifies us as a
        # clean-deploy (the marker content + mtime are what matter).
        # Best-effort — a filesystem that rejects the write is non-fatal.
        with contextlib.suppress(OSError):
            write_clean_exit_marker(
                self._settings.data_dir / ".last_clean_exit"
            )
        # Stop scheduler tasks gracefully so their in-flight trigger
        # processing can mark_acked / revert_to_pending before the
        # DB closes.
        if self._sched_loop is not None:
            self._sched_loop.stop()
        if self._sched_dispatcher is not None:
            self._sched_dispatcher.stop()
        # Phase 6: signal the picker to stop BEFORE the bg-tasks drain
        # so no new dispatch is initiated during shutdown.  In-flight
        # dispatches finish their record_started / record_finished SQL
        # because the picker awaits inline (research RQ3 / devil H-6).
        if self._sub_picker is not None:
            self._sub_picker.request_stop()
        if self._adapter is not None:
            await self._adapter.stop()
        # S8 (wave-3): cancel tracked bg coroutines (skill_creator bootstrap,
        # sweepers) BEFORE closing the sqlite connection. Otherwise a
        # long-running ``fetch_bundle_async`` could try to touch the DB-adjacent
        # sentinel after ``close()``. ``gather(..., return_exceptions=True)``
        # swallows the CancelledError so shutdown stays clean.
        for t in list(self._bg_tasks):
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        # Phase 6e: drain bg audio persist tasks BEFORE the subagent
        # drain + conn.close. Each persist task writes to
        # ``conversations`` (user-row marker) and updates the ``turns``
        # row (interrupt_turn). The persist tasks were SHIELDED inside
        # the bg job's ``finally`` so the previous ``_bg_tasks`` cancel
        # didn't kill them — they keep running, anchored to this set.
        # On drain timeout the turn stays ``pending`` in the DB and the
        # boot reaper handles it on next start (acceptable trade-off:
        # 5s budget covers p99 sqlite write latency on a healthy VPS).
        if self._audio_persist_pending:
            audio_pending = list(self._audio_persist_pending)
            log.info(
                "daemon_draining_audio_persist",
                count=len(audio_pending),
            )
            # F11: snapshot which tasks are still running BEFORE the
            # outer ``gather`` is cancelled by ``wait_for`` — gather
            # cascades the cancellation to its inner tasks, which would
            # flip them to ``done()`` by the time the except block
            # observed them. Build the snapshot just-in-time inside
            # ``asyncio.wait`` so the post-timeout log carries the real
            # outstanding set.
            try:
                _done, not_done = await asyncio.wait(
                    audio_pending,
                    timeout=self._settings.audio_bg.drain_timeout_s,
                    return_when=asyncio.ALL_COMPLETED,
                )
                if not_done:
                    log.warning(
                        "daemon_audio_persist_drain_timeout",
                        outstanding=[t.get_name() for t in not_done],
                    )
                    # Cancel the still-pending so they don't outlive
                    # ``conn.close()`` and raise on a closed DB.
                    for t in not_done:
                        t.cancel()
                    await asyncio.gather(*not_done, return_exceptions=True)
            except Exception as exc:
                log.warning(
                    "daemon_audio_persist_drain_error",
                    error=repr(exc),
                )
        # Phase 6 (GAP #12): drain SubagentStop shielded notify tasks
        # BEFORE conn.close so a fired-but-not-yet-delivered Telegram
        # send can finish without ProgrammingError on a closed DB.
        if self._sub_pending_updates:
            updates = list(self._sub_pending_updates)
            log.info(
                "daemon_draining_subagent_notifies",
                count=len(updates),
            )
            # F11 (parity with audio drain): snapshot still-running
            # tasks via ``asyncio.wait`` so the timeout log carries the
            # real outstanding set instead of the post-cancel empty
            # list. Cancel any leftover tasks before ``conn.close()``.
            try:
                _done, not_done = await asyncio.wait(
                    updates,
                    timeout=self._settings.subagent.drain_timeout_s,
                    return_when=asyncio.ALL_COMPLETED,
                )
                if not_done:
                    log.warning(
                        "daemon_subagent_drain_timeout",
                        outstanding=[t.get_name() for t in not_done],
                    )
                    for t in not_done:
                        t.cancel()
                    await asyncio.gather(*not_done, return_exceptions=True)
            except Exception as exc:
                log.warning(
                    "daemon_subagent_drain_error",
                    error=repr(exc),
                )
        # Cancel installer-spawned uv-sync tasks (module-level set).
        await _core.cancel_bg_tasks()
        if self._conn is not None:
            await self._conn.close()
        # Fix 6 / H6-W3: release the singleton lock last so any
        # concurrent restart attempt still sees us as the holder
        # until our state is fully torn down.
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None
        log.info("daemon_stopped")

    @property
    def polling_task(self) -> asyncio.Task[None] | None:
        """Polling task — проставляется после `start()`. None до того."""
        if self._adapter is None:
            return None
        return self._adapter.polling_task


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    d = Daemon(settings)
    stop_event = asyncio.Event()
    polling_exc: BaseException | None = None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    try:
        await d.start()
        polling = d.polling_task
        # start() гарантирует что _adapter создан и polling task запущен.
        assert polling is not None, "polling_task must exist after start()"

        def _on_polling_done(t: asyncio.Task[None]) -> None:
            """Разбудить main при crash polling task — иначе silent hang."""
            nonlocal polling_exc
            if not t.cancelled():
                polling_exc = t.exception()
            stop_event.set()

        polling.add_done_callback(_on_polling_done)
        await stop_event.wait()
    finally:
        await d.stop()
    if polling_exc is not None:
        # Log + re-raise: asyncio.run() напечатает traceback, exit-код
        # будет ненулевым. Owner увидит причину вместо "daemon_started"
        # и тишины в Telegram.
        log.error("polling_crashed", error=str(polling_exc))
        raise polling_exc
