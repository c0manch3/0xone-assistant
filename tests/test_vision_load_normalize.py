"""Phase 6b — load_and_normalize / build_image_content_block tests.

Covers:

- Resize: large image (3000x2000) → output ≤ 1568 px on long edge.
- Aspect-ratio preserved on non-square sources.
- EXIF stripped from output JPEG.
- Alpha (RGBA) source → RGB output.
- Tiny image (32x32) → output dims unchanged.
- Image bomb: dims > MAX_IMAGE_PIXELS → VisionError("image too large").
- Corrupt file (random bytes wrapped in JPEG suffix) → VisionError.
- build_image_content_block returns the canonical dict shape with
  base64-encoded data.

Real Pillow used (no mocks) — fixtures generated in tmp_path; libheif
isolation is in test_vision_heic.py.
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from assistant.files import vision as vmod
from assistant.files.vision import (
    JPEG_QUALITY,
    OUTPUT_MEDIA_TYPE,
    RESIZE_LONG_EDGE,
    VisionError,
    build_image_content_block,
    load_and_normalize,
)


def _save_jpeg(im: Image.Image, p: Path) -> None:
    im.save(p, format="JPEG", quality=92)


def _save_png(im: Image.Image, p: Path) -> None:
    im.save(p, format="PNG")


# ---------------------------------------------------------------------------
# Resize
# ---------------------------------------------------------------------------


def test_load_resizes_large_image(tmp_path: Path) -> None:
    """Source 3000x2000 → output long edge ≤ 1568."""
    src = Image.new("RGB", (3000, 2000), color=(200, 100, 50))
    src_path = tmp_path / "big.jpg"
    _save_jpeg(src, src_path)

    jpeg_bytes = load_and_normalize(src_path)

    out = Image.open(BytesIO(jpeg_bytes))
    assert max(out.size) <= RESIZE_LONG_EDGE
    # Aspect 3:2 preserved (width / height ≈ 1.5 ± rounding).
    ratio = out.size[0] / out.size[1]
    assert 1.4 < ratio < 1.6


def test_load_keeps_small_image_unchanged_dim(tmp_path: Path) -> None:
    """32x32 source — thumbnail no-op so output dims == 32x32."""
    src = Image.new("RGB", (32, 32), color="red")
    src_path = tmp_path / "tiny.jpg"
    _save_jpeg(src, src_path)

    jpeg_bytes = load_and_normalize(src_path)
    out = Image.open(BytesIO(jpeg_bytes))
    assert out.size == (32, 32)


def test_load_portrait_source_resizes_long_edge(tmp_path: Path) -> None:
    """Portrait 2000x3000 → height (long edge) ≤ 1568."""
    src = Image.new("RGB", (2000, 3000), color=(50, 50, 200))
    src_path = tmp_path / "portrait.jpg"
    _save_jpeg(src, src_path)

    jpeg_bytes = load_and_normalize(src_path)
    out = Image.open(BytesIO(jpeg_bytes))
    assert out.size[1] <= RESIZE_LONG_EDGE
    # Long edge is height for portrait; aspect preserved.
    ratio = out.size[1] / out.size[0]
    assert 1.4 < ratio < 1.6


# ---------------------------------------------------------------------------
# EXIF / alpha / format
# ---------------------------------------------------------------------------


def test_load_strips_exif(tmp_path: Path) -> None:
    """Output JPEG carries no APP1 EXIF block (privacy / GPS strip)."""
    src = Image.new("RGB", (200, 200), color="green")
    src_path = tmp_path / "with_exif.jpg"
    # Save with a synthetic EXIF block.
    exif_bytes = b"Exif\x00\x00MM\x00*" + b"\x00" * 20
    src.save(src_path, format="JPEG", exif=exif_bytes)

    jpeg_bytes = load_and_normalize(src_path)
    out = Image.open(BytesIO(jpeg_bytes))
    # Pillow returns ``b""`` or the EXIF bytes; our save passed ``exif=b""``
    # so the output should be either None or empty bytes.
    raw_exif = out.info.get("exif", b"")
    assert raw_exif in (b"", None)


def test_load_drops_alpha_to_rgb(tmp_path: Path) -> None:
    """RGBA PNG source → JPEG (RGB) output, no alpha-encode error."""
    src = Image.new("RGBA", (128, 128), color=(255, 0, 0, 128))
    src_path = tmp_path / "rgba.png"
    _save_png(src, src_path)

    jpeg_bytes = load_and_normalize(src_path)
    out = Image.open(BytesIO(jpeg_bytes))
    assert out.mode == "RGB"
    assert out.format == "JPEG"


def test_load_outputs_jpeg(tmp_path: Path) -> None:
    """Magic of the returned bytes is JPEG (FF D8 FF)."""
    src = Image.new("RGB", (64, 64), color="blue")
    src_path = tmp_path / "x.png"
    _save_png(src, src_path)

    jpeg_bytes = load_and_normalize(src_path)
    assert jpeg_bytes[:3] == b"\xff\xd8\xff"


# ---------------------------------------------------------------------------
# Image bomb
# ---------------------------------------------------------------------------


def test_load_rejects_image_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decoded pixel count > MAX_DECODED_PIXELS → VisionError.

    We monkeypatch the cap to a tiny value to avoid generating an
    actual 25 MP fixture in the test directory.
    """
    monkeypatch.setattr(vmod.Image, "MAX_IMAGE_PIXELS", 1000)

    # 100x100 = 10_000 px > 1_000.
    src = Image.new("RGB", (100, 100), color="red")
    src_path = tmp_path / "bomb.png"
    _save_png(src, src_path)

    with pytest.raises(VisionError, match="image too large"):
        load_and_normalize(src_path)


