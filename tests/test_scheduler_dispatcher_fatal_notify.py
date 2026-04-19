"""Phase 5 fix-pack HIGH #4 — dispatcher `run()` outer fatal notify.

Mirrors `SchedulerLoop.run`'s wave-2 N-W2-4 behaviour: when the drain
loop raises outside the per-delivery `try/except`, we log
`scheduler_dispatcher_fatal` and fire `notify_fn` once. Ordinary
shutdown (`CancelledError`) does NOT notify.
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


class _FakeHandler:
    def __init__(self) -> None:
        self.fn: Callable[[Any, Callable[[str], Awaitable[None]]], Awaitable[None]] | None = None

    async def handle(self, msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        assert self.fn is not None
        await self.fn(msg, emit)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(),
    )


async def test_fatal_triggers_notify_once(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "f.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SchedulerStore(conn, lock)
    adapter = _FakeAdapter()
    handler = _FakeHandler()
    settings = _settings(tmp_path)

    notifies: list[str] = []

    async def notify(msg: str) -> None:
        notifies.append(msg)

    queue: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(maxsize=2)
    disp = SchedulerDispatcher(
        queue=queue,
        store=store,
        handler=handler,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
        notify_fn=notify,
        dedup_ledger=_DedupLedger(),
    )

    # Patch the queue.get path so the FIRST wait_for raises a fatal exception
    # OUTSIDE the per-delivery try/except — simulating a crash in the
    # dequeue plumbing (e.g. a TypeError from a third-party hook).
    real_wait_for = asyncio.wait_for
    call_count = {"n": 0}

    # Signature mirrors `asyncio.wait_for` for the monkeypatch; the
    # `timeout` parameter trips ASYNC109 but is required.
    async def bad_wait_for(
        awaitable: Any,
        timeout: float,  # noqa: ASYNC109
    ) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("fatal-dispatcher")
        return await real_wait_for(awaitable, timeout=timeout)

    import assistant.scheduler.dispatcher as disp_mod

    orig_wait_for = disp_mod.asyncio.wait_for
    disp_mod.asyncio.wait_for = bad_wait_for  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            await disp.run()
    finally:
        disp_mod.asyncio.wait_for = orig_wait_for  # type: ignore[assignment]
        await conn.close()

    assert len(notifies) == 1
    assert "dispatcher" in notifies[0].lower()
    assert "fatal-dispatcher" in notifies[0]


async def test_cancelled_error_does_not_notify(tmp_path: Path) -> None:
    """Ordinary SIGTERM → disp.stop() → CancelledError must NOT notify."""
    conn = await connect(tmp_path / "f.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SchedulerStore(conn, lock)
    adapter = _FakeAdapter()
    handler = _FakeHandler()
    settings = _settings(tmp_path)

    notifies: list[str] = []

    async def notify(msg: str) -> None:
        notifies.append(msg)

    # Handler hangs forever; cancellation mid-delivery exercises the
    # shielded mark_pending_retry branch and bubbles CancelledError.
    async def hang(msg: Any, emit: Any) -> None:
        del msg, emit
        await asyncio.Event().wait()

    handler.fn = hang

    sid = await store.insert_schedule(cron="0 9 * * *", prompt="x", tz="UTC")
    trig = await store.try_materialize_trigger(sid, "x", datetime(2026, 4, 15, 9, 0, tzinfo=UTC))
    assert trig is not None

    queue: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(maxsize=2)
    await queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    disp = SchedulerDispatcher(
        queue=queue,
        store=store,
        handler=handler,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
        notify_fn=notify,
        dedup_ledger=_DedupLedger(),
    )

    task = asyncio.create_task(disp.run(), name="d")
    # wait until the handler actually starts
    for _ in range(20):
        await asyncio.sleep(0.02)
        if trig in disp.inflight():
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await conn.close()

    assert notifies == [], f"CancelledError must not notify: {notifies!r}"
