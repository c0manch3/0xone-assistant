"""Phase 5 / commit 5 — SchedulerLoop producer cases.

We drive the loop's `_tick()` directly — each test constructs a loop
with a fake clock view via the injected settings and calls `_tick()` in
isolation so test-time is O(ms), not O(seconds). `run()` is exercised
via a focused path-test that verifies the outer try/except calls the
notify hook.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

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


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(**overrides),
    )


async def _setup(
    tmp_path: Path, **sched_overrides: Any
) -> tuple[
    SchedulerStore,
    asyncio.Queue[ScheduledTrigger],
    SchedulerDispatcher,
    SchedulerLoop,
    Settings,
]:
    conn = await connect(tmp_path / "loop.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SchedulerStore(conn, lock)
    adapter = _FakeAdapter()
    handler = _FakeHandler()
    settings = _settings(tmp_path, **sched_overrides)
    queue: asyncio.Queue[ScheduledTrigger] = asyncio.Queue(maxsize=8)
    disp = SchedulerDispatcher(
        queue=queue,
        store=store,
        handler=handler,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
    )
    loop_ = SchedulerLoop(queue=queue, store=store, dispatcher=disp, settings=settings)
    return store, queue, disp, loop_, settings


async def _close(store: SchedulerStore) -> None:
    await store._conn.close()


# ---------------------------------------------------------------- basic produce


async def test_single_schedule_materializes_on_first_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, queue, _, loop_, _ = await _setup(tmp_path)
    try:
        await store.insert_schedule(cron="*/5 * * * *", prompt="ping", tz="UTC")
        # Freeze clock at 09:00 UTC (matches `*/5`).
        fixed = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return fixed if tz is None else fixed.astimezone(tz)

        monkeypatch.setattr("assistant.scheduler.loop.datetime", _FrozenDT)
        await loop_._tick()

        assert queue.qsize() == 1
        t = await queue.get()
        assert t.prompt == "ping"
        assert t.scheduled_for == fixed
    finally:
        await _close(store)


async def test_three_consecutive_ticks_same_minute_produce_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, queue, _, loop_, _ = await _setup(tmp_path)
    try:
        await store.insert_schedule(cron="*/5 * * * *", prompt="ping", tz="UTC")
        base = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
        times = [base, base + timedelta(seconds=15), base + timedelta(seconds=30)]

        class _ClockHolder:
            current = base

        class _StepDT(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                nt = _ClockHolder.current
                return nt if tz is None else nt.astimezone(tz)

        monkeypatch.setattr("assistant.scheduler.loop.datetime", _StepDT)
        for t_now in times:
            _ClockHolder.current = t_now
            await loop_._tick()
        assert queue.qsize() == 1
    finally:
        await _close(store)


async def test_long_gap_returns_latest_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, queue, _, loop_, _ = await _setup(tmp_path)
    try:
        sid = await store.insert_schedule(cron="*/5 * * * *", prompt="ping", tz="UTC")
        # Seed last_fire_at to 08:00 UTC; now = 09:02 → latest match within
        # catchup window of 3600s is 09:00.
        async with store._lock:
            await store._conn.execute(
                "UPDATE schedules SET last_fire_at=? WHERE id=?",
                ("2026-04-15T08:00:00Z", sid),
            )
            await store._conn.commit()

        fixed = datetime(2026, 4, 15, 9, 2, tzinfo=UTC)

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return fixed if tz is None else fixed.astimezone(tz)

        monkeypatch.setattr("assistant.scheduler.loop.datetime", _FrozenDT)
        await loop_._tick()

        t = await queue.get()
        assert t.scheduled_for == datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
    finally:
        await _close(store)


async def test_outside_catchup_window_produces_no_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, queue, _, loop_, _ = await _setup(tmp_path)
    try:
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="ping", tz="UTC")
        async with store._lock:
            await store._conn.execute(
                "UPDATE schedules SET last_fire_at=? WHERE id=?",
                ("2026-04-14T09:00:00Z", sid),
            )
            await store._conn.commit()

        fixed = datetime(2026, 4, 15, 10, 30, tzinfo=UTC)

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return fixed if tz is None else fixed.astimezone(tz)

        monkeypatch.setattr("assistant.scheduler.loop.datetime", _FrozenDT)
        await loop_._tick()
        assert queue.qsize() == 0
    finally:
        await _close(store)


async def test_disabled_schedule_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, queue, _, loop_, _ = await _setup(tmp_path)
    try:
        sid = await store.insert_schedule(cron="* * * * *", prompt="ping", tz="UTC")
        await store.set_enabled(sid, False)
        fixed = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return fixed if tz is None else fixed.astimezone(tz)

        monkeypatch.setattr("assistant.scheduler.loop.datetime", _FrozenDT)
        await loop_._tick()
        assert queue.qsize() == 0
    finally:
        await _close(store)


async def test_malformed_cron_warns_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, queue, _, loop_, _ = await _setup(tmp_path)
    try:
        # Seed a bad cron directly (bypassing parser-level validation).
        async with store._lock:
            await store._conn.execute(
                "INSERT INTO schedules(cron, prompt, tz) VALUES (?, ?, ?)",
                ("not a cron", "ping", "UTC"),
            )
            await store._conn.execute(
                "INSERT INTO schedules(cron, prompt, tz) VALUES (?, ?, ?)",
                ("* * * * *", "ok", "UTC"),
            )
            await store._conn.commit()

        fixed = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return fixed if tz is None else fixed.astimezone(tz)

        monkeypatch.setattr("assistant.scheduler.loop.datetime", _FrozenDT)
        await loop_._tick()

        # The second row (valid cron) still produces a trigger.
        assert queue.qsize() == 1
        t = await queue.get()
        assert t.prompt == "ok"
    finally:
        await _close(store)


async def test_invalid_tz_warns_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, queue, _, loop_, _ = await _setup(tmp_path)
    try:
        async with store._lock:
            await store._conn.execute(
                "INSERT INTO schedules(cron, prompt, tz) VALUES (?, ?, ?)",
                ("* * * * *", "bad", "Not/AValidTZ"),
            )
            await store._conn.execute(
                "INSERT INTO schedules(cron, prompt, tz) VALUES (?, ?, ?)",
                ("* * * * *", "ok", "UTC"),
            )
            await store._conn.commit()

        fixed = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)

        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return fixed if tz is None else fixed.astimezone(tz)

        monkeypatch.setattr("assistant.scheduler.loop.datetime", _FrozenDT)
        await loop_._tick()
        assert queue.qsize() == 1
        t = await queue.get()
        assert t.prompt == "ok"
    finally:
        await _close(store)


# ---------------------------------------------------------------- run() + notify


async def test_run_notifies_on_fatal_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _, _, loop_, _ = await _setup(tmp_path)
    try:
        notifications: list[str] = []

        async def notify(msg: str) -> None:
            notifications.append(msg)

        loop_._notify = notify

        # Force _tick to raise CancelledError first time (simulates fatal).
        calls = {"n": 0}

        async def bad_tick() -> None:
            calls["n"] += 1
            raise RuntimeError("test-fatal")

        # Simulate: wrap run's `while` path to raise at the outermost scope.
        # Since the per-tick except captures everything, we force the fatal
        # path by patching wait_for to raise.
        async def raise_fatal(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("fatal-via-wait")

        monkeypatch.setattr("asyncio.wait_for", raise_fatal)

        # Patch _tick to a known benign body so we hit wait_for.
        monkeypatch.setattr(loop_, "_tick", bad_tick)
        with pytest.raises(RuntimeError):
            await loop_.run()
        assert len(notifications) == 1
        assert "fatal-via-wait" in notifications[0]
    finally:
        await _close(store)


async def test_count_catchup_misses_delegates_to_store(
    tmp_path: Path,
) -> None:
    store, _, _, loop_, _ = await _setup(tmp_path)
    try:
        # Empty DB → 0 misses.
        n = await loop_.count_catchup_misses()
        assert n == 0
    finally:
        await _close(store)
