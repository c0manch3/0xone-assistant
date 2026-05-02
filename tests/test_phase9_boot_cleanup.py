"""Phase 9 §2.2 + W1-MED-4 — boot-time stale-artefact cleanup tests.

  - Old final artefacts (mtime > cleanup_threshold_s) → unlinked.
  - Young final artefacts → preserved.
  - ``.staging/`` files → UNCONDITIONAL wipe regardless of age.
  - Missing dir → no-op (no error).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from assistant.render_doc.boot import _cleanup_stale_artefacts


def test_missing_dir_is_noop(tmp_path: Path) -> None:
    _cleanup_stale_artefacts(tmp_path / "does-not-exist")  # must not raise


def test_old_final_artefact_unlinked(tmp_path: Path) -> None:
    art = tmp_path / "out.pdf"
    art.write_bytes(b"%PDF-1.4\n")
    # Backdate mtime by 25 hours.
    old = time.time() - 25 * 3600
    os.utime(art, (old, old))
    _cleanup_stale_artefacts(tmp_path, cleanup_threshold_s=86400)
    assert not art.exists()


def test_young_final_artefact_preserved(tmp_path: Path) -> None:
    art = tmp_path / "out.pdf"
    art.write_bytes(b"%PDF-1.4\n")
    _cleanup_stale_artefacts(tmp_path, cleanup_threshold_s=86400)
    assert art.exists()


def test_staging_dir_files_unconditionally_wiped(tmp_path: Path) -> None:
    """W1-MED-4: staging files are by definition orphans (a healthy
    daemon cleans them in finally), wipe regardless of age."""
    staging = tmp_path / ".staging"
    staging.mkdir()
    young = staging / "fresh.md"
    young.write_text("just made")
    older = staging / "old.html"
    older.write_text("from earlier crash")
    old = time.time() - 60
    os.utime(older, (old, old))
    _cleanup_stale_artefacts(tmp_path)
    # Both files gone — staging is unconditional.
    assert not young.exists()
    assert not older.exists()


def test_only_files_under_artefact_dir_walked_not_subdirs(
    tmp_path: Path,
) -> None:
    """Sweeper does NOT recurse into arbitrary subdirs (only the top-
    level + ``.staging/``)."""
    nested = tmp_path / "nested-junk"
    nested.mkdir()
    nested_file = nested / "file.pdf"
    nested_file.write_bytes(b"x")
    old = time.time() - 25 * 3600
    os.utime(nested_file, (old, old))
    _cleanup_stale_artefacts(tmp_path, cleanup_threshold_s=86400)
    # Top-level cleanup does NOT recurse into nested-junk.
    assert nested_file.exists()
