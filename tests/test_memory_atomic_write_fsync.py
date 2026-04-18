"""Memory atomic_write: rename failure leaves no orphan tmp or target."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.memory._lib.vault import atomic_write


def test_rename_failure_cleans_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".tmp").mkdir()

    def boom(src: str, dst: str) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "rename", boom)

    with pytest.raises(OSError):
        atomic_write(vault, Path("inbox/a.md"), "content")

    # Target was never created.
    assert not (vault / "inbox" / "a.md").exists()
    # Tmp directory ended up empty — no orphan scratch.
    leftovers = list((vault / ".tmp").iterdir())
    assert leftovers == [], f"expected .tmp/ to be empty, got {leftovers}"


def test_happy_write_creates_file(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    path = atomic_write(vault, Path("inbox/a.md"), "hello")
    assert path.read_text(encoding="utf-8") == "hello"
    # Mode on the written file is whatever the fs gave us; the key
    # invariant is the tmp-dir is also empty after success.
    assert list((vault / ".tmp").iterdir()) == []
