---
phase: 6a
title: Telegram file uploads (PDF/DOCX/TXT/MD/XLSX)
date: 2026-04-27
status: shipped (CI pending; image rebuild required for new pure-python deps)
---

# Phase 6a — Telegram file uploads summary

Phase 6a adds INPUT-only file attachment handling: owner sends PDF, DOCX, TXT, MD, or XLSX via Telegram → adapter validates size + suffix → downloads to per-turn tmp file → handler routes to extractor (DOCX/XLSX/TXT/MD) or attaches as system-note for Claude's native `Read` tool (PDF, hybrid Option C contingent on live container probe) → bridge.ask returns reply → tmp deleted in `finally` (or quarantined to `.failed/` on extraction error).

No source-code changes to bridge/scheduler/memory/installer subsystems. Phase 5d Docker invariants preserved.

## What shipped

- **`assistant/files/extract.py`** (~330 LOC) — 5 extractors + `ExtractionError` + dispatch table. DOCX uses `doc.element.body.iterchildren()` for document-order paragraphs+tables (RQ4 fidelity 100%). XLSX `read_only=True data_only=True` with `wb.close()` finally; ROW_CAP=20, COL_CAP=30 per sheet, all sheets, post-extract 200K char total cap. PDF fallback via pypdf (used if RQ1 live probe fails).
- **`Settings.uploads_dir`** — container `/app/.uploads/`, Mac dev `<data_dir>/uploads/` (post-fix-pack: was `<project_root>/.uploads/` initially, drift caught in code-review H1 + QA M1).
- **Telegram adapter `_on_document` handler** — registered before catch-all (text → document → catch-all per RQ3 PASS). Pre-download `(file_size or 0) > 20MB` guard (devil C2). Suffix whitelist `.pdf .docx .txt .md .xlsx`. Filename sanitization `<uuid>__<sanitized>.<ext>` with `re.sub(r"[^\w.-]", "_")` capped 64 chars. `bot.download(timeout=90)` (devil M-W2-6 fix-pack); `TelegramBadRequest` + `TimeoutError` + generic `Exception` branches with sanitized Russian replies (no `repr(exc)` leak — fix-pack F3).
- **`IncomingMessage` extension** — 3 new optional fields: `attachment: Path | None`, `attachment_kind: AttachmentKind | None`, `attachment_filename: str | None`. Backward-compatible (kwargs callers preserved per phase-5 RQ1).
- **Handler attachment branch** in `_handle_locked` — defensive `tmp_path.resolve().is_relative_to(uploads_dir)` guard (fix-pack F2 / QA HIGH §I.194). PDF Option C: append system-note `"the user attached a PDF at path={tmp_path}; use Read tool"`. Other formats Option B: pre-extract + inject `[attached: {filename}]` into `user_text_for_envelope`. User row persisted with marker `[file: {filename}]` only — NOT extracted bytes.
- **`Daemon._boot_sweep_uploads`** — UNCONDITIONAL wipe of all top-level entries in `uploads_dir` at startup (devil H3 fix). `.failed/` files older than 7 days pruned. Quarantine OSError chain: rename → log + `shutil.copy2` fallback (fix-pack F6 — preserves evidence).
- **Dockerfile** — `mkdir -p /app/.uploads /app/.uploads/.failed && chown -R 1000:1000 /app/.uploads`. ~6 MB image delta (3 pure-python wheels: python-docx, openpyxl≥3.1.5, pypdf).
- **`.gitignore`** — `.uploads/` excluded (Mac dev fallback).

## Pipeline mechanics

- Plan v2 (`plan/phase6a/description.md`, ~270 lines patched).
- Devil wave 1 — 25 findings (4 CRITICAL, 7 HIGH, 8 MEDIUM, 6 LOW). All CRITICAL'ы (C1 hook, C2 file_size, C3 plan ambiguities, C4 multimodal replay) addressed via researcher fix-pack.
- Spike wave RQ1-RQ4 — RQ1 PASS conditional (Option 1: tmp at `/app/.uploads/`, no hook surgery). RQ2 PASS (42 MB peak RSS on 308 MB adversarial XLSX with caps). RQ3 PASS (text→document→catch-all routing clean). RQ4 PASS @100% recall with `iterchildren()` document-order fix.
- Researcher fix-pack — `description.md` patched inline + `implementation-v2.md` (833 LOC) created.
- Coder — 71 new tests. ruff + mypy --strict clean. 581 passed.
- 4 parallel reviewers — 0 CRITICAL across all 4 (code 0C/1H/5M, QA 0C/1H/5M, devil w2 0C/0H/6M, devops Ready). Ship verdict.
- Fix-pack — 7 items applied (2 HIGH + 5 MEDIUM): uploads_dir Mac drift, defensive `is_relative_to` assert, sanitized error replies, openpyxl pin >=3.1.5, ToolResultBlock test gap, quarantine OSError fallback, download timeout 90s. +16 new regression tests. Final: **602 passed / 1 pre-existing seed-vault fail / 2 skipped**.

