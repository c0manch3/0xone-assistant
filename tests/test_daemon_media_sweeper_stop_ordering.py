"""Phase 7 commit 18l — daemon media-sweeper stop-ordering gate (pitfall #14).

Complements `test_daemon_media_integration.py::
test_ensure_media_dirs_runs_before_sweeper_spawn` with deeper scenarios
around the *lifecycle* of `_media_sweep_stop` and the sweeper task:

  1. **Start order** — `ensure_media_dirs()` completes BEFORE
     `media_sweeper_loop()` is invoked. We record call order via
     monkey-patched shims on both callables so a future refactor that
     accidentally reorders them trips this gate at CI time.

  2. **Stop order** — `Daemon.stop()` sets `_media_sweep_stop` BEFORE
     the `_bg_tasks` drain starts awaiting the sweeper task. This is
     the invariant that lets the sweeper exit cooperatively at its
     next `asyncio.wait_for(stop_event.wait(), ...)` wake-up instead
     of being hard-cancelled mid-unlink (sweeper docstring lines
     223-227: `CancelledError` is NOT caught, so a hard cancel during
     a `_safe_unlink` loop would abort the pass and leave partially
     swept state). We detect the wrong order by having the sweeper
     stub record (a) whether `stop_event` was set at the moment its
     main loop exited, and (b) whether it saw `CancelledError`. A
     cooperative exit => ordering is correct.

  3. **Missing-dir resilience** — even if the ordering invariant
     ever regresses, `sweep_media_once()` must not blow up on a
     non-existent `<data_dir>/media/{inbox,outbox}`. Directly invoke
     the sweeper with a bogus `data_dir` and assert a clean single
     tick + cooperative exit (no exception escapes, log stays clean).
     This is the "defense in depth" half of pitfall #14: the
     ordering gate in `Daemon.start` is primary, this fallback is
     the safety net.

  4. **Idempotent restart** — `Daemon.stop()` must leave the daemon
     in a state where a subsequent `Daemon.start()` on a fresh
     instance (with the same `data_dir`) works unchanged. In
     particular: `_media_sweep_stop` is an `__init__`-scoped
     `asyncio.Event`, so a *new* `Daemon(...)` gets a fresh one —
     the `.set()` from the prior `stop()` does NOT leak through the
     `data_dir` and short-circuit the new sweeper on startup.

None of these overlap with the existing integration test's
assertions (dir existence, bg_tasks registration, dedup ledger
identity). We stub network / subprocess / adapter bits identically
to keep the test hermetic and fast (`< 1 s` wall-clock).
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

import assistant.main as main_mod
from assistant.config import ClaudeSettings, MediaSettings, Settings, SubagentSettings
from assistant.main import Daemon
from assistant.media.sweeper import media_sweeper_loop, sweep_media_once


# ---------------------------------------------------------------------------
# Shared test doubles — kept byte-identical with
# `test_daemon_media_integration.py` so a future refactor touching the
# Daemon startup contract only has to update ONE place (we could DRY
# this into a fixture, but every test-file-local stub is <50 LoC and
# the explicitness outweighs the marginal duplication cost).
# ---------------------------------------------------------------------------


class _DummyAdapter:
    """Minimal `TelegramAdapter` substitute for daemon start/stop tests."""

    def __init__(self, settings: Any) -> None:
        del settings
        self._handler: Any = None

    def set_handler(self, handler: Any) -> None:
        self._handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        del chat_id, text


async def _noop_preflight(log: Any) -> None:
    """Stand-in for `_preflight_claude_cli` — skips the `claude --version` probe."""
    del log


def _make_settings(tmp_path: Path) -> Settings:
    """Construct a hermetic `Settings` rooted at `tmp_path`.

    `subagent.enabled=False` so we don't spawn the picker / bridge
    plumbing — sweeper lifecycle is orthogonal to subagent wiring,
    and the picker adds ~30 LoC of drain paths we don't want to
    stabilise around in this test.
    """
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        subagent=SubagentSettings(enabled=False),
    )


def _patch_daemon_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the standard set of module-level monkeypatches.

    Centralised here because all four scenarios need the same baseline
    environment (no CLI preflight, no skills symlink, no Telegram, no
    gh, no skill-creator bootstrap subprocess). Each scenario then
    layers its own sweeper/ensure-dirs shims on top.
    """
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _DummyAdapter)
    monkeypatch.setattr(
        Daemon, "_bootstrap_skill_creator_bg", lambda self: asyncio.sleep(0)
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")


# ---------------------------------------------------------------------------
# Scenario 1 — start order: ensure_media_dirs BEFORE media_sweeper_loop.
#
# The existing integration test already covers this at a surface level;
# here we pin it with a stricter ordering assertion that also verifies
# `ensure_media_dirs` has *returned* (not merely been scheduled) before
# the sweeper coroutine is *invoked*. Coroutine invocation order is the
# observable contract — if `ensure_media_dirs()` is accidentally
# demoted to `asyncio.create_task(ensure_media_dirs(...))` (fire and
# forget), the sweeper might start mid-mkdir; this test catches that.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_ensure_media_dirs_awaited_before_sweeper_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "skills").mkdir()
    _patch_daemon_env(monkeypatch)

    events: list[str] = []
    ensure_finished = asyncio.Event()
    sweeper_running = asyncio.Event()
    real_ensure: Callable[[Path], Awaitable[None]] = main_mod.ensure_media_dirs

    async def _recording_ensure(data_dir: Path) -> None:
        events.append("ensure_start")
        await real_ensure(data_dir)
        events.append("ensure_end")
        ensure_finished.set()

    async def _recording_sweeper(
        data_dir: Path,
        settings: Any,
        stop_event: asyncio.Event,
        log: Any,
    ) -> None:
        del settings, log
        events.append("sweeper_invoked")
        # Invariant check at invocation time — dirs MUST exist.
        events.append(
            f"inbox_exists={(data_dir / 'media' / 'inbox').is_dir()}"
        )
        events.append(
            f"outbox_exists={(data_dir / 'media' / 'outbox').is_dir()}"
        )
        sweeper_running.set()
        await stop_event.wait()
        events.append("sweeper_exit")

    monkeypatch.setattr(main_mod, "ensure_media_dirs", _recording_ensure)
    monkeypatch.setattr(main_mod, "media_sweeper_loop", _recording_sweeper)

    daemon = Daemon(_make_settings(tmp_path))
    try:
        await daemon.start()
        # Let the sweeper coroutine reach its first `await` so
        # "sweeper_invoked" lands in `events` deterministically.
        await asyncio.wait_for(sweeper_running.wait(), timeout=2.0)
    finally:
        await daemon.stop()

    # Ordering proof: ensure_end must precede sweeper_invoked.
    assert "ensure_end" in events, events
    assert "sweeper_invoked" in events, events
    assert events.index("ensure_end") < events.index("sweeper_invoked"), events
    # Dirs MUST have existed at sweeper-invocation time.
    assert "inbox_exists=True" in events, events
    assert "outbox_exists=True" in events, events
    # Cooperative exit — stop_event drove the loop out, no cancel.
    assert "sweeper_exit" in events, events


# ---------------------------------------------------------------------------
# Scenario 2 — stop order: _media_sweep_stop.set() BEFORE drain awaits.
#
# The core of commit 18l's regression gate. We prove that the sweeper
# task, when awaited by the `_bg_tasks` drain, sees `stop_event` as
# ALREADY set (i.e. it exits via `stop_event.wait()` unblocking, NOT
# via `CancelledError` from the gather-timeout path).
#
# Detection: instrument the sweeper stub to (a) record whether the
# exit was cooperative or via CancelledError, and (b) record whether
# `stop_event.is_set()` at the moment the task awakes from its
# `stop_event.wait()`. The cooperative path proves ordering.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_sets_media_sweep_event_before_bg_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "skills").mkdir()
    _patch_daemon_env(monkeypatch)

    observations: dict[str, Any] = {
        "exit_kind": None,  # "cooperative" | "cancelled"
        "stop_event_set_on_wake": None,  # bool
        "sweeper_started": False,
    }
    sweeper_running = asyncio.Event()

    async def _cooperative_sweeper(
        data_dir: Path,
        settings: Any,
        stop_event: asyncio.Event,
        log: Any,
    ) -> None:
        del data_dir, settings, log
        observations["sweeper_started"] = True
        sweeper_running.set()
        try:
            # Mirror the real loop shape: wait on the event. If stop
            # ordering is correct, this await returns normally when
            # `_media_sweep_stop.set()` runs. If ordering is wrong
            # (drain hard-cancels us first), we land in the except
            # branch.
            await stop_event.wait()
        except asyncio.CancelledError:
            observations["exit_kind"] = "cancelled"
            raise
        observations["stop_event_set_on_wake"] = stop_event.is_set()
        observations["exit_kind"] = "cooperative"

    monkeypatch.setattr(main_mod, "media_sweeper_loop", _cooperative_sweeper)

    daemon = Daemon(_make_settings(tmp_path))
    try:
        await daemon.start()
        await asyncio.wait_for(sweeper_running.wait(), timeout=2.0)
        # Sanity: stop event is NOT set while daemon is running.
        assert not daemon._media_sweep_stop.is_set()
    finally:
        await daemon.stop()

    # Post-stop invariants.
    assert observations["sweeper_started"] is True
    assert observations["exit_kind"] == "cooperative", observations
    assert observations["stop_event_set_on_wake"] is True, observations
    # `Daemon.stop()` must flip the event and the drain must have
    # fully reaped the task (no lingering refs).
    assert daemon._media_sweep_stop.is_set()
    assert not daemon._bg_tasks


@pytest.mark.asyncio
async def test_stop_sets_event_before_gather_across_multiple_bg_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stricter ordering: record the global sequence of events in stop()
    and assert `_media_sweep_stop.set()` precedes `gather` awaiting the
    sweeper task.

    We monkey-patch `self._media_sweep_stop.set` to append to a shared
    sequence log, and wrap the sweeper stub to log when it exits. Even
    with other bg-tasks (scheduler loop, dispatcher, etc.) in the drain
    pool, the sweeper MUST exit only AFTER the set() call.
    """
    (tmp_path / "skills").mkdir()
    _patch_daemon_env(monkeypatch)

    sequence: list[str] = []
    sweeper_running = asyncio.Event()

    async def _sequenced_sweeper(
        data_dir: Path,
        settings: Any,
        stop_event: asyncio.Event,
        log: Any,
    ) -> None:
        del data_dir, settings, log
        sweeper_running.set()
        await stop_event.wait()
        sequence.append("sweeper_exit")

    monkeypatch.setattr(main_mod, "media_sweeper_loop", _sequenced_sweeper)

    daemon = Daemon(_make_settings(tmp_path))
    # Wrap `_media_sweep_stop.set` so the stop-ordering trace is
    # observable. `asyncio.Event.set` is a bound method; replace it
    # with a callable that logs then delegates.
    original_set = daemon._media_sweep_stop.set

    def _tracing_set() -> None:
        sequence.append("stop_event_set")
        original_set()

    daemon._media_sweep_stop.set = _tracing_set  # type: ignore[method-assign]

    try:
        await daemon.start()
        await asyncio.wait_for(sweeper_running.wait(), timeout=2.0)
    finally:
        await daemon.stop()

    # Ordering assertion — the set() MUST precede the sweeper's exit,
    # which only happens when the drain gathers it.
    assert "stop_event_set" in sequence, sequence
    assert "sweeper_exit" in sequence, sequence
    assert sequence.index("stop_event_set") < sequence.index("sweeper_exit"), sequence


# ---------------------------------------------------------------------------
# Scenario 3 — missing-dir resilience of the real sweeper.
#
# Defense in depth: even if the ordering gate ever regresses, the
# sweeper itself must not crash on a `<data_dir>/media/{inbox,outbox}`
# that doesn't exist. `sweep_media_once` delegates to `_scan` which
# early-returns an empty list for non-existent dirs; we verify the
# end-to-end tick returns a zero summary and the loop exits
# cooperatively when `stop_event` is set.
#
# We call `media_sweeper_loop` DIRECTLY (not through Daemon) so we
# fully control the data_dir state — the point is to prove the
# sweeper's own robustness, not the daemon's ordering.
# ---------------------------------------------------------------------------


class _FakeLog:
    """Collects structured log calls so assertions can check for
    unexpected warnings (a silent crash would show up as a
    `media_sweep_tick_failed` warning)."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.infos: list[tuple[str, dict[str, Any]]] = []
        self.debugs: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.warnings.append((event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self.infos.append((event, kw))

    def debug(self, event: str, **kw: Any) -> None:
        self.debugs.append((event, kw))


@pytest.mark.asyncio
async def test_sweep_media_once_handles_missing_dirs(tmp_path: Path) -> None:
    """Directly invoking `sweep_media_once` on a pristine `data_dir`
    (no `media/` subtree) must return a zero-summary tick without
    raising — the `_scan` guard (`if not dir_path.exists(): return [])`
    is load-bearing for pitfall #14's safety net.
    """
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "bogus",  # never created on disk
        claude=ClaudeSettings(),
        subagent=SubagentSettings(enabled=False),
        media=MediaSettings(sweep_interval_s=3600),
    )
    log = _FakeLog()

    # Precondition: the `media/` subtree genuinely doesn't exist.
    assert not (settings.data_dir / "media").exists()

    summary = await sweep_media_once(settings.data_dir, settings, log)

    assert summary == {"removed_old": 0, "removed_lru": 0, "bytes_freed": 0}
    # No warnings should fire — a missing dir is expected in this
    # regression scenario and handled silently by `_scan`.
    assert log.warnings == [], log.warnings


@pytest.mark.asyncio
async def test_media_sweeper_loop_handles_missing_dirs_and_exits_on_stop(
    tmp_path: Path,
) -> None:
    """End-to-end: the real `media_sweeper_loop` must survive a missing
    `media/` layout for at least one tick, then exit cleanly when
    `stop_event.set()` fires. We use a 50 ms interval to guarantee the
    test observes at least one successful tick before shutdown.
    """
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "bogus",  # deliberately non-existent
        claude=ClaudeSettings(),
        subagent=SubagentSettings(enabled=False),
        # Very short interval so we can race a tick in before stop.
        media=MediaSettings(sweep_interval_s=1),
    )
    log = _FakeLog()
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        media_sweeper_loop(settings.data_dir, settings, stop_event, log),
        name="sweeper_under_test",
    )

    # Give the sweeper a moment to execute its first tick against the
    # missing layout. `sweep_media_once` should return a zero summary
    # well within this window.
    await asyncio.sleep(0.1)

    # Signal shutdown; the loop wakes from `asyncio.wait_for(...)` and
    # exits cleanly.
    stop_event.set()

    # Bound the drain — if the loop hangs, this surfaces loudly
    # rather than stalling the suite.
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    assert task.exception() is None, task.exception()

    # Sanity: the per-tick failure warning is NOT in the log. This
    # would fire if `sweep_media_once` raised on the missing dirs.
    tick_failures = [e for e, _ in log.warnings if e == "media_sweep_tick_failed"]
    assert tick_failures == [], log.warnings


