from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import FSInputFile, Message, TelegramObject, Update
from aiogram.utils.chat_action import ChatActionSender

from assistant.adapters.base import (
    Handler,
    IncomingMessage,
    MediaAttachment,
    MediaKind,
    MessengerAdapter,
)
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.media.download import SizeCapExceeded, download_telegram_file
from assistant.media.paths import inbox_dir

log = get_logger("adapters.telegram")

TELEGRAM_LIMIT = 4096
# Wave-2 G-W2-1: retry a `TelegramRetryAfter` up to this many times per
# message part before giving up. 2 retries x typical 3 s rate-limit window
# is a full 6-second buffer — good enough for bursts from scheduler + user
# overlapping without turning into a turn-burn.
TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS = 2

# Phase 7 / Wave 7A (C-6, I-7.6) — attachment-ingress dedup window.
# An IncomingMessage carrying the same `(chat_id, local_path)` within
# this window is dropped (log.debug) rather than re-dispatched to the
# handler. 60 s balances two concurrent pressures: (a) Telegram
# media_group regrouping + `edited_message` replays can fire the same
# file_id within a few seconds, so a small TTL would miss them; (b)
# any longer and we could mask a genuine re-upload of the same file
# that the user intended as a new attachment. 128 entries is a
# per-adapter cap (single-owner bot — never more than a handful in
# flight); OrderedDict FIFO eviction trims the oldest entry when full
# even if still within TTL, which is acceptable because the older the
# key the more likely it's past TTL already.
_ATTACHMENT_DEDUP_TTL_S: float = 60.0
_ATTACHMENT_DEDUP_MAX_ENTRIES: int = 128

# Phase 7 / Wave 7A (L-20, L-21) — send_photo/document/audio retry
# budgets. RetryAfter gets a single extra attempt (total 2) because
# Telegram flood-waits are infrequent for outbound media and a second
# 429 in a row is almost always a signal to back off rather than
# retry further — the caller (dispatch_reply) already swallows the
# re-raise. NetworkError gets two extra attempts (total 3) with
# exponential backoff because transient DNS/connection resets are
# more common and worth a slightly longer retry window.
_SEND_RETRY_AFTER_MAX_RETRIES: int = 1
_SEND_NETWORK_MAX_RETRIES: int = 2


def _load_max_retry_after_s() -> int:
    """Fix-pack HIGH #3: cap the server-advised `retry_after` value.

    A Telegram 429 can technically request minutes or hours of backoff.
    Sleeping that long inside `send_text` would block the scheduler
    dispatcher (which awaits the send synchronously) and starve the
    entire bot on a flood-wait burst. The cap defaults to 30 s; a
    server advisory above the cap raises `TelegramRetryAfter` out of
    `send_text` so the caller (scheduler: mark_pending_retry; user
    path: the handler's except) gets to decide the recovery.

    Overridable via `TELEGRAM_MAX_RETRY_AFTER_S` for integration
    testing where the cap needs tightening (e.g. sub-second bursts).
    """
    raw = os.environ.get("TELEGRAM_MAX_RETRY_AFTER_S", "30")
    try:
        value = int(raw)
    except ValueError:
        log.warning("telegram_max_retry_after_s_invalid", raw=raw)
        return 30
    return max(1, value)


