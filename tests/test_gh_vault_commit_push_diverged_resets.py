"""B-B2 / I-8.3 — divergence on a fresh commit triggers ``reset --soft HEAD~1``.

Real-git end-to-end: we engineer a divergence by pushing from a second
clone INTO the bare repo between the vault's own commits. The next
vault push fails with "non-fast-forward", the CLI classifies as
diverged and runs ``reset --soft HEAD~1``.

Post-condition assertions:

- Exit 7.
- Payload has ``"reset": true``.
- ``git log`` on vault_dir: the commit we just made is no longer HEAD
  (HEAD moved back one step).
- ``git status --porcelain`` on vault_dir is non-empty — the working
  tree changes are preserved so the next run can retry.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run `git -C <cwd> <args>` via subprocess; return stripped stdout."""
    proc = subprocess.run(  # noqa: S603 — trusted git
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
    )
    return proc.stdout.strip()


def _seed_divergence(
    bare_repo: Path, tmp_path: Path, branch: str, author_email: str
) -> None:
    """Push a "foreign" commit into the bare repo from a throw-away clone.

    After this call, the bare repo's ``<branch>`` ref points at a commit
    with a file NOT present in the vault's history — guaranteeing that
    the vault's next push is non-fast-forward.
    """
    clone = tmp_path / f"foreign-clone-{branch}"
    subprocess.run(  # noqa: S603
        ["git", "clone", "-q", str(bare_repo), str(clone)],
        check=True,
        capture_output=True,
    )
    # If the clone came up with a different default branch, switch.
    _git(clone, "checkout", "-q", "-B", branch)
    (clone / "foreign.md").write_text("foreign commit\n")
    _git(clone, "add", "foreign.md")
    _git(clone, "-c", f"user.email={author_email}", "-c", "user.name=foreign",
         "commit", "-q", "-m", "foreign")
    _git(clone, "push", "-q", "origin", branch)


def test_diverged_resets_local_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real divergence → exit 7 → ``reset --soft`` preserves working-tree changes."""
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # First successful push: establish the vault+bare common history.
    (env.vault_dir / "note.md").write_text("initial\n")
    rc1 = gh_main.main(["vault-commit-push"])
    assert rc1 == 0, f"initial push expected OK, got {rc1}"
    capsys.readouterr()  # drain

    head_before_diverge = _git(env.vault_dir, "rev-parse", "HEAD")

    # Create the divergence — another clone pushes a "foreign" commit.
    _seed_divergence(
        env.bare_repo,
        tmp_path,
        env.settings.vault_branch,
        env.settings.commit_author_email,
    )

    # Now the vault writes its own new content. The next push WILL fail
    # with non-fast-forward because bare has "foreign.md" that the vault
    # doesn't know about.
    (env.vault_dir / "note.md").write_text("local change\n")

    rc2 = gh_main.main(["vault-commit-push"])
    assert rc2 == 7, f"expected DIVERGED (7), got {rc2}"

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"] == "remote_has_diverged"
    assert payload["reset"] is True, (
        f"B-B2: reset flag should be True, got: {payload!r}"
    )

    # HEAD moved back to the pre-commit state — the commit we just made
    # has been unmade.
    head_after_reset = _git(env.vault_dir, "rev-parse", "HEAD")
    assert head_after_reset == head_before_diverge, (
        f"B-B2: HEAD should have rolled back. before={head_before_diverge!r}, "
        f"after={head_after_reset!r}"
    )

    # Working tree is dirty — the owner's edits are preserved.
    porcelain = _git(env.vault_dir, "status", "--porcelain")
    assert porcelain, (
        f"B-B2: working tree should be dirty post-reset; porcelain empty: {porcelain!r}"
    )
    # note.md is the file that was re-edited; it must show in porcelain.
    assert "note.md" in porcelain
