"""Fix 10 / code-review C-3: ``note_queue_saturation`` scoped to
``status IN ('pending', 'sent')`` so it never stomps ``last_error`` on
a terminal (acked / dead / dropped) row.
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


async def test_saturation_preserves_acked_terminal(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None
    await st.mark_acked(tid)
    # Bogus call on an acked row must not mutate ``last_error``.
    await st.note_queue_saturation(
        tid, last_error="queue saturated at tick X"
    )
    cur = await st._conn.execute(
        "SELECT status, last_error FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "acked"
    assert row[1] is None


async def test_saturation_preserves_dead_terminal(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None
    await st.mark_dead(tid, "genuine failure reason")
    await st.note_queue_saturation(
        tid, last_error="queue saturated at tick Y"
    )
    cur = await st._conn.execute(
        "SELECT status, last_error FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "dead"
    assert row[1] == "genuine failure reason"


async def test_saturation_preserves_dropped_terminal(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None
    await st.mark_dropped(tid)
    await st.note_queue_saturation(
        tid, last_error="queue saturated at tick Z"
    )
    cur = await st._conn.execute(
        "SELECT status, last_error FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "dropped"
    assert row[1] is None


async def test_saturation_still_writes_pending(tmp_path: Path) -> None:
    """Positive control: the expected path still writes ``last_error``
    on a pending row.
    """
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None
    await st.note_queue_saturation(
        tid, last_error="queue saturated at tick T"
    )
    cur = await st._conn.execute(
        "SELECT status, last_error FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1] == "queue saturated at tick T"
