"""Media sub-package (phase 7).

Four disjoint concerns live here:

  * `paths` — canonical `<data_dir>/media/{inbox,outbox}` and
    `<data_dir>/run/render-stage` directory helpers (single source of
    truth so Daemon, sweeper, adapter and the CLI tools never disagree
    on where media lives).
  * `download` — size-capped streaming download of Telegram files via
    `aiogram.Bot.download_file` (pitfall #3; S-6 verified). Implements
    BOTH the pre-flight `file.file_size` check AND a streaming
    `_SizeCappedWriter` with `write()` + `flush()` so the cap holds
    even when the server lies about `file_size` (None or stale).
  * `sweeper` — background age-based + LRU retention enforcement
    (pitfall #14: must be spawned AFTER `ensure_media_dirs()`).
  * `artefacts` — canonical `ARTEFACT_RE` (v3 per S-2 corpus) that
    `adapters/dispatch_reply.py` uses to extract outbox paths from
    assistant-produced text.

All four modules are intentionally import-cheap (stdlib + aiogram for
the download module; no DB, no SDK). Callers import them individually.
"""

from __future__ import annotations
