from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
from collections.abc import Coroutine
from typing import Any

import aiosqlite
import structlog

from assistant.adapters.telegram import TelegramAdapter
from assistant.bridge.bootstrap import ensure_skills_symlink
from assistant.bridge.claude import ClaudeBridge
from assistant.config import Settings, get_settings
from assistant.handlers.message import ClaudeHandler
from assistant.logger import get_logger, setup_logging
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore

AUTH_PREFLIGHT_FAIL_EXIT = 3

# Phase-3 TTLs (Q7).
_TMP_TTL_S = 3600  # 1 hour — aborted fetches
_INSTALLER_CACHE_TTL_S = 7 * 86400  # 7 days — preview -> install window
_BOOTSTRAP_TIMEOUT_S = 120.0


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
        installer = self._settings.project_root / "tools" / "skill-installer" / "main.py"
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
            await self._bootstrap_notify_failure()
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
            await self._bootstrap_notify_failure()
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
            await self._bootstrap_notify_failure()
            return
        self._log.info("skill_creator_bootstrap_ok")

    async def _bootstrap_notify_failure(self) -> None:
        """Send a one-shot Telegram message to the owner on bootstrap failure.

        Tracked by a file marker so restarts don't resend. Never raises
        upstream — a failing notification is far less important than the
        underlying bootstrap failure we're already logging.
        """
        marker = self._settings.data_dir / "run" / ".bootstrap_notified"
        if marker.exists():
            return
        if self._adapter is None:
            return
        msg = (
            "Автобутстрап skill-creator не удался — marketplace-установка "
            "временно недоступна. Проверь `gh auth status` и логи."
        )
        try:
            await self._adapter.send_text(self._settings.owner_chat_id, msg)
        except Exception as exc:
            self._log.warning("bootstrap_notify_failed", error=repr(exc))
            return
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError as exc:
            self._log.warning("bootstrap_marker_write_failed", error=repr(exc))

    # ------------------------------------------------------------------

    async def start(self) -> None:
        await _preflight_claude_cli(self._log)
        ensure_skills_symlink(self._settings.project_root)

        self._settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        (self._settings.data_dir / "run").mkdir(parents=True, exist_ok=True)
        self._conn = await connect(self._settings.db_path)
        await apply_schema(self._conn)

        conv = ConversationStore(self._conn)
        turns = TurnStore(self._conn, lock=conv.lock)

        # A previous process may have crashed mid-turn; promote any orphan
        # 'pending' rows to 'interrupted' so they're excluded from history.
        swept = await turns.sweep_pending()
        if swept:
            self._log.warning("startup_swept_pending_turns", count=swept)

        if shutil.which("gh") is None:
            self._log.warning(
                "gh_cli_not_found",
                hint=("marketplace features disabled. Install https://cli.github.com/ to enable."),
            )

        bridge = ClaudeBridge(self._settings)

        self._adapter = TelegramAdapter(self._settings)
        handler = ClaudeHandler(self._settings, conv, turns, bridge)
        self._adapter.set_handler(handler)
        await self._adapter.start()

        # Fire-and-forget housekeeping. `Daemon.start()` must not wait on
        # GitHub or `uv sync`; bootstrap has its own 120 s timeout internally.
        self._spawn_bg(self._sweep_run_dirs(), name="sweep_run_dirs")
        self._spawn_bg(self._bootstrap_skill_creator_bg(), name="skill_creator_bootstrap")

        self._log.info("daemon_started", owner=self._settings.owner_chat_id)

    async def stop(self) -> None:
        self._log.info("daemon_stopping")
        if self._adapter is not None:
            try:
                await self._adapter.stop()
            except Exception:
                self._log.warning("stop_step_failed", step="adapter", exc_info=True)
        if self._bg_tasks:
            self._log.info("daemon_waiting_bg_tasks", count=len(self._bg_tasks))
            # return_exceptions keeps one rogue task from propagating and
            # masking the others; each task already logs its own failures.
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                self._log.warning("stop_step_failed", step="db", exc_info=True)
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
