"""Phase 6b — magic-byte validation tests.

Covers ``assistant.files.vision.detect_image_kind`` +
``validate_magic_bytes``:

- JPEG / PNG / WEBP magic happy paths;
- All 6 HEIC ftyp brands (heic / heix / mif1 / msf1 / hevc / heim);
- Mismatch suffix vs. magic raises VisionError;
- Short file (< 12 bytes) raises VisionError;
- JPEG / JPG suffix alias allowed;
- Bogus magic returns None.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.files.vision import (
    VisionError,
    detect_image_kind,
    validate_magic_bytes,
)


def _heic_head(brand: bytes) -> bytes:
    """Synthesise a 12-byte HEIC head: 4 bytes box-size + b"ftyp" + brand."""
    return b"\x00\x00\x00\x18ftyp" + brand


# ---------------------------------------------------------------------------
# detect_image_kind
# ---------------------------------------------------------------------------


def test_detect_jpeg_returns_jpg() -> None:
    head = b"\xff\xd8\xff" + b"\x00" * 9
    assert detect_image_kind(head) == "jpg"


def test_detect_png_returns_png() -> None:
    head = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4
    assert detect_image_kind(head) == "png"


def test_detect_webp_returns_webp() -> None:
    head = b"RIFF\x00\x00\x00\x00WEBP"
    assert detect_image_kind(head) == "webp"


@pytest.mark.parametrize(
    "brand",
    [b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"heim"],
)
def test_detect_heic_brands(brand: bytes) -> None:
    head = _heic_head(brand)
    assert detect_image_kind(head) == "heic"


def test_detect_unknown_returns_none() -> None:
    head = b"\x00" * 12
    assert detect_image_kind(head) is None


def test_detect_random_bytes_returns_none() -> None:
    head = b"deadbeef\xff\xff\xff\xff"
    assert detect_image_kind(head) is None


def test_detect_short_input_returns_none() -> None:
    head = b"\xff\xd8"  # 2 bytes only
    assert detect_image_kind(head) is None


def test_detect_riff_without_webp_returns_none() -> None:
    """RIFF prefix but bytes 8..11 != WEBP (e.g. AVI/WAV) → not webp."""
    head = b"RIFF\x00\x00\x00\x00AVI "
    assert detect_image_kind(head) is None


def test_detect_ftyp_with_unknown_brand_returns_none() -> None:
    head = b"\x00\x00\x00\x18ftyp" + b"avif"
    assert detect_image_kind(head) is None


# ---------------------------------------------------------------------------
# validate_magic_bytes
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, payload: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


def test_validate_jpg_match_passes(tmp_path: Path) -> None:
    p = _write(tmp_path, "x.jpg", b"\xff\xd8\xff" + b"\x00" * 9)
    validate_magic_bytes(p, "jpg")


def test_validate_jpeg_alias_passes(tmp_path: Path) -> None:
    """``suffix='jpeg'`` + JPEG magic → accepted (alias)."""
    p = _write(tmp_path, "x.jpeg", b"\xff\xd8\xff" + b"\x00" * 9)
    validate_magic_bytes(p, "jpeg")


def test_validate_png_match_passes(tmp_path: Path) -> None:
    p = _write(tmp_path, "x.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4)
    validate_magic_bytes(p, "png")


def test_validate_webp_match_passes(tmp_path: Path) -> None:
    p = _write(tmp_path, "x.webp", b"RIFF\x00\x00\x00\x00WEBP")
    validate_magic_bytes(p, "webp")


def test_validate_heic_match_passes(tmp_path: Path) -> None:
    p = _write(tmp_path, "x.heic", _heic_head(b"heic"))
    validate_magic_bytes(p, "heic")


@pytest.mark.parametrize("brand", [b"heic", b"mif1", b"hevc"])
def test_validate_heif_alias_accepts_heif_suffix(
    tmp_path: Path, brand: bytes
) -> None:
    """F10: ``.heif`` is an Apple iOS-emitted alias for the same HEIF
    container as ``.heic``. The validator must accept either suffix
    when the magic bytes carry any HEIF brand.
    """
    p = _write(tmp_path, "x.heif", _heic_head(brand))
    validate_magic_bytes(p, "heif")


def test_validate_jpg_with_png_magic_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "x.jpg", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4)
    with pytest.raises(VisionError, match="magic mismatch"):
        validate_magic_bytes(p, "jpg")


def test_validate_png_with_jpg_magic_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "x.png", b"\xff\xd8\xff" + b"\x00" * 9)
    with pytest.raises(VisionError, match="magic mismatch"):
        validate_magic_bytes(p, "png")


def test_validate_heic_with_webp_magic_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "x.heic", b"RIFF\x00\x00\x00\x00WEBP")
    with pytest.raises(VisionError, match="magic mismatch"):
        validate_magic_bytes(p, "heic")


def test_validate_short_file_raises(tmp_path: Path) -> None:
    """File < 12 bytes → corrupt VisionError, not a magic mismatch."""
    p = _write(tmp_path, "x.jpg", b"\xff\xd8\xff")
    with pytest.raises(VisionError, match="shorter than 12 bytes"):
        validate_magic_bytes(p, "jpg")


def test_validate_unknown_magic_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "x.jpg", b"\x00" * 12)
    with pytest.raises(VisionError, match="not a recognised image"):
        validate_magic_bytes(p, "jpg")


def test_validate_unreadable_path_raises(tmp_path: Path) -> None:
    """Nonexistent path → VisionError("corrupt or unreadable")."""
    p = tmp_path / "nope.jpg"
    with pytest.raises(VisionError, match="corrupt or unreadable"):
        validate_magic_bytes(p, "jpg")
