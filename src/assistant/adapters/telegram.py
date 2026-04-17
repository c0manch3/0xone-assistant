from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message, TelegramObject, Update
from aiogram.utils.chat_action import ChatActionSender

from assistant.adapters.base import Handler, IncomingMessage, MessengerAdapter
from assistant.config import Settings
from assistant.logger import get_logger

log = get_logger("adapters.telegram")

TELEGRAM_LIMIT = 4096
# Wave-2 G-W2-1: retry a `TelegramRetryAfter` up to this many times per
# message part before giving up. 2 retries x typical 3 s rate-limit window
# is a full 6-second buffer — good enough for bursts from scheduler + user
# overlapping without turning into a turn-burn.
TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS = 2


def split_for_telegram(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split `text` into ≤`limit`-sized chunks, preferring paragraph / line
    boundaries. Falls back to a hard cut when a single line exceeds `limit`."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # Prefer paragraph boundary, then single newline, then hard cut.
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramAdapter(MessengerAdapter):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # parse_mode=None — phase 2 sends plain text (Claude markdown would need
        # escaping); migration to MarkdownV2/HTML is deferred to phase 3+.
        self._bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=None),
        )
        self._dp: Dispatcher = Dispatcher()
        self._handler: Handler | None = None
        self._polling_task: asyncio.Task[None] | None = None

        self._dp.update.outer_middleware.register(self._log_non_owner_middleware)

        self._dp.message.filter(F.chat.id == settings.owner_chat_id)
        self._dp.message.register(self._on_text, F.text)
        self._dp.message.register(self._on_non_text)

        self._dp.shutdown.register(self._on_shutdown)

    def set_handler(self, handler: Handler) -> None:
        self._handler = handler

    async def _log_non_owner_middleware(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            msg = event.message
            if (
                msg is not None
                and msg.from_user is not None
                and msg.from_user.id != self._settings.owner_chat_id
            ):
                log.debug(
                    "non_owner_rejected",
                    chat_id=msg.chat.id,
                    user_id=msg.from_user.id,
                )
        return await handler(event, data)

    async def _on_text(self, message: Message) -> None:
        if self._handler is None:
            log.warning("text_received_without_handler")
            return
        assert message.text is not None
        incoming = IncomingMessage(
            chat_id=message.chat.id,
            text=message.text,
            message_id=message.message_id,
            origin="telegram",
        )
        chunks: list[str] = []

        async def emit(text: str) -> None:
            chunks.append(text)

        async with ChatActionSender.typing(bot=self._bot, chat_id=message.chat.id):
            await self._handler.handle(incoming, emit)

        full = "".join(chunks).strip()
        if not full:
            log.info("empty_reply_skipped", chat_id=message.chat.id)
            return
        # Fix-pack CRITICAL #2: route through `send_text` so the
        # `TelegramRetryAfter` retry loop (wave-2 G-W2-1) covers user
        # replies, not just scheduler deliveries. Avoids the duplicate
        # split-and-send body that previously raised on any 429.
        await self.send_text(message.chat.id, full)

    async def _on_non_text(self, message: Message) -> None:
        log.info("non_text_rejected", content_type=message.content_type)
        await message.answer("Пока принимаю только текст.")

    async def _on_shutdown(self) -> None:
        log.info("telegram_shutdown")

    async def start(self) -> None:
        self._polling_task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False),
            name="aiogram-polling",
        )

    async def stop(self) -> None:
        if self._polling_task is None:
            return
        try:
            await self._dp.stop_polling()
        except (RuntimeError, LookupError) as exc:
            log.warning("stop_polling_skipped", error=str(exc))
        with contextlib.suppress(asyncio.CancelledError):
            await self._polling_task

    async def send_text(self, chat_id: int, text: str) -> None:
        """Send `text` to Telegram, splitting into 4096-char chunks.

        WAVE-2 G-W2-1: catch `TelegramRetryAfter` per-chunk, sleep the
        server-advised `retry_after + 1` seconds, retry up to
        `TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS` times. Without this wrapper,
        a 429 from the Telegram API propagates out of `send_text` and
        into the scheduler dispatcher's `except Exception` branch, which
        would mark the trigger `pending_retry` and burn a Claude turn on
        the next materialisation.
        """
        for part in split_for_telegram(text):
            for attempt in range(TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS + 1):
                try:
                    await self._bot.send_message(chat_id=chat_id, text=part)
                    break
                except TelegramRetryAfter as exc:
                    if attempt >= TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS:
                        log.warning(
                            "telegram_retry_after_exhausted",
                            chat_id=chat_id,
                            retry_after=exc.retry_after,
                            attempts=attempt + 1,
                        )
                        raise
                    log.info(
                        "telegram_retry_after",
                        chat_id=chat_id,
                        retry_after=exc.retry_after,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(exc.retry_after + 1)
