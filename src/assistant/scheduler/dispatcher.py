"""Consumer half of the scheduler: pops :class:`ScheduledTrigger`
items from the queue, assembles the scheduler-origin
:class:`IncomingMessage`, and drives it through :class:`ClaudeHandler`.

Protection layers wired here:
  - LRU dedup (256 slots) — catches post-crash duplicates when
    :meth:`SchedulerStore.clean_slate_sent` replays a trigger that
    actually DID fire before the crash. 256 ≈ 4x max(in-flight)=64+1.
  - Re-check ``schedules.enabled`` — the model can disable mid-tick;
    we don't want to deliver a prompt that the model already
    countermanded.
  - CR2.2: read the prompt from the ``triggers`` row (immutable
    per-fire snapshot), NOT from the ``schedules`` row. The loop
    already copied ``schedules.prompt`` into ``triggers.prompt`` at
    materialisation time, so this is the dispatcher's defence against
    an in-flight ``UPDATE schedules SET prompt=...``.
  - CR-3 dispatch-time wrap — the final step before building the
    ``IncomingMessage`` fires :func:`wrap_scheduler_prompt` to give
    the model a fresh per-trigger nonce envelope.
"""

from __future__ import annotations

import asyncio
import collections

import structlog

from assistant.adapters.base import IncomingMessage, MessengerAdapter
from assistant.config import Settings
from assistant.handlers.message import ClaudeHandler
from assistant.scheduler.loop import ScheduledTrigger
from assistant.scheduler.store import SchedulerStore
from assistant.tools_sdk._scheduler_core import wrap_scheduler_prompt

_log = structlog.get_logger(__name__)


class SchedulerDispatcher:
    def __init__(
        self,
        *,
        queue: asyncio.Queue[ScheduledTrigger],
        store: SchedulerStore,
        handler: ClaudeHandler,
        adapter: MessengerAdapter,
        owner_chat_id: int,
        settings: Settings,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self._q = queue
        self._store = store
        self._handler = handler
        self._adapter = adapter
        self._owner = owner_chat_id
        self._settings = settings
        self._stop = stop_event or asyncio.Event()
        # 256 = 4x max in-flight (64+1). Insertion-ordered so we can
        # pop the oldest in O(1).
        self._lru: collections.OrderedDict[int, None] = (
            collections.OrderedDict()
        )
        # Public so :class:`SchedulerLoop` can share the same set.
        self.inflight: set[int] = set()

    def stop(self) -> None:
        self._stop.set()

    def _lru_seen(self, trigger_id: int) -> bool:
        """Return True iff ``trigger_id`` was already processed. Uses
        LRU semantics so heavy load doesn't evict a legitimate dedup
        target prematurely.
        """
        if trigger_id in self._lru:
            self._lru.move_to_end(trigger_id)
            return True
        self._lru[trigger_id] = None
        if len(self._lru) > 256:
            self._lru.popitem(last=False)
        return False

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                trig = await asyncio.wait_for(self._q.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await self._process(trig)
            finally:
                self.inflight.discard(trig.trigger_id)

    async def _process(self, trig: ScheduledTrigger) -> None:
        if self._lru_seen(trig.trigger_id):
            _log.info(
                "scheduler_dispatch_dedup", trigger_id=trig.trigger_id
            )
            await self._store.mark_dropped(trig.trigger_id)
            return
        # Re-check enabled.
        sch = await self._store.get_schedule(trig.schedule_id)
        if sch is None or not sch["enabled"]:
            _log.info(
                "scheduler_dispatch_dropped_disabled",
                trigger_id=trig.trigger_id,
                schedule_id=trig.schedule_id,
            )
            await self._store.mark_dropped(trig.trigger_id)
            return
        # Fix 5 / devil H1 + code-review H-1: read the prompt directly
        # from the queue payload. The loop (CR2.2) copies
        # ``schedules.prompt`` into ``triggers.prompt`` at materialise
        # time and propagates that snapshot on ``ScheduledTrigger``, so
        # the dispatcher already has the immutable per-fire body
        # without a 200-row history SELECT on every fire.
        trigger_prompt = trig.prompt
        fired_text, nonce = wrap_scheduler_prompt(trigger_prompt)
        accumulator: list[str] = []

        async def emit(chunk: str) -> None:
            accumulator.append(chunk)

        msg = IncomingMessage(
            chat_id=self._owner,
            message_id=0,
            text=fired_text,
            origin="scheduler",
            meta={
                "trigger_id": trig.trigger_id,
                "schedule_id": trig.schedule_id,
                "scheduler_nonce": nonce,
                "scheduled_for_utc": trig.scheduled_for_utc,
            },
        )
        try:
            await self._handler.handle(msg, emit)
        except Exception as exc:  # scheduler-turn outer wall
            attempts = await self._store.revert_to_pending(
                trig.trigger_id, last_error=repr(exc)
            )
            _log.warning(
                "scheduler_dispatch_error",
                trigger_id=trig.trigger_id,
                attempts=attempts,
                error=repr(exc)[:200],
            )
            threshold = (
                self._settings.scheduler.dead_attempts_threshold
            )
            if attempts >= threshold:
                await self._store.mark_dead(
                    trig.trigger_id,
                    f"dead after {attempts} attempts: {exc!r}",
                )
                # Best-effort one-shot notify; if Telegram's down the
                # dead-letter state is still recorded.
                try:
                    await self._adapter.send_text(
                        self._owner,
                        f"scheduler: trigger id={trig.trigger_id} "
                        f"failed {attempts}x — dead-lettered",
                    )
                except Exception as notify_exc:
                    _log.warning(
                        "scheduler_dead_notify_failed",
                        error=repr(notify_exc),
                    )
            return
        final = "".join(accumulator).strip()
        if not final:
            # Fix 9 / QA H4: the handler returned without emitting text
            # — the model refused, used all turns on tool calls, or the
            # SDK closed after a ``max_turns_exceeded`` ResultMessage.
            # Treating this as success hides the failure from the owner
            # while advancing state. Revert_to_pending so the retry +
            # dead-letter machinery runs.
            attempts = await self._store.revert_to_pending(
                trig.trigger_id,
                last_error="empty handler output; no text emitted",
            )
            threshold = self._settings.scheduler.dead_attempts_threshold
            if attempts >= threshold:
                await self._store.mark_dead(
                    trig.trigger_id,
                    f"dead after {attempts} attempts: empty output",
                )
                try:
                    await self._adapter.send_text(
                        self._owner,
                        f"scheduler: trigger id={trig.trigger_id} "
                        f"returned empty output {attempts}x — dead-lettered",
                    )
                except Exception as notify_exc:
                    _log.warning(
                        "scheduler_dead_notify_failed",
                        error=repr(notify_exc),
                    )
            _log.warning(
                "scheduler_dispatch_empty_output",
                trigger_id=trig.trigger_id,
                attempts=attempts,
            )
            return
        try:
            await self._adapter.send_text(self._owner, final)
        except Exception as exc:  # don't mark-acked on send fail
            attempts = await self._store.revert_to_pending(
                trig.trigger_id,
                last_error=f"adapter.send_text failed: {exc!r}",
            )
            _log.warning(
                "scheduler_adapter_send_failed",
                trigger_id=trig.trigger_id,
                attempts=attempts,
                error=repr(exc)[:200],
            )
            return
        await self._store.mark_acked(trig.trigger_id)
        _log.info(
            "scheduler_dispatch_acked",
            trigger_id=trig.trigger_id,
            schedule_id=trig.schedule_id,
            out_chars=len(final),
        )