## Architectural decisions frozen

- **Hybrid extraction:** PDF via SDK Read (Option C); DOCX/XLSX/TXT/MD via pre-extract (Option B). Single-line `_is_pdf_native_read` flip if RQ1 live probe fails.
- **Tmp dir at `/app/.uploads/`** (NOT `data_dir/tmp/`). No `make_file_hook` extension — hook keeps single `project_root` constraint.
- **`uploads_dir` Mac fallback `<data_dir>/uploads/`** — mirrors `vault_dir`/`memory_index_path` (fix-pack F1).
- **20 MB pre-download cap** + 200K post-extract cap with truncate marker. XLSX dual-cap: per-sheet ROW=20 × COL=30 + total 200K (clean sheet boundary).
- **Suffix whitelist** sufficient for single-user trust; no `python-magic` (deferred phase 9).
- **Boot-sweep UNCONDITIONAL** at every restart. `.failed/` 7-day prune.
- **User row marker `[file: filename]` only** — not full extracted bytes (keeps conversations small).
- **Russian default caption** "опиши содержимое файла" when caption empty for ALL formats.

## Known carry-forwards (debt)

- **RQ1 live container probe** — coder did NOT run (Docker not on dev box); single-line flip ready if FAILS at owner smoke.
- **F.document over-match** (devil M-W2-1) — animations/videos-as-files. Phase 6b adds `~F.animation` exclude.
- **Concurrent burst download** (devil M-W2-3) — 3 parallel `bot.download` saturates VPS bandwidth before per-chat lock kicks. Single-user accepted; phase 6b/c may revisit.
- **`.failed/` size cap** (devops top-2) — only 7-day age prune, no size cap. Worst-case 1.4 GB/7d. Phase 6e.
- **Container-layer destruction on `docker compose down`** (devops top-1) — `.failed/` not bind-mounted; routine `down` wipes forensics. Documented per spec §A.
- **OCR scanned PDFs** — phase 6e (tesseract or rejection hint).
- **DOCX images / track-changes / equations** — silently dropped; phase 6b vision will pick up images via separate pipeline.
- **History recall of file contents** — model sees `[file: X.pdf]` marker on next turn but no bytes; owner re-attaches if needed (Q8 design choice).

## Phase 6b/6c/6d unlocked

- **6b vision** reuses `IncomingMessage.attachment` + `_on_document`/`_on_photo` handler split. `F.photo` filter + multimodal user envelope.
- **6c whisper voice** — `F.voice` handler. Tmp-dir + per-turn-cleanup + boot-sweep contract reused. Mac sidecar protocol = phase 6c spec.
- **6d image-gen** — outbound `bot.send_photo`. Independent of INPUT path.

`/app/.uploads/{photos,voices}/` subdir convention recommended for 6b/6c (devops top phase-6b prereq).

## Owner smoke checklist (post-cutover)

1. Send small PDF (text layer) → bot reads + answers question.
2. Send DOCX (Russian text + table) → extracted, answered.
3. Send XLSX (multi-sheet) → CSV-like extract.
4. Send 50MB PDF → "файл больше 20 МБ".
5. Send `.exe` → "формат не поддерживается".
6. Verify `/app/.uploads/` empty after each turn (`docker exec 0xone-assistant ls /home/bot/.local/share/0xone-assistant/uploads/` should return nothing — wait, container path is `/app/.uploads/` not bind-mounted — `docker exec ls /app/.uploads/`).
7. Phase 1-5d regressions: ping, memory recall, marketplace_list, scheduler proactive fire — all still GREEN.

## References

- `plan/phase6a/description.md` — final spec (~270 lines).
- `plan/phase6a/implementation-v2.md` — coder blueprint (833 LOC).
- `plan/phase6a/devil-wave-{1,2}.md` — 25 + 12 findings.
- `plan/phase6a/spike-findings.md` — RQ1-4 results.
- `plan/phase6a/spikes/rq{1,2,2b,3,4}_*.{md,py}` — spike artifacts.
- `plan/phase6a/review-{code,qa,devops}.md` — parallel reviewer reports.
