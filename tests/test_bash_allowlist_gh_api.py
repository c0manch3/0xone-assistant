"""Bash allowlist for gh api — B11 wave-2 flag-whitelist.

Covers deny and allow cases outlined in implementation.md §3.4.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import make_bash_hook


def _is_deny(resp: dict[str, object]) -> bool:
    out = resp.get("hookSpecificOutput")
    return isinstance(out, dict) and out.get("permissionDecision") == "deny"


async def _decide(cmd: str, project_root: Path) -> dict[str, object]:
    hook = make_bash_hook(project_root)
    return await hook({"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {})


DENY_CASES = [
    # B11 regression: --hostname must be denied (absent from old deny-list).
    "gh api --hostname evil.com /repos/anthropics/skills/contents/skills",
    "gh api --paginate /repos/anthropics/skills/contents/skills",
    "gh api -X DELETE /repos/anthropics/skills/contents/skills",
    "gh api /repos/anthropics/skills/contents/skills -H Authorization:Bearer X",
    "gh api /repos/anthropics/skills/contents/skills -F field=value",
    "gh api --input payload.json /repos/anthropics/skills/contents/skills",
    # Endpoint whitelist: traversal + restricted path.
    "gh api /repos/../../etc/passwd",
    "gh api /user",
    "gh api /search/code",
    # Non-api subcommands.
    "gh issue list",
    "gh pr create",
    # Only `gh auth status` passes under the `auth` subcommand.
    "gh auth login",
    "gh auth refresh",
]

ALLOW_CASES = [
    "gh api /repos/anthropics/skills/contents/skills",
    "gh api -H Accept:application/vnd.github.v3+json /repos/anthropics/skills/contents/skills",
    "gh api /repos/anthropics/skills/tarball/main",
    "gh auth status",
]


@pytest.mark.parametrize("cmd", DENY_CASES)
async def test_gh_api_denied(cmd: str, tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    decision = await _decide(cmd, pr)
    assert _is_deny(decision), f"expected DENY for {cmd!r}, got {decision!r}"


@pytest.mark.parametrize("cmd", ALLOW_CASES)
async def test_gh_api_allowed(cmd: str, tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    decision = await _decide(cmd, pr)
    assert not _is_deny(decision), f"expected ALLOW for {cmd!r}, got {decision!r}"


async def test_gh_api_endpoint_with_query(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    cmd = "gh api /repos/anthropics/skills/contents/skills?ref=main"
    decision = await _decide(cmd, pr)
    assert not _is_deny(decision)
