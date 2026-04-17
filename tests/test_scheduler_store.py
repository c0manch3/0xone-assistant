"""Phase 5 / commit 3 — SchedulerStore CRUD, atomicity, transitions.

Covers:
  * schedule CRUD (insert/list/delete/set_enabled + cap semantics).
  * `try_materialize_trigger` atomic INSERT-OR-IGNORE + last_fire_at UPDATE
    (plan §5.3 / spike S-8).
  * Every `mark_*` transition with its status precondition (wave-2 G-W2-6):
    wrong starting state → rowcount=0, returns False, does NOT mutate the row.
  * `clean_slate_sent` affects only `sent` rows; `revert_stuck_sent` honours
    `exclude_ids` AND the timeout.
  * `count_catchup_misses` with `COALESCE(last_fire_at, created_at)` lower
    bound capped at 4x catchup_window_s (wave-2 G-W2-4).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect


async def _mkstore(tmp_path: Path) -> tuple[SchedulerStore, asyncio.Lock]:
    conn = await connect(tmp_path / "sched.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    return SchedulerStore(conn, lock), lock


async def _close(store: SchedulerStore) -> None:
    await store._conn.close()


# ---------------------------------------------------------------- CRUD


async def test_insert_and_list_schedules(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid1 = await store.insert_schedule(cron="0 9 * * *", prompt="daily", tz="UTC")
        sid2 = await store.insert_schedule(cron="*/15 * * * *", prompt="fast", tz="Europe/Berlin")
        rows = await store.list_schedules()
        assert len(rows) == 2
        assert rows[0]["id"] == sid1
        assert rows[0]["cron"] == "0 9 * * *"
        assert rows[1]["id"] == sid2
        assert rows[1]["tz"] == "Europe/Berlin"
        assert rows[0]["enabled"] is True
    finally:
        await _close(store)


async def test_set_enabled_and_delete(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        assert await store.set_enabled(sid, False) is True
        got = await store.get_schedule(sid)
        assert got is not None
        assert got["enabled"] is False
        # list_schedules(enabled_only=True) excludes disabled.
        active = await store.list_schedules(enabled_only=True)
        assert len(active) == 0
        # Hard delete.
        assert await store.delete_schedule(sid) is True
        assert await store.get_schedule(sid) is None
    finally:
        await _close(store)


async def test_count_enabled(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        assert await store.count_enabled() == 0
        s1 = await store.insert_schedule(cron="0 9 * * *", prompt="a")
        await store.insert_schedule(cron="0 10 * * *", prompt="b")
        assert await store.count_enabled() == 2
        await store.set_enabled(s1, False)
        assert await store.count_enabled() == 1
    finally:
        await _close(store)


# ---------------------------------------------------------------- materialize


async def test_try_materialize_inserts_and_advances_last_fire(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        t = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
        trig = await store.try_materialize_trigger(sid, "x", t)
        assert trig is not None
        sched = await store.get_schedule(sid)
        assert sched is not None
        assert sched["last_fire_at"] == "2026-04-15T09:00:00Z"
        # Check trigger row
        async with store._conn.execute(
            "SELECT status, scheduled_for FROM triggers WHERE id=?", (trig,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "pending"
        assert row[1] == "2026-04-15T09:00:00Z"
    finally:
        await _close(store)


async def test_try_materialize_idempotent_on_unique_violation(tmp_path: Path) -> None:
    """Second INSERT with same (schedule_id, scheduled_for) is a no-op AND
    last_fire_at is NOT advanced on the second call (which would violate the
    invariant since it could only ever equal itself)."""
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        t = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)

        first = await store.try_materialize_trigger(sid, "x", t)
        assert first is not None

        # Mutate last_fire_at to a detectable sentinel so we can prove the
        # second call does not re-stamp it with the same value-by-coincidence.
        async with store._lock:
            await store._conn.execute(
                "UPDATE schedules SET last_fire_at=? WHERE id=?",
                ("SENTINEL", sid),
            )
            await store._conn.commit()

        second = await store.try_materialize_trigger(sid, "x", t)
        assert second is None

        sched = await store.get_schedule(sid)
        assert sched is not None
        assert sched["last_fire_at"] == "SENTINEL"
    finally:
        await _close(store)


# ---------------------------------------------------------------- transitions


async def _seed_pending(store: SchedulerStore, sid: int) -> int:
    t = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
    trig = await store.try_materialize_trigger(sid, "x", t)
    assert trig is not None
    return trig


async def _get_status(store: SchedulerStore, trig: int) -> str:
    async with store._conn.execute("SELECT status FROM triggers WHERE id=?", (trig,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def test_mark_sent_only_from_pending(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        trig = await _seed_pending(store, sid)
        assert await store.mark_sent(trig) is True
        # Second call: status is now 'sent' — precondition fails.
        assert await store.mark_sent(trig) is False
        assert await _get_status(store, trig) == "sent"
    finally:
        await _close(store)


async def test_mark_acked_only_from_sent(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        trig = await _seed_pending(store, sid)
        # pending → acked directly must FAIL (skew).
        assert await store.mark_acked(trig) is False
        assert await _get_status(store, trig) == "pending"
        # happy path.
        assert await store.mark_sent(trig) is True
        assert await store.mark_acked(trig) is True
        assert await _get_status(store, trig) == "acked"
    finally:
        await _close(store)


async def test_mark_pending_retry_only_from_sent(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        trig = await _seed_pending(store, sid)
        # pending → pending_retry: skew; does NOT double-increment attempts.
        attempts0 = await store.mark_pending_retry(trig, last_error="x")
        assert attempts0 == 0  # starting row has attempts=0
        # Move to sent, then retry.
        await store.mark_sent(trig)
        attempts1 = await store.mark_pending_retry(trig, last_error="x")
        assert attempts1 == 1
        assert await _get_status(store, trig) == "pending"
    finally:
        await _close(store)


async def test_mark_dead_only_from_pending(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        trig = await _seed_pending(store, sid)
        assert await store.mark_dead(trig, last_error="boom") is True
        # Idempotent: already dead → False.
        assert await store.mark_dead(trig, last_error="boom") is False
        assert await _get_status(store, trig) == "dead"
    finally:
        await _close(store)


async def test_mark_dropped_from_pending_or_sent(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        trig1 = await _seed_pending(store, sid)
        assert await store.mark_dropped(trig1, reason="disabled") is True
        assert await _get_status(store, trig1) == "dropped"

        # dropped → dropped must skew (not from pending/sent).
        assert await store.mark_dropped(trig1, reason="again") is False
    finally:
        await _close(store)


# ---------------------------------------------------------------- recovery


async def test_clean_slate_reverts_only_sent(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        t_pending = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
        t_sent = datetime(2026, 4, 15, 9, 5, tzinfo=UTC)
        t_acked = datetime(2026, 4, 15, 9, 10, tzinfo=UTC)
        t_dead = datetime(2026, 4, 15, 9, 15, tzinfo=UTC)

        trig_pending = await store.try_materialize_trigger(sid, "x", t_pending)
        trig_sent = await store.try_materialize_trigger(sid, "x", t_sent)
        trig_acked = await store.try_materialize_trigger(sid, "x", t_acked)
        trig_dead = await store.try_materialize_trigger(sid, "x", t_dead)
        assert trig_sent is not None
        assert trig_acked is not None
        assert trig_dead is not None
        assert trig_pending is not None

        await store.mark_sent(trig_sent)
        await store.mark_sent(trig_acked)
        await store.mark_acked(trig_acked)
        await store.mark_dead(trig_dead, last_error="x")

        # Seed another sent row to confirm counting.
        trig_second_sent = await store.try_materialize_trigger(
            sid, "x", datetime(2026, 4, 15, 9, 20, tzinfo=UTC)
        )
        assert trig_second_sent is not None
        await store.mark_sent(trig_second_sent)

        reverted = await store.clean_slate_sent()
        assert reverted == 2

        assert await _get_status(store, trig_pending) == "pending"
        assert await _get_status(store, trig_sent) == "pending"
        assert await _get_status(store, trig_acked) == "acked"
        assert await _get_status(store, trig_dead) == "dead"
        assert await _get_status(store, trig_second_sent) == "pending"
    finally:
        await _close(store)


async def test_revert_stuck_sent_honours_exclude_and_timeout(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        t_fresh = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
        t_stale = datetime(2026, 4, 15, 9, 5, tzinfo=UTC)

        trig_fresh = await store.try_materialize_trigger(sid, "x", t_fresh)
        trig_stale = await store.try_materialize_trigger(sid, "x", t_stale)
        assert trig_fresh is not None
        assert trig_stale is not None

        # Both transitioned to 'sent'.
        await store.mark_sent(trig_fresh)
        await store.mark_sent(trig_stale)

        # Back-date one trigger's sent_at by 10 minutes (> 360 s timeout).
        async with store._lock:
            await store._conn.execute(
                "UPDATE triggers SET sent_at=? WHERE id=?",
                ("2020-01-01T00:00:00Z", trig_stale),
            )
            await store._conn.commit()

        reverted = await store.revert_stuck_sent(timeout_s=360, exclude_ids=set())
        assert reverted == 1
        assert await _get_status(store, trig_fresh) == "sent"
        assert await _get_status(store, trig_stale) == "pending"

        # Reset: put it back to sent AND stale again.
        await store.mark_sent(trig_stale)
        async with store._lock:
            await store._conn.execute(
                "UPDATE triggers SET sent_at=? WHERE id=?",
                ("2020-01-01T00:00:00Z", trig_stale),
            )
            await store._conn.commit()

        # Exclude the stale one (in-flight guard) → should NOT be reverted.
        reverted = await store.revert_stuck_sent(timeout_s=360, exclude_ids={trig_stale})
        assert reverted == 0
        assert await _get_status(store, trig_stale) == "sent"
    finally:
        await _close(store)


# ---------------------------------------------------------------- catchup


async def test_count_catchup_misses_caps_at_four_windows(tmp_path: Path) -> None:
    """Schedule created long ago, last_fire_at=NULL, every-minute cron:
    cap should limit count to floor((4x catchup - catchup)/60) = 180 (3 hours
    worth at 1/min)."""
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="* * * * *", prompt="x")
        # Back-date created_at to 10 hours ago.
        old = datetime(2026, 4, 15, 0, 0, tzinfo=UTC)
        async with store._lock:
            await store._conn.execute(
                "UPDATE schedules SET created_at=?, last_fire_at=NULL WHERE id=?",
                ("2026-04-14T22:00:00Z", sid),
            )
            await store._conn.commit()
        now = datetime(2026, 4, 15, 8, 0, tzinfo=UTC)
        # lower_cap = now - 4h = 04:00; upper = now - 1h = 07:00.
        # Minutes in (04:00, 07:00] with `* * * * *` → 180.
        n = await store.count_catchup_misses(now=now, catchup_window_s=3600, tz_default="UTC")
        assert n == 180
        _ = old  # silence unused
    finally:
        await _close(store)


async def test_count_catchup_misses_uses_last_fire_at_when_present(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="*/30 * * * *", prompt="x")
        async with store._lock:
            await store._conn.execute(
                "UPDATE schedules SET last_fire_at=? WHERE id=?",
                ("2026-04-15T06:00:00Z", sid),
            )
            await store._conn.commit()
        now = datetime(2026, 4, 15, 8, 0, tzinfo=UTC)
        # lower = 06:00 (after last_fire_at, inside 4h cap).
        # upper = now - 1h = 07:00.
        # Matches in (06:00, 07:00]: 06:30 and 07:00 → 2.
        n = await store.count_catchup_misses(now=now, catchup_window_s=3600, tz_default="UTC")
        assert n == 2
    finally:
        await _close(store)


async def test_count_catchup_skips_parse_failures(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        # Seed a bad cron directly (bypassing insert_schedule validation —
        # the CLI validates, but the DB column is TEXT NOT NULL).
        async with store._lock:
            await store._conn.execute(
                "INSERT INTO schedules(cron, prompt, tz, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("not a cron", "x", "UTC", 1, "2026-04-14T22:00:00Z"),
            )
            await store._conn.commit()
        now = datetime(2026, 4, 15, 8, 0, tzinfo=UTC)
        # Should not raise — bad cron is skipped, no matches counted.
        n = await store.count_catchup_misses(now=now, catchup_window_s=3600, tz_default="UTC")
        assert n == 0
    finally:
        await _close(store)


async def test_fk_cascade_drops_triggers_on_schedule_delete(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x")
        t = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
        trig = await store.try_materialize_trigger(sid, "x", t)
        assert trig is not None
        assert await store.delete_schedule(sid) is True
        # Trigger row gone via cascade.
        async with store._conn.execute("SELECT COUNT(*) FROM triggers WHERE id=?", (trig,)) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0
    finally:
        await _close(store)


async def test_recent_triggers_order_and_limit(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        sid = await store.insert_schedule(cron="* * * * *", prompt="x")
        base = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
        trigs = []
        for i in range(5):
            t = base + timedelta(minutes=i)
            tid = await store.try_materialize_trigger(sid, "x", t)
            assert tid is not None
            trigs.append(tid)
        rows = await store.recent_triggers(schedule_id=sid, limit=3)
        assert len(rows) == 3
        # DESC on id → most recent first.
        assert rows[0]["id"] == trigs[-1]
        assert rows[2]["id"] == trigs[-3]
    finally:
        await _close(store)


# ---------------------------------------------------------------- mypy helper
# pytest "unused import" guard for explicit asyncio ref.
_ = pytest
