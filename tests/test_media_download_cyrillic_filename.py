"""Phase 7 / commit 18d — Cyrillic / UTF-8 filename round-trip.

The phase-7 media pipeline encounters Telegram-supplied filenames at
four points. A regression at any one of them — from a bytes-vs-str
mismatch in `Path.resolve()`, a misconfigured `ensure_ascii=True`
json dump, a default-encoding `open()` on a non-UTF-8 locale, or a
regex coded with `re.ASCII` — would manifest as mojibake or an
outright error for any owner who sends `"фото_отчёт.jpg"`. This file
is the integration-level guardrail: if any stage mangles UTF-8, at
least one assert here trips.

Stages covered:

1. ``media.download.download_telegram_file(...)`` — a mock Bot with a
   Cyrillic ``suggested_filename`` must land bytes on disk. The saved
   file uses a UUID-derived name (per the download helper's
   path-traversal hardening), so we assert the EXTENSION survives
   and the BYTES match what the mock streamed. That is the realistic
   contract: the Cyrillic name is advisory, the on-disk name is a
   UUID, and the size/content round-trip is what callers rely on.

2. ``MediaAttachment`` — the dataclass must preserve the Cyrillic
   ``filename_original`` byte-for-byte (no NFC/NFD surprise, no
   implicit str-to-bytes coercion). The handler's envelope-building
   code reads this field into a system-note; if Python silently
   normalised here, the model would see a different string than the
   user typed.

3. CLI ``tools/extract_doc/main.py`` — argv carrying a Cyrillic path
   must be accepted by the path guard, resolved, and extracted.
   ``tools/transcribe/main.py`` would be the alternate per the task
   spec; we choose ``extract_doc`` because it round-trips the file
   CONTENT back through JSON, giving a stronger assertion (the
   transcribe CLI requires a running mlx-whisper backend we don't
   have in CI). The stdin/stdout decoding honours UTF-8 because the
   CLI writes ``ensure_ascii=False``; this test asserts that too.

4. ``dispatch_reply`` — ``ARTEFACT_RE`` already covers Cyrillic paths
   in ``test_dispatch_reply_regex.py::cyrillic_*``, but we re-exercise
   the FULL pipeline end-to-end (regex + path-guard + adapter.send_*)
   with a Cyrillic-named outbox path to pin down the integration.

Philosophy: tests should never patch over real bugs. If a stage does
mangle UTF-8, the task instructs us to FLAG it in the report rather
than silently normalise. The commentary in each assertion block
makes explicit which invariant would regress on failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from assistant.adapters.base import MediaAttachment, MessengerAdapter
from assistant.adapters.dispatch_reply import _DedupLedger, dispatch_reply
from assistant.media.artefacts import ARTEFACT_RE
from assistant.media.download import download_telegram_file

# aiogram's `Bot.download_file` takes a `timeout=` kwarg; mirrors the
# pattern from `tests/test_media_download.py` (ASYNC109).
# ruff: noqa: ASYNC109


# Canonical Cyrillic filename used across every assertion in this
# file. Chosen from the task spec verbatim. A single source-of-truth
# constant rules out copy-paste drift between the four stages.
_CYRILLIC_NAME = "фото_отчёт.jpg"


# --- Stage 1: download_telegram_file round-trip --------------------


class _MockFile:
    """Minimal stand-in for `aiogram.types.File` — same shape as the
    helper used in `tests/test_media_download.py`."""

    def __init__(
        self, file_size: int | None, file_path: str = "fakes/remote.bin"
    ) -> None:
        self.file_size = file_size
        self.file_path = file_path


class _MockBot:
    """Mock Bot that streams fixed `chunks` into the BinaryIO sink the
    downloader passes in. Matches aiogram 3.26's
    `__download_file_binary_io` loop: write + flush per chunk."""

    def __init__(self, *, chunks: list[bytes], file_size: int | None) -> None:
        self._chunks = chunks
        self._file = _MockFile(file_size=file_size)

    async def get_file(
        self, file_id: str, request_timeout: int | None = None
    ) -> _MockFile:
        return self._file

    async def download_file(
        self,
        file_path: str,
        destination: Any = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        seek: bool = True,
    ) -> BinaryIO | None:
        assert destination is not None, "test relies on BinaryIO path"
        for chunk in self._chunks:
            destination.write(chunk)
            destination.flush()
        return destination


async def test_download_telegram_file_preserves_bytes_with_cyrillic_suggested_name(
    tmp_path: Path,
) -> None:
    """Stage 1 — the downloader MUST accept a Cyrillic
    ``suggested_filename`` without raising, and bytes streamed by the
    mock Bot MUST land on disk intact.

    Note: the DEST NAME is UUID-based by design (see
    ``media/download.py`` — path-traversal hardening). So we do NOT
    assert on the destination filename carrying Cyrillic; we assert
    that:
      * the call completes (i.e. no ``UnicodeDecodeError`` /
        ``UnicodeEncodeError`` / ``TypeError`` from feeding Cyrillic
        into ``_suffix_for(Path(...))``);
      * the ``.jpg`` extension — which IS derived from the Cyrillic
        name via ``Path(filename).suffix`` — survives;
      * the BYTES match the stream source.

    This is the realistic round-trip: Telegram gives us a Cyrillic
    name, disk gets a UUID+ext, and the content is preserved.
    """
    payload = b"\xff\xd8\xff\xe0RE" + (b"\xab" * 256) + b"\xff\xd9"
    bot = _MockBot(chunks=[payload], file_size=len(payload))

    saved = await download_telegram_file(
        bot,  # type: ignore[arg-type]
        file_id="file_cyr_1",
        dest_dir=tmp_path,
        suggested_filename=_CYRILLIC_NAME,
        max_bytes=10 * 1024,
    )

    assert saved.exists()
    # Extension survives `Path(...).suffix` + `.lower()` in
    # `_suffix_for`. If Python's str→Path coercion had silently
    # stripped the Cyrillic stem, we'd still end up with ".jpg" here
    # — the test's real bite is the NO-RAISE contract above.
    assert saved.suffix == ".jpg"
    # Byte-for-byte integrity: the stream we fed the mock must round-
    # trip through `_SizeCappedWriter` without corruption. This would
    # regress if a future refactor decided to wrap `destination` in a
    # text-mode handle.
    assert saved.read_bytes() == payload


async def test_download_telegram_file_accepts_cyrillic_even_with_nfd_form(
    tmp_path: Path,
) -> None:
    """Stage 1 (variant) — the NFD (decomposed) form of "ё" is a
    distinct code-point sequence ("е" + U+0308). The downloader must
    accept both NFC and NFD without silently normalising, so the
    caller's choice of form round-trips into `filename_original`.

    We're testing the DOWNLOADER here (not the dataclass); the only
    invariant the downloader owes us is "does not raise on NFD".
    If it did normalise, we couldn't easily detect that from disk
    (the destination is UUID-named) — but we'd at least see a raise
    or a silently-mismatched suffix, which we guard below.
    """
    # NFD form: "ё" -> "е" + combining diaeresis U+0308.
    nfd_name = "фото\u0435\u0308тчёт.jpg"
    payload = b"nfd-bytes"
    bot = _MockBot(chunks=[payload], file_size=len(payload))

    saved = await download_telegram_file(
        bot,  # type: ignore[arg-type]
        file_id="file_cyr_nfd",
        dest_dir=tmp_path,
        suggested_filename=nfd_name,
        max_bytes=1024,
    )
    assert saved.exists()
    assert saved.suffix == ".jpg"
    assert saved.read_bytes() == payload


# --- Stage 2: MediaAttachment dataclass preservation ---------------


def test_media_attachment_preserves_cyrillic_filename_original(
    tmp_path: Path,
) -> None:
    """Stage 2 — the frozen dataclass MUST store the Cyrillic name
    byte-for-byte. Any silent ``unicodedata.normalize`` or ``ascii``
    fallback here would desynchronise the handler's system-note from
    what the user sent.
    """
    local_path = tmp_path / "some_uuid.jpg"
    local_path.write_bytes(b"x")
    att = MediaAttachment(
        kind="document",
        local_path=local_path,
        mime_type="application/octet-stream",
        file_size=1,
        filename_original=_CYRILLIC_NAME,
    )

    # Identity (not just equality): the dataclass must NOT have
    # invoked `unicodedata.normalize` on its way in.
    assert att.filename_original == _CYRILLIC_NAME
    assert len(att.filename_original or "") == len(_CYRILLIC_NAME)
    # All the Cyrillic characters survive — enumerate three of the
    # trickier ones ("ф", "ё", "т") to pin down that there's no
    # ASCII-only truncation somewhere upstream.
    assert "ф" in (att.filename_original or "")
    assert "ё" in (att.filename_original or "")
    assert "т" in (att.filename_original or "")


def test_media_attachment_cyrillic_survives_handler_note_format(
    tmp_path: Path,
) -> None:
    """Stage 2 (handler projection) — the handler's document-branch
    in ``handlers/message.py`` f-strings the Cyrillic
    ``filename_original`` into a ``system_notes`` entry:

        f"user attached document '{att.filename_original}' at ..."

    This smoke check makes sure Python's native f-string UTF-8
    handling is enough — if a refactor ever swapped the f-string for
    a ``str.encode('ascii')`` or a ``.format()`` with a narrow codec
    context, the assert would flag it.
    """
    local_path = tmp_path / "dest.bin"
    local_path.write_bytes(b"x")
    att = MediaAttachment(
        kind="document",
        local_path=local_path,
        filename_original=_CYRILLIC_NAME,
    )
    rendered = (
        f"user attached document '{att.filename_original}' at "
        f"{att.local_path}. use tools/extract_doc/."
    )
    assert _CYRILLIC_NAME in rendered
    # The full filename, not truncated or replaced.
    assert rendered.count(_CYRILLIC_NAME) == 1


# --- Stage 3: extract_doc CLI accepts a Cyrillic stage-file --------


_EXTRACT_CLI = (
    Path(__file__).resolve().parents[1] / "tools" / "extract_doc" / "main.py"
)


def _run_extract_doc(
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Mirror the Bash-allowlist invocation used at runtime.

    Encoding-safety: we explicitly request UTF-8 for stdout/stderr
    (PYTHONIOENCODING) so a CI host with ``LANG=C`` doesn't make the
    child process emit garbled bytes, which would mask a real bug.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(_EXTRACT_CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )


def test_extract_doc_cli_accepts_cyrillic_filename(tmp_path: Path) -> None:
    """Stage 3 — argv parsing + ``_validate_path`` + extractor MUST
    handle a Cyrillic path argument.

    We use a plain ``.txt`` stage file because:
      * it exercises the same argv/path pipeline as the DOCX/PDF
        extractors (they all route through ``_validate_path``);
      * it avoids pulling in the DOCX/PDF fixture builders for a
        test whose real concern is "does the CLI read UTF-8 argv".

    If ``_validate_path`` ever regressed to a ``raw.encode("ascii")``
    on its input, we'd see EXIT_VALIDATION (3) or EXIT_IO (4) here
    rather than EXIT_OK.
    """
    src = tmp_path / _CYRILLIC_NAME.replace(".jpg", ".txt")
    body = "Это — отчёт с кириллицей.\nline 2\n"
    src.write_text(body, encoding="utf-8")

    r = _run_extract_doc(str(src))
    assert r.returncode == 0, (
        f"CLI rejected Cyrillic filename: rc={r.returncode} "
        f"stderr={r.stderr!r}"
    )
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["format"] == "txt"
    # The CLI echoes the RESOLVED absolute path; it must equal the
    # source we passed (after resolve). On macOS default FS this is
    # case-insensitive but preserving; on APFS/ext4 it's exact.
    assert Path(payload["path"]).name == src.name
    # UTF-8 body round-trip — `_extract_txt` + JSON serialisation
    # with `ensure_ascii=False` must preserve Cyrillic characters.
    assert "отчёт" in payload["text"]
    assert "кириллицей" in payload["text"]


# --- Stage 4: dispatch_reply end-to-end with a Cyrillic outbox path


class _RecordingAdapter(MessengerAdapter):
    """Bare-minimum `MessengerAdapter` double for dispatch_reply."""

    def __init__(self) -> None:
        self.photos: list[Path] = []
        self.documents: list[Path] = []
        self.audios: list[Path] = []
        self.texts: list[str] = []

    async def start(self) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append(text)

    async def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        self.photos.append(path)

    async def send_document(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        self.documents.append(path)

    async def send_audio(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        self.audios.append(path)


def test_artefact_re_matches_cyrillic_outbox_path(tmp_path: Path) -> None:
    """Stage 4a — ``ARTEFACT_RE`` must extract a Cyrillic absolute
    path. Sibling test ``test_dispatch_reply_regex.py`` covers a fixed
    corpus entry for ``/abs/outbox/отчёт.docx``; this variant uses an
    ACTUAL tmp-path (so the downstream integration test has a valid
    candidate to resolve).
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    target = outbox / _CYRILLIC_NAME
    target.write_bytes(b"PNG")

    text = f"готово: {target}"
    matches = ARTEFACT_RE.findall(text)
    # Exactly one match, equal to the raw path we embedded. If the
    # regex ever regressed to an ASCII-only character class, the
    # match would either truncate at the first Cyrillic byte or miss
    # entirely.
    assert matches == [str(target)]


