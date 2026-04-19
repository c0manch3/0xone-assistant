"""Phase 7 / commit 6 — `classify_artefact` routing.

`dispatch_reply` delegates extension → dispatch-kind classification
to `assistant.media.artefacts.classify_artefact`. This test pins the
mapping so a future extension-table drift is caught before it
misroutes a photo to `send_document` (or worse, loses the file
entirely).

Coverage:
  * Every `_PHOTO_EXT` entry → "photo".
  * Every `_AUDIO_EXT` entry → "audio".
  * Every `_DOC_EXT`   entry → "document".
  * Upper-case / mixed-case suffix — matches `ARTEFACT_RE`'s
    `re.IGNORECASE` behaviour.
  * Unknown extension → ValueError (loud failure; per docstring).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.media.artefacts import classify_artefact

# Hard-coded tables so any drift in `media/artefacts.py` surfaces
# here as an explicit test failure rather than silent mis-routing.
_PHOTO = (".png", ".jpg", ".jpeg", ".webp")
_AUDIO = (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac")
_DOC = (".pdf", ".docx", ".txt", ".xlsx", ".rtf")


@pytest.mark.parametrize("ext", _PHOTO)
def test_classify_photo(ext: str) -> None:
    assert classify_artefact(Path(f"/abs/outbox/x{ext}")) == "photo"


@pytest.mark.parametrize("ext", _AUDIO)
def test_classify_audio(ext: str) -> None:
    assert classify_artefact(Path(f"/abs/outbox/x{ext}")) == "audio"


@pytest.mark.parametrize("ext", _DOC)
def test_classify_document(ext: str) -> None:
    assert classify_artefact(Path(f"/abs/outbox/x{ext}")) == "document"


@pytest.mark.parametrize(
    "path",
    [
        "/abs/outbox/IMG.PNG",
        "/abs/outbox/SOUND.MP3",
        "/abs/outbox/Report.PDF",
        "/abs/outbox/Mixed.jPeG",
        "/abs/outbox/Another.DoCx",
    ],
)
def test_classify_case_insensitive(path: str) -> None:
    """Mirror ARTEFACT_RE's re.IGNORECASE — no split-brain between
    extract and classify."""
    # We only care that it returns SOMETHING valid; the per-ext asserts
    # above cover the lookup table.
    kind = classify_artefact(Path(path))
    assert kind in {"photo", "audio", "document"}


def test_classify_unknown_raises() -> None:
    """Per docstring: classify MUST raise on unknown ext. A silent
    fallback to "document" would misroute `.exe` / `.dll` / `.zip`
    through Telegram without a caller noticing."""
    with pytest.raises(ValueError, match="unknown artefact extension"):
        classify_artefact(Path("/abs/outbox/x.exe"))
    with pytest.raises(ValueError):
        classify_artefact(Path("/abs/outbox/x"))  # no extension at all


def test_classify_cyrillic_filename_keeps_extension_detection() -> None:
    """UTF-8 stem with an ASCII suffix MUST still classify correctly —
    `Path.suffix` is byte-string tolerant (suffix is the dot-last
    component, independent of stem encoding)."""
    assert classify_artefact(Path("/abs/outbox/отчёт.pdf")) == "document"
    assert classify_artefact(Path("/abs/outbox/фото.jpg")) == "photo"
    assert classify_artefact(Path("/abs/outbox/голос.ogg")) == "audio"
