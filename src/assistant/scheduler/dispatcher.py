"""Dispatcher — consumes `ScheduledTrigger` from the in-process queue and
drives one `ClaudeHandler.handle` round per trigger (phase 5 §1.4 / §5.4).

Design points:

* Shutdown uses a `stop_event + asyncio.wait_for(queue.get, timeout=0.5)`
  loop instead of a poison pill (spike S-5: `put_nowait(POISON)` on a
  full queue raises `QueueFull`).

* `_deliver` wraps the happy path in `try/except`; on
  `asyncio.CancelledError` we `asyncio.shield(store.mark_pending_retry)`
  so the DB UPDATE is NOT itself cancelled (wave-2 B-W2-3). Without the
  shield, a SIGTERM mid-delivery leaves the trigger stuck in `status='sent'`
  and the next boot's clean-slate has to pick it up — racier than the
  direct retry ledger.

* Every transition (`mark_sent`, `mark_acked`, `mark_pending_retry`,
  `mark_dead`, `mark_dropped`) is protected by a SQL status precondition
  (wave-2 G-W2-6). If any of them returns False we log the skew and move
  on — we do NOT raise. The store logs the skew itself; dispatcher logs
  at its own level to help operators correlate.

* LRU dedup (`_recent_acked`) guards against a clean-slate after crash
  re-materialising an already-acked trigger via UNIQUE check loophole
  (size 256 — cold-start after 257 deliveries the LRU drops oldest, but
  status='acked' in DB is still authoritative).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from assistant.adapters.base import IncomingMessage
from assistant.adapters.dispatch_reply import _DedupLedger, dispatch_reply
from assistant.logger import get_logger
from assistant.media.paths import outbox_dir

if TYPE_CHECKING:  # pragma: no cover — avoid circular imports at runtime
    from assistant.adapters.base import MessengerAdapter
    from assistant.config import Settings
    from assistant.handlers.message import ClaudeHandler
    from assistant.scheduler.store import SchedulerStore

log = get_logger("scheduler.dispatcher")

_LRU_SIZE = 256


@dataclass(frozen=True, slots=True)
class ScheduledTrigger:
    """One materialised trigger ready for delivery.

    `attempt` is 1-based. The producer fills it from `(triggers.attempts + 1)`;
    a retry re-queued after `mark_pending_retry` carries the next value.
    Phase 8 will send the same shape over UDS — keep it serialisable.
    """

    trigger_id: int
    schedule_id: int
    prompt: str
    scheduled_for: datetime
    attempt: int


class SchedulerDispatcher:
    """Queue consumer. One instance per `Daemon`.

    Public methods:
      * `run()` — main loop; returns when stop() is set and queue drained.
      * `stop()` — non-blocking signal.
      * `inflight()` — snapshot of `_inflight` set for `SchedulerLoop`'s
        runtime revert sweep (wave-2 B-W2-1).
      * `last_tick_at()` — monotonic event-loop timestamp of the last queue
        dequeue or timeout (wave-2 G-W2-10; consumed by the daemon's
        heartbeat health-check).
    """

    def __init__(
        self,
        *,
        queue: asyncio.Queue[ScheduledTrigger],
        store: SchedulerStore,
        handler: ClaudeHandler,
        adapter: MessengerAdapter,
        owner_chat_id: int,
        settings: Settings,
        notify_fn: Callable[[str], Awaitable[None]] | None = None,
        dedup_ledger: _DedupLedger,
    ) -> None:
        self._queue = queue
        self._store = store
        self._handler = handler
        self._adapter = adapter
        self._owner = owner_chat_id
        self._settings = settings
        self._notify = notify_fn
        # Phase-7 commit 14: scheduler deliveries go through
        # `dispatch_reply` so artefact paths emitted by the model (e.g.
        # `/abs/outbox/photo.png`) are routed via `send_photo/document/
        # audio` instead of being dumped verbatim via `send_text`. The
        # `_DedupLedger` is shared with the main-turn + subagent-hook
        # call-sites (one per `Daemon`) — invariant I-7.5 guarantees
        # at-most-once delivery in the 300 s race window between the
        # main turn's final text and a worker's `SubagentStop` hook.
        self._dedup_ledger = dedup_ledger
        self._stop = asyncio.Event()
        self._inflight: set[int] = set()
        self._recent_acked: deque[int] = deque(maxlen=_LRU_SIZE)
        self._last_tick_at: float = 0.0
        # Fix-pack HIGH #5: track shielded `mark_pending_retry` tasks
        # that fire on shutdown-cancellation. Without this, an
        # orphaned shield-task can still be executing the DB UPDATE
        # when `Daemon.stop()` closes the aiosqlite connection — the
        # UPDATE then races a `ProgrammingError: Cannot operate on a
        # closed database`. `Daemon.stop()` awaits `pending_updates()`
        # with a short timeout between bg-drain and conn.close.
        self._pending_updates: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------

    def inflight(self) -> set[int]:
        """Snapshot (copy) of the current `_inflight` ids. Consumed by
        `SchedulerLoop` to exclude from `revert_stuck_sent`."""
        return self._inflight.copy()

    def pending_updates(self) -> set[asyncio.Task[Any]]:
        """Snapshot of outstanding shielded DB-update tasks (fix-pack
        HIGH #5). `Daemon.stop()` awaits this set with a short timeout
        before closing the aiosqlite connection."""
        return self._pending_updates.copy()

    def last_tick_at(self) -> float:
        return self._last_tick_at

    def stop(self) -> None:
        self._stop.set()

    def stop_event(self) -> asyncio.Event:
        """Public accessor for the stop event (fix-pack CRITICAL #5).

        Mirrors `SchedulerLoop.stop_event()` so the daemon's shutdown /
        health-check paths don't need to reach into `_stop`."""
        return self._stop

    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Drain loop.

        Uses `wait_for(get, timeout=0.5)` so stop() wakes the consumer via
        timeout within at most 500 ms. No poison pill — avoids the
        QueueFull pathology from spike S-5.

        Fix-pack HIGH #4: the outer body is wrapped in the same
        try/except-fatal + notify pattern as `SchedulerLoop.run` so a
        fatal crash on the consumer side surfaces to Telegram with a
        distinct `scheduler_dispatcher_fatal` reason. Without this
        handler a silent dispatcher crash is only visible through the
        heartbeat watchdog's stale-timer (CRITICAL #3), several minutes
        later.
        """
        log.info("scheduler_dispatcher_started")
        try:
            while not self._stop.is_set():
                try:
                    trigger = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                except TimeoutError:
                    self._last_tick_at = asyncio.get_running_loop().time()
                    continue
                try:
                    await self._deliver(trigger)
                finally:
                    self._last_tick_at = asyncio.get_running_loop().time()
        except asyncio.CancelledError:
            # Ordinary shutdown path (Daemon.stop drain timeout hits).
            # Do NOT notify; the supervisor knows we're stopping.
            raise
        except Exception as exc:
            log.error("scheduler_dispatcher_fatal", error=repr(exc), exc_info=True)
            if self._notify is not None:
                try:
                    await self._notify(f"scheduler dispatcher crashed: {exc!r}")
                except Exception:
                    log.warning("scheduler_dispatcher_notify_failed", exc_info=True)
            raise
        finally:
            log.info("scheduler_dispatcher_stopped")

    # ------------------------------------------------------------------

    async def _deliver(self, t: ScheduledTrigger) -> None:
        self._inflight.add(t.trigger_id)
        try:
            # LRU dedup — guards against double-delivery after a restart
            # where the same trigger_id gets re-queued.
            if t.trigger_id in self._recent_acked:
                log.info(
                    "scheduler_trigger_dup_skip",
                    trigger_id=t.trigger_id,
                    schedule_id=t.schedule_id,
                )
                return

            # Schedule-disabled branch (GAP #2).
            sched = await self._store.get_schedule(t.schedule_id)
            if sched is None or not sched.get("enabled", False):
                log.info(
                    "scheduler_trigger_dropped_disabled",
                    trigger_id=t.trigger_id,
                    schedule_id=t.schedule_id,
                )
                await self._store.mark_dropped(t.trigger_id, reason="schedule_disabled")
                return

            # pending → sent (precondition checked in SQL).
            if not await self._store.mark_sent(t.trigger_id):
                # Skew already logged inside store.mark_sent; no raise.
                return

            try:
                joined = await self._deliver_with_handler(t)
                if joined:
                    await dispatch_reply(
                        self._adapter,
                        self._owner,
                        joined,
                        outbox_root=outbox_dir(self._settings.data_dir),
                        dedup=self._dedup_ledger,
                        log_ctx={
                            "trigger_id": t.trigger_id,
                            "schedule_id": t.schedule_id,
                        },
                    )
                if await self._store.mark_acked(t.trigger_id):
                    self._recent_acked.append(t.trigger_id)
            except asyncio.CancelledError:
                # WAVE-2 B-W2-3: shield the UPDATE so shutdown-cancellation
                # doesn't cancel the DB round-trip itself. Fix-pack HIGH #5:
                # wrap the shielded coroutine in an explicit Task and track
                # it on `_pending_updates` so `Daemon.stop()` can await
                # completion before closing the DB connection.
                update_task: asyncio.Task[Any] = asyncio.create_task(
                    self._store.mark_pending_retry(t.trigger_id, last_error="shutdown_cancelled"),
                    name=f"mark_pending_retry-{t.trigger_id}",
                )
                self._pending_updates.add(update_task)
                update_task.add_done_callback(self._pending_updates.discard)
                await asyncio.shield(update_task)
                raise
            except Exception as exc:
                attempts = await self._store.mark_pending_retry(
                    t.trigger_id, last_error=repr(exc)[:512]
                )
                log.warning(
                    "scheduler_delivery_failed",
                    trigger_id=t.trigger_id,
                    schedule_id=t.schedule_id,
                    attempts=attempts,
                    error=repr(exc)[:200],
                )
                threshold = self._settings.scheduler.dead_attempts_threshold
                if attempts >= threshold and await self._store.mark_dead(
                    t.trigger_id, last_error=repr(exc)[:512]
                ):
                    await self._notify_dead(t.trigger_id, repr(exc))
        finally:
            self._inflight.discard(t.trigger_id)

    # ------------------------------------------------------------------

    async def _deliver_with_handler(self, t: ScheduledTrigger) -> str:
        """Build the IncomingMessage + accumulate handler.emit chunks.

        Handler does NOT call the adapter directly (plan §1.6); we collect
        its output into one joined string and the caller decides whether to
        send to Telegram (skipped when the model emitted nothing).
        """
        msg = IncomingMessage(
            chat_id=self._owner,
            text=t.prompt,
            origin="scheduler",
            meta={"trigger_id": t.trigger_id, "schedule_id": t.schedule_id},
        )
        chunks: list[str] = []

        async def emit(text: str) -> None:
            chunks.append(text)

        await self._handler.handle(msg, emit)
        return "".join(chunks).strip()

    # ------------------------------------------------------------------

    async def _notify_dead(self, trigger_id: int, err: str) -> None:
        """Best-effort one-shot Telegram notice. Never raises — a failure to
        deliver the death notice shouldn't cascade into a dispatcher crash.
        """
        try:
            msg = (
                f"scheduler trigger {trigger_id} marked dead after "
                f"{self._settings.scheduler.dead_attempts_threshold} attempts. "
                f"last error: {err[:200]}"
            )
            await self._adapter.send_text(self._owner, msg)
        except Exception:
            log.warning("scheduler_dead_notify_failed", trigger_id=trigger_id, exc_info=True)


# Keep mypy quiet on the unused Any import if future-hooked; explicit shadow.
_ = Any
