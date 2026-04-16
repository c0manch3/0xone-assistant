"""Path-guard test for the file-tool PreToolUse hook.

Covers Read/Write/Edit (file_path), Glob/Grep (pattern), and the relative-
plus-`..` traversal vector that v1 missed because `is_absolute()` skipped
relative inputs entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_file_path


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# ok\n", encoding="utf-8")
    return tmp_path.resolve()


# ---------------------------------------------------------------- ALLOW

ALLOW_PATHS = [
    "src/main.py",
    "src/",
    "README.md",  # may not exist; resolution still inside root
    "tools/ping/main.py",
]


@pytest.mark.parametrize("raw_path", ALLOW_PATHS)
def test_file_hook_allow_inside_root(raw_path: str, project_root: Path) -> None:
    assert check_file_path(raw_path, project_root) is None


def test_file_hook_allow_absolute_inside_root(project_root: Path) -> None:
    abs_path = str(project_root / "src" / "main.py")
    assert check_file_path(abs_path, project_root) is None


# ---------------------------------------------------------------- DENY

DENY_PATHS = [
    "../../etc/passwd",
    "src/../../../tmp/leak",
    "tools/../../etc/hosts",
    "/etc/passwd",
    "/etc/shadow",
    "/root/.ssh/id_rsa",
    # Glob-style relative traversal -- v1 hook had `if p.is_absolute()` so
    # this case slipped through entirely. Now `..`-component check rejects
    # it before resolution.
    "../**/*.env",
    "../*",
]


@pytest.mark.parametrize("raw_path", DENY_PATHS)
def test_file_hook_deny_outside_or_dotdot(raw_path: str, project_root: Path) -> None:
    reason = check_file_path(raw_path, project_root)
    assert reason is not None, f"expected DENY: {raw_path!r}"
    assert ".." in reason or "escapes" in reason


def test_file_hook_empty_path_allowed(project_root: Path) -> None:
    # Empty / missing field is a no-op (some tools may pass blank); the
    # hook must not crash, just allow.
    assert check_file_path("", project_root) is None
