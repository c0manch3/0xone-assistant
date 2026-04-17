"""Bash allowlist matrix for `gh` CLI (phase 3).

Five allow cases cover the read-only endpoints the skill-installer actually
needs; the deny matrix pins down every write-flag + every blocked
subcommand so a future `gh` version that adds new aliases cannot silently
broaden the attack surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "skills").mkdir()
    (tmp_path / "tools").mkdir()
    return tmp_path


# ---------------------------------------------------------------- ALLOW

ALLOW_CASES = [
    "gh api /repos/anthropics/skills/contents/skills",
    "gh api /repos/anthropics/skills/contents/skills/skill-creator/SKILL.md",
    'gh api "/repos/x/y/contents/skills?ref=main"',
    "gh api /repos/x/y/tarball/main",
    "gh auth status",
]


@pytest.mark.parametrize("cmd", ALLOW_CASES)
def test_gh_allow(cmd: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is None, f"expected ALLOW, got DENY: {reason!r}"


# ---------------------------------------------------------------- DENY

DENY_MATRIX = [
    ("gh api /graphql", "read-only whitelist"),
    ("gh api /user", "read-only whitelist"),
    ("gh api /search/repositories", "read-only whitelist"),
    ("gh api -X POST /repos/x/y/issues", "flag -X"),
    ("gh api --method PATCH /repos/x/y", "flag --method"),
    ("gh api -F title=X /repos/x/y/issues", "flag -F"),
    ("gh api -f title=X /repos/x/y/issues", "flag -f"),
    ("gh api --field title=X /repos/x/y/issues", "flag --field"),
    ("gh api --raw-field title=X /repos/x/y", "flag --raw-field"),
    ("gh api --input foo.json /repos/x/y", "flag --input"),
    ("gh pr create", "subcommand 'pr'"),
    ("gh issue create", "subcommand 'issue'"),
    ("gh repo create", "subcommand 'repo'"),
    ("gh workflow run foo", "subcommand 'workflow'"),
    ("gh gist create", "subcommand 'gist'"),
    ("gh release create v1", "subcommand 'release'"),
    ("gh secret set FOO", "subcommand 'secret'"),
    ("gh auth login", "only `gh auth status`"),
    ("gh auth logout", "only `gh auth status`"),
]


@pytest.mark.parametrize("cmd,needle", DENY_MATRIX)
def test_gh_deny(cmd: str, needle: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is not None, f"expected DENY: {cmd!r}"
    assert needle in reason, f"reason {reason!r} missing {needle!r}"
