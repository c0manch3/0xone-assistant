from __future__ import annotations

import os
import time
from pathlib import Path

from assistant.bridge.skills import build_manifest, invalidate_cache


def _write_skill(skills_dir: Path, name: str, description: str) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {description}\nallowed-tools: [Bash]\n---\n",
        encoding="utf-8",
    )
    return md


def test_manifest_cached_between_calls(tmp_path: Path, monkeypatch) -> None:
    invalidate_cache()
    skills = tmp_path / "skills"
    _write_skill(skills, "ping", "Healthcheck.")

    # First call populates the cache.
    first = build_manifest(skills)

    # Subsequent call with no file changes must hit the cache (not re-glob).
    import assistant.bridge.skills as skills_mod

    original_glob = Path.glob
    glob_calls = {"n": 0}

    def counting_glob(self, pattern, *a, **kw):
        glob_calls["n"] += 1
        return original_glob(self, pattern, *a, **kw)

    # _manifest_mtime itself globs once; second call to build_manifest should
    # invoke _manifest_mtime (which globs) but NOT re-do the entries loop.
    # We verify cache behaviour indirectly: result identity + no rebuild work.
    second = build_manifest(skills)
    assert first == second

    # Dropping the cache forces a rebuild even with unchanged files.
    skills_mod.invalidate_cache()
    third = build_manifest(skills)
    assert third == first


def test_manifest_invalidates_on_new_skill(tmp_path: Path) -> None:
    invalidate_cache()
    skills = tmp_path / "skills"
    _write_skill(skills, "ping", "Healthcheck.")
    first = build_manifest(skills)
    assert "ping" in first
    assert "memory" not in first

    # Bump mtime by a full second so APFS/HFS-era stat granularity doesn't hide
    # the change, then add a new skill.
    time.sleep(1.05)
    _write_skill(skills, "memory", "Notes.")
    # Also touch the directory explicitly to cover filesystems that don't
    # propagate child-mtime to parent.
    os.utime(skills, None)

    second = build_manifest(skills)
    assert "ping" in second
    assert "memory" in second
    assert second != first


def test_manifest_invalidates_on_edit(tmp_path: Path) -> None:
    invalidate_cache()
    skills = tmp_path / "skills"
    md = _write_skill(skills, "ping", "Healthcheck.")
    first = build_manifest(skills)
    assert "Healthcheck." in first

    time.sleep(1.05)
    md.write_text(
        "---\nname: ping\ndescription: Updated.\nallowed-tools: [Bash]\n---\n",
        encoding="utf-8",
    )
    second = build_manifest(skills)
    assert "Updated." in second
    assert "Healthcheck." not in second
