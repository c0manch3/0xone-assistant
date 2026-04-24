"""Fix 4 / devil C2: ``_spawn_bg_supervised``'s ``CancelledError`` branch
must cancel (and briefly await) the inner factory task before
re-raising. Without this, the child outlives the supervisor and when
the daemon closes the sqlite connection the next DB call raises
``sqlite3.ProgrammingError``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.main import Daemon


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
        scheduler=SchedulerSettings(),
    )


async def test_supervisor_cancels_inner_task_when_cancelled(
    tmp_path: Path,
) -> None:
    """Directly drive ``_spawn_bg_supervised``; don't boot the daemon."""
    d = Daemon(_settings(tmp_path))
    inner_started = asyncio.Event()
    inner_cancelled = asyncio.Event()

    async def _factory() -> None:
        inner_started.set()
        try:
            await asyncio.sleep(60)  # would hang past shutdown
        except asyncio.CancelledError:
            inner_cancelled.set()
            raise

    d._spawn_bg_supervised(_factory, name="test_child")
    await asyncio.wait_for(inner_started.wait(), timeout=1.0)

    assert len(d._bg_tasks) == 1
    supervisor = next(iter(d._bg_tasks))

    supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await supervisor

    # Give the event loop a tick to drain cancellation.
    await asyncio.sleep(0)
    assert inner_cancelled.is_set(), (
        "inner factory task must be cancelled by the supervisor "
        "before it re-raises — otherwise the child outlives shutdown "
        "and hits a closed DB connection."
    )
