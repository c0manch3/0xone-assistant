"""Producer half of the scheduler: tick-driven cron evaluation that
materialises triggers and enqueues them for the dispatcher.

Clock injection (RQ4): the loop takes a :class:`Clock` protocol so
tests drive ticks deterministically with :class:`FakeClock` without
touching real wall-clock wait / OS sleep calls. Production wires a
:class:`RealClock`.

Key policies:
  - ``put_nowait`` + ``QueueFull`` catch (H-1): we never block the
    producer on a saturated dispatcher. On overflow the row stays
    ``pending`` with a note; the next tick's reclaim sweep picks it up
    once the dispatcher drains.
  - ``sweep_expired_sent`` every tick (CR2.1): stale ``sent`` rows from
    hung handlers get reverted mid-run rather than waiting for the
    next daemon restart.
  - Per-tick outer ``try/except``: one bad cron expression (shouldn't
    happen — we parse at add time — but paranoia budget is cheap)
    doesn't kill the loop.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from assistant.config import Settings
from assistant.scheduler.cron import CronParseError, is_due, parse_cron
from assistant.scheduler.store import SchedulerStore

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ScheduledTrigger:
    """Dispatch-queue payload.

    ``prompt`` holds the per-trigger snapshot (from ``triggers.prompt``,
    NOT ``schedules.prompt``) — CR2.2. The dispatcher re-reads from the
    store row during processing to guard against in-flight edits; this
    field is just the loop's at-enqueue copy.
    """

    trigger_id: int
    schedule_id: int
    prompt: str
    scheduled_for_utc: str


class Clock(Protocol):
    """Minimal async-clock interface. Production: :class:`RealClock`.
    Tests: ``tests/conftest.py::FakeClock``.
    """

    def now(self) -> dt.datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    """Default clock — wraps ``datetime.now`` + ``asyncio.sleep``."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SchedulerLoop:
    def __init__(
        self,
        *,
        queue: asyncio.Queue[ScheduledTrigger],
        store: SchedulerStore,
        inflight_ref: set[int],
        settings: Settings,
        clock: Clock | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self._q = queue
        self._store = store
        # Same ``set`` as :class:`SchedulerDispatcher.inflight` — mutated
        # here (add on enqueue) and there (discard on completion).
        self._inflight = inflight_ref
        self._settings = settings
        self._clock: Clock = clock or RealClock()
        self._stop = stop_event or asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Main tick loop. Runs until ``stop_event`` is set.

        Per-tick order:
          1. CR2.1 sweep — revert stale ``sent`` rows.
          2. Scan enabled schedules, materialise + enqueue due triggers.
          3. Reclaim any pending-not-queued orphans from a prior
             saturation.
          4. Sleep ``tick_interval_s``.
        """
        tick = self._settings.scheduler.tick_interval_s
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except Exception:  # outermost tick-level guard
                _log.exception("scheduler_loop_tick_error")
            await self._clock.sleep(float(tick))

    async def _tick_once(self) -> None:
        # CR2.1: revert stale ``sent`` rows BEFORE scanning. A hung
        # handler from last tick gets a fresh chance this tick rather
        # than sitting dead until the next daemon restart.
        await self._store.sweep_expired_sent(
            sent_revert_timeout_s=self._settings.scheduler.sent_revert_timeout_s,
        )
        now = self._clock.now()
        schedules = await self._store.list_schedules(enabled_only=True)
        for sch in schedules:
            try:
                expr = parse_cron(sch["cron"])
            except CronParseError as exc:
                _log.warning(
                    "scheduler_cron_parse_error",
                    id=sch["id"],
                    cron=sch["cron"],
                    error=str(exc),
                )
                continue
            try:
                tz = ZoneInfo(sch["tz"])
            except ZoneInfoNotFoundError as exc:
                _log.warning(
                    "scheduler_tz_unknown",
                    id=sch["id"],
                    tz=sch["tz"],
                    error=str(exc),
                )
                continue
            last_raw = sch["last_fire_at"]
            last: dt.datetime | None
            if last_raw:
                try:
                    last = dt.datetime.fromisoformat(
                        str(last_raw).replace("Z", "+00:00")
                    )
                except ValueError:
                    last = None
            else:
                last = None
            due = is_due(
                expr,
                last_fire_at=last,
                now_utc=now,
                tz=tz,
                catchup_window_s=self._settings.scheduler.catchup_window_s,
            )
            if due is None:
                continue
            trig_id = await self._store.try_materialize_trigger(
                int(sch["id"]), str(sch["prompt"]), due
            )
            if trig_id is None:
                continue  # duplicate minute
            self._inflight.add(trig_id)
            trig = ScheduledTrigger(
                trigger_id=trig_id,
                schedule_id=int(sch["id"]),
                prompt=str(sch["prompt"]),
                scheduled_for_utc=due.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            try:
                self._q.put_nowait(trig)
            except asyncio.QueueFull:
                self._inflight.discard(trig_id)
                await self._store.note_queue_saturation(
                    trig_id,
                    last_error=f"queue saturated at tick {now.isoformat()}",
                )
                _log.warning(
                    "scheduler_queue_saturated", trigger_id=trig_id
                )
                continue
            await self._store.mark_sent(trig_id)
        # Orphan reclaim — covers the pending rows left behind by a
        # saturation event (Fix 2 / CR-2: SQL now gates on
        # ``last_error LIKE 'queue saturated%'`` so a freshly
        # materialised trigger with a stale ``scheduled_for`` (catchup)
        # is never picked up). Threshold sourced from settings.
        orphans = await self._store.reclaim_pending_not_queued(
            self._inflight,
            older_than_s=self._settings.scheduler.reclaim_older_than_s,
        )
        for o in orphans:
            self._inflight.add(int(o["id"]))
            trig = ScheduledTrigger(
                trigger_id=int(o["id"]),
                schedule_id=int(o["schedule_id"]),
                prompt=str(o["prompt"]),
                scheduled_for_utc=str(o["scheduled_for"]),
            )
            try:
                self._q.put_nowait(trig)
                await self._store.mark_sent(int(o["id"]))
            except asyncio.QueueFull:
                self._inflight.discard(int(o["id"]))
                continue
