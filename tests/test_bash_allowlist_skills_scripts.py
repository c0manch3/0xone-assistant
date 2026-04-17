"""H-1: phase-3 permits `python skills/<name>/...` + `uv run skills/...`.

Phase-2 `_validate_python_invocation` hard-coded `startswith("tools/")`.
Anthropic's `skill-creator` bundle ships `scripts/*.py` under
`skills/skill-creator/scripts/` that the model is expected to invoke
directly — `skills/` prefix is required for the bootstrap path to work.
Traversal / absolute-path escapes still fail the deny path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    # Populate a realistic `skills/skill-creator/scripts/init_skill.py` so
    # the path-resolution check inside the hook finds a real target.
    scripts_dir = tmp_path / "skills" / "skill-creator" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "init_skill.py").write_text("# ok\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "src" / "assistant").mkdir(parents=True)
    (tmp_path / "src" / "assistant" / "main.py").write_text("# ok\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- ALLOW


def test_python_skills_scripts_allowed(project_root: Path) -> None:
    assert (
        check_bash_command(
            "python skills/skill-creator/scripts/init_skill.py --name foo",
            project_root,
        )
        is None
    )


def test_uv_run_skills_scripts_allowed(project_root: Path) -> None:
    assert (
        check_bash_command(
            "uv run skills/skill-creator/scripts/init_skill.py",
            project_root,
        )
        is None
    )


def test_python_tools_still_allowed(project_root: Path) -> None:
    (project_root / "tools" / "ping").mkdir()
    (project_root / "tools" / "ping" / "main.py").write_text("", encoding="utf-8")
    assert (
        check_bash_command("python tools/ping/main.py", project_root) is None
    )


# ---------------------------------------------------------------- DENY


def test_python_skills_dotdot_denied(project_root: Path) -> None:
    reason = check_bash_command(
        "python skills/../../../etc/passwd", project_root
    )
    assert reason is not None
    assert "'..'" in reason


def test_python_src_denied(project_root: Path) -> None:
    reason = check_bash_command("python src/assistant/main.py", project_root)
    assert reason is not None
    # The prefix check fires before path resolution — the reason text
    # points at the allowed-prefixes list.
    assert "tools/" in reason and "skills/" in reason


def test_python_absolute_path_escape_denied(project_root: Path) -> None:
    reason = check_bash_command("python /tmp/evil.py", project_root)
    assert reason is not None
    assert "escape" in reason or "tools/" in reason