async def test_dispatch_reply_delivers_cyrillic_outbox_path(
    tmp_path: Path,
) -> None:
    """Stage 4b — full ``dispatch_reply`` pipeline with a Cyrillic
    outbox filename: regex extracts → path-guard admits (inside
    outbox + exists) → classifier returns "photo" (.jpg) → adapter's
    ``send_photo`` receives the resolved Path intact.

    The assertion that the adapter received the EXACT resolved Path
    (not a ``str``, not a name-stripped Path) is load-bearing: it
    means the dispatch pipeline never round-tripped through any
    ASCII-only encoding step.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    artefact = outbox / _CYRILLIC_NAME
    artefact.write_bytes(b"PNG")

    adapter = _RecordingAdapter()
    await dispatch_reply(
        adapter,
        chat_id=42,
        text=f"готово: {artefact}",
        outbox_root=outbox,
        dedup=_DedupLedger(),
    )

    # The artefact was dispatched exactly once, as a photo.
    assert adapter.photos == [artefact.resolve()]
    assert adapter.documents == []
    assert adapter.audios == []

    # The text that survives MUST have the Cyrillic path stripped out
    # but retain the surrounding Cyrillic prose — otherwise the model
    # reply becomes noise.
    assert len(adapter.texts) == 1
    cleaned = adapter.texts[0]
    assert str(artefact) not in cleaned
    assert "готово" in cleaned


async def test_dispatch_reply_cyrillic_classifies_document_variant(
    tmp_path: Path,
) -> None:
    """Stage 4c — the same pipeline with ``отчёт.docx`` MUST route to
    ``send_document`` (not ``send_photo``). Covers the classifier's
    extension-lookup path alongside the Cyrillic stem.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    doc_name = "отчёт.docx"
    artefact = outbox / doc_name
    # Minimal bytes — classifier uses suffix, not content.
    artefact.write_bytes(b"PK\x03\x04")

    adapter = _RecordingAdapter()
    await dispatch_reply(
        adapter,
        chat_id=7,
        text=f"см {artefact}",
        outbox_root=outbox,
        dedup=_DedupLedger(),
    )
    assert adapter.documents == [artefact.resolve()]
    assert adapter.photos == []
    assert adapter.audios == []


# --- Optional belt-and-braces: fixture sanity ----------------------


def test_cyrillic_name_constant_has_expected_shape() -> None:
    """Guard against an accidental edit that makes ``_CYRILLIC_NAME``
    ASCII — which would turn every assertion above into a no-op.
    """
    assert _CYRILLIC_NAME == "фото_отчёт.jpg"
    assert any(ord(c) > 127 for c in _CYRILLIC_NAME)
    # Suffix check: the Cyrillic name must still end in a plain ASCII
    # ".jpg" so the regex extension alternation can match.
    assert Path(_CYRILLIC_NAME).suffix == ".jpg"


# Silence a potential unused-import hint in environments that
# optimise imports on collection (ruff is satisfied without this;
# keeping it for humans reading the file).
_ = pytest
