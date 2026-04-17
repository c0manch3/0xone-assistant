"""B-3 + H-3: sha256_of_tree is deterministic, skips .git/__pycache__/etc.,
and is symlink-safe."""

from __future__ import annotations

import hashlib
from pathlib import Path

from _lib.validate import sha256_of_tree


def _write(path: Path, content: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_sha256_of_tree_idempotent(tmp_path: Path) -> None:
    _write(tmp_path / "SKILL.md", b"---\nname: x\ndescription: y\n---\n")
    _write(tmp_path / "scripts" / "a.py", b"print(1)\n")
    h1 = sha256_of_tree(tmp_path)
    h2 = sha256_of_tree(tmp_path)
    assert h1 == h2
    assert len(h1) == 64


def test_sha256_of_tree_skips_dot_git_and_pycache(tmp_path: Path) -> None:
    _write(tmp_path / "SKILL.md", b"a")
    baseline = sha256_of_tree(tmp_path)
    # Pollute the tree with exactly the directories `_git_clone` leaves
    # behind and the CPython toolchain drops in.
    _write(tmp_path / ".git" / "HEAD", b"ref: abc")
    _write(tmp_path / ".git" / "packed-refs", b"xyz")
    _write(tmp_path / "__pycache__" / "a.cpython-312.pyc", b"\x00\x00")
    _write(tmp_path / ".ruff_cache" / "x", b"r")
    _write(tmp_path / ".mypy_cache" / "x", b"m")
    _write(tmp_path / ".pytest_cache" / "x", b"p")
    _write(tmp_path / ".DS_Store", b"ds")
    assert sha256_of_tree(tmp_path) == baseline


def test_sha256_of_tree_mutation_flips_digest(tmp_path: Path) -> None:
    _write(tmp_path / "SKILL.md", b"---\nname: x\ndescription: y\n---\n")
    h1 = sha256_of_tree(tmp_path)
    _write(tmp_path / "SKILL.md", b"---\nname: x\ndescription: y\n---\n# toctou\n")
    h2 = sha256_of_tree(tmp_path)
    assert h1 != h2


def test_sha256_of_tree_skips_symlinks(tmp_path: Path) -> None:
    _write(tmp_path / "SKILL.md", b"a")
    # Symlink whose target lives outside the tree. If the hasher followed,
    # the digest would include `/etc/hostname` content and differ between
    # machines; the `not p.is_symlink()` guard keeps hashing stable.
    (tmp_path / "link").symlink_to("/etc/hostname")
    h1 = sha256_of_tree(tmp_path)
    # Swap the symlink target; digest must not move.
    (tmp_path / "link").unlink()
    (tmp_path / "link").symlink_to("/etc/passwd")
    h2 = sha256_of_tree(tmp_path)
    assert h1 == h2


def test_sha256_of_tree_changes_on_file_rename(tmp_path: Path) -> None:
    _write(tmp_path / "a.md", b"x")
    h1 = sha256_of_tree(tmp_path)
    (tmp_path / "a.md").rename(tmp_path / "b.md")
    h2 = sha256_of_tree(tmp_path)
    # Relative paths are hashed into the digest — rename must flip it.
    assert h1 != h2


def test_sha256_of_tree_format_is_sha256(tmp_path: Path) -> None:
    _write(tmp_path / "only.txt", b"")
    h = sha256_of_tree(tmp_path)
    assert all(c in "0123456789abcdef" for c in h)
    assert len(h) == hashlib.sha256().digest_size * 2
