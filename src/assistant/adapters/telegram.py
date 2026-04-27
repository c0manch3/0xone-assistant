from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import cast
from uuid import uuid4

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from assistant.adapters.base import (
    AttachmentKind,
    Handler,
    IncomingMessage,
    MessengerAdapter,
)
from assistant.config import Settings
from assistant.logger import get_logger

log = get_logger("adapters.telegram")

# Telegram hard limit is 4096 chars per message; we split on paragraph /
# newline boundaries when exceeded.
TELEGRAM_MSG_LIMIT = 4096

# Phase 6a: file-attachment ingestion guards.
#
# Telegram bot API hard cap on inbound attachments. Pre-download check
# avoids spawning ``bot.download(...)`` for files Telegram would refuse
# to send anyway; the post-download ``TelegramBadRequest`` envelope
# catches the rare case where ``Document.file_size`` is missing but the
# server-side file actually exceeds 20 MB (devil C2).
TELEGRAM_DOC_MAX_BYTES = 20 * 1024 * 1024

# Whitelisted attachment suffixes (lower-case, no leading dot). Anything
# outside this set is rejected with a Russian "формат не поддерживается"
# reply. Aligned with the ``AttachmentKind`` Literal in adapters/base.
_SUFFIX_WHITELIST: frozenset[str] = frozenset({"pdf", "docx", "txt", "md", "xlsx"})

# Default caption when the owner sends a document with no caption
# (devil L6 — applies to ALL whitelisted formats including TXT/MD for
# UX consistency).
_DEFAULT_FILE_CAPTION = "опиши содержимое файла"

