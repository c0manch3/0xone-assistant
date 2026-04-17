"""Must-fix #4: validator rejects hardlinks.

Hardlinks to files outside the bundle pass every phase-3 check a
symlink-aware validator relies on: `is_symlink() == False`, `is_file()
== True`, `resolve()` returns the path itself (no symbolic redirect to
follow). The only signal that survives is `stat().st_nlink > 1` — new
in `_reject_unsafe_paths`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from _lib.validate import ValidationError, validate_bundle


def _minimal_skill(tmp_path: Path) -> Path:
    (tmp_path / "SKILL.md").write_text("---\nname: ok\ndescription: ok\n---\n", encoding="utf-8")
    return tmp_path


def test_hardlink_inside_bundle_rejected(tmp_path: Path) -> None:
    _minimal_skill(tmp_path)
    target = tmp_path / "scripts"
    target.mkdir()
    (target / "legit.py").write_text("print(1)\n", encoding="utf-8")
    # Hardlink from `/etc/hostname` (a file that certainly has nlink=1 as
    # a filesystem entry, but creating an in-bundle hardlink immediately
    # bumps nlink to 2 on both sides — any nlink > 1 is the signal).
    os.link(target / "legit.py", target / "sneaky.py")
    with pytest.raises(ValidationError, match="hardlink not allowed"):
        validate_bundle(tmp_path)


def test_hardlink_to_external_file_rejected(tmp_path: Path) -> None:
    """The classic attack: a bundle carrying a hardlink to /etc/passwd
    bypasses `is_symlink()` and `is_file() inside bundle`; validator must
    catch it via the `st_nlink > 1` branch.
    """
    _minimal_skill(tmp_path)
    # Use /etc/hostname (harmless, always exists, nlink = 1) — after the
    # hardlink is created, both paths have nlink = 2.
    try:
        os.link("/etc/hostname", tmp_path / "smuggled")
    except OSError:  # pragma: no cover -- cross-device on some CI images
        pytest.skip("/etc/hostname not hardlink-compatible on this FS")
    with pytest.raises(ValidationError, match="hardlink not allowed"):
        validate_bundle(tmp_path)


def test_plain_regular_files_still_pass(tmp_path: Path) -> None:
    _minimal_skill(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "a.py").write_text("pass\n", encoding="utf-8")
    report = validate_bundle(tmp_path)
    assert report["name"] == "ok"


def test_symlink_still_rejected_after_rename(tmp_path: Path) -> None:
    """Sanity: the renamed `_reject_unsafe_paths` still handles symlinks."""
    _minimal_skill(tmp_path)
    (tmp_path / "link").symlink_to("/etc/hostname")
    with pytest.raises(ValidationError, match="symlink not allowed"):
        validate_bundle(tmp_path)
