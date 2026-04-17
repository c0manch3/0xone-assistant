"""Atomic install of a validated bundle into `skills/<name>/` + optional
`tools/<name>/` split.

All operations are same-FS (we stage inside `<project_root>/skills/` and
rename to `<project_root>/skills/<name>/` — POSIX `rename()` is atomic on
a single filesystem). The bundle's validator has already rejected all
symlinks; we still pass `symlinks=True` to `shutil.copytree` as
defence-in-depth (see gotcha #4).
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Any

from .validate import _should_hash


class InstallError(Exception):
    """Raised when the atomic copy cannot complete."""


def atomic_install(bundle: Path, report: dict[str, Any], project_root: Path) -> None:
    """Copy `bundle` into `<project_root>/skills/<name>/` atomically.

    If `bundle/tools/` exists, move it to `<project_root>/tools/<name>/`
    before the final rename. Both target paths must not already exist —
    we do NOT overwrite (caller decides whether to remove + retry).
    """
    name = report["name"]
    skills_dst = project_root / "skills" / name
    tools_dst = project_root / "tools" / name
    if skills_dst.exists():
        raise InstallError(f"skills/{name}/ already exists")
    if tools_dst.exists():
        raise InstallError(f"tools/{name}/ already exists")

    skills_dst.parent.mkdir(parents=True, exist_ok=True)
    stage = skills_dst.parent / f".tmp-{name}-{uuid.uuid4().hex}"

    # DO NOT change to symlinks=False — validator already rejected all
    # symlinks; symlinks=True is defence-in-depth against a TOCTOU swap
    # between validate and copy. See gotcha #4 in
    # plan/phase3/implementation.md.
    shutil.copytree(bundle, stage, symlinks=True)

    inner_tools = stage / "tools"
    tools_stage: Path | None = None
    if inner_tools.is_dir():
        tools_dst.parent.mkdir(parents=True, exist_ok=True)
        tools_stage = tools_dst.parent / f".tmp-{name}-{uuid.uuid4().hex}"
        shutil.move(str(inner_tools), str(tools_stage))

    try:
        stage.rename(skills_dst)
        if tools_stage is not None:
            tools_stage.rename(tools_dst)
    except OSError as exc:
        # Best-effort rollback — the second rename could have failed after
        # the first succeeded.
        if skills_dst.exists() and tools_stage is not None and not tools_dst.exists():
            shutil.rmtree(skills_dst, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)
        if tools_stage is not None:
            shutil.rmtree(tools_stage, ignore_errors=True)
        raise InstallError(f"atomic rename failed: {exc}") from exc


def diff_trees(old: Path, new: Path) -> list[str]:
    """Return a sorted list of `REMOVED/ADDED/CHANGED: <rel>` lines.

    Used for the TOCTOU `exit 7` stderr — lets the operator see which
    files changed between the preview-time bundle and the install-time
    re-fetch without having to shell into `<data_dir>/run/installer-cache/`.

    Memory bound: both trees have already passed `validate_bundle` which
    enforces `MAX_FILES=100` + `MAX_SINGLE_FILE=2 MB`, so the pair never
    reads more than ~400 MB in the worst case (both sides, both fully
    loaded via `read_bytes` one file at a time). In practice Anthropic
    bundles are ≤5.5 MB each (spike S1.c), so the real peak is <12 MB.
    Callers additionally cap the rendered diff at 50 lines.
    """

    def _digest(p: Path) -> str:
        h = hashlib.sha256()
        h.update(p.read_bytes())
        return h.hexdigest()

    def _tree_map(root: Path) -> dict[str, str]:
        if not root.is_dir():
            return {}
        return {
            p.relative_to(root).as_posix(): _digest(p)
            for p in root.rglob("*")
            if _should_hash(p, root)
        }

    a, b = _tree_map(old), _tree_map(new)
    lines: list[str] = []
    for rel in sorted(set(a) | set(b)):
        if rel in a and rel not in b:
            lines.append(f"REMOVED: {rel}")
        elif rel in b and rel not in a:
            lines.append(f"ADDED: {rel}")
        elif a[rel] != b[rel]:
            lines.append(f"CHANGED: {rel}")
    return lines
