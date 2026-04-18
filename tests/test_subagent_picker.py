"""Phase 6 / commit 6 — SubagentRequestPicker behaviour.

Uses a stub ClaudeBridge that drains `pending_updates` and fires the
Start hook from within `ask()` to simulate SDK semantics. Covers:
  * Picker claims a pending row, sets ContextVar, dispatches via bridge.
  * Start hook reads ContextVar and patches the row's sdk_agent_id.
  * Picker skips rows whose `cancel_requested=1` (no dispatch).
  * `_inflight` stays empty after drain (no leak).
  * `request_stop` + idle queue causes `run()` to return quickly.
"""

from __future__ import annotations

import asyncio
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
from assistant.subagent.context import CURRENT_REQUEST_ID
from assistant.subagent.picker import SubagentRequestPicker
from assistant.subagent.store import SubagentStore


class _StubBridge:
    """Minimal stand-in for `ClaudeBridge` that records invocations and
    fires a fake `on_subagent_start` hook with the ContextVar captured
    mid-flight. `ask` is an async generator so the picker can iterate
    it directly via `async for` — matches real ClaudeBridge semantics."""

    def __init__(self, start_hook: Any) -> None:
        self._start_hook = start_hook
        self.calls: list[tuple[int, str]] = []
        self._agent_counter = 0

    async def ask(
        self,
        *,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        del history
        self.calls.append((chat_id, user_text))
        self._agent_counter += 1
        agent_id = f"stub-agent-{self._agent_counter}"
        # Fire the Start hook INSIDE ask() — preserves the ContextVar
        # just like the real SDK would.
        await self._start_hook(
            {
                "agent_id": agent_id,
                "agent_type": "general",
                "session_id": "stub-parent-session",
            },
            None,
            None,
        )
        # Empty async generator body — the caller drains via
        # `async for ... pass`. `yield` inside an `if False:` makes the
        # function an async generator so it is iterable without the
        # caller awaiting `ask` first.
        if False:  # pragma: no cover
            yield None


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        memory=MemorySettings(),
        scheduler=SchedulerSettings(),
        subagent=SubagentSettings(picker_tick_s=0.05),
    )


