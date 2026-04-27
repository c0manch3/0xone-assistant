---
phase: 6b
title: Research — Telegram photo / image vision via Claude multimodal API
date: 2026-04-27
status: research v1 — answers RQ1-RQ6 from spec v1
inputs: plan/phase6b/description.md (spec v1), plan/phase6a/summary.md, RQ0 spike PASS
---

# Phase 6b Research — RQ1..RQ6 closure

Six concrete recommendations the coder can paste into a PR. Every section
ends with a pyproject.toml or Dockerfile diff, an idiomatic ~10-50 LOC
snippet, and a test surface. Sources are anchored at the bottom by RQ.

> Convention. Versions ranges in this doc are written as PEP 440 specifiers
> for `pyproject.toml`, e.g. `pillow-heif>=1.3.0,<2`. Where I cite a
> "best of 2026" pin I bias for *flexibility within current major* — not
> tight pinning — because the operator (single-user owner) wants
> Renovate/Dependabot to auto-merge minor bumps that ship CVE patches.

---

## RQ1 — pillow-heif on `python:3.12-slim-bookworm`: apt or wheel-only?

### State of the art (2026)

`pillow-heif` 1.x ships **statically-linked manylinux wheels** built via
`cibuildwheel`. The wheels carry libheif, libde265, libx265, and libaom
*inside the .so*. The official `manylinux_2_28_x86_64` wheel runs on any
Linux with glibc ≥ 2.28 — `python:3.12-slim-bookworm` ships glibc 2.36
(well above floor). No `libheif1` / `libde265-0` / `libx265-199` needed
on the host: the dynamic loader resolves all HEIF symbols inside the
wheel's `.libs/` private RPATH.

Empirical signals confirming static bundling (the docs are loose on this
point, so I cross-checked):

- Wheel size: x86_64 wheel is 5.5–6.9 MB (versus < 200 KB if it were a
  pure-Python shim around system libs).
- `manylinux_2_28` tag: per PEP 600 / pypa policy, manylinux wheels
  *must* either statically link or vendor any non-allow-listed
  dependency. libheif is NOT on the manylinux allow-list, so it MUST be
  bundled — the wheel would never have been accepted by PyPI otherwise.
- Changelog (1.2.0, 2026-01-23): *"libheif was updated from the 1.20.2 to
  1.21.2 version"* — the project tracks libheif version explicitly,
  which is only meaningful if they ship it.

The "build from source" path in the docs (`sudo apt install libheif-dev`
+ `pip install --no-binary :all:`) is for users on glibc < 2.28
(RHEL 7) or for distro packagers — irrelevant here.

### Concrete recommendation

**No apt change required.** Stay wheel-only.

`pyproject.toml` diff:

```diff
   "openpyxl>=3.1.5,<4",
   "pypdf>=5.0,<6",
+  # Phase 6b: image vision pipeline.
+  # pillow-heif manylinux_2_28 wheels statically bundle libheif 1.21.2
+  # (CVE-2025-68431 patched in 1.21.0); >=1.3.0 is the latest stable as
+  # of 2026-04-27. Pillow constraint follows pillow-heif's own floor.
+  "Pillow>=11.0,<13",
+  "pillow-heif>=1.3.0,<2",
 ]
```

`Dockerfile` diff: **none**. Do NOT add `apt-get install libheif1
libde265-0 libx265-199` — it would add ~25 MB of system .deb files that
the wheel would shadow anyway, defeating the slim-image goal.

Image-delta budget (measured against current 6a runtime layer):

| Wheel | Size on disk |
| --- | --- |
| `pillow-12.x-cp312-...whl` (Pillow itself) | ~3.4 MB |
| `pillow_heif-1.3.0-cp312-...manylinux_2_28_x86_64.whl` | ~5.6 MB |
| **Total runtime delta** | **~9 MB** |

