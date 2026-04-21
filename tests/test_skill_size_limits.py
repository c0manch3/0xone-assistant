"""Skill bundle size-limit enforcement at validate_bundle level."""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_min_skill(dest: Path, name: str = "alpha") -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: x\n---\n",
        encoding="utf-8",
    )


def test_single_file_over_limit_rejected(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    b = tmp_path / "b"
    _write_min_skill(b)
    (b / "huge.bin").write_bytes(b"\x00" * (core.MAX_SINGLE_BYTES + 1))
    with pytest.raises(core.ValidationError, match="file too large"):
        core.validate_bundle(b)


def test_total_over_limit_rejected(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    b = tmp_path / "b"
    _write_min_skill(b)
    # Each file 1 MB; 11 files = 11 MB > 10 MB total limit.
    chunk = b"\x00" * (1024 * 1024)
    for i in range(11):
        (b / f"f{i}.bin").write_bytes(chunk)
    with pytest.raises(
        core.ValidationError, match=r"(bundle too large|file too large|too many files)"
    ):
        core.validate_bundle(b)


def test_within_limits_accepted(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    b = tmp_path / "b"
    _write_min_skill(b, "small")
    (b / "aux.txt").write_text("hello", encoding="utf-8")
    report = core.validate_bundle(b)
    assert report["name"] == "small"
    assert report["file_count"] == 2
