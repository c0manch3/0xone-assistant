---
phase: 6b
title: Telegram photo / image vision via Claude multimodal API
date: 2026-04-27
status: spec v1 — devil wave-1 cleared, pre-researcher
prereqs: phase 6a (file uploads) shipped 2026-04-27 — image suffixes piggyback on existing Option B uniform pipeline + new vision branch
---

# Phase 6b — Telegram photo vision

## Goal

INPUT-only image attachments via Telegram → Claude Vision API multimodal envelope → text response. Owner sends photo (jpeg auto-compressed) OR image-as-document (`.jpg/.jpeg/.png/.webp/.heic`) → bot validates + downloads → pre-process (HEIC convert, resize, EXIF strip) → builds streaming-input multimodal envelope → bridge.ask returns reply → tmp deleted in `finally`.

NO source-code changes to bridge/scheduler/memory/installer subsystems. Phase 5d Docker invariants preserved. Phase 6a Option B uniform extraction pipeline (PDF/DOCX/TXT/MD/XLSX) preserved unchanged — image-kinds branch BEFORE the `EXTRACTORS` dispatch.

## C1 spike result (RQ0 multimodal envelope) — PASS 2026-04-27

Live VPS container probe (`plan/phase6b/spikes/rq0_multimodal/probe.py`):
- Sent 200×200 JPEG (red/blue diagonal triangles) via streaming-input `list[dict]` content-block form.
- `claude-opus-4-7[1m]` correctly described content: «диагональ из верхнего правого угла в нижний левый, верхний левый красный треугольник, нижний правый…»
- ResultMessage stop_reason=`end_turn`. No `Unknown message type`. Cost: $0.109 first call (16 916 cache_creation tokens for system prompt; 1h cache → subsequent calls cheap).
- **Verdict:** SDK + bundled `claude` ELF reliably propagate `{type: "image", source: {type: "base64", media_type: ..., data: ...}}` content blocks. The "list[dict] form is unverified" comment in `bridge/claude.py:241` is now CLEARED for image use case.

## Architectural decisions (post devil wave-1, post Q&A v0+v1)

### Source

- `F.photo` aiogram handler (Telegram inline preview; auto-compressed JPEG up to ~5-10 MB; PhotoSize variants — pick last/largest).
- `_on_document` 6a handler — extend whitelist with image suffixes `.jpg .jpeg .png .webp .heic` for raw "send as file" path. Same suffix sanitization, same per-download size cap (20 MB inherited from 6a).
- Filter precedence: `text → photo → document & ~F.animation → catch-all`. `~F.animation` exclude closes 6a debt M-W2-1 (animated GIF / sticker over-match).

### Media-group aggregation

