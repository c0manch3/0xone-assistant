"""Phase 8 fix-pack F7 — AC#15 prompt-injection regression.

A hostile transcript could try to coax the model into invoking
``vault_push_now`` repeatedly. The 60s rate-limit + JSONL audit log
caps the damage: only ONE pipeline run per minute lands real work,
all other invocations record a ``rate_limited`` row and return
without running git ops.

This test simulates the model's many-tries loop without touching the
SDK — we drive ``push_now`` in a tight burst and assert the audit
log shape.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from assistant.config import VaultSyncSettings
from assistant.vault_sync import subsystem as sub_mod
from assistant.vault_sync.subsystem import VaultSyncSubsystem


@pytest.fixture
def patch_git_pushes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Stub each git op so ``push_now`` succeeds without invoking real
    git. Track invocation counts so the test can assert "git ops
    fired exactly once even though we called push_now N times"."""
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


def _build_subsystem(
    tmp_path: Path,
) -> VaultSyncSubsystem:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (vault / ".git").mkdir(exist_ok=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    settings = VaultSyncSettings(
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
    pending: set[asyncio.Task[Any]] = set()
    sub = VaultSyncSubsystem(
        vault_dir=vault,
        index_db_lock_path=tmp_path / "memory-index.db.lock",
        settings=settings,
        adapter=None,
        owner_chat_id=42,
        run_dir=run_dir,
        pending_set=pending,
    )
    key = tmp_path / "vault_deploy"
    key.write_text("dummy")
    kh = tmp_path / "known_hosts_vault"
    kh.write_text("github.com ssh-ed25519 AAAA")
    sub._resolved_key_path = key  # type: ignore[attr-defined]
    sub._resolved_known_hosts_path = kh  # type: ignore[attr-defined]
    return sub


@pytest.mark.asyncio
async def test_ac15_burst_invocations_caps_at_one_pipeline(
    tmp_path: Path, patch_git_pushes: dict[str, int]
) -> None:
    """AC#15 — model spam-loops the @tool 5 times within 60s. The
    rate-limit lets exactly ONE pipeline run; the rest return
    ``rate_limit`` without firing git ops. Audit log records ONE
    ``"pushed"`` row + four ``"rate_limited"`` rows.
    """
    sub = _build_subsystem(tmp_path)
    # First call → fires the pipeline + records last_invocation_at.
    first = await sub.push_now()
    assert first.get("ok") is True
    assert first.get("result") == "pushed"
    assert patch_git_pushes["push"] == 1
    # 4 more rapid calls — all should be rate-limited.
    for _ in range(4):
        r = await sub.push_now()
        assert r.get("ok") is False
        assert r.get("reason") == "rate_limit"
    # No additional git ops fired.
    assert patch_git_pushes["push"] == 1
    # Audit log: exactly one "pushed" row + 4 "rate_limited" rows.
    audit = sub._audit_log_path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in audit.splitlines() if line]
    pushed_rows = [r for r in rows if r["result"] == "pushed"]
    rl_rows = [r for r in rows if r["result"] == "rate_limited"]
    assert len(pushed_rows) == 1
    assert len(rl_rows) == 4


@pytest.mark.asyncio
async def test_ac15_failure_resets_window_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10 — a failed manual invocation resets the rate-limit window
    so the owner can retry immediately. The audit log row still
    records the failure for forensic value.
    """

    async def _status(_v: Path, *, timeout_s: float) -> str:
        return "?? new.md\n"

    async def _add(_v: Path, *, timeout_s: float) -> None:
        return None

    async def _diff(_v: Path, *, timeout_s: float) -> list[str]:
        return ["new.md"]

    async def _commit(_v: Path, **_kw: Any) -> str:
        return "deadbeef"

    async def _push(_v: Path, **_kw: Any) -> None:
        from assistant.vault_sync.git_ops import GitOpError

        raise GitOpError("push", "rejected non-fast-forward")

    monkeypatch.setattr(sub_mod, "git_status_porcelain", _status)
    monkeypatch.setattr(sub_mod, "git_add_all", _add)
    monkeypatch.setattr(sub_mod, "git_diff_cached_names", _diff)
    monkeypatch.setattr(sub_mod, "git_commit", _commit)
    monkeypatch.setattr(sub_mod, "git_push", _push)

    sub = _build_subsystem(tmp_path)
    # Pre-set a 30s-old invocation to test that a FAILURE resets the
    # timer back to whatever it was BEFORE this call.
    pre = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=120)).isoformat()
    sub._state.last_invocation_at = pre
    sub._state.save(sub._state_path)
    r = await sub.push_now()
    assert r.get("result") == "failed"
    # F10: timer is restored to the prior value (or earlier — what
    # matters is "owner can retry now").
    restored = sub._state.last_invocation_at
    assert restored == pre  # exactly the prior value