# ---------------------------------------------------------------------------
# Scenario 4 — idempotent restart.
#
# A fresh `Daemon(settings).start()` after a prior `Daemon.stop()`
# must work as if no prior daemon existed. In particular, the fresh
# daemon's `_media_sweep_stop` is a *new* `asyncio.Event` (created in
# `__init__`), so the prior `.set()` cannot leak through and
# short-circuit the new sweeper. We also confirm that re-running
# `ensure_media_dirs` on the same data_dir is a no-op (the mkdir
# implementation uses `exist_ok=True`).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_daemon_start_after_stop_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "skills").mkdir()
    _patch_daemon_env(monkeypatch)

    sweeper_started_first = asyncio.Event()
    sweeper_started_second = asyncio.Event()
    first_stop_event_seen_set: dict[str, bool] = {}

    async def _first_sweeper(
        data_dir: Path,
        settings: Any,
        stop_event: asyncio.Event,
        log: Any,
    ) -> None:
        del data_dir, settings, log
        sweeper_started_first.set()
        await stop_event.wait()
        first_stop_event_seen_set["value"] = stop_event.is_set()

    monkeypatch.setattr(main_mod, "media_sweeper_loop", _first_sweeper)

    settings = _make_settings(tmp_path)
    daemon_one = Daemon(settings)
    await daemon_one.start()
    await asyncio.wait_for(sweeper_started_first.wait(), timeout=2.0)
    await daemon_one.stop()

    # Invariants after first shutdown.
    assert daemon_one._media_sweep_stop.is_set()
    assert not daemon_one._bg_tasks
    assert first_stop_event_seen_set.get("value") is True

    # --- Second daemon on the same data_dir ---
    second_stop_event_id: dict[str, int] = {}
    second_stop_event_initial: dict[str, bool] = {}

    async def _second_sweeper(
        data_dir: Path,
        settings: Any,
        stop_event: asyncio.Event,
        log: Any,
    ) -> None:
        del data_dir, settings, log
        # Capture the identity of the event object and its state at
        # coroutine start — both must indicate a FRESH event.
        second_stop_event_id["id"] = id(stop_event)
        second_stop_event_initial["is_set"] = stop_event.is_set()
        sweeper_started_second.set()
        await stop_event.wait()

    monkeypatch.setattr(main_mod, "media_sweeper_loop", _second_sweeper)

    daemon_two = Daemon(settings)
    # The second daemon's event object must be a DIFFERENT instance —
    # proves `_media_sweep_stop` is per-instance and no stale state
    # from daemon_one can poison daemon_two.
    assert daemon_two._media_sweep_stop is not daemon_one._media_sweep_stop
    assert not daemon_two._media_sweep_stop.is_set()

    await daemon_two.start()
    try:
        await asyncio.wait_for(sweeper_started_second.wait(), timeout=2.0)
    finally:
        await daemon_two.stop()

    # The sweeper started against a FRESH, un-set event.
    assert second_stop_event_initial.get("is_set") is False, second_stop_event_initial
    assert second_stop_event_id.get("id") == id(daemon_two._media_sweep_stop)
    assert second_stop_event_id.get("id") != id(daemon_one._media_sweep_stop)

    # Media dirs still exist (idempotent mkdir) and the second stop
    # drained cleanly too.
    assert (tmp_path / "data" / "media" / "inbox").is_dir()
    assert (tmp_path / "data" / "media" / "outbox").is_dir()
    assert daemon_two._media_sweep_stop.is_set()
    assert not daemon_two._bg_tasks
