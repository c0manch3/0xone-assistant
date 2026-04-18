"""Validator enforces MAX_FILES / MAX_TOTAL_SIZE / MAX_SINGLE_FILE."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.skill_installer._lib.validate import (
    MAX_FILES,
    MAX_SINGLE_FILE,
    MAX_TOTAL_SIZE,
    ValidationError,
    validate_bundle,
)


def _minimal(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("---\nname: foo\ndescription: ok\n---\n", encoding="utf-8")


def test_too_many_files_rejected(tmp_path: Path) -> None:
    _minimal(tmp_path)
    for i in range(MAX_FILES + 1):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="cap is"):
        validate_bundle(tmp_path)


def test_single_file_over_cap_rejected(tmp_path: Path) -> None:
    _minimal(tmp_path)
    (tmp_path / "big.bin").write_bytes(b"\x00" * (MAX_SINGLE_FILE + 1))
    with pytest.raises(ValidationError, match="file too large"):
        validate_bundle(tmp_path)


def test_total_size_cap_enforced(tmp_path: Path) -> None:
    _minimal(tmp_path)
    # Three files each just under the per-file cap but totalling over the
    # bundle cap. `(MAX_SINGLE_FILE * 3)` is well over MAX_TOTAL_SIZE.
    chunk = b"\x00" * (MAX_SINGLE_FILE - 16)  # under single-file cap
    needed_total = MAX_TOTAL_SIZE + len(chunk)  # push over total
    count = needed_total // len(chunk) + 1
    for i in range(count):
        (tmp_path / f"blob{i}.bin").write_bytes(chunk)
    with pytest.raises(ValidationError, match="total size"):
        validate_bundle(tmp_path)


def test_within_caps_accepted(tmp_path: Path) -> None:
    _minimal(tmp_path)
    (tmp_path / "small.txt").write_text("small", encoding="utf-8")
    report = validate_bundle(tmp_path)
    assert report["file_count"] == 2
    assert report["total_size"] > 0
