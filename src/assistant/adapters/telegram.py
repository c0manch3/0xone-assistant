from __future__ import annotations

import asyncio
import contextlib

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from assistant.adapters.base import Handler, IncomingMessage, MessengerAdapter
from assistant.config import Settings
from assistant.logger import get_logger

log = get_logger("adapters.telegram")

# Telegram hard limit is 4096 chars per message; we split on paragraph /
# newline boundaries when exceeded.
TELEGRAM_MSG_LIMIT = 4096


def _split_for_telegram(text: str, *, limit: int = TELEGRAM_MSG_LIMIT) -> list[str]:
    """Split ``text`` into chunks of <= ``limit`` chars.

    Prefers breaking at ``\\n\\n``, falls back to ``\\n``, and last-resort
    hard-cuts at exactly ``limit``. Empty/whitespace chunks are dropped.
    """
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < 0:
            cut = remaining.rfind("\n", 0, limit)
        if cut < 0:
            cut = limit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out


class TelegramAdapter(MessengerAdapter):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Q11: parse_mode=None — Claude's markdown output frequently fails
        # Telegram's HTML/MarkdownV2 validation. Plain text is safer.
        self._bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=None),
        )
        self._dp = Dispatcher()
        self._handler: Handler | None = None
        self._polling_task: asyncio.Task[None] | None = None

        # Router-level owner filter: messages from non-owners don't reach
        # any handler.
        self._dp.message.filter(F.chat.id == settings.owner_chat_id)

        # Order matters: text handler first, then catch-all for media.
        self._dp.message.register(self._on_text, F.text)
        self._dp.message.register(self._on_non_text)

        self._dp.shutdown.register(self._on_shutdown)

    def set_handler(self, handler: Handler) -> None:
        self._handler = handler

    @property
    def polling_task(self) -> asyncio.Task[None] | None:
        """Polling task, for main() supervision. ``None`` until ``start()``."""
        return self._polling_task

    # ------------------------------------------------------------------
    # Dispatcher entry points
    # ------------------------------------------------------------------
    async def _on_text(self, message: Message) -> None:
        if self._handler is None:
            log.warning("text_received_without_handler")
            return
        assert message.text is not None  # guaranteed by F.text filter.

        chunks: list[str] = []

        async def emit(text: str) -> None:
            chunks.append(text)

        incoming = IncomingMessage(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=message.text,
        )
        async with ChatActionSender.typing(bot=self._bot, chat_id=message.chat.id):
            await self._handler.handle(incoming, emit)

        full = "".join(chunks).strip() or "(пустой ответ)"
        for part in _split_for_telegram(full, limit=TELEGRAM_MSG_LIMIT):
            await self._bot.send_message(message.chat.id, part)

    async def _on_non_text(self, message: Message) -> None:
        log.info("non_text_rejected", content_type=message.content_type)
        await message.answer("Медиа пока не поддерживаю — это будет в phase 6.")

    async def _on_shutdown(self) -> None:
        log.info("telegram_shutdown")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        # handle_signals=False: Daemon in main.py owns signal wiring.
        # close_bot_session=False: session is closed explicitly in stop();
        # otherwise start_polling would close it first and stop() would
        # then double-close.
        self._polling_task = asyncio.create_task(
            self._dp.start_polling(
                self._bot,
                handle_signals=False,
                close_bot_session=False,
            ),
            name="aiogram-polling",
        )

    async def stop(self) -> None:
        # If SIGTERM arrives before polling task enters Dispatcher's
        # internal ``_running_lock``, ``stop_polling()`` raises
        # RuntimeError ("Polling is not started"). Suppress — the task
        # is about to be cancelled anyway and the session is closed
        # unconditionally at the bottom.
        with contextlib.suppress(RuntimeError):
            await self._dp.stop_polling()
        if self._polling_task is not None:
            self._polling_task.cancel()
            # Awaiting a crashed polling task would re-raise its exception
            # here and abort cleanup. The supervisor callback in main.py
            # has already captured the exception and will re-raise after
            # the finally. Suppress CancelledError + Exception so DB
            # closes and "daemon_stopped" line is written; don't suppress
            # KeyboardInterrupt / SystemExit — those are immediate-exit
            # requests from the user.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._polling_task
        await self._bot.session.close()

    async def send_text(self, chat_id: int, text: str) -> None:
        # Split-respecting counterpart for scheduler-originated messages
        # (phase 5). Phase 2 doesn't use this path but we keep parity.
        for part in _split_for_telegram(text, limit=TELEGRAM_MSG_LIMIT):
            await self._bot.send_message(chat_id=chat_id, text=part)
