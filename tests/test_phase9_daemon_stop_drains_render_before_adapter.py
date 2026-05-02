"""Phase 9 fix-pack F2 (W3-CRIT-2) — ``Daemon.stop`` drains the
``_render_doc_pending`` set BEFORE closing the adapter session.

The pre-fix-pack order was ``adapter.stop → vault drain →
render_doc drain``. Closing aiogram first cascades CancelledError to
in-flight handler tasks; any ``send_document`` already in flight
raises ``ClientSessionAlreadyClosed`` before the render drain even
gets a chance to run. The fix moves the render drain ABOVE
``adapter.stop`` so the artefact delivery path completes before the
underlying transport tears down.

This test exercises the structural ordering invariant by patching
``Daemon`` instances with the minimum state to call ``Daemon.stop``
without booting the full daemon, then asserts the recorded sequence
of events.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from assistant.config import Settings
from assistant.main import Daemon


class _OrderRecorder:
    """Captures the order of stop-time events for a single test."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, label: str) -> None:
        self.events.append(label)


class _FakeAdapter:
    """Minimal adapter: ``stop`` records the call order."""

    def __init__(self, recorder: _OrderRecorder) -> None:
        self._rec = recorder

    async def start(self) -> None: ...

    async def stop(self) -> None:
        self._rec.record("adapter.stop")

    async def send_text(self, chat_id: int, text: str) -> None: ...


class _FakeSubsystem:
    """Mock render_doc subsystem; tracks shutdown ledger flip."""

    def __init__(self, recorder: _OrderRecorder) -> None:
        self._rec = recorder
        self.force_disabled = False

    async def mark_orphans_delivered_at_shutdown(self) -> None:
        self._rec.record("mark_orphans")

    def get_inflight_count(self) -> int:  # observability hook
        return 0


@pytest.fixture
def stub_daemon() -> AsyncIterator[Daemon]:
    """Yield a minimally-bootstrapped Daemon — only the fields
    ``Daemon.stop`` reads are populated."""
    recorder = _OrderRecorder()
    settings = Settings(
        telegram_bot_token="x" * 16, owner_chat_id=1
    )
    d = Daemon.__new__(Daemon)
    d._settings = settings
    d._adapter = _FakeAdapter(recorder)  # type: ignore[assignment]
    d._sched_loop = None
    d._sched_dispatcher = None
    d._sub_picker = None
    d._render_doc_pending = set()
    d._render_doc = _FakeSubsystem(recorder)  # type: ignore[assignment]
    d._vault_sync_pending = set()
    d._audio_persist_pending = set()
    d._sub_pending_updates = set()
    d._bg_tasks = set()
    d._conn = None  # type: ignore[assignment]
    d._lock_fd = None
    d._recorder = recorder  # type: ignore[attr-defined]
    yield d


@pytest.mark.asyncio
async def test_render_doc_drain_runs_before_adapter_stop(
    stub_daemon: Daemon,
) -> None:
    """A render task that records its cancellation moment must finish
    BEFORE ``adapter.stop`` lands."""
    recorder = stub_daemon._recorder  # type: ignore[attr-defined]

    parked = asyncio.Event()

    async def parked_render() -> None:
        try:
            parked.set()
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            recorder.record("render_task.cancelled")
            raise

    task = asyncio.create_task(parked_render(), name="render-task")
    stub_daemon._render_doc_pending.add(task)
    await parked.wait()

    # Tighten the drain budget so the test finishes within seconds —
    # the parked task will be cancelled inside the drain block.
    stub_daemon._settings.render_doc.render_drain_timeout_s = 0.05

    await stub_daemon.stop()

    # F2 invariant: render task termination MUST land BEFORE the
    # adapter close. The orphan-marker call also lands before
    # ``adapter.stop`` since it's part of the same render-cleanup
    # block; that's fine — the failure mode we guard against is
    # adapter close cascading CancelledError into in-flight handlers.
    assert "render_task.cancelled" in recorder.events
    assert "adapter.stop" in recorder.events
    render_idx = recorder.events.index("render_task.cancelled")
    adapter_idx = recorder.events.index("adapter.stop")
    assert render_idx < adapter_idx, (
        f"render task must terminate before adapter.stop; "
        f"got events={recorder.events}"
    )


@pytest.mark.asyncio
async def test_render_doc_drain_completes_within_budget(
    stub_daemon: Daemon,
) -> None:
    """Empty pending set + healthy adapter still terminates cleanly."""
    await stub_daemon.stop()
    recorder = stub_daemon._recorder  # type: ignore[attr-defined]
    assert "adapter.stop" in recorder.events
    assert "mark_orphans" in recorder.events
