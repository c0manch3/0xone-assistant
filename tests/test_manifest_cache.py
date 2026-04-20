from __future__ import annotations

import os
import time
from pathlib import Path

from assistant.bridge import skills as skills_mod
from assistant.bridge.skills import build_manifest


def _reset_cache() -> None:
    skills_mod._MANIFEST_CACHE.clear()


def _write_skill(root: Path, name: str, description: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    path = skill / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\nallowed-tools: [Bash]\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return path


def test_builds_and_caches(tmp_path: Path) -> None:
    _reset_cache()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "alpha", "Does alpha things")

    first = build_manifest(skills_dir)
    assert "alpha" in first
    # Second call — same cache key, same string object should come back.
    second = build_manifest(skills_dir)
    assert second is first


def test_invalidates_on_skill_mtime_change(tmp_path: Path) -> None:
    _reset_cache()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    path = _write_skill(skills_dir, "alpha", "Does alpha things")
    first = build_manifest(skills_dir)
    assert "alpha" in first

    # Tick the skill's mtime forward so the cache key changes.
    future = time.time() + 10
    os.utime(path, (future, future))
    path.write_text(
        "---\nname: alpha\ndescription: New description\nallowed-tools: [Bash]\n---\n",
        encoding="utf-8",
    )
    os.utime(path, (future, future))

    second = build_manifest(skills_dir)
    assert "New description" in second


def test_invalidates_on_file_count_change(tmp_path: Path) -> None:
    """S5 edge case: a deleted SKILL.md replaced by a new file with
    exactly the same max mtime would fool a pure-mtime cache. The
    file-count component of the cache key defends against that."""
    _reset_cache()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "alpha", "Desc A")
    first = build_manifest(skills_dir)
    assert "alpha" in first

    _write_skill(skills_dir, "beta", "Desc B")

    second = build_manifest(skills_dir)
    assert "alpha" in second
    assert "beta" in second


def test_missing_skills_dir() -> None:
    _reset_cache()
    assert build_manifest(Path("/nonexistent/path/skills")) == ("(skills directory missing)")


def test_skill_without_description_filtered(tmp_path: Path) -> None:
    _reset_cache()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    # Missing description
    skill = skills_dir / "noop"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: noop\nallowed-tools: []\n---\n", encoding="utf-8")
    manifest = build_manifest(skills_dir)
    assert "noop" not in manifest
    assert manifest == "(no skills registered yet)"
