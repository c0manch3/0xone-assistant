from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_FRONT_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

# Cache key: (max-mtime, file-count, manifest-text).
_MANIFEST_CACHE: dict[Path, tuple[float, int, str]] = {}


def parse_skill(path: Path) -> dict[str, Any]:
    """Parse a SKILL.md frontmatter block.

    Returns an empty dict if the file has no frontmatter — caller filters
    on a non-empty ``description``.
    """
    text = path.read_text(encoding="utf-8")
    m = _FRONT_RE.match(text)
    if not m:
        return {}
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        return {}
    return {
        "name": meta.get("name", path.parent.name),
        "description": (meta.get("description") or "").strip(),
        "allowed_tools": meta.get("allowed-tools", []),
    }


def _manifest_cache_key(skills_dir: Path) -> tuple[float, int]:
    """Return a tuple of ``(max-mtime, file-count)``.

    APFS directory mtime does NOT change on in-place edit of a child file —
    it only changes when the directory listing changes (add/remove). We
    therefore take the max over the directory mtime AND every SKILL.md
    mtime, and include the file-count as a second component so that a
    delete+add sequence with a coincidentally-equal max mtime still
    invalidates (S5 edge case).
    """
    paths = sorted(skills_dir.glob("*/SKILL.md"))
    mtimes = [skills_dir.stat().st_mtime]
    mtimes.extend(p.stat().st_mtime for p in paths)
    return (max(mtimes), len(paths))


def build_manifest(skills_dir: Path) -> str:
    """Render the skills manifest used in the system prompt.

    Cached on ``(mtime-max, file-count)`` to avoid re-parsing every
    SKILL.md on each turn. Safe to call on a cold cache — returns
    a placeholder if the directory is missing or empty.
    """
    if not skills_dir.exists():
        return "(skills directory missing)"
    mtime, count = _manifest_cache_key(skills_dir)
    cached = _MANIFEST_CACHE.get(skills_dir)
    if cached and cached[0] == mtime and cached[1] == count:
        return cached[2]
    entries: list[str] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        meta = parse_skill(skill_md)
        if not meta.get("description"):
            continue
        entries.append(f"- **{meta['name']}** — {meta['description']}")
    manifest = "\n".join(entries) if entries else "(no skills registered yet)"
    _MANIFEST_CACHE[skills_dir] = (mtime, count, manifest)
    return manifest
