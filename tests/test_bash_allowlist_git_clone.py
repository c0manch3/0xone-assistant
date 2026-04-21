"""Bash allowlist for git clone — phase 3."""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.hooks import make_bash_hook


def _is_deny(resp: dict[str, object]) -> bool:
    out = resp.get("hookSpecificOutput")
    return isinstance(out, dict) and out.get("permissionDecision") == "deny"


async def _decide(cmd: str, pr: Path) -> dict[str, object]:
    hook = make_bash_hook(pr)
    return await hook({"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {})


async def test_git_clone_allowed_inside_project(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    (pr / "skills").mkdir()
    # Use a relative dest to avoid the macOS tmpdir's long pseudo-base64
    # path component accidentally tripping the BASH_SLIP_GUARD_RE regex
    # (48+ alphanumeric chars → matches the encoded-payload heuristic).
    cmd = "git clone --depth=1 https://github.com/owner/repo skills/repo"
    decision = await _decide(cmd, pr)
    assert not _is_deny(decision)


async def test_git_clone_outside_project_denied(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    cmd = "git clone --depth=1 https://github.com/owner/repo /etc/leak"
    decision = await _decide(cmd, pr)
    assert _is_deny(decision)


async def test_git_clone_non_github_denied(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    # git@evil.com:foo/bar — not github.com
    cmd = "git clone --depth=1 git@evil.com:x/y /etc/foo"
    decision = await _decide(cmd, pr)
    assert _is_deny(decision)


async def test_git_clone_no_depth_denied(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    cmd = "git clone https://github.com/x/y skills/foo"
    decision = await _decide(cmd, pr)
    assert _is_deny(decision)


async def test_git_clone_depth_not_one_denied(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    cmd = "git clone --depth=5 https://github.com/x/y skills/foo"
    decision = await _decide(cmd, pr)
    assert _is_deny(decision)


async def test_git_clone_relative_dest_inside(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    (pr / "skills").mkdir()
    cmd = "git clone --depth=1 https://github.com/owner/repo skills/repo"
    decision = await _decide(cmd, pr)
    assert not _is_deny(decision)


async def test_git_clone_relative_dest_escapes_denied(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    cmd = "git clone --depth=1 https://github.com/x/y ../escape"
    decision = await _decide(cmd, pr)
    assert _is_deny(decision)


async def test_git_clone_ssh_github_ok(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    (pr / "skills").mkdir()
    cmd = "git clone --depth=1 git@github.com:x/y.git skills/y"
    decision = await _decide(cmd, pr)
    assert not _is_deny(decision)
