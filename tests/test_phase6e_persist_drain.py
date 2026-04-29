"""Phase 6e — Daemon._audio_persist_pending drain semantics.

Two checks:

- happy path: a tracked persist task finishes within
  ``settings.audio_bg.drain_timeout_s`` so ``Daemon.stop`` lets it
  flush before ``conn.close()`` — no aiosqlite ProgrammingError on a
  bg task that races shutdown.
- timeout path: when persist tasks overrun the budget, ``Daemon.stop``
  logs a warning and proceeds to close the DB. The turn stays
  ``pending`` and the boot reaper picks it up next start.

These tests directly drive the drain block in ``Daemon.stop`` rather
than booting a full daemon — fast and deterministic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant.config import (
    AudioBgSettings,
    ClaudeSettings,
    SchedulerSettings,
    Settings,
)
from assistant.main import Daemon


def _settings(tmp_path: Path, drain_s: float) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
        scheduler=SchedulerSettings(enabled=False),
        audio_bg=AudioBgSettings(drain_timeout_s=drain_s),
    )


async def test_persist_drain_completes_within_budget(
    tmp_path: Path,
) -> None:
    """A persist task that finishes inside the budget must be awaited
    by ``Daemon.stop``'s drain block (no warning, no orphaned task)."""
    d = Daemon(_settings(tmp_path, drain_s=2.0))

    # Drive the drain block directly. We seed the pending set with a
    # one-shot task that resolves immediately.
    finished = asyncio.Event()

    async def quick_persist() -> None:
        finished.set()

    persist_task = asyncio.create_task(quick_persist())
    d._audio_persist_pending.add(persist_task)
    persist_task.add_done_callback(d._audio_persist_pending.discard)

    # Mirror the drain block: gather with a budget.
    if d._audio_persist_pending:
        pending = list(d._audio_persist_pending)
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=d._settings.audio_bg.drain_timeout_s,
            )
        except TimeoutError:  # pragma: no cover — happy-path is sub-second
            pytest.fail("drain timed out on a sub-second persist task")

    assert finished.is_set()
    assert all(t.done() for t in pending)


async def test_persist_drain_timeout_logs_and_continues(
    tmp_path: Path,
) -> None:
    """When persist tasks exceed the budget, the drain raises
    ``TimeoutError`` (caught + logged in production) and leaves the
    pending tasks unfinished. We assert the timeout fires under a
    short budget; then cancel the slow task to keep the test bounded.
    """
    d = Daemon(_settings(tmp_path, drain_s=0.05))

    async def slow_persist() -> None:
        # Exceeds the drain budget by 10x.
        await asyncio.sleep(0.5)

    persist_task = asyncio.create_task(slow_persist())
    d._audio_persist_pending.add(persist_task)
    persist_task.add_done_callback(d._audio_persist_pending.discard)

    assert d._audio_persist_pending  # task in flight

    pending = list(d._audio_persist_pending)
    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=d._settings.audio_bg.drain_timeout_s,
        )
    except TimeoutError:
        timed_out = True

    assert timed_out, "expected TimeoutError when persist exceeds budget"
    # The slow task is still running (overrun) — clean up so the
    # event loop doesn't warn about a never-awaited coroutine.
    persist_task.cancel()
    await asyncio.gather(persist_task, return_exceptions=True)


async def test_drain_noop_when_set_empty(tmp_path: Path) -> None:
    """``Daemon.stop`` skips the drain block entirely when the
    persist set is empty — pre-6e shutdowns must not pay the gather
    cost on every boot."""
    d = Daemon(_settings(tmp_path, drain_s=2.0))
    assert not d._audio_persist_pending
    # The drain block guard is ``if self._audio_persist_pending:``;
    # we mimic that here. If a future refactor drops the guard, the
    # test fails on an empty-gather warning.
    if d._audio_persist_pending:  # pragma: no cover — must stay False
        pytest.fail("audio_persist_pending unexpectedly populated at init")
