"""Should-fix #9: `.tmp/` is chmod-ed to 0o700 even when it pre-existed loose."""

from __future__ import annotations

import os
from pathlib import Path

from _memlib.vault import ensure_vault


def test_existing_loose_tmp_gets_tightened(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    tmp = vault / ".tmp"
    tmp.mkdir(mode=0o755)
    tmp.chmod(0o755)

    warnings = ensure_vault(vault)
    assert tmp.stat().st_mode & 0o777 == 0o700
    # No warning on the happy path (chmod succeeded).
    assert not any("vault_tmp_chmod_failed" in w for w in warnings)


def test_already_tight_tmp_unchanged(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    # Create .tmp with the expected mode — nothing to do.
    (vault / ".tmp").mkdir(mode=0o700)
    ensure_vault(vault)
    assert (vault / ".tmp").stat().st_mode & 0o777 == 0o700


def test_chmod_failure_warns_without_raising(tmp_path: Path, monkeypatch: object) -> None:
    """If chmod raises (e.g. FS does not support mode changes), the caller
    gets a warning string and init still proceeds.
    """
    import pytest

    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    tmp = vault / ".tmp"
    tmp.mkdir(mode=0o755)
    tmp.chmod(0o755)

    orig_chmod = os.chmod

    def boom(path: str | Path, mode: int, *args: object, **kwargs: object) -> None:
        if str(path) == str(tmp):
            raise OSError("simulated EPERM")
        orig_chmod(path, mode, *args, **kwargs)

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(os, "chmod", boom)
        warnings = ensure_vault(vault)
    finally:
        mp.undo()

    assert any("vault_tmp_chmod_failed" in w for w in warnings)