# ---------------------------------------------------------------------------
# Corrupt input
# ---------------------------------------------------------------------------


def test_load_corrupt_file_raises(tmp_path: Path) -> None:
    """File with JPEG suffix but random bytes → VisionError("corrupt …")."""
    src_path = tmp_path / "x.jpg"
    src_path.write_bytes(b"\xff\xd8\xff\x00garbage" * 20)

    with pytest.raises(VisionError, match="corrupt"):
        load_and_normalize(src_path)


def test_load_nonimage_file_raises(tmp_path: Path) -> None:
    """Plain text file with image suffix → VisionError."""
    src_path = tmp_path / "x.png"
    src_path.write_bytes(b"this is not an image")

    with pytest.raises(VisionError, match="corrupt"):
        load_and_normalize(src_path)


# ---------------------------------------------------------------------------
# build_image_content_block
# ---------------------------------------------------------------------------


def test_build_content_block_shape() -> None:
    """Block matches the Anthropic vision contract."""
    payload = b"\xff\xd8\xfffake jpeg"
    block = build_image_content_block(payload)
    assert block["type"] == "image"
    src = block["source"]
    assert src["type"] == "base64"
    assert src["media_type"] == OUTPUT_MEDIA_TYPE
    decoded = base64.standard_b64decode(src["data"])
    assert decoded == payload


def test_build_content_block_data_is_ascii_str() -> None:
    """``data`` field is a plain ASCII str (not bytes), so it survives
    JSON serialisation in the SDK envelope.
    """
    payload = b"\x00\x01\x02\x03" * 10
    block = build_image_content_block(payload)
    assert isinstance(block["source"]["data"], str)
    block["source"]["data"].encode("ascii")  # must not raise


def test_jpeg_quality_constant_matches_research() -> None:
    """RQ2: JPEG quality=85 is the Anthropic vision sweet spot."""
    assert JPEG_QUALITY == 85


def test_resize_long_edge_aligned_to_28() -> None:
    """RQ2 risk #1: avoid Anthropic server-side padding."""
    assert RESIZE_LONG_EDGE % 28 == 0
