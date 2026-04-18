"""Canonical path validation + vault scan excludes.

Stdlib-only. Shared by `cmd_write`, `cmd_read`, `cmd_delete`, `cmd_list`,
`cmd_reindex`.
"""

from __future__ import annotations

from pathlib import Path

# G2: directories / files that are inside the vault but NOT notes. Scans
# (`list`, `reindex`, `rglob`) must skip them so the model is never shown
# Obsidian plugin state or our atomic-write staging scratch.
_VAULT_SCAN_EXCLUDES: frozenset[str] = frozenset(
    {
        ".obsidian",  # Obsidian desktop metadata
        ".tmp",  # our atomic-write staging
        ".git",  # if the operator git-init'd the vault manually
        ".trash",  # Obsidian trashed notes
        "__pycache__",
        ".DS_Store",
    }
)


class PathValidationError(ValueError):
    """Raised when a vault-relative path fails validation."""


def validate_rel_path(rel_path: str) -> Path:
    """Reject absolute / parent-segment / non-`.md` paths.

    Returns a `Path` that is guaranteed relative, dotdot-free, and suffixed
    with `.md`. Callers are responsible for joining to the vault root.
    """
    if not rel_path or rel_path.strip() != rel_path:
        raise PathValidationError("path must not be empty or have surrounding whitespace")
    p = Path(rel_path)
    if p.is_absolute():
        raise PathValidationError("path must be relative to the vault")
    # A `.parts` containing `..` is impossible to resolve safely within
    # the vault. Reject even nested patterns like `inbox/../../etc`.
    if any(part == ".." for part in p.parts):
        raise PathValidationError("path must not contain '..'")
    if any(part == "." for part in p.parts):
        raise PathValidationError("path must not contain '.' segments")
    if not rel_path.endswith(".md"):
        raise PathValidationError("path must end with '.md'")
    return p


def should_skip_vault_path(path: Path, vault_root: Path) -> bool:
    """Return True if any segment of `path` relative to `vault_root` is excluded."""
    try:
        rel = path.relative_to(vault_root)
    except ValueError:
        return True
    return any(part in _VAULT_SCAN_EXCLUDES for part in rel.parts)
