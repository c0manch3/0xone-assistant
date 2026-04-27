"""Phase 6b — per-chat media-group aggregator.

Telegram delivers album messages as separate ``Message`` updates with a
shared ``media_group_id`` and no ordering guarantee. aiogram 3 has no
built-in aggregator; community libraries (``aiogram-media-group``,
``aiogram-mediagroup-handle``) use a non-resetting debounce + race-
prone shared dict (research.md RQ3).

Hand-rolled adapter-level aggregator with:

1. **Resetting** debounce: each new arrival cancels the pending flush
   and re-arms the timer.
2. Per-bucket ``asyncio.Lock`` so the flush coroutine and ``add``
   serialise on the same bucket.
3. Hard cap ``MAX_PHOTOS_PER_TURN`` — overflow photos drop with a
   one-shot Russian reply per group (best-effort tmp cleanup).
4. ``flush_for_chat`` external trigger: text arrival for the same
   chat preempts pending photo windows.
5. In-memory only — daemon restart wipes pending state, ``boot_sweep``
   removes orphan tmp files.

Race contract:

* ``add`` mutates ``Bucket.photos`` only inside ``Bucket.lock``.
* ``_do_flush`` pops the bucket from the registry under the registry
  lock, then drains ``photos`` under ``Bucket.lock``. A 6th photo
  arriving while the flush coroutine is mid-callback either landed in
  the bucket pre-flush (gets flushed) or arrives after the bucket is
  popped (starts a fresh bucket — two adjacent groups, two adjacent
  turns).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assistant.logger import get_logger

log = get_logger("adapters.media_group")

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

DEBOUNCE_SEC: float = 1.5
MAX_PHOTOS_PER_TURN: int = 5

# Callback signatures. We use ``Coroutine`` (not ``Awaitable``) because
# the aggregator wraps callbacks in ``asyncio.create_task`` for the
# overflow path, and ``create_task`` is annotated against
# ``Coroutine[Any, Any, T]``.
FlushCallback = Callable[
    [int, str, list[Path], str, int], Coroutine[Any, Any, None]
]
"""(chat_id, media_group_id, paths, caption, first_message_id) → coroutine.

