"""Phase 6b — image vision pre-processing pipeline.

Owner sends image (jpeg/png/webp/heic) via Telegram; this module:

1. Validates 12-byte magic against the declared kind (suffix).
2. Loads via Pillow with ``Image.MAX_IMAGE_PIXELS = 25_000_000`` cap
   (image-bomb guard).
3. Applies EXIF orientation, downscales to max edge ``RESIZE_LONG_EDGE``
   (1568, multiple of 28 to avoid Anthropic server-side padding).
4. Drops EXIF metadata + alpha channel.
5. Re-encodes JPEG quality=85.
6. Returns base64-encoded content block ready for the multimodal envelope.

Module import side-effect: registers the pillow-heif opener once
(idempotent; subsequent imports are no-ops). HEIC decode is lazy — the
opener does not pre-load libheif.

CVE risk for libheif (CVE-2025-68431 etc.) is accepted under the
single-user trust model; mitigation is the 25 MP ceiling + timely
``pillow-heif`` floor in ``pyproject.toml``.
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener  # type: ignore[import-untyped]

from assistant.logger import get_logger

log = get_logger("files.vision")

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# 25 MP cap on the DECODED image (post-magic). Pillow's default is
# 178 956 970 (~179 MP); we tighten to keep peak RSS bounded for the
# single-user VPS budget. Triggers ``Image.DecompressionBombError`` on
# the first ``load()``.
MAX_DECODED_PIXELS = 25_000_000

# Resize target — long edge in pixels. 1568 = 28 * 56 (multiple of 28
# avoids server-side padding cost; see plan/phase6b/research.md RQ2).
RESIZE_LONG_EDGE = 1568

# JPEG output quality. 85 is the Anthropic vision sweet spot
# ("Avoid heavy compression… especially when multiple compression
# passes are applied"). Do not drop below.
JPEG_QUALITY = 85

# 12-byte magic table. Order matters — HEIC sub-brands must follow
# the ``ftyp`` prefix branch.
_HEIC_BRANDS = (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"heim")

# Suffix → media_type for the Anthropic content block. Output is
# always re-encoded to JPEG (HEIC/PNG/WEBP all become ``image/jpeg``)
# so the constant is fixed.
OUTPUT_MEDIA_TYPE = "image/jpeg"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VisionError(Exception):
    """Raised on any pre-processing failure for an image attachment.

    F3 fix-pack: restructured into a typed-kind exception so the handler
    can produce format-specific Russian replies (spec AC#6 — ``"файл не
    похож на JPEG"`` rather than the generic ``"файл не похож на
    изображение"``).

    Attributes:

    * ``kind`` — one of ``"magic_mismatch"``, ``"image_too_large"``,
      ``"corrupt"``. Drives the Russian reply branch.
    * ``declared`` — owner-supplied suffix (``"jpg"``, ``"png"``, …)
      when applicable; ``None`` for ``image_too_large`` / ``corrupt``.
    * ``detected`` — magic-byte detected kind when relevant; ``None``
      otherwise.

    Mirrors :class:`assistant.files.extract.ExtractionError` so the
    handler quarantines vision failures via the same path.
    """

    def __init__(
        self,
        msg: str,
        *,
        kind: str = "corrupt",
        declared: str | None = None,
        detected: str | None = None,
    ) -> None:
        super().__init__(msg)
        self.kind = kind
        self.declared = declared
        self.detected = detected


# ---------------------------------------------------------------------------
# Module-level setup
# ---------------------------------------------------------------------------

# Tighten Pillow's bomb guard BEFORE any Image.open runs.
Image.MAX_IMAGE_PIXELS = MAX_DECODED_PIXELS

# Register the HEIC opener once. ``register_heif_opener`` is idempotent
# (Pillow's plugin registry de-dupes) so re-imports during testing are
# safe. ``thumbnails=False`` skips the embedded-thumbnail decoder we
# don't use — small but real RAM saving.
register_heif_opener(thumbnails=False)


# ---------------------------------------------------------------------------
# Magic-byte validation
# ---------------------------------------------------------------------------


def detect_image_kind(head: bytes) -> str | None:
    """Return one of ``{'jpg','png','webp','heic'}`` or ``None``.

    ``head`` must be at least 12 bytes; caller is responsible.
    Returns ``None`` for unknown / malformed magic.
    """
    if len(head) < 12:
        return None
    # JPEG: FF D8 FF.
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    # PNG: 89 50 4E 47 0D 0A 1A 0A.
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    # WEBP: "RIFF" .... "WEBP".
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    # HEIC: bytes 4..7 = "ftyp"; bytes 8..11 = brand.
    if head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS:
        return "heic"
    return None


def validate_magic_bytes(path: Path, expected_kind: str) -> None:
    """Read 12 bytes from ``path`` and assert the magic matches
    ``expected_kind``.

    ``expected_kind`` is the lower-case suffix without leading dot
    (one of ``{'jpg','jpeg','png','webp','heic','heif'}``). The
    ``jpg``/``jpeg`` and ``heic``/``heif`` aliases are honored
    (HEIF is the container format; HEIC is one of its brands —
    Apple iOS sometimes writes the ``.heif`` suffix on the same byte
    stream).

    Raises :class:`VisionError` on mismatch, short-read, or OSError.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
    except OSError as exc:
        raise VisionError(
            f"corrupt or unreadable: {exc}",
            kind="corrupt",
            declared=expected_kind,
        ) from exc

    if len(head) < 12:
        raise VisionError(
            "corrupt: file shorter than 12 bytes",
            kind="corrupt",
            declared=expected_kind,
        )

    detected = detect_image_kind(head)
    if detected is None:
        raise VisionError(
            f"magic mismatch: not a recognised image format (suffix={expected_kind})",
            kind="magic_mismatch",
            declared=expected_kind,
            detected=None,
        )

    expected_normalised = expected_kind.lower()
    if expected_normalised == "jpeg":
        expected_normalised = "jpg"
    if expected_normalised == "heif":
        expected_normalised = "heic"
    if detected != expected_normalised:
        raise VisionError(
            f"magic mismatch: declared={expected_kind} actual={detected}",
            kind="magic_mismatch",
            declared=expected_kind,
            detected=detected,
        )


# ---------------------------------------------------------------------------
# Decode + normalise pipeline
# ---------------------------------------------------------------------------


def load_and_normalize(path: Path) -> bytes:
    """Decode an image, downscale, drop EXIF + alpha, re-encode JPEG.

    Pipeline (research.md RQ4 — peak RSS approx. width * height * 4 for
    HEIC/PNG/WebP; ~50 MB worst case on a 12 MP HEIC):

    1. ``Image.open`` (lazy).
    2. ``ImageOps.exif_transpose`` triggers ``load()`` honoring rotation.
    3. ``thumbnail((1568, 1568), LANCZOS)`` — in-place, preserves aspect.
    4. ``convert("RGB")`` — drops alpha (HEIC/PNG carry it).
    5. ``save(format="JPEG", quality=85, exif=b"")`` — strips EXIF /
       APP1 metadata.

    Raises :class:`VisionError` on Pillow open / decode / save failure
    OR ``Image.DecompressionBombError`` (image-bomb guard).
    """
    try:
        with Image.open(path) as src_im:
            try:
                # exif_transpose triggers a full decode internally —
                # the bomb guard fires here (DecompressionBombError) if
                # the decoded pixel count would exceed MAX_IMAGE_PIXELS.
                im: Image.Image = ImageOps.exif_transpose(src_im) or src_im
            except Image.DecompressionBombError as exc:
                raise VisionError(
                    f"image too large: {exc}", kind="image_too_large"
                ) from exc
            except Exception as exc:
                # Pillow can raise OSError / SyntaxError / ValueError
                # for malformed files — coalesce.
                raise VisionError(
                    f"corrupt: cannot decode image: {exc}", kind="corrupt"
                ) from exc

            try:
                im.thumbnail(
                    (RESIZE_LONG_EDGE, RESIZE_LONG_EDGE),
                    Image.Resampling.LANCZOS,
                )
            except Exception as exc:
                raise VisionError(
                    f"corrupt: thumbnail failed: {exc}", kind="corrupt"
                ) from exc

            if im.mode != "RGB":
                im = im.convert("RGB")

            buf = BytesIO()
            try:
                im.save(
                    buf,
                    format="JPEG",
                    quality=JPEG_QUALITY,
                    optimize=True,
                    exif=b"",
                )
            except Exception as exc:
                raise VisionError(
                    f"corrupt: JPEG encode failed: {exc}", kind="corrupt"
                ) from exc
            return buf.getvalue()
    except VisionError:
        raise
    except Image.DecompressionBombError as exc:
        # Belt-and-suspenders: some pillow versions raise the bomb
        # error from the ``Image.open`` path even before ``load()``.
        raise VisionError(
            f"image too large: {exc}", kind="image_too_large"
        ) from exc
    except OSError as exc:
        # ``Image.open`` raises OSError ("cannot identify image file")
        # for unknown formats or truncated reads.
        raise VisionError(f"corrupt: {exc}", kind="corrupt") from exc


def build_image_content_block(jpeg_bytes: bytes) -> dict[str, Any]:
    """Wrap base64-encoded JPEG bytes into the Anthropic content-block dict.

    Output shape (verified by RQ0 spike, plan/phase6b/spikes/rq0_multimodal):

    .. code-block:: python

        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "<base64 ascii>",
            },
        }
    """
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": OUTPUT_MEDIA_TYPE,
            "data": base64.standard_b64encode(jpeg_bytes).decode("ascii"),
        },
    }
