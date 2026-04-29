"""Phase 6e fix-pack-2 (DevOps CRIT-3) — Daemon._rss_observer.

Four invariants:

- Happy path: a synthetic ``/proc/self/status`` is parsed and a
  structured ``daemon_rss`` log is emitted with the expected
  ``rss_mb``, ``bg_tasks``, ``audio_persist_pending``, ``sub_pending``
  counters. Counters reflect current set sizes so the operator can
  correlate memory bumps with bg work in flight.
- macOS dev path: when ``/proc/self/status`` is absent the observer
  exits silently after the first attempt; no log spam, no exception.
- Cancel-safe: cancelling the observer mid-sleep settles cleanly with
  a single ``CancelledError`` — ``Daemon.stop`` must be able to drain
  the observer without surprises.
- Read errors are logged at ``debug`` and the loop continues — a
  transient permission flake or one-off parse error must not crash
  the daemon's observability path.

These tests drive ``Daemon._rss_observer`` directly rather than
booting a full daemon — fast, deterministic, no Telegram surface.
"""

from __future__ import annotations

import asyncio
import builtins
from pathlib import Path
from typing import Any

import pytest
import structlog

from assistant.config import (
    AudioBgSettings,
    ClaudeSettings,
    ObservabilitySettings,
    SchedulerSettings,
    Settings,
)
from assistant.main import Daemon


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(
            timeout=30, max_concurrent=1, history_limit=5
        ),
        scheduler=SchedulerSettings(enabled=False),
        audio_bg=AudioBgSettings(drain_timeout_s=2.0),
        observability=ObservabilitySettings(rss_interval_s=60.0),
    )


async def test_rss_observer_emits_structured_log_with_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First tick parses ``VmRSS:`` and emits ``daemon_rss`` with the
    correct rss_mb plus the current bg/persist/sub counters.

    Mocks ``open("/proc/self/status")`` so the test is portable across
    Linux + macOS CI runners (the CRIT-3 hook explicitly targets the
    Linux container at deploy time).
    """
    d = Daemon(_settings(tmp_path))

    # Seed the counters so we can pin the emitted values. The set
    # contents are irrelevant — only ``len`` is read.
    sentinel_bg = asyncio.create_task(asyncio.sleep(60))
    sentinel_persist = asyncio.create_task(asyncio.sleep(60))
    sentinel_sub = asyncio.create_task(asyncio.sleep(60))
    d._bg_tasks.add(sentinel_bg)
    d._audio_persist_pending.add(sentinel_persist)
    d._sub_pending_updates.add(sentinel_sub)

    # 5 GiB RSS in kB so the integer division ``rss_kb // 1024`` lands
    # cleanly at 5120 MB.
    fake_status = (
        "Name:\tpython3\n"
        "State:\tR (running)\n"
        "VmRSS:\t5242880 kB\n"
        "VmSize:\t8388608 kB\n"
    )

    real_open = builtins.open

    def _fake_open(
        path: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if str(path) == "/proc/self/status":
            from io import StringIO

            return StringIO(fake_status)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)

    with structlog.testing.capture_logs() as records:
        # Use a small interval and cancel after the first emit so the
        # test stays sub-second.
        task = asyncio.create_task(d._rss_observer(interval_s=10.0))
        # Yield enough times for the first tick to land before sleep.
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    rss_events = [r for r in records if r.get("event") == "daemon_rss"]
    assert rss_events, f"expected one daemon_rss event, got: {records!r}"
    evt = rss_events[0]
    assert evt["rss_mb"] == 5242880 // 1024  # = 5120
    assert evt["bg_tasks"] == len(d._bg_tasks)
    assert evt["audio_persist_pending"] == len(d._audio_persist_pending)
    assert evt["sub_pending"] == len(d._sub_pending_updates)
    # log level is INFO for the heartbeat (operator wants visibility).
    assert evt.get("log_level") == "info"

    # Cleanup the seed tasks — they hold long sleeps and would warn
    # at loop close otherwise.
    for t in (sentinel_bg, sentinel_persist, sentinel_sub):
        t.cancel()
    await asyncio.gather(
        sentinel_bg,
        sentinel_persist,
        sentinel_sub,
        return_exceptions=True,
    )


async def test_rss_observer_silent_exit_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/proc/self/status`` is absent on macOS — the observer must
    exit silently (no log line, no traceback) after the first
    ``FileNotFoundError`` so dev runs aren't spammed.
    """
    d = Daemon(_settings(tmp_path))

    real_open = builtins.open

    def _fake_open(
        path: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if str(path) == "/proc/self/status":
            raise FileNotFoundError(2, "No such file or directory")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)

    with structlog.testing.capture_logs() as records:
        # Returns immediately on FileNotFoundError — no need to cancel.
        await asyncio.wait_for(
            d._rss_observer(interval_s=10.0), timeout=2.0
        )

    rss_events = [r for r in records if r.get("event") == "daemon_rss"]
    failed_events = [
        r for r in records if r.get("event") == "rss_read_failed"
    ]
    assert rss_events == []
    assert failed_events == [], (
        "FileNotFoundError must be silent (early return), "
        f"got rss_read_failed events: {failed_events!r}"
    )


async def test_rss_observer_cancel_safe_during_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the observer mid-sleep yields a clean ``CancelledError``
    out of ``await asyncio.sleep(...)`` — no leaked task, no shielded
    inner state to drain. ``Daemon.stop`` relies on this for prompt
    shutdown.
    """
    d = Daemon(_settings(tmp_path))

    real_open = builtins.open

    def _fake_open(
        path: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if str(path) == "/proc/self/status":
            from io import StringIO

            return StringIO("VmRSS:\t1024 kB\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)

    # Use a long interval so the task spends most of its life inside
    # ``await asyncio.sleep(...)`` after the first tick.
    task = asyncio.create_task(d._rss_observer(interval_s=3600.0))
    # Allow the first tick to fire and the loop to enter sleep.
    for _ in range(5):
        await asyncio.sleep(0)

    assert not task.done(), "observer must still be running mid-sleep"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Task is in the cancelled state; no pending callbacks.
    assert task.cancelled()


async def test_rss_observer_logs_debug_on_read_error_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-FileNotFoundError exception (e.g. a transient parse glitch
    or PermissionError) is logged at ``debug`` and the loop continues
    sleeping; a single bad read must NOT crash the observer.
    """
    d = Daemon(_settings(tmp_path))

    calls = {"n": 0}
    real_open = builtins.open

    def _fake_open(
        path: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if str(path) == "/proc/self/status":
            calls["n"] += 1
            raise PermissionError(13, "denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)

    with structlog.testing.capture_logs() as records:
        task = asyncio.create_task(d._rss_observer(interval_s=10.0))
        # Allow the first attempt to land and the loop to reach sleep.
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # PermissionError logged at debug, NOT propagated; observer was
    # alive after the failed read (proven by reaching the cancel
    # point inside ``asyncio.sleep``).
    assert calls["n"] >= 1
    failed = [r for r in records if r.get("event") == "rss_read_failed"]
    assert failed, (
        "expected rss_read_failed debug log on PermissionError, "
        f"records: {records!r}"
    )
    assert failed[0].get("log_level") == "debug"
    # And no bogus daemon_rss emitted on the failed read.
    rss_events = [r for r in records if r.get("event") == "daemon_rss"]
    assert rss_events == []
