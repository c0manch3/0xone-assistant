"""``vault-commit-push`` exit 5 ``no_changes`` path (phase-8 C4).

R-9 / B2: we use ``git status --porcelain`` (not ``git diff --quiet``) to
detect changes, because ``--quiet`` misses untracked files. An empty
porcelain output means the vault is clean and there's nothing to commit.

On a freshly bootstrapped vault (no upstream tracking configured yet,
no real content), the CLI runs through:

    1. bootstrap the repo (empty bootstrap commit on branch ``main``).
    2. ``unpushed_commit_count`` returns 0 (no ``@{u}`` → treats as
       nothing unpushed; see :func:`git_ops.unpushed_commit_count`).
    3. porcelain is empty → exit 5 ``no_changes``.

We also cover the "content-present, then committed, then re-run with no
new changes" scenario by pushing the content once and re-running; this
time there IS an upstream, unpushed count is 0, porcelain is empty → 5.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main


def test_vault_commit_push_no_changes_fresh_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """First run with empty vault: bootstrap fires, porcelain empty → exit 5."""
    env = install_file_remote(monkeypatch, tmp_path)

    rc = gh_main.main(["vault-commit-push"])

    assert rc == 5, f"expected no_changes (5), got {rc}"
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"ok": True, "no_changes": True}

    # Bootstrap side effects: .git + .gitignore both present.
    assert (env.vault_dir / ".git").is_dir()
    gitignore = (env.vault_dir / ".gitignore").read_text()
    assert ".tmp/" in gitignore, (
        f".gitignore should exclude .tmp/ per SF-D7, got: {gitignore!r}"
    )


def test_vault_commit_push_no_changes_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Second run after a successful commit+push sees no new changes → exit 5."""
    env = install_file_remote(monkeypatch, tmp_path)

    # First run: bootstrap + detect no changes (vault is empty).
    rc1 = gh_main.main(["vault-commit-push"])
    assert rc1 == 5
    capsys.readouterr()  # drain

    # Add real content; run again → real commit + push. vault_dir was
    # created by the CLI's mkdir step during the first run, so the
    # directory already exists.
    note = env.vault_dir / "note.md"
    note.write_text("hello\n")
    rc2 = gh_main.main(["vault-commit-push"])
    assert rc2 == 0, f"content run expected OK (0), got {rc2}"
    payload2 = json.loads(capsys.readouterr().out.strip())
    assert payload2["ok"] is True
    assert payload2["files_changed"] >= 1

    # Third run: nothing changed since last commit → exit 5.
    rc3 = gh_main.main(["vault-commit-push"])
    assert rc3 == 5, f"clean re-run expected no_changes (5), got {rc3}"
    payload3 = json.loads(capsys.readouterr().out.strip())
    assert payload3 == {"ok": True, "no_changes": True}
