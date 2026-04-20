from __future__ import annotations

import asyncio
import signal

import aiosqlite

from assistant.adapters.telegram import TelegramAdapter
from assistant.config import get_settings
from assistant.handlers.message import EchoHandler
from assistant.logger import get_logger, setup_logging
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect

log = get_logger("main")


class Daemon:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._conn: aiosqlite.Connection | None = None
        self._adapter: TelegramAdapter | None = None

    async def start(self) -> None:
        self._conn = await connect(self._settings.db_path)
        await apply_schema(self._conn)
        store = ConversationStore(self._conn)
        self._adapter = TelegramAdapter(self._settings)
        handler = EchoHandler(store, self._adapter)
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
    d = Daemon()
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
