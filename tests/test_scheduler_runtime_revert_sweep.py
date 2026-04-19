"""Phase 5 / wave-2 B-W2-1 regression.

Runtime sweep is wired into `SchedulerLoop._tick()` every 4th iteration.
Without it, a dispatcher crash (which empties `_inflight` but leaves the
trigger row in `status='sent'`) only recovers at the next daemon boot's
`clean_slate_sent`, which can be hours away on an always-on host.

Two cases:

  * `not in inflight` → sweep reverts the stuck row (`exclude_ids=set()`).
  * `in inflight` → sweep excludes it (dispatcher is actively delivering).

Both cases use `_tick()` directly to keep test time bounded; the
sweep-every-N-ticks cadence is driven by the `_tick_count` counter.
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
    tmp_path: Path, *, revert_timeout_s: int = 360
) -> tuple[
    SchedulerStore,
    asyncio.Queue[ScheduledTrigger],
    SchedulerDispatcher,
    SchedulerLoop,
]:
    conn = await connect(tmp_path / "sweep.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SchedulerStore(conn, lock)
    adapter = _FakeAdapter()
    handler = _FakeHandler()
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(sent_revert_timeout_s=revert_timeout_s),
    )
    queue: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(maxsize=8)
    disp = SchedulerDispatcher(
        queue=queue,
        store=store,
        handler=handler,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
        dedup_ledger=_DedupLedger(),
    )
    loop_ = SchedulerLoop(queue=queue, store=store, dispatcher=disp, settings=settings)
    return store, queue, disp, loop_


async def _close(store: SchedulerStore) -> None:
    await store._conn.close()


async def _seed_stuck_sent(store: SchedulerStore) -> int:
    """Create a schedule + trigger frozen in `status='sent'` with a
    sent_at older than the sweep timeout."""
    sid = await store.insert_schedule(cron="* * * * *", prompt="x", tz="UTC")
    trig = await store.try_materialize_trigger(sid, "x", datetime(2026, 4, 15, 9, 0, tzinfo=UTC))
    assert trig is not None
    await store.mark_sent(trig)
    async with store._lock:
        await store._conn.execute(
            "UPDATE triggers SET sent_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", trig),
        )
        await store._conn.commit()
    return trig


async def test_runtime_sweep_reverts_stuck_sent_not_in_inflight(tmp_path: Path) -> None:
    store, _queue, disp, loop_ = await _build(tmp_path)
    try:
        trig = await _seed_stuck_sent(store)
        assert disp.inflight() == set()

        # Drive 4 ticks — the 4th triggers the sweep.
        for _ in range(4):
            await loop_._tick()

        async with store._conn.execute(
            "SELECT status, attempts FROM triggers WHERE id=?", (trig,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        status, attempts = row
        assert status == "pending"
        assert attempts >= 1
    finally:
        await _close(store)


async def test_runtime_sweep_skips_inflight(tmp_path: Path) -> None:
    store, _queue, disp, loop_ = await _build(tmp_path)
    try:
        trig = await _seed_stuck_sent(store)
        # Simulate the dispatcher being mid-delivery.
        disp._inflight.add(trig)

        for _ in range(4):
            await loop_._tick()

        async with store._conn.execute("SELECT status FROM triggers WHERE id=?", (trig,)) as cur:
            row = await cur.fetchone()
        assert row is not None
        # Sweep must NOT revert an in-flight trigger.
        assert row[0] == "sent"
    finally:
        await _close(store)
