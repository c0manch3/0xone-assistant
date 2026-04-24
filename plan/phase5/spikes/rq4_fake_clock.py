"""RQ4 — fake clock injection for async tick-loop tests.

Goal: test a `while True: await clock.sleep(tick_interval_s); tick()`
loop deterministically without `freezegun`.

Pattern:
  - `Clock` protocol with async `now()` + `sleep(seconds)`.
  - Real clock: `datetime.now(UTC)` + `asyncio.sleep`.
  - Fake clock: holds a virtual "now"; `sleep()` advances it deterministically
    and yields to the scheduler so awaiting tasks can observe the bump.

This file doubles as documentation — coder can copy the FakeClock
into `tests/conftest.py` or a test helper module as-is.

Run:
  uv run python plan/phase5/spikes/rq4_fake_clock.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from typing import Protocol


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Clock protocol + implementations.
# ---------------------------------------------------------------------------
class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests.

    - `now()` returns the virtual time.
    - `sleep(s)` advances `now` by s seconds and yields once so that other
      tasks (e.g. the driver) observe the new time before returning.
    - Each `sleep` call records its duration in `slept` (useful to assert
      the loop sleeps with the expected cadence).
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)
        # Yield so the event loop can schedule other tasks that may react
        # to the advanced clock. Zero-delay sleep is enough for test
        # coroutines that are simply awaiting this sleep.
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# The production-shape loop under test.
# ---------------------------------------------------------------------------
async def tick_loop(
    clock: Clock,
    tick_interval_s: float,
    tick_fn,
    stop_event: asyncio.Event,
) -> None:
    """Production-shape loop: sleep, then tick. `stop_event` ends the loop."""
    while not stop_event.is_set():
        await clock.sleep(tick_interval_s)
        await tick_fn(clock.now())


# ---------------------------------------------------------------------------
# Demo / self-test.
# ---------------------------------------------------------------------------
async def _demo() -> int:
    clock = FakeClock(start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    stop = asyncio.Event()
    ticks: list[datetime] = []

    async def my_tick(now: datetime) -> None:
        ticks.append(now)
        # Stop after 8 ticks (2 simulated minutes @ 15s cadence).
        if len(ticks) >= 8:
            stop.set()

    await tick_loop(clock, tick_interval_s=15.0, tick_fn=my_tick, stop_event=stop)

    assert len(ticks) == 8, f"expected 8 ticks, got {len(ticks)}"
    # 8 sleeps of 15s each.
    assert clock.slept == [15.0] * 8, clock.slept
    # Clock advanced by exactly 2 minutes.
    assert clock.now() - datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC) == timedelta(minutes=2)
    # Each tick timestamp monotonic + 15s apart.
    deltas = [(ticks[i] - ticks[i - 1]).total_seconds() for i in range(1, len(ticks))]
    assert deltas == [15.0] * 7, deltas
    print("FakeClock driver: 8 ticks in 2 simulated minutes — PASS")

    # Extra: stop_event set mid-sleep — loop does NOT terminate mid-sleep,
    # it waits for the current sleep to complete. This is fine for our
    # use case (tick_interval_s=15) but coders should know the latency.
    clock2 = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    stop2 = asyncio.Event()
    count = [0]

    async def tick2(_now: datetime) -> None:
        count[0] += 1

    stop2.set()  # set BEFORE loop starts
    await tick_loop(clock2, 15.0, tick2, stop2)
    # Loop sleeps once then the while-check sees stop set.
    # Wait — actually: `while not stop.is_set()` is evaluated BEFORE sleep,
    # so the loop body never runs. Confirm:
    assert count[0] == 0, f"expected 0 ticks with pre-set stop, got {count[0]}"
    print("Pre-set stop_event: loop exits immediately (0 ticks) — PASS")

    return 0


def main() -> int:
    return asyncio.run(_demo())


if __name__ == "__main__":
    sys.exit(main())
