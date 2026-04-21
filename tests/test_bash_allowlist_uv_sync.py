"""Bash allowlist for uv sync — phase 3."""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.hooks import make_bash_hook


def _is_deny(resp: dict[str, object]) -> bool:
    out = resp.get("hookSpecificOutput")
    return isinstance(out, dict) and out.get("permissionDecision") == "deny"


async def _decide(cmd: str, pr: Path) -> dict[str, object]:
    hook = make_bash_hook(pr)
    return await hook({"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {})


async def test_uv_sync_tools_allowed(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    (pr / "tools" / "foo").mkdir(parents=True)
    decision = await _decide("uv sync --directory=tools/foo", pr)
    assert not _is_deny(decision)


async def test_uv_sync_skills_allowed(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    (pr / "skills" / "foo").mkdir(parents=True)
    decision = await _decide("uv sync --directory=skills/foo", pr)
    assert not _is_deny(decision)


async def test_uv_sync_outside_denied(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    decision = await _decide("uv sync --directory=../../etc", pr)
    assert _is_deny(decision)


async def test_uv_sync_missing_directory_denied(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    decision = await _decide("uv sync", pr)
    assert _is_deny(decision)


async def test_uv_sync_space_separated_directory_allowed(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    (pr / "tools" / "bar").mkdir(parents=True)
    decision = await _decide("uv sync --directory tools/bar", pr)
    assert not _is_deny(decision)
