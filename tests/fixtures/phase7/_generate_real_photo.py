"""Generate a real-entropy JPEG ≥3 MB for phase-7 multimodal tests (C-2).

Rationale
---------
Null-padded / solid-colour JPEGs compress to tens of kilobytes regardless
of declared dimensions; the Spike 0 padded-COM variants used that trick
to stress the 10 MB inline cap. The real-photo test (C-2) must exercise
the SDK's multimodal path with a payload whose HTTP/2-HPACK + gzip wire
size is close to its on-disk size (~7.5 bits/byte entropy). We generate
a 4000x3000 image at JPEG quality 92 whose every pixel is sampled from
`random.Random(seed)` with a pinch of low-frequency structure (bands,
checker, gradient) so it doesn't merely look like noise — entropy stays
> 7 bits/byte and JPEG can't shrink it below the 3 MB threshold.

Determinism
-----------
Seeded PRNG + constant Pillow version pin (>=10.4,<13 from the root
pyproject.toml) keeps the byte-exact output stable across CI runs; if
Pillow ever changes its JPEG encoder defaults a regenerated fixture
will still satisfy the ≥3 MB contract but may shift hash. Tests read
`.stat().st_size` rather than hashing so that drift is harmless.

Regenerate with:
    uv run python tests/fixtures/phase7/_generate_real_photo.py
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

WIDTH = 2048
HEIGHT = 1536
SEED = 0xC0FFEE
MIN_BYTES = 3 * 1024 * 1024  # 3 MB floor per C-2
# Target around 3.5-4.5 MB -- comfortably above floor, well below the
# 10 MB photo_download_max_bytes cap, and small enough to commit.


def _generate(out_path: Path) -> None:
    rng = random.Random(SEED)
    img = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = img.load()
    assert pixels is not None
    # Row-by-row PRNG fill with low-frequency structure (sinusoidal
    # bands in G, gradient in B) — prevents JPEG's DCT from finding a
    # low-rank representation that crushes the file below 3 MB.
    for y in range(HEIGHT):
        band_g = int(127 + 90 * ((y * 7) % 256 / 256 - 0.5))
        for x in range(WIDTH):
            r = rng.randint(0, 255)
            # band_g mixed with noise so variance stays high
            g = (band_g + rng.randint(-50, 50)) & 0xFF
            b = ((x * 256) // WIDTH + rng.randint(-40, 40)) & 0xFF
            pixels[x, y] = (r, g, b)

    # Overlay a checkerboard + thick diagonals so low-pass smoothing in
    # the JPEG encoder has sharp edges to preserve — another entropy
    # floor.
    draw = ImageDraw.Draw(img)
    tile = 40
    for yy in range(0, HEIGHT, tile):
        for xx in range(0, WIDTH, tile):
            if ((xx // tile) + (yy // tile)) % 2 == 0:
                draw.rectangle(
                    (xx, yy, xx + tile // 4, yy + tile // 4),
                    fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)),
                )
    for _k in range(20):
        x0 = rng.randint(0, WIDTH)
        y0 = rng.randint(0, HEIGHT)
        x1 = rng.randint(0, WIDTH)
        y1 = rng.randint(0, HEIGHT)
        draw.line(
            (x0, y0, x1, y1),
            fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)),
            width=6,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # quality=92 empirically keeps a 4000x3000 noisy-image JPEG around
    # 4-5 MB -- comfortably above the 3 MB floor. subsampling=0 (4:4:4)
    # disables chroma subsampling, a further entropy preserver.
    img.save(out_path, format="JPEG", quality=92, subsampling=0, optimize=False)
    size = out_path.stat().st_size
    if size < MIN_BYTES:
        raise SystemExit(
            f"generated fixture {out_path} is {size} B, below 3 MB floor — "
            "bump WIDTH/HEIGHT or JPEG quality"
        )
    print(f"wrote {out_path} ({size} bytes)")


if __name__ == "__main__":
    out = Path(__file__).parent / "real_photo_3mb.jpg"
    _generate(out)
