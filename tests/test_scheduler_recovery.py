"""Scheduler recovery:
- ``clean_slate_sent`` on boot reverts orphan ``sent`` rows.
- ``count_catchup_misses`` picks enabled schedules whose last_fire_at
  is older than the catchup window.
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


async def test_clean_slate_sent_bumps_attempts(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="0 9 * * *", prompt="p", tz="UTC", max_schedules=64
    )
    tid = await st.try_materialize_trigger(
        sid, "p", dt.datetime(2026, 4, 1, 9, 0, tzinfo=dt.UTC)
    )
    assert tid is not None
    await st.mark_sent(tid)
    reverted = await st.clean_slate_sent()
    assert reverted == 1
    cur = await st._conn.execute(
        "SELECT status, attempts FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1] == 1


async def test_count_catchup_misses_counts_only_stale(
    tmp_path: Path,
) -> None:
    st = await _store(tmp_path)
    sid_fresh = await st.add_schedule(
        cron="0 9 * * *", prompt="a", tz="UTC", max_schedules=64
    )
    sid_stale = await st.add_schedule(
        cron="0 9 * * *", prompt="b", tz="UTC", max_schedules=64
    )
    await st._conn.execute(
        "UPDATE schedules SET last_fire_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
        "WHERE id=?",
        (sid_fresh,),
    )
    await st._conn.execute(
        "UPDATE schedules SET last_fire_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours') "
        "WHERE id=?",
        (sid_stale,),
    )
    await st._conn.commit()
    missed = await st.count_catchup_misses(catchup_window_s=3600)
    assert missed == 1
