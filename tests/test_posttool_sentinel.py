"""PostToolUse sentinel touch — hot reload path."""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.hooks import make_posttool_hooks


async def test_sentinel_touched_on_skill_write(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    (pr / "skills").mkdir()
    dd = tmp_path / "data"
    dd.mkdir()
    matchers = make_posttool_hooks(pr, dd)
    # Phase 4: Write + Edit + mcp__memory__.* audit.
    assert len(matchers) == 3

    write_hook = matchers[0].hooks[0]
    await write_hook(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(pr / "skills" / "x" / "SKILL.md")},
        },
        None,
        {},
    )
    assert (dd / "run" / "skills.dirty").is_file()


async def test_sentinel_touched_on_tools_write(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    (pr / "tools").mkdir()
    dd = tmp_path / "data"
    dd.mkdir()
    matchers = make_posttool_hooks(pr, dd)
    hook = matchers[0].hooks[0]
    await hook(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(pr / "tools" / "y" / "cache.json")},
        },
        None,
        {},
    )
    assert (dd / "run" / "skills.dirty").is_file()


async def test_sentinel_not_touched_outside_skills_tools(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    dd = tmp_path / "data"
    dd.mkdir()
    matchers = make_posttool_hooks(pr, dd)
    hook = matchers[0].hooks[0]
    await hook(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(pr / "foo.py")},
        },
        None,
        {},
    )
    assert not (dd / "run" / "skills.dirty").exists()


async def test_sentinel_traversal_rejected(tmp_path: Path) -> None:
    pr = tmp_path / "proj"
    pr.mkdir()
    dd = tmp_path / "data"
    dd.mkdir()
    matchers = make_posttool_hooks(pr, dd)
    hook = matchers[0].hooks[0]
    for path in ("..", "../..", "../outside/skills/x/SKILL.md"):
        await hook(
            {"tool_name": "Write", "tool_input": {"file_path": path}},
            None,
            {},
        )
        assert not (dd / "run" / "skills.dirty").exists(), (
            f"traversal {path!r} must not touch sentinel"
        )


async def test_sentinel_edit_matcher_fires(tmp_path: Path) -> None:
    """Edit matcher fires on Edit (not just Write)."""
    pr = tmp_path / "proj"
    pr.mkdir()
    (pr / "skills").mkdir()
    dd = tmp_path / "data"
    dd.mkdir()
    matchers = make_posttool_hooks(pr, dd)
    edit_hook = matchers[1].hooks[0]  # Edit matcher
    await edit_hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(pr / "skills" / "z" / "SKILL.md")},
        },
        None,
        {},
    )
    assert (dd / "run" / "skills.dirty").is_file()
