"""Scheduler store — CRUD, UNIQUE dedup, CASCADE, idempotency."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect


async def _store(tmp_path: Path) -> SchedulerStore:
    db = tmp_path / "sched.db"
    conn = await connect(db)
    await apply_schema(conn)
    return SchedulerStore(conn)


async def test_add_and_list_schedules(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    new_id = await st.add_schedule(
        cron="0 9 * * *", prompt="morning", tz="UTC", max_schedules=64
    )
    assert new_id > 0
    rows = await st.list_schedules()
    assert len(rows) == 1
    assert rows[0]["cron"] == "0 9 * * *"
    assert rows[0]["enabled"] is True


async def test_add_enforces_cap(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    await st.add_schedule(
        cron="0 9 * * *", prompt="a", tz="UTC", max_schedules=1
    )
    with pytest.raises(ValueError, match="cap reached"):
        await st.add_schedule(
            cron="0 10 * * *", prompt="b", tz="UTC", max_schedules=1
        )


async def test_disable_enable_idempotent(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="0 9 * * *", prompt="x", tz="UTC", max_schedules=64
    )
    assert await st.disable_schedule(sid) is True
    assert await st.disable_schedule(sid) is False  # already disabled
    assert await st.enable_schedule(sid) is True
    assert await st.enable_schedule(sid) is False


async def test_materialize_trigger_unique_gate(tmp_path: Path) -> None:
    """Two calls for the same (schedule_id, scheduled_for) — second is
    dedup'd at the UNIQUE constraint and returns None."""
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="0 9 * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 1, 9, 0, tzinfo=dt.UTC)
    first = await st.try_materialize_trigger(sid, "p", when)
    second = await st.try_materialize_trigger(sid, "p", when)
    assert first is not None
    assert second is None


async def test_cascade_delete(tmp_path: Path) -> None:
    """Hard-delete of schedules.row should CASCADE to triggers."""
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="0 9 * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 1, 9, 0, tzinfo=dt.UTC)
    await st.try_materialize_trigger(sid, "p", when)
    # Direct DELETE (not public API — phase 5 rm is soft-delete).
    await st._conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
    await st._conn.commit()
    cur = await st._conn.execute(
        "SELECT COUNT(*) FROM triggers WHERE schedule_id=?", (sid,)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == 0


async def test_clean_slate_sent_reverts(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="0 9 * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 1, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None
    await st.mark_sent(tid)
    reverted = await st.clean_slate_sent()
    assert reverted == 1
    cur = await st._conn.execute(
        "SELECT status FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == "pending"
