"""Phase 6c: direct-callable transcript wrapper around the phase-4
memory subsystem.

Audio handlers call :func:`save_transcript` to persist a Whisper output
as a vault note WITHOUT routing through the Claude SDK. This keeps the
transcript a deterministic side-effect of receiving audio, rather than
a stochastic decision the model might skip, rewrite, or forget.

Invariants preserved (see :func:`assistant.tools_sdk.memory.memory_write`):

- ``created`` stamped server-side at first write; never owner-controlled.
  On overwrite the original ``created`` is preserved.
- ``updated`` always now-time on every write.
- ``area`` validated as a top-level path segment matching ``_AREA_RE``.
- ``body`` sanitised via ``core.sanitize_body`` BUT — per spec H7 — a
  transcript matching the sentinel pattern is REPLACED, not rejected.
  Owner content cannot be lost.
- ``frontmatter`` serialised via ``core.serialize_frontmatter``.
- ``tags_json`` populated from the ``tags`` list.
- ``write_note_tx`` holds the same blocking ``vault_lock`` as the
  @tool layer — concurrent voice-saves serialise.

The function is **stateless**: it takes ``vault_dir`` + ``index_db_path``
as kwargs every call, with no module-level configure step. Each test
constructs its own ``tmp_path`` vault and passes the paths positionally.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

import structlog
import yaml

from assistant.tools_sdk import _memory_core as core

log = structlog.get_logger(__name__)

# Mirrors the phase-4 area regex — keep them in lock-step.
_AREA_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Defence-in-depth body cap dedicated to transcripts (separate from the
# env-driven ``MEMORY_MAX_BODY_BYTES`` cap that protects the @tool
# write path). 200 KiB ~= 30 000 RU words ~= 2-3 hours of transcript.
TRANSCRIPT_MAX_BODY_BYTES = 200_000

# Source kinds that audio callers may pass.
_VALID_SOURCES = frozenset({"voice", "audio", "url"})

# Russian transliteration table (schoolbook). Single-char keys — multi-
# char outputs handle ж/ч/ш/щ/ю/я. Anything unmapped is dropped by
# :func:`slugify_area`.
_RU_TO_LAT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


class TranscriptSaveError(RuntimeError):
    """Raised on irrecoverable save failures (validation, IO, lock).

    The handler maps this to the spec's Russian transcript-save-failed
    reply. :func:`save_transcript` NEVER raises for a sentinel hit —
    those are scrubbed + logged + the save proceeds.
    """


def slugify_area(caption: str | None) -> str:
    """Map a free-form caption to a vault area name.

    Examples:
        ``"проект альфа"`` → ``"proekt_alfa"``
        ``""``             → ``"inbox"``
        ``"../etc"``       → ``"inbox"`` (validate_path safety net)

    The Russian schoolbook transliteration covers Cyrillic; ASCII
    alphanumerics + ``_-`` pass through; whitespace becomes ``_``;
    everything else (punctuation, emoji) is silently dropped.
    """
    if not caption:
        return "inbox"
    out: list[str] = []
    for ch in caption.strip().lower():
        if ch.isascii() and (ch.isalnum() or ch in {"_", "-"}):
            out.append(ch)
        elif ch in _RU_TO_LAT:
            out.append(_RU_TO_LAT[ch])
        elif ch.isspace():
            out.append("_")
        # any other char (punctuation, non-Russian Cyrillic, emoji) → drop
    slug = "".join(out).strip("_-")
    if not slug:
        return "inbox"
    # _AREA_RE caps at 33 chars total (1 leading + up to 32 trailing).
    slug = slug[:32]
    if not _AREA_RE.match(slug):
        return "inbox"
    return slug


def _fmt_duration(sec: int) -> str:
    """``312 -> "5m12s"``, ``45 -> "45s"``, ``3672 -> "1h1m12s"``."""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


async def save_transcript(
    *,
    vault_dir: Path,
    index_db_path: Path,
    area: str,
    title: str,
    body: str,
    tags: list[str],
    source: str,
    duration_sec: int | None,
    language: str = "ru",
    extra_frontmatter: dict[str, str] | None = None,
    max_body_bytes: int = TRANSCRIPT_MAX_BODY_BYTES,
) -> Path:
    """Persist a transcript to ``vault_dir/<area>/<title>.md``.

    Returns the absolute path written. Caller derives a vault-relative
    string for marker rendering via ``.relative_to(vault_dir)``.

    Raises :class:`TranscriptSaveError` only on validation / IO / lock
    failures. Sentinel hits in ``body`` are replaced with
    ``[redacted-tag]`` and the save proceeds (H7 closure).

    On overwrite the existing note's ``created`` is preserved; ``updated``
    is always now-time. ``extra_frontmatter`` keys never override the
    server-managed ``created`` / ``updated`` / ``area`` / ``source`` /
    ``lang`` / ``duration_sec`` / ``duration_human`` invariants.
    """
    # -- 1. Argument validation --------------------------------------
    if not _AREA_RE.match(area):
        raise TranscriptSaveError(f"invalid area name {area!r}")
    if not isinstance(title, str) or not title.strip():
        raise TranscriptSaveError("title must be non-empty")
    if source not in _VALID_SOURCES:
        raise TranscriptSaveError(f"invalid source {source!r}")
    if not isinstance(body, str):
        raise TranscriptSaveError("body must be a string")
    if not isinstance(tags, list):
        raise TranscriptSaveError("tags must be a list")

    # -- 2. Path derivation + auto-mkdir (devil M9) ------------------
    # ``vault_dir`` may be configured at boot via ``configure_memory``,
    # but this module is stateless — guarantee the index DB schema is
    # in place before ``write_note_tx`` runs. ``_ensure_index`` is
    # idempotent (CREATE TABLE IF NOT EXISTS throughout). The few sync
    # I/O calls here are bounded (single mkdir + one sqlite open) and
    # protected by the vault_lock that ``write_note_tx`` acquires
    # downstream — no anyio hop required.
    await asyncio.to_thread(vault_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        (vault_dir / ".tmp").mkdir, parents=True, exist_ok=True
    )
    await asyncio.to_thread(core._ensure_index, index_db_path)
    rel = Path(area) / f"{title}.md"
    try:
        full = core.validate_path(str(rel), vault_dir)
    except ValueError as exc:
        raise TranscriptSaveError(f"path: {exc}") from exc
    await asyncio.to_thread(full.parent.mkdir, parents=True, exist_ok=True)

    # -- 3. Body sentinel-strip BEFORE sanitize (H7) ------------------
    # Owner content trumps the sentinel reject — replace literal
    # sentinel tokens with a static placeholder, never reject. Logging
    # the scrub helps post-mortem if Whisper hallucinates a sentinel
    # shape from a tag-rich source.
    scrubbed = core._SENTINEL_RE.sub("[redacted-tag]", body)
    if scrubbed != body:
        log.warning(
            "save_transcript_sentinel_scrubbed",
            area=area,
            title=title,
            source=source,
        )
    try:
        clean_body = core.sanitize_body(scrubbed, max_body_bytes)
    except ValueError as exc:
        msg = str(exc)
        # Bare ``---`` line — rare for a transcript but possible
        # (TV-show title, dialogue marker hallucinated by Whisper).
        # Indent the offending line and retry sanitize ONCE; if still
        # bad, surface to the caller. This is the only retry path —
        # other validation failures (oversize body, encoding) raise
        # immediately.
        if "bare '---'" in msg:
            # ``sanitize_body`` rejects any line whose ``.strip() ==
            # "---"``, which includes whitespace-indented variants.
            # Replacing the bare separator with the visually-similar
            # ``\u2014\u2014\u2014`` (three em-dashes) preserves the
            # author's intent (TV-show title separator, dialogue
            # marker) without re-tripping the YAML-frontmatter guard.
            replaced = re.sub(r"(?m)^---$", "\u2014\u2014\u2014", scrubbed)
            try:
                clean_body = core.sanitize_body(replaced, max_body_bytes)
            except ValueError as exc2:
                raise TranscriptSaveError(f"sanitize: {exc2}") from exc2
        else:
            raise TranscriptSaveError(f"sanitize: {msg}") from exc

    # -- 4. Frontmatter assembly --------------------------------------
    # Preserve the original ``created`` on overwrite (mirrors phase-4
    # ``memory_write`` semantics). For new files, ``created == updated``.
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    preserved_created: str | None = None
    if full.exists():
        try:
            existing_raw = full.read_text(encoding="utf-8")
            existing_fm, _ = core.parse_frontmatter(existing_raw)
            cand = existing_fm.get("created")
            if cand is not None:
                preserved_created = str(cand)
        except (OSError, ValueError):
            preserved_created = None
    created = preserved_created or now_iso

    tags_str = [str(t) for t in tags]

    fm: dict[str, object] = {
        "title": title.strip(),
        "tags": tags_str,
        "area": area,
        "created": created,           # NEVER caller-controlled on first write
        "updated": now_iso,
        "source": source,
        "lang": language,
    }
    if duration_sec is not None:
        fm["duration_sec"] = int(duration_sec)
        fm["duration_human"] = _fmt_duration(int(duration_sec))
    # Caller-supplied extras MUST NOT override server-managed keys.
    if extra_frontmatter:
        for k, v in extra_frontmatter.items():
            fm.setdefault(k, str(v))

    try:
        content = core.serialize_frontmatter(fm, clean_body)
    except yaml.YAMLError as exc:
        raise TranscriptSaveError(f"frontmatter: {exc}") from exc

    # -- 5. Index row + atomic write under vault lock ----------------
    tags_json = json.dumps(tags_str, ensure_ascii=False)
    row = (
        str(rel),
        title.strip(),
        tags_json,
        area,
        clean_body,
        created,
        now_iso,
    )
    try:
        await asyncio.to_thread(
            core.write_note_tx,
            full,
            rel,
            row,
            content,
            vault_dir,
            index_db_path,
        )
    except TimeoutError as exc:
        raise TranscriptSaveError("vault lock contention") from exc
    except sqlite3.OperationalError as exc:
        raise TranscriptSaveError(f"index: {exc}") from exc
    except OSError as exc:
        raise TranscriptSaveError(f"vault io: {exc}") from exc

    log.info(
        "save_transcript_ok",
        area=area,
        title=title,
        source=source,
        duration_sec=duration_sec,
        body_bytes=len(clean_body.encode("utf-8")),
        path=str(rel),
    )
    return full
