"""Phase 9 §2.2 / W1-MED-4 — boot-time stale artefact cleanup.

Mirror of phase-8 ``_cleanup_stale_vault_locks``: runs at the top of
:meth:`assistant.main.Daemon.start` BEFORE :class:`RenderDocSubsystem`
is constructed. Walks both:

  - ``artefact_dir/`` — final artefacts. Removes files with
    ``mtime > cleanup_threshold_s`` (default 24h) — defence against
    crash mid-delivery where the in-flight ledger died with the
    daemon.
  - ``artefact_dir/.staging/`` — staging files (orphaned pandoc
    inputs / WeasyPrint intermediates from a SIGKILL'd daemon).
    UNCONDITIONAL wipe — staging files are by definition transient;
    a healthy run cleans them in ``finally``. MED-4 closure.

Best-effort: missing dirs are no-ops; OS errors during ``unlink`` are
logged and swallowed so boot can still proceed.
"""

from __future__ import annotations

import time
from pathlib import Path

from assistant.logger import get_logger

log = get_logger("render_doc.boot")


def _cleanup_stale_artefacts(
    artefact_dir: Path,
    *,
    cleanup_threshold_s: int = 86400,
) -> None:
    """Remove orphaned final artefacts (mtime-gated) + ALL staging
    files (unconditional).

    Args:
      artefact_dir: ``<data_dir>/artefacts/`` (or override).
      cleanup_threshold_s: Skip final artefacts younger than this
        many seconds (default 24h). Staging dir is unconditional —
        the threshold is ignored there.
    """
    if not artefact_dir.exists():
        log.debug(
            "render_doc_cleanup_skip_missing_dir",
            artefact_dir=str(artefact_dir),
        )
        return

    now = time.time()
    final_cleared = 0
    staging_cleared = 0

    # Final artefacts under artefact_dir/ (NOT recursive, NOT under
    # .staging/).
    try:
        entries = list(artefact_dir.iterdir())
    except OSError as exc:
        log.warning(
            "render_doc_cleanup_iterdir_failed",
            path=str(artefact_dir),
            error=repr(exc),
        )
        return

    staging_dir = artefact_dir / ".staging"

    for entry in entries:
        if entry == staging_dir:
            # Handled below.
            continue
        try:
            if not entry.is_file():
                continue
            mtime = entry.stat().st_mtime
            if now - mtime <= cleanup_threshold_s:
                continue
            entry.unlink(missing_ok=True)
            final_cleared += 1
        except OSError as exc:
            log.warning(
                "render_doc_cleanup_final_unlink_error",
                path=str(entry),
                error=repr(exc),
            )

    # Staging dir — UNCONDITIONAL wipe of every file inside.
    if staging_dir.exists() and staging_dir.is_dir():
        try:
            staging_entries = list(staging_dir.iterdir())
        except OSError as exc:
            log.warning(
                "render_doc_cleanup_staging_iterdir_failed",
                path=str(staging_dir),
                error=repr(exc),
            )
            staging_entries = []
        for entry in staging_entries:
            try:
                if entry.is_file() or entry.is_symlink():
                    entry.unlink(missing_ok=True)
                    staging_cleared += 1
            except OSError as exc:
                log.warning(
                    "render_doc_cleanup_staging_unlink_error",
                    path=str(entry),
                    error=repr(exc),
                )

    if final_cleared or staging_cleared:
        log.info(
            "render_doc_cleanup_done",
            final_cleared=final_cleared,
            staging_cleared=staging_cleared,
        )
