"""Phase 8 (C6) — Bash-hook integration test for the gh CLI dispatch.

Verifies that ``_validate_python_invocation`` routes
``python tools/gh/main.py <argv>`` through ``_validate_gh_argv`` and
that the phase-3 ``gh`` direct CLI validator (``gh api``, ``gh auth
status``) is NOT regressed by the new wiring.

Uses ``check_bash_command`` so the full pipeline (metachar gate →
shlex → program allowlist → python-path prefix → gh-argv validator)
is exercised end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import (
    _validate_bash_argv,
    check_bash_command,
)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "tools" / "gh").mkdir(parents=True)
    (tmp_path / "tools" / "gh" / "main.py").write_text("# stub\n")
    return tmp_path


# ---------------------------------------------------------------- ALLOW

def test_bash_argv_routes_gh_cli_auth_status(project_root: Path) -> None:
    argv = ["python", "tools/gh/main.py", "auth-status"]
    assert _validate_bash_argv(argv, project_root) is None


def test_bash_argv_routes_gh_cli_issue_list(project_root: Path) -> None:
    argv = [
        "python",
        "tools/gh/main.py",
        "issue",
        "list",
        "--repo",
        "owner/repo",
    ]
    assert _validate_bash_argv(argv, project_root) is None


def test_check_bash_command_allows_vault_commit_push(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/gh/main.py vault-commit-push --dry-run",
        project_root,
    )
    assert reason is None, reason


def test_check_bash_command_allows_repo_view(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/gh/main.py repo view --repo owner/repo",
        project_root,
    )
    assert reason is None, reason


# ---------------------------------------------------------------- DENY (routed via gh validator)

def test_bash_argv_denies_force_in_vault_commit(project_root: Path) -> None:
    argv = ["python", "tools/gh/main.py", "vault-commit-push", "--force"]
    reason = _validate_bash_argv(argv, project_root)
    assert reason is not None and "not allowed" in reason


def test_bash_argv_denies_pr_create(project_root: Path) -> None:
    argv = ["python", "tools/gh/main.py", "pr", "create"]
    reason = _validate_bash_argv(argv, project_root)
    assert reason is not None and "phase 9" in reason


def test_check_bash_command_denies_repo_clone(project_root: Path) -> None:
    """SF-C6: ``repo`` is restricted to ``view`` only."""
    reason = check_bash_command(
        "python tools/gh/main.py repo clone owner/repo",
        project_root,
    )
    assert reason is not None
    assert "only 'view'" in reason


def test_check_bash_command_denies_body_file(project_root: Path) -> None:
    """SF-C6: ``--body-file`` is a path-escape vector; globally denied."""
    reason = check_bash_command(
        "python tools/gh/main.py issue create --repo o/r "
        "--title t --body-file /etc/passwd",
        project_root,
    )
    assert reason is not None
    assert "not allowed" in reason


def test_check_bash_command_denies_limit_over_cap(project_root: Path) -> None:
    """--limit must stay in 1..100."""
    reason = check_bash_command(
        "python tools/gh/main.py issue list --repo o/r --limit 101",
        project_root,
    )
    assert reason is not None
    assert "1..100" in reason


def test_check_bash_command_denies_unknown_subcommand(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/gh/main.py api /repos/owner/repo",
        project_root,
    )
    assert reason is not None
    assert "not allowed" in reason


# ---------------------------------------------------------------- regression (phase-3 gh direct)

def test_phase3_gh_api_still_allowed(project_root: Path) -> None:
    """``gh api <safe-endpoint>`` must remain allowed — the new
    ``_validate_gh_argv`` only handles ``python tools/gh/main.py``.
    """
    argv = ["gh", "api", "/repos/anthropics/skills/contents/"]
    assert _validate_bash_argv(argv, project_root) is None


def test_phase3_gh_auth_status_still_allowed(project_root: Path) -> None:
    argv = ["gh", "auth", "status"]
    assert _validate_bash_argv(argv, project_root) is None


def test_phase3_gh_api_post_still_denied(project_root: Path) -> None:
    """Write-flag block on the direct ``gh api`` path is untouched."""
    argv = ["gh", "api", "-X", "POST", "/repos/owner/repo/issues"]
    reason = _validate_bash_argv(argv, project_root)
    assert reason is not None
    assert "not allowed" in reason or "not in read-only" in reason
