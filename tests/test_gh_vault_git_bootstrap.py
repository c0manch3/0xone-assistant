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

    # .gitignore contains `/.tmp/` (SF-D7 — memory indexer scratch excluded).
    # T4.3: the template now uses `/.tmp/` (anchored) to match only the
    # top-level .tmp directory. It also carries credential patterns
    # mirrored from bridge/hooks.py.
    gitignore = (env.vault_dir / ".gitignore").read_text()
    assert "/.tmp/" in gitignore, f"SF-D7 violated: {gitignore!r}"
    assert "*.tmp" in gitignore
    # T4.3 extended patterns — the template also covers credential files.
    for pattern in (".env", "*.pem", "*.key", "id_rsa*", "credentials*"):
        assert pattern in gitignore, (
            f"T4.3: expected {pattern!r} in .gitignore template; got {gitignore!r}"
        )

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


def test_bootstrap_merges_preexisting_gitignore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T4.3: if the owner pre-populated ``.gitignore``, bootstrap preserves
    the curated content but appends the mandatory SF-D7 lines.

    Earlier behaviour skipped the template entirely when a file was
    present, which left migrated vaults without the ``/.tmp/`` guard
    — a potential data leak. T4.3 merge semantics append only the
    missing mandatory lines so owner customisation is preserved while
    SF-D7 is guaranteed.
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
    # Owner's original content is preserved verbatim at the top.
    assert gitignore.startswith(custom_ignore), (
        f"pre-existing .gitignore must be preserved verbatim; got {gitignore!r}"
    )
    # SF-D7 mandatory lines are appended.
    lines_stripped = {line.strip() for line in gitignore.splitlines()}
    assert "/.tmp/" in lines_stripped, (
        f"T4.3: SF-D7 '/.tmp/' must be appended to existing .gitignore; "
        f"got {gitignore!r}"
    )
    assert "*.tmp" in lines_stripped, (
        f"T4.3: SF-D7 '*.tmp' must be appended to existing .gitignore; "
        f"got {gitignore!r}"
    )
    # Custom line is still present.
    assert "*.bak" in lines_stripped


def test_bootstrap_preserves_gitignore_when_sfd7_already_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T4.3: no append when SF-D7 lines are already in the owner's file.

    Idempotent behaviour — a ``.gitignore`` that already contains the
    mandatory lines is left untouched so repeat invocations don't
    grow the file with duplicate marker blocks.
    """
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    custom_ignore = (
        "# Owner's custom ignore\n"
        "/.tmp/\n"
        "*.tmp\n"
        "*.bak\n"
    )
    (env.vault_dir / ".gitignore").write_text(custom_ignore)
    (env.vault_dir / "note.md").write_text("x\n")

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 0
    capsys.readouterr()

    gitignore = (env.vault_dir / ".gitignore").read_text()
    assert gitignore == custom_ignore, (
        "SF-D7 lines already present; bootstrap must not append a "
        f"second marker block. Got: {gitignore!r}"
    )
