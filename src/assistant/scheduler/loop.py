"""Producer loop — materialises triggers from enabled schedules (phase 5).

Each tick (`settings.scheduler.tick_interval_s`, default 15 s):

 1. Snapshot enabled schedules.
 2. For each: compute `is_due` against `last_fire_at` + `now` under the
    schedule's tz.
 3. `try_materialize_trigger` atomically inserts the row + advances
    `last_fire_at` (unique violation → skip).
 4. Put a `ScheduledTrigger` on the shared queue (blocking put — full
    queue means the consumer is behind; producer stalls naturally until
    it catches up).
 5. Every `_SWEEP_EVERY_N_TICKS` iterations: runtime
    `revert_stuck_sent(exclude_ids=dispatcher.inflight())` — wave-2
    B-W2-1. This is the regression the phase-4 B-CRIT-1 dance produced;
    the sweep MUST be wired into the tick schedule, not a separate
    background coroutine, or else the loop crashing silently also stops
    stuck-trigger recovery.

`run()` wraps the tick cadence in an outermost try/except so a single
malformed row doesn't kill the loop (per-tick warning + continue), and
a fatal crash bubbles up to the daemon's notify hook with a 24 h
marker-file cooldown (`_scheduler_loop_notify`; GAP #15 / wave-2
N-W2-4).

Heartbeat (wave-2 G-W2-10): `_last_tick_at` = event-loop time at the
end of each tick — consumed by the daemon's `_scheduler_health_check_bg`
to detect a silently-stuck loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.logger import get_logger
from assistant.scheduler import from_iso_utc
from assistant.scheduler.cron import CronParseError, is_due, parse_cron
from assistant.scheduler.dispatcher import ScheduledTrigger

if TYPE_CHECKING:  # pragma: no cover
    from assistant.config import Settings
    from assistant.scheduler.dispatcher import SchedulerDispatcher
    from assistant.scheduler.store import SchedulerStore

log = get_logger("scheduler.loop")


class SchedulerLoop:
    """Periodic producer. One instance per `Daemon`.

    Lives in the same event loop as the dispatcher; shutdown is via
    `stop()` which wakes the `asyncio.wait_for(stop.wait, …)` tick
    pacing immediately.
    """

    # Run runtime sweep every 4th tick (15 s x 4 = 60 s) per wave-2 B-W2-1.
    _SWEEP_EVERY_N_TICKS = 4
    # Fix-pack CRITICAL #1: the retry re-enqueue pass shares the same
    # cadence. Faster ticks would re-queue a row the dispatcher is still
    # staging (in the brief window between `mark_pending_retry` and the
    # next trigger arriving at the consumer); 60 s is plenty of slack.
    _RETRY_SWEEP_BATCH = 32

    def __init__(
        self,
        *,
        queue: asyncio.Queue[ScheduledTrigger],
        store: SchedulerStore,
        dispatcher: SchedulerDispatcher,
        settings: Settings,
        notify_fn: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._queue = queue
        self._store = store
        self._dispatcher = dispatcher
        self._settings = settings
        self._notify = notify_fn
        self._stop = asyncio.Event()
        self._tick_count = 0
        self._last_tick_at: float = 0.0

    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def last_tick_at(self) -> float:
        return self._last_tick_at

    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop. Outermost try/except fires the notify hook on fatal
        crash (GAP #15 / wave-2 N-W2-4), then re-raises so the daemon's
        supervisor logs the failure and phase-8 launchd restarts the
        process."""
        log.info("scheduler_loop_started")
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception:
                    log.warning("scheduler_tick_failed", exc_info=True)
                self._last_tick_at = asyncio.get_running_loop().time()
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._settings.scheduler.tick_interval_s,
                    )
                except TimeoutError:
                    continue
        except Exception as exc:
            log.error("scheduler_loop_fatal", error=repr(exc), exc_info=True)
            if self._notify is not None:
                try:
                    await self._notify(f"scheduler loop crashed: {exc!r}")
                except Exception:
                    log.warning("scheduler_loop_notify_failed", exc_info=True)
            raise
        finally:
            log.info("scheduler_loop_stopped")

    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        self._tick_count += 1
        now = datetime.now(UTC)
        schedules = await self._store.iter_enabled_schedules()

        for row in schedules:
            await self._maybe_materialize(row, now)

        # Wave-2 B-W2-1 + fix-pack CRITICAL #1: every 4th tick, run the
        # runtime revert sweep AND the pending-retry re-enqueue pass.
        # Both share the dispatcher's inflight() snapshot so the same
        # row can't be touched twice.
        if self._tick_count % self._SWEEP_EVERY_N_TICKS == 0:
            inflight = self._dispatcher.inflight()
            try:
                reverted = await self._store.revert_stuck_sent(
                    timeout_s=self._settings.scheduler.sent_revert_timeout_s,
                    exclude_ids=inflight,
                )
                if reverted:
                    log.info("scheduler_revert_stuck_sent", count=reverted)
            except Exception:
                log.warning("scheduler_revert_stuck_sent_failed", exc_info=True)
            try:
                await self._reenqueue_pending_retries(inflight)
            except Exception:
                log.warning("scheduler_retry_reenqueue_failed", exc_info=True)

    # ------------------------------------------------------------------

    async def _reenqueue_pending_retries(self, inflight: set[int]) -> None:
        """Fix-pack CRITICAL #1: push rows previously marked
        `pending_retry` (status='pending' AND attempts>0) back onto the
        in-process queue so the dispatcher can make the next attempt.

        We iterate `list_pending_retries` once per sweep; the caller's
        outer try/except handles DB failures. Each `put_nowait` refuses
        to block on a full queue — the sweep is retried on the next
        cadence, so losing one pass is preferred over stalling the
        producer loop (which would freeze the heartbeat).
        """
        rows = await self._store.list_pending_retries(
            exclude_ids=inflight, limit=self._RETRY_SWEEP_BATCH
        )
        for row in rows:
            try:
                scheduled_for = from_iso_utc(str(row["scheduled_for"]))
            except ValueError:
                log.warning(
                    "scheduler_retry_reenqueue_parse_failed",
                    trigger_id=row["id"],
                    scheduled_for=row["scheduled_for"],
                )
                continue
            trigger = ScheduledTrigger(
                trigger_id=int(row["id"]),
                schedule_id=int(row["schedule_id"]),
                prompt=str(row["prompt"]),
                scheduled_for=scheduled_for,
                # attempts is the count of PREVIOUS failures; next delivery
                # is N+1 so the dead-letter threshold advances in lock-step
                # with the dispatcher's `attempts >= threshold` gate.
                attempt=int(row["attempts"]) + 1,
            )
            try:
                self._queue.put_nowait(trigger)
            except asyncio.QueueFull:
                log.info(
                    "scheduler_retry_reenqueue_backpressure",
                    trigger_id=trigger.trigger_id,
                    attempt=trigger.attempt,
                )
                # Stop early — the remaining rows wait for the next sweep.
                return
            log.info(
                "scheduler_retry_reenqueued",
                trigger_id=trigger.trigger_id,
                schedule_id=trigger.schedule_id,
                attempt=trigger.attempt,
            )

    # ------------------------------------------------------------------

    async def _maybe_materialize(self, row: dict[str, Any], now: datetime) -> None:
        """Parse one schedule row and, if due, push a ScheduledTrigger."""
        schedule_id = int(row["id"])
        cron_raw = str(row["cron"])
        try:
            expr = parse_cron(cron_raw)
        except CronParseError as exc:
            log.warning(
                "scheduler_cron_parse_failed",
                schedule_id=schedule_id,
                cron=cron_raw,
                error=str(exc),
            )
            return

        tz_raw = str(row["tz"])
        try:
            tz = ZoneInfo(tz_raw)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            log.warning(
                "scheduler_tz_invalid",
                schedule_id=schedule_id,
                tz=tz_raw,
                error=str(exc),
            )
            return

        last_raw = row.get("last_fire_at")
        last: datetime | None = None
        if last_raw is not None:
            try:
                last = from_iso_utc(str(last_raw))
            except ValueError:
                log.warning(
                    "scheduler_last_fire_parse_failed",
                    schedule_id=schedule_id,
                    last_fire_at=str(last_raw),
                )
                return

        t = is_due(
            expr,
            last_fire_at=last,
            now=now,
            tz=tz,
            catchup_window_s=self._settings.scheduler.catchup_window_s,
        )
        if t is None:
            # Catchup-miss logging: only bother when the schedule HAD a
            # prior fire (else first-ever fire is not a miss).
            return

        prompt = str(row["prompt"])
        trigger_id = await self._store.try_materialize_trigger(schedule_id, prompt, t)
        if trigger_id is None:
            # Raced with another process / already materialised.
            return

        await self._queue.put(
            ScheduledTrigger(
                trigger_id=trigger_id,
                schedule_id=schedule_id,
                prompt=prompt,
                scheduled_for=t,
                attempt=1,
            )
        )
        log.info(
            "scheduler_trigger_materialized",
            schedule_id=schedule_id,
            trigger_id=trigger_id,
            scheduled_for=t.isoformat(),
        )

    # ------------------------------------------------------------------

    async def count_catchup_misses(self) -> int:
        """Startup helper — thin wrapper around `SchedulerStore.count_catchup_misses`
        with the loop's settings bound (GAP #16). Called once from
        `Daemon.start()` before the tick pacing begins."""
        now = datetime.now(UTC)
        return await self._store.count_catchup_misses(
            now=now,
            catchup_window_s=self._settings.scheduler.catchup_window_s,
            tz_default=self._settings.scheduler.tz_default,
        )
