---
phase: 6b
title: Telegram photo / image vision via Claude multimodal API
date: 2026-04-27
status: shipped (commit c1efa4b, CI green run 25003129308, VPS healthy)
---

# Phase 6b — Telegram photo vision summary

Phase 6b adds INPUT-only image attachments (`F.photo` inline + `.jpg/.jpeg/.png/.webp/.heic/.heif` via `_on_document`). Pipeline: magic-byte gate → Pillow decode (25 MP image-bomb cap) → HEIC convert via pillow-heif → resize max edge 1568 px → EXIF strip → JPEG q=85 → base64 multimodal envelope. Media-group aggregation with 1.5s resetting debounce + 5-photo hard cap. Auto-summary captured into `[photo: name | seen: ...]` marker for multi-turn vision context.

NO source-code changes to bridge (semantically — `bridge.ask` got `image_blocks` kwarg)/scheduler/memory/installer subsystems. Phase 5d Docker invariants preserved. Phase 6a Option B uniform extraction pipeline (PDF/DOCX/TXT/MD/XLSX) preserved unchanged — image-kinds branch BEFORE the `EXTRACTORS` dispatch.

## What shipped

- **`assistant/files/vision.py`** (~256 LOC) — magic-byte validation (12 B sniff for JPEG/PNG/WEBP/6 HEIC ftyp brands), `load_and_normalize` (Pillow decode → resize → EXIF strip → JPEG re-encode), `build_image_content_block`, `VisionError` with structured `kind`/`declared`/`detected` fields, idempotent `register_heif_opener(thumbnails=False)` at import time. `Image.MAX_IMAGE_PIXELS = 25_000_000` set module-level (single-user policy; documented).
- **`assistant/adapters/media_group.py`** (~250 LOC) — hand-rolled `MediaGroupAggregator` with resetting debounce (`asyncio.create_task` + cancellable timer), per-bucket lock, 5-photo hard cap with one-shot Russian overflow reply, `flush_for_chat(chat_id)` pre-empt for text arrival, `cancel_all` graceful shutdown, **C1 self-task fix** (skip cancel if `bucket.flush_task is asyncio.current_task()`).
- **`assistant/adapters/base.py`** — `AttachmentKind` Literal extended with image kinds, `IMAGE_KINDS` frozenset, `attachment_paths: list[Path] | None` field for media_group case.
- **`assistant/adapters/telegram.py`** (+285 LOC) — `_on_photo` handler with PhotoSize area-selector (`max(p, key=lambda p: p.width * p.height)`), `_on_animation` handler emitting `"анимации не поддерживаю — пришли картинку"`, `_on_document` whitelist extended + `F.document & ~F.animation` filter (closes 6a debt M-W2-1), default caption `"что на фото?"` for empty captions, `_on_text` flush-pre-empt for pending media_group buckets. Handler order: `text → photo → animation → document & ~F.animation → catch-all`. Catch-all reply updated (no more stale "phase 6" string).
- **`assistant/handlers/message.py`** (+360 LOC) — vision branch BEFORE existing `EXTRACTORS` dispatch keyed by `attachment_kind in IMAGE_KINDS`, `_handle_vision_failure` symmetric quarantine (mirrors 6a `_handle_extraction_failure`), `_build_vision_summary_segment` accumulating ALL TextBlocks into `seen:` marker (200-char trim at word boundary), deferred user-row append for multi-photo media_group (one marker per photo with shared seen), multi-path `is_relative_to` defensive guard for `attachment_paths`, format-specific Russian replies via `VisionError.kind` discriminator.
- **`assistant/bridge/claude.py`** — `ask` extended with `image_blocks: list[dict] | None = None` kwarg; when present, envelope flips to `list[dict]` content with **images BEFORE text** (Anthropic perf guidance per RQ2). C1 spike PASS 2026-04-27 cleared the "list[dict] form unverified" comment.
- **`Dockerfile`** — build-time HEIC sanity check (`RUN python -c "import pillow_heif; ..."` asserts plugin registers). NO apt change — pillow-heif manylinux_2_28 wheel statically bundles libheif/libde265/libx265/libaom (RQ1).
- **`pyproject.toml`** — `Pillow>=10,<13`, `pillow-heif>=1.3.0,<2`. Image delta ~9 MB.
- **`.github/renovate.json`** (NEW) — pillow-heif/Pillow auto-merge minor/patch + manual major-review labels. Primary CVE pipeline for libheif (Trivy doesn't see bundled `.so`).

## Pipeline mechanics

- Plan v1 (`plan/phase6b/description.md`, ~270 lines).
- **C1 spike** (`plan/phase6b/spikes/rq0_multimodal/probe.py`) — live VPS container probe with 200×200 JPEG → `claude-opus-4-7[1m]` correctly described content. Cleared the "list[dict] form unverified" risk that drove phase 6a's Option C → Option B flip.
- Devil wave-1 — 6 CRITICAL closed in spec v1 (envelope, debounce/lock, animation exclude, image-bomb, HEIC strategy, drop /photos subdir).
- Researcher RQ1-6 (`plan/phase6b/research.md`, ~600 LOC) — closed pillow-heif transitive deps (no apt), Anthropic 2026 vision pricing (`w*h/750` linear, NOT tile-bucket), aiogram media_group debouncer (hand-rolled, NOT `aiogram-media-group` library — buggy), memory profile, libheif CVE auto-update, magic-byte hand-rolled.
- Coder — 74 new tests, 17 files modified (+712/-60 LOC). ruff + mypy --strict clean.
- 4 parallel reviewers (code 0C/1H/4M/5L · qa 0C/3H/10M/11L · devops conditionally ready · devil-w2 1C/5H/7M/5L).
- Fix-pack — 17 items applied + 13 new regression tests. Final: **693 passed / 1 deselected seed-vault flake / 3 skipped**.
- VPS deploy 2026-04-27 → owner smoke ALL ACs PASSED.

## Architectural decisions frozen

- **C1 multimodal envelope verified** for vision. `bridge.ask(image_blocks=...)` flips to `list[dict]` content; image blocks BEFORE text per Anthropic perf guidance.
- **5-photo cap per turn** (Q2 v1) — Anthropic 2026 cost ~$0.082/turn at cap (3278 tokens × 5 + system prompt cache miss).
- **HEIC accept on VPS** — pillow-heif manylinux wheel; libheif CVE risk accepted under single-user trust model; Renovate as primary CVE pipeline.
- **Media-group state machine adapter-level** — resetting debounce (1.5s), per-bucket lock, hard 5-photo cap with one-shot Russian overflow reply, `flush_for_chat` pre-empt on text.
- **Per-chat lock acquired ONLY at flush time** (not per-photo) — prevents cost amplification.
- **Pre-resize max edge 1568 px** — cost + memory dual win. Enforced in `load_and_normalize`.
- **Image-bomb guard 25 MP** — `Image.MAX_IMAGE_PIXELS = 25_000_000` module-level (rejects 50000×50000 panic-bombs at IDAT chunk before allocation).
- **EXIF strip mandatory** — `image.save(buf, exif=b"")` for privacy (GPS) + prompt-injection defense (comment field).
- **Magic-byte hand-rolled** — covers all 6 HEIC ftyp brands (`heic`, `heix`, `mif1`, `msf1`, `hevc`, `heim`); `filetype.py` 1.x has gaps.
- **Storage flat at `/app/.uploads/`** — uuid prefix gives uniqueness; reuses 6a boot-sweep + quarantine; no `/photos/` subdir.
- **Marker + auto-summary** `[photo: name | seen: <first 200 chars>]` per photo; ALL TextBlocks concatenated (not just first — fix-pack F11).
- **`F.document & ~F.animation`** exclude — closes 6a debt M-W2-1.
- **Russian default caption** `"что на фото?"` when empty (photo + image-as-document unified).
- **`message_id` from first photo** in media_group bucket (forensic correlation).

## Known carry-forwards (debt → phase 6e)

- **`.failed/` size cap** — currently 7-day age-prune only. With image kinds added, growth rate doubles; worst-case ~1.4 GB/7d.
- **`/app/.uploads/` not bind-mounted** — quarantine evidence dies on `docker compose down` / `docker rm`. Doubles forensic loss surface vs. 6a.
- **Daily Anthropic cost circuit-breaker** — single-user discipline currently; 100-photo accident = $8 burn. Cumulative `last_meta["cost_usd"]` rolling sum.
- **Trivy bundled-`.so` blind spot** — Trivy library scan reads PyPI metadata, can't see vendored libheif inside `pillow_heif/.libs/`. Renovate is the actual pipeline; documented.
- **Image-as-document multi-file aggregation** — `_on_document` for image suffix kinds processes sequentially (5 doc-route photos = 5 turns). Spec accepts; revisit if owner experience needs it.
- **HEIC video (Apple Live Photo)** — libheif extracts first frame; embedded HEVC video path untested.
- **Animated WebP / APNG** — Pillow defaults to first frame; cosmetic test gap.
- **Module-level Pillow `MAX_IMAGE_PIXELS` mutation** — leaks to all Pillow callers in process. Today no other module uses Pillow; documented as policy.

## Phase 6c/6d unlocked

- **6c whisper voice** — `F.voice` aiogram handler. Mac sidecar protocol (Whisper on Mac CPU, not VPS). Reuses `IncomingMessage.attachment` + tmp-dir + per-turn-cleanup + boot-sweep contract from 6a/6b.
- **6d image-gen** — outbound `bot.send_photo`. Independent of INPUT path. Likely involves new MCP tool for image generation.

## References

- `plan/phase6b/description.md` — final spec (~280 lines).
- `plan/phase6b/research.md` — RQ1-6 closure (~600 LOC).
- `plan/phase6b/spikes/rq0_multimodal/probe.py` — C1 spike PASS artifact.
- Commit `c1efa4b` — phase 6b: Telegram photo vision via Claude multimodal API.
- CI run `25003129308` — green 3m48s.
