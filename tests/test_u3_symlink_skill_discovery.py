"""U3: skill discovery works via `.claude/skills -> ../skills` symlink.

Pure unit variant: verifies that our own `build_manifest` finds skills through
the symlink. The full end-to-end SDK round-trip is gated behind a real
`claude` CLI + OAuth and is executed only in manual smoke (§3.14 item 12).
"""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.bootstrap import ensure_skills_symlink
from assistant.bridge.skills import build_manifest, invalidate_cache


def test_manifest_via_symlink(tmp_path: Path) -> None:
    invalidate_cache()
    skills = tmp_path / "skills"
    (skills / "echo").mkdir(parents=True)
    (skills / "echo" / "SKILL.md").write_text(
        "---\nname: echo\ndescription: Echoes back.\nallowed-tools: [Bash]\n---\n",
        encoding="utf-8",
    )

    ensure_skills_symlink(tmp_path)
    link = tmp_path / ".claude" / "skills"
    assert link.is_symlink()

    # Build manifest through the symlink path — it must resolve and list `echo`.
    manifest = build_manifest(link)
    assert "**echo**" in manifest
    assert "Echoes back." in manifest
