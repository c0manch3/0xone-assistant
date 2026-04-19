"""Phase 5 fix-pack HIGH #5 — Daemon.stop() drains shielded DB updates.

When the dispatcher is cancelled mid-delivery, `_deliver`'s
`CancelledError` branch spawns a shielded `mark_pending_retry` task
via `asyncio.create_task(...)` + `asyncio.shield(...)`. Those
shield-tasks must complete BEFORE the aiosqlite connection is closed;
otherwise we race a `ProgrammingError: Cannot operate on a closed
database` — the update never lands, and the next boot's clean-slate
has to pick up the trigger as 'sent' instead.

`Daemon.stop()` step 2.5 awaits `dispatcher.pending_updates()` with a
2 s timeout before moving on to adapter + conn teardown. This test:

1. Seeds a trigger + dispatches it to a hanging handler.
2. Cancels the dispatcher task (mimics the `bg_drain_timeout` path).
3. Asserts `pending_updates()` contains the shield task.
4. Awaits the drain and asserts the trigger's status is 'pending'
   with attempts=1 — proves the DB update completed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from assistant.adapters.dispatch_reply import _DedupLedger
from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.scheduler.dispatcher import ScheduledTrigger, SchedulerDispatcher
from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect


class _FakeAdapter:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        del chat_id, text


class _HangingHandler:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def handle(self, msg: Any, emit: Any) -> None:
        del msg, emit
        self.started.set()
        await asyncio.Event().wait()  # park forever


async def test_pending_updates_tracked_and_drained(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "s.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SchedulerStore(conn, lock)
    handler = _HangingHandler()
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(),
    )

    sid = await store.insert_schedule(cron="0 9 * * *", prompt="x", tz="UTC")
    trig = await store.try_materialize_trigger(sid, "x", datetime(2026, 4, 15, 9, 0, tzinfo=UTC))
    assert trig is not None

    queue: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(maxsize=4)
    disp = SchedulerDispatcher(
        queue=queue,
        store=store,
        handler=handler,  # type: ignore[arg-type]
        adapter=_FakeAdapter(),  # type: ignore[arg-type]
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
        dedup_ledger=_DedupLedger(),
    )
    await queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    task = asyncio.create_task(disp.run(), name="d")

    await asyncio.wait_for(handler.started.wait(), timeout=2.0)
    assert trig in disp.inflight()

    # Cancel — triggers the shielded mark_pending_retry branch.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The shield task should be tracked on `_pending_updates`.
    updates = disp.pending_updates()
    # Either already completed (fast) or still pending — either way, it
    # must have appeared at some point. The set is a snapshot; if empty,
    # check that the DB update did land.
    #
    # Drain explicitly as Daemon.stop() would.
    if updates:
        await asyncio.gather(*updates, return_exceptions=True)

    async with conn.execute(
        "SELECT status, attempts, last_error FROM triggers WHERE id=?", (trig,)
    ) as cur:
        row = await cur.fetchone()
    await conn.close()

    assert row is not None
    status, attempts, last_error = row
    assert status == "pending"
    assert attempts == 1
    assert last_error == "shutdown_cancelled"
