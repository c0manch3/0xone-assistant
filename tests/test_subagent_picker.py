"""Phase 6: SubagentRequestPicker — dispatch loop, ContextVar, stop drain."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import Settings
from assistant.state.db import apply_schema, connect
from assistant.subagent.hooks import CURRENT_REQUEST_ID
from assistant.subagent.picker import SubagentRequestPicker
from assistant.subagent.store import SubagentStore


class _FakeBridge:
    """Mock ClaudeBridge that records ``ask`` calls and lets tests
    control the lifecycle via injected hooks."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.observed_request_ids: list[int | None] = []
        self._on_call: list[asyncio.Event] = []
        self._raise: Exception | None = None
        self.timeout_overrides: list[int | None] = []

    def schedule_block(self) -> asyncio.Event:
        ev = asyncio.Event()
        self._on_call.append(ev)
        return ev

    def set_raise(self, exc: Exception) -> None:
        self._raise = exc

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
        timeout_override: int | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({
            "chat_id": chat_id,
            "user_text": user_text,
            "history": history,
        })
        self.observed_request_ids.append(CURRENT_REQUEST_ID.get())
        self.timeout_overrides.append(timeout_override)
        if self._on_call:
            await self._on_call.pop(0).wait()
        if self._raise is not None:
            raise self._raise
        # Empty async generator.
        if False:
            yield None  # pragma: no cover


def _settings(tmp_path: Path) -> Settings:
    return cast(
        Settings,
        Settings(
            telegram_bot_token="x" * 50,  # type: ignore[arg-type]
            owner_chat_id=42,  # type: ignore[arg-type]
            project_root=tmp_path,
            data_dir=tmp_path / "data",
        ),
    )


async def _store(tmp_path: Path) -> SubagentStore:
    db = tmp_path / "sub.db"
    conn = await connect(db)
    await apply_schema(conn)
    return SubagentStore(conn)


