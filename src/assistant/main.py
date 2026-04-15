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


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    d = Daemon()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    try:
        await d.start()
        await stop_event.wait()
    finally:
        await d.stop()
