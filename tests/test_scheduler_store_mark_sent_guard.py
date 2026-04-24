"""Fix 1 / CR-1a: ``mark_sent`` is guarded on ``status='pending'``.

If a concurrent dispatcher marks the row terminal (acked / dropped /
dead) after the loop enqueues but before the loop's ``mark_sent``
runs, ``mark_sent`` must NOT resurrect the terminal row. The state
machine relies on this to preserve the at-least-once contract across
daemon restarts.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect


async def _store(tmp_path: Path) -> SchedulerStore:
    db = tmp_path / "sched.db"
    conn = await connect(db)
    await apply_schema(conn)
    return SchedulerStore(conn)


async def test_mark_sent_no_op_on_acked_row(tmp_path: Path) -> None:
    """Dispatcher marked the trigger 'acked' first; loop's later
    ``mark_sent`` must not flip it back to 'sent'."""
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None
    # Dispatcher beats the loop to the terminal transition.
    await st.mark_acked(tid)
    # Loop then calls mark_sent — must be a no-op.
    await st.mark_sent(tid)
    cur = await st._conn.execute(
        "SELECT status, sent_at FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "acked"
    assert row[1] is None  # sent_at never populated


async def test_mark_sent_no_op_on_dropped_row(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None
    await st.mark_dropped(tid)
    await st.mark_sent(tid)
    cur = await st._conn.execute(
        "SELECT status FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == "dropped"


async def test_mark_sent_no_op_on_dead_row(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None
    await st.mark_dead(tid, "forced-failure")
    await st.mark_sent(tid)
    cur = await st._conn.execute(
        "SELECT status FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == "dead"


async def test_mark_sent_happy_path_still_works(tmp_path: Path) -> None:
    """Positive control: a pending row still transitions to sent."""
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None
    await st.mark_sent(tid)
    cur = await st._conn.execute(
        "SELECT status, sent_at FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "sent"
    assert row[1] is not None
