"""Phase 7 commit 16 — `Daemon` media integration.

Covers the wire-up that the Wave-8 dispatch_reply refactor left
dangling on main:

  * `ensure_media_dirs()` runs BEFORE any background task is spawned
    (implementation.md §0 pitfall #14 — sweeper would otherwise log
    spurious `FileNotFoundError` on its first tick);
  * `media_sweeper_loop` is registered on `_bg_tasks` so
    `Daemon.stop()` drains it;
  * the Daemon's `_dedup_ledger` singleton is threaded into BOTH
    `SchedulerDispatcher.__init__(..., dedup_ledger=...)` AND
    `make_subagent_hooks(..., dedup_ledger=...)` — invariant I-7.5
    requires the three artefact call-sites to share ONE ledger;
  * `Daemon.stop()` sets `_media_sweep_stop` and the sweeper exits
    cleanly (no cancellation, no leaked task).
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

import assistant.main as main_mod
from assistant.config import ClaudeSettings, Settings, SubagentSettings
from assistant.main import Daemon


class _DummyAdapter:
    def __init__(self, settings: Any, *, dedup_ledger: Any = None) -> None:
        # Phase 7 fix-pack C1: the daemon now threads the shared
        # `_DedupLedger` through `TelegramAdapter.__init__`. The dummy
        # accepts it as a kwarg and records it so the integration test
        # can assert the same ledger reached all three call-sites.
        del settings
        self._handler: Any = None
        self._dedup_ledger: Any = dedup_ledger

    def set_handler(self, handler: Any) -> None:
        self._handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        del chat_id, text


async def _noop_preflight(log: Any) -> None:
    del log


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        subagent=SubagentSettings(enabled=False),
    )


@pytest.mark.asyncio
async def test_daemon_start_creates_media_dirs_and_spawns_sweeper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full lifecycle: init → start → stop.

    Asserts the four integration invariants described in the module
    docstring: media dirs exist, sweeper task is in `_bg_tasks`, the
    SAME `_dedup_ledger` reached both factory call-sites, and
    `stop()` drains the sweeper cleanly (no un-awaited task).
    """
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _DummyAdapter)
    monkeypatch.setattr(
        Daemon, "_bootstrap_skill_creator_bg", lambda self: asyncio.sleep(0)
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    captured: dict[str, Any] = {}

    real_dispatcher = main_mod.SchedulerDispatcher

    def _recording_dispatcher(**kwargs: Any) -> Any:
        captured["dispatcher_dedup"] = kwargs["dedup_ledger"]
        return real_dispatcher(**kwargs)

    real_hooks = main_mod.make_subagent_hooks

    def _recording_hooks(**kwargs: Any) -> Any:
        captured["hooks_dedup"] = kwargs["dedup_ledger"]
        return real_hooks(**kwargs)

    monkeypatch.setattr(main_mod, "SchedulerDispatcher", _recording_dispatcher)
    monkeypatch.setattr(main_mod, "make_subagent_hooks", _recording_hooks)

    daemon = Daemon(_make_settings(tmp_path))

    try:
        await daemon.start()

        # (1) Media dirs created BEFORE anything else touched data_dir/media.
        assert (tmp_path / "data" / "media" / "inbox").is_dir()
        assert (tmp_path / "data" / "media" / "outbox").is_dir()
        assert (tmp_path / "data" / "run" / "render-stage").is_dir()

        # (2) Sweeper bg task is tracked.
        sweeper_tasks = [t for t in daemon._bg_tasks if t.get_name() == "media_sweeper_loop"]
        assert len(sweeper_tasks) == 1, sweeper_tasks
        assert not sweeper_tasks[0].done()

        # (3) SAME dedup ledger instance reached both call-sites.
        assert captured["dispatcher_dedup"] is daemon._dedup_ledger
        assert captured["hooks_dedup"] is daemon._dedup_ledger
    finally:
        await daemon.stop()

    # (4) stop() sets the event and drains the sweeper task.
    assert daemon._media_sweep_stop.is_set()
    # All bg tasks fully drained (done-callbacks discarded them).
    assert not daemon._bg_tasks


@pytest.mark.asyncio
async def test_ensure_media_dirs_runs_before_sweeper_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pitfall #14 regression: `ensure_media_dirs()` MUST complete
    BEFORE `media_sweeper_loop` is invoked.

    We shim `media_sweeper_loop` to capture the state of the media
    layout at call time. If the call happens before `ensure_media_dirs`,
    the `inbox/outbox` check will fail and the assertion below fires.
    """
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _DummyAdapter)
    monkeypatch.setattr(
        Daemon, "_bootstrap_skill_creator_bg", lambda self: asyncio.sleep(0)
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    snapshot: dict[str, bool] = {}

    async def _stub_sweeper(
        data_dir: Path,
        settings: Any,
        stop_event: asyncio.Event,
        log: Any,
    ) -> None:
        del settings, log
        # Capture existence AT THE MOMENT the coroutine starts running.
        # The real `media_sweeper_loop` would scan these dirs on its
        # first tick; the ordering invariant is that both exist here.
        snapshot["inbox_exists"] = (data_dir / "media" / "inbox").is_dir()
        snapshot["outbox_exists"] = (data_dir / "media" / "outbox").is_dir()
        # Behave like the real loop — block until stop_event fires.
        await stop_event.wait()

    monkeypatch.setattr(main_mod, "media_sweeper_loop", _stub_sweeper)

    # Also sanity-check that `ensure_media_dirs` ran before the stub.
    # We wrap the real implementation with an ordering sentinel.
    order: list[str] = []
    real_ensure: Callable[[Path], Awaitable[None]] = main_mod.ensure_media_dirs

    async def _ordered_ensure(data_dir: Path) -> None:
        order.append("ensure_media_dirs")
        await real_ensure(data_dir)

    async def _ordered_sweeper(
        data_dir: Path,
        settings: Any,
        stop_event: asyncio.Event,
        log: Any,
    ) -> None:
        order.append("media_sweeper_loop")
        await _stub_sweeper(data_dir, settings, stop_event, log)

    monkeypatch.setattr(main_mod, "ensure_media_dirs", _ordered_ensure)
    monkeypatch.setattr(main_mod, "media_sweeper_loop", _ordered_sweeper)

    daemon = Daemon(_make_settings(tmp_path))
    try:
        await daemon.start()
        # Give the sweeper stub a chance to reach its `await` so the
        # snapshot dict is populated.
        for _ in range(50):
            if snapshot:
                break
            await asyncio.sleep(0.01)
    finally:
        await daemon.stop()

    assert snapshot.get("inbox_exists") is True, snapshot
    assert snapshot.get("outbox_exists") is True, snapshot
    assert order[0] == "ensure_media_dirs", order
    assert "media_sweeper_loop" in order
    assert order.index("ensure_media_dirs") < order.index("media_sweeper_loop")
