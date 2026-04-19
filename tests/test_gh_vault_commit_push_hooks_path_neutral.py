"""T4.1 — bootstrap neutralises `.git/hooks/*` via `core.hooksPath=/dev/null`.

Defence-in-depth: a restore operation (user copies a backup over the
live `vault_dir`, or `git clone`s into the same path) could deposit
hostile executable scripts at `.git/hooks/post-commit` etc. Setting
`core.hooksPath=/dev/null` redirects git's hook lookup to a path that
is never a directory, so the scripts never run.

We assert:

1. After the first ``vault-commit-push`` on a fresh vault, the repo's
   `core.hooksPath` config value is exactly ``"/dev/null"``.
2. An executable hook placed at ``<vault>/.git/hooks/post-commit``
   does NOT run on a subsequent commit (we prove this by having the
   hook write a sentinel file; the sentinel must not appear).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main


def test_bootstrap_sets_hooks_path_to_dev_null(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After bootstrap, ``git config core.hooksPath`` == ``/dev/null``."""
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("x\n")

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 0
    capsys.readouterr()

    proc = subprocess.run(
        ["git", "-C", str(env.vault_dir), "config", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "/dev/null", (
        f"T4.1: expected core.hooksPath=/dev/null, got {proc.stdout!r}"
    )


def test_hooks_path_neutral_blocks_post_commit_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A post-commit hook under `.git/hooks/` MUST NOT execute.

    We plant an executable hook that touches a sentinel file, run a
    second vault-commit-push (which makes a real commit), and assert
    the sentinel does NOT appear. If the hooksPath neutralisation
    regresses, the hook runs and the sentinel materialises — failing
    this test.
    """
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("first\n")

    # First run: bootstrap + commit; HEAD advances, hooks dir exists.
    rc = gh_main.main(["vault-commit-push"])
    assert rc == 0
    capsys.readouterr()

    # Plant a hostile hook. Using a path INSIDE tmp_path (not vault_dir)
    # so even if something accidentally staged the sentinel, it would
    # not pollute the commit payload under test.
    sentinel = tmp_path / "hook_ran_sentinel.txt"
    hooks_dir = env.vault_dir / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f'touch "{sentinel}"\n'
    )
    hook.chmod(0o755)
    assert os.access(hook, os.X_OK), "hook must be executable for this test"

    # Second run: edit the file so porcelain has changes, commit again.
    (env.vault_dir / "note.md").write_text("second\n")
    rc = gh_main.main(["vault-commit-push"])
    assert rc == 0
    capsys.readouterr()

    assert not sentinel.exists(), (
        "T4.1 regression: post-commit hook fired despite core.hooksPath=/dev/null; "
        f"sentinel at {sentinel}"
    )
