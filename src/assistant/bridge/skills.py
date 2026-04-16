from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_FRONT_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def _normalize_allowed_tools(raw: Any) -> list[str]:
    """Accept either a string `Bash` or a list `[Bash, Read]` in frontmatter."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


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
        "allowed_tools": _normalize_allowed_tools(meta_raw.get("allowed-tools")),
    }


# Module-level cache keyed by absolute skills_dir path.
_MANIFEST_CACHE: dict[Path, tuple[float, str]] = {}


def _manifest_mtime(skills_dir: Path) -> float:
    """Max mtime across skills_dir itself and every SKILL.md inside it.

    A directory mtime on APFS does NOT bump on in-place SKILL.md edits -- the
    explicit `max` across files is load-bearing for cache invalidation.
    """
    mtimes = [skills_dir.stat().st_mtime]
    for skill_md in skills_dir.glob("*/SKILL.md"):
        mtimes.append(skill_md.stat().st_mtime)
    return max(mtimes)


def build_manifest(skills_dir: Path) -> str:
    """Return a markdown-list manifest of discovered skills, mtime-cached.

    Phase-3 skill-installer SHOULD:

    1. Write/replace `<skills_dir>/<name>/SKILL.md` via atomic rename.
    2. Call `os.utime(skills_dir, None)` so a containing-dir mtime bump
       guarantees invalidation even on filesystems with second-level
       granularity (HFS+, FAT32) or where a child mtime change does not
       propagate to the parent.
    3. Call `invalidate_manifest_cache()` for an immediate refresh; otherwise
       the next request rebuilds lazily once the mtime check fires.
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


def invalidate_manifest_cache() -> None:
    """Drop the module-level manifest cache.

    Call from phase-3 skill-installer after add / remove / replace so the
    next `build_manifest()` rebuilds without waiting for mtime detection.
    """
    _MANIFEST_CACHE.clear()


def touch_skills_dir(skills_dir: Path) -> None:
    """Bump `skills_dir` mtime so `_manifest_mtime` picks up the change.

    Useful from phase-3 skill-installer when individual file mtimes alone may
    not propagate (FS granularity, atomic-rename semantics on some FSes).
    """
    if skills_dir.exists():
        os.utime(skills_dir, None)


# Backwards-compatible alias for tests written against the original name.
invalidate_cache = invalidate_manifest_cache
