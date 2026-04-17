"""Phase 5 fix-pack CRITICAL #3 — heartbeat watchdog latch.

The daemon's `_scheduler_health_check_bg` fires
`_notify_with_marker(..., bypass=True)` when `last_tick_at` is stale.
`bypass=True` short-circuits the 24 h marker cooldown, so WITHOUT a
per-instance latch a genuinely-dead loop spams Telegram every 60 s
until the daemon itself dies.

The test injects a stub `asyncio.wait_for` that counts iterations and
raises `TimeoutError` so the watchdog does its stale-detection pass.
After N iterations we raise `asyncio.CancelledError` from the stub to
exit the watchdog cleanly. Exactly ONE notification must land.

Also covers CRITICAL #3's reset-on-recovery semantics: once
`last_tick_at` moves forward past the previous stale window, the
latch resets so a subsequent stall re-notifies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.main import Daemon


class _FakeAdapter:
    def __init__(self) -> None:
        self.sends: list[tuple[int, str]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sends.append((chat_id, text))


class _FakeLoop:
    def __init__(self, last: float) -> None:
        self._last = last
        self._stop = asyncio.Event()

    def last_tick_at(self) -> float:
        return self._last

    def set_last(self, last: float) -> None:
        self._last = last

    def stop_event(self) -> asyncio.Event:
        return self._stop

    def stop(self) -> None:
        self._stop.set()


class _FakeDispatcher:
    def __init__(self, last: float = 0.0) -> None:
        self._last = last
        self._stop = asyncio.Event()

    def last_tick_at(self) -> float:
        return self._last

    def set_last(self, last: float) -> None:
        self._last = last

    def stop_event(self) -> asyncio.Event:
        return self._stop

    def stop(self) -> None:
        self._stop.set()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=7,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(tick_interval_s=1, heartbeat_stale_multiplier=2),
    )


class _CancelAfter:
    """asyncio.wait_for stub that raises TimeoutError for the first N-1
    iterations and CancelledError on the Nth so the watchdog loop exits."""

    def __init__(self, iterations: int, before_iter: Any = None) -> None:
        self._left = iterations
        self._before_iter = before_iter
        self.count = 0

    # The signature mirrors `asyncio.wait_for`; the `timeout` param is
    # required for the monkeypatch to slot in cleanly. `noqa: ASYNC109`
    # silences the stylistic "don't add your own timeout kwarg" rule.
    async def __call__(
        self,
        awaitable: Any,
        timeout: float,  # noqa: ASYNC109
    ) -> None:
        del awaitable, timeout
        self.count += 1
        if self._before_iter is not None:
            self._before_iter(self.count)
        if self.count >= self._left:
            raise asyncio.CancelledError
        raise TimeoutError


async def test_heartbeat_watchdog_only_notifies_once_while_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the watchdog past three stale-detections; only one notify must fire."""
    (tmp_path / "data" / "run").mkdir(parents=True, exist_ok=True)
    adapter = _FakeAdapter()
    daemon = Daemon(_settings(tmp_path))
    daemon._adapter = adapter  # type: ignore[assignment]

    fake_loop = _FakeLoop(last=1.0)
    fake_disp = _FakeDispatcher(last=1.0)
    daemon._scheduler_loop = fake_loop  # type: ignore[assignment]
    daemon._scheduler_dispatcher = fake_disp  # type: ignore[assignment]

    # Monkeypatch asyncio.get_running_loop().time() to return a constant far
    # in the future so the stale threshold is exceeded. The running loop is
    # the real one — we only override `.time()` by wrapping the result.
    real_get_loop = asyncio.get_running_loop

    class _TimeWrap:
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            self._loop = loop

        def time(self) -> float:
            return 1_000_000.0

        def __getattr__(self, name: str) -> Any:
            return getattr(self._loop, name)

    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: _TimeWrap(real_get_loop()),
    )

    stub = _CancelAfter(iterations=4)
    monkeypatch.setattr(asyncio, "wait_for", stub)

    with pytest.raises(asyncio.CancelledError):
        await daemon._scheduler_health_check_bg()

    # Exactly ONE notification across three stale-detections.
    assert len(adapter.sends) == 1, (
        f"expected 1 notify, got {len(adapter.sends)}: {adapter.sends!r}"
    )
    assert "heartbeat" in adapter.sends[0][1].lower()


async def test_heartbeat_latch_resets_after_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the loop recovers, the latch resets — a second stall re-notifies."""
    (tmp_path / "data" / "run").mkdir(parents=True, exist_ok=True)
    adapter = _FakeAdapter()
    daemon = Daemon(_settings(tmp_path))
    daemon._adapter = adapter  # type: ignore[assignment]

    fake_loop = _FakeLoop(last=1.0)
    fake_disp = _FakeDispatcher(last=1.0)
    daemon._scheduler_loop = fake_loop  # type: ignore[assignment]
    daemon._scheduler_dispatcher = fake_disp  # type: ignore[assignment]

    now_value = 1_000_000.0

    real_get_loop = asyncio.get_running_loop

    class _TimeWrap:
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            self._loop = loop

        def time(self) -> float:
            return now_value

        def __getattr__(self, name: str) -> Any:
            return getattr(self._loop, name)

    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: _TimeWrap(real_get_loop()),
    )

    # Iteration schedule (each iteration = one `wait_for` call before the
    # stale-check):
    #   iter 1: stale → notify #1 (latch engages)
    #   iter 2: recover (last = NOW) → latch resets, no notify
    #   iter 3: stale again (last = 1.0) → notify #2
    #   iter 4: stop.
    def before(i: int) -> None:
        if i == 2:
            fake_loop.set_last(now_value)
            fake_disp.set_last(now_value)
        elif i == 3:
            fake_loop.set_last(1.0)
            fake_disp.set_last(1.0)

    stub = _CancelAfter(iterations=4, before_iter=before)
    monkeypatch.setattr(asyncio, "wait_for", stub)

    with pytest.raises(asyncio.CancelledError):
        await daemon._scheduler_health_check_bg()

    assert len(adapter.sends) == 2, (
        f"expected 2 separate notifies after recovery, got {adapter.sends!r}"
    )
