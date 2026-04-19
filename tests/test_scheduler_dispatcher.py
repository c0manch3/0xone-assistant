"""Phase 5 / commit 4 — SchedulerDispatcher delivery state-machine.

Scenarios:
  1. Happy path — queue.put → consumer delivers → mark_acked + LRU populated.
  2. Handler raises → mark_pending_retry (attempts=1).
  3. attempts >= threshold on retry → mark_dead + dead-notify called.
  4. schedule disabled between put and delivery → mark_dropped, no handler.
  5. Duplicate trigger_id in LRU → skipped; _inflight cleanly discards.
  6. inflight() reflects in-progress id; cleared after finally.
  7. Handler emits no text → no adapter.send_text; still mark_acked.
  8. mark_sent skew (status='dead' at time of delivery) → no handler call.
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
        self.raise_on_send: BaseException | None = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sends.append((chat_id, text))


class _FakeHandler:
    """Mock handler — callers override `.fn` to script behaviour."""

    def __init__(self) -> None:
        self.fn: Callable[[Any, Callable[[str], Awaitable[None]]], Awaitable[None]] | None = None
        self.calls: list[Any] = []

    async def handle(self, msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        self.calls.append(msg)
        assert self.fn is not None
        await self.fn(msg, emit)


def _settings(tmp_path: Path, *, dead_threshold: int = 5) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(dead_attempts_threshold=dead_threshold),
    )


async def _setup(
    tmp_path: Path, *, dead_threshold: int = 5
) -> tuple[
    SchedulerStore,
    asyncio.Queue[ScheduledTrigger],
    SchedulerDispatcher,
    _FakeAdapter,
    _FakeHandler,
    Settings,
]:
    conn = await connect(tmp_path / "disp.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SchedulerStore(conn, lock)
    adapter = _FakeAdapter()
    handler = _FakeHandler()
    settings = _settings(tmp_path, dead_threshold=dead_threshold)
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
    return store, queue, disp, adapter, handler, settings


async def _seed_pending(store: SchedulerStore, *, prompt: str = "x") -> tuple[int, int]:
    sid = await store.insert_schedule(cron="0 9 * * *", prompt=prompt, tz="UTC")
    trig = await store.try_materialize_trigger(sid, prompt, datetime(2026, 4, 15, 9, 0, tzinfo=UTC))
    assert trig is not None
    return sid, trig


async def _drain(
    disp: SchedulerDispatcher, queue: asyncio.Queue[ScheduledTrigger]
) -> asyncio.Task[None]:
    """Start the dispatcher, wait until the queue drains and the consumer has
    time to `_deliver` the last item, then `stop()` and await.

    `_stop` is cleared on entry so a test may invoke `_drain` multiple times
    against the same dispatcher (used by the retry-to-dead scenario)."""
    disp._stop.clear()
    task: asyncio.Task[None] = asyncio.create_task(disp.run(), name="disp")
    # Poll-and-yield for queue drain. ASYNC110 flags this pattern because
    # `asyncio.Event` is usually the right tool, but we don't own the
    # dispatcher's dequeue signal — polling is the minimum-surface test
    # helper. Suppressed narrowly at the while-loop line.
    deadline = asyncio.get_event_loop().time() + 2.0
    while not queue.empty() and asyncio.get_event_loop().time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    # Give the consumer one tick to complete the last _deliver.
    await asyncio.sleep(0.1)
    disp.stop()
    await asyncio.wait_for(task, timeout=2.0)
    return task


# ---------------------------------------------------------------- tests


async def test_happy_path_marks_acked_and_populates_lru(tmp_path: Path) -> None:
    store, queue, disp, adapter, handler, _ = await _setup(tmp_path)
    sid, trig = await _seed_pending(store)

    async def ok(msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        del msg
        await emit("hello")

    handler.fn = ok

    await queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    await _drain(disp, queue)

    # Assertions.
    assert len(handler.calls) == 1
    assert adapter.sends == [(42, "hello")]
    async with store._conn.execute("SELECT status FROM triggers WHERE id=?", (trig,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "acked"
    assert trig in disp._recent_acked
    assert disp.inflight() == set()


async def test_handler_raises_moves_to_pending_retry(tmp_path: Path) -> None:
    store, queue, disp, adapter, handler, _ = await _setup(tmp_path)
    sid, trig = await _seed_pending(store)

    async def boom(msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        del msg, emit
        raise RuntimeError("kaboom")

    handler.fn = boom

    await queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    await _drain(disp, queue)

    async with store._conn.execute(
        "SELECT status, attempts FROM triggers WHERE id=?", (trig,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    status, attempts = row
    assert status == "pending"
    assert attempts == 1
    assert adapter.sends == []
    assert disp.inflight() == set()


async def test_repeated_failures_marks_dead_with_notify(tmp_path: Path) -> None:
    store, queue, disp, adapter, handler, _ = await _setup(tmp_path, dead_threshold=2)
    sid, trig = await _seed_pending(store)

    async def boom(msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        del msg, emit
        raise RuntimeError("persistent failure")

    handler.fn = boom

    # First failure: attempts=1, status=pending.
    await queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    await _drain(disp, queue)

    # Re-enqueue for the second failure to exercise the threshold path.
    # Production re-queue is done by `SchedulerLoop._reenqueue_pending_retries`
    # (fix-pack CRITICAL #1); this unit test skips the producer loop and
    # drives the dispatcher directly — see
    # `tests/test_scheduler_retry_reenqueue.py` for the end-to-end case.
    await queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=2,
        )
    )
    await _drain(disp, queue)

    async with store._conn.execute(
        "SELECT status, attempts FROM triggers WHERE id=?", (trig,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    status, attempts = row
    assert status == "dead"
    assert attempts == 2
    # Dead-notify must have been sent.
    assert len(adapter.sends) == 1
    assert "marked dead" in adapter.sends[0][1]


async def test_disabled_schedule_marks_dropped(tmp_path: Path) -> None:
    store, queue, disp, _adapter, handler, _ = await _setup(tmp_path)
    sid, trig = await _seed_pending(store)
    await store.set_enabled(sid, False)

    called = {"n": 0}

    async def never(msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        del msg, emit
        called["n"] += 1

    handler.fn = never

    await queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    await _drain(disp, queue)

    async with store._conn.execute("SELECT status FROM triggers WHERE id=?", (trig,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "dropped"
    assert called["n"] == 0


async def test_lru_dedup_skips_delivery(tmp_path: Path) -> None:
    store, queue, disp, adapter, handler, _ = await _setup(tmp_path)
    sid, trig = await _seed_pending(store)

    called = {"n": 0}

    async def bump(msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        del msg, emit
        called["n"] += 1

    handler.fn = bump

    # Seed LRU with trig.
    disp._recent_acked.append(trig)

    await queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    await _drain(disp, queue)

    assert called["n"] == 0
    assert adapter.sends == []
    assert disp.inflight() == set()


async def test_empty_emit_skips_send_text(tmp_path: Path) -> None:
    store, queue, disp, adapter, handler, _ = await _setup(tmp_path)
    sid, trig = await _seed_pending(store)

    async def silent(msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        del msg, emit
        # No emit calls.

    handler.fn = silent

    await queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    await _drain(disp, queue)

    async with store._conn.execute("SELECT status FROM triggers WHERE id=?", (trig,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "acked"
    assert adapter.sends == []


async def test_mark_sent_skew_dead_row_logs_and_returns(tmp_path: Path) -> None:
    """Wave-2 G-W2-6 regression: trigger was marked dead out-of-band (operator
    intervention). Dispatcher receives it from the queue, mark_sent precondition
    fails, handler never runs."""
    store, queue, disp, adapter, handler, _ = await _setup(tmp_path)
    sid, trig = await _seed_pending(store)
    # Force status='dead' directly.
    async with store._lock:
        await store._conn.execute("UPDATE triggers SET status='dead' WHERE id=?", (trig,))
        await store._conn.commit()

    called = {"n": 0}

    async def never(msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        del msg, emit
        called["n"] += 1

    handler.fn = never

    await queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    await _drain(disp, queue)

    assert called["n"] == 0
    assert adapter.sends == []
    assert disp.inflight() == set()


_ = pytest  # keep import for lint pattern consistency
