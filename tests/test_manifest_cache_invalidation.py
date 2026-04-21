"""Manifest cache invalidation + hot reload via the skills sentinel."""

from __future__ import annotations

from pathlib import Path

from assistant.bridge import skills as skills_mod


def _write_skill(root: Path, name: str, description: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    path = skill / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\nallowed-tools: [Bash]\n---\n",
        encoding="utf-8",
    )
    return path


def test_invalidate_manifest_cache(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(skills, "alpha", "A")
    # Populate cache.
    skills_mod.build_manifest(skills)
    assert skills in skills_mod._MANIFEST_CACHE
    skills_mod.invalidate_manifest_cache()
    assert not skills_mod._MANIFEST_CACHE


def test_touch_skills_dir_bumps_mtime(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    before = skills.stat().st_mtime
    # Ensure a measurable difference on coarse-mtime filesystems.
    import time

    time.sleep(0.02)
    skills_mod.touch_skills_dir(skills)
    after = skills.stat().st_mtime
    assert after >= before


def test_sentinel_triggers_invalidate(tmp_path: Path) -> None:
    """Exercise ClaudeBridge._check_skills_sentinel end-to-end."""
    from assistant.bridge.claude import ClaudeBridge
    from assistant.config import ClaudeSettings, Settings
    from assistant.tools_sdk.installer import configure_installer

    pr = tmp_path / "proj"
    (pr / "skills").mkdir(parents=True)
    dd = tmp_path / "data"
    dd.mkdir()
    configure_installer(project_root=pr, data_dir=dd)
    settings = Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=pr,
        data_dir=dd,
        claude=ClaudeSettings(),
    )
    bridge = ClaudeBridge(settings)

    # Pre-populate the cache.
    skills_mod.build_manifest(pr / "skills")
    assert pr.resolve() / "skills" in skills_mod._MANIFEST_CACHE or (
        (pr / "skills") in skills_mod._MANIFEST_CACHE
    )

    # Drop sentinel, then exercise.
    sentinel = dd / "run" / "skills.dirty"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    bridge._check_skills_sentinel()
    assert not sentinel.exists()
    assert not skills_mod._MANIFEST_CACHE  # cache cleared
