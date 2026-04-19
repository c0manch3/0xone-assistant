"""B-A3 — ``vault_dir`` missing on a fresh install must NOT trigger exit 3.

Pre-v2, the CLI ran an ``is_dir()`` check before bootstrap which exited
``VALIDATION (3)`` on a brand-new install where the daemon hadn't yet
created ``<data>/vault/``. The v2 fix: ``_cmd_vault_commit_push`` now
``mkdir(parents=True, exist_ok=True, mode=0o700)``-s the vault dir
unconditionally BEFORE any check.

This test proves:

- ``ASSISTANT_DATA_DIR`` pointing at a path that does not exist yet.
- ``python tools/gh/main.py vault-commit-push --dry-run`` returns rc 0
  (not rc 3) and reports ``"would_bootstrap": true``.
- ``<tmp>/fresh/vault`` is created with mode ``0o700`` afterwards.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main


def test_mkdir_fresh_dry_run_creates_vault_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fresh ASSISTANT_DATA_DIR + ``--dry-run`` → rc 0, vault_dir created 0o700."""
    # `install_file_remote` uses <tmp>/data for data_dir + <tmp>/data/vault
    # for vault_dir. Neither exists at test entry.
    env = install_file_remote(monkeypatch, tmp_path)
    assert not env.data_dir.exists(), "test premise: data_dir should not exist yet"
    assert not env.vault_dir.exists(), "test premise: vault_dir should not exist yet"

    rc = gh_main.main(["vault-commit-push", "--dry-run"])
    assert rc == 0, f"expected OK (0) on fresh install, got {rc}"

    # Post-condition: vault_dir created with mode 0o700.
    assert env.vault_dir.is_dir(), f"{env.vault_dir} was not created"
    mode = stat.S_IMODE(env.vault_dir.stat().st_mode)
    assert mode == 0o700, (
        f"vault_dir mode should be 0o700, got {oct(mode)}"
    )

    # data_dir also created (side effect of step 5 run-dir mkdir).
    assert env.data_dir.is_dir()

    # Dry-run payload flags bootstrap would fire on a non-dry run.
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["would_bootstrap"] is True
    assert payload["vault_dir"] == str(env.vault_dir)


def test_mkdir_fresh_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running twice on the same vault_dir doesn't raise (`exist_ok=True`)."""
    env = install_file_remote(monkeypatch, tmp_path)
    # Pre-create the dir with a different mode; `exist_ok=True` should
    # accept it without rewriting permissions (Python `mkdir`'s contract).
    env.vault_dir.mkdir(parents=True, mode=0o755)

    rc1 = gh_main.main(["vault-commit-push", "--dry-run"])
    assert rc1 == 0
    capsys.readouterr()

    rc2 = gh_main.main(["vault-commit-push", "--dry-run"])
    assert rc2 == 0
    capsys.readouterr()
