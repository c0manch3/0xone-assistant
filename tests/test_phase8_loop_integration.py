"""Phase 8 fix-pack F3 + F13 — loop() integration test.

Drives the real ``VaultSyncSubsystem.loop()`` task with mocked git
ops so each tick fires deterministically. Pins three properties:

  - **F3** — between ticks, the loop task is NOT in
    ``_vault_sync_pending``. The set holds at most one
    ``vault_sync_tick`` child task at a time, and it self-removes via
    ``add_done_callback``.
  - **F11** — first tick fires AFTER ``first_tick_delay_s`` seconds
    (we set ``first_tick_delay_s=0`` for test speed; the contract is
    "no immediate tick at startup if the delay is positive").
  - **F13** — when the loop is cancelled mid-flight, the drain logic
    completes cleanly within a small budget.

We mock both ``vault_lock`` (so the inner sync ctx mgr does nothing)
and the git_ops async wrappers.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from assistant.config import VaultSyncSettings
from assistant.vault_sync import subsystem as sub_mod
from assistant.vault_sync.subsystem import VaultSyncSubsystem


def _settings() -> VaultSyncSettings:
    return VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=True,
        cron_interval_s=0.1,  # tight tick for test
        push_timeout_s=10,
        drain_timeout_s=10.0,
        git_op_timeout_s=2,
        vault_lock_acquire_timeout_s=8.0,
        first_tick_delay_s=0.0,  # no boot-pressure delay in tests
    )


def _build_sub(tmp_path: Path) -> tuple[
    VaultSyncSubsystem, set[asyncio.Task[Any]], dict[str, int]
]:
    """Build a subsystem + the pending_set it observes + a tick-count
    dict the test inspects to decide when to stop the loop."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".git").mkdir()
    run = tmp_path / "run"
    run.mkdir()
    pending: set[asyncio.Task[Any]] = set()
    sub = VaultSyncSubsystem(
        vault_dir=vault,
        index_db_lock_path=tmp_path / "memory-index.db.lock",
        settings=_settings(),
        adapter=None,
        owner_chat_id=42,
        run_dir=run,
        pending_set=pending,
    )
    key = tmp_path / "vault_deploy"
    key.write_text("dummy")
    kh = tmp_path / "known_hosts_vault"
    kh.write_text("github.com ssh-ed25519 AAAA")
    sub._resolved_key_path = key  # type: ignore[attr-defined]
    sub._resolved_known_hosts_path = kh  # type: ignore[attr-defined]
    return sub, pending, {"ticks": 0}


