"""Phase 6 fix-pack CRITICAL #2 (devil C-3 / CR I-1).

The picker's `_dispatch_one` coroutines are spawned via
`asyncio.create_task` from inside `picker.run()` and kept alive only
via the strong ref in `self._dispatch_tasks`. They outlive the
`picker.run()` coroutine itself. A bare `Daemon._bg_tasks` drain
closes only `picker.run()`; any `_dispatch_one` still in flight when
the subsequent `adapter.stop()` + `conn.close()` run is free to
continue executing against a closed DB and a stopped adapter —
exactly the phase-5 Daemon-stop race we just fixed for the scheduler
dispatcher.

Post-fix, `Daemon.stop()` between its scheduler-shield drain (2.5)
and its subagent-notify drain (2.6) runs a new step 2.57 that gathers
`self._subagent_picker.dispatch_tasks()` with a 5 s budget BEFORE
the DB/adapter shutdown.

The test seeds a stub picker bridge whose `ask()` suspends on an
event; the picker claims a pending row, begins a dispatch, and the
test calls `Daemon.stop()`. We assert (a) the dispatch was awaited
to completion (no race) and (b) no exception propagates out of
`stop()`.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import assistant.main as main_mod
from assistant.config import ClaudeSettings, Settings, SubagentSettings
from assistant.main import Daemon


class _DummyAdapter:
    def __init__(self, settings: Any, *, dedup_ledger: Any = None) -> None:
        # Phase 7 fix-pack C1: daemon threads the shared ledger.
        del settings, dedup_ledger
        self._handler: Any = None
        self.sent: list[tuple[int, str]] = []
        self.stopped: bool = False

    def set_handler(self, handler: Any) -> None:
        self._handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


async def _noop_preflight(log: Any) -> None:
    del log


class _SlowPickerBridge:
    """Stand-in for ClaudeBridge that suspends inside `ask()` until a
    test-controlled event is set. Each call records (chat_id, prompt)
    in `self.calls` and increments a local counter the test can
    inspect to verify the dispatch was actually awaited."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.release_event = asyncio.Event()
        self.entered = asyncio.Event()
        self.completed_count = 0

    async def ask(
        self,
        *,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        del history
        self.calls.append((chat_id, user_text))
        self.entered.set()
        # Suspend until the test says "release" — simulates a real
        # SDK turn that takes a while to finish.
        await self.release_event.wait()
        self.completed_count += 1
        if False:  # pragma: no cover
            yield None


@pytest.mark.asyncio
async def test_daemon_stop_drains_picker_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed a `requested` row, start the Daemon with a picker bridge
    whose `ask()` hangs until the test releases it, then call `stop()`.
    The dispatch must be awaited to completion (not cancelled mid-
    flight) so the Stop hook's DB UPDATE and shielded send_text land
    before `conn.close()` and `adapter.stop()`."""
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _DummyAdapter)
    # Skip bootstrap; it would block start() on the real installer.
    monkeypatch.setattr(Daemon, "_bootstrap_skill_creator_bg", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    # Build a Settings with a tight picker tick so the picker claims
    # the row within a hundred ms.
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        subagent=SubagentSettings(enabled=True, picker_tick_s=0.05),
    )
    daemon = Daemon(settings)

    # Start the daemon. At this point schema is applied and we can
    # seed a `requested` row directly against the ledger (before the
    # picker ticks — we'll let it pick it up after we swap the bridge).
    await daemon.start()

    # Swap in the test-controlled stub bridge so the picker's next
    # dispatch hits our hanging `ask()`. This is a deliberate
    # monkeypatch — the real `ClaudeBridge` would try to spawn a
    # claude CLI subprocess which is absent in the test env.
    assert daemon._subagent_picker is not None
    stub_bridge = _SlowPickerBridge()
    daemon._subagent_picker._bridge = stub_bridge  # type: ignore[assignment]

    # Seed a pending request — the picker's next tick will claim it.
    assert daemon._sub_store is not None
    await daemon._sub_store.record_pending_request(
        agent_type="general",
        task_text="slow job",
        callback_chat_id=42,
        spawned_by_kind="cli",
    )

    # Wait for the picker to enter the stub bridge.
    try:
        await asyncio.wait_for(stub_bridge.entered.wait(), timeout=2.0)
    except TimeoutError as exc:
        await daemon.stop()
        raise AssertionError("picker never dispatched the pending row") from exc

    # Confirm the dispatch is currently suspended (not yet completed).
    assert stub_bridge.completed_count == 0
    dispatches = daemon._subagent_picker.dispatch_tasks()
    assert len(dispatches) == 1
    task = next(iter(dispatches))
    assert not task.done()

    # Kick off Daemon.stop() as a task, then release the bridge so the
    # drain in step 2.57 sees a completing task. If the fix is absent,
    # `stop()` would close conn before the dispatch's finally clause
    # runs and we'd see a ProgrammingError in the logs (still surfacing
    # as a green test because stop() swallows exceptions; we assert on
    # `completed_count` instead, which is the decisive signal).
    stop_task = asyncio.create_task(daemon.stop())
    # Give stop() a moment to reach step 2.57; release a hair later so
    # the drain sees a real in-flight task.
    await asyncio.sleep(0.05)
    stub_bridge.release_event.set()

    await asyncio.wait_for(stop_task, timeout=10.0)

    # The dispatch's body ran to completion — not cancelled mid-flight.
    assert stub_bridge.completed_count == 1
    # And nothing still in flight (successful drain).
    assert all(t.done() for t in dispatches)


@pytest.mark.asyncio
async def test_daemon_stop_picker_drain_timeout_cancels_stuck_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second scenario: the dispatch hangs past the 5 s drain budget.
    We monkeypatch the drain timeout to a short value so the test
    runs quickly. Stop() must not hang forever AND the straggler
    must be cancelled (no zombie dispatch polluting the next
    daemon start)."""
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _DummyAdapter)
    monkeypatch.setattr(Daemon, "_bootstrap_skill_creator_bg", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")
    # Short drain timeout so we don't wait 5 s per test run.
    monkeypatch.setattr(main_mod, "_STOP_DRAIN_TIMEOUT_S", 0.3)

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        subagent=SubagentSettings(enabled=True, picker_tick_s=0.05),
    )
    daemon = Daemon(settings)
    await daemon.start()

    assert daemon._subagent_picker is not None
    stub_bridge = _SlowPickerBridge()
    daemon._subagent_picker._bridge = stub_bridge  # type: ignore[assignment]

    assert daemon._sub_store is not None
    await daemon._sub_store.record_pending_request(
        agent_type="general",
        task_text="stuck job",
        callback_chat_id=42,
        spawned_by_kind="cli",
    )
    try:
        await asyncio.wait_for(stub_bridge.entered.wait(), timeout=2.0)
    except TimeoutError as exc:
        await daemon.stop()
        raise AssertionError("picker never dispatched the pending row") from exc

    # Do NOT release the bridge — the drain will time out and cancel.
    dispatches = daemon._subagent_picker.dispatch_tasks()
    assert len(dispatches) == 1
    task = next(iter(dispatches))

    await asyncio.wait_for(daemon.stop(), timeout=10.0)

    # Drain timed out → straggler cancelled, never completed.
    assert task.cancelled() or task.done()
    assert stub_bridge.completed_count == 0
