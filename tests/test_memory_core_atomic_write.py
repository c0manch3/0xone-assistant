"""atomic_write tests — tmp cleanup on rename failure + tmp_dir creation."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.tools_sdk import _memory_core as core


def test_memory_atomic_write_happy(tmp_path: Path) -> None:
    tmp_dir = tmp_path / ".tmp"
    dest = tmp_path / "note.md"
    core.atomic_write(dest, "hello\n", tmp_dir=tmp_dir)
    assert dest.read_text(encoding="utf-8") == "hello\n"


def test_memory_atomic_write_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename failure → tmp cleaned up, dest absent."""
    tmp_dir = tmp_path / ".tmp"
    dest = tmp_path / "note.md"

    def bad_replace(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(core.os, "replace", bad_replace)
    with pytest.raises(OSError, match="simulated"):
        core.atomic_write(dest, "hello\n", tmp_dir=tmp_dir)
    assert not dest.exists()
    # tmp dir should exist but contain no leftover files.
    leftovers = list(tmp_dir.glob(".tmp-*"))
    assert leftovers == []


def test_atomic_write_tmp_dir_missing(tmp_path: Path) -> None:
    """M2.2: tmp_dir is created on demand rather than raising."""
    tmp_dir = tmp_path / "never-existed"
    dest = tmp_path / "note.md"
    core.atomic_write(dest, "x", tmp_dir=tmp_dir)
    assert tmp_dir.exists()
    assert dest.read_text(encoding="utf-8") == "x"
