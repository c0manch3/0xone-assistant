"""Phase 6b fix-pack F7 — exercise the real libheif decode path.

Researcher RQ4 explicitly recommended a "16x16 HEIC fixture committed to
repo". We use Approach 1 (in-test generation, no committed binary) —
``pillow-heif`` itself encodes a 16x16 HEIC payload at test time, then
``load_and_normalize`` decodes via the same plugin. This exercises the
actual libheif round-trip without committing an opaque binary.

Covers:

* ``load_and_normalize`` round-trip on a real HEIC stream → JPEG output.
* HEIC decode honours the resize-to-1568-max-edge rule (tested via a
  smaller fixture; production photos are typically 4032x3024 — the same
  thumbnail code path runs regardless of source dimensions).
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pillow_heif  # type: ignore[import-untyped]
import pytest
from PIL import Image

from assistant.files.vision import (
    RESIZE_LONG_EDGE,
    detect_image_kind,
    load_and_normalize,
    validate_magic_bytes,
)


def _write_heic(path: Path, *, size: tuple[int, int] = (16, 16)) -> Path:
    """Encode a tiny HEIC payload and write it to ``path``.

    pillow-heif registers a HEIF save plugin under the ``HEIF`` format
    name. The encoded byte stream begins with the standard
    ``ftyp`` + brand magic our magic-byte validator recognises.
    """
    pillow_heif.register_heif_opener()
    img = Image.new("RGB", size, color="red")
    buf = BytesIO()
    img.save(buf, format="HEIF")
    path.write_bytes(buf.getvalue())
    return path


def test_heic_magic_bytes_recognised(tmp_path: Path) -> None:
    """Sanity: a real pillow-heif-generated HEIC stream passes the
    12-byte magic guard for both ``.heic`` and ``.heif`` declared
    suffixes.
    """
    p = _write_heic(tmp_path / "tiny.heic")
    head = p.read_bytes()[:12]
    assert detect_image_kind(head) == "heic"
    # Both suffix aliases accepted (F10 — HEIF is the format,
    # HEIC is one of its brands).
    validate_magic_bytes(p, "heic")
    validate_magic_bytes(p, "heif")


def test_heic_round_trip_load_and_normalize(tmp_path: Path) -> None:
    """Real libheif decode → resize → JPEG re-encode round-trip."""
    p = _write_heic(tmp_path / "tiny.heic")
    out = load_and_normalize(p)

    assert out.startswith(b"\xff\xd8\xff"), "output must be a JPEG"
    # Decode the output to confirm it is a valid JPEG and dimensions
    # are sane for a 16x16 source (no upscaling).
    decoded = Image.open(BytesIO(out))
    assert decoded.format == "JPEG"
    assert decoded.size == (16, 16)


def test_heic_decode_resizes_to_1568_max_edge(tmp_path: Path) -> None:
    """A HEIC source larger than ``RESIZE_LONG_EDGE`` is downscaled
    so the long edge becomes ``RESIZE_LONG_EDGE``; aspect ratio is
    preserved.
    """
    # 2000x1000 HEIC source. pillow-heif encodes any RGB image; the
    # subsequent ``load_and_normalize`` will trigger ``thumbnail`` to
    # produce 1568x784 (long edge clamps; short edge scales to half
    # of 1568, i.e. 784).
    p = _write_heic(tmp_path / "wide.heic", size=(2000, 1000))
    out = load_and_normalize(p)

    decoded = Image.open(BytesIO(out))
    assert decoded.format == "JPEG"
    assert max(decoded.size) == RESIZE_LONG_EDGE
    # 2:1 aspect ratio preserved (within rounding).
    long_edge, short_edge = max(decoded.size), min(decoded.size)
    assert pytest.approx(long_edge / short_edge, rel=0.02) == 2.0
