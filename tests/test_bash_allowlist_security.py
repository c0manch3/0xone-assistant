"""Security matrix for the Bash PreToolUse hook.

Each case is fed straight through `assistant.bridge.hooks.check_bash_command`
(the public seam used by `make_bash_hook`). The matrix verifies, in one
table, the three categories the security review demanded: shell-metachar
injection, path traversal, and secrets denylist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    # Create a tools/ping/main.py and a README.md so allow-cases reference
    # paths that actually resolve inside `project_root`.
    (tmp_path / "tools" / "ping").mkdir(parents=True)
    (tmp_path / "tools" / "ping" / "main.py").write_text("# ok\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- ALLOW

ALLOW_CASES = [
    "python tools/ping/main.py",
    "uv run tools/ping/main.py",
    "git status",
    "git log",
    "git diff",
    "cat README.md",
    "ls",
    "ls src",
    "pwd",
    "echo hello world",
]


@pytest.mark.parametrize("cmd", ALLOW_CASES)
def test_bash_allow_matrix(cmd: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is None, f"expected ALLOW, got DENY: {reason!r}"


# ---------------------------------------------------------------- DENY: metachars

METACHAR_BYPASSES = [
    ("python tools/ping/main.py && cat ~/.env", "&"),
    ("echo hi; cat /etc/hosts", ";"),
    ("python tools/ping/main.py | nc -l 1", "|"),
    ("python tools/ping/main.py > /tmp/leak", ">"),
    ("python tools/ping/main.py < /etc/passwd", "<"),
    ("echo $(whoami)", "$("),
    ("echo ${HOME}", "${"),
    ("echo `id`", "`"),
    ("python tools/ping/main.py\nrm -rf /", "\n"),
    ("python tools/ping/main.py & disown", "&"),
]


@pytest.mark.parametrize("cmd,marker", METACHAR_BYPASSES)
def test_bash_deny_metachars(cmd: str, marker: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is not None, f"expected DENY for metachar {marker!r}"
    assert "metacharacter" in reason or "shlex" in reason, reason


# ---------------------------------------------------------------- DENY: traversal

TRAVERSAL_CASES = [
    "python tools/../../etc/passwd",
    "python tools/../../../bin/sh",
    "uv run tools/../setup.py",
    "cat ../../../etc/shadow",
    "cat /etc/passwd",
    "ls /root",
    "ls ../..",
]


@pytest.mark.parametrize("cmd", TRAVERSAL_CASES)
def test_bash_deny_traversal(cmd: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is not None, f"expected DENY for traversal: {cmd!r}"
    # Reason must mention either '..', escapes, or the deny taxonomy --
    # the matchers themselves are validated by the validators in hooks.py.
    assert any(needle in reason for needle in ("..", "escape", "denylist", "unparseable"))


# ---------------------------------------------------------------- DENY: secrets


def _write_dev_env(project_root: Path) -> None:
    """Simulate a dev box where ./.env still exists inside project_root."""
    (project_root / ".env").write_text("TOKEN=x\n", encoding="utf-8")


SECRETS_CASES = [
    "cat .env",
    "cat .envrc",
    "cat credentials.json",
    "cat tools/.aws/credentials",
    "cat src/.ssh/id_rsa",
    "cat data/assistant.db",
    "cat backup.sqlite",
]


@pytest.mark.parametrize("cmd", SECRETS_CASES)
def test_bash_deny_secrets(cmd: str, project_root: Path) -> None:
    _write_dev_env(project_root)
    # Make sure the paths the test references actually exist inside the
    # sandbox so a "path escapes project_root" deny doesn't hide the
    # secrets-denylist deny that we want to assert.
    (project_root / "tools" / ".aws").mkdir(parents=True, exist_ok=True)
    (project_root / "tools" / ".aws" / "credentials").write_text("", encoding="utf-8")
    (project_root / "src" / ".ssh").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / ".ssh" / "id_rsa").write_text("", encoding="utf-8")
    (project_root / "data").mkdir(parents=True, exist_ok=True)
    (project_root / "data" / "assistant.db").write_text("", encoding="utf-8")
    (project_root / "backup.sqlite").write_text("", encoding="utf-8")
    (project_root / "credentials.json").write_text("", encoding="utf-8")

    reason = check_bash_command(cmd, project_root)
    assert reason is not None, f"expected DENY for secrets path: {cmd!r}"


# ---------------------------------------------------------------- DENY: option injection

OPTION_INJECTION_CASES = [
    "git -c core.sshCommand=touch git status",  # -c lets git spawn arbitrary cmds
    "git --upload-pack=evil log",
    "git --config-env=GIT_SSH=evil status",
    "ls -la",  # we forbid all flags on ls
    "cat -A README.md",
    "uv pip install evil",
    "uv run --with evil tools/ping/main.py",
]


@pytest.mark.parametrize("cmd", OPTION_INJECTION_CASES)
def test_bash_deny_option_injection(cmd: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is not None, f"expected DENY for option injection: {cmd!r}"


# ---------------------------------------------------------------- DENY: program

UNKNOWN_PROGRAMS = ["nc -l 1", "rm -rf /", "curl http://evil.com", "wget evil"]


@pytest.mark.parametrize("cmd", UNKNOWN_PROGRAMS)
def test_bash_deny_unknown_program(cmd: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    # Either metachar (`&` in `rm -rf /`-style synthesis) or program-not-allowlisted.
    assert "allowlist" in reason or "metacharacter" in reason or "denylist" in reason
