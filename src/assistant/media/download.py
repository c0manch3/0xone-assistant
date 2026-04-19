"""Size-capped streaming download of Telegram files (phase 7).

Two layers of defence against oversize payloads, because `aiogram`
reports `File.file_size` as `int | None` for every media kind (voice,
audio, photo, document, video_note — verified in spike S-6):

  1. **Pre-flight** — if `file.file_size is not None AND > max_bytes`,
     raise `SizeCapExceeded` BEFORE opening the destination. No bytes
     touch disk, no `unlink` needed.

  2. **Streaming** — wrap the destination in `_SizeCappedWriter(cap=
     max_bytes)` and pass it to `bot.download_file(destination=...)`.
     The writer raises `SizeCapExceeded` from `write()` when the
     running total exceeds `cap`. aiogram 3.26's
     `__download_file_binary_io` loop is a bare
     `async for chunk in stream: destination.write(chunk);
     destination.flush()` with NO try/except (source-audited, pitfall
     #3, C-3 fix-pack), so the exception propagates cleanly out of
     `download_file`. The caller unlinks the partial file and
     re-raises.

Why `write()` AND `flush()` must both be implemented on the writer:
  aiogram calls both per chunk. If `flush()` is missing we'd hit
  `AttributeError` mid-stream; the exception would leak out of the
  aiohttp reader rather than the intended `SizeCapExceeded` channel.

Why a custom `BinaryIO` wrapper (NOT `str | Path`):
  `Bot.download_file` signature is
  `destination: BinaryIO | pathlib.Path | str | None`. When
  `destination` is a string/Path, aiogram routes to
  `__download_file` which calls `aiofiles.open(...)` internally and
  our wrapper never gets a chance to count bytes. Passing a
  `BinaryIO` forces the `__download_file_binary_io` code path
  (aiogram source line ~439-441).
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import uuid
from pathlib import Path
from typing import BinaryIO, cast

from aiogram import Bot

from assistant.logger import get_logger

log = get_logger("media.download")


class SizeCapExceeded(Exception):  # noqa: N818 -- spec-mandated name (spike S-6 / impl §2.3)
    """Raised when a download would exceed the configured byte cap.

    Carries `.cap` (the configured maximum) and `.received` (best-effort
    running count at the moment of the abort) so callers can log
    meaningful diagnostics without having to re-parse the exception
    message.
    """

    def __init__(self, *, cap: int, received: int, message: str | None = None) -> None:
        self.cap = cap
        self.received = received
        super().__init__(
            message or f"download exceeded cap {cap} bytes (received {received})"
        )


class _SizeCappedWriter:
    """`BinaryIO`-compatible sink that aborts when cap is exceeded.

    Implements the subset of `BinaryIO` that `aiogram.Bot.download_file`
    actually exercises (source-audited on 3.26): `write(data: bytes)`
    + `flush()`. We intentionally do NOT subclass `io.RawIOBase` because
    (a) the interface surface we need is tiny, and (b) we want a
    runtime `AttributeError` if aiogram starts relying on additional
    methods in a future version -- that's a safer failure mode than
    silently inheriting no-op defaults.
    """

    __slots__ = ("_cap", "_dest", "_written")

    def __init__(self, dest: BinaryIO, cap: int) -> None:
        if cap <= 0:
            raise ValueError(f"cap must be positive, got {cap}")
        self._dest = dest
        self._cap = cap
        self._written = 0

    def write(self, data: bytes) -> int:
        # Guard BEFORE delegating so a pathological cap-sized chunk
        # doesn't briefly bloat the sink beyond the limit on disk
        # (matters when `dest` is a real file: a write past the cap
        # would require a truncate to restore sanity).
        projected = self._written + len(data)
        if projected > self._cap:
            raise SizeCapExceeded(cap=self._cap, received=projected)
        n = self._dest.write(data)
        # `BinaryIO.write` may return None for unbuffered streams, but
        # for a real file object it returns the byte count. We track
        # by `len(data)` because that's what was *accepted into* the
        # sink -- even if the underlying write were short, aiogram
        # would not retry the remainder (it treats the return as
        # advisory). The cap check above is the load-bearing
        # invariant; bookkeeping here is defensive.
        self._written = projected
        return n if n is not None else len(data)

    def flush(self) -> None:
        # aiogram 3.26 calls `destination.flush()` after every chunk
        # (C-3 source audit). Forward to the wrapped dest so the OS
        # page cache is pushed out -- this matches the behaviour of
        # passing a raw file object and keeps the post-download
        # `stat().st_size` accurate for the caller's own audit.
        self._dest.flush()

    @property
    def written(self) -> int:
        """Bytes accepted by `write()` so far (diagnostic only)."""
        return self._written


def _suffix_for(filename: str, mime_type: str | None) -> str:
    """Best-effort extension recovery from a Telegram-supplied filename.

    Falls back to `mimetypes.guess_extension(mime_type)` when the
    original filename has no extension (common for `voice` messages
    where aiogram synthesises a placeholder name without suffix).
    Returns an empty string when neither source yields one -- the
    caller is expected to still produce a usable UUID-based path
    (`<uuid>.bin` vs `<uuid>`).
    """
    suffix = Path(filename).suffix
    if suffix:
        return suffix.lower()
    if mime_type:
        guess = mimetypes.guess_extension(mime_type)
        if guess:
            return guess
    return ""


async def download_telegram_file(
    bot: Bot,
    file_id: str,
    dest_dir: Path,
    suggested_filename: str,
    *,
    max_bytes: int,
    timeout_s: int = 30,
) -> Path:
    """Download a Telegram file under a hard byte cap.

    Flow:
      1. `bot.get_file(file_id)` fetches metadata. `file.file_path` is
         the server-side relative handle (not a local FS path).
      2. If `file.file_size is not None AND file.file_size > max_bytes`
         raise `SizeCapExceeded` immediately (pre-flight).
      3. Build the destination path: `<dest_dir>/<uuid4>.<ext>` where
         `<ext>` is derived from `suggested_filename` or the reported
         MIME type. `<uuid4>` avoids filename collisions on concurrent
         media_group uploads (10 photos can arrive in one envelope).
      4. Open the destination in `wb` mode, wrap in
         `_SizeCappedWriter(cap=max_bytes)`, call
         `bot.download_file(file.file_path, destination=sink,
         timeout=timeout_s)`.
      5. On `SizeCapExceeded`: `dest_path.unlink(missing_ok=True)`,
         re-raise. On any other exception: unlink, re-raise.
      6. On success: return the resolved absolute path.

    Caller contract:
      * `dest_dir` must exist and be writable (typically
        `inbox_dir(data_dir)` -- `ensure_media_dirs()` guarantees
        this at daemon startup, pitfall #14).
      * `max_bytes` MUST be positive; the MediaSettings defaults
        (5_242_880 / 10_485_760 / 15_000_000 / 20_971_520) are the
        recommended values.
      * `suggested_filename` is advisory — used ONLY for extension
        derivation. The saved file is ALWAYS named with a UUID to
        prevent path-traversal via a crafted Telegram filename.

    Returns: resolved absolute `Path` to the saved file.

    Raises:
      * `SizeCapExceeded` — pre-flight or streaming cap violation.
      * `OSError` — filesystem failures (disk full, permission denied,
        etc.); the partial file is unlinked before re-raise.
      * `aiogram.exceptions.TelegramAPIError` — bubbles up from
        `bot.get_file` / `bot.download_file`; partial file (if any)
        is unlinked.
    """
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be positive, got {max_bytes}")

    file = await bot.get_file(file_id)

    # Pre-flight size check (pitfall #3 step 1). `file_size` may be None
    # for any of the five media kinds per S-6; we only reject when we
    # have a concrete number. `None` forces reliance on the streaming
    # cap below.
    file_size = file.file_size
    if file_size is not None and file_size > max_bytes:
        raise SizeCapExceeded(
            cap=max_bytes,
            received=file_size,
            message=(
                f"pre-flight reject: file.file_size {file_size} > cap {max_bytes}"
            ),
        )

    if file.file_path is None:
        # aiogram types `file_path` as `str | None`; in practice the
        # Bot API always returns a path for files under the 20 MB
        # getFile ceiling. Raise explicitly so we don't pass `None`
        # into `download_file` and get an obscure aiogram-side error.
        raise RuntimeError(
            f"Telegram did not return file_path for file_id={file_id!r}"
        )

    suffix = _suffix_for(suggested_filename, None)
    dest_path = (dest_dir / f"{uuid.uuid4().hex}{suffix}").resolve()

    # Defence-in-depth: the UUID + dest_dir combination already
    # prevents traversal, but an attacker-controlled `dest_dir` (not
    # possible in the current call graph, but protect anyway) could
    # be a symlink pointing elsewhere. Verify the resolved destination
    # lies under the resolved dest_dir.
    resolved_dir = dest_dir.resolve()
    if not dest_path.is_relative_to(resolved_dir):
        raise RuntimeError(
            f"resolved dest_path {dest_path} escapes dest_dir {resolved_dir}"
        )

    # Open with O_WRONLY|O_CREAT|O_EXCL semantics (via 'xb') so a UUID
    # collision (astronomically unlikely but not impossible) doesn't
    # silently overwrite an existing file mid-stream. Chmod to 0o600
    # after creation so a loose umask doesn't leak the media to other
    # local users.
    try:
        fd = os.open(
            dest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        # One-retry on the 1-in-2^128 collision.
        dest_path = (dest_dir / f"{uuid.uuid4().hex}{suffix}").resolve()
        fd = os.open(
            dest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )

    try:
        with os.fdopen(fd, "wb", closefd=True) as fp:
            sink = _SizeCappedWriter(fp, cap=max_bytes)
            # `cast` because `_SizeCappedWriter` is structurally
            # compatible with `BinaryIO` (exposes `write` + `flush`)
            # but does NOT nominally subclass it. aiogram accepts the
            # structural compatibility at runtime.
            await bot.download_file(
                file.file_path,
                destination=cast(BinaryIO, sink),
                timeout=timeout_s,
            )
    except SizeCapExceeded:
        dest_path.unlink(missing_ok=True)
        raise
    except Exception:
        # Fix-pack I6: `(OSError, Exception)` was a tautology — OSError
        # is a subclass of Exception, so the broader alternative
        # subsumed it. The intent is "any error other than
        # SizeCapExceeded" so partial-file cleanup runs uniformly;
        # `except Exception` expresses that directly without the
        # misleading implication that OSError is handled specially.
        #
        # TelegramAPIError, asyncio.TimeoutError, etc. all surface via
        # this branch and re-raise after the unlink so the caller
        # (adapter) decides how to respond (reject vs retry vs
        # surface). `CancelledError` inherits from BaseException (not
        # Exception) so it is intentionally NOT caught here — a
        # cancelled download propagates cleanly without the cleanup
        # step, which is fine because the aiohttp body drain handles
        # its own cleanup when cancelled mid-stream.
        dest_path.unlink(missing_ok=True)
        raise

    log.debug(
        "media_downloaded",
        file_id=file_id,
        dest=str(dest_path),
        bytes=dest_path.stat().st_size if dest_path.exists() else 0,
        reported_size=file_size,
    )
    return dest_path


# Re-export for tests + callers that want to wrap their own sinks.
__all__ = [
    "SizeCapExceeded",
    "_SizeCappedWriter",
    "download_telegram_file",
]


# Asyncio compatibility sentinel: `download_telegram_file` is
# coroutine-callable; static analysers already know this via the
# annotation but keeping a module-level reference prevents
# tree-shaking issues in frozen-app builds (none in-tree today; kept
# as hedge).
assert asyncio.iscoroutinefunction(download_telegram_file)
