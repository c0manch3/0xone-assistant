"""Phase 8 §2.5 / AC#11 — stale .git/index.lock cleanup at boot.

A SIGKILL'd daemon mid-``git commit`` leaves
``<vault>/.git/index.lock`` on disk; the next sync cycle hangs
indefinitely on ``fatal: Unable to create '.git/index.lock': File
exists``. ``_cleanup_stale_vault_locks`` mirrors the phase-6a
``_boot_sweep_uploads`` pattern.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from assistant.vault_sync.boot import _cleanup_stale_vault_locks


def _make_git_dir(vault_dir: Path) -> Path:
    git = vault_dir / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    return git


def test_removes_stale_index_lock(tmp_path: Path) -> None:
    """5-min-old ``.git/index.lock`` is removed on boot."""
    vault = tmp_path / "vault"
    vault.mkdir()
    git = _make_git_dir(vault)
    lock = git / "index.lock"
    lock.write_text("")
    old = time.time() - 5 * 60
    os.utime(lock, (old, old))
    assert lock.exists()

    _cleanup_stale_vault_locks(vault)

    assert not lock.exists()


def test_keeps_fresh_index_lock(tmp_path: Path) -> None:
    """Fresh (<60s) lock is left alone — a healthy git op may hold it
    momentarily and we must not race it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    git = _make_git_dir(vault)
    lock = git / "index.lock"
    lock.write_text("")
    # mtime is now → not stale.
    assert lock.exists()

    _cleanup_stale_vault_locks(vault)

    assert lock.exists()


def test_missing_dir_is_noop(tmp_path: Path) -> None:
    """A vault dir that does not exist yet (fresh deploy) → no error."""
    target = tmp_path / "no_such_vault"
    assert not target.exists()
    _cleanup_stale_vault_locks(target)  # must not raise
    assert not target.exists()


def test_removes_stale_ref_lock(tmp_path: Path) -> None:
    """``refs/heads/main.lock`` is also reaped when stale."""
    vault = tmp_path / "vault"
    vault.mkdir()
    git = _make_git_dir(vault)
    ref_lock = git / "refs" / "heads" / "main.lock"
    ref_lock.write_text("")
    old = time.time() - 5 * 60
    os.utime(ref_lock, (old, old))

    _cleanup_stale_vault_locks(vault)

    assert not ref_lock.exists()


def test_no_git_dir_is_noop(tmp_path: Path) -> None:
    """Vault dir exists but is not yet a git repo (pre-bootstrap) →
    no-op."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _cleanup_stale_vault_locks(vault)  # must not raise
    assert vault.exists()
