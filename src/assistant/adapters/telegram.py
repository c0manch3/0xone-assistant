from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from assistant.adapters.base import IncomingMessage, MessengerAdapter
from assistant.config import Settings
from assistant.logger import get_logger

log = get_logger("adapters.telegram")


class _Handler(Protocol):
    async def handle(self, msg: IncomingMessage) -> None: ...


class TelegramAdapter(MessengerAdapter):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp: Dispatcher = Dispatcher()
        self._handler: _Handler | None = None
        self._polling_task: asyncio.Task[None] | None = None

        self._dp.message.filter(F.chat.id == settings.owner_chat_id)

        self._dp.message.register(self._on_text, F.text)
        self._dp.message.register(self._on_non_text)

        self._dp.shutdown.register(self._on_shutdown)

    def set_handler(self, handler: _Handler) -> None:
        self._handler = handler

    async def _on_text(self, message: Message) -> None:
        if self._handler is None:
            log.warning("text_received_without_handler")
            return
        assert message.text is not None
        incoming = IncomingMessage(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=message.text,
        )
        async with ChatActionSender.typing(bot=self._bot, chat_id=message.chat.id):
            await self._handler.handle(incoming)

    async def _on_non_text(self, message: Message) -> None:
        log.info("non_text_rejected", content_type=message.content_type)
        await message.answer("Медиа пока не поддерживаю — это будет в phase 6.")

    async def _on_shutdown(self) -> None:
        log.info("telegram_shutdown")

    async def start(self) -> None:
        self._polling_task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False),
            name="aiogram-polling",
        )

    async def stop(self) -> None:
        await self._dp.stop_polling()
        if self._polling_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._polling_task
        await self._bot.session.close()

    async def send_text(self, chat_id: int, text: str) -> None:
        await self._bot.send_message(chat_id=chat_id, text=text)
