"""Phase 6a — PDF extractor (fallback) unit tests.

Covers:
- happy path with synthetic PDF;
- sub-100-char text layer → Russian OCR hint string;
- corrupt PDF → ExtractionError;
- missing file → ExtractionError.

NB: the production path for PDFs is Option C (SDK Read tool with
multimodal payload). The pypdf-based ``extract_pdf`` is the fallback
when the live in-container probe fails. These tests pin the fallback
behaviour so flipping ``_is_pdf_native_read`` to ``False`` ships a
working pypdf-uniform path on day one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.files.extract import (
    PDF_NO_TEXT_LAYER_HINT,
    ExtractionError,
    extract_pdf,
)


def _make_text_pdf(path: Path, text: str) -> None:
    """Build a spec-compliant minimal text PDF.

    We don't use reportlab to avoid a test-only dep. The PDF below
    has a proper xref + trailer so pypdf 5.x reads it cleanly.
    """
    from io import BytesIO

    safe = text.replace("(", r"\(").replace(")", r"\)")
    content = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")

    buf = BytesIO()
    buf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")  # binary marker
    offsets: list[int] = [0]

    def obj(n: int, body: bytes) -> None:
        offsets.append(buf.tell())
        buf.write(f"{n} 0 obj\n".encode())
        buf.write(body)
        buf.write(b"\nendobj\n")

    obj(1, b"<</Type/Catalog/Pages 2 0 R>>")
    obj(2, b"<</Type/Pages/Count 1/Kids[3 0 R]>>")
    obj(
        3,
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
    )
    obj(
        4,
        f"<</Length {len(content)}>>\nstream\n".encode() + content + b"\nendstream",
    )
    obj(5, b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")

    xref_off = buf.tell()
    buf.write(b"xref\n0 6\n")
    buf.write(b"0000000000 65535 f \n")
    for o in offsets[1:]:
        buf.write(f"{o:010d} 00000 n \n".encode())
    buf.write(b"trailer\n<</Size 6/Root 1 0 R>>\nstartxref\n")
    buf.write(str(xref_off).encode() + b"\n%%EOF\n")

    path.write_bytes(buf.getvalue())


def test_extract_pdf_happy_path_returns_text(tmp_path: Path) -> None:
    """Sanity: a text PDF with > PDF_MIN_TEXT_LAYER_CHARS chars
    extracts to the underlying text."""
    p = tmp_path / "long.pdf"
    payload = (
        "PHASE 6A FILE UPLOADS BY 0XONE ASSISTANT WITH ENOUGH "
        "TEXT TO EXCEED THE 100 CHAR THRESHOLD COMFORTABLY THANKS"
    )
    _make_text_pdf(p, payload)

    text, n = extract_pdf(p)
    # pypdf may add whitespace/encoding artifacts; assert the payload
    # tokens survive.
    assert n == len(text)
    # We can't assert exact equality because pypdf normalises the
    # text layer — but the key tokens must round-trip.
    assert "PHASE" in text
    assert "FILE" in text
    assert "UPLOADS" in text


def test_extract_pdf_short_text_returns_no_text_layer_hint(tmp_path: Path) -> None:
    """Sub-100-char text layer → return PDF_NO_TEXT_LAYER_HINT.

    Devil M8 accepted: a real text PDF with <100 chars total triggers
    the same hint. Single-user trust model.
    """
    p = tmp_path / "short.pdf"
    _make_text_pdf(p, "short")

    text, n = extract_pdf(p)
    assert text == PDF_NO_TEXT_LAYER_HINT
    assert n == 0


def test_extract_pdf_corrupt_raises(tmp_path: Path) -> None:
    """A garbage file masquerading as ``.pdf`` raises ExtractionError."""
    p = tmp_path / "garbage.pdf"
    p.write_bytes(b"this is not a PDF at all")

    with pytest.raises(ExtractionError):
        extract_pdf(p)


def test_extract_pdf_missing_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "ghost.pdf"
    with pytest.raises(ExtractionError):
        extract_pdf(p)
