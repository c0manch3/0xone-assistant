"""Phase 8 §2.8 — vault_push_now manual @tool rate-limit tests.

AC#13 + W2-M2:
- First call within rate-limit window → fires.
- Second call within window → ``rate_limit`` reason; git ops do NOT
  fire.
- Persistence: a fresh subsystem instance (mimicking daemon restart)
  reading the same state file still observes the rate-limit window.
- Successful invocation updates ``last_invocation_at``; rate-limited
  rejection does NOT (only successful starts reset the timer).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from assistant.config import VaultSyncSettings
from assistant.vault_sync import subsystem as sub_mod
from assistant.vault_sync.subsystem import VaultSyncSubsystem


def _settings() -> VaultSyncSettings:
    """F4: keep ``vault_lock_acquire_timeout_s >= 4 * git_op_timeout_s``."""
    return VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=True,
        manual_tool_min_interval_s=60.0,
        push_timeout_s=10,
        drain_timeout_s=10.0,
        git_op_timeout_s=2,
        vault_lock_acquire_timeout_s=8.0,
        first_tick_delay_s=0.0,
    )


def _make_subsystem(
    tmp_path: Path,
    pending: set[asyncio.Task[Any]],
) -> VaultSyncSubsystem:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (vault / ".git").mkdir(exist_ok=True)
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
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
    return sub


@pytest.fixture
def patch_git_pushes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Stub each git op so push_now succeeds without invoking real git.
    Track invocation counts."""
    counts = {"status": 0, "add": 0, "diff": 0, "commit": 0, "push": 0}

    async def _status(_v: Path, *, timeout_s: float) -> str:
        counts["status"] += 1
        return "?? new.md\n"

    async def _add(_v: Path, *, timeout_s: float) -> None:
        counts["add"] += 1

    async def _diff(_v: Path, *, timeout_s: float) -> list[str]:
        counts["diff"] += 1
        return ["new.md"]

    async def _commit(_v: Path, **_kw: Any) -> str:
        counts["commit"] += 1
        return "deadbeef"

    async def _push(_v: Path, **_kw: Any) -> None:
        counts["push"] += 1

    monkeypatch.setattr(sub_mod, "git_status_porcelain", _status)
    monkeypatch.setattr(sub_mod, "git_add_all", _add)
    monkeypatch.setattr(sub_mod, "git_diff_cached_names", _diff)
    monkeypatch.setattr(sub_mod, "git_commit", _commit)
    monkeypatch.setattr(sub_mod, "git_push", _push)
    return counts


async def test_first_call_succeeds(
    tmp_path: Path, patch_git_pushes: dict[str, int]
) -> None:
    """First push_now invocation runs the pipeline + records the
    invocation timestamp."""
    pending: set[asyncio.Task[Any]] = set()
    sub = _make_subsystem(tmp_path, pending)
    result = await sub.push_now()
    assert result.get("ok") is True
    assert result.get("result") == "pushed"
    assert sub._state.last_invocation_at is not None


async def test_second_call_within_window_rate_limited(
    tmp_path: Path, patch_git_pushes: dict[str, int]
) -> None:
    """A second invocation 30s after the first → ``reason=rate_limit``
    AND no git ops fire."""
    pending: set[asyncio.Task[Any]] = set()
    sub = _make_subsystem(tmp_path, pending)
    # Simulate first invocation 30s in the past.
    now_minus_30 = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30)
    sub._state.last_invocation_at = now_minus_30.isoformat()
    sub._state.save(sub._state_path)
    counts_before = dict(patch_git_pushes)
    result = await sub.push_now()
    assert result.get("ok") is False
    assert result.get("reason") == "rate_limit"
    next_eligible = result.get("next_eligible_in_s")
    assert next_eligible is not None and 25 <= next_eligible <= 35
    # No git op fired during the rejection.
    assert patch_git_pushes == counts_before


async def test_rate_limit_persists_across_restart(
    tmp_path: Path, patch_git_pushes: dict[str, int]
) -> None:
    """W2-M2 — a fresh subsystem instance (daemon restart) reading
    the same state file still honours the rate-limit window."""
    pending: set[asyncio.Task[Any]] = set()
    # First subsystem records the invocation.
    sub1 = _make_subsystem(tmp_path, pending)
    now_minus_10 = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=10)
    sub1._state.last_invocation_at = now_minus_10.isoformat()
    sub1._state.save(sub1._state_path)
    # Build a SECOND subsystem with the same run_dir → it loads the
    # state from disk.
    sub2 = _make_subsystem(tmp_path, pending)
    assert sub2._state.last_invocation_at is not None
    result = await sub2.push_now()
    assert result.get("ok") is False
    assert result.get("reason") == "rate_limit"


async def test_rate_limit_after_window_succeeds(
    tmp_path: Path, patch_git_pushes: dict[str, int]
) -> None:
    """Once the window elapses, the call succeeds again."""
    pending: set[asyncio.Task[Any]] = set()
    sub = _make_subsystem(tmp_path, pending)
    # Simulate an invocation > 60s ago.
    now_minus_120 = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=120)
    sub._state.last_invocation_at = now_minus_120.isoformat()
    sub._state.save(sub._state_path)
    result = await sub.push_now()
    assert result.get("ok") is True
    assert result.get("result") == "pushed"
