from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path
from typing import Any

import yaml

from assistant.logger import get_logger

log = get_logger("bridge.skills")

_FRONT_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

# Cache key: (max-mtime, file-count, manifest-text).
_MANIFEST_CACHE: dict[Path, tuple[float, int, str]] = {}


def _normalize_allowed_tools(raw: Any) -> list[str] | None:
    """Three-way result:

    missing / None / malformed  -> None  (permissive default sentinel)
    scalar str                  -> [str(raw)]
    list                        -> [str(x) for x in raw]
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return None


def parse_skill(path: Path) -> dict[str, Any]:
    """Parse a SKILL.md frontmatter block.

    Returns an empty dict if the file has no frontmatter — caller filters
    on a non-empty ``description``.

    Phase 3 addition: warn when ``allowed-tools`` is missing (permissive
    default applies — global baseline) or empty (lockdown requested but
    not enforced in phase 3; see NH-8 in ``plan/phase3/implementation.md``).
    """
    text = path.read_text(encoding="utf-8")
    m = _FRONT_RE.match(text)
    if not m:
        return {}
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        return {}
    name = meta.get("name", path.parent.name)
    description = (meta.get("description") or "").strip()
    allowed = _normalize_allowed_tools(meta.get("allowed-tools"))
    if allowed is None:
        log.warning(
            "skill_permissive_default",
            skill_name=name,
            reason="allowed-tools missing in SKILL.md; global baseline applies",
        )
    elif allowed == []:
        log.warning(
            "skill_lockdown_not_enforced",
            skill_name=name,
            reason="phase 3 uses global baseline; per-skill gating is phase 4",
        )
    return {
        "name": name,
        "description": description,
        "allowed_tools": allowed,
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


def invalidate_manifest_cache() -> None:
    """Drop the whole manifest cache dict.

    Called from ``ClaudeBridge._check_skills_sentinel`` when
    ``data/run/skills.dirty`` is detected. ``dict.clear`` is atomic under
    the GIL on CPython; the daemon's single-event-loop model gives us
    even stronger guarantees.
    """
    _MANIFEST_CACHE.clear()


def touch_skills_dir(skills_dir: Path) -> None:
    """Bump ``mtime`` on ``skills_dir`` so the next ``_manifest_cache_key``
    returns a strictly higher max value — forcing a rebuild even if the
    cache dict was NOT cleared."""
    with contextlib.suppress(OSError):
        os.utime(skills_dir, None)
