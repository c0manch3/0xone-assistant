"""Phase 8 §2.5 — boot-time stale-lock cleanup.

A SIGKILL'd daemon mid-``git commit`` leaves
``<vault>/.git/index.lock`` on disk; the next sync cycle hangs
indefinitely on ``fatal: Unable to create '.git/index.lock': File
exists``. Mirroring the phase-6a ``_boot_sweep_uploads`` pattern,
:func:`_cleanup_stale_vault_locks` runs at the top of
:meth:`assistant.main.Daemon.start` (BEFORE the loop spawn) so the
first tick always sees a clean working tree.

The 60s mtime threshold is generous — a healthy ``git commit`` releases
the index lock in milliseconds, so any lock present at boot is by
definition stale. Errors during removal are logged + swallowed:
``startup_check`` is independently authoritative on whether vault sync
runs at all.
"""

from __future__ import annotations

import time
from pathlib import Path

from assistant.logger import get_logger

log = get_logger("vault_sync.boot")

_STALE_AGE_S = 60.0


def _cleanup_stale_vault_locks(vault_dir: Path) -> None:
    """Remove ``<vault>/.git/index.lock`` and any
    ``<vault>/.git/refs/**/*.lock`` whose mtime is older than 60s.

    Best-effort: a non-existent ``vault_dir`` (fresh deploy that hasn't
    been bootstrapped yet) is a no-op; OS errors during ``unlink`` are
    logged and swallowed so boot can still proceed.
    """
    if not vault_dir.exists():
        log.debug(
            "vault_sync_stale_lock_skip_missing_dir",
            vault_dir=str(vault_dir),
        )
        return
    git_dir = vault_dir / ".git"
    if not git_dir.exists():
        log.debug(
            "vault_sync_stale_lock_skip_no_git",
            vault_dir=str(vault_dir),
        )
        return

    now = time.time()
    cleared = 0

    candidates: list[Path] = [git_dir / "index.lock"]
    refs_dir = git_dir / "refs"
    if refs_dir.exists():
        try:
            candidates.extend(refs_dir.rglob("*.lock"))
        except OSError as exc:
            log.warning(
                "vault_sync_stale_lock_walk_error",
                refs_dir=str(refs_dir),
                error=repr(exc),
            )

    for cand in candidates:
        try:
            if not cand.exists():
                continue
            mtime = cand.stat().st_mtime
            if now - mtime <= _STALE_AGE_S:
                continue
            cand.unlink(missing_ok=True)
            cleared += 1
            log.info(
                "vault_sync_stale_index_lock_cleared",
                path=str(cand),
                age_s=int(now - mtime),
            )
        except OSError as exc:
            log.warning(
                "vault_sync_stale_lock_unlink_error",
                path=str(cand),
                error=repr(exc),
            )

    if cleared:
        log.info("vault_sync_stale_locks_done", cleared=cleared)