async def _mkstore(tmp_path: Path) -> SubagentStore:
    conn = await connect(tmp_path / "p.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    return SubagentStore(conn, lock=lock)


def _make_start_hook(store: SubagentStore) -> Any:
    """Stand-alone Start hook that reads the ContextVar and patches the
    pending row — mirrors `on_subagent_start` without pulling in the
    whole hook factory (keeps this test hermetic)."""

    async def on_start(input_data: dict[str, Any], tool_use_id: Any, ctx: Any) -> dict[str, Any]:
        del tool_use_id, ctx
        request_id = CURRENT_REQUEST_ID.get()
        if request_id is not None:
            await store.update_sdk_agent_id_for_claimed_request(
                job_id=request_id,
                sdk_agent_id=str(input_data["agent_id"]),
                parent_session_id=input_data.get("session_id"),
            )
        return {}

    return on_start


async def test_picker_dispatches_pending_and_patches_row(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    jid = await store.record_pending_request(
        agent_type="general",
        task_text="write hello",
        callback_chat_id=42,
        spawned_by_kind="cli",
    )
    bridge = _StubBridge(_make_start_hook(store))
    picker = SubagentRequestPicker(store, bridge, settings=_settings(tmp_path))

    run_task = asyncio.create_task(picker.run())
    try:
        # Poll for up to 2 s for the bridge to receive a call.
        for _ in range(40):
            if bridge.calls:
                break
            await asyncio.sleep(0.05)
        assert bridge.calls, "picker never invoked the bridge"
        chat_id, prompt = bridge.calls[0]
        assert chat_id == 42
        assert "write hello" in prompt
        assert "general" in prompt

        # Give the in-flight task a moment to run the Start hook and
        # settle (the _dispatch_one finally-block needs to run).
        for _ in range(40):
            job = await store.get_by_id(jid)
            assert job is not None
            if job.status == "started":
                break
            await asyncio.sleep(0.05)
        assert job.status == "started"
        assert job.sdk_agent_id == "stub-agent-1"
    finally:
        picker.request_stop()
        await asyncio.wait_for(run_task, timeout=2.0)
    assert picker.inflight() == set()
    await store._conn.close()


async def test_picker_drops_cancelled_rows_on_first_tick(tmp_path: Path) -> None:
    """Fix-pack HIGH #1 (CR I-3 / devil H-3): cancelled `requested`
    rows are transitioned straight to `dropped` on the first tick
    instead of log-spamming every tick for up to 1 h until
    recover_orphans sweeps them."""
    store = await _mkstore(tmp_path)
    jid = await store.record_pending_request(
        agent_type="general",
        task_text="skip me",
        callback_chat_id=42,
        spawned_by_kind="cli",
    )
    # Cancel BEFORE the picker runs.
    await store.set_cancel_requested(jid)

    bridge = _StubBridge(_make_start_hook(store))
    picker = SubagentRequestPicker(store, bridge, settings=_settings(tmp_path))

    run_task = asyncio.create_task(picker.run())
    # Poll for the transition; should happen on the first tick.
    for _ in range(40):
        job = await store.get_by_id(jid)
        assert job is not None
        if job.status == "dropped":
            break
        await asyncio.sleep(0.05)
    picker.request_stop()
    await asyncio.wait_for(run_task, timeout=2.0)

    # Bridge NEVER called (no dispatch for a cancelled row).
    assert bridge.calls == []
    # Row transitioned to `dropped` + carries finished_at.
    job = await store.get_by_id(jid)
    assert job is not None
    assert job.status == "dropped"
    assert job.finished_at is not None
    assert job.cancel_requested is True
    await store._conn.close()


async def test_picker_stop_returns_on_empty_queue(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    bridge = _StubBridge(_make_start_hook(store))
    picker = SubagentRequestPicker(store, bridge, settings=_settings(tmp_path))

    run_task = asyncio.create_task(picker.run())
    # Let it tick once.
    await asyncio.sleep(0.1)
    picker.request_stop()
    await asyncio.wait_for(run_task, timeout=2.0)
    assert bridge.calls == []
    assert picker.inflight() == set()
    await store._conn.close()


async def test_picker_does_not_double_dispatch_same_row(tmp_path: Path) -> None:
    """The picker must not re-enqueue a row whose _dispatch_one is
    still in-flight. We simulate a slow bridge by making `ask` await
    for an event the test controls."""
    store = await _mkstore(tmp_path)
    await store.record_pending_request(
        agent_type="general",
        task_text="slow",
        callback_chat_id=42,
        spawned_by_kind="cli",
    )

    slow_event = asyncio.Event()

    class _SlowBridge(_StubBridge):
        async def ask(
            self,
            *,
            chat_id: int,
            user_text: str,
            history: list[dict[str, Any]],
        ) -> AsyncIterator[Any]:
            del history
            self.calls.append((chat_id, user_text))
            self._agent_counter += 1
            agent_id = f"slow-agent-{self._agent_counter}"
            await self._start_hook(
                {
                    "agent_id": agent_id,
                    "agent_type": "general",
                    "session_id": "sess",
                },
                None,
                None,
            )
            await slow_event.wait()
            if False:  # pragma: no cover
                yield None

    bridge = _SlowBridge(_make_start_hook(store))
    picker = SubagentRequestPicker(store, bridge, settings=_settings(tmp_path))

    run_task = asyncio.create_task(picker.run())
    try:
        # Wait until the first dispatch begins.
        for _ in range(40):
            if bridge.calls:
                break
            await asyncio.sleep(0.05)
        assert len(bridge.calls) == 1

        # Give the picker multiple extra ticks; the row is now `started`
        # (ContextVar patched it) so list_pending_requests won't return
        # it anymore. No second call should happen.
        await asyncio.sleep(0.4)
        assert len(bridge.calls) == 1
    finally:
        slow_event.set()
        picker.request_stop()
        await asyncio.wait_for(run_task, timeout=3.0)
    await store._conn.close()