def _patch_git(
    monkeypatch: pytest.MonkeyPatch,
    counters: dict[str, int],
) -> None:
    @contextlib.contextmanager
    def _noop_lock(*_args: Any, **_kw: Any) -> Iterator[None]:
        yield None

    monkeypatch.setattr(sub_mod, "vault_lock", _noop_lock)

    async def _status(_v: Path, *, timeout_s: float) -> str:
        return ""  # noop

    async def _add(_v: Path, *, timeout_s: float) -> None:
        return None

    async def _diff(_v: Path, *, timeout_s: float) -> list[str]:
        return []

    async def _commit(_v: Path, **_kw: Any) -> str:
        return "sha-noop"

    async def _push(_v: Path, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(sub_mod, "git_status_porcelain", _status)
    monkeypatch.setattr(sub_mod, "git_add_all", _add)
    monkeypatch.setattr(sub_mod, "git_diff_cached_names", _diff)
    monkeypatch.setattr(sub_mod, "git_commit", _commit)
    monkeypatch.setattr(sub_mod, "git_push", _push)


@pytest.mark.asyncio
async def test_f3_pending_set_does_not_hold_loop_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 (code-review CRIT-1, qa CRIT-3): the OUTER loop task must
    NEVER be in ``_vault_sync_pending``. The set holds only the
    PER-TICK child tasks, which self-remove on completion.

    Earlier impl used ``asyncio.current_task()`` inside
    ``_run_once_tracked`` which captured the infinite loop task and
    stuck it in the pending_set — drain budget always exhausted.
    """
    sub, pending, _counters = _build_sub(tmp_path)
    _patch_git(monkeypatch, _counters)
    loop_task = asyncio.create_task(sub.loop())
    # Let the loop fire >= 1 tick.
    await asyncio.sleep(0.25)
    # The loop_task itself MUST NOT be in pending.
    assert loop_task not in pending
    # The pending set is small (transient ticks self-remove). The
    # invariant is "loop never stays in pending" — we don't assert
    # set is empty because there might be a tick in flight.
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task


@pytest.mark.asyncio
async def test_f11_first_tick_delay_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F11 — when ``first_tick_delay_s`` is non-zero, the loop sleeps
    BEFORE firing the first tick (boot-pressure protection)."""
    sub, _pending, _counters = _build_sub(tmp_path)
    # Use a 1.0s delay for the test.
    sub._settings = VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=True,
        cron_interval_s=0.1,
        push_timeout_s=10,
        drain_timeout_s=10.0,
        git_op_timeout_s=2,
        vault_lock_acquire_timeout_s=8.0,
        first_tick_delay_s=1.0,
    )

    @contextlib.contextmanager
    def _noop_lock(*_args: Any, **_kw: Any) -> Iterator[None]:
        yield None

    monkeypatch.setattr(sub_mod, "vault_lock", _noop_lock)
    fired = asyncio.Event()

    async def _status(_v: Path, *, timeout_s: float) -> str:
        fired.set()
        return ""

    async def _add(_v: Path, *, timeout_s: float) -> None:
        return None

    async def _diff(_v: Path, *, timeout_s: float) -> list[str]:
        return []

    async def _commit(_v: Path, **_kw: Any) -> str:
        return "sha-noop"

    async def _push(_v: Path, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(sub_mod, "git_status_porcelain", _status)
    monkeypatch.setattr(sub_mod, "git_add_all", _add)
    monkeypatch.setattr(sub_mod, "git_diff_cached_names", _diff)
    monkeypatch.setattr(sub_mod, "git_commit", _commit)
    monkeypatch.setattr(sub_mod, "git_push", _push)

    loop_task = asyncio.create_task(sub.loop())
    # At t=0.5s, the first tick should NOT have fired yet (delay=1.0s).
    try:
        await asyncio.wait_for(fired.wait(), timeout=0.5)
        pytest.fail("first tick fired before first_tick_delay_s elapsed")
    except TimeoutError:
        pass  # expected
    # By t=1.5s the first tick should have fired.
    try:
        await asyncio.wait_for(fired.wait(), timeout=1.5)
    finally:
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task


@pytest.mark.asyncio
async def test_f13_loop_drain_completes_fast_after_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F13 — cancelling the loop while one or more ticks are pending
    drains within a tight budget (<500ms). The drain mirrors the
    ``Daemon.stop`` path in isolation.
    """
    sub, pending, _counters = _build_sub(tmp_path)
    _patch_git(monkeypatch, _counters)
    loop_task = asyncio.create_task(sub.loop())
    await asyncio.sleep(0.25)  # let it run ≥1 tick
    loop_start = asyncio.get_event_loop().time()
    loop_task.cancel()
    # Drain anything left in the pending set (mirrors Daemon.stop).
    if pending:
        snapshot = list(pending)
        _done, not_done = await asyncio.wait(
            snapshot,
            timeout=0.5,
            return_when=asyncio.ALL_COMPLETED,
        )
        for t in not_done:
            t.cancel()
        await asyncio.gather(*not_done, return_exceptions=True)
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    elapsed = asyncio.get_event_loop().time() - loop_start
    assert elapsed < 0.6, f"drain took too long: {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_f3_each_tick_is_a_fresh_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 — each tick runs as a new asyncio.Task named
    'vault_sync_tick' that registers and self-removes from the
    pending_set. Multiple ticks across a short window must not
    accumulate in the set.
    """
    sub, pending, _counters = _build_sub(tmp_path)
    _patch_git(monkeypatch, _counters)
    loop_task = asyncio.create_task(sub.loop())
    # Let several ticks fire.
    await asyncio.sleep(0.5)
    # Pending set size is bounded (≤1 in flight at a time). The
    # invariant is "no unbounded growth".
    assert len(pending) <= 1
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