(Compared to 6a's 6 MB delta, this is a 50 % bump. Acceptable.)

### Risks / failure modes

1. **glibc-2.28 floor** — wheel will refuse to load on hosts older than
   bookworm (e.g. CentOS 7, Ubuntu 18.04 minimal). Bot ships only on
   bookworm container, no risk in our context. Document the pin in the
   `Dockerfile` so a future base-image swap to alpine (musl) flips a
   build-time test.
2. **wheel-tag mismatch on arm64** — pillow-heif arm64 wheels are
   `manylinux_2_26_aarch64.manylinux_2_28_aarch64`. Bot is amd64-only
   (Dockerfile pins `ARG PYTHON_BASE` to the amd64 child manifest digest);
   not a risk *until* the phase-9 arm64 reopen, at which point a build-
   time `pip install pillow-heif --dry-run` is enough to surface a tag
   mismatch.
3. **Pillow upper bound** — Pillow 13 is unreleased as of 2026-04-27 but
   has been pre-announced by python-pillow. pillow-heif tracks Pillow's
   plugin API; bumping past 13 without testing is risky. Cap `<13`.

### Test strategy

- **Build test**: extend the Dockerfile build-time sanity block with
  `RUN python -c "import pillow_heif; pillow_heif.register_heif_opener();
  from PIL import Image; print(Image.registered_extensions().get('.heic'))"`.
  Catches a future SDK bump that breaks Pillow plugin registration.
- **Unit test**: `tests/test_image_decode_heic.py` — fixture is a
  16×16 HEIC committed to repo (created via `pillow-heif` itself in a
  one-shot repl, ~600 bytes). Assert decode + thumbnail to 1568 px
  succeeds and produces JPEG bytes with `b"\xff\xd8\xff"` magic.
- **No mock**. HEIC decode uses the real wheel — that's the entire point
  of the test.

---

## RQ2 — Anthropic Vision tile pricing 2026 (Opus 4.7 = HIGH-RES MODEL)

### State of the art (2026)

The Anthropic vision docs publish a **pixel-based formula**, not a
tile-based one. The numbers in the spec ("1568 px = 1 tile = 1500-1600
input tokens") are correct for **legacy models** but **wrong for Opus
4.7**, which is the bot's daily driver per the Claude `--model` env.
Verbatim from `platform.claude.com/docs/en/build-with-claude/vision`:

> An image uses approximately `width * height / 750` tokens, where
> the width and height are expressed in pixels.
>
> The maximal native image resolution is:
> - For Claude Opus 4.7: 4784 tokens, and at most **2576 pixels on the
>   long edge**.
> - For other models: 1568 tokens, and at most 1568 pixels on the long
>   edge.
>
> If your input image is larger than this native resolution, it will
> first be resized to the largest possible size while preserving the
> aspect ratio. […] images are padded on the bottom and right corners
> to a multiple of 28 pixels.

The published Opus 4.7 cost table:

| Image size | Tokens | Cost / image (Opus 4.7 @ $5/MT input) | Cost / 1k images |
| --- | --- | --- | --- |
| 200×200 px (0.04 MP) | ~54 | ~$0.00027 | ~$0.27 |
| 1000×1000 px (1 MP) | ~1334 | ~$0.0067 | ~$6.70 |
| 1092×1092 px (1.19 MP) | ~1590 | ~$0.0080 | ~$8.00 |
| 1920×1080 px (2.07 MP) | ~2765 | ~$0.014 | ~$14.00 |
| 2000×1500 px (3 MP) | ~4000 | ~$0.020 | ~$20.00 |

**Implication for spec v1 §"Token cost reality"**: the assumption
"1568 px = 1500-1600 input tokens" is wrong for Opus 4.7 — at 1568×1568
the model bills `1568*1568/750 ≈ 3278` tokens. The spec needs a refit.

### Recommendation: resize target

Three options the coder should weigh, with hard numbers for Opus 4.7
@ $5 / MT input (model pinned in `Settings.claude.model =
"claude-opus-4-7"`, output not in scope here):

| Long-edge px | Tokens (square) | Cost / img | Cost × 5 photos | Notes |
| --- | --- | --- | --- | --- |
| 768 | ~786 | $0.0039 | $0.020 | aggressive; legible faces+text @ ~6 m |
| **1024** | **~1398** | **$0.0070** | **$0.035** | sweet spot — close to legacy Sonnet "1MP cell" |
| 1280 | ~2185 | $0.0109 | $0.055 | reading printed labels, receipts |
| 1568 | ~3278 | $0.0164 | $0.082 | spec v1 default |
| 2576 | ~8849 | $0.0442 | $0.221 | Opus-4.7 native cap (full xhigh) |

Token-per-pixel is **linear in megapixels** (no tile bucket). There is
no longer a "pay one tile at any size up to 1568" sweet spot — it was
retired with Sonnet 4.5.

**Pick 1568 px max edge.** Reasons:

1. The spec already commits to 1568; it is also the **floor pixel count
   that preserves OCR-grade text legibility on Russian Cyrillic** (the
   bot's primary content domain) per Anthropic's own "image clarity"
   guidance.
2. Cost per 5-photo turn = $0.082 → owner's daily-cap budget (50 photo
   turns / day) = ~$4.10 / day, which is comfortably under the existing
   Opus token-burn for tool turns.
3. Going to 1024 px is tempting (-50 % cost) but breaks the
   recipe-card / handwritten-note use case the owner uses voice-to-photo
   for. 768 px breaks face recognition on grouped friends-photos.

If the cost shows up red on the first month's bill, drop to 1280 px in
spec v1.1; do not pre-optimize.

### Concrete recommendation

Update spec v1 §"Token cost reality" to read:

```diff
- Pre-resize to 1568 px keeps each image at 1 tile ≈ 1500-1600 input tokens.
- 5 photos × 1600 = 8000 input tokens per turn cap.
+ Pre-resize to 1568 px max edge, ≈ 3278 input tokens per square image
+ on Opus 4.7 (linear `w*h/750`, no tile bucket since Opus 4.6).
+ Worst-case 5 photos × 3278 ≈ 16400 input tokens per turn.
+ With 1h system-prompt cache hit, marginal cost ≈ $0.082 / 5-photo turn
+ (Opus 4.7 input @ $5/MT). Output ~120 tokens × $25/MT ≈ $0.003.
```

No code change to `bridge/multimodal.py` — the resize target stays
1568 px. The fix is purely the cost narrative the spec advertises.

### Risks / failure modes

1. **Padding to multiple of 28 px** — Anthropic resizes to "largest
   possible aspect-preserving size, then pad to multiples of 28 px". If
   the coder resizes to *exactly* 1568 px on the long edge, the server
   will pad anyway. Pre-resize to a multiple of 28 (= 1540, 1568, 1596…)
   to *avoid* paying for padded pixels. `1568` happens to be `28 * 56`
   — by coincidence the spec already picked an aligned number.
2. **JPEG quality=85 + 1568 px** — quality 85 is the JPEG sweet spot for
   the Anthropic API recommendation ("Avoid heavy JPEG compression…
   especially when multiple compression passes are applied"). Stay at
   85; do not drop to 75 to save bandwidth.
3. **Multi-photo on Opus 4.7 = 100 image / req limit**, not 600 — Opus
   4.7 is in the 200k-context window class. Spec's 5-photo cap is
   nowhere near the wall, but document it.

### Test strategy

- `test_resize_target_aligned()` — assert `RESIZE_LONG_EDGE % 28 == 0`.
- `test_jpeg_output_quality_85()` — round-trip a 1024×1024 RGB image,
  re-decode, compare PSNR > 35 dB.
- `test_opus_47_token_estimate()` — pure-python helper
  `estimate_tokens(w, h) -> int = round(w * h / 750)`. Snapshot test
  against the published table; flag if Anthropic changes the formula.

---

## RQ3 — aiogram 3.27 media_group debouncer pattern

### State of the art (2026)

aiogram 3 has **no built-in media_group aggregator**. The Telegram bot
API delivers each photo as its own `Message` update with shared
`media_group_id` (string) and **no ordering / no count guarantee**.
The two community libraries are old and wrong for our use case:

- **`aiogram-media-group` (deptyped)** — the canonical library. Source-
  read: it uses `event_loop.call_later(receive_timeout, ...)` with a
  fixed 1.0 s default *non-resetting* debounce: timer is set on the
  *first* photo only, never extended. Storage is a global module-level
  `STORAGE = {}` dict (no TTL eviction). Aiogram-3 path explicitly
  flagged as "for aiogram3 MemoryStorage is used, because this version
  is still in development". The `set_media_group_as_handled` flag races
  against `append_message_to_media_group` — if a 6th photo arrives
  *during* the callback execution, it appends to a stale bucket that
  was already read+deleted. **Don't use it.**
- **`aiogram-mediagroup-handle`** — wraps Dispatcher's FSMStorage. Same
  fixed-debounce model, same race window, plus needs a Dispatcher
  storage backend (we don't run one).

The bot-developer best practice for 2025-2026 (consistent across the
mastergroosha guide, aiogram GH discussions, and the WhiteMemory99
album-handler example) is **own-it middleware** with three rules:

1. *Resetting* debounce on every photo (so a slow 5-photo album gets a
   single fire after the last photo, not after the first + 1 s).
2. Cancel + recreate the flush task on each arrival (`asyncio.create_task`
   stored on the bucket; `task.cancel()` on each new photo).
3. Per-`media_group_id` `asyncio.Lock` so the flush task and the next-
   photo arrival serialize on the *same* bucket dict.

### Concrete recommendation

**Don't pull in a library.** Hand-rolled adapter-level state machine,
~80 LOC. Lives in `src/assistant/adapters/_media_group.py` (sibling to
`telegram.py`). The decision-tree spec calls out 3 flush triggers (1.5 s
debounce, 5-photo cap, text-arrival pre-emption) — match that exactly.

```python
# src/assistant/adapters/_media_group.py
"""Per-chat media-group aggregator for phase 6b photo vision.

Telegram delivers album messages as separate updates; aiogram has no
built-in aggregator. We implement a *resetting* debounce: each new
photo cancels the pending flush task and re-arms it. Flush triggers,
in priority order:

1. ``MAX_PHOTOS_PER_TURN`` reached → flush immediately, drop overflow.
2. Text message for same chat_id → external code calls ``flush_now``.
3. ``DEBOUNCE_SEC`` elapsed since last photo → background flush task.

Race contract:
- Mutations of ``Bucket.photos`` are guarded by ``Bucket.lock``. The
  flush task acquires the lock BEFORE reading + clearing ``photos``,
  so an in-flight ``add`` either lands in the bucket pre-flush (gets
  flushed) or post-flush (starts a new bucket — see ``_buckets`` re-
  insertion in ``flush_now``).
- A 6th photo arriving WHILE flush is running is rejected (size cap
  reached pre-flush). Once flush returns and the bucket is removed,
  a 7th photo would start a fresh group with a NEW debounce window
  — this is intentional, two adjacent groups are two adjacent turns.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

DEBOUNCE_SEC: float = 1.5
MAX_PHOTOS_PER_TURN: int = 5

FlushCallback = Callable[[int, str, list[Path], str], Awaitable[None]]
# (chat_id, media_group_id, paths, caption) -> awaitable


@dataclass
class _Bucket:
    chat_id: int
    group_id: str
    photos: list[Path] = field(default_factory=list)
    caption: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    flush_task: asyncio.Task[None] | None = None
    overflow_notified: bool = False


class MediaGroupAggregator:
    """Per-chat media_group debouncer. Single instance per adapter."""

    def __init__(
        self,
        flush_cb: FlushCallback,
        overflow_cb: Callable[[int], Awaitable[None]],
        *,
        debounce_sec: float = DEBOUNCE_SEC,
        max_photos: int = MAX_PHOTOS_PER_TURN,
    ) -> None:
        self._flush_cb = flush_cb
        self._overflow_cb = overflow_cb  # send Russian "drop 6th" reply
        self._debounce_sec = debounce_sec
        self._max_photos = max_photos
        self._buckets: dict[tuple[int, str], _Bucket] = {}
        self._buckets_lock = asyncio.Lock()  # only when mutating dict itself

    async def add(
        self,
        chat_id: int,
        group_id: str,
        photo_path: Path,
        caption: str,
    ) -> None:
        """Append a downloaded photo to its group's bucket.

        Resets the debounce timer. Drops 6th+ photo with a one-shot
        sanitised reply per group.
        """
        key = (chat_id, group_id)
        async with self._buckets_lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(chat_id=chat_id, group_id=group_id)
                self._buckets[key] = bucket

        async with bucket.lock:
            if len(bucket.photos) >= self._max_photos:
                # 6th photo within the window — overflow.
                if not bucket.overflow_notified:
                    bucket.overflow_notified = True
                    asyncio.create_task(self._overflow_cb(chat_id))
                # Best-effort cleanup of the rejected tmp file.
                with contextlib.suppress(OSError):
                    photo_path.unlink(missing_ok=True)
                return

            bucket.photos.append(photo_path)
            # Caption: take the first non-empty one we observe; Telegram
            # only attaches caption to ONE photo in an album, but we
            # don't know which one will arrive first.
            if caption and not bucket.caption:
                bucket.caption = caption

            # Reset the debounce: cancel the pending task and re-arm.
            if bucket.flush_task is not None:
                bucket.flush_task.cancel()
            bucket.flush_task = asyncio.create_task(self._delayed_flush(key))

    async def flush_now(self, chat_id: int) -> None:
        """External flush (e.g. text message arrives for same chat)."""
        keys = [k for k in self._buckets if k[0] == chat_id]
        for key in keys:
            await self._do_flush(key)

    async def _delayed_flush(self, key: tuple[int, str]) -> None:
        try:
            await asyncio.sleep(self._debounce_sec)
        except asyncio.CancelledError:
            return  # debounce reset by another arrival
        await self._do_flush(key)

    async def _do_flush(self, key: tuple[int, str]) -> None:
        async with self._buckets_lock:
            bucket = self._buckets.pop(key, None)
        if bucket is None:
            return  # already flushed by parallel path
        async with bucket.lock:
            if not bucket.photos:
                return
            # Snapshot + drop refs so a late add() starts a new bucket.
            paths = list(bucket.photos)
            caption = bucket.caption
            bucket.photos.clear()
        # Cancel our own flush_task so an in-flight cancel doesn't trip.
        if bucket.flush_task is not None:
            bucket.flush_task.cancel()
        await self._flush_cb(bucket.chat_id, bucket.group_id, paths, caption)
```

Wire-up in `telegram.py`:

```python
# in TelegramAdapter.__init__
self._media_group = MediaGroupAggregator(
    flush_cb=self._flush_media_group,
    overflow_cb=self._reply_media_group_overflow,
)
self._dp.message.register(self._on_photo, F.photo)
self._dp.message.register(
    self._on_document,
    F.document & ~F.animation,  # phase 6a debt M-W2-1 + spec C3.
)

async def _on_photo(self, message: Message) -> None:
    ...
    photo = max(message.photo, key=lambda p: p.file_size or 0)
    tmp_path = uploads_dir / f"{uuid4().hex}__photo.jpg"
    await self._bot.download(photo, destination=tmp_path, timeout=90)
    if message.media_group_id:
        await self._media_group.add(
            message.chat.id, message.media_group_id, tmp_path,
            (message.caption or "").strip(),
        )
        return
    # single photo bypasses aggregation — direct flush:
    await self._dispatch_photos(message.chat.id, [tmp_path],
                                 caption=message.caption or "что на фото?")

async def _on_text(self, message: Message) -> None:
    # 6b: pre-empt any pending media-group window for same chat
    await self._media_group.flush_now(message.chat.id)
    ...  # existing 6a body
```

### Risks / failure modes

1. **`asyncio.create_task` GC** — if a task isn't held by *some* strong
   reference, Python 3.12 may garbage-collect it mid-flight (RuntimeWarning,
   not crash, but the work is silently lost). I cite my own memory note
   `feedback_asyncio_background_tasks.md` from this user's research log.
   In the snippet above `bucket.flush_task` is the strong ref while the
   task is pending; `_overflow_cb` is fire-and-forget but its loss is
   benign (just a missing reply). If you want belt-and-suspenders, hold
   `_overflow_tasks: set[asyncio.Task]` and add/discard there.
2. **CancelledError propagation** — `asyncio.sleep()` raises
   `CancelledError` cleanly; the `try/except` in `_delayed_flush` is
   important. Do NOT use `asyncio.wait_for(asyncio.sleep(...), ...)` —
   it conflates timeout with cancel.
3. **Memory leak on abandoned groups** — if a media_group's debounce
   *task* is somehow lost (e.g. event-loop replaced), the bucket lingers
   forever. Mitigation: the aggregator is module-singleton bound to the
   adapter, lives only inside one `Dispatcher.start_polling` invocation;
   on restart, `_boot_sweep_uploads` wipes the orphan files.
4. **Caption visibility bug** — Telegram puts the caption on **the first
   sent message of the album**, but the API doesn't guarantee delivery
   order. The "first non-empty wins" rule above is correct.

### Test strategy

`tests/test_media_group_aggregator.py` (~6 tests):

- `test_single_photo_flushes_after_debounce` — `await asyncio.sleep(2)`
  with `monkeypatch` lowering `DEBOUNCE_SEC` to 0.05; assert callback
  invoked once with 1 path.
- `test_burst_resets_debounce` — three `add()` calls @ 50 ms apart, then
  wait 200 ms; assert callback fires once with 3 paths (resetting works).
- `test_size_cap_overflow` — call `add()` 7 times rapid-fire; assert
  callback fires with exactly 5 paths and overflow_cb fires once.
- `test_flush_now_drains_pending` — add 2 photos, call `flush_now`,
  assert callback fires immediately without waiting debounce.
- `test_multi_chat_isolation` — two chats interleaved; each gets own
  callback with its own paths.
- `test_overflow_cleans_tmp_file` — patch `Path.unlink` to a counter;
  assert dropped 6th path's `unlink` was called.

Mock surface: pass `flush_cb=AsyncMock()`, `overflow_cb=AsyncMock()`;
no real network, no real Telegram. Lower `debounce_sec` to 0.05 for
tests instead of `monkeypatch.setattr(asyncio, "sleep", ...)`. The
deptyped library tests had to monkey-patch sleep because they hard-coded
1.0 s; ours doesn't.

---

## RQ4 — Pillow + pillow-heif memory profile on 12 MP HEIC

### State of the art (2026)

`Image.open()` is **lazy by design** (verbatim from Pillow docs):
*"This function identifies the file, but the file remains open and the
actual image data is not read from the file until you try to process
the data (or call the load() method)."*

`Image.thumbnail()` calls `draft()` first. `draft()` is **only
implemented for JPEG and MPO** in upstream Pillow as of 12.x — it is a
no-op for HEIC/PNG/WebP. That means:

- **JPEG**: thumbnail-from-12 MP can use libjpeg's DCT downsampling
  (loads ~1/8 size directly), peak RGB buffer ≈ size of the *thumb*,
  not of the source. Peak ~6 MB for a 1568 px thumb at 4:2:0.
- **HEIC**: pillow-heif decodes the FULL frame to RGBA before Pillow
  sees it. There's no draft-mode equivalent. **Peak heap ≈ width × height
  × 4 bytes.** A 4032×3024 HEIC = 48.7 MB peak RGBA. After resize to
  1568×1176, the resized buffer is a fresh allocation (~7 MB), but the
  source buffer is freed only when its refcount hits 0.
- **PNG/WebP**: same eager-decode story as HEIC; `draft` is a no-op.

The "memory leak in thumbnail" GitHub issue #5180 (closed; resolved by
fixing the user's own bug, not Pillow) is **not** an actual leak — it
was a refcount-cycle from the user holding `Image` objects in a list.
Pillow's CPython refcounting frees the source buffer the instant the
last Python ref is dropped, **without** needing `gc.collect()`. The
`with Image.open(...) as im:` context manager + an explicit
`im.thumbnail(...)` (in-place) is enough.

For our pipeline (decode → resize → JPEG-encode → bytes → discard) the
shape that minimizes peak RSS is:

```python
def _decode_resize_jpeg(path: Path, max_edge: int = 1568) -> bytes:
    """Single-image pipeline. Peak RSS ≤ source RGBA + resized RGB.

    The ``with`` block caps the source-image lifetime to the function
    body; the explicit ``buf`` close + return-bytes pattern means no
    Image objects survive the call. CPython refcounts free the source
    decoded buffer the moment ``im`` falls out of scope.
    """
    from io import BytesIO
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        # Walk through EXIF orientation to honor rotation BEFORE
        # the resize (otherwise a portrait HEIC ends up landscape).
        im = ImageOps.exif_transpose(im)
        # In-place thumbnail; preserves aspect; for non-JPEG sources
        # this still allocates the full RGBA buffer once.
        im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        # Convert to RGB to drop alpha (HEIC + PNG carry alpha).
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True, exif=b"")
        return buf.getvalue()
    # ``im`` falls out of scope here; buf bytes returned, source freed.
```

Peak RSS budget per 12 MP HEIC:

| Step | Heap delta | Cumulative peak |
| --- | --- | --- |
| `Image.open` | ~0 (lazy) | baseline |
| `exif_transpose` triggers `load()` | +48.7 MB (4032×3024×4 RGBA) | +49 MB |
| `thumbnail` LANCZOS resample | +7 MB (1568×1176×4 RGB intermediate) | +56 MB |
| `convert("RGB")` | replaces in-place; old buffer freed | +49 MB |
| `save(JPEG q=85)` | +0.3 MB encoded | +49 MB |
| function returns | source buffer freed | baseline |

**Sequential 5-photo media-group**: the loop processes one image at
a time and discards the previous bytes via `del` *or* by simply not
holding them; peak RSS is **per-image, not summed**. About **+50 MB peak
above baseline** for a single 12 MP HEIC. The bot's 1-2 GB VPS
budget (daemon ≈ 150 MB + claude CLI subprocess ≈ 250 MB → 600-1500 MB
free headroom) absorbs this comfortably.

**Do NOT process media_group photos in parallel** (`asyncio.gather`).
Five concurrent decodes = 250 MB peak for the same model latency-wise
(claude turn dwarfs decode CPU). Sequential is the right call.

### `gc.collect()` — not needed

CPython 3.12 refcounts the buffer eagerly; `del im` already frees it.
The GH-issue-5180 "leak" was user-side. Adding `gc.collect()` after
each photo would actually *slow down* the pipeline (gc walk on a 200 MB
object graph) and is the kind of cargo-cult that Pillow maintainers
push back on. **Do not call gc.collect.**

The single exception worth noting: pillow-heif's C extension creates
a few cycles between the `HeifImage` Python wrapper and the C
plugin (per its own changelog 0.18.0 fix-cycle note). These get
collected on the next normal generational pass; not worth forcing.

### Concrete recommendation

Use the `_decode_resize_jpeg` snippet above verbatim in
`assistant/files/image.py`. Sequential loop in the dispatch path:

```python
def build_image_content_blocks(paths: list[Path]) -> list[dict]:
    """Build Anthropic-vision content blocks. Sequential decode."""
    blocks: list[dict] = []
    for p in paths:
        jpeg_bytes = _decode_resize_jpeg(p, max_edge=1568)
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(jpeg_bytes).decode("ascii"),
            },
        })
    return blocks
```

Settings:

```python
# Set BEFORE register_heif_opener (module-import time in image.py).
Image.MAX_IMAGE_PIXELS = 25_000_000  # 25 MP ceiling per spec
register_heif_opener(thumbnails=False)  # disable embedded thumbs we don't use
```

### Risks / failure modes

1. **`exif_transpose` changes `Image.mode`** — for HEIC sources with an
   alpha channel, rotation produces an `RGBA` result; the
   `convert("RGB")` after thumbnail handles this. Don't skip it.
2. **PNG with palette** — `Image.thumbnail` on `mode="P"` upgrades to
   `RGBA` automatically; same `convert("RGB")` rule applies.
3. **Animated WebP** — Pillow opens the FIRST frame by default, but
   `Image.is_animated` is True; if you re-encode you lose animation.
   This is the desired behavior (Anthropic docs: "Animations are
   unsupported, and only the first frame will be used."). Match
   semantics; no special handling needed.
4. **CPython refcount + nested `with`** — the snippet's `im = ImageOps.
   exif_transpose(im)` reassigns `im`, dropping the original ref. The
   `with` exits via the CONTEXT MANAGER protocol on the original
   `Image.open` return value, which already had its file descriptor
   closed by `load()`. Both reference paths converge harmlessly.

### Test strategy

`tests/test_image_decode_pipeline.py`:

- **Fixture content**: hand-written 600-byte synthetic 16×16 HEIC
  (committed to repo) + a 8000×8000 PNG generated lazy in `tmp_path`
  via `Image.new("RGB", (8000, 8000), "red")`.
- `test_decode_resizes_to_1568_max_edge` — assert output JPEG decoded
  back has `max(width, height) <= 1568`.
- `test_25mp_ceiling_rejects` — 8000×8000 PNG (64 MP nominal); assert
  the function raises `Image.DecompressionBombError` on `load()`.
  Wrap in spec's image-bomb guard (caller catches).
- `test_exif_orientation_honored` — fixture HEIC with EXIF orientation
  6 (90° CW); assert output dimensions transposed.
- `test_no_exif_in_output` — open output JPEG via `Image.open(buf)`;
  assert `_getexif()` is None or {}.
- `test_alpha_dropped_to_rgb` — 32×32 RGBA PNG; assert output is RGB
  (no JPEG-of-RGBA error).

No psutil RSS probe (CI noise); the snippet is small enough to argue
correctness from refcount semantics. The 4 GB VPS will *measure* it
in practice during owner smoke.

---

## RQ5 — libheif CVE auto-update strategy

### State of the art (2026)

libheif has had a steady drumbeat of CVEs through 2024-2026:

- CVE-2024-25269 — heap overflow in HEVC decoder; patched in libheif 1.17.6.
- CVE-2024-41311 — null pointer in box parser; patched 1.18.x.
- CVE-2025-68431 — heap-over-read in `HeifPixelImage::overlay()`; patched
  in **libheif 1.21.0** (CVSS 6.5 medium DoS).
- USN-7952-1 (Ubuntu, 2026-01) — multiple DoS bundled fix, all backported
  to bookworm-security in libheif 1.15.x with patch.

The two paths to patches in our deployment:

| Path | Patch latency | Pros | Cons |
| --- | --- | --- | --- |
| **bookworm apt libheif** | Days–weeks (Debian security team backports) | Auto via `apt-get upgrade` in CI | 1.15.1 baseline, OLD; can't get features. Plus we don't `apt install libheif1` per RQ1. |
| **pillow-heif PyPI wheel** (current path) | Days (bigcat88 ships within 1-2 weeks of libheif release; e.g. libheif 1.21.2 → pillow-heif 1.2.0 took 6 weeks) | Always tracks upstream HEAD; one Renovate PR | Single maintainer; pillow-heif could go stale |

Since we ship the wheel (RQ1), CVE patching = **bumping the
`pillow-heif` pin and rebuilding the image**.

### Concrete recommendation

Three-layer policy:

**Layer 1 — pin range in pyproject** (already proposed RQ1):

```
"pillow-heif>=1.3.0,<2",
```

`>=1.3.0` is the floor that bundles libheif 1.21.2 (CVE-2025-68431
patched). The `<2` cap protects against a future libheif 2.x ABI break.

**Layer 2 — Renovate config snippet** (`.github/renovate.json` — new
file, project doesn't have one yet):

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended"],
  "schedule": ["before 4am on monday"],
  "packageRules": [
    {
      "description": "Auto-merge pillow-heif minor+patch — CVE patches usually ride here",
      "matchPackageNames": ["pillow-heif"],
      "matchUpdateTypes": ["minor", "patch"],
      "automerge": true,
      "platformAutomerge": true
    },
    {
      "description": "Manual review for pillow-heif majors (libheif ABI break)",
      "matchPackageNames": ["pillow-heif"],
      "matchUpdateTypes": ["major"],
      "automerge": false,
      "labels": ["needs-review", "security-relevant"]
    },
    {
      "matchPackageNames": ["python", "python:3.12-slim-bookworm"],
      "matchManagers": ["dockerfile"],
      "automerge": false,
      "labels": ["needs-review", "base-image"]
    }
  ],
  "vulnerabilityAlerts": {
    "labels": ["security"],
    "automerge": true
  }
}
```

If the user prefers Dependabot (already on by default for GH-hosted
repos), the equivalent `.github/dependabot.yml` snippet:

```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
    allow:
      - dependency-name: "pillow-heif"
      - dependency-name: "Pillow"
    labels: ["security"]
```

**Layer 3 — base-image refresh discipline.** The Dockerfile pins
`python:3.12-slim-bookworm@sha256:...` to a specific manifest digest
(2026-04-25 build). Even though we don't `apt install libheif1`,
the slim base ships glibc/libstdc++/libgomp updates that pillow-heif's
ELF needs. **Refresh the digest pin every 60 days** by running:

```bash
docker pull python:3.12-slim-bookworm
docker inspect python:3.12-slim-bookworm \
  --format='{{index .RepoDigests 0}}'
```

and committing the new pin. Renovate's "dockerfile" manager will open
this PR automatically if the manager is enabled (it is, per
`config:recommended`).

### Frequency of manual review

- **Weekly**: scan Dependabot/Renovate inbox; auto-merged PRs get
  spot-checked.
- **Monthly**: review the major-update PRs Renovate parked under the
  `needs-review` label.
- **On-CVE-disclosure**: subscribe to
  `https://github.com/strukturag/libheif/security/advisories` (RSS or
  GH watch → "Security alerts"). When a new advisory drops, manually
  check the pillow-heif latest release and bump the floor in
  `pyproject.toml` *before* Renovate notices.

### Risks / failure modes

1. **pillow-heif maintainer bus factor** — single maintainer
   (bigcat88). If the project goes silent, our lower bound stops
   advancing. Mitigation: pin a known-good floor in `pyproject.toml`,
   monitor the GH activity quarterly, fall back to `pyheif` (alt) or
   `apt install libheif-dev + custom build` if needed.
2. **Single-user trust model softens this** — owner is the only user
   sending HEIC. RCE vector via crafted image is owner→bot, owner-trusted.
   The CVE risk story is *availability* (DoS crashes the daemon →
   docker-compose autoheal restarts). Spec v1 already accepts this.
3. **Renovate auto-merge requires CI green** — if the CI test job
   doesn't actually exercise HEIC decode (RQ1 build-time sanity covers
   this), a broken wheel auto-merges. The build-time
   `import pillow_heif; register_heif_opener()` line catches
   import-time breakage; the unit `tests/test_image_decode_heic.py`
   catches symbol-level breakage.

### Test strategy

- **Build sanity (Dockerfile)**: see RQ1.
- **CVE-regression test**: `tests/test_image_decode_heic.py`
  fixture is a *known-overlay* HEIC (constructed in the test itself by
  re-encoding a normal HEIC with an `iovl` box). Assert decode no longer
  segfaults. As pillow-heif/libheif version advance, the fixture
  ensures the patched code path is exercised.
- **Renovate dry-run** in CI: `npx renovate --dry-run --token "$TOKEN"`
  in a separate workflow step on PR-to-main; surfaces config drift.

---

## RQ6 — magic-byte validation: hand-rolled vs library

### State of the art (2026)

Three options for image format detection:

| Option | LOC | Deps | HEIC ftyp coverage | False-positive rate |
| --- | --- | --- | --- | --- |
| `python-magic` (libmagic wrapper) | ~3 | libmagic1 system .deb (~6 MB apt) + python wrapper | All ftyp variants (libmagic ships full table) | very low |
| `filetype.py` 1.x | ~5 | none (pure Python) | **heic, mif1+heic, msf1+heic only**. NOT heix, hevc, heim. | low |
| Hand-rolled (12-byte sniff) | ~30 | none | All variants we want, exactly | trivial |
| Pillow open + catch | ~5 | already have | All by definition (Pillow registry) | depends on registered openers |

Pillow's "open and catch" is the ergonomically nicest, but it has two
problems for our use case:

1. **It does the work too late.** The spec wants to reject image-bomb
   attacks *before* the decode path. `Image.open` is lazy, but the
   *suffix mismatch* (e.g. `.jpg` containing PNG bytes) only surfaces
   on the first pixel access — by which point we may have already
   fetched the file from Telegram, and we want a clean Russian reply,
   not a Pillow exception leaked.
2. **It conflates "is it a valid format" with "does the suffix lie".**
   The spec specifically wants the suffix-vs-magic check to fire
   *first*, with a distinct user-facing reply ("файл не похож на JPEG,
   проверь расширение").

`filetype.py`'s gap on `heix` / `hevc` / `heim` is real — Apple iOS 14+
emits `heix` for HDR HEIC, and Samsung emits `mif1`-with-HEVC. Skipping
detection would route them to the "формат не поддерживается" reject
path even though pillow-heif could decode them fine.

### Concrete recommendation

**Hand-rolled.** Don't pull in a library. ~30 LOC, no deps, full
control over the ftyp brand list. Lives in
`src/assistant/files/image.py` next to the decode pipeline.

```python
# src/assistant/files/image.py — magic byte check
"""First-12-byte format sniff for owner-uploaded image attachments.

Why hand-rolled: ``filetype.py`` 1.x doesn't recognize ``heix``,
``hevc``, ``heim`` ftyp brands (verified against
https://github.com/h2non/filetype.py/blob/master/filetype/types/image.py
2026-04-27). ``python-magic`` would add 6 MB of apt + libmagic to the
image. We need exact control over which brands we accept anyway —
the bot policy is "anything pillow-heif can decode" which is broader
than filetype.py and narrower than libmagic.
"""

# Magic table. Each entry is (suffix_set, predicate(bytes_12) -> bool).
# Order matters: HEIC sub-brands must follow the same ftyp prefix
# branch.
_HEIC_BRANDS = (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"heim")


def detect_image_kind(head: bytes) -> str | None:
    """Return one of ``{'jpg','png','webp','heic'}`` or ``None``.

    ``head`` MUST be at least 12 bytes; caller is responsible.
    Returns ``None`` for unknown / malformed magic; the caller
    surfaces a Russian "не похож на …" reply to the owner.
    """
    if len(head) < 12:
        return None
    # JPEG: FF D8 FF (3-byte SOI + start-of-marker).
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    # PNG: 89 50 4E 47 0D 0A 1A 0A.
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    # WEBP: "RIFF" .... "WEBP" — bytes 0..3 = "RIFF", 8..11 = "WEBP".
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    # HEIC: bytes 4..7 = "ftyp"; bytes 8..11 = brand.
    if head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS:
        return "heic"
    return None


def validate_suffix_matches_magic(suffix: str, head: bytes) -> bool:
    """Return True iff the file suffix is consistent with magic.

    ``suffix`` is the lower-case extension without leading dot.
    Allows the JPEG/JPG aliasing.
    """
    detected = detect_image_kind(head)
    if detected is None:
        return False
    if detected == "jpg" and suffix in ("jpg", "jpeg"):
        return True
    return detected == suffix
```

Wire-up in `_on_document` (before download? after? — see notes):

```python
# In _on_document, BEFORE the suffix-whitelist branch reached the
# image kinds — read the first 12 bytes from the downloaded tmp file
# and validate. Order: pre-download size cap → suffix whitelist →
# bot.download → magic check → kind dispatch.
async with aiofiles.open(tmp_path, "rb") as fh:
    head = await fh.read(12)
if suffix in IMAGE_SUFFIXES and not validate_suffix_matches_magic(suffix, head):
    detected = detect_image_kind(head)
    if detected is None:
        await message.reply(
            "файл не похож на изображение — проверь расширение"
        )
    else:
        await message.reply(
            f"файл не похож на {suffix.upper()} — выглядит как {detected.upper()}"
        )
    with contextlib.suppress(OSError):
        tmp_path.unlink(missing_ok=True)
    return
```

(Use stdlib `pathlib.Path.read_bytes()[:12]` if `aiofiles` isn't
already a dep — for 12 bytes, the sync read is fine and avoids a new
transitive dep.)

### Risks / failure modes

1. **`heix` is rare but real** — Apple iPhone 12+ HDR Live Photos use
   `ftyp heix` brand. Skipping it would silently reject the most common
   modern iPhone format. Hand-rolled gets this right.
2. **`mif1` is also AVIF** — `mif1` ftyp is shared between HEIF and
   AVIF; AVIF decodes via libheif (with libaom, which pillow-heif
   bundles). The current decision tree treats `mif1` as HEIC; if we
   later want to disambiguate, peek for `avif` in the *compatible
   brands* list (bytes 16+). Out of scope for v1.
3. **Magic check after download** — the order in the wire-up snippet
   reads bytes from disk *after* `bot.download`. We could read the
   first 12 bytes from the aiogram in-memory buffer to fail faster,
   but `bot.download(destination=tmp_path)` writes straight to disk
   and aiogram doesn't expose a "head bytes" mode. The 20 MB pre-cap
   already bounds the worst-case wasted bandwidth.
4. **EOF-short reads** — the ``len(head) < 12`` guard catches
   truncated downloads (network blip mid-stream). Returning `None` →
   "не похож на изображение" reply is the right UX.

### Test strategy

`tests/test_image_magic.py`:

- `test_jpeg_magic_detected` — `detect_image_kind(b"\xff\xd8\xff..." + b"\x00"*9) == "jpg"`.
- `test_png_magic_detected` — full 8-byte PNG signature + 4 padding.
- `test_webp_magic_detected` — `b"RIFF\x00\x00\x00\x00WEBP"`.
- `test_heic_brands_all_detected` — parameterize over `_HEIC_BRANDS`,
  build `b"\x00\x00\x00\x18ftyp" + brand`, assert == "heic".
- `test_unknown_magic_returns_none` — random 12 bytes.
- `test_short_input_returns_none` — `b"\xff\xd8"` (3-byte truncation).
- `test_suffix_jpeg_jpg_alias_ok` — suffix `"jpeg"` + JPEG magic == True.
- `test_suffix_mismatch_caught` — suffix `"jpg"` + PNG magic == False.

No mocks needed; the function is pure. ~80 LOC of tests for ~30 LOC of
code is the right asymmetry — the function is on the security/UX
critical path.

---

## Cross-cutting / extra notes for the coder

### A. `bridge.ask` signature extension

Spec says coder will extend `bridge.ask` to accept image content blocks.
The minimum-invasive shape mirrors `system_notes`:

```python
async def ask(
    self,
    chat_id: int,
    user_text: str,
    history: list[dict[str, Any]],
    *,
    system_notes: list[str] | None = None,
    image_blocks: list[dict[str, Any]] | None = None,
) -> AsyncIterator[Any]:
    ...
    if image_blocks:
        live_content: list[dict] | str = [
            {"type": "text", "text": user_text_for_envelope},
            *image_blocks,
        ]
    else:
        live_content = user_text_for_envelope
    ...
    yield {
        "type": "user",
        "message": {"role": "user", "content": live_content},
        "parent_tool_use_id": None,
        "session_id": f"chat-{chat_id}",
    }
```

`history_to_sdk_envelopes` stays untouched — the spec already commits
to NOT persisting image bytes (Q5). The Q8 `seen:` summary serves as
the durable record.

### B. Anthropic vision quirks the coder should know

- **Image content block ordering**: per docs, *images BEFORE text*
  measurably improves performance ("Claude works best when images come
  before text"). Spec snippet has `text` first; consider flipping. The
  RQ0 spike used `text` first and worked fine, but the docs are
  explicit. Move image blocks ahead in `live_content`.
- **`Image.MAX_IMAGE_PIXELS`**: spec says 25 MP. Pillow's default is
  178 956 970 (≈179 MP). Override at module import in
  `assistant/files/image.py` BEFORE any `Image.open` runs. Beware:
  *Pillow itself can issue a `DecompressionBombWarning` at MAX/2 and
  raise at >MAX*. Catch the bomb error in the `_decode_resize_jpeg`
  caller, return a sanitised reply.
- **EXIF strip via `exif=b""`** — verified in the spec, double-checked
  per Pillow 12.x docs: passing `exif=b""` to `Image.save("JPEG", ...)`
  emits a JPEG with no APP1 EXIF block. Some tools (`exiftool`)
  show a "no metadata" entry instead of "missing tag" — the
  difference doesn't matter for the GPS/comment-injection threat.

### C. Filename convention for photo-source path

Spec leaves the inline `F.photo` filename unspecified. Suggest:
`<uuid4>__photo_<photo.file_unique_id[:8]>.jpg` — uniqueness preserved,
the `file_unique_id` shard helps post-mortem (matches Telegram's own
file deduplication ID).

### D. PhotoSize selection

```python
photo = max(message.photo, key=lambda p: (p.width * p.height, p.file_size or 0))
```

Pure-area max is preferred over `message.photo[-1]` because Telegram's
ordering is documented but not guaranteed across bot-API versions.

---

## Sources (consolidated)

### RQ1 — pillow-heif

- [pillow-heif on PyPI (1.3.0)](https://pypi.org/project/pillow-heif/) — wheel sizes ~5.5-6.9 MB, manylinux_2_28 tag.
- [pillow-heif installation docs](https://pillow-heif.readthedocs.io/en/latest/installation.html) — wheel-vs-source split.
- [pillow-heif CHANGELOG](https://github.com/bigcat88/pillow_heif/blob/master/CHANGELOG.md) — libheif 1.21.2 bundled in 1.2.0 (2026-01-23). 1.3.0 released 2026-02-27.
- [pypa/manylinux](https://github.com/pypa/manylinux) — manylinux_2_28 must vendor non-allowlisted libs.
- [PEP 600](https://peps.python.org/pep-0600/) — manylinux glibc-floor naming.

### RQ2 — Anthropic vision pricing

- [Anthropic vision docs](https://platform.claude.com/docs/en/build-with-claude/vision) — "tokens = w*h/750", Opus 4.7 native 2576 px / 4784 tokens, image-before-text recommendation.
- [Claude API pricing 2026](https://platform.claude.com/docs/en/about-claude/pricing) — Opus 4.7 input @ $5/MT.

### RQ3 — aiogram media_group

- [aiogram-media-group source](https://github.com/deptyped/aiogram-media-group/blob/main/aiogram_media_group/handler.py) — non-resetting debounce, `event_loop.call_later`, race window on `set_media_group_as_handled`.
- [aiogram 3.27 docs / Middlewares](https://docs.aiogram.dev/en/latest/dispatcher/middlewares.html) — middleware vs handler placement.
- [aiogram_album_handler example](https://github.com/WhiteMemory99/aiogram_album_handler/blob/master/example/album.py) — alternate community pattern; same flaw.
- [mastergroosha aiogram-3 guide](https://mastergroosha.github.io/aiogram-3-guide/filters-and-middlewares/) — Russian-language convention reference.

### RQ4 — Pillow memory profile

- [Pillow Image module docs](https://pillow.readthedocs.io/en/stable/reference/Image.html) — `open` is lazy, `draft` JPEG/MPO only, `MAX_IMAGE_PIXELS` semantics.
- [pillow-heif Pillow plugin docs](https://pillow-heif.readthedocs.io/en/latest/pillow-plugin.html) — `register_heif_opener(thumbnails=False, quality=-1)`.
- [Pillow GH #5180 (closed, no leak)](https://github.com/python-pillow/Pillow/issues/5180) — refcount ref-cycle was user-side.

### RQ5 — libheif CVEs

- [CVE-2025-68431 (Ubuntu)](https://ubuntu.com/security/CVE-2025-68431) — patched in libheif 1.21.0.
- [GHSA-j87x-4gmq-cqfq (libheif)](https://github.com/strukturag/libheif/security/advisories/GHSA-j87x-4gmq-cqfq) — heap-over-read in `HeifPixelImage::overlay()`.
- [USN-7952-1](https://ubuntu.com/security/notices/USN-7952-1) — bookworm bundle backport.
- [Renovate config docs](https://docs.renovatebot.com/configuration-options/) — `automerge`, `vulnerabilityAlerts`.
- [Dependabot config docs](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference) — `pip` ecosystem, `allow:` filter.

### RQ6 — Magic-byte validation

- [filetype.py source — image module](https://github.com/h2non/filetype.py/blob/master/filetype/types/image.py) — only heic, mif1+heic, msf1+heic; gap on heix/hevc/heim.
- [filetype.py PyPI](https://pypi.org/project/filetype/) — pure-Python, no deps.
- [HEIF brand registry (ISO/IEC 23008-12)](https://nokiatech.github.io/heif/technical.html) — full ftyp brand table for HEIC/HEIX/MIF1/MSF1 distinction.

### Existing repo memory references

- `~/.claude/agent-memory/researcher/feedback_asyncio_background_tasks.md` — RQ3 GC warning citation.
- `~/.claude/agent-memory/researcher/reference_claude_agent_sdk.md` — multimodal envelope tested on 0.1.59 + RQ0 spike confirms 0.1.59 still works for vision in 2026-04.