``first_message_id`` is the Telegram ``message_id`` of the FIRST photo
that arrived in the bucket (devil M5 F13). Allows downstream
``IncomingMessage.message_id`` to carry a real id rather than ``0``.
"""

OverflowCallback = Callable[[int], Coroutine[Any, Any, None]]
"""(chat_id) → coroutine. Single Russian reply per group."""

ErrorReplyCallback = Callable[[int, str], Coroutine[Any, Any, None]]
"""(chat_id, russian_text) → coroutine. F4: invoked when ``flush_cb``
raises so the owner is not left silent on a bridge / scheduler error.
"""


@dataclass
class _Bucket:
    chat_id: int
    group_id: str
    photos: list[Path] = field(default_factory=list)
    caption: str = ""
    # F13: ``message_id`` of the FIRST photo in the bucket. ``0`` means
    # the bucket has not received any photos yet (defensive — a fresh
    # bucket without any ``add()`` should never reach ``_do_flush``).
    first_message_id: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    flush_task: asyncio.Task[None] | None = None
    overflow_notified: bool = False


class MediaGroupAggregator:
    """Per-chat media_group debouncer; one instance per adapter.

    Lifecycle:

    * Construct in ``TelegramAdapter.__init__`` with the flush + overflow
      coroutines bound to the adapter (so they have access to ``_bot``,
      ``_handler``, etc.).
    * No explicit ``stop()`` is required — pending tasks are cancelled
      when the event loop shuts down. The adapter's ``stop()`` may
      optionally call :meth:`cancel_all` to drop pending buckets early.
    """

    def __init__(
        self,
        flush_cb: FlushCallback,
        overflow_cb: OverflowCallback,
        *,
        error_reply_cb: ErrorReplyCallback | None = None,
        debounce_sec: float = DEBOUNCE_SEC,
        max_photos: int = MAX_PHOTOS_PER_TURN,
    ) -> None:
        self._flush_cb = flush_cb
        self._overflow_cb = overflow_cb
        self._error_reply_cb = error_reply_cb
        self._debounce_sec = debounce_sec
        self._max_photos = max_photos
        self._buckets: dict[tuple[int, str], _Bucket] = {}
        # Mutated only when adding/removing a bucket from the registry.
        self._buckets_lock = asyncio.Lock()
        # Strong references to overflow callbacks so the GC does not
        # collect them mid-flight (NH-5: ``asyncio.create_task`` orphan).
        self._overflow_tasks: set[asyncio.Task[None]] = set()

    @property
    def max_photos(self) -> int:
        return self._max_photos

    async def add(
        self,
        chat_id: int,
        group_id: str,
        photo_path: Path,
        caption: str,
        message_id: int = 0,
    ) -> None:
        """Append a downloaded photo to its group's bucket.

        Resets the debounce timer. Overflow (>= ``max_photos``) drops
        the photo with one-shot Russian reply per group + tmp cleanup.

        ``message_id`` (F13) is the Telegram ``message_id`` of the
        photo update; the FIRST photo's id is captured into
        ``Bucket.first_message_id`` for the flush callback.
        """
        key = (chat_id, group_id)
        async with self._buckets_lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(chat_id=chat_id, group_id=group_id)
                self._buckets[key] = bucket

        async with bucket.lock:
            if len(bucket.photos) >= self._max_photos:
                # 6th+ photo within the window — overflow.
                if not bucket.overflow_notified:
                    bucket.overflow_notified = True
                    overflow_task: asyncio.Task[None] = asyncio.create_task(
                        self._overflow_cb(chat_id),
                        name=f"media_group_overflow_{chat_id}",
                    )
                    self._overflow_tasks.add(overflow_task)
                    overflow_task.add_done_callback(
                        self._overflow_tasks.discard
                    )
                # Cleanup the rejected tmp file. Best-effort.
                with contextlib.suppress(OSError):
                    photo_path.unlink(missing_ok=True)
                log.info(
                    "media_group_overflow_dropped",
                    chat_id=chat_id,
                    group_id=group_id,
                    cap=self._max_photos,
                )
                return

            bucket.photos.append(photo_path)
            # F13: capture the FIRST photo's message_id (album delivery
            # order is not guaranteed, but ``first_message_id`` reflects
            # whichever message arrived in the bucket first).
            if bucket.first_message_id == 0 and message_id:
                bucket.first_message_id = message_id
            # Telegram attaches the caption to ONE message in the
            # album, but delivery order is not guaranteed; first
            # non-empty caption wins.
            if caption and not bucket.caption:
                bucket.caption = caption

            # Reset debounce: cancel the pending flush + re-arm.
            if bucket.flush_task is not None:
                bucket.flush_task.cancel()
            bucket.flush_task = asyncio.create_task(
                self._delayed_flush(key),
                name=f"media_group_flush_{chat_id}_{group_id}",
            )

    async def flush_for_chat(self, chat_id: int) -> None:
        """External flush trigger.

        Called by ``_on_text`` when the owner sends a text message —
        pending photo windows for the same chat preempt so the vision
        turn doesn't block the text turn.
        """
        keys = [k for k in list(self._buckets) if k[0] == chat_id]
        for key in keys:
            await self._do_flush(key)

    async def cancel_all(self) -> None:
        """Cancel pending flush tasks + drop all buckets.

        Used by adapter ``stop()`` for clean shutdown. Tmp files
        accumulated in pending buckets are NOT explicitly removed —
        ``boot_sweep`` wipes them on the next daemon start.
        """
        async with self._buckets_lock:
            keys = list(self._buckets)
            for key in keys:
                bucket = self._buckets.pop(key, None)
                if bucket is None:
                    continue
                if bucket.flush_task is not None:
                    bucket.flush_task.cancel()
        for task in list(self._overflow_tasks):
            task.cancel()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _delayed_flush(self, key: tuple[int, str]) -> None:
        try:
            await asyncio.sleep(self._debounce_sec)
        except asyncio.CancelledError:
            return  # debounce reset by another arrival
        await self._do_flush(key)

    async def _do_flush(self, key: tuple[int, str]) -> None:
        async with self._buckets_lock:
            bucket = self._buckets.pop(key, None)
        if bucket is None:
            return  # already flushed by parallel path
        async with bucket.lock:
            if not bucket.photos:
                return
            paths = list(bucket.photos)
            caption = bucket.caption
            first_message_id = bucket.first_message_id
            bucket.photos.clear()

        # Cancel our own pending flush_task — when ``_do_flush`` is
        # called via ``flush_for_chat`` while the debounce timer is
        # pending, leaving the task running would race against this
        # path.
        #
        # CRITICAL (devil-w2 F1): ``_do_flush`` is reachable from two
        # paths — ``_delayed_flush`` (debounce-fired) AND
        # ``flush_for_chat`` (text pre-emption). On the debounce path,
        # ``bucket.flush_task`` IS the currently-running task; calling
        # ``.cancel()`` on it schedules CancelledError on self. The
        # next ``await self._flush_cb(...)`` would enter the handler
        # and the first internal yield (e.g. ``bot.send_chat_action``)
        # would raise CancelledError. The owner sees no Telegram reply
        # and tmp files leak until boot-sweep. Guard: only cancel when
        # the pending task is not us.
        pending = bucket.flush_task
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if pending is not None and pending is not current:
            pending.cancel()

        try:
            await self._flush_cb(
                bucket.chat_id,
                bucket.group_id,
                paths,
                caption,
                first_message_id,
            )
        except Exception as exc:  # surfaces in adapter; never re-raise
            log.exception(
                "media_group_flush_callback_failed",
                chat_id=bucket.chat_id,
                group_id=bucket.group_id,
                photos=len(paths),
                error=repr(exc),
            )
            # F4: surface a sanitised Russian "internal error" reply
            # so the owner is not left without feedback when the
            # flush callback raises (bridge error, scheduler-origin
            # exception, etc.).
            if self._error_reply_cb is not None:
                with contextlib.suppress(Exception):
                    await self._error_reply_cb(
                        bucket.chat_id,
                        "произошла внутренняя ошибка обработки фото",
                    )
            # Best-effort: clean tmp files so a flush-callback bug
            # doesn't leave orphans on disk.
            for p in paths:
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)
