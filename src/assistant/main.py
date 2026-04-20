from __future__ import annotations

import asyncio
import logging
import signal
import sys

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

log = get_logger("main")


class Daemon:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: aiosqlite.Connection | None = None
        self._adapter: TelegramAdapter | None = None

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
        await self._preflight_claude_auth()
        # S15: refuse to start if .claude/settings*.json carries hooks/permissions.
        # We pass a plain stdlib logger because the helper is typed against
        # logging.Logger — structlog's BoundLogger is not a direct subtype.
        assert_no_custom_claude_settings(
            self._settings.project_root,
            logging.getLogger("bridge.bootstrap"),
        )
        ensure_skills_symlink(self._settings.project_root)

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

    async def stop(self) -> None:
        log.info("daemon_stopping")
        if self._adapter is not None:
            await self._adapter.stop()
        if self._conn is not None:
            await self._conn.close()
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
