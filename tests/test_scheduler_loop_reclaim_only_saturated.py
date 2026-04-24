"""Fix 2 / CR-2: ``reclaim_pending_not_queued`` picks up ONLY rows with
``last_error LIKE 'queue saturated%'``.

Prior behaviour picked up any stale pending row, which during a
catchup walk could re-queue a freshly-materialised trigger whose
``scheduled_for`` legitimately sits minutes behind ``now``.
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


async def test_reclaim_ignores_pending_without_saturation_marker(
    tmp_path: Path,
) -> None:
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    # Materialise a trigger 10 minutes in the past so the age filter
    # (30s threshold) is satisfied.
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    tid = await st.try_materialize_trigger(sid, "p", past)
    assert tid is not None
    # No note_queue_saturation call → last_error stays NULL.
    orphans = await st.reclaim_pending_not_queued(
        set(), older_than_s=30
    )
    assert orphans == [], (
        "a fresh pending row with no saturation note must NOT be "
        "reclaimed — otherwise every catchup-walk materialisation "
        "could be double-queued in the same tick"
    )


async def test_reclaim_picks_up_saturated_row(tmp_path: Path) -> None:
    """Positive control: once ``note_queue_saturation`` stamps the row,
    the next reclaim call returns it."""
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    tid = await st.try_materialize_trigger(sid, "p", past)
    assert tid is not None
    await st.note_queue_saturation(
        tid, last_error="queue saturated at tick 2026-04-21T09:00:00"
    )
    orphans = await st.reclaim_pending_not_queued(
        set(), older_than_s=30
    )
    assert len(orphans) == 1
    assert orphans[0]["id"] == tid


async def test_reclaim_respects_inflight_filter(tmp_path: Path) -> None:
    """Even a saturated row is skipped if already in ``inflight``."""
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    tid = await st.try_materialize_trigger(sid, "p", past)
    assert tid is not None
    await st.note_queue_saturation(tid, last_error="queue saturated: x")
    orphans = await st.reclaim_pending_not_queued(
        {tid}, older_than_s=30
    )
    assert orphans == []


async def test_reclaim_ignores_unrelated_last_error(tmp_path: Path) -> None:
    """A row carrying a different ``last_error`` (e.g. a revert_to_pending
    after a handler crash) is NOT eligible for reclaim.
    """
    st = await _store(tmp_path)
    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    tid = await st.try_materialize_trigger(sid, "p", past)
    assert tid is not None
    await st.mark_sent(tid)
    await st.revert_to_pending(
        tid, last_error="adapter.send_text failed: ConnectionReset"
    )
    orphans = await st.reclaim_pending_not_queued(
        set(), older_than_s=30
    )
    assert orphans == [], (
        "only explicit queue-saturation notes must drive reclaim; "
        "other retry paths are handled by the dispatcher."
    )
