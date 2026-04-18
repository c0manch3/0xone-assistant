from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from assistant.logger import get_logger

log = get_logger("bridge.skills")

_FRONT_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def _normalize_allowed_tools(raw: Any) -> list[str] | None:
    """Three-way classification of the `allowed-tools` frontmatter value.

    Phase 3 (B-1): the loader must distinguish three author intents:

    * field missing / `None` / malformed  → `None` (sentinel "permissive default");
      `_build_options` picks the global baseline and logs `skill_permissive_default`.
    * scalar string (`allowed-tools: Bash`) → `["Bash"]`.
    * list (`allowed-tools: [Bash, Read]`)  → `[str(x) for x in raw]`;
      **empty list `[]`** is a legitimate (though phase-3-unenforced) lockdown.

    The return type is `list[str] | None`; phase 4 will merge per-skill sets
    into `_build_options`. Phase 3 itself only log-differentiates between
    the "missing" (warn `skill_permissive_default`) and "empty list"
    (warn `skill_lockdown_not_enforced`) cases — the SDK still sees the
    global baseline either way.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return None


def parse_skill(path: Path) -> dict[str, Any]:
    """Parse the YAML frontmatter of a SKILL.md. Returns {} on missing block."""
    text = path.read_text(encoding="utf-8")
    match = _FRONT_RE.match(text)
    if not match:
        return {}
    meta_raw = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta_raw, dict):
        return {}
    # Note: `meta_raw.get("allowed-tools")` returns None both when the key is
    # absent AND when the key is explicitly `allowed-tools:` (null). Both
    # cases map to the permissive sentinel — that's the intended semantics.
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

    Phase-3 invalidation contract — the skill_installer:

    1. Writes/replaces `<skills_dir>/<name>/SKILL.md` via atomic rename.
    2. Touches `<data_dir>/run/skills.dirty`; the bridge's
       `_check_skills_sentinel` calls `invalidate_manifest_cache()` and
       `touch_skills_dir(skills_dir)` on the next turn.
    3. The `touch_skills_dir` bump is load-bearing on filesystems with
       second-level `stat.st_mtime` granularity (HFS+, FAT32) or where a
       child mtime change does not propagate to the parent.

    PostToolUse Write/Edit under `skills/` or `tools/` also touches the
    sentinel — the model never has to invoke the installer's own
    invalidation flow explicitly.
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
        # Phase-3 B-6 telemetry. Phase 3 uses the global baseline for every
        # skill regardless of frontmatter; we emit log events so operators
        # can see author intent and so phase 4 can safely start gating.
        tools = meta.get("allowed_tools")
        if tools is None:
            log.warning(
                "skill_permissive_default",
                skill_name=meta["name"],
                reason="allowed-tools missing in SKILL.md",
            )
        elif tools == []:
            log.warning(
                "skill_lockdown_not_enforced",
                skill_name=meta["name"],
                reason="phase 3 uses global baseline; per-skill gating arrives in phase 4",
            )
        entries.append(f"- **{meta['name']}** — {desc}")

    manifest = "\n".join(entries) if entries else "(no skills registered yet)"
    _MANIFEST_CACHE[skills_dir] = (mtime, manifest)
    return manifest


def invalidate_manifest_cache() -> None:
    """Drop the module-level manifest cache.

    Call from phase-3 skill_installer after add / remove / replace so the
    next `build_manifest()` rebuilds without waiting for mtime detection.
    """
    _MANIFEST_CACHE.clear()


def touch_skills_dir(skills_dir: Path) -> None:
    """Bump `skills_dir` mtime so `_manifest_mtime` picks up the change.

    Useful from phase-3 skill_installer when individual file mtimes alone may
    not propagate (FS granularity, atomic-rename semantics on some FSes).
    """
    if skills_dir.exists():
        os.utime(skills_dir, None)


# Backwards-compatible alias for tests written against the original name.
invalidate_cache = invalidate_manifest_cache
