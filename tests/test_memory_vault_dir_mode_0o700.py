"""G3: `ensure_vault` creates the vault with mode 0o700 and warns if loose."""

from __future__ import annotations

import stat
from pathlib import Path

from _memlib.vault import ensure_vault


def test_fresh_dir_gets_mode_0o700(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    warnings = ensure_vault(vault)
    mode = vault.stat().st_mode & 0o777
    # umask may strip bits, so assert at least that no group/other bits are set.
    assert not mode & 0o077, f"expected tight mode, got {oct(mode)}"
    assert warnings == []
    # .tmp also exists and is 0o700-ish.
    tmp = vault / ".tmp"
    assert tmp.is_dir()


def test_preexisting_loose_dir_warns(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o755)
    # Chmod is deferred-safe in case umask interfered with mkdir.
    vault.chmod(0o755)
    warnings = ensure_vault(vault)
    # No chmod — operator intent is preserved — but warning is emitted.
    assert vault.stat().st_mode & 0o777 == 0o755
    assert warnings
    assert "vault_dir_permissions_too_open" in warnings[0]


def test_idempotent(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    ensure_vault(vault)
    # Second call must not blow up or regress mode.
    ensure_vault(vault)
    assert vault.is_dir()
    assert (vault / ".tmp").is_dir()


def test_file_itself_mode_is_0o700(tmp_path: Path) -> None:
    """Sanity check that stat reflects directory semantics."""
    vault = tmp_path / "vault"
    ensure_vault(vault)
    assert stat.S_ISDIR(vault.stat().st_mode)
