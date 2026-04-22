"""validate_path tests — traversal, symlinks, suffix, MOC rejection."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.tools_sdk._memory_core import validate_path


def test_validate_path_happy(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "inbox").mkdir()
    out = validate_path("inbox/note.md", vault)
    assert out == (vault / "inbox" / "note.md").resolve()


def test_validate_path_escapes_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match="'\\.\\.'"):
        validate_path("../outside.md", vault)


def test_validate_path_symlink_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = tmp_path / "real.md"
    target.write_text("x", encoding="utf-8")
    link = vault / "link.md"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        validate_path("link.md", vault)


def test_validate_path_moc_underscore_rejected(tmp_path: Path) -> None:
    """H3: ``_*.md`` (Obsidian MOC) must be refused."""
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match="MOC"):
        validate_path("_index.md", vault)


def test_validate_path_non_md_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match=r"\.md"):
        validate_path("note.txt", vault)


def test_validate_path_absolute_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match="vault-relative"):
        validate_path("/etc/passwd", vault)


def test_validate_path_home_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match="vault-relative"):
        validate_path("~/secrets.md", vault)


def test_validate_path_empty_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match="non-empty"):
        validate_path("", vault)
