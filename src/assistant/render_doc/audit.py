"""Phase 9 audit log writer (W1-MED-1 + W2-MED-4 + Wave D D1).

JSONL-formatted append-only log at
``<data_dir>/run/render-doc-audit.jsonl``. Each ``render_doc`` @tool
invocation writes exactly one row. Schema:

    {
        "ts": "<iso8601 utc>",
        "format": "pdf" | "docx" | "xlsx",
        "result": "ok" | "failed" | "disabled",
        "filename": str,
        "bytes": int | null,
        "duration_ms": int,
        "error": str | null,
        "schema_version": 1
    }

Rotation policy: **date-stamped** with keep-last-N.

  - Before each append, ``stat`` the file.
  - If size exceeds ``audit_log_max_size_mb`` MB, ``os.replace`` the
    file to ``<path>.<YYYYMMDD-HHMMSS-mmm>`` (millisecond suffix added
    in fix-pack F12 to handle parallel-render collisions; an
    additional ``-<n>`` integer suffix is appended on the unlikely
    same-millisecond collision), then delete any rotated siblings
    beyond the ``keep_last_n`` most recent.
  - Open a fresh file at the original path and write the row.

Differs from phase-8 ``vault_sync/audit.py`` (single-step ``.1``
rotation) — render_doc audit alone gets date-stamped from day one
(LOW-2 + Q9 owner compromise; phase 8 invariants preserved).

W2-MED-4 truncation: ALL str-typed fields capped at 256 codepoints
via :func:`_truncate_str_fields` BEFORE JSON serialisation. Future
str field additions inherit the cap automatically. Full error stays
in structured logs (no truncation there).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_TRUNCATE_CHARS = 256
SCHEMA_VERSION = 1


def _truncate_str_fields(
    row: dict[str, Any], *, max_chars: int = DEFAULT_TRUNCATE_CHARS
) -> dict[str, Any]:
    """Cap every str-typed value in ``row`` at ``max_chars`` codepoints.

    Python ``str`` slicing is codepoint-based (R3.14 PASS) so this is
    correct under multi-byte UTF-8 encodings. Non-str values pass
    through unchanged.
    """
    return {
        k: (v[:max_chars] if isinstance(v, str) else v)
        for k, v in row.items()
    }


def _list_rotated_siblings(path: Path) -> list[Path]:
    """Return rotated audit files (``<path>.<YYYYMMDD-HHMMSS>``) in
    DESCENDING mtime order (newest first)."""
    parent = path.parent
    if not parent.exists():
        return []
    prefix = path.name + "."
    siblings: list[Path] = []
    for entry in parent.iterdir():
        if entry.is_file() and entry.name.startswith(prefix):
            # Reject the live file itself.
            if entry.name == path.name:
                continue
            siblings.append(entry)
    siblings.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return siblings


def write_audit_row(
    path: Path,
    row: dict[str, Any],
    *,
    max_size_bytes: int,
    keep_last_n: int = 5,
    truncate_chars: int = DEFAULT_TRUNCATE_CHARS,
) -> None:
    """Append ``row`` as a single JSONL line at ``path``.

    On size-trigger:
      1. Rotate ``path`` → ``<path>.<YYYYMMDD-HHMMSS>`` via
         :func:`os.replace` (atomic).
      2. Prune older rotated siblings beyond ``keep_last_n``.
      3. Open fresh file at ``path`` and write.

    All str-typed fields in ``row`` are pre-truncated to
    ``truncate_chars`` codepoints (W2-MED-4). The caller MAY include
    a ``schema_version`` key; if absent, :data:`SCHEMA_VERSION` is
    injected.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size >= max_size_bytes:
            # Date-stamped rotation with millisecond suffix.
            # Fix-pack F12 (W3-HIGH-2 / spec §3 D1 closure): two
            # rotations within the same SECOND under
            # ``render_max_concurrent=2`` would collide on the prior
            # ``%Y%m%d-%H%M%S`` stamp — ``os.replace`` would silently
            # overwrite the first rotated file (data loss for that
            # archive). Adding millisecond resolution makes the
            # collision window 1000x narrower; uniqueness is
            # additionally hardened by appending an integer suffix
            # when the millisecond stamp itself collides (extremely
            # unlikely under realistic audit cadence but defensive).
            now = dt.datetime.now(dt.UTC)
            stamp = now.strftime("%Y%m%d-%H%M%S-") + (
                f"{now.microsecond // 1000:03d}"
            )
            rotated = path.with_suffix(path.suffix + f".{stamp}")
            collision_idx = 0
            while rotated.exists():
                collision_idx += 1
                rotated = path.with_suffix(
                    path.suffix + f".{stamp}-{collision_idx}"
                )
            os.replace(path, rotated)
            # Prune older rotated siblings beyond keep_last_n.
            siblings = _list_rotated_siblings(path)
            for old in siblings[keep_last_n:]:
                # Best-effort prune; swallow OS errors so a single
                # locked / permission-denied file doesn't crash the
                # live audit append below.
                with contextlib.suppress(OSError):
                    old.unlink(missing_ok=True)
    if "schema_version" not in row:
        row = dict(row)
        row["schema_version"] = SCHEMA_VERSION
    row = _truncate_str_fields(row, max_chars=truncate_chars)
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
