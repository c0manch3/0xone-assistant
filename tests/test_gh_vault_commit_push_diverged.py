"""``vault-commit-push`` divergence detection via ``DIVERGED_RE`` (phase-8 C4 / R-14).

When ``git push`` exits non-zero AND stderr matches
:data:`tools.gh._lib.git_ops.DIVERGED_RE`, the CLI classifies as
"diverged" and returns exit 7.

We exercise this via subprocess mocking: the `git push` call returns a
canned stderr containing ``! [rejected]  main -> main (non-fast-forward)``
while every other `git` call passes through the real binary. This lets
us validate the classifier without needing to construct a real diverged
upstream (which is covered separately by
``test_gh_vault_commit_push_diverged_resets.py``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main
from tools.gh._lib import git_ops


# Stderr sample captured during the R-14 spike, lightly trimmed.
_DIVERGED_STDERR = (
    "To file:///tmp/vault-bare.git\n"
    " ! [rejected]        main -> main (non-fast-forward)\n"
    "error: failed to push some refs to 'file:///tmp/vault-bare.git'\n"
    "hint: Updates were rejected because the tip of your current branch is behind\n"
    "hint: its remote counterpart. Integrate the remote changes (e.g.\n"
)


def test_diverged_classified_as_exit_7(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mock only the ``push`` invocation; everything else runs real git."""
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("content\n")

    real_run = subprocess.run

    def _selective_mock(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        # Identify the `git push` call inside _run_git's argv layout:
        #   ["git", "-C", "<vault_dir>", "push", "<remote>", "<branch>"]
        if (
            isinstance(cmd, list)
            and len(cmd) >= 5
            and cmd[0] == "git"
            and cmd[1] == "-C"
            and cmd[3] == "push"
        ):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr=_DIVERGED_STDERR,
            )
        # Everything else (init, commit, status, rev-list, etc.) goes
        # through the real `git` so the test exercises real bootstrap +
        # stage + commit semantics.
        return real_run(cmd, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(git_ops.subprocess, "run", _selective_mock)

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 7, f"expected DIVERGED (7), got {rc}"
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"] == "remote_has_diverged"
    # Divergence on a fresh (stage=True) cycle invokes `reset --soft`,
    # so payload["reset"] must be True.
    assert payload["reset"] is True
    # Stderr echoed in truncated form for observability.
    assert "non-fast-forward" in payload["stderr"]


def test_diverged_re_matches_all_known_markers() -> None:
    """Spike R-14 stderr markers match :data:`DIVERGED_RE`.

    Defence against a future regex drift: if anyone edits
    `DIVERGED_RE` to tighten coverage, this test catches the common
    markers observed on the spike.
    """
    for sample in (
        "! [rejected]        main -> main (non-fast-forward)",
        "! [rejected]        main -> main (fetch first)",
        "Updates were rejected because the tip of your current branch",
        "error: failed to push some refs; updates were rejected",
    ):
        assert git_ops.DIVERGED_RE.search(sample) is not None, (
            f"DIVERGED_RE should match: {sample!r}"
        )

    # Unrelated stderr must NOT match — otherwise generic push failures
    # would be misclassified as divergence.
    for benign in (
        "fatal: unable to access remote",
        "ssh: connect to host permission denied",
        "remote: Repository not found.",
    ):
        assert git_ops.DIVERGED_RE.search(benign) is None, (
            f"DIVERGED_RE should NOT match: {benign!r}"
        )
