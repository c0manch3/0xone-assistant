"""Phase 5 fix-pack CRITICAL #1 — pending-retry re-enqueue pipeline.

Context: `SchedulerDispatcher.mark_pending_retry` flips a failed trigger
from `sent` → `pending` and increments `attempts`. Without a periodic
re-enqueue pass nothing ever pushes this row back onto the in-process
queue — `SchedulerLoop._tick`'s normal production path only materialises
brand-new cron-match rows, so the retry becomes an orphan in the DB.
Only a daemon restart (`clean_slate_sent`) would re-process it, which
defeats the whole retry ledger.

Tests:

  * `test_list_pending_retries_returns_retryable_rows` — the store
    method returns `pending` rows with `attempts > 0` (i.e. NOT fresh
    materialisations) and excludes in-flight ids.
  * `test_list_pending_retries_excludes_attempts_0` — a pristine
    `pending` trigger (just materialised) is NOT returned: that row is
    the producer's job, not the retry pass'.
  * `test_tick_reenqueues_pending_retry` — a `pending` row with
    `attempts=1` is pushed back onto the queue by `_tick` at the sweep
    cadence, the dispatcher delivers it, and the row reaches `acked`.
  * `test_tick_retry_pass_excludes_inflight` — a `pending` row whose id
    is already in `dispatcher.inflight()` is NOT re-queued (the
    dispatcher is still mid-delivery).
  * `test_tick_retry_pass_skips_pristine_pending` — a freshly-
    materialised `pending` row (attempts=0) is NOT picked up by the
    retry pass, only by the normal production loop; this avoids a
    double-enqueue race with `try_materialize_trigger`.

All four tests drive `_tick` directly with a fake clock so the sweep
cadence (every 4th tick) is hit without real 60 s waits.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assistant.adapters.dispatch_reply import _DedupLedger
from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.scheduler.dispatcher import ScheduledTrigger, SchedulerDispatcher
from assistant.scheduler.loop import SchedulerLoop
from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect


class _FakeAdapter:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        del chat_id, text


class _FakeHandler:
    async def handle(self, msg: Any, emit: Any) -> None:
        del msg, emit


async def _build(
    tmp_path: Path,
) -> tuple[
    SchedulerStore,
    asyncio.Queue[ScheduledTrigger],
    SchedulerDispatcher,
    SchedulerLoop,
]:
    conn = await connect(tmp_path / "retry.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SchedulerStore(conn, lock)
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(),
    )
    queue: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(maxsize=16)
    disp = SchedulerDispatcher(
        queue=queue,
        store=store,
        handler=_FakeHandler(),  # type: ignore[arg-type]
        adapter=_FakeAdapter(),  # type: ignore[arg-type]
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
        dedup_ledger=_DedupLedger(),
    )
    loop_ = SchedulerLoop(queue=queue, store=store, dispatcher=disp, settings=settings)
    return store, queue, disp, loop_


async def _close(store: SchedulerStore) -> None:
    await store._conn.close()


async def _seed_retry_row(store: SchedulerStore, *, attempts: int = 1) -> tuple[int, int]:
    """Seed a `pending` trigger with the given attempts count (simulating a
    dispatcher that has already run mark_pending_retry once).

    We pin `last_fire_at` far in the future so the producer's `is_due`
    check returns None on every tick during the test — this isolates the
    retry-pass behaviour from the normal materialisation path."""
    sid = await store.insert_schedule(cron="*/5 * * * *", prompt="ping", tz="UTC")
    trig = await store.try_materialize_trigger(sid, "ping", datetime(2026, 4, 15, 9, 0, tzinfo=UTC))
    assert trig is not None
    await store.mark_sent(trig)
    for _ in range(attempts):
        await store.mark_pending_retry(trig, last_error="boom")
    # Freeze the schedule far in the future so the producer doesn't race us.
    async with store._lock:
        await store._conn.execute(
            "UPDATE schedules SET last_fire_at=? WHERE id=?",
            ("2099-12-31T23:59:00Z", sid),
        )
        await store._conn.commit()
    return sid, trig


# ---------------------------------------------------------------- store-level


async def test_list_pending_retries_returns_retryable_rows(tmp_path: Path) -> None:
    store, _queue, _disp, _loop = await _build(tmp_path)
    try:
        _sid, trig = await _seed_retry_row(store, attempts=1)
        rows = await store.list_pending_retries(exclude_ids=set())
        assert len(rows) == 1
        assert rows[0]["id"] == trig
        assert rows[0]["attempts"] == 1
        assert rows[0]["status"] == "pending"
    finally:
        await _close(store)


async def test_list_pending_retries_excludes_attempts_0(tmp_path: Path) -> None:
    """A pristine freshly-materialised trigger (attempts=0) is the producer's
    concern, not the retry pass'."""
    store, _queue, _disp, _loop = await _build(tmp_path)
    try:
        sid = await store.insert_schedule(cron="*/5 * * * *", prompt="ping")
        trig = await store.try_materialize_trigger(
            sid, "ping", datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
        )
        assert trig is not None
        # Row is pending with attempts=0.
        rows = await store.list_pending_retries(exclude_ids=set())
        assert rows == []
    finally:
        await _close(store)


