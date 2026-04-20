from __future__ import annotations

from pathlib import Path

from assistant.bridge.hooks import make_file_hook


def _decision_deny(resp: dict[str, object]) -> bool:
    out = resp.get("hookSpecificOutput")
    return isinstance(out, dict) and out.get("permissionDecision") == "deny"


async def test_relative_path_resolved_against_project_root(tmp_path: Path) -> None:
    """B8: Read('../../etc/passwd') MUST deny — the hook must resolve the
    relative path against project_root before checking containment."""
    project_root = tmp_path / "proj"
    project_root.mkdir()

    hook = make_file_hook(project_root)
    resp = await hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "../../etc/passwd"},
        },
        None,
        {},
    )
    assert _decision_deny(resp)


async def test_absolute_path_outside_denied(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    hook = make_file_hook(project_root)
    resp = await hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/etc/passwd"},
        },
        None,
        {},
    )
    assert _decision_deny(resp)


async def test_relative_path_inside_allowed(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "file.py").write_text("# test")
    hook = make_file_hook(project_root)
    resp = await hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "file.py"},
        },
        None,
        {},
    )
    assert not _decision_deny(resp)


async def test_write_empty_file_path_denied(tmp_path: Path) -> None:
    """B9: empty file_path for Read/Write/Edit → deny."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    hook = make_file_hook(project_root)
    for tool in ("Read", "Write", "Edit"):
        resp = await hook({"tool_name": tool, "tool_input": {"file_path": ""}}, None, {})
        assert _decision_deny(resp), tool
        resp = await hook({"tool_name": tool, "tool_input": {}}, None, {})
        assert _decision_deny(resp), tool


async def test_grep_empty_path_defaults_to_project_root(tmp_path: Path) -> None:
    """B9: Glob/Grep with empty path defaults to '.' (project root) —
    project-wide scans are legitimate and must be allowed."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    hook = make_file_hook(project_root)
    for tool in ("Glob", "Grep"):
        resp = await hook({"tool_name": tool, "tool_input": {"pattern": "*.py"}}, None, {})
        assert not _decision_deny(resp), tool


async def test_sibling_directory_denied(tmp_path: Path) -> None:
    """BW1: ``proj-sibling/secret.txt`` shares the string prefix of
    ``proj`` — the old ``str.startswith`` containment check would admit
    it. ``Path.is_relative_to`` must deny this cross-project read across
    ALL 5 file tools.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    sibling = tmp_path / "proj-sibling"
    sibling.mkdir()
    secret = sibling / "secret.txt"
    secret.write_text("leak")

    hook = make_file_hook(proj)

    # Try the sibling via relative traversal AND via absolute path.
    for candidate in (
        "../proj-sibling/secret.txt",  # relative, string prefix still matches
        str(secret.resolve()),  # absolute, string prefix matches
    ):
        for tool in ("Read", "Write", "Edit"):
            resp = await hook(
                {"tool_name": tool, "tool_input": {"file_path": candidate}},
                None,
                {},
            )
            assert _decision_deny(resp), f"{tool} {candidate!r} must deny"
        for tool in ("Glob", "Grep"):
            resp = await hook(
                {"tool_name": tool, "tool_input": {"path": candidate}},
                None,
                {},
            )
            assert _decision_deny(resp), f"{tool} {candidate!r} must deny"
