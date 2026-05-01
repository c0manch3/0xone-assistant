"""Phase 8 audit log writer (W2-H2).

JSONL-formatted append-only log at
``<data_dir>/run/vault-sync-audit.jsonl``. Each invocation of the
subsystem (cron tick or manual @tool) writes exactly one row with
schema:

    {
        "ts": "<iso8601 utc>",
        "reason": "scheduled" | "manual" | "boot",
        "result": "pushed" | "noop" | "rate_limited" |
                  "lock_contention" | "failed",
        "files_changed": int,
        "commit_sha": str | null,
        "error": str | null
    }

W2-H2 rotation: before each append, the writer ``stat`` s the file; if
size exceeds ``audit_log_max_size_mb`` MB, it ``os.rename`` s the file
to ``<path>.1`` (overwriting any prior ``.1`` — single-step rotation,
no chain) and opens a fresh file at the original path. Atomic via
rename; no log lines from the pre-rotation file are lost.

Sync (not async) on purpose: the write is a single small line to a
local SSD-backed JSONL file; running through ``asyncio.to_thread``
would add a thread hop more expensive than the write itself, mirroring
the same trade-off the daemon makes for ``/proc/self/status`` reads in
:meth:`assistant.main.Daemon._rss_observer`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_audit_row(
    path: Path,
    row: dict[str, Any],
    max_size_bytes: int,
) -> None:
    """Append ``row`` as a single JSONL line at ``path``.

    If the existing file size at ``path`` is at-or-above
    ``max_size_bytes``, perform single-step rotation:

      1. ``os.replace`` the current file to ``<path>.1`` (atomic;
         overwrites any prior ``.1``).
      2. Open a fresh file at ``path`` and write the row.

    Errors during rotation are propagated — the caller wraps the
    audit-log call in a ``try/except`` so a transient write failure
    cannot crash the whole vault sync pipeline (the consequence of a
    skipped audit row is acceptable; the consequence of an unhandled
    OSError escaping the loop is supervisor respawn).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size >= max_size_bytes:
            rotated = path.with_suffix(path.suffix + ".1")
            os.replace(path, rotated)
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
