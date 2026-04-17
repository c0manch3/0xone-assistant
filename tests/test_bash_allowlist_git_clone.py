"""Bash allowlist matrix for `git clone --depth=1` + `uv sync --directory ...`
(phase 3 extensions to the phase-2 allowlist)."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "skills").mkdir()
    (tmp_path / "tools").mkdir()
    return tmp_path


# ---------------------------------------------------------------- git clone


def test_git_clone_skills_allow(project_root: Path) -> None:
    assert (
        check_bash_command(
            "git clone --depth=1 https://github.com/x/y skills/y", project_root
        )
        is None
    )


def test_git_clone_tools_allow(project_root: Path) -> None:
    assert (
        check_bash_command(
            "git clone --depth=1 https://github.com/x/y tools/y", project_root
        )
        is None
    )


def test_git_clone_ssh_url_allow(project_root: Path) -> None:
    assert (
        check_bash_command(
            "git clone --depth=1 git@github.com:x/y.git skills/y", project_root
        )
        is None
    )


DENY_MATRIX_GIT = [
    ("git clone --depth=1 https://github.com/x/y /tmp/x", "escape"),
    ("git clone --depth=1 https://github.com/x/y ../escape", "'..'"),
    ("git clone --depth=1 file:///etc/passwd dest", "https://"),
    ("git clone --depth=1 http://1.2.3.4 dest", "https://"),
    ("git clone https://github.com/x/y dest", "--depth=1"),
    (
        "git clone --depth=1 https://169.254.169.254/foo dest",
        "non-public IP literal",
    ),
    (
        "git -c core.sshCommand=touch clone --depth=1 https://github.com/x/y skills/y",
        "-c",
    ),
]


@pytest.mark.parametrize("cmd,needle", DENY_MATRIX_GIT)
def test_git_clone_deny(cmd: str, needle: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is not None, f"expected DENY: {cmd!r}"
    assert needle in reason, f"reason {reason!r} missing {needle!r}"


# ---------------------------------------------------------------- uv sync


def test_uv_sync_allow(project_root: Path) -> None:
    (project_root / "tools" / "foo").mkdir()
    assert (
        check_bash_command("uv sync --directory tools/foo", project_root) is None
    )


def test_uv_sync_allow_equals_form(project_root: Path) -> None:
    (project_root / "tools" / "foo").mkdir()
    assert (
        check_bash_command("uv sync --directory=tools/foo", project_root) is None
    )


DENY_MATRIX_UV_SYNC = [
    ("uv sync", "requires --directory"),
    ("uv sync --directory ../etc", "'..'"),
    ("uv sync --directory /tmp/evil", "under tools/"),
    ("uv sync --directory skills/foo", "under tools/"),
    ("uv sync --directory tools/foo --extra dev", "not allowed"),
    ("uv pip install evil", "subcommand 'pip'"),
]


@pytest.mark.parametrize("cmd,needle", DENY_MATRIX_UV_SYNC)
def test_uv_sync_deny(cmd: str, needle: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is not None, f"expected DENY: {cmd!r}"
    assert needle in reason, f"reason {reason!r} missing {needle!r}"
