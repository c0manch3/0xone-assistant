"""Phase 8 fix-pack F7 — startup_check ACs (AC#3, AC#17, AC#26).

Three force-disable paths exercised:

  - **AC#3** SSH key file missing → ``disabled_reason="ssh_key_missing"``,
    daemon continues serving phase-1..6e traffic.
  - **AC#17** known_hosts file missing →
    ``disabled_reason="known_hosts_missing"``.
  - **AC#26** known_hosts file exists but has no ``github.com`` line
    → ``disabled_reason="host_key_mismatch"``.

The subsystem under test must NEVER raise; ``startup_check`` only
flags ``_force_disabled`` and surfaces a structured log + the
``disabled_reason`` attribute the RSS observer / tests can probe.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from assistant.config import VaultSyncSettings
from assistant.vault_sync.subsystem import VaultSyncSubsystem


def _build(
    tmp_path: Path,
    *,
    ssh_key_path: Path | None,
    ssh_known_hosts_path: Path | None,
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
        ssh_key_path=ssh_key_path,
        ssh_known_hosts_path=ssh_known_hosts_path,
    )
    pending: set[asyncio.Task[Any]] = set()
    return VaultSyncSubsystem(
        vault_dir=vault,
        index_db_lock_path=tmp_path / "memory-index.db.lock",
        settings=settings,
        adapter=None,
        owner_chat_id=42,
        run_dir=run_dir,
        pending_set=pending,
    )


@pytest.mark.asyncio
async def test_ac3_ssh_key_missing_force_disables(tmp_path: Path) -> None:
    """AC#3 — SSH key file does not exist on host. ``startup_check``
    must NOT raise; it sets ``disabled_reason='ssh_key_missing'``."""
    sub = _build(
        tmp_path,
        ssh_key_path=tmp_path / "does_not_exist_key",
        ssh_known_hosts_path=tmp_path / "fake_kh",
    )
    await sub.startup_check()
    assert sub.force_disabled is True
    assert sub.disabled_reason == "ssh_key_missing"


@pytest.mark.asyncio
async def test_ac17_known_hosts_missing_force_disables(
    tmp_path: Path,
) -> None:
    """AC#17 — pinned known_hosts file does not exist on host."""
    key = tmp_path / "vault_deploy"
    key.write_text("dummy")
    sub = _build(
        tmp_path,
        ssh_key_path=key,
        ssh_known_hosts_path=tmp_path / "missing_kh",
    )
    await sub.startup_check()
    assert sub.force_disabled is True
    assert sub.disabled_reason == "known_hosts_missing"


@pytest.mark.asyncio
async def test_ac26_known_hosts_no_github_entry_force_disables(
    tmp_path: Path,
) -> None:
    """AC#26 — known_hosts file present but missing the
    ``github.com`` substring (e.g. corrupted / wrong host key
    rotation). ``disabled_reason='host_key_mismatch'``.
    """
    key = tmp_path / "vault_deploy"
    key.write_text("dummy")
    kh = tmp_path / "known_hosts_vault"
    # Pinned file with a non-github entry — simulates a stale or
    # corrupted file post host-key rotation.
    kh.write_text("not-the-real-host ssh-ed25519 AAAA")
    sub = _build(tmp_path, ssh_key_path=key, ssh_known_hosts_path=kh)
    await sub.startup_check()
    assert sub.force_disabled is True
    assert sub.disabled_reason == "host_key_mismatch"


@pytest.mark.asyncio
async def test_happy_path_keeps_force_disabled_false(
    tmp_path: Path,
) -> None:
    """Both files present + known_hosts mentions github.com → loop
    is allowed to run."""
    key = tmp_path / "vault_deploy"
    key.write_text("dummy")
    kh = tmp_path / "known_hosts_vault"
    kh.write_text("github.com ssh-ed25519 AAAAreal-fingerprint")
    sub = _build(tmp_path, ssh_key_path=key, ssh_known_hosts_path=kh)
    await sub.startup_check()
    assert sub.force_disabled is False
    assert sub.disabled_reason is None


@pytest.mark.asyncio
async def test_loop_skips_when_force_disabled(tmp_path: Path) -> None:
    """``loop()`` is a no-op when ``_force_disabled`` is set — must
    return without spawning a child tick."""
    sub = _build(
        tmp_path,
        ssh_key_path=tmp_path / "missing",
        ssh_known_hosts_path=tmp_path / "missing_kh",
    )
    await sub.startup_check()
    assert sub.force_disabled is True
    # ``loop()`` should return immediately rather than block.
    await asyncio.wait_for(sub.loop(), timeout=1.0)
