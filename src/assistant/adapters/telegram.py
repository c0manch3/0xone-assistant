from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from assistant.adapters.base import (
    AUDIO_KINDS,
    AttachmentKind,
    Handler,
    IncomingMessage,
    MessengerAdapter,
)
from assistant.adapters.media_group import MediaGroupAggregator
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.services.transcription import TranscriptionService

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
#
# Phase 6b: image suffixes added (``jpg/jpeg/png/webp/heic``). The
# adapter does NOT pre-validate magic bytes — that lives in
# ``assistant.files.vision`` and runs INSIDE the handler so a bad
# magic surfaces as a ``VisionError`` and routes through the same
# ``_handle_vision_failure`` quarantine path as a corrupt decode.
_DOC_SUFFIX_WHITELIST: frozenset[str] = frozenset(
    {"pdf", "docx", "txt", "md", "xlsx"}
)
_IMAGE_SUFFIX_WHITELIST: frozenset[str] = frozenset(
    {"jpg", "jpeg", "png", "webp", "heic", "heif"}
)
# Phase 6c: extended document-route whitelist with audio kinds that the
# owner sometimes "send as file" (e.g. exported iPhone Voice Memo m4a).
_AUDIO_SUFFIX_WHITELIST: frozenset[str] = AUDIO_KINDS
_SUFFIX_WHITELIST: frozenset[str] = (
    _DOC_SUFFIX_WHITELIST | _IMAGE_SUFFIX_WHITELIST | _AUDIO_SUFFIX_WHITELIST
)

# Phase 6c (C4 closure): explicit URL-transcribe trigger. Only this regex
# routes a text message to the Mac sidecar's ``/extract`` endpoint;
# arbitrary URLs in conversation continue to feed the phase-3 installer
# hint via ``_URL_RE`` in handlers/message.py.
_URL_TRANSCRIBE_TRIGGER_RE = re.compile(
    r"^(?:транскрибируй|/voice)\s+(https?://\S+)\s*$",
    re.IGNORECASE,
)

# Manual typing-action loop cadence. Telegram's "typing" status TTL is
# ~5 s server-side; we refresh slightly under that to avoid the indicator
# blinking off mid-transcribe (H2 closure: ChatActionSender.typing was
# replaced because its internal cancel-on-error behaviour produced silent
# typing dropouts during long voice transcribes).
_TYPING_REFRESH_INTERVAL_S = 4.5

# Default caption when the owner sends a document with no caption
# (devil L6 — applies to ALL whitelisted formats including TXT/MD for
# UX consistency).
_DEFAULT_FILE_CAPTION = "опиши содержимое файла"

