"""B-6 semantics: `allowed-tools` sentinel vs empty-list vs scalar vs list.

Phase 3 does NOT yet gate per-skill; `_build_options` keeps the global
baseline for every skill. The loader only *differentiates* the four cases
so phase 4 can gate without another data migration, and so operators get
a warning log that surfaces author intent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from assistant.bridge.skills import build_manifest, invalidate_cache, parse_skill


def _write_md(skills_dir: Path, name: str, frontmatter: str) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(f"---\n{frontmatter}\n---\n", encoding="utf-8")
    return md


@pytest.fixture(autouse=True)
def _structlog_capture_ready() -> None:
    """`capture_logs` requires the default processor chain. Tests that run
    after `setup_logging` (daemon) would otherwise hit a `ValueError`."""
    structlog.reset_defaults()


def test_missing_allowed_tools_is_sentinel(tmp_path: Path) -> None:
    md = _write_md(tmp_path / "skills", "echo", "name: echo\ndescription: test")
    meta = parse_skill(md)
    assert meta["allowed_tools"] is None


def test_scalar_allowed_tools_is_singleton_list(tmp_path: Path) -> None:
    md = _write_md(
        tmp_path / "skills", "echo", "name: echo\ndescription: test\nallowed-tools: Bash"
    )
    meta = parse_skill(md)
    assert meta["allowed_tools"] == ["Bash"]


def test_list_allowed_tools_kept(tmp_path: Path) -> None:
    md = _write_md(
        tmp_path / "skills",
        "echo",
        "name: echo\ndescription: test\nallowed-tools: [Bash, Read]",
    )
    meta = parse_skill(md)
    assert meta["allowed_tools"] == ["Bash", "Read"]


def test_empty_allowed_tools_parses_but_does_not_gate(tmp_path: Path) -> None:
    invalidate_cache()
    skills = tmp_path / "skills"
    _write_md(
        skills,
        "echo",
        "name: echo\ndescription: test\nallowed-tools: []",
    )
    meta = parse_skill(skills / "echo" / "SKILL.md")
    assert meta["allowed_tools"] == []
    with capture_logs() as cap:
        build_manifest(skills)
    assert any(e["event"] == "skill_lockdown_not_enforced" for e in cap), cap


def test_missing_allowed_tools_emits_permissive_warning(tmp_path: Path) -> None:
    invalidate_cache()
    skills = tmp_path / "skills"
    _write_md(skills, "echo", "name: echo\ndescription: test")
    with capture_logs() as cap:
        build_manifest(skills)
    assert any(e["event"] == "skill_permissive_default" for e in cap), cap


def test_malformed_allowed_tools_is_sentinel(tmp_path: Path) -> None:
    # Integer / dict values round-trip as None. Matches the "malformed"
    # branch of `_normalize_allowed_tools`.
    md = _write_md(
        tmp_path / "skills",
        "echo",
        "name: echo\ndescription: test\nallowed-tools: 42",
    )
    meta = parse_skill(md)
    assert meta["allowed_tools"] is None
