"""Phase 8 (C6) — ``_validate_gh_argv`` unit tests.

Covers the ``python tools/gh/main.py <argv>`` hook gate. Every DENY case
verifies an SF-C6 hardening decision: ``repo`` limited to ``view`` only,
new issue sub-subs (`develop`/`pin`/`unpin`/`status`) blocked,
``--body-file`` rejected as a path-escape vector, ``--limit`` capped
to ``1..100``.
"""

from __future__ import annotations

import pytest

from assistant.bridge.hooks import _validate_gh_argv


ALLOW: list[list[str]] = [
    ["auth-status"],
    ["issue", "list", "--repo", "owner/a"],
    ["issue", "view", "42", "--repo", "owner/a"],
    ["issue", "create", "--repo", "o/r", "--title", "t", "--body", "b"],
    ["pr", "list", "--repo", "o/r"],
    ["pr", "view", "15", "--repo", "o/r"],
    ["repo", "view", "--repo", "o/r"],
    ["vault-commit-push"],
    ["vault-commit-push", "--message", "x"],
    ["vault-commit-push", "--dry-run"],
    ["issue", "list", "--repo", "o/r", "--limit", "100"],
    ["issue", "list", "--repo", "o/r", "--limit=50"],
]

DENY: list[tuple[list[str], str]] = [
    ([], "subcommand"),
    (["api"], "not allowed"),
    (["unknown"], "not allowed"),
    (["issue", "close", "1"], "phase 9"),
    (["issue", "comment", "1"], "phase 9"),
    (["issue", "delete", "1"], "phase 9"),
    (["issue", "pin", "1"], "phase 9"),      # SF-C6
    (["issue", "unpin", "1"], "phase 9"),    # SF-C6
    (["issue", "develop", "1"], "phase 9"),  # SF-C6
    (["issue", "status"], "phase 9"),        # SF-C6
    (["pr", "create"], "phase 9"),
    (["pr", "merge", "1"], "phase 9"),
    (["pr", "diff", "1"], "phase 9"),        # SF-C6
    (["pr", "ready", "1"], "phase 9"),
    (["repo"], "sub-subcommand"),                 # SF-C6
    (["repo", "clone", "owner/repo"], "only 'view'"),   # SF-C6
    (["repo", "create", "owner/repo"], "only 'view'"),  # SF-C6
    (["repo", "delete", "owner/repo"], "only 'view'"),  # SF-C6
    (["vault-commit-push", "--force"], "not allowed"),
    (["vault-commit-push", "--no-verify"], "not allowed"),
    (["vault-commit-push", "--force-with-lease"], "not allowed"),
    (["issue", "create", "--repo", "o/r", "--body-file", "/etc/passwd"],
     "not allowed"),  # SF-C6
    (["issue", "list", "--repo", "o/r", "--limit", "101"], "1..100"),
    (["issue", "list", "--repo", "o/r", "--limit", "-1"], "1..100"),
    (["issue", "list", "--repo", "o/r", "--limit=0"], "1..100"),
    (["issue", "list", "--repo", "o/r", "--limit", "abc"], "integer"),
    (["issue", "list", "--repo", "a/b", "--repo", "c/d"], "duplicate flag"),
    (["auth-status", "-X", "POST"], "not allowed"),
    (["auth-status", "--method=POST"], "not allowed"),
]


@pytest.mark.parametrize("argv", ALLOW)
def test_allow(argv: list[str]) -> None:
    result = _validate_gh_argv(argv)
    assert result is None, f"expected ALLOW for {argv!r}, got DENY: {result!r}"


@pytest.mark.parametrize("argv,needle", DENY)
def test_deny(argv: list[str], needle: str) -> None:
    result = _validate_gh_argv(argv)
    assert result is not None, f"expected DENY for {argv!r}"
    assert needle in result, (
        f"expected deny-reason for {argv!r} to contain {needle!r}; got {result!r}"
    )


def test_limit_requires_value() -> None:
    """Trailing ``--limit`` with no value is rejected, not silently allowed."""
    result = _validate_gh_argv(["issue", "list", "--repo", "o/r", "--limit"])
    assert result is not None
    assert "requires a value" in result or "requires integer" in result