# Phase 6b: default caption for image-source uploads (photo or
# image-as-document). Q4 v0 — applies to BOTH inline ``F.photo`` and
# ``_on_document`` image kinds.
_DEFAULT_PHOTO_CAPTION = "что на фото?"

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

        # Phase 6b: media-group aggregator for ``F.photo`` albums. The
        # aggregator's flush + overflow callbacks are bound to ``self``
        # so they have access to ``_bot``, ``_handler``, etc. F4: an
        # ``error_reply_cb`` is wired so flush-callback exceptions
        # (bridge / scheduler errors) reach the owner as a sanitised
        # Russian "internal error" reply rather than silent log-only.
        self._media_group = MediaGroupAggregator(
            flush_cb=self._flush_media_group,
            overflow_cb=self._reply_media_group_overflow,
            error_reply_cb=self._reply_media_group_error,
        )

        # Router-level owner filter: messages from non-owners don't reach
        # any handler.
        self._dp.message.filter(F.chat.id == settings.owner_chat_id)

        # Order matters: aiogram picks the first matching handler.
        # Phase 6a (RQ3 verified): text → document → catch-all. Inserting
        # ``F.document`` after the catch-all would silently route every
        # upload to the "медиа пока не поддерживаю" reply.
        #
        # Phase 6b: text → photo → animation → document & ~F.animation
        # → catch-all. The dedicated ``F.animation`` handler emits the
        # spec'd Russian "анимации не поддерживаю" reply (AC#5) BEFORE
        # the document filter; without it, animations would land on the
        # generic catch-all. The ``~F.animation`` guard on the document
        # branch is retained so an animated GIF that bypasses the
        # ``F.animation`` aiogram class (rare encoding) still routes
        # away from the suffix whitelist.
        # Phase 6c order: text → voice → audio → photo → animation →
        # document → catch-all. ``F.voice`` and ``F.audio`` MUST land
        # BEFORE ``F.document`` because a forwarded m4a sometimes
        # arrives as a generic ``Document`` whose mime hint says
        # ``audio/mp4``; the suffix whitelist on the document route
        # then routes it back into the audio path uniformly.
        self._dp.message.register(self._on_text, F.text)
        self._dp.message.register(self._on_voice, F.voice)
        self._dp.message.register(self._on_audio, F.audio)
        self._dp.message.register(self._on_photo, F.photo)
        self._dp.message.register(self._on_animation, F.animation)
        self._dp.message.register(self._on_document, F.document & ~F.animation)
        self._dp.message.register(self._on_non_text)

        # Phase 6c: transcription service is set post-construction by
        # main.py; tests for non-audio paths can leave it None.
        self._transcription: TranscriptionService | None = None

        self._dp.shutdown.register(self._on_shutdown)

    def set_handler(self, handler: Handler) -> None:
        self._handler = handler

    def set_transcription(self, transcription: TranscriptionService) -> None:
        """Phase 6c: wire the Mac sidecar client.

        Adapter calls ``transcription.health_check()`` BEFORE entering
        the per-chat handler lock so an offline Mac short-circuits with
        a cheap rejection rather than dragging the lock through a full
        timeout.
        """
        self._transcription = transcription

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

        # Phase 6b (F6) / Phase 6c F8 (fix-pack): pre-empt any pending
        # photo media-group debounce timer for this chat — the photo
        # turn flushes immediately and runs to completion BEFORE this
        # text turn enters the per-chat lock. This preserves
        # owner-visible order (photos answered before subsequent text).
        # F8 (fix-pack): the flush MUST run BEFORE the URL-transcribe
        # routing check; otherwise an owner sending a photo album
        # followed by ``транскрибируй <URL>`` would see the URL ack
        # land before the photo answer.
        await self._media_group.flush_for_chat(message.chat.id)

        # Phase 6c (C4 closure): explicit URL transcribe trigger. ONLY
        # when the message text matches ``транскрибируй <URL>`` or
        # ``/voice <URL>`` do we route to the Mac sidecar's /extract
        # endpoint. Other URLs continue to feed the phase-3 installer
        # hint via ``_URL_RE`` in handlers/message.py. The check is
        # deliberately strict (anchored ``^`` + ``\s*$``) so a URL
        # buried in a longer message stays in the normal text path.
        url_match = _URL_TRANSCRIBE_TRIGGER_RE.match(message.text.strip())
        if url_match:
            await self._on_url_transcribe(message, url_match.group(1))
            return

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

    async def _on_url_transcribe(self, message: Message, url: str) -> None:
        """Phase 6c URL extraction path — explicit trigger only."""
        if self._handler is None:
            return
        if not await self._ensure_sidecar_health(message):
            return
        # ack BEFORE lock — same pattern as voice/audio
        await self._send_pre_lock_ack(
            message.chat.id,
            self._format_initial_ack(0, source="url"),
        )
        incoming = IncomingMessage(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text="",
            url_for_extraction=url,
        )
        await self._dispatch_audio_turn(message.chat.id, incoming)

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
        # F14: list image kinds too — phase 6b expanded the whitelist
        # to JPG/PNG/WEBP/HEIC/HEIF, so the prior reply was wrong on
        # supported formats.
        # Phase 6c: audio kinds added (MP3/M4A/WAV/OGG/OPUS).
        file_name = doc.file_name or ""
        suffix = Path(file_name).suffix.lower().lstrip(".")
        if not suffix or suffix not in _SUFFIX_WHITELIST:
            await message.reply(
                "формат не поддерживается; список: "
                "PDF, DOCX, TXT, MD, XLSX, JPG, PNG, WEBP, HEIC, "
                "MP3, M4A, WAV, OGG, OPUS"
            )
            return

        # Phase 6c: audio-suffix documents route to the audio path
        # (transcribe → standard turn) instead of the extract path.
        # H3 closure: suffix is PRIMARY, MIME secondary.
        if suffix in _AUDIO_SUFFIX_WHITELIST:
            await self._on_audio_document(message, doc, suffix, file_name)
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

        # 4. Build IncomingMessage. Caption fallback (devil L6 + Q4 v0):
        # image kinds default to ``"что на фото?"``; document kinds
        # default to ``"опиши содержимое файла"``.
        is_image = suffix in _IMAGE_SUFFIX_WHITELIST
        default_caption = (
            _DEFAULT_PHOTO_CAPTION if is_image else _DEFAULT_FILE_CAPTION
        )
        caption = (message.caption or "").strip() or default_caption
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

    async def _on_photo(self, message: Message) -> None:
        """Phase 6b: ``F.photo`` ingestion (Telegram inline preview).

        Telegram delivers photo messages with a list of ``PhotoSize``
        variants (different resolutions); we pick the largest by area.
        ``message.media_group_id`` distinguishes single photos from
        album sends — albums are aggregated by
        :class:`MediaGroupAggregator`, single photos bypass aggregation
        and fire the handler directly.

        Validation order:

        1. Pre-download size guard (largest variant ≤ 20 MB).
        2. Filename synthesis: ``<uuid>__photo_<msg_id>.jpg``. The
           Telegram inline preview is always JPEG-compressed by the
           client, so suffix ``jpg`` is correct without a magic check.
        3. ``bot.download(timeout=90)`` mirroring 6a's envelope.
        4. Single photo → ``_dispatch_image_turn`` immediately.
           Album photo → ``_media_group.add(...)`` for debounce.
        """
        if self._handler is None:
            log.warning("photo_received_without_handler")
            return

        if not message.photo:  # pragma: no cover — F.photo guarantees set
            return

        # PhotoSize area selector (research.md RQ3 + extra notes D).
        # Pure-area max beats ``message.photo[-1]``: Telegram's
        # documented ordering is by ascending size, but bot-API
        # versions have shipped reversed lists in the past.
        photo = max(message.photo, key=lambda p: (p.width * p.height, p.file_size or 0))
        photo_size = photo.file_size or 0
        if photo_size > TELEGRAM_DOC_MAX_BYTES:
            await message.reply(
                "фото больше 20 МБ — это лимит Telegram bot API; пришли поменьше"
            )
            return

        uploads_dir = self._settings.uploads_dir
        try:
            uploads_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("uploads_mkdir_failed", path=str(uploads_dir), error=repr(exc))
            await message.reply("не смог подготовить папку для файлов")
            return

        # Filename: <uuid4>__photo_<msg_id>.jpg. The msg_id shard helps
        # post-mortem (matches the Telegram update sequence). We do NOT
        # use ``photo.file_unique_id`` because it is ID-only metadata
        # exposed in logs; uuid4 is sufficient + neutral.
        tmp_name = f"{uuid4().hex}__photo_{message.message_id}.jpg"
        tmp_path = uploads_dir / tmp_name

        try:
            await self._bot.download(photo, destination=tmp_path, timeout=90)
        except TelegramBadRequest as exc:
            log.warning(
                "telegram_photo_download_failed",
                path=str(tmp_path),
                error=str(exc),
            )
            msg_lower = str(exc).lower()
            if "too big" in msg_lower or "too large" in msg_lower:
                await message.reply(
                    "фото больше 20 МБ — это лимит Telegram bot API"
                )
            else:
                await message.reply("не смог скачать фото — проверь логи")
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return
        except TimeoutError:
            log.warning(
                "telegram_photo_download_timeout",
                path=str(tmp_path),
                timeout_s=90,
            )
            await message.reply(
                "Telegram не успел отдать фото за 90 секунд — попробуй ещё раз"
            )
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return
        except Exception as exc:
            log.exception(
                "telegram_photo_unexpected_error",
                path=str(tmp_path),
                error_type=type(exc).__name__,
            )
            await message.reply("произошла внутренняя ошибка обработки фото")
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return

        caption = (message.caption or "").strip()

        if message.media_group_id:
            await self._media_group.add(
                message.chat.id,
                message.media_group_id,
                tmp_path,
                caption,
                message.message_id,
            )
            return

        # Single photo (no media_group) → direct dispatch with default
        # Russian caption fallback.
        await self._dispatch_image_turn(
            chat_id=message.chat.id,
            message_id=message.message_id,
            paths=[tmp_path],
            caption=caption or _DEFAULT_PHOTO_CAPTION,
            original_filename=None,
        )

    async def _flush_media_group(
        self,
        chat_id: int,
        group_id: str,
        paths: list[Path],
        caption: str,
        first_message_id: int,
    ) -> None:
        """Aggregator flush callback — called per-bucket after debounce.

        Synthesises one :class:`IncomingMessage` carrying every photo
        path and dispatches the vision turn through the handler.
        ``group_id`` is logged but not propagated downstream (single-
        user model — no need to surface it to the SDK).

        F13: ``first_message_id`` is the Telegram ``message_id`` of the
        first photo to arrive in the bucket — propagated into
        ``IncomingMessage.message_id`` so handler logs / DB rows can
        correlate with the chat thread.
        """
        if self._handler is None:
            log.warning("media_group_flush_without_handler")
            for p in paths:
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)
            return

        log.info(
            "media_group_flush",
            chat_id=chat_id,
            group_id=group_id,
            photos=len(paths),
            first_message_id=first_message_id,
        )
        # Use the first photo's message-id-derived synthetic name as
        # the IncomingMessage.attachment_filename so handler logs +
        # forensics are consistent with the single-photo path.
        await self._dispatch_image_turn(
            chat_id=chat_id,
            message_id=first_message_id,
            paths=paths,
            caption=caption or _DEFAULT_PHOTO_CAPTION,
            original_filename=None,
        )

    async def _reply_media_group_overflow(self, chat_id: int) -> None:
        """One-shot Russian reply for a media_group that exceeded the
        per-turn cap (5 photos).

        ``message.reply`` is unavailable here (we don't have the
        Message object), so we send a plain message to the chat. The
        Telegram bot will send it asynchronously; the aggregator does
        not wait for completion.
        """
        try:
            await self._bot.send_message(
                chat_id,
                "пришлю в этот раз только первые 5 фото — лишние не уместились в одно окно",
            )
        except Exception as exc:  # surface in logs, never crash the aggregator
            log.warning(
                "media_group_overflow_reply_failed",
                chat_id=chat_id,
                error=repr(exc),
            )

    async def _reply_media_group_error(
        self, chat_id: int, text: str
    ) -> None:
        """F4: Russian reply when the flush callback raises.

        Invoked by :class:`MediaGroupAggregator` when ``_flush_cb``
        raises (bridge error, scheduler exception, etc.). Without
        this, the owner sees no Telegram reply for a failed photo
        turn — only the structured log line surfaces.
        """
        try:
            await self._bot.send_message(chat_id, text)
        except Exception as exc:  # never crash the aggregator
            log.warning(
                "media_group_error_reply_failed",
                chat_id=chat_id,
                error=repr(exc),
            )

    async def _dispatch_image_turn(
        self,
        *,
        chat_id: int,
        message_id: int,
        paths: list[Path],
        caption: str,
        original_filename: str | None,
        kind: AttachmentKind = "jpg",
    ) -> None:
        """Common path for single-photo + media-group flush.

        Builds an :class:`IncomingMessage` with the vision-style
        attachment fields populated and runs it through the handler.
        Reply is split + sent.

        F12: ``original_filename`` carries the user-supplied filename
        for image-as-document uploads (e.g. ``IMG_1234.heic``). For
        ``F.photo`` inline + media-group paths, no original filename
        exists — the synthetic ``<uuid>__photo_<msg_id>.jpg`` from
        ``paths[0]`` is used.
        """
        if self._handler is None:
            for p in paths:
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)
            return

        first_path = paths[0]
        # IncomingMessage invariant: ``attachment`` reflects the first
        # path even when ``attachment_paths`` carries the full list,
        # so 6a-style guards on ``attachment`` still cover photo[0].
        # F12: prefer the user-supplied original filename for the
        # forensic marker so post-mortem rows show ``IMG_1234.heic``
        # rather than the uuid-prefixed tmp synthetic.
        attachment_filename = (
            original_filename
            if original_filename and len(paths) == 1
            else first_path.name
        )
        incoming = IncomingMessage(
            chat_id=chat_id,
            message_id=message_id,
            text=caption,
            attachment=first_path,
            attachment_kind=kind,
            attachment_filename=attachment_filename,
            attachment_paths=list(paths) if len(paths) > 1 else None,
        )

        chunks: list[str] = []

        async def emit(text: str) -> None:
            chunks.append(text)

        async with ChatActionSender.typing(bot=self._bot, chat_id=chat_id):
            await self._handler.handle(incoming, emit)

        full = "".join(chunks).strip() or "(пустой ответ)"
        for part in _split_for_telegram(full, limit=TELEGRAM_MSG_LIMIT):
            await self._bot.send_message(chat_id, part)

    # ------------------------------------------------------------------
    # Phase 6c: voice / audio / URL transcription
    # ------------------------------------------------------------------
    @staticmethod
    def _format_initial_ack(duration_sec: int, *, source: str) -> str:
        """Build the Russian "⏳ получил…" reply sent BEFORE the lock.

        ETA is ``max(1, duration // 4)`` seconds at 4× realtime on M4
        (research RQ1). For URL extraction we don't know the audio
        duration up front, so the ack omits the number.

        F15 (fix-pack): when the source is ``audio`` and duration is
        unknown (audio-document route — Telegram doesn't expose
        duration metadata for arbitrary documents), emit a clean
        "длительность определяю на сервере" string instead of the
        broken "⏳ получил аудио 0:00, начинаю транскрибацию (~1 мин)".
        """
        if source == "url":
            return (
                "⏳ получил ссылку, скачиваю аудио и транскрибирую "
                "(~5-15 мин для длинного видео)"
            )
        if source == "audio" and duration_sec <= 0:
            return (
                "⏳ получил аудио, начинаю транскрибацию "
                "(длительность определяю на сервере)"
            )
        h, rem = divmod(max(0, duration_sec), 3600)
        m, s = divmod(rem, 60)
        dur = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        eta_sec = max(int(duration_sec / 4), 5)
        eta_min = max(eta_sec // 60, 1)
        return (
            f"⏳ получил аудио {dur}, начинаю транскрибацию "
            f"(~{eta_min} мин при 4x realtime)"
        )

    async def _periodic_typing(self, chat_id: int) -> None:
        """Manual replacement for ``ChatActionSender.typing`` (H2 closure).

        Keeps the typing indicator alive across the full transcribe +
        Claude turn. Each ``send_chat_action`` call is wrapped in
        try/except — an aiogram transient (rate-limit, network blip)
        must NOT abort the loop. Cancelled in the caller's ``finally``.
        """
        while True:
            try:
                await self._bot.send_chat_action(chat_id, "typing")
            except asyncio.CancelledError:
                # Re-raise so the caller's gather/await sees the cancel.
                raise
            except Exception as exc:
                log.debug("typing_action_skip", error=repr(exc))
            await asyncio.sleep(_TYPING_REFRESH_INTERVAL_S)

    async def _send_pre_lock_ack(self, chat_id: int, ack: str) -> bool:
        """Send the initial ack message directly (bypass chunks).

        Returns ``True`` on success, ``False`` if the send failed —
        we still proceed with the transcribe path because losing the
        ack does NOT invalidate the audio. Logged either way.
        """
        try:
            await self._bot.send_message(chat_id, ack)
            return True
        except Exception as exc:
            log.warning("audio_pre_lock_ack_failed", error=repr(exc))
            return False

    async def _ensure_sidecar_health(self, message: Message) -> bool:
        """Pre-flight Mac sidecar health check before per-chat lock.

        Returns ``True`` when the sidecar reports healthy. Otherwise
        replies in Russian and returns ``False``. NO queue, NO retry
        per spec.
        """
        if self._transcription is None or not self._transcription.enabled:
            await message.reply(
                "транскрипция временно недоступна (Mac sidecar offline), "
                "перезапиши через минуту"
            )
            return False
        if not await self._transcription.health_check():
            await message.reply(
                "транскрипция временно недоступна (Mac sidecar offline), "
                "перезапиши через минуту"
            )
            return False
        return True

    async def _on_voice(self, message: Message) -> None:
        """Phase 6c: ``F.voice`` ingestion (Telegram native voice).

        Routing contract: ``F.voice`` filter guarantees ``message.voice``
        is set; the OGG/Opus encoding is fixed by Telegram (no need to
        sniff). Synthetic filename ``<uuid>__voice_<msg_id>.ogg``.
        """
        if self._handler is None:
            log.warning("voice_received_without_handler")
            return

        voice = message.voice
        if voice is None:  # pragma: no cover — F.voice filter guarantees set
            return

        # 1. Pre-download size guard.
        file_size = voice.file_size or 0
        if file_size > TELEGRAM_DOC_MAX_BYTES:
            await message.reply(
                "голосовое больше 20 МБ — это лимит Telegram bot API"
            )
            return

        duration = voice.duration or 0
        # 2. 3-hour cap pre-flight (server-side check too, but we
        # short-circuit here to save a transcribe round-trip).
        if duration > 3 * 3600:
            await message.reply(
                "слишком длинная запись (>3 часа), разбей на части"
            )
            return

        # 3. Pre-flight Mac sidecar health.
        if not await self._ensure_sidecar_health(message):
            return

        # 4. Initial ack (BEFORE lock). H1: aiogram defaults parse_mode
        # to None at adapter init; the ack is plain text.
        await self._send_pre_lock_ack(
            message.chat.id,
            self._format_initial_ack(duration, source="voice"),
        )

        # 5. Download.
        tmp_path = await self._download_audio_to_uploads(
            message,
            file_obj=voice,
            synthetic_name=f"voice_{message.message_id}.ogg",
        )
        if tmp_path is None:
            return

        incoming = IncomingMessage(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=(message.caption or "").strip(),
            attachment=tmp_path,
            attachment_kind="ogg",
            attachment_filename=tmp_path.name,
            audio_duration=duration,
            audio_mime_type="audio/ogg",
        )
        await self._dispatch_audio_turn(message.chat.id, incoming)

    async def _on_audio(self, message: Message) -> None:
        """Phase 6c: ``F.audio`` ingestion (audio file with metadata).

        Audio attachments carry a ``file_name`` (often) and ``mime_type``
        (sometimes). Suffix is the PRIMARY signal (H3 closure); MIME is
        fallback. ffmpeg server-side handles the rest.
        """
        if self._handler is None:
            log.warning("audio_received_without_handler")
            return

        audio = message.audio
        if audio is None:  # pragma: no cover — F.audio filter guarantees set
            return

        file_size = audio.file_size or 0
        if file_size > TELEGRAM_DOC_MAX_BYTES:
            await message.reply(
                "аудио больше 20 МБ — это лимит Telegram bot API"
            )
            return

        duration = audio.duration or 0
        if duration and duration > 3 * 3600:
            await message.reply(
                "слишком длинная запись (>3 часа), разбей на части"
            )
            return

        # Suffix detection (H3 closure: filename PRIMARY, MIME fallback).
        file_name = audio.file_name or ""
        suffix = Path(file_name).suffix.lower().lstrip(".")
        mime = (audio.mime_type or "").lower()
        if suffix not in _AUDIO_SUFFIX_WHITELIST:
            # Fallback by MIME — common iPhone Voice Memo path is
            # ``audio/x-m4a`` with a missing or non-audio file_name.
            if "ogg" in mime or "opus" in mime:
                suffix = "ogg"
            elif "mp4" in mime or "m4a" in mime:
                suffix = "m4a"
            elif "mpeg" in mime or "mp3" in mime:
                suffix = "mp3"
            elif "wav" in mime:
                suffix = "wav"
            else:
                await message.reply(
                    "аудио формат не распознан; пришли ogg/mp3/m4a/wav/opus"
                )
                return

        if not await self._ensure_sidecar_health(message):
            return

        await self._send_pre_lock_ack(
            message.chat.id,
            self._format_initial_ack(duration, source="audio"),
        )

        # Synthesise upload filename. We keep the suffix because the
        # sidecar uses it for ffmpeg's input-format detection.
        synthetic = file_name or f"audio_{message.message_id}.{suffix}"
        tmp_path = await self._download_audio_to_uploads(
            message,
            file_obj=audio,
            synthetic_name=synthetic,
        )
        if tmp_path is None:
            return

        kind = cast(AttachmentKind, suffix)
        incoming = IncomingMessage(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=(message.caption or "").strip(),
            attachment=tmp_path,
            attachment_kind=kind,
            attachment_filename=file_name or tmp_path.name,
            audio_duration=duration if duration > 0 else None,
            audio_mime_type=audio.mime_type,
        )
        await self._dispatch_audio_turn(message.chat.id, incoming)

    async def _download_audio_to_uploads(
        self,
        message: Message,
        *,
        file_obj: Any,
        synthetic_name: str,
    ) -> Path | None:
        """Common download wrapper for voice / audio / audio-document.

        On success, returns the tmp Path. On any failure (timeout, too
        big, generic error) replies in Russian, cleans up the partial
        file, and returns ``None`` — caller short-circuits.
        """
        uploads_dir = self._settings.uploads_dir
        try:
            uploads_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("uploads_mkdir_failed", path=str(uploads_dir), error=repr(exc))
            await message.reply("не смог подготовить папку для файлов")
            return None

        # Filename normalisation: <uuid>__<sanitised>.<ext>
        stem = Path(synthetic_name).stem or "audio"
        safe_stem = re.sub(r"[^\w.-]", "_", stem)[:_FILENAME_MAX_LEN]
        if not safe_stem or safe_stem.strip(".") == "":
            safe_stem = "audio"
        suffix = Path(synthetic_name).suffix.lstrip(".") or "ogg"
        safe_suffix = re.sub(r"[^\w]", "", suffix)[:8] or "ogg"
        tmp_name = f"{uuid4().hex}__{safe_stem}.{safe_suffix}"
        tmp_path = uploads_dir / tmp_name

        try:
            await self._bot.download(file_obj, destination=tmp_path, timeout=90)
        except TelegramBadRequest as exc:
            log.warning(
                "telegram_audio_download_failed",
                path=str(tmp_path),
                error=str(exc),
            )
            msg_lower = str(exc).lower()
            if "too big" in msg_lower or "too large" in msg_lower:
                await message.reply(
                    "аудио больше 20 МБ — это лимит Telegram bot API"
                )
            else:
                await message.reply("не смог скачать аудио — проверь логи")
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return None
        except TimeoutError:
            log.warning(
                "telegram_audio_download_timeout",
                path=str(tmp_path),
                timeout_s=90,
            )
            await message.reply(
                "Telegram не успел отдать аудио за 90 секунд — попробуй ещё раз"
            )
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return None
        except Exception as exc:
            log.exception(
                "telegram_audio_unexpected_error",
                path=str(tmp_path),
                error_type=type(exc).__name__,
            )
            await message.reply("произошла внутренняя ошибка обработки аудио")
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return None

        return tmp_path

    async def _on_audio_document(
        self,
        message: Message,
        doc: Any,
        suffix: str,
        file_name: str,
    ) -> None:
        """Audio file via "send as file" route (e.g. iPhone Voice Memo
        export)."""
        if not await self._ensure_sidecar_health(message):
            return
        # No reliable duration on the document path — Telegram doesn't
        # surface it for arbitrary documents. Pass 0 to the ack helper;
        # the user sees "~5-15 мин" only when source=url, and a
        # placeholder otherwise.
        await self._send_pre_lock_ack(
            message.chat.id,
            self._format_initial_ack(0, source="audio"),
        )
        synthetic = file_name or f"audio_{message.message_id}.{suffix}"
        tmp_path = await self._download_audio_to_uploads(
            message,
            file_obj=doc,
            synthetic_name=synthetic,
        )
        if tmp_path is None:
            return
        kind = cast(AttachmentKind, suffix)
        # MIME hint for the sidecar; ffmpeg uses suffix anyway, but a
        # correct MIME helps server-side telemetry.
        mime_hint = (getattr(doc, "mime_type", None) or "").lower() or {
            "ogg": "audio/ogg",
            "mp3": "audio/mpeg",
            "m4a": "audio/mp4",
            "wav": "audio/wav",
            "opus": "audio/ogg",
        }.get(suffix, "application/octet-stream")
        incoming = IncomingMessage(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=(message.caption or "").strip(),
            attachment=tmp_path,
            attachment_kind=kind,
            attachment_filename=file_name or tmp_path.name,
            audio_duration=None,
            audio_mime_type=mime_hint,
        )
        await self._dispatch_audio_turn(message.chat.id, incoming)

    async def _dispatch_audio_turn(
        self,
        chat_id: int,
        incoming: IncomingMessage,
    ) -> None:
        """Run the audio turn through the standard handler with a manual
        typing loop instead of ``ChatActionSender`` (H2 closure)."""
        if self._handler is None:
            return
        chunks: list[str] = []

        async def emit(text: str) -> None:
            chunks.append(text)

        typing_task = asyncio.create_task(self._periodic_typing(chat_id))
        try:
            await self._handler.handle(incoming, emit)
        finally:
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await typing_task

        full = "".join(chunks).strip() or "(пустой ответ)"
        for part in _split_for_telegram(full, limit=TELEGRAM_MSG_LIMIT):
            await self._bot.send_message(chat_id, part)

    async def _on_animation(self, message: Message) -> None:
        """Phase 6b (F2 / AC#5): dedicated handler for animated GIFs and
        other ``F.animation`` updates.

        Animations route here BEFORE ``_on_document`` so the spec'd
        Russian "анимации не поддерживаю" reply lands instead of the
        generic catch-all (which would otherwise advertise the wrong
        capability).
        """
        log.info("animation_rejected")
        await message.reply("анимации не поддерживаю — пришли картинку")

    async def _on_non_text(self, message: Message) -> None:
        log.info("non_text_rejected", content_type=message.content_type)
        # F2: prior copy claimed "это будет в phase 6" — phase 6 has
        # shipped, so the stale message is replaced with a sanitised
        # capability list. Keeps owner expectations aligned with the
        # registered handlers.
        await message.answer(
            "этот тип медиа пока не поддерживаю — пришли текст, фото или файл"
        )

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
        # Phase 6b: cancel pending media-group buckets first so the
        # aggregator doesn't try to flush + dispatch a turn while the
        # daemon tears down.
        with contextlib.suppress(Exception):
            await self._media_group.cancel_all()
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
