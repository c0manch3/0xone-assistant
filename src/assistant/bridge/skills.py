from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_FRONT_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def parse_skill(path: Path) -> dict[str, Any]:
    """Parse the YAML frontmatter of a SKILL.md. Returns {} on missing block."""
    text = path.read_text(encoding="utf-8")
    match = _FRONT_RE.match(text)
    if not match:
        return {}
    meta_raw = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta_raw, dict):
        return {}
    return {
        "name": str(meta_raw.get("name", path.parent.name)),
        "description": str(meta_raw.get("description", "")).strip(),
        "allowed_tools": meta_raw.get("allowed-tools", []),
    }


# Module-level cache keyed by absolute skills_dir path.
_MANIFEST_CACHE: dict[Path, tuple[float, str]] = {}


def _manifest_mtime(skills_dir: Path) -> float:
    """Max mtime across skills_dir itself and every SKILL.md inside it.

    A directory mtime on APFS does NOT bump on in-place SKILL.md edits — the
    explicit `max` across files is load-bearing for cache invalidation.
    """
    mtimes = [skills_dir.stat().st_mtime]
    for skill_md in skills_dir.glob("*/SKILL.md"):
        mtimes.append(skill_md.stat().st_mtime)
    return max(mtimes)


def build_manifest(skills_dir: Path) -> str:
    """Return a markdown-list manifest of discovered skills, mtime-cached.

    Phase 3 skill-installer writes via atomic rename, which bumps the
    containing directory's mtime → cache is invalidated on next call.
    """
    if not skills_dir.exists():
        return "(skills directory not found)"

    mtime = _manifest_mtime(skills_dir)
    cached = _MANIFEST_CACHE.get(skills_dir)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    entries: list[str] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        meta = parse_skill(skill_md)
        desc = meta.get("description", "")
        if not desc:
            continue
        entries.append(f"- **{meta['name']}** — {desc}")

    manifest = "\n".join(entries) if entries else "(no skills registered yet)"
    _MANIFEST_CACHE[skills_dir] = (mtime, manifest)
    return manifest


def invalidate_cache() -> None:
    """Testing hook: drop the module-level manifest cache."""
    _MANIFEST_CACHE.clear()
