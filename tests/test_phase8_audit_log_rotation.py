"""Phase 8 W2-H2 — audit log JSONL append + 10 MB single-step rotation.

AC#14:
- Below ``audit_log_max_size_mb`` MB → plain append.
- At-or-above the threshold → atomic rename to ``<path>.1`` then a
  fresh file is opened.
- The new file starts at 0 bytes (rotation is single-step, no chain).
"""

from __future__ import annotations

import json
from pathlib import Path

from assistant.vault_sync.audit import write_audit_row


def test_append_below_max_size(tmp_path: Path) -> None:
    """Three appends under the threshold → one growing file, no rotation."""
    p = tmp_path / "audit.jsonl"
    for i in range(3):
        write_audit_row(
            p,
            {"ts": "2026-04-28T12:00:00", "i": i},
            max_size_bytes=10 * 1024 * 1024,
        )
    rows = p.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3
    assert json.loads(rows[0])["i"] == 0
    assert json.loads(rows[2])["i"] == 2
    assert not (tmp_path / "audit.jsonl.1").exists()


def test_rotation_when_size_exceeds_max(tmp_path: Path) -> None:
    """File at threshold → next append rotates it to .1 and starts a
    fresh log."""
    p = tmp_path / "audit.jsonl"
    # Plant a "full" file (just past threshold).
    threshold = 1024  # 1 KB threshold for the test
    p.write_text("x" * (threshold + 100), encoding="utf-8")
    assert p.stat().st_size > threshold
    write_audit_row(
        p,
        {"ts": "2026-04-28T12:00:00", "after": "rotation"},
        max_size_bytes=threshold,
    )
    rotated = tmp_path / "audit.jsonl.1"
    assert rotated.exists()
    # The rotated file kept the original content (filler bytes).
    assert rotated.read_text(encoding="utf-8").startswith("x" * 100)
    # The new file holds exactly one row.
    new_lines = p.read_text(encoding="utf-8").splitlines()
    assert len(new_lines) == 1
    assert json.loads(new_lines[0])["after"] == "rotation"


def test_rotation_overwrites_prior_dot1(tmp_path: Path) -> None:
    """Single-step rotation: a prior ``.1`` is overwritten, no chain."""
    p = tmp_path / "audit.jsonl"
    rotated = tmp_path / "audit.jsonl.1"
    rotated.write_text("PRIOR-ROTATION-CONTENT", encoding="utf-8")
    threshold = 1024
    p.write_text("y" * (threshold + 100), encoding="utf-8")
    write_audit_row(
        p,
        {"new": True},
        max_size_bytes=threshold,
    )
    # Prior .1 content is gone; replaced by the freshly-rotated file.
    assert "PRIOR-ROTATION-CONTENT" not in rotated.read_text(
        encoding="utf-8"
    )
    assert "y" in rotated.read_text(encoding="utf-8")


def test_fresh_file_starts_at_zero_bytes(tmp_path: Path) -> None:
    """After rotation, the new file has exactly one line (0 bytes
    pre-write)."""
    p = tmp_path / "audit.jsonl"
    threshold = 1024
    p.write_text("z" * (threshold + 1), encoding="utf-8")
    write_audit_row(
        p,
        {"first_after_rotate": 1},
        max_size_bytes=threshold,
    )
    raw = p.read_text(encoding="utf-8")
    # Exactly one trailing newline + one JSON line.
    assert raw.count("\n") == 1
    parsed = json.loads(raw.strip())
    assert parsed == {"first_after_rotate": 1}
