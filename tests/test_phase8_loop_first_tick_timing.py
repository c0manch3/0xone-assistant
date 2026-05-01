"""Phase 8 fix-pack F7 — AC#24 first tick timing under F11.

F11 (devops CRIT-2) reordered the loop body: instead of
"fire-then-sleep" the loop now sleeps ``first_tick_delay_s`` seconds
BEFORE the first tick. Owner can override the delay to 0 to restore
the previous immediate-first-tick semantics; the spec accepts either.

This test pins the contract:

  - ``first_tick_delay_s=0`` → first tick fires within ~100ms of
    ``loop()`` start.
  - ``first_tick_delay_s=N`` → first tick fires AT ``T + N`` (within
    a generous tolerance for slow CI).
  - Subsequent ticks fire at ``cron_interval_s`` cadence (no clock
    drift accumulation under F11's wall-clock-target sleep).
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


def _build(
    tmp_path: Path,
    *,
    first_tick_delay_s: float,
    cron_interval_s: float,
) -> VaultSyncSubsystem:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (vault / ".git").mkdir(exist_ok=True)
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    settings = VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=True,
        cron_interval_s=cron_interval_s,
        push_timeout_s=10,
        drain_timeout_s=10.0,
        git_op_timeout_s=2,
        vault_lock_acquire_timeout_s=8.0,
        first_tick_delay_s=first_tick_delay_s,
    )
    pending: set[asyncio.Task[Any]] = set()
    sub = VaultSyncSubsystem(
        vault_dir=vault,
        index_db_lock_path=tmp_path / "memory-index.db.lock",
        settings=settings,
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
    return sub


def _patch_git(
    monkeypatch: pytest.MonkeyPatch, fire_times: list[float]
) -> None:
    @contextlib.contextmanager
    def _noop_lock(*_args: Any, **_kw: Any) -> Iterator[None]:
        yield None

    monkeypatch.setattr(sub_mod, "vault_lock", _noop_lock)

    async def _status(_v: Path, *, timeout_s: float) -> str:
        fire_times.append(asyncio.get_event_loop().time())
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


async def _wait_for_fire(
    fire_times: list[float], deadline: float
) -> None:
    """Polling helper — replaces a `while not fire_times: sleep(0.01)`
    pattern. We poll because the production code under test fires
    via real ``asyncio.create_task`` ticks — there's no Event to
    await without further patching production. The loop is bounded
    by ``deadline`` so the test cannot hang.
    """
    while (  # noqa: ASYNC110
        not fire_times
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_ac24_zero_delay_first_tick_immediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``first_tick_delay_s=0`` → first tick fires within ~200ms."""
    fire_times: list[float] = []
    _patch_git(monkeypatch, fire_times)
    sub = _build(
        tmp_path, first_tick_delay_s=0.0, cron_interval_s=10.0
    )
    start = asyncio.get_event_loop().time()
    loop_task = asyncio.create_task(sub.loop())
    await _wait_for_fire(fire_times, start + 0.5)
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    assert len(fire_times) >= 1
    elapsed = fire_times[0] - start
    assert elapsed < 0.2, f"first tick at {elapsed:.3f}s with delay=0"


@pytest.mark.asyncio
async def test_ac24_nonzero_delay_first_tick_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F11 — with ``first_tick_delay_s=0.3``, the first tick fires
    at T+0.3 (give or take CI slack)."""
    fire_times: list[float] = []
    _patch_git(monkeypatch, fire_times)
    sub = _build(
        tmp_path, first_tick_delay_s=0.3, cron_interval_s=10.0
    )
    start = asyncio.get_event_loop().time()
    loop_task = asyncio.create_task(sub.loop())
    # At t=0.1s, no tick yet.
    await asyncio.sleep(0.1)
    assert fire_times == []
    await _wait_for_fire(fire_times, start + 1.0)
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    assert len(fire_times) >= 1
    elapsed = fire_times[0] - start
    # Allow generous slack for CI (0.3s ± 0.3s).
    assert 0.25 <= elapsed <= 0.7, (
        f"first tick at {elapsed:.3f}s with delay=0.3"
    )


@pytest.mark.asyncio
async def test_ac24_subsequent_ticks_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the first tick, subsequent ticks fire at
    ``cron_interval_s`` cadence."""
    fire_times: list[float] = []
    _patch_git(monkeypatch, fire_times)
    sub = _build(
        tmp_path, first_tick_delay_s=0.0, cron_interval_s=0.15
    )
    loop_task = asyncio.create_task(sub.loop())
    # Let several ticks accumulate.
    await asyncio.sleep(0.5)
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    assert len(fire_times) >= 2
    # Check the gap between consecutive ticks is roughly
    # cron_interval_s (allow generous CI slack).
    for i in range(1, len(fire_times)):
        gap = fire_times[i] - fire_times[i - 1]
        assert 0.10 <= gap <= 0.30, (
            f"unexpected tick gap {gap:.3f}s at i={i}"
        )
