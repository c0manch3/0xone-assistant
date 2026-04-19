"""B-B2 — unpushed-commit detection triggers a push-only retry on the next run.

Scenario:

1. Vault has dirty content; CLI is invoked.
2. Stage + commit succeed (local HEAD advances).
3. Push fails for a transient reason (we simulate by temporarily breaking
   the bare-repo path — the URL can't resolve, so push exits non-zero
   with a stderr that does NOT match ``DIVERGED_RE`` → classified as
   ``PUSH_FAILED`` exit 8).
4. The working tree is now clean (commit drained the index) but HEAD is
   one commit ahead of upstream — the invariant B-B2 cares about.
5. We restore the bare repo and invoke the CLI AGAIN with no new edits.
6. The CLI's unpushed-count detector sees ``unpushed > 0`` BEFORE the
   porcelain check → triggers ``_do_push_cycle(stage=False)``.
7. Push succeeds → exit 0, payload has ``"retried_unpushed": true`` and
   ``"retried_unpushed_count": 1``.

Critical property: no new commit is created during step 6/7 — the push
ships the SAME sha we created in step 2, so no data loss / duplication.

T6.1 note: this test no longer manually sets ``branch --set-upstream-to``.
The refspec-based :func:`unpushed_commit_count` uses the
``refs/remotes/<remote>/<branch>`` ref which ``git push`` auto-updates on
every successful push. The first push in the scenario is what populates
that ref; no extra config mutation is needed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main


def _git(cwd: Path, *args: str) -> str:
    """Run ``git -C <cwd> <args>`` and return stripped stdout."""
    proc = subprocess.run(  # noqa: S603 — trusted git
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def test_unpushed_retry_preserves_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Push fail → next run pushes same commit; no re-stage, no new sha."""
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("content\n")

    # Stage 0: initial successful push. `git push` on success
    # auto-updates `refs/remotes/<remote>/<branch>` — that's the ref the
    # refspec-based `unpushed_commit_count` compares against from the
    # second run onward. T6.1 removed the manual `--set-upstream-to`
    # setup that the @{u}-based implementation used to need.
    rc0 = gh_main.main(["vault-commit-push"])
    assert rc0 == 0, f"initial push expected OK (0), got {rc0}"
    capsys.readouterr()

    # Sanity: the remote-tracking ref MUST exist after the first push,
    # otherwise the refspec comparison in stage 2 would silently see 0
    # unpushed commits and the retry wouldn't fire.
    remote_ref = (
        env.vault_dir
        / ".git"
        / "refs"
        / "remotes"
        / env.settings.vault_remote_name
        / env.settings.vault_branch
    )
    assert remote_ref.is_file(), (
        f"git push must auto-populate {remote_ref} on success; "
        "refspec-based unpushed count depends on it"
    )

    # Stage 1: edit content + break the bare repo so push fails.
    (env.vault_dir / "note.md").write_text("updated content\n")
    broken_bare = env.bare_repo.with_suffix(".broken")
    env.bare_repo.rename(broken_bare)

    rc1 = gh_main.main(["vault-commit-push"])
    assert rc1 == 8, f"push-fail expected PUSH_FAILED (8), got {rc1}"
    payload1 = json.loads(capsys.readouterr().out.strip())
    assert payload1["ok"] is False
    assert payload1["error"] == "push_failed"
    # The commit HAS been made — that's the B-B2 concern.
    sha_after_first = _git(env.vault_dir, "rev-parse", "HEAD")
    assert sha_after_first == payload1["commit_sha"], (
        f"commit_sha {payload1['commit_sha']!r} != HEAD {sha_after_first!r}"
    )

    # Working tree clean but HEAD is ahead of upstream.
    porcelain_after_fail = _git(env.vault_dir, "status", "--porcelain")
    assert not porcelain_after_fail, (
        f"post-commit working tree should be clean: {porcelain_after_fail!r}"
    )

    # Stage 2: restore the bare repo, run again without touching vault files.
    broken_bare.rename(env.bare_repo)

    rc2 = gh_main.main(["vault-commit-push"])
    assert rc2 == 0, f"retry expected OK (0), got {rc2}"
    payload2 = json.loads(capsys.readouterr().out.strip())
    assert payload2["ok"] is True
    # The sha shipped on retry MUST equal the sha from stage 1 — no new
    # commit was manufactured.
    assert payload2["commit_sha"] == sha_after_first
    assert payload2["retried_unpushed"] is True
    assert payload2["retried_unpushed_count"] == 1

    # HEAD on vault is unchanged.
    sha_after_retry = _git(env.vault_dir, "rev-parse", "HEAD")
    assert sha_after_retry == sha_after_first, (
        f"retry must not create a new commit. before={sha_after_first!r} "
        f"after={sha_after_retry!r}"
    )

    # Bare repo now contains the sha.
    bare_ref = subprocess.run(  # noqa: S603
        ["git", "-C", str(env.bare_repo), "show-ref",
         f"refs/heads/{env.settings.vault_branch}"],
        capture_output=True, text=True,
    )
    assert sha_after_first in bare_ref.stdout, (
        f"bare repo should now hold {sha_after_first!r}; got: {bare_ref.stdout!r}"
    )
