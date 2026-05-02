"""Phase 9 §2.2 + W2-MED-4 + Wave D — audit log writer tests.

Covers:
  - JSONL append shape (single line, schema_version=1).
  - Field truncation: filename + error capped at 256 codepoints
    (W2-MED-4 uniform truncation).
  - Date-stamped rotation triggers at size threshold.
  - keep_last_n prunes older rotated siblings.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from assistant.render_doc.audit import (
    SCHEMA_VERSION,
    _truncate_str_fields,
    write_audit_row,
)


def test_truncate_helper_caps_str_values() -> None:
    row = {
        "filename": "x" * 1000,
        "error": "y" * 1000,
        "bytes": 12345,
        "schema_version": 1,
    }
    out = _truncate_str_fields(row, max_chars=256)
    assert len(out["filename"]) == 256
    assert len(out["error"]) == 256
    assert out["bytes"] == 12345
    assert out["schema_version"] == 1


def test_write_appends_jsonl_with_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    row = {
        "ts": "2026-05-02T12:00:00+00:00",
        "format": "pdf",
        "result": "ok",
        "filename": "report.pdf",
        "bytes": 1024,
        "duration_ms": 250,
        "error": None,
    }
    write_audit_row(path, row, max_size_bytes=10_000)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    parsed = json.loads(text.strip())
    assert parsed["format"] == "pdf"
    assert parsed["schema_version"] == SCHEMA_VERSION


def test_field_truncation_uniform(tmp_path: Path) -> None:
    """W2-MED-4: filename + error both capped at 256 codepoints."""
    path = tmp_path / "audit.jsonl"
    row = {
        "ts": "2026-05-02T12:00:00+00:00",
        "format": "pdf",
        "result": "failed",
        "filename": "x" * 1000,
        "bytes": None,
        "duration_ms": 0,
        "error": "y" * 1000,
    }
    write_audit_row(path, row, max_size_bytes=10_000, truncate_chars=256)
    parsed = json.loads(path.read_text(encoding="utf-8").strip())
    assert len(parsed["filename"]) == 256
    assert len(parsed["error"]) == 256


def test_date_stamped_rotation_at_size_threshold(tmp_path: Path) -> None:
    """Rotation kicks in once file size exceeds the cap."""
    path = tmp_path / "audit.jsonl"
    row = {
        "ts": "2026-05-02T12:00:00+00:00",
        "format": "pdf",
        "result": "ok",
        "filename": "a.pdf",
        "bytes": 1,
        "duration_ms": 1,
        "error": None,
    }
    # Pre-create a large file BEFORE the next append to trigger rotation.
    path.write_text("x" * 1024, encoding="utf-8")
    write_audit_row(path, row, max_size_bytes=512)
    # Rotation must produce a sibling matching the date-stamp pattern.
    parent = path.parent
    siblings = [p for p in parent.iterdir() if p.name.startswith("audit.jsonl.")]
    assert len(siblings) == 1, (
        f"Expected exactly one rotated sibling, got: {siblings}"
    )
    # Live file holds only the new row.
    live = path.read_text(encoding="utf-8").strip()
    assert json.loads(live)["filename"] == "a.pdf"


def test_keep_last_n_prunes_older_siblings(tmp_path: Path) -> None:
    """``keep_last_n=3`` retains only the 3 most recent rotated
    siblings; rotation #6 deletes the oldest.

    Fix-pack F12: rotation stamps now have millisecond precision so
    the explicit 1.1s sleep that previously guarded against
    same-second collisions can be replaced with a small sleep that
    only widens the mtime gap (sort key) — the suffix itself is
    unique even within a single second.
    """
    path = tmp_path / "audit.jsonl"
    row = {
        "ts": "2026-05-02T12:00:00+00:00",
        "format": "pdf",
        "result": "ok",
        "filename": "a.pdf",
        "bytes": 1,
        "duration_ms": 1,
        "error": None,
    }
    for _i in range(6):
        path.write_text("x" * 1024, encoding="utf-8")
        write_audit_row(
            path,
            row,
            max_size_bytes=512,
            keep_last_n=3,
        )
        # Small sleep — millisecond stamps make 1s no longer required;
        # we just want monotonic mtime ordering for the sort.
        time.sleep(0.05)
    siblings = [
        p
        for p in path.parent.iterdir()
        if p.name.startswith("audit.jsonl.") and p.name != "audit.jsonl"
    ]
    assert len(siblings) <= 3, (
        f"keep_last_n=3 violated: {[p.name for p in siblings]}"
    )


def test_rotation_microsecond_stamp_avoids_same_second_collision(
    tmp_path: Path,
) -> None:
    """Fix-pack F12 (W3-HIGH-2 / spec §3 D1): two rotations within the
    same SECOND must produce DISTINCT rotated filenames — pre-fix-pack
    they'd collide on ``%Y%m%d-%H%M%S`` and ``os.replace`` would
    silently overwrite the first.
    """
    path = tmp_path / "audit.jsonl"
    row = {
        "ts": "2026-05-02T12:00:00+00:00",
        "format": "pdf",
        "result": "ok",
        "filename": "x.pdf",
        "bytes": 1,
        "duration_ms": 1,
        "error": None,
    }
    # Two rotations back-to-back inside the same wall-clock second.
    # Without millisecond precision, both would land on the same
    # ``audit.jsonl.<YYYYMMDD-HHMMSS>`` filename.
    for _ in range(2):
        path.write_text("x" * 1024, encoding="utf-8")
        write_audit_row(
            path,
            row,
            max_size_bytes=512,
            keep_last_n=10,
        )
    siblings = sorted(
        p.name
        for p in path.parent.iterdir()
        if p.name.startswith("audit.jsonl.")
        and p.name != "audit.jsonl"
    )
    assert len(siblings) == 2, (
        f"expected 2 distinct rotated siblings; got {siblings}"
    )
    # Sanity: stamp must include a millisecond component (digits AFTER
    # the HHMMSS section), e.g. ``audit.jsonl.20260502-120000-123``.
    for name in siblings:
        suffix = name.removeprefix("audit.jsonl.")
        # Either ``YYYYMMDD-HHMMSS-mmm`` or
        # ``YYYYMMDD-HHMMSS-mmm-<n>`` (collision-suffix branch).
        parts = suffix.split("-")
        assert len(parts) >= 3, (
            f"expected millisecond stamp; got suffix={suffix!r}"
        )
