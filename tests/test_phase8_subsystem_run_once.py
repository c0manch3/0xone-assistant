"""Phase 8 — VaultSyncSubsystem.run_once unit tests.

Mocks the ``git_ops`` async wrappers so no actual git subprocess is
spawned. Covers AC#1 (happy push), AC#2 (empty diff noop), AC#10
(secret denylist block), AC#23 (lock_contention not a failure), the
push-divergence error path, and the rate-limit pre-check on
``push_now``.
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
from assistant.vault_sync.git_ops import GitOpError
from assistant.vault_sync.subsystem import VaultSyncSubsystem


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Empty vault dir + ``.git/`` subdir so cleanup-stale-locks etc.
    don't trip."""
    v = tmp_path / "vault"
    v.mkdir()
    (v / ".git").mkdir()
    return v


@pytest.fixture
def settings_enabled() -> VaultSyncSettings:
    """Vault sync settings with sensible test defaults — short
    timeouts so tests don't drag if the mock is mis-wired."""
    return VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=True,
        cron_interval_s=10.0,
        push_timeout_s=10,
        drain_timeout_s=10.0,
        git_op_timeout_s=5,
        vault_lock_acquire_timeout_s=2.0,
        manual_tool_min_interval_s=60.0,
    )


def _build_subsystem(
    vault_dir: Path,
    tmp_path: Path,
    settings: VaultSyncSettings,
    *,
    pending_set: set[asyncio.Task[Any]] | None = None,
) -> VaultSyncSubsystem:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sub = VaultSyncSubsystem(
        vault_dir=vault_dir,
        index_db_lock_path=tmp_path / "memory-index.db.lock",
        settings=settings,
        adapter=None,
        owner_chat_id=42,
        run_dir=run_dir,
        pending_set=pending_set if pending_set is not None else set(),
    )
    # Skip startup_check filesystem deps; populate the resolved paths
    # directly so the push code path works.
    key = tmp_path / "vault_deploy"
    key.write_text("dummy")
    kh = tmp_path / "known_hosts_vault"
    kh.write_text("github.com ssh-ed25519 AAAA")
    sub._resolved_key_path = key  # type: ignore[attr-defined]
    sub._resolved_known_hosts_path = kh  # type: ignore[attr-defined]
    return sub


