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
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.tools_sdk import _installer_core as _core
from assistant.tools_sdk import installer as _installer_mod
from assistant.tools_sdk import memory as _memory_mod

log = get_logger("main")


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

        bridge = ClaudeBridge(self._settings)
        handler = ClaudeHandler(self._settings, store, bridge)
        self._adapter = TelegramAdapter(self._settings)
        self._adapter.set_handler(handler)
        await self._adapter.start()
        log.info("daemon_started", owner=self._settings.owner_chat_id)

    def _spawn_bg(self, coro: Any) -> None:
        """Anchor a background coroutine so it is not GC'd mid-flight
        (NH-5: ``asyncio.create_task`` orphan risk)."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

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
