"""Canonical media directory layout (phase 7).

Single source of truth for the four locations the media pipeline
touches:

    <data_dir>/media/inbox/            -- downloaded Telegram files
    <data_dir>/media/outbox/           -- tool-produced artefacts
    <data_dir>/run/render-stage/       -- transient body-file staging
                                         (tools/render_doc writes here
                                         via the file-hook allowlist;
                                         path-guarded by the bash hook
                                         to refuse writes outside this
                                         sub-tree).

Every entry is created with mode 0o700 (same as the vault and the
memory stage — see `Daemon._ensure_vault` for precedent). `ensure_media_dirs`
is the only write-side entry point; the three `*_dir` helpers are
pure path builders (no FS side-effects) so hooks, tests and the
sweeper can import them without creating empty directories as a
side-effect.

Pitfall #14: `media_sweeper_loop` must NOT run before
`ensure_media_dirs()` -- it would log spurious `FileNotFoundError`
on its first tick against the non-existing `inbox`/`outbox`.
`Daemon.start` enforces the ordering.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from assistant.logger import get_logger

log = get_logger("media.paths")


def inbox_dir(data_dir: Path) -> Path:
    """Return `<data_dir>/media/inbox` (no FS access)."""
    return data_dir / "media" / "inbox"


def outbox_dir(data_dir: Path) -> Path:
    """Return `<data_dir>/media/outbox` (no FS access)."""
    return data_dir / "media" / "outbox"


def stage_dir(data_dir: Path) -> Path:
    """Return `<data_dir>/run/render-stage` (no FS access).

    `tools/render_doc/main.py` expects its `--body-file` argument to
    resolve under this directory; the bash hook path-guard rejects
    anything else (detailed-plan §11).
    """
    return data_dir / "run" / "render-stage"


async def ensure_media_dirs(data_dir: Path) -> None:
    """Create `inbox`, `outbox`, and `render-stage` with mode 0o700.

    Idempotent — `mkdir(parents=True, exist_ok=True)` is safe across
    daemon restarts. We run `mkdir` inside `asyncio.to_thread` so a
    slow NFS / fuse backing store does NOT block the event loop's
    first tick (`Daemon.start` awaits this before spawning any bg
    task). The three directories are created serially (one blocking
    call dispatched to the default executor) because the cost is
    bounded by three `mkdir` syscalls; parallel dispatch would hurt
    more than help.

    On failure we log a warning and re-raise so `Daemon.start` can
    surface the problem via `_spawn_bg`'s supervisor; silently
    continuing would let the sweeper loop tick against missing
    directories and generate noise rather than a single startup
    failure.
    """

    def _mkdirs() -> None:
        for path in (inbox_dir(data_dir), outbox_dir(data_dir), stage_dir(data_dir)):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            # `mkdir(mode=0o700)` honours the mode only on creation; on
            # a pre-existing directory the mode argument is silently
            # ignored (POSIX semantics). A follow-up `chmod(0o700)`
            # tightens perms if an earlier run created the dir with
            # a loose umask. We don't log on "loose" here -- the
            # vault path already emits that warning; media is a lower
            # sensitivity surface (no PII written by the daemon
            # itself -- only Telegram payloads the user chose to
            # send).
            try:
                path.chmod(0o700)
            except OSError as exc:
                log.warning(
                    "media_dir_chmod_failed",
                    path=str(path),
                    error=repr(exc),
                )

    try:
        await asyncio.to_thread(_mkdirs)
    except OSError as exc:
        log.warning(
            "ensure_media_dirs_failed",
            data_dir=str(data_dir),
            error=repr(exc),
        )
        raise
