"""Vault I/O: atomic write, read, list, delete, permission init.

Stdlib-only. `fcntl.flock` serialisation lives in `fts.py::vault_lock`;
this module only deals with the filesystem side.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from _memlib.paths import should_skip_vault_path


def ensure_vault(vault_dir: Path) -> list[str]:
    """Idempotent init of the vault root and its staging dir.

    Creates both with mode `0o700` (personal memory must not be
    world-readable). Does NOT chmod pre-existing directories — the operator
    may have deliberately loosened perms. Returns a list of warning
    messages (non-fatal) for the caller to emit on the structured logger.
    """
    warnings: list[str] = []
    vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        mode = vault_dir.stat().st_mode & 0o777
    except OSError:
        mode = 0
    if mode & 0o077:
        warnings.append(f"vault_dir_permissions_too_open: path={vault_dir}, mode={oct(mode)}")

    tmp = vault_dir / ".tmp"
    tmp.mkdir(exist_ok=True, mode=0o700)
    # Should-fix #9: `Path.mkdir(mode=...)` silently ignores the mode on
    # an existing dir, so a stage dir created under a lax umask (or left
    # over from phase 4 v1) could keep world-read/execute bits. The
    # staging dir is ours alone — unlike the vault root where we respect
    # operator intent — so chmod to 0o700 unconditionally. OSError from
    # chmod is logged as a warning but does not fail init.
    try:
        tmp_mode = tmp.stat().st_mode & 0o777
    except OSError:
        tmp_mode = 0o700
    if tmp_mode != 0o700:
        try:
            os.chmod(tmp, 0o700)
        except OSError as exc:
            warnings.append(
                f"vault_tmp_chmod_failed: path={tmp}, mode={oct(tmp_mode)}, error={exc}"
            )
    return warnings


def atomic_write(vault_dir: Path, rel_path: Path, content: str) -> Path:
    """Atomic create-or-replace for a note.

    Writes to `<vault>/.tmp/<random>.md`, `fsync`s the tmp file, then
    `os.rename`s it onto the target. `os.rename` is POSIX-atomic when
    src and dst sit on the same filesystem — we place `.tmp/` inside
    the vault root exactly for this guarantee (R-U4-3).
    """
    target = vault_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = vault_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Tempfile is created with mode 0o600 via tempfile.mkstemp (default),
    # matching the 0o700 vault dir.
    fd, tmp_name = tempfile.mkstemp(dir=str(tmp_dir), suffix=".md")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, target)
    except Exception:
        # Clean the tmp file on any failure before rename lands.
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def read_note(vault_dir: Path, rel_path: Path) -> str:
    """Read a note from disk (raises `FileNotFoundError` if missing)."""
    target = vault_dir / rel_path
    return target.read_text(encoding="utf-8")


def delete_note(vault_dir: Path, rel_path: Path) -> None:
    """Delete a note from disk (raises `FileNotFoundError` if missing)."""
    target = vault_dir / rel_path
    target.unlink()


def list_notes(vault_dir: Path, area: str | None = None) -> list[Path]:
    """Return vault-relative paths of every `.md` file honouring excludes.

    `area`, if given, filters to the immediate top-level directory
    (e.g. `area="inbox"` matches `inbox/a.md` but not `projects/x/a.md`).
    """
    if not vault_dir.exists():
        return []
    results: list[Path] = []
    for md in vault_dir.rglob("*.md"):
        if should_skip_vault_path(md, vault_dir):
            continue
        rel = md.relative_to(vault_dir)
        if area is not None and (not rel.parts or rel.parts[0] != area):
            continue
        results.append(rel)
    results.sort()
    return results