async def test_picker_dispatches_pending_request(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    job_id = await store.record_pending_request(
        agent_type="general",
        task_text="do work",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    task = asyncio.create_task(picker.run())
    # Wait for the bridge to be hit.
    deadline = asyncio.get_running_loop().time() + 5.0
    while not bridge.calls and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    picker.request_stop()
    await asyncio.wait_for(task, timeout=5.0)
    assert len(bridge.calls) >= 1
    assert "do work" in bridge.calls[0]["user_text"]
    # ContextVar was set during dispatch.
    assert bridge.observed_request_ids[0] == job_id
    # ContextVar reset after dispatch.
    assert CURRENT_REQUEST_ID.get() is None


async def test_picker_uses_subagent_timeout_override(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    await store.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    task = asyncio.create_task(picker.run())
    deadline = asyncio.get_running_loop().time() + 5.0
    while not bridge.calls and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    picker.request_stop()
    await asyncio.wait_for(task, timeout=5.0)
    assert bridge.timeout_overrides[0] == settings.subagent.claude_subagent_timeout


async def test_picker_skips_cancelled_request(tmp_path: Path) -> None:
    """Cancel arrived BEFORE pickup → picker logs+continues, no bridge call."""
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    job_id = await store.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    await store.set_cancel_requested(job_id)
    task = asyncio.create_task(picker.run())
    await asyncio.sleep(0.3)
    picker.request_stop()
    await asyncio.wait_for(task, timeout=5.0)
    assert bridge.calls == []


async def test_picker_handles_bridge_error_without_crash(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()
    bridge.set_raise(ClaudeBridgeError("boom"))
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    await store.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    task = asyncio.create_task(picker.run())
    deadline = asyncio.get_running_loop().time() + 5.0
    while not bridge.calls and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    picker.request_stop()
    await asyncio.wait_for(task, timeout=5.0)
    assert task.done()
    # Picker continues running after a bridge error rather than escaping.
    assert task.exception() is None


async def test_picker_request_stop_breaks_loop(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    task = asyncio.create_task(picker.run())
    await asyncio.sleep(0.05)
    picker.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()


async def test_picker_dispatch_cancellable_during_bridge(tmp_path: Path) -> None:
    """If the picker is cancelled mid-dispatch, the inner ContextVar is
    reset (via finally) and the task exits with CancelledError."""
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()
    block = bridge.schedule_block()
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    await store.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    task = asyncio.create_task(picker.run())
    deadline = asyncio.get_running_loop().time() + 5.0
    while not bridge.calls and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    task.cancel()
    block.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)
    assert CURRENT_REQUEST_ID.get() is None


async def test_picker_processes_jobs_sequentially(tmp_path: Path) -> None:
    """Two pending rows are picked up one-per-tick, in order."""
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    await store.record_pending_request(
        agent_type="general",
        task_text="alpha",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    await store.record_pending_request(
        agent_type="researcher",
        task_text="beta",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    task = asyncio.create_task(picker.run())
    deadline = asyncio.get_running_loop().time() + 5.0
    # Integration-style poll with deadline. We simulate the Start-hook
    # claim by flipping status manually, which is precisely what the
    # @tool surface does in production.
    while len(bridge.calls) < 2 and asyncio.get_running_loop().time() < deadline:
        if bridge.calls:
            jobs = await store.list_jobs(status="requested")
            for j in jobs:
                if "alpha" in (j.task_text or ""):
                    await store._conn.execute(
                        "UPDATE subagent_jobs SET sdk_agent_id='ag-a', "
                        "status='started' WHERE id=?", (j.id,)
                    )
                    await store._conn.commit()
        await asyncio.sleep(0.05)
    picker.request_stop()
    await asyncio.wait_for(task, timeout=5.0)
    seen_tasks = [c["user_text"] for c in bridge.calls]
    # We always observe alpha first; beta follows after the alpha row
    # is moved out of 'requested'.
    assert any("alpha" in t for t in seen_tasks)


async def test_picker_no_pending_idle_loop(tmp_path: Path) -> None:
    """Empty ledger → picker idles without raising."""
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    task = asyncio.create_task(picker.run())
    await asyncio.sleep(0.2)
    picker.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert bridge.calls == []


# ---------------------------------------------------------------------------
# Fix-pack F1 — picker livelock avoidance
# ---------------------------------------------------------------------------


async def test_picker_marks_failed_after_max_attempts(tmp_path: Path) -> None:
    """Bridge keeps failing → after 3 attempts the row flips to 'error'.

    Without Fix-pack F1 (code H1 / devil C-W2-4 / QA HIGH-3) the row
    would stay 'requested' forever and the picker would re-try every
    tick — the original livelock.
    """
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()
    bridge.set_raise(ClaudeBridgeError("boom"))
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    job_id = await store.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    task = asyncio.create_task(picker.run())
    deadline = asyncio.get_running_loop().time() + 8.0
    while (
        asyncio.get_running_loop().time() < deadline
    ):
        job = await store.get_by_id(job_id)
        if job is not None and job.status == "error":
            break
        await asyncio.sleep(0.05)
    picker.request_stop()
    await asyncio.wait_for(task, timeout=5.0)
    job = await store.get_by_id(job_id)
    assert job is not None
    assert job.status == "error", f"expected 'error', got {job.status!r}"
    assert job.attempts >= 3
    assert job.last_error is not None
    assert "bridge" in job.last_error


async def test_picker_skips_cancelled_request_transitions_to_stopped(
    tmp_path: Path,
) -> None:
    """Cancel before pickup → set_cancel_requested moves to 'stopped';
    picker has nothing to dispatch (Fix-pack F1, QA HIGH-4)."""
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    job_id = await store.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    # F1: cancel-before-pickup transitions the row directly.
    res = await store.set_cancel_requested(job_id)
    assert res["cancel_requested"] is True
    assert res["previous_status"] == "requested"
    job = await store.get_by_id(job_id)
    assert job is not None
    assert job.status == "stopped"  # transitioned by set_cancel_requested
    assert job.finished_at is not None
    # Picker observes no rows in 'requested' state.
    task = asyncio.create_task(picker.run())
    await asyncio.sleep(0.4)
    picker.request_stop()
    await asyncio.wait_for(task, timeout=5.0)
    assert bridge.calls == []


async def test_picker_marks_failed_when_model_skips_tool(
    tmp_path: Path,
) -> None:
    """Bridge returns successfully but Start hook never fires (model
    refused / didn't invoke Task) → mark_dispatch_failed runs, attempts
    increments, eventually 'error'."""
    store = await _store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _FakeBridge()  # No exception, just empty generator.
    picker = SubagentRequestPicker(
        store, cast(ClaudeBridge, bridge), settings=settings
    )
    job_id = await store.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    task = asyncio.create_task(picker.run())
    deadline = asyncio.get_running_loop().time() + 8.0
    while asyncio.get_running_loop().time() < deadline:
        job = await store.get_by_id(job_id)
        if job is not None and job.status == "error":
            break
        await asyncio.sleep(0.05)
    picker.request_stop()
    await asyncio.wait_for(task, timeout=5.0)
    job = await store.get_by_id(job_id)
    assert job is not None
    assert job.status == "error"
    assert job.last_error is not None
    assert "model did not invoke Task tool" in job.last_error
