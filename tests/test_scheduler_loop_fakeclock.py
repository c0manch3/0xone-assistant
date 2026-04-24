"""SchedulerLoop end-to-end via FakeClock (RQ4 pattern).

Asserts:
  - a ``* * * * *`` schedule at T+1 minute produces a trigger on the queue
    within one tick.
  - duplicate minute is NOT re-enqueued (UNIQUE-gate via
    :meth:`SchedulerStore.try_materialize_trigger`).
  - ``sweep_expired_sent`` is invoked every tick (CR2.1).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

from assistant.config import get_settings
from assistant.scheduler.loop import ScheduledTrigger, SchedulerLoop
from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect
from tests.conftest import FakeClock


async def _store(tmp_path: Path) -> SchedulerStore:
    db = tmp_path / "sched.db"
    conn = await connect(db)
    await apply_schema(conn)
    return SchedulerStore(conn)


async def test_tick_materializes_and_enqueues(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x" * 20)
    monkeypatch.setenv("OWNER_CHAT_ID", "1")
    settings = get_settings.__wrapped__()

    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="hi", tz="UTC", max_schedules=64
    )

    q: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(maxsize=64)
    inflight: set[int] = set()
    stop = asyncio.Event()
    # Anchor 30 seconds past the minute boundary — ``is_due`` with
    # last_fire_at=None returns the current floor-minute if it matches.
    clock = FakeClock(
        start=dt.datetime(2026, 1, 1, 0, 0, 30, tzinfo=dt.UTC)
    )
    loop = SchedulerLoop(
        queue=q,
        store=st,
        inflight_ref=inflight,
        settings=settings,
        clock=clock,
        stop_event=stop,
    )
    await loop._tick_once()

    assert q.qsize() == 1
    trig = q.get_nowait()
    assert trig.schedule_id == sid
    assert trig.prompt == "hi"

    # Second tick at the same minute must NOT re-enqueue.
    await loop._tick_once()
    assert q.qsize() == 0


async def test_tick_calls_sweep_expired_sent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """CR2.1: ``sweep_expired_sent`` is invoked on every tick.

    We assert by spy rather than end-state because the orphan-reclaim
    step later in the same tick may re-enqueue the freshly reverted
    row (by design) and flip it back to ``sent``. Unit-level CR2.1
    coverage lives in ``test_scheduler_sweep_expired_sent.py``; this
    test just proves the loop wires the store method into its tick.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x" * 20)
    monkeypatch.setenv("OWNER_CHAT_ID", "1")
    settings = get_settings.__wrapped__()

    st = await _store(tmp_path)

    called: list[int] = []
    orig = st.sweep_expired_sent

    async def _spy(*, sent_revert_timeout_s: int) -> int:
        called.append(sent_revert_timeout_s)
        return await orig(sent_revert_timeout_s=sent_revert_timeout_s)

    st.sweep_expired_sent = _spy  # type: ignore[method-assign]

    q: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(maxsize=64)
    inflight: set[int] = set()
    stop = asyncio.Event()
    clock = FakeClock(start=dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC))
    loop = SchedulerLoop(
        queue=q,
        store=st,
        inflight_ref=inflight,
        settings=settings,
        clock=clock,
        stop_event=stop,
    )
    await loop._tick_once()
    assert called == [settings.scheduler.sent_revert_timeout_s]
