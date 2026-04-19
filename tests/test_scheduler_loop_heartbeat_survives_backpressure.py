"""Phase 5 fix-pack CRITICAL #4 — loop heartbeat survives queue backpressure.

Before the fix, `SchedulerLoop._tick` did `await self._queue.put(...)`
as the final step. When the dispatcher was busy for the full claude
timeout (300 s) holding an inflight slot, a second `is_due` fire would
block on `put` — and the health-check read `_last_tick_at` (which only
updates at the END of `run`'s loop body) as frozen. That would flag a
healthy-but-backpressured producer as "dead" and spam the operator.

Fix: update `_last_tick_at` BEFORE the blocking `queue.put`. A long put
is still slow, but the heartbeat reflects real liveness — we went
through `iter_enabled_schedules` and computed `is_due`, so the loop is
alive.

This test drives `_maybe_materialize` with a tiny queue (maxsize=1)
that is already full. The second `put` would block forever, but the
test checks `last_tick_at` is updated BEFORE the put ever runs.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

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
    tmp_path: Path, *, queue_max: int = 1
) -> tuple[SchedulerStore, asyncio.Queue[ScheduledTrigger], SchedulerLoop]:
    conn = await connect(tmp_path / "bp.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SchedulerStore(conn, lock)
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(tick_interval_s=1, heartbeat_stale_multiplier=2),
    )
    queue: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(maxsize=queue_max)
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
    return store, queue, loop_


async def test_heartbeat_updated_before_blocking_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, queue, loop_ = await _build(tmp_path, queue_max=1)
    try:
        # Pre-fill the queue so the NEXT `put` blocks.
        await queue.put(
            ScheduledTrigger(
                trigger_id=0,
                schedule_id=0,
                prompt="blocker",
                scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
                attempt=1,
            )
        )
        assert queue.full()

        # Insert a schedule that will materialise on the first tick.
        await store.insert_schedule(cron="* * * * *", prompt="x", tz="UTC")

        fixed = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return fixed if tz is None else fixed.astimezone(tz)

        monkeypatch.setattr("assistant.scheduler.loop.datetime", _FrozenDT)

        # Run the tick in a task; it should block on the SECOND put.
        before = loop_.last_tick_at()
        task = asyncio.create_task(loop_._tick())
        # Yield enough control for the heartbeat update + materialise
        # to happen before put blocks.
        await asyncio.sleep(0.1)

        mid = loop_.last_tick_at()

        # Cancel the blocked tick task.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await task

        assert mid > before, (
            "heartbeat must update during the tick, BEFORE the blocking put "
            f"(before={before}, mid={mid})"
        )
    finally:
        await store._conn.close()
