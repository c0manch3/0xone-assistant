"""Bootstrap flow for an empty vault_dir (phase-8 C4 / R-8).

Asserts the side effects of ``_vault_bootstrap`` when ``vault_dir`` has
no ``.git/`` on first run:

- ``.git/`` directory created (``rev-parse --is-inside-work-tree`` = ``true``).
- ``.gitignore`` contains ``.tmp/`` (SF-D7).
- ``git remote -v`` shows the configured vault-backup URL.
- HEAD's parent is the bootstrap commit (``"bootstrap"`` message), and
  HEAD itself is the real content commit (after the CLI stages + commits
  the owner's file).

We drive the CLI end-to-end via :func:`install_file_remote` so the
behaviour observed is what the daemon / owner actually gets.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main


def _git(cwd: Path, *args: str) -> str:
    """Run ``git -C <cwd> <args>`` and return stripped stdout.

    Raises :class:`subprocess.CalledProcessError` on non-zero rc so the
    test surfaces the exact git failure rather than an opaque assertion.
    """
    proc = subprocess.run(  # noqa: S603 — trusted git binary
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def test_bootstrap_empty_vault(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty vault_dir bootstraps on first run; subsequent commit is HEAD."""
    env = install_file_remote(monkeypatch, tmp_path)

    # The CLI's step-2 mkdir will create `vault_dir` if missing. We
    # pre-create it empty (no `.git/`) to match the plan's "user has a
    # directory but no git" scenario.
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("bootstrap content\n")

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 0, f"expected OK, got {rc}"
    capsys.readouterr()  # drain

    # .git directory exists.
    assert (env.vault_dir / ".git").is_dir()
    # Inside a work tree per git's own opinion.
    assert _git(env.vault_dir, "rev-parse", "--is-inside-work-tree") == "true"

    # .gitignore contains `.tmp/` (SF-D7 — memory indexer scratch excluded).
    gitignore = (env.vault_dir / ".gitignore").read_text()
    assert ".tmp/" in gitignore, f"SF-D7 violated: {gitignore!r}"
    assert "*.tmp" in gitignore

    # Remote registered and points at the configured URL.
    remotes = _git(env.vault_dir, "remote", "-v")
    assert "vault-backup" in remotes, remotes
    assert env.remote_url in remotes, remotes

    # HEAD is the real commit (`note.md` present) and its parent is the
    # `bootstrap` empty commit.
    log = _git(env.vault_dir, "log", "--format=%s", "-n", "10")
    commits = log.splitlines()
    assert len(commits) == 2, (
        f"expected exactly 2 commits (bootstrap + content), got: {commits!r}"
    )
    assert commits[1] == "bootstrap", (
        f"oldest commit should be bootstrap, got {commits[1]!r}"
    )
    # Newest commit should NOT be bootstrap (it's the content commit).
    assert commits[0] != "bootstrap"

    # HEAD contains `note.md`.
    files_in_head = _git(env.vault_dir, "show", "--name-only", "--pretty=format:", "HEAD")
    assert "note.md" in files_in_head.splitlines()


def test_bootstrap_respects_preexisting_gitignore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the owner pre-populated ``.gitignore``, bootstrap doesn't overwrite it.

    The production code checks ``gitignore.exists()`` and skips the
    template write if true. This preserves manually tuned ignores
    across a reinstall / user-deletes-`.git` scenario.
    """
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    custom_ignore = "# Owner's custom ignore\n*.bak\n"
    (env.vault_dir / ".gitignore").write_text(custom_ignore)
    (env.vault_dir / "note.md").write_text("x\n")

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 0
    capsys.readouterr()

    gitignore = (env.vault_dir / ".gitignore").read_text()
    assert gitignore == custom_ignore, (
        f"pre-existing .gitignore was overwritten: {gitignore!r}"
    )
