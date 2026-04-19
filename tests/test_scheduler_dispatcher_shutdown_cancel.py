"""Phase 5 / wave-2 B-W2-3 regression.

When the dispatcher task is cancelled mid-`_deliver` (SIGTERM path), the
`mark_pending_retry` DB UPDATE must complete to end-of-statement — if we
forget `asyncio.shield` the UPDATE is cancelled too and the trigger row
stays in `status='sent'`, which leaks to the next boot's clean-slate.

Setup:
  * Patch the handler to `await asyncio.Event().wait()` (hangs forever).
  * Start dispatcher.run() as a task.
  * Wait until _inflight picks up the trigger (proves delivery started).
  * Cancel the dispatcher task.
  * Assert trigger row → `status='pending'`, `attempts=1`,
    `last_error='shutdown_cancelled'`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
    def __init__(self) -> None:
        self.sends: list[tuple[int, str]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sends.append((chat_id, text))


class _HangingHandler:
    """Handler that never returns — simulates a real model call interrupted
    by SIGTERM mid-stream."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def handle(self, msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        del msg, emit
        self.started.set()
        # Park forever.
        await asyncio.Event().wait()


async def test_cancelled_deliver_shielded_mark_pending(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "cancel.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SchedulerStore(conn, lock)
    adapter = _FakeAdapter()
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
        adapter=adapter,  # type: ignore[arg-type]
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
    task = asyncio.create_task(disp.run(), name="disp-cancel")

    # Wait until handler is actually running (trigger moved to sent).
    await asyncio.wait_for(handler.started.wait(), timeout=2.0)
    assert trig in disp.inflight()

    # Cancel the task (mimics SIGTERM → Daemon.stop() flow).
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Shielded UPDATE must have landed — status is `pending`, attempts=1,
    # last_error='shutdown_cancelled'.
    async with conn.execute(
        "SELECT status, attempts, last_error FROM triggers WHERE id=?", (trig,)
    ) as cur:
        row = await cur.fetchone()
    await conn.close()
    assert row is not None
    status, attempts, last_error = row
    assert status == "pending", f"without shield, status would still be 'sent'; got {status!r}"
    assert attempts == 1
    assert last_error == "shutdown_cancelled"