- aiogram delivers each photo in a Telegram media_group as separate `Message` update with shared `media_group_id` (asynchronous, ordering NOT guaranteed).
- Adapter-level state machine: `dict[(chat_id, media_group_id), MediaGroupBucket]`. Each photo arrives → `bot.download` immediately → tmp path appended to bucket.
- Flush triggers: (a) **1.5 sec debounce timer** since last photo in bucket; (b) bucket size reached `MAX_PHOTOS_PER_TURN = 5` (hard cap — extra photos beyond 5 within window get dropped with sanitized Russian reply); (c) explicit text-message arrival for same chat_id (flush early so vision turn doesn't block text).
- Single photo (no `media_group_id`) bypasses aggregation — direct flush.
- Restart during debounce window: tmp paths orphan on disk → boot-sweep wipes → owner re-shares. Debounce state is in-memory only; no persistence (overkill for single-user).
- Per-chat lock acquired ONLY at flush time (not per-photo), preventing cost amplification race devil C2 flagged.

### HEIC handling

- `pillow-heif` wheel + libheif on slim-bookworm. Apt deps verified by researcher (RQ — see open questions).
- libheif CVE risk (CVE-2023-49463/64, CVE-2024-25269 RCE family) accepted under single-user trust model. Mitigation: timely base-image + wheel updates; hard ceiling on decoded image size (25 MP) reduces blast radius.
- Convert HEIC → JPEG quality=85 in-memory before envelope construction. JPEG result re-encoded with EXIF stripped.
- HEIC video (Apple Live Photo with embedded HEVC) → libheif extracts first frame; if extraction fails → quarantine with sanitized error.

### Image pipeline (pre-envelope)

1. **Magic byte validation** — read first 12 bytes, validate JPEG (`\xff\xd8\xff`), PNG (`\x89PNG\r\n\x1a\n`), WEBP (`RIFF....WEBP`), HEIC (`....ftypheic`/`heix`/`mif1`/`hevc`). Reject mismatched suffix/magic with Russian reply.
2. **Decode via Pillow** with `Image.MAX_IMAGE_PIXELS = 25_000_000`. Decoded image > 25 MP → reject as image bomb.
3. **HEIC → JPEG** if needed (pillow-heif).
4. **Resize** to max edge 1568 px (Pillow `thumbnail` preserving aspect). Saves cost (Anthropic Vision auto-tile threshold) and memory (peak RSS halved).
5. **EXIF strip** — `image.save(buf, format="JPEG", quality=85, exif=b"")` mandatory (privacy: GPS; security: prompt-injection vector via comment field).
6. **Base64 encode** result, build content block `{type: "image", source: {type: "base64", media_type: "image/jpeg", data: ...}}`.

### Multimodal envelope construction

- New helper `build_vision_envelope(text: str, image_paths: list[Path]) -> dict` (probably in `bridge/claude.py` or new `bridge/multimodal.py`).
- Returns streaming-input dict:
  ```python
  {
      "type": "user",
      "message": {
          "role": "user",
          "content": [
              {"type": "text", "text": user_text},
              *[{"type": "image", "source": {...}} for path in image_paths],
          ],
      },
      "parent_tool_use_id": None,
      "session_id": f"chat-{chat_id}",
  }
  ```
- `history_to_sdk_envelopes` (`bridge/history.py`) UNCHANGED — replays only text/tool_use/tool_result. Image-blocks NOT persisted by design (Q5).

### History persistence (Q8 v1 — auto-summary)

- User row content format: `<original caption>\n[photo: <filename> | seen: <first 200 chars of assistant response>]`.
- Assistant response captured AFTER first TextBlock in vision-turn. First 200 chars (truncated at word boundary + `…`) embedded in `seen:` segment.
- For multi-photo media_group: marker is one line per photo with same `seen:` summary repeated (model's response is for the group, not per-photo).
- Cost: ~50 output tokens × $15/1M = $0.00075/turn. Negligible.

### Caption defaults

- Empty caption + photo source → default `"что на фото?"`.
- Empty caption + document image-route → default `"что на фото?"` (unified).
- Non-empty caption preserved verbatim, Russian default skipped.

### Storage layout (Q7 v1 — flat)

- All attachments (6a + 6b) flat in `/app/.uploads/`. uuid prefix `<uuid>__<sanitized>.<ext>` gives uniqueness.
- Quarantine: `/app/.uploads/.failed/` (shared with 6a) — `_handle_extraction_failure` UNCHANGED.
- Boot-sweep `_boot_sweep_uploads` UNCHANGED — top-level wipe + 7-day `.failed/` prune already covers image kinds.

### Token cost reality (devil H6 / fix-pack F15)

- Anthropic 2026 vision pricing is **linear** ``w*h/750`` (not the
  legacy tile-bucket model — see ``plan/phase6b/research.md`` RQ2 closure).
- 1568 px square ≈ 1568×1568/750 ≈ 3278 input tokens / image.
- 5 photos × 3278 = 16 390 input tokens / turn cap.
- Marginal cost (cache miss on system prompt): ~$0.082/turn for Opus
  4.7 at $5/MTok input. With 1h system-prompt cache hit, dramatically
  cheaper (cache reads bill at 10% of input rate).
- Hard cap of 5 photos/turn (Q2 v1) keeps worst-case bounded.

### Existing infra reused (zero changes)

- `IncomingMessage.attachment` → extend type to `Path | list[Path] | None` (or new `attachments: list[Path]`; coder picks idiom).
- `AttachmentKind` Literal extended: `"jpg" | "jpeg" | "png" | "webp" | "heic"` added.
- `ClaudeHandler._handle_locked` — new branch BEFORE existing `EXTRACTORS[kind]` dispatch:
  ```python
  if kind in IMAGE_KINDS:
      content_blocks = build_image_content_blocks(attachment_paths)
      # bridge.ask gets content_blocks instead of plain string
      ...
  elif kind in EXTRACTABLE_KINDS:
      # existing 6a Option B path
      ...
  ```
- `bridge.ask` — extend signature to accept `image_blocks: list[dict] | None = None`; if present, build multi-content-block envelope; else current plain-string path.
- `make_file_hook` UNCHANGED — vision does NOT call SDK Read; bytes go through envelope. Note: hook still applies to ANY future Read call by model in the same turn (e.g. owner text after photo asks "также прочитай /app/skills/foo/SKILL.md").

### Open for researcher (RQ list)

- **RQ1** — pillow-heif transitive deps on slim-bookworm: apt vs static wheel. `apt install libheif1 libde265-0 libx265-199`?
- **RQ2** — Anthropic Vision API tile pricing 2026 (tile size, tokens-per-tile current rate; spec assumes 1568 max edge = 1 tile).
- **RQ3** — aiogram 3.27 media_group event delivery patterns (idiom for adapter-level aggregator with `media_group_id`).
- **RQ4** — Pillow/pillow-heif memory profile on 12 MP HEIC decode (peak RSS measurement); decide whether `gc.collect()` after save is necessary.
- **RQ5** — libheif CVE auto-update strategy (Renovate? base-image tag pin discipline? bookworm point-release tracking?).
- **RQ6** — magic-byte validation library (`filetype.py` vs hand-rolled struct.unpack).

### Acceptance criteria

- AC#1 — owner sends single jpeg via inline → bot describes content within 30 sec.
- AC#2 — owner sends HEIC via "send as file" → conversion + response without quarantine.
- AC#3 — owner sends 5-photo media_group → ONE response covering all 5 (within debounce window).
- AC#4 — owner sends 6th photo within window → 5 first delivered + 1 rejected with Russian reply.
- AC#5 — owner sends animated GIF as document → reply "анимации не поддерживаю".
- AC#6 — owner sends `.jpg` file with PNG bytes → reply "файл не похож на JPEG, проверь расширение".
- AC#7 — image bomb (small file, 50000×50000 dimensions claim) → reject before decode-OOM.
- AC#8 — phase 6a regressions (PDF/DOCX/TXT/MD/XLSX) ALL GREEN.
- AC#9 — phase 1-5d regressions (ping/memory/marketplace_list/scheduler proactive fire) ALL GREEN.

### Devil wave-1 closure

- C1 (multimodal envelope unverified) — CLEARED via RQ0 spike.
- C2 + H10 + C6 (debounce vs lock + cost cap) — adapter-level state machine + 5-photo cap.
- C3 + H2 (F.animation + handler order) — explicit `~F.animation` exclude + canonical order.
- C4 + H4 + H6 (image bomb + memory + cost) — magic byte + 25 MP ceiling + 1568 resize.
- C5 + M5 + H7 (HEIC strategy) — accept on VPS with libheif; CVE risk accepted; pre-decode magic check.
- H3 + M2 + M6 (subdir) — DROPPED. Flat layout reuses 6a infra.
- H1 (history footgun) — auto-summary `seen:` segment.
- H5 (EXIF) — mandatory strip.
- M1 (IMAGE_KINDS branching) — explicit branch in `_handle_locked` before EXTRACTORS dispatch.

Devil wave-2 expected after researcher pass; specifically targeting RQ1-6 outcomes + implementation specifics not yet locked.
