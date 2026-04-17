"""PostToolUse hook: Write/Edit inside `skills/` or `tools/` touches
`<data_dir>/run/skills.dirty`. Writes outside those subtrees are no-ops."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from assistant.bridge.hooks import make_posttool_hooks


async def _fire(
    project_root: Path,
    data_dir: Path,
    *,
    tool_name: str,
    file_path: str,
) -> dict[str, Any]:
    matchers = make_posttool_hooks(project_root, data_dir)
    # Take either matcher — both share the same callback.
    hook = matchers[0].hooks[0]
    out = await hook(
        cast(
            Any,
            {
                "tool_name": tool_name,
                "tool_input": {"file_path": file_path, "content": "..."},
            },
        ),
        "tu-1",
        cast(Any, {}),
    )
    return cast(dict[str, Any], out)


@pytest.mark.asyncio
async def test_write_inside_skills_touches_sentinel(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (tmp_path / "skills" / "echo").mkdir(parents=True)
    target = tmp_path / "skills" / "echo" / "SKILL.md"
    out = await _fire(tmp_path, data_dir, tool_name="Write", file_path=str(target))
    assert out == {}
    assert (data_dir / "run" / "skills.dirty").exists()


@pytest.mark.asyncio
async def test_edit_inside_tools_touches_sentinel(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (tmp_path / "tools" / "bar").mkdir(parents=True)
    target = tmp_path / "tools" / "bar" / "main.py"
    out = await _fire(tmp_path, data_dir, tool_name="Edit", file_path=str(target))
    assert out == {}
    assert (data_dir / "run" / "skills.dirty").exists()


@pytest.mark.asyncio
async def test_write_outside_skills_or_tools_noop(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "foo.py"
    out = await _fire(tmp_path, data_dir, tool_name="Write", file_path=str(target))
    assert out == {}
    assert not (data_dir / "run" / "skills.dirty").exists()


@pytest.mark.asyncio
async def test_relative_path_under_skills(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (tmp_path / "skills" / "x").mkdir(parents=True)
    out = await _fire(tmp_path, data_dir, tool_name="Write", file_path="skills/x/SKILL.md")
    assert out == {}
    assert (data_dir / "run" / "skills.dirty").exists()


@pytest.mark.asyncio
async def test_traversal_attempt_ignored(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (tmp_path / "skills").mkdir()
    out = await _fire(tmp_path, data_dir, tool_name="Write", file_path="../../etc/passwd")
    assert out == {}
    assert not (data_dir / "run" / "skills.dirty").exists()


@pytest.mark.asyncio
async def test_substring_only_match_ignored(tmp_path: Path) -> None:
    # `/tmp/evil/skills/foo` must NOT count — the detection is based on
    # `is_relative_to(project_root/skills)`, not a substring search.
    data_dir = tmp_path / "data"
    other = tmp_path / "other"
    (other / "skills").mkdir(parents=True)
    outside = other / "skills" / "x.py"
    out = await _fire(tmp_path, data_dir, tool_name="Write", file_path=str(outside))
    assert out == {}
    assert not (data_dir / "run" / "skills.dirty").exists()


@pytest.mark.asyncio
async def test_empty_file_path_ignored(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    out = await _fire(tmp_path, data_dir, tool_name="Write", file_path="")
    assert out == {}
    assert not (data_dir / "run" / "skills.dirty").exists()


@pytest.mark.asyncio
async def test_matchers_registered_for_write_and_edit(tmp_path: Path) -> None:
    matchers = make_posttool_hooks(tmp_path, tmp_path / "data")
    assert len(matchers) == 2
    assert {m.matcher for m in matchers} == {"Write", "Edit"}
