from __future__ import annotations

import asyncio
import signal
import sys

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

    async def start(self) -> None:
        await _preflight_claude_cli(self._log)
        ensure_skills_symlink(self._settings.project_root)

        self._settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await connect(self._settings.db_path)
        await apply_schema(self._conn)

        conv = ConversationStore(self._conn)
        turns = TurnStore(self._conn, lock=conv.lock)

        # A previous process may have crashed mid-turn; promote any orphan
        # 'pending' rows to 'interrupted' so they're excluded from history.
        swept = await turns.sweep_pending()
        if swept:
            self._log.warning("startup_swept_pending_turns", count=swept)

        bridge = ClaudeBridge(self._settings)

        self._adapter = TelegramAdapter(self._settings)
        handler = ClaudeHandler(self._settings, conv, turns, bridge)
        self._adapter.set_handler(handler)
        await self._adapter.start()
        self._log.info("daemon_started", owner=self._settings.owner_chat_id)

    async def stop(self) -> None:
        self._log.info("daemon_stopping")
        if self._adapter is not None:
            try:
                await self._adapter.stop()
            except Exception:
                self._log.warning("stop_step_failed", step="adapter", exc_info=True)
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
