"""Phase 6a — Daemon._boot_sweep_uploads unit tests.

Covers (devil H3 — UNCONDITIONAL sweep):
- nonexistent uploads_dir → no exception;
- empty uploads_dir → no-op;
- top-level orphans wiped unconditionally (no age check);
- ``.failed/`` retained, files older than 7 days pruned;
- ``.failed/`` files newer than 7 days kept;
- permission error on prune is logged + sweep continues;
- ``.failed/`` is a regular file (not a dir) → tolerated, sweep continues.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from assistant.main import _boot_sweep_uploads


def test_boot_sweep_nonexistent_uploads_dir_no_exception(tmp_path: Path) -> None:
    """``uploads_dir`` does not exist → graceful no-op."""
    target = tmp_path / "no_such_dir"
    assert not target.exists()
    _boot_sweep_uploads(target)  # no exception
    assert not target.exists()


def test_boot_sweep_empty_dir_is_noop(tmp_path: Path) -> None:
    target = tmp_path / "uploads"
    target.mkdir()
    _boot_sweep_uploads(target)
    assert target.exists()
    assert list(target.iterdir()) == []


def test_boot_sweep_wipes_top_level_orphans_unconditionally(tmp_path: Path) -> None:
    """All top-level files are wiped — devil H3 dropped the 1h-bound
    age check (open to crash-loop disk fill).
    """
    target = tmp_path / "uploads"
    target.mkdir()
    # Create three orphan files of various ages.
    f_now = target / "fresh__a.pdf"
    f_now.write_bytes(b"x")
    f_old = target / "uuid__b.docx"
    f_old.write_bytes(b"y")
    # Backdate one to test that even "old" files are wiped (the
    # ``.failed/`` 7d window is the only age-bound).
    old_time = time.time() - 30 * 86400
    os.utime(f_old, (old_time, old_time))

    _boot_sweep_uploads(target)

    assert not f_now.exists()
    assert not f_old.exists()


def test_boot_sweep_keeps_failed_dir(tmp_path: Path) -> None:
    """``.failed/`` subdir survives the sweep (only its contents are
    age-pruned).
    """
    target = tmp_path / "uploads"
    target.mkdir()
    failed = target / ".failed"
    failed.mkdir()
    _boot_sweep_uploads(target)
    assert failed.exists()
    assert failed.is_dir()


def test_boot_sweep_prunes_old_failed_entries(tmp_path: Path) -> None:
    """``.failed/`` files older than 7 days are deleted; newer ones kept."""
    target = tmp_path / "uploads"
    target.mkdir()
    failed = target / ".failed"
    failed.mkdir()

    fresh = failed / "fresh__a.pdf"
    fresh.write_bytes(b"x")
    old = failed / "old__b.pdf"
    old.write_bytes(b"y")
    backdate = time.time() - 8 * 86400
    os.utime(old, (backdate, backdate))

    _boot_sweep_uploads(target)

    assert fresh.exists()
    assert not old.exists()


def test_boot_sweep_keeps_recent_failed_entries(tmp_path: Path) -> None:
    """A ``.failed/`` file 6 days old is preserved (boundary case)."""
    target = tmp_path / "uploads"
    target.mkdir()
    failed = target / ".failed"
    failed.mkdir()
    p = failed / "kept__c.pdf"
    p.write_bytes(b"x")
    six_days_ago = time.time() - 6 * 86400
    os.utime(p, (six_days_ago, six_days_ago))

    _boot_sweep_uploads(target)

    assert p.exists()


def test_boot_sweep_tolerates_failed_being_a_file(tmp_path: Path) -> None:
    """``.failed`` exists as a file (corruption / dev mistake) → log
    + skip; the daemon must not crash on boot.
    """
    target = tmp_path / "uploads"
    target.mkdir()
    bad = target / ".failed"
    bad.write_bytes(b"oops, this should be a dir")

    # Should not raise.
    _boot_sweep_uploads(target)
    # The file is left in place — the next ExtractionError attempt to
    # mkdir a quarantine dir will surface a clear error then.
    assert bad.exists()


def test_boot_sweep_handles_unlink_oserror(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """If unlink raises OSError on a top-level orphan, the sweep logs
    and continues with the next entry.
    """
    target = tmp_path / "uploads"
    target.mkdir()
    bad = target / "uuid__bad.pdf"
    bad.write_bytes(b"x")
    good = target / "uuid__good.pdf"
    good.write_bytes(b"y")

    real_unlink = Path.unlink

    def selective_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == "uuid__bad.pdf":
            raise OSError("simulated permission denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", selective_unlink)

    _boot_sweep_uploads(target)

    # ``bad`` is still there (the OSError was caught + logged); ``good``
    # is gone.
    assert bad.exists()
    assert not good.exists()


def test_boot_sweep_skips_unexpected_subdir(tmp_path: Path) -> None:
    """Future-proof: an unexpected subdir under uploads is skipped, not
    recursed into. Phase 6b might land a peer subdir we don't know
    about yet.
    """
    target = tmp_path / "uploads"
    target.mkdir()
    weird = target / "future_subdir"
    weird.mkdir()
    inside = weird / "inside.txt"
    inside.write_bytes(b"x")

    _boot_sweep_uploads(target)

    # Subdir + contents preserved.
    assert weird.is_dir()
    assert inside.exists()
