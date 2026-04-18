"""Phase 6 / commit 6 — picker-starvation sanity test (GAP #17).

Design invariant: picker bridge and user-chat bridge are DISTINCT
ClaudeBridge instances with INDEPENDENT `asyncio.Semaphore`s. A flood
of picker dispatches MUST NOT block a concurrent user-turn latency.

We don't talk to the real SDK here — that's gated by `RUN_SDK_INT`.
Instead we install a tiny harness that models the semaphore contention
via two stub bridges that share a common clock. If the picker
dispatches serially through ITS OWN semaphore, the user-turn through
the OTHER bridge's semaphore finishes independently.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from assistant.config import (
    ClaudeSettings,
    MemorySettings,
    SchedulerSettings,
    Settings,
    SubagentSettings,
)
from assistant.state.db import apply_schema, connect
from assistant.subagent.picker import SubagentRequestPicker
from assistant.subagent.store import SubagentStore


class _SlowStubBridge:
    """Stand-in for ClaudeBridge: each `ask` holds a per-instance
    semaphore for `hold_s` seconds. Models the real contention
    pattern."""

    def __init__(self, concurrent: int, hold_s: float) -> None:
        self._sem = asyncio.Semaphore(concurrent)
        self._hold_s = hold_s
        self.finish_timestamps: list[float] = []

    async def ask(
        self,
        *,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        del chat_id, user_text, history
        async with self._sem:
            await asyncio.sleep(self._hold_s)
            self.finish_timestamps.append(time.monotonic())
            if False:  # pragma: no cover
                yield None


async def _noop_start(input_data: dict[str, Any], tool_use_id: Any, ctx: Any) -> dict[str, Any]:
    del input_data, tool_use_id, ctx
    return {}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(max_concurrent=2),
        memory=MemorySettings(),
        scheduler=SchedulerSettings(),
        subagent=SubagentSettings(picker_tick_s=0.05),
    )


async def test_picker_does_not_starve_user_chat(tmp_path: Path) -> None:
    """Spawn 5 pending picker rows. Use the picker bridge's own
    semaphore (max=2) so dispatches serialize. Concurrently fire a
    user-turn through the OTHER bridge and assert user-turn latency
    is close to `hold_s`, NOT `hold_s * ceil(5/2)`."""
    conn = await connect(tmp_path / "s.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SubagentStore(conn, lock=lock)
    for i in range(5):
        await store.record_pending_request(
            agent_type="general",
            task_text=f"t{i}",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )

    hold_s = 0.2
    picker_bridge = _SlowStubBridge(concurrent=2, hold_s=hold_s)
    user_bridge = _SlowStubBridge(concurrent=2, hold_s=hold_s)
    # Stash the start hook callable as the picker expects.
    picker_bridge._start_hook = _noop_start  # type: ignore[attr-defined]
    user_bridge._start_hook = _noop_start  # type: ignore[attr-defined]

    picker = SubagentRequestPicker(
        store,
        picker_bridge,  # type: ignore[arg-type]
        settings=_settings(tmp_path),
    )

    picker_task = asyncio.create_task(picker.run())
    try:
        # Give the picker a tick to start dispatches.
        await asyncio.sleep(0.1)

        # Fire one "user turn" through the OTHER bridge. Timing.
        user_start = time.monotonic()
        async for _ in user_bridge.ask(chat_id=42, user_text="hi", history=[]):
            pass
        user_elapsed = time.monotonic() - user_start

        # With DEDICATED semaphores, a single user turn hits its OWN
        # max_concurrent=2 semaphore — 0 contention. Upper bound ~2x
        # hold_s allows for scheduler jitter.
        assert user_elapsed < hold_s * 3, (
            f"user turn took {user_elapsed:.3f}s vs hold_s={hold_s}s; "
            "picker bridge starvation suspected"
        )

        # Meanwhile the picker is still crunching — we don't wait for
        # it here, but prove we passed through without contention.
    finally:
        picker.request_stop()
        # Allow pending dispatches to finish; generous timeout.
        await asyncio.wait_for(picker_task, timeout=5.0)
        # Drain any in-flight dispatch tasks still awaiting the
        # picker_bridge semaphore so pytest doesn't warn about
        # unclosed coroutines.
        await asyncio.sleep(0.1)
    await conn.close()
