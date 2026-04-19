"""S-3 — ``subprocess.TimeoutExpired`` inside the push path maps to exit 8.

A hung ``git push`` (dead ssh connection, stalled TCP) must NOT kill
the scheduler or leave the lock held indefinitely. ``_run_git`` wraps
``subprocess.run`` in a try/except and returns a sentinel
``GitResult(rc=-1, stderr="timeout after Ns")``; ``push()`` classifies
that as ``"failed"`` (not ``"diverged"``, because we have no
information about the remote's ref state on a timeout).

This test monkeypatches ``subprocess.run`` on the ``git_ops`` module
so only the ``git push`` invocation times out — every preceding
``git`` call (``rev-parse``, ``status``, ``add``, ``commit``) runs
normally via the real binary. Exit 8 keeps the scheduler's retry
semantics aligned: the next tick will try again, no HEAD reset.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main
from tools.gh._lib import git_ops


def test_push_timeout_maps_to_push_failed_exit_8(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,  # type: ignore[no-untyped-def]
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``git push`` timeout → exit 8, payload ``{"error": "push_failed"}``."""
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("content\n")

    real_run = subprocess.run

    def _selective_timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Raise TimeoutExpired only for the `git push` argv; passthrough otherwise.

        ``_run_git`` calls ``subprocess.run(["git", "-C", vault_dir, ...])``.
        We pattern-match on the argv to detect the push call specifically:
        positional arg 0 is the cmd list, and the first non-``-C`` token
        after ``git`` is ``push``.
        """
        cmd = args[0] if args else kwargs.get("args")
        if (
            isinstance(cmd, list)
            and len(cmd) >= 4
            and cmd[0] == "git"
            and cmd[1] == "-C"
            and cmd[3] == "push"
        ):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60.0)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_ops.subprocess, "run", _selective_timeout)

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 8, f"S-3: push timeout must map to PUSH_FAILED (8), got {rc}"

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"] == "push_failed"
    # The stderr field carries the timeout marker so operators can
    # distinguish a timeout from a generic push failure in logs.
    assert "timeout" in payload.get("stderr", "").lower(), (
        f"expected 'timeout' in stderr field; got {payload!r}"
    )


def test_run_git_timeout_returns_sentinel_not_raising(
    tmp_path,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run_git` with ``check=False`` returns a sentinel on timeout.

    Direct unit-level proof that the wrapper normalises TimeoutExpired
    into GitResult(rc=-1, stderr="timeout after Ns"), so the push()
    classifier can rely on that invariant.
    """
    # Create a real git repo so the argv is valid.
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, capture_output=True
    )

    def _always_timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=args[0] if args else "git", timeout=kwargs.get("timeout", 1.0)
        )

    monkeypatch.setattr(git_ops.subprocess, "run", _always_timeout)

    result = git_ops._run_git(
        ["status"], tmp_path, timeout_s=1.0, check=False
    )
    assert result.rc == -1, f"expected sentinel rc=-1, got {result.rc}"
    assert result.stderr.startswith("timeout after"), (
        f"expected stderr to start with 'timeout after', got {result.stderr!r}"
    )
    assert result.stdout == ""


def test_run_git_timeout_raises_when_check_true(
    tmp_path,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run_git(check=True)` on timeout must raise RuntimeError, not return sentinel.

    Callers that use ``check=True`` (``stage_all``, ``commit``, etc.)
    rely on RuntimeError to abort the cycle. Silently returning a
    sentinel would let a timeout sneak through as if the command
    succeeded.
    """
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, capture_output=True
    )

    def _always_timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=args[0] if args else "git", timeout=kwargs.get("timeout", 1.0)
        )

    monkeypatch.setattr(git_ops.subprocess, "run", _always_timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        git_ops._run_git(
            ["status"], tmp_path, timeout_s=1.0, check=True
        )
