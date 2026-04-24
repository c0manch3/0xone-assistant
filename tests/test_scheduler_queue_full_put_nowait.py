"""H-1: loop.put_nowait + QueueFull ⇒ row stays pending, note recorded,
never blocks the producer.
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


async def test_queue_full_leaves_row_pending_with_note(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # The loop's Settings call uses env vars via pydantic — fake them.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x" * 20)
    monkeypatch.setenv("OWNER_CHAT_ID", "1")
    settings = get_settings.__wrapped__()

    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )

    q: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(maxsize=1)
    q.put_nowait(
        ScheduledTrigger(
            trigger_id=-1,
            schedule_id=sid,
            prompt="pre-fill",
            scheduled_for_utc="2026-01-01T00:00:00Z",
        )
    )
    inflight: set[int] = set()
    stop = asyncio.Event()
    # Anchor the clock at 2026-01-01 00:00 UTC so the ``* * * * *``
    # schedule's first due minute is 00:01 UTC (exists under FakeClock
    # advancing within the loop tick).
    clock = FakeClock(start=dt.datetime(2026, 1, 1, 0, 1, tzinfo=dt.UTC))
    loop = SchedulerLoop(
        queue=q,
        store=st,
        inflight_ref=inflight,
        settings=settings,
        clock=clock,
        stop_event=stop,
    )
    await loop._tick_once()

    # Queue is still full (our pre-fill + whatever try_materialize did).
    # But the new trigger row must be pending with last_error set.
    cur = await st._conn.execute(
        "SELECT status, last_error FROM triggers "
        "WHERE schedule_id=? AND status='pending' "
        "ORDER BY id DESC LIMIT 1",
        (sid,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert "queue saturated" in (row[1] or "")
