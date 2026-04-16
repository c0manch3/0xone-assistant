from __future__ import annotations

from pathlib import Path

from assistant.bridge.skills import build_manifest, invalidate_cache, parse_skill


def _write_skill(skills_dir: Path, name: str, description: str) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {description}\nallowed-tools: [Bash]\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return md


def test_parse_skill_frontmatter(tmp_path: Path) -> None:
    md = _write_skill(tmp_path, "ping", "Healthcheck skill.")
    meta = parse_skill(md)
    assert meta["name"] == "ping"
    assert meta["description"] == "Healthcheck skill."
    assert meta["allowed_tools"] == ["Bash"]


def test_parse_skill_missing_frontmatter(tmp_path: Path) -> None:
    md = tmp_path / "foo" / "SKILL.md"
    md.parent.mkdir()
    md.write_text("# No frontmatter here\n", encoding="utf-8")
    assert parse_skill(md) == {}


def test_build_manifest_lists_skills(tmp_path: Path) -> None:
    invalidate_cache()
    skills = tmp_path / "skills"
    _write_skill(skills, "ping", "Healthcheck.")
    _write_skill(skills, "memory", "Persistent notes.")
    manifest = build_manifest(skills)
    assert "- **ping** — Healthcheck." in manifest
    assert "- **memory** — Persistent notes." in manifest


def test_build_manifest_empty_dir(tmp_path: Path) -> None:
    invalidate_cache()
    skills = tmp_path / "skills"
    skills.mkdir()
    assert build_manifest(skills) == "(no skills registered yet)"