# Sanitisation cap on the original filename portion of the tmp path.
# 64 chars is enough for a useful post-mortem in ``.failed/`` without
# encouraging owner-supplied long names from blowing the path budget.
_FILENAME_MAX_LEN = 64


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

        # Order matters: aiogram picks the first matching handler.
        # Phase 6a (RQ3 verified): text → document → catch-all. Inserting
        # ``F.document`` after the catch-all would silently route every
        # upload to the "медиа пока не поддерживаю" reply.
        self._dp.message.register(self._on_text, F.text)
        self._dp.message.register(self._on_document, F.document)
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

    async def _on_document(self, message: Message) -> None:
        """Phase 6a: file-attachment ingestion.

        Routing contract (RQ3): triggered for any ``F.document`` update;
        ``message.text`` is ``None`` here, the user-supplied caption (if
        any) lives on ``message.caption``.

        Validation order:

        1. Pre-download size guard. ``Document.file_size`` is
           ``Optional[int]`` (Telegram occasionally omits it for
           forwards from old clients) — ``(file_size or 0) > N`` is the
           type-safe form (devil C2).
        2. Suffix whitelist on ``Document.file_name``. ``file_name`` is
           also ``Optional[str]``; missing or unrecognised suffix → reject
           with the Russian format-list reply (devil M1).
        3. ``bot.download(...)`` wrapped in ``try/except
           TelegramBadRequest``. The "file is too big" error is the
           catch-net for the file_size-missing-but-actually-too-big case;
           any other download failure surfaces a Russian "не смог
           скачать" reply (devil C2).

        Tmp filename: ``<uuid4>__<sanitized_orig>.<ext>``. The UUID
        prevents collisions across concurrent owner+scheduler turns; the
        sanitised original stem eases post-mortem when an extraction
        failure quarantines the file (devil M5). Sanitisation strips any
        path-traversal / control / punctuation chars and caps at
        ``_FILENAME_MAX_LEN``.

        On success, an :class:`IncomingMessage` is constructed with the
        three attachment fields populated; the handler owns cleanup via
        ``try/finally tmp.unlink()``.
        """
        if self._handler is None:
            log.warning("document_received_without_handler")
            return

        doc = message.document
        if doc is None:  # pragma: no cover — F.document filter guarantees set
            return

        # 1. Pre-download size guard.
        file_size = doc.file_size or 0
        if file_size > TELEGRAM_DOC_MAX_BYTES:
            await message.reply(
                "файл больше 20 МБ — это лимит Telegram bot API; пришли поменьше"
            )
            return

        # 2. Suffix whitelist (devil M1: file_name may be None).
        file_name = doc.file_name or ""
        suffix = Path(file_name).suffix.lower().lstrip(".")
        if not suffix or suffix not in _SUFFIX_WHITELIST:
            await message.reply(
                "формат не поддерживается; список: PDF, DOCX, TXT, MD, XLSX"
            )
            return

        # Tmp path inside ``settings.uploads_dir``. The hook in
        # ``bridge/hooks.py`` constrains every model-issued ``Read`` to
        # ``project_root``; placing tmp at ``/app/.uploads/`` (Option 1)
        # keeps the hook untouched.
        orig_stem = Path(file_name).stem or "attachment"
        safe_stem = re.sub(r"[^\w.-]", "_", orig_stem)[:_FILENAME_MAX_LEN]
        # Defensive: collapse pathological "all-dots" stems that survive
        # the regex (e.g. "...." -> ".").
        if not safe_stem or safe_stem.strip(".") == "":
            safe_stem = "attachment"
        tmp_name = f"{uuid4().hex}__{safe_stem}.{suffix}"
        uploads_dir = self._settings.uploads_dir
        try:
            uploads_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("uploads_mkdir_failed", path=str(uploads_dir), error=repr(exc))
            await message.reply("не смог подготовить папку для файлов")
            return
        tmp_path = uploads_dir / tmp_name

        # 3. Download with TelegramBadRequest envelope.
        #
        # Reply sanitisation policy: NEVER echo ``repr(exc)`` /
        # ``str(exc)`` to Telegram. Raw exception text can leak
        # internal paths, file IDs, or aiohttp tracebacks into the chat
        # log. For owner-debuggability we always log the structured
        # event with the full error; the user-facing reply is a fixed
        # Russian string.
        try:
            await self._bot.download(
                doc,
                destination=tmp_path,
                # Devil M-W2-6: aiogram's default is 30s; a 19 MB file
                # on a slow uplink overruns nominally. Bump to 90s and
                # surface a dedicated Russian "timeout" reply below.
                timeout=90,
            )
        except TelegramBadRequest as exc:
            log.warning(
                "telegram_download_failed",
                path=str(tmp_path),
                error=str(exc),
            )
            msg_lower = str(exc).lower()
            if "too big" in msg_lower or "too large" in msg_lower:
                await message.reply(
                    "файл больше 20 МБ — это лимит Telegram bot API"
                )
            else:
                await message.reply("не смог скачать файл — проверь логи")
            # Best-effort cleanup of partial download.
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return
        except TimeoutError:
            log.warning(
                "telegram_download_timeout",
                path=str(tmp_path),
                timeout_s=90,
            )
            await message.reply(
                "Telegram не успел отдать файл за 90 секунд — попробуй ещё раз"
            )
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return
        except Exception as exc:
            # Any other failure (network blip, disk full, runtime bug) →
            # logged structurally + fixed Russian reply (no exception
            # text echoed to Telegram). The handler is never invoked so
            # the model never sees a half-downloaded file.
            log.exception(
                "telegram_unexpected_error",
                path=str(tmp_path),
                error_type=type(exc).__name__,
            )
            await message.reply("произошла внутренняя ошибка обработки файла")
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return

        # 4. Build IncomingMessage. Caption fallback: empty caption →
        # default Russian prompt for ALL whitelisted formats including
        # TXT/MD (devil L6).
        caption = (message.caption or "").strip() or _DEFAULT_FILE_CAPTION
        # Suffix is narrowed to AttachmentKind by the whitelist check
        # above; mypy can't infer Literal narrowing from frozenset
        # membership so we cast explicitly.
        kind = cast(AttachmentKind, suffix)
        incoming = IncomingMessage(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=caption,
            attachment=tmp_path,
            attachment_kind=kind,
            attachment_filename=file_name,
        )

        chunks: list[str] = []

        async def emit(text: str) -> None:
            chunks.append(text)

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