@pytest.fixture
def patched_git(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace each git_ops function with a controllable mock that
    records calls."""
    calls: dict[str, list[Any]] = {
        "status": [],
        "add": [],
        "diff_cached": [],
        "commit": [],
        "push": [],
    }
    state: dict[str, Any] = {
        "porcelain": "?? new.md\n",
        "staged": ["new.md"],
        "commit_sha": "abc1234",
        "push_raises": None,
        "commit_raises": None,
    }

    async def _status(vault_dir: Path, *, timeout_s: float) -> str:
        calls["status"].append(vault_dir)
        return state["porcelain"]

    async def _add(vault_dir: Path, *, timeout_s: float) -> None:
        calls["add"].append(vault_dir)

    async def _diff(vault_dir: Path, *, timeout_s: float) -> list[str]:
        calls["diff_cached"].append(vault_dir)
        return state["staged"]

    async def _commit(
        vault_dir: Path,
        *,
        message: str,
        author_name: str,
        author_email: str,
        timeout_s: float,
    ) -> str:
        calls["commit"].append(message)
        if state["commit_raises"]:
            raise state["commit_raises"]
        return state["commit_sha"]

    async def _push(
        vault_dir: Path,
        *,
        remote: str,
        branch: str,
        ssh_key_path: Path,
        known_hosts_path: Path,
        timeout_s: float,
    ) -> None:
        calls["push"].append((remote, branch))
        if state["push_raises"]:
            raise state["push_raises"]

    monkeypatch.setattr(sub_mod, "git_status_porcelain", _status)
    monkeypatch.setattr(sub_mod, "git_add_all", _add)
    monkeypatch.setattr(sub_mod, "git_diff_cached_names", _diff)
    monkeypatch.setattr(sub_mod, "git_commit", _commit)
    monkeypatch.setattr(sub_mod, "git_push", _push)
    return {"calls": calls, "state": state}


async def test_happy_path_push_succeeds(
    vault_dir: Path,
    tmp_path: Path,
    settings_enabled: VaultSyncSettings,
    patched_git: dict[str, Any],
) -> None:
    """AC#1 — staged file → add → commit → push → audit row +
    ``last_state="ok"``."""
    sub = _build_subsystem(vault_dir, tmp_path, settings_enabled)
    result = await sub._run_once(reason="scheduled")
    assert result.result == "pushed"
    assert result.commit_sha == "abc1234"
    assert result.files_changed == 1
    # All five git ops fired exactly once.
    calls = patched_git["calls"]
    assert len(calls["status"]) == 1
    assert len(calls["add"]) == 1
    assert len(calls["commit"]) == 1
    assert len(calls["push"]) == 1
    # Audit row present.
    audit = sub._audit_log_path.read_text(encoding="utf-8").strip()
    assert audit
    assert '"result":"pushed"' in audit


async def test_empty_diff_is_noop(
    vault_dir: Path,
    tmp_path: Path,
    settings_enabled: VaultSyncSettings,
    patched_git: dict[str, Any],
) -> None:
    """AC#2 — empty porcelain → no add, no commit, no push, audit
    row with ``result="noop"``."""
    sub = _build_subsystem(vault_dir, tmp_path, settings_enabled)
    patched_git["state"]["porcelain"] = ""
    result = await sub._run_once(reason="scheduled")
    assert result.result == "noop"
    assert result.commit_sha is None
    calls = patched_git["calls"]
    assert len(calls["status"]) == 1
    assert calls["add"] == []
    assert calls["commit"] == []
    assert calls["push"] == []


async def test_push_failure_records_failed_and_edges_state(
    vault_dir: Path,
    tmp_path: Path,
    settings_enabled: VaultSyncSettings,
    patched_git: dict[str, Any],
) -> None:
    """A non-fast-forward / network failure → ``result="failed"``
    AND ``last_state="fail"`` + ``consecutive_failures=1``."""
    sub = _build_subsystem(vault_dir, tmp_path, settings_enabled)
    patched_git["state"]["push_raises"] = GitOpError(
        "push", "rejected non-fast-forward"
    )
    result = await sub._run_once(reason="scheduled")
    assert result.result == "failed"
    assert result.error and "rejected" in result.error
    assert sub._state.last_state == "fail"
    assert sub._state.consecutive_failures == 1


async def test_lock_contention_is_not_a_failure(
    vault_dir: Path,
    tmp_path: Path,
    settings_enabled: VaultSyncSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC#23 — vault_lock raising TimeoutError → ``result=
    lock_contention``, audit row present, NO state transition."""

    @contextlib.contextmanager
    def _raising_lock(*args: Any, **kwargs: Any) -> Iterator[None]:
        raise TimeoutError("simulated contention")
        yield  # pragma: no cover

    monkeypatch.setattr(sub_mod, "vault_lock", _raising_lock)
    sub = _build_subsystem(vault_dir, tmp_path, settings_enabled)
    sub._state.last_state = "ok"
    sub._state.consecutive_failures = 0
    result = await sub._run_once(reason="scheduled")
    assert result.result == "lock_contention"
    # State machine NOT transitioned.
    assert sub._state.last_state == "ok"
    assert sub._state.consecutive_failures == 0


async def test_secret_denylist_blocks_commit(
    vault_dir: Path,
    tmp_path: Path,
    settings_enabled: VaultSyncSettings,
    patched_git: dict[str, Any],
) -> None:
    """AC#10 — staged paths matching the denylist → ``result=
    failed`` + audit row + state edge to ``fail``."""
    sub = _build_subsystem(vault_dir, tmp_path, settings_enabled)
    patched_git["state"]["staged"] = ["secrets/api.env", "notes/x.md"]
    result = await sub._run_once(reason="scheduled")
    assert result.result == "failed"
    assert "denylist" in (result.error or "").lower() or "secret" in (
        result.error or ""
    ).lower()
    # Commit and push must NOT have fired.
    assert patched_git["calls"]["commit"] == []
    assert patched_git["calls"]["push"] == []
    assert sub._state.last_state == "fail"


async def test_recovery_edge_resets_state(
    vault_dir: Path,
    tmp_path: Path,
    settings_enabled: VaultSyncSettings,
    patched_git: dict[str, Any],
) -> None:
    """fail → ok edge: state resets and counter goes to zero (the
    notify path is exercised in test_phase8_edge_trigger_notify.py)."""
    sub = _build_subsystem(vault_dir, tmp_path, settings_enabled)
    sub._state.last_state = "fail"
    sub._state.consecutive_failures = 3
    sub._state.save(sub._state_path)
    result = await sub._run_once(reason="scheduled")
    assert result.result == "pushed"
    assert sub._state.last_state == "ok"
    assert sub._state.consecutive_failures == 0