TELEGRAM_MAX_RETRY_AFTER_S: int = _load_max_retry_after_s()


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

        # Phase 7 / Wave 7A (C-6, I-7.6) — attachment-ingress dedup.
        # Keys: `(chat_id, str(local_path))`. Values: `time.monotonic()`
        # of first ingress. OrderedDict preserves insertion order so we
        # can pop the oldest entry when the 128-entry cap is reached.
        # Monotonic clock (not wall-clock) because NTP / DST jumps would
        # otherwise break TTL reasoning.
        self._emitted_attachments: OrderedDict[tuple[int, str], float] = OrderedDict()

        self._dp.update.outer_middleware.register(self._log_non_owner_middleware)

        self._dp.message.filter(F.chat.id == settings.owner_chat_id)
        self._dp.message.register(self._on_text, F.text)
        # Phase 7 media handlers — registered BEFORE the generic
        # `_on_non_text` fallback so each media kind matches its own
        # specific handler. aiogram's router walks registration order
        # and short-circuits on first match.
        self._dp.message.register(self._on_voice, F.voice)
        self._dp.message.register(self._on_audio, F.audio)
        self._dp.message.register(self._on_photo, F.photo)
        self._dp.message.register(self._on_document, F.document)
        self._dp.message.register(self._on_video_note, F.video_note)
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
        await self._dispatch_to_handler(message.chat.id, incoming)

    async def _on_non_text(self, message: Message) -> None:
        log.info("non_text_rejected", content_type=message.content_type)
        await message.answer("Пока принимаю только текст.")

    # ------------------------------------------------------------------
    # Phase 7 / Wave 7A media ingress handlers (commit 12, I-7.6, #13)
    # ------------------------------------------------------------------

    async def _on_voice(self, message: Message) -> None:
        voice = message.voice
        if voice is None:  # defensive — F.voice filter guarantees it
            return
        max_bytes = self._settings.media.voice_max_bytes
        if voice.file_size is not None and voice.file_size > max_bytes:
            await self._reject_oversize(message, "voice", voice.file_size, max_bytes)
            return
        att = await self._ingest_media(
            message,
            kind="voice",
            file_id=voice.file_id,
            suggested_filename=f"voice_{voice.file_id}.ogg",
            max_bytes=max_bytes,
            build=lambda local_path: MediaAttachment(
                kind="voice",
                local_path=local_path,
                mime_type=voice.mime_type,
                file_size=voice.file_size,
                duration_s=voice.duration,
                telegram_file_id=voice.file_id,
            ),
        )
        if att is not None:
            await self._emit_attachment(message, att)

    async def _on_audio(self, message: Message) -> None:
        audio = message.audio
        if audio is None:
            return
        max_bytes = self._settings.media.audio_max_bytes
        if audio.file_size is not None and audio.file_size > max_bytes:
            await self._reject_oversize(message, "audio", audio.file_size, max_bytes)
            return
        # Prefer the Telegram-provided filename for extension recovery.
        suggested = audio.file_name or f"audio_{audio.file_id}.mp3"
        att = await self._ingest_media(
            message,
            kind="audio",
            file_id=audio.file_id,
            suggested_filename=suggested,
            max_bytes=max_bytes,
            build=lambda local_path: MediaAttachment(
                kind="audio",
                local_path=local_path,
                mime_type=audio.mime_type,
                file_size=audio.file_size,
                duration_s=audio.duration,
                filename_original=audio.file_name,
                telegram_file_id=audio.file_id,
            ),
        )
        if att is not None:
            await self._emit_attachment(message, att)

    async def _on_photo(self, message: Message) -> None:
        # Telegram delivers a PhotoSize list — select the largest
        # variant (the last entry is the highest-resolution per the
        # Bot API contract). On the unlikely empty-list case, bail.
        if not message.photo:
            return
        photo = message.photo[-1]
        max_bytes = self._settings.media.photo_download_max_bytes
        if photo.file_size is not None and photo.file_size > max_bytes:
            await self._reject_oversize(message, "photo", photo.file_size, max_bytes)
            return
        att = await self._ingest_media(
            message,
            kind="photo",
            file_id=photo.file_id,
            suggested_filename=f"photo_{photo.file_id}.jpg",
            max_bytes=max_bytes,
            build=lambda local_path: MediaAttachment(
                kind="photo",
                local_path=local_path,
                mime_type="image/jpeg",  # Bot API always returns JPEG
                file_size=photo.file_size,
                width=photo.width,
                height=photo.height,
                telegram_file_id=photo.file_id,
            ),
        )
        if att is not None:
            await self._emit_attachment(message, att)

    async def _on_document(self, message: Message) -> None:
        document = message.document
        if document is None:
            return
        max_bytes = self._settings.media.document_max_bytes
        if document.file_size is not None and document.file_size > max_bytes:
            await self._reject_oversize(message, "document", document.file_size, max_bytes)
            return
        suggested = document.file_name or f"doc_{document.file_id}.bin"
        att = await self._ingest_media(
            message,
            kind="document",
            file_id=document.file_id,
            suggested_filename=suggested,
            max_bytes=max_bytes,
            build=lambda local_path: MediaAttachment(
                kind="document",
                local_path=local_path,
                mime_type=document.mime_type,
                file_size=document.file_size,
                filename_original=document.file_name,
                telegram_file_id=document.file_id,
            ),
        )
        if att is not None:
            await self._emit_attachment(message, att)

    async def _on_video_note(self, message: Message) -> None:
        video_note = message.video_note
        if video_note is None:
            return
        # video_note has no configured cap in MediaSettings (phase 7
        # treats video as out-of-scope per handler §3.1 note);
        # fall back to the document cap so we still enforce SOMETHING.
        # The handler will emit a "video out of scope" note anyway.
        max_bytes = self._settings.media.document_max_bytes
        if video_note.file_size is not None and video_note.file_size > max_bytes:
            await self._reject_oversize(message, "video_note", video_note.file_size, max_bytes)
            return
        att = await self._ingest_media(
            message,
            kind="video_note",
            file_id=video_note.file_id,
            suggested_filename=f"vnote_{video_note.file_id}.mp4",
            max_bytes=max_bytes,
            build=lambda local_path: MediaAttachment(
                kind="video_note",
                local_path=local_path,
                mime_type="video/mp4",
                file_size=video_note.file_size,
                duration_s=video_note.duration,
                telegram_file_id=video_note.file_id,
            ),
        )
        if att is not None:
            await self._emit_attachment(message, att)

    async def _reject_oversize(
        self,
        message: Message,
        kind: str,
        file_size: int,
        cap: int,
    ) -> None:
        """Reject an oversize inbound media file with a user-facing message.

        Kept as a dedicated helper so the five kind-specific handlers
        share one code path for the "file too large" UX and we do not
        accidentally log at different severities.
        """
        log.info(
            "media_rejected_oversize",
            chat_id=message.chat.id,
            kind=kind,
            file_size=file_size,
            cap=cap,
        )
        await message.answer("Файл слишком большой.")

    async def _ingest_media(
        self,
        message: Message,
        *,
        kind: MediaKind,
        file_id: str,
        suggested_filename: str,
        max_bytes: int,
        build: Callable[[Path], MediaAttachment],
    ) -> MediaAttachment | None:
        """Shared download + MediaAttachment construction path.

        Returns the built `MediaAttachment` on success, or `None` when
        the download was rejected (oversize mid-stream) or failed for
        another reason. The caller's `None`-branch is silent — the
        user-facing error message is emitted from inside this helper
        so every kind-specific handler produces consistent UX.

        Concurrency note: `download_telegram_file` is awaited inside
        the aiogram handler task. Cancellation (e.g. adapter.stop())
        propagates cleanly — the download helper's partial-file
        unlink runs in its own `except` branch and re-raises
        `CancelledError` uninterrupted.
        """
        try:
            local_path = await download_telegram_file(
                self._bot,
                file_id,
                inbox_dir(self._settings.data_dir),
                suggested_filename,
                max_bytes=max_bytes,
            )
        except SizeCapExceeded as exc:
            log.info(
                "media_download_cap_exceeded",
                chat_id=message.chat.id,
                kind=kind,
                cap=exc.cap,
                received=exc.received,
            )
            await message.answer("Файл слишком большой.")
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning(
                "media_download_failed",
                chat_id=message.chat.id,
                kind=kind,
                file_id=file_id,
                exc_info=True,
            )
            # The Russian message starts with a Cyrillic character
            # whose glyph overlaps a Latin letter; keep the Cyrillic
            # spelling since the rest of the string is Cyrillic too.
            # `noqa: RUF001` acknowledges the known visual ambiguity.
            await message.answer("Не удалось скачать файл.")  # noqa: RUF001
            return None
        return build(local_path)

    def _evict_expired_attachments(self, now: float) -> None:
        """Drop dedup entries older than `_ATTACHMENT_DEDUP_TTL_S`.

        Called BEFORE the membership check in `_is_duplicate_attachment`.
        Because `OrderedDict` preserves insertion order and entries are
        inserted in monotonic-clock order, we can stop scanning at the
        first entry still within TTL — so the amortised cost per
        incoming attachment is O(1) under steady-state traffic.
        """
        cutoff = now - _ATTACHMENT_DEDUP_TTL_S
        while self._emitted_attachments:
            key, ts = next(iter(self._emitted_attachments.items()))
            if ts > cutoff:
                break
            del self._emitted_attachments[key]

    def _is_duplicate_attachment(self, chat_id: int, local_path: Path) -> bool:
        """Return True iff `(chat_id, local_path)` was already emitted.

        Side effects on False return:
          * Caller is expected to record the new key via
            `_record_attachment`. Kept as two calls so the log-point
            fires with an accurate "last_seen" timestamp before the
            OrderedDict is mutated.

        Side effects on True return: none. The adapter drops the
        emit; no memory churn.
        """
        now = time.monotonic()
        self._evict_expired_attachments(now)
        key = (chat_id, str(local_path))
        if key in self._emitted_attachments:
            log.debug(
                "attachment_dedup",
                chat_id=chat_id,
                path=str(local_path),
                last_seen=self._emitted_attachments[key],
            )
            return True
        return False

    def _record_attachment(self, chat_id: int, local_path: Path) -> None:
        """Record `(chat_id, local_path)` as emitted (post-dedup-check).

        Trims the dedup map back to the 128-entry cap via FIFO
        eviction of the oldest key when full. Split from
        `_is_duplicate_attachment` to keep the read + write phases
        explicit — callers should check, then decide to handle, then
        record.
        """
        now = time.monotonic()
        key = (chat_id, str(local_path))
        self._emitted_attachments[key] = now
        # Enforce LRU cap after insertion so the current key is never
        # the eviction victim of its own insertion.
        while len(self._emitted_attachments) > _ATTACHMENT_DEDUP_MAX_ENTRIES:
            self._emitted_attachments.popitem(last=False)

    async def _emit_attachment(self, message: Message, attachment: MediaAttachment) -> None:
        """Build the IncomingMessage envelope and dispatch.

        Applies the attachment-ingress dedup invariant (I-7.6) BEFORE
        invoking the handler — a duplicate `(chat_id, local_path)`
        inside the 60 s window is silently dropped.
        """
        if self._is_duplicate_attachment(message.chat.id, attachment.local_path):
            return
        self._record_attachment(message.chat.id, attachment.local_path)

        if self._handler is None:
            log.warning(
                "media_received_without_handler",
                chat_id=message.chat.id,
                kind=attachment.kind,
            )
            return

        incoming = IncomingMessage(
            chat_id=message.chat.id,
            # Prefer a caption if present; otherwise empty so the
            # handler sees an attachment-only turn. Claude still needs
            # SOME text block (handler supplies a default), so an
            # empty string is fine.
            text=message.caption or "",
            message_id=message.message_id,
            origin="telegram",
            attachments=(attachment,),
        )
        await self._dispatch_to_handler(message.chat.id, incoming)

    async def _dispatch_to_handler(self, chat_id: int, incoming: IncomingMessage) -> None:
        """Shared handler-invocation path for text + media turns.

        Factors the emit-collect + typing-action + send_text tail out
        of the five media handlers so the retry-wrapper semantics of
        `send_text` apply uniformly across all ingress paths.
        """
        assert self._handler is not None  # caller guarantees
        chunks: list[str] = []

        async def emit(text: str) -> None:
            chunks.append(text)

        async with ChatActionSender.typing(bot=self._bot, chat_id=chat_id):
            await self._handler.handle(incoming, emit)

        full = "".join(chunks).strip()
        if not full:
            log.info("empty_reply_skipped", chat_id=chat_id)
            return
        await self.send_text(chat_id, full)

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

        Fix-pack HIGH #3: if Telegram advises a retry-after BEYOND
        `TELEGRAM_MAX_RETRY_AFTER_S`, we do NOT sleep and re-raise the
        exception so the caller decides. Scheduler dispatcher's
        mark_pending_retry path then owns the delivery-later choice;
        the user _on_text path lets the handler surface the error.
        """
        max_retry_after_s = TELEGRAM_MAX_RETRY_AFTER_S
        for part in split_for_telegram(text):
            for attempt in range(TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS + 1):
                try:
                    await self._bot.send_message(chat_id=chat_id, text=part)
                    break
                except TelegramRetryAfter as exc:
                    if exc.retry_after > max_retry_after_s:
                        log.warning(
                            "telegram_retry_after_over_cap",
                            chat_id=chat_id,
                            retry_after=exc.retry_after,
                            cap_s=max_retry_after_s,
                        )
                        raise
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

    # ------------------------------------------------------------------
    # Phase 7 / Wave 7A (commit 12) — out-of-band media delivery.
    # ------------------------------------------------------------------
    #
    # Each `send_*` implementation mirrors the `send_text` retry shape,
    # extended for the two transient-error classes (L-20, L-21, #13):
    #
    #   * `TelegramRetryAfter`: honour `retry_after`, retry ONCE. The
    #     single-retry budget is deliberately tight — the caller
    #     (dispatch_reply) swallows the re-raise and continues with the
    #     cleaned text, so a 429 storm should not block subsequent
    #     artefact sends or the text tail.
    #
    #   * `TelegramNetworkError`: exponential backoff `(1, 2, 4)` s,
    #     retry TWICE then re-raise. Backoff base is 1 s so the worst-
    #     case total wait is 3 s — inside the user-visible responsiveness
    #     budget.
    #
    #   * `(FileNotFoundError, PermissionError, OSError)`: NOT retryable.
    #     Log at `warning` level with stack, re-raise so
    #     dispatch_reply can classify as `artefact_send_failed` and move
    #     on. This mirrors the §3.4 fix-pack spec (L-20).
    #
    # DRY: the three methods share `_send_media_with_retry` which
    # accepts the aiogram `send_*` coroutine factory as a callable.
    async def send_photo(self, chat_id: int, path: Path, *, caption: str | None = None) -> None:
        await self._send_media_with_retry(
            "photo",
            chat_id,
            path,
            caption,
            send_fn=lambda fs_input: self._bot.send_photo(
                chat_id=chat_id, photo=fs_input, caption=caption
            ),
        )

    async def send_document(self, chat_id: int, path: Path, *, caption: str | None = None) -> None:
        await self._send_media_with_retry(
            "document",
            chat_id,
            path,
            caption,
            send_fn=lambda fs_input: self._bot.send_document(
                chat_id=chat_id, document=fs_input, caption=caption
            ),
        )

    async def send_audio(self, chat_id: int, path: Path, *, caption: str | None = None) -> None:
        await self._send_media_with_retry(
            "audio",
            chat_id,
            path,
            caption,
            send_fn=lambda fs_input: self._bot.send_audio(
                chat_id=chat_id, audio=fs_input, caption=caption
            ),
        )

    async def _send_media_with_retry(
        self,
        kind: str,
        chat_id: int,
        path: Path,
        caption: str | None,
        *,
        send_fn: Callable[[FSInputFile], Awaitable[Any]],
    ) -> None:
        """Run `send_fn` with RetryAfter + NetworkError retry policies.

        See class-level comment above for the three branches. `kind` is
        used purely for structured-log keying (`send_photo_retry_after`,
        `send_document_read_failed`, etc.) so operators can grep by
        artefact kind.
        """
        # File-read errors surface BEFORE the retry loop: FSInputFile
        # defers open() until the aiohttp body is built, but catching
        # at the send-call site (instead of pre-flight stat()) means
        # we correctly surface PermissionError / race-condition deletes
        # rather than racing against the sweeper ourselves.

        retry_after_attempts = 0
        network_attempts = 0
        max_retry_after_s = TELEGRAM_MAX_RETRY_AFTER_S

        while True:
            try:
                await send_fn(FSInputFile(path))
                return
            except TelegramRetryAfter as exc:
                if exc.retry_after > max_retry_after_s:
                    log.warning(
                        f"send_{kind}_retry_after_over_cap",
                        chat_id=chat_id,
                        path=str(path),
                        retry_after=exc.retry_after,
                        cap_s=max_retry_after_s,
                    )
                    raise
                if retry_after_attempts >= _SEND_RETRY_AFTER_MAX_RETRIES:
                    log.warning(
                        f"send_{kind}_retry_after_exhausted",
                        chat_id=chat_id,
                        path=str(path),
                        retry_after=exc.retry_after,
                        attempts=retry_after_attempts + 1,
                    )
                    raise
                log.info(
                    f"send_{kind}_retry_after",
                    chat_id=chat_id,
                    path=str(path),
                    retry_after=exc.retry_after,
                    attempt=retry_after_attempts + 1,
                )
                await asyncio.sleep(exc.retry_after + 1)
                retry_after_attempts += 1
            except TelegramNetworkError as exc:
                if network_attempts >= _SEND_NETWORK_MAX_RETRIES:
                    log.warning(
                        f"send_{kind}_network_error_exhausted",
                        chat_id=chat_id,
                        path=str(path),
                        error=str(exc),
                        attempts=network_attempts + 1,
                    )
                    raise
                # Exponential backoff: 1 s, 2 s (two retries).
                backoff = 2**network_attempts
                log.info(
                    f"send_{kind}_network_error",
                    chat_id=chat_id,
                    path=str(path),
                    error=str(exc),
                    attempt=network_attempts + 1,
                    backoff_s=backoff,
                )
                await asyncio.sleep(backoff)
                network_attempts += 1
            except (FileNotFoundError, PermissionError, OSError):
                # L-20: file-read failures are NOT retryable. Log with
                # stack + re-raise so dispatch_reply can classify the
                # failure and keep going with the remaining artefacts.
                # Caption is included in the log so the operator can
                # correlate with the intended reply; caption itself is
                # not a secret.
                log.warning(
                    f"send_{kind}_read_failed",
                    chat_id=chat_id,
                    path=str(path),
                    caption=caption,
                    exc_info=True,
                )
                raise