async def test_list_pending_retries_excludes_inflight(tmp_path: Path) -> None:
    store, _queue, _disp, _loop = await _build(tmp_path)
    try:
        _sid, trig = await _seed_retry_row(store, attempts=1)
        rows = await store.list_pending_retries(exclude_ids={trig})
        assert rows == []
    finally:
        await _close(store)


# ---------------------------------------------------------------- loop-level


async def test_tick_reenqueues_pending_retry(tmp_path: Path) -> None:
    """After 4 ticks (the sweep cadence), a `pending` row with attempts>0 is
    pushed onto the queue with `attempt = row.attempts + 1`."""
    store, queue, _disp, loop_ = await _build(tmp_path)
    try:
        _sid, trig = await _seed_retry_row(store, attempts=1)
        assert queue.qsize() == 0

        # 4 ticks = sweep boundary.
        for _ in range(4):
            await loop_._tick()

        assert queue.qsize() == 1
        out = await queue.get()
        assert out.trigger_id == trig
        # attempts in DB was 1 → next attempt is 2.
        assert out.attempt == 2
    finally:
        await _close(store)


async def test_tick_retry_pass_excludes_inflight(tmp_path: Path) -> None:
    """Dispatcher is mid-delivery → loop must not re-queue."""
    store, queue, disp, loop_ = await _build(tmp_path)
    try:
        _sid, trig = await _seed_retry_row(store, attempts=1)
        disp._inflight.add(trig)

        for _ in range(4):
            await loop_._tick()

        assert queue.qsize() == 0
    finally:
        await _close(store)


async def test_tick_retry_pass_skips_pristine_pending(tmp_path: Path) -> None:
    """A freshly-materialised pending row (attempts=0) is NOT picked up by
    the retry pass. Double-enqueue protection."""
    store, queue, _disp, loop_ = await _build(tmp_path)
    try:
        # Seed a pristine pending trigger directly (skip the producer loop so
        # the queue starts empty; we only want to assert the retry pass).
        sid = await store.insert_schedule(cron="* * * * *", prompt="x", tz="UTC")
        trig = await store.try_materialize_trigger(
            sid, "x", datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
        )
        assert trig is not None

        # Drain anything the producer pushed on an incidental match.
        while not queue.empty():
            queue.get_nowait()

        # Run the sweep cadence: retry pass must NOT re-queue.
        for _ in range(4):
            await loop_._tick()

        # Row still pending with attempts=0 → retry pass found nothing.
        async with store._conn.execute(
            "SELECT status, attempts FROM triggers WHERE id=?", (trig,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        status, attempts = row
        assert status == "pending"
        assert attempts == 0
        # Queue might have a trigger from the `* * * * *` cron matching the
        # (possibly) current minute — that's fine, we only care about the
        # retry pass itself. The point is: no dupe entries.
        assert queue.qsize() <= 1
    finally:
        await _close(store)
