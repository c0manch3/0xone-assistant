"""CR2.1: store.sweep_expired_sent reverts stale ``sent`` rows mid-run."""

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


async def test_sweep_reverts_stale_sent_row(tmp_path: Path) -> None:
    """A ``sent`` row older than the timeout should flip back to
    ``pending`` with ``attempts`` bumped and a readable ``last_error``.
    """
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="0 9 * * *", prompt="p", tz="UTC", max_schedules=64
    )
    tid = await st.try_materialize_trigger(
        sid, "p", dt.datetime(2026, 4, 1, 9, 0, tzinfo=dt.UTC)
    )
    assert tid is not None
    # Force sent_at to a long-past time so the sweep picks it up.
    await st._conn.execute(
        "UPDATE triggers SET status='sent', "
        "sent_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour') "
        "WHERE id=?",
        (tid,),
    )
    await st._conn.commit()
    reverted = await st.sweep_expired_sent(sent_revert_timeout_s=360)
    assert reverted == 1
    cur = await st._conn.execute(
        "SELECT status, attempts, last_error FROM triggers WHERE id=?",
        (tid,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1] == 1  # attempts bumped
    assert "sent-expired" in (row[2] or "")


async def test_sweep_leaves_fresh_sent_row_alone(tmp_path: Path) -> None:
    """A ``sent`` row that's only a few seconds old should NOT be reverted
    when the timeout is 360s.
    """
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="0 9 * * *", prompt="p", tz="UTC", max_schedules=64
    )
    tid = await st.try_materialize_trigger(
        sid, "p", dt.datetime(2026, 4, 1, 9, 0, tzinfo=dt.UTC)
    )
    assert tid is not None
    await st.mark_sent(tid)  # sent_at = now
    reverted = await st.sweep_expired_sent(sent_revert_timeout_s=360)
    assert reverted == 0
    cur = await st._conn.execute(
        "SELECT status FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == "sent"
