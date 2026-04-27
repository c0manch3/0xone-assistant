# Phase 6a — Devil's Advocate Wave 2 (shipped code)

> Reviewer: devil's-advocate. Inputs: uncommitted code (manifest in
> `implementation-v2.md` §0), wave-1 + spike findings (assumed mitigated
> per researcher fix-pack), aiogram 3.27.0 source, Telegram Bot API
> contract. **Read-only**. Cites file:line. Wave-1 findings (C1 hook,
> C2 file_size, C3 plan ambiguities, C4 multimodal replay) are NOT
> revisited.

## Executive summary

Implementation is **mostly clean**. Wave-1 fix-pack landed correctly:
the suffix whitelist + `(file_size or 0)` guard + `TelegramBadRequest`
envelope + boot-sweep unconditional + handler-order tests + clean
sheet-boundary truncation are all in the shipped diff and pinned by
tests.

Wave-2 surfaces **6 NEW concerns** plus **3 carry-forwards** that
should be acknowledged in the commit message or follow-up issue, not
fixed pre-commit.

The single non-obvious risk class is the `F.document` filter's
**over-match surface**: animations (GIFs sent as documents),
videos-as-files, and force-sent stickers populate
`Message.document` per Telegram Bot API §Message and aiogram's
`message.py:215` (`document: Document | None`). The whitelist
catches almost all of them by suffix (animations have
`file_name="something.mp4"` or no name at all → reject), but the
**routing fires on every file-attachment update**, which means the
size + suffix checks become the de-facto media filter. Acceptable
single-owner; documented for 6b/6c.

**Commit-blocked: NO.** Recommendations are all MEDIUM / LOW with
clear owner-trust-model rationales.

---

## NEW findings

### CRITICAL — none.

Wave-1 + spike fix-pack closed every gate-blocking issue. The shipped
code matches `implementation-v2.md` and the live RQ1 probe is the only
remaining unverified gate (researcher already noted it as owner-run).

### HIGH — none.

### MEDIUM

#### M-W2-1 — `F.document` over-matches animations / videos sent as files

`adapters/telegram.py:101` registers `_on_document` on `F.document`.
Per `aiogram/types/message.py:215` the `document` field is set
**also for animations** (GIF / silent H.264 sent uncompressed —
docstring at `message.py:660-672` shows `if self.document` matches
animations and other binary files; the Bot API contract sets BOTH
`animation` and `document` for the same Update on a GIF send). Same
for video sent as a "file" not a "video". The suffix whitelist
(`telegram.py:43`) catches most of them — `.mp4`, `.gif`, `.mov` all
fall outside `{"pdf","docx","txt","md","xlsx"}` and get the Russian
"формат не поддерживается" reply. **But** an owner-side workflow
sending `report.pdf` as the attached file in a media-group with a
photo would still match — and the photo + caption are dropped
silently because aiogram delivers each media-group item as a
separate Update.

**Concrete failure mode:** owner forwards a Telegram message with
`document=report.pdf` AND `animation=preview.gif`. Two Updates
arrive. The PDF one runs through `_on_document`. The GIF one *also*
runs through `_on_document` (animation populates `document` too) →
suffix whitelist rejects (`.gif` not in whitelist) → "формат не
поддерживается" reply. Owner sees one good reply (PDF) and one
spurious reject (GIF) within ~1 sec. UX is noisy but not broken.

**Severity:** MEDIUM. Acceptable single-owner. Carry-forward to 6b
when photo handler lands — at that point `F.document & ~F.animation`
becomes the right filter.

**Mitigation suggestion (NOT pre-commit):** add `F.document & ~F.animation`
filter in 6b alongside the photo handler.

**Cite:**
- `src/assistant/adapters/telegram.py:100-103`
- `.venv/.../aiogram/types/message.py:211-229` (animation/document/sticker
  all `Optional`, populated independently per Bot API)

#### M-W2-2 — `Document.mime_type` is ignored; suffix-only classification

`Document.mime_type` is populated by Telegram from the client's
content-type sniff. The shipped code sniffs only `Document.file_name`
suffix at `telegram.py:191`. Owner-malicious case (single-user trust
model — irrelevant) aside, **the legitimate failure mode** is a file
named `report.pdf` whose `mime_type="application/octet-stream"`
because the sender's client failed to detect the mimetype. The
suffix matches → `bot.download` succeeds → `pypdf.PdfReader` raises
`PdfReadError` → `ExtractionError("corrupt PDF")` → quarantine +
"не смог прочитать файл" reply. UX: the failure is detected, just
later than necessary.

**Severity:** MEDIUM. Quarantine path handles it. Adding a
`mime_type` cross-check would catch the failure earlier (before
download), but adds UI rejection complexity for legitimate sniff
misses.

**Recommendation:** accept current behaviour; document in
`implementation-v2.md` §11 as a known carry-forward to phase 9
alongside `python-magic`.

**Cite:**
- `src/assistant/adapters/telegram.py:189-196`
- `.venv/.../aiogram/types/document.py:25` — `mime_type: str | None`

#### M-W2-3 — Concurrent `bot.download` with no per-chat serialisation

`adapters/telegram.py:101` registers `_on_document` against aiogram's
default `handle_as_tasks=True` dispatcher (verified
`.venv/.../aiogram/dispatcher/dispatcher.py:358,397-411`). Three
documents posted within ~100ms produce 3 concurrent
`_on_document` tasks. Each task downloads to a unique uuid path
(`telegram.py:208`) so no on-disk collision; `bot.download`'s default
`timeout=30` (`.venv/.../aiogram/client/bot.py:450`) bounds each call.

**Two concrete impacts:**

1. **Telegram getFile rate-limit risk.** Telegram's bot API rate-limit
   is ~30 reqs/sec/bot across all chats. Three concurrent `getFile`
   calls inside a 1-second window are well under that, but a media-
   group of 10 docs (Telegram's max) could brush against it. The
   per-chat handler lock (`handlers/message.py:172-186`) serialises
   only AFTER download.
2. **VPS bandwidth.** 3 × 19 MB download in parallel saturates a
   small VPS uplink. Single-owner; non-issue in practice.

The on-disk side is safe (uuid filenames, per-chat lock serialises
the SDK call). The **concurrency surface is the network**, not state.

**Severity:** MEDIUM (theoretical). Pre-commit no-op. If 6a smoke
shows getFile 429s, `tasks_concurrency_limit=4` on the dispatcher
would bound it.

**Cite:**
- `src/assistant/adapters/telegram.py:100-103`
- `.venv/.../aiogram/dispatcher/dispatcher.py:358-411`
- `.venv/.../aiogram/client/bot.py:446-484`

#### M-W2-4 — `_handler is None` document path is silent — owner sees no reply

`adapters/telegram.py:173-175`:

```
if self._handler is None:
    log.warning("document_received_without_handler")
    return
```

If a doc arrives during a narrow window between `adapter.start()` and
`set_handler()` (which can't happen in the current `Daemon.start()`
ordering — handler is set BEFORE `adapter.start()` at
`main.py:343-344`), the owner uploads silently fail. Pre-existing
pattern with `_on_text`. The `log.warning` is observability — but
no Telegram reply, no retry hint.

**Severity:** MEDIUM if the ordering ever flips. Pre-commit
recommendation: assert or `await message.reply("сервис ещё стартует, повтори через секунду")`.

**Cite:**
- `src/assistant/adapters/telegram.py:117-120, 173-175`
- `src/assistant/main.py:343-344` (handler set BEFORE `adapter.start()`).

#### M-W2-5 — Quarantine rename failure silently drops the file

`handlers/message.py:438-443`:

```
except OSError as rename_exc:
    log.warning(
        "quarantine_rename_failed",
        path=str(msg.attachment),
        error=repr(rename_exc),
    )
```

If `rename` raises (cross-device move, permissions glitch), control
falls through. Then the outer `finally` in `_handle_locked`
(`handlers/message.py:397-405`) runs `attachment.unlink(missing_ok=True)`
— **deleting the failed file from its original location**. Result:
post-mortem evidence is gone. Log carries the path + error, so the
incident is recoverable from journalctl, but the corrupted DOCX is
not on disk for owner inspection.

**Severity:** MEDIUM. Quarantine on different filesystem than
`/app/.uploads/` would never be picked (both inside `/app` per
Dockerfile §6a). On Mac dev, `<project_root>/.uploads/` is one fs.
Cross-device rename only happens if owner mounts `.failed/`
elsewhere — unsupported config.

**Recommendation:** acceptable. Document the trade-off.

**Cite:**
- `src/assistant/handlers/message.py:427-443, 397-405`

#### M-W2-6 — `bot.download` 30s default timeout surfaces as opaque error

`bot.download(doc, destination=tmp_path)` (`telegram.py:220`) inherits
`timeout=30` from the SDK default. A 19 MB file on a 5 Mbps uplink
takes ~30s nominally; transient network jitter pushes it over. The
download raises `asyncio.TimeoutError` (not `TelegramBadRequest`) →
the catch-all `except Exception` at `telegram.py:233` fires → reply:
`f"не смог скачать файл: {exc}"`. Owner sees `не смог скачать файл:
TimeoutError(...)` — unhelpful.

**Severity:** MEDIUM. Acceptable for owner-only; the 20 MB cap +
typical home-fibre uplink keeps practical timeouts under 30s.
Tightening would require an explicit `timeout=120` arg + a Russian
"timeout" reply variant.

**Recommendation:** monitor. If owner reports timeouts in 6a smoke,
add `timeout=settings.bot_download_timeout_s` (default 90).

**Cite:**
- `src/assistant/adapters/telegram.py:218-245`
- `.venv/.../aiogram/client/bot.py:446-453` (`timeout: int = 30`).

### LOW

#### L-W2-1 — `_QUARANTINE_RETENTION_S` is a private module constant

`main.py:51` defines `_QUARANTINE_RETENTION_S = 7 * 86400`. Not a
`Settings` field. Owner can't tune via env without code edit. Plan
acknowledges as 6e enhancement (`.failed/` size cap deferred). LOW.

**Cite:** `src/assistant/main.py:48-51`

#### L-W2-2 — `_DEFAULT_FILE_CAPTION` is a hardcoded Russian literal

`telegram.py:48` — `_DEFAULT_FILE_CAPTION = "опиши содержимое файла"`.
Future i18n / multilingual would have to relocate. Pre-existing
pattern (`_on_non_text` reply at `telegram.py:278` is also Russian
literal). LOW.

**Cite:** `src/assistant/adapters/telegram.py:48, 278`

#### L-W2-3 — `extract_xlsx` formula caveat surfaces empty cells silently

`extract.py:185-209` — `data_only=True` returns `None` for any
formula cell whose cached value is missing (script-generated XLSX).
The cell renders as `""` in the TSV output. Model sees `5\t10\t\t`
and may not realise a formula was elided. Documented in
`extract.py` docstring + tested at `test_files_extract_xlsx.py:185-209`.
Wave-1 L3 already flagged. LOW (acknowledged trade-off).

**Cite:** `src/assistant/files/extract.py:152-234`

#### L-W2-4 — DOCX extractor drops tracked changes / footnotes / hyperlinks silently

`extract.py:88-89` docstring lists the dropped items. `iterchildren()`
walks `<w:p>` / `<w:tbl>` only; `<w:ins>`/`<w:del>` runs (track
changes) inside paragraphs survive (text content is gathered) but
their accept/reject state is lost. **Footnotes** live in a separate
OPC part (`footnotes.xml`) — completely absent from output.
**Embedded charts/equations** silently dropped. Single-user trust
model accepts this; owner re-attaches if footnotes matter.

**Cite:** `src/assistant/files/extract.py:77-145`

#### L-W2-5 — Boot-sweep `iterdir()` non-atomic vs adapter not-yet-started

`main.py:79-86` iterates `uploads_dir.iterdir()` then unlinks each.
The plan's correctness argument is "adapter hasn't started — no
turn in flight". Verified at `main.py:317-322` (sweep runs at
line 322; `adapter.start()` runs at line 392). LOW (correct as
implemented).

**Cite:** `src/assistant/main.py:317-322, 392`

#### L-W2-6 — `_handle_extraction_failure` has no test for `quarantine_rename_failed`

`tests/test_handler_attachment_branch.py:283-330` tests the happy-path
quarantine. There is no test for the OSError-on-rename branch
(`handlers/message.py:438-443`). Defensive code is uncovered; if a
future refactor introduces a typo in the OSError handling, no test
catches it.

**Cite:** `tests/test_handler_attachment_branch.py:283-330`,
`src/assistant/handlers/message.py:427-443`

---

## Carry-forwards (do NOT block commit)

1. **History replay marker drift (devil M3, wave-1).** Persisted user
   row contains `[file: NAME]` marker but no extracted bytes
   (`handlers/message.py:223`). On the next turn,
   `bridge/history.py:62-65` replays the marker as plain text. The
   model sees "user previously uploaded file.pdf" with no content.
   Owner re-attaches if needed — documented behaviour. Verified test
   `test_user_row_persists_marker_not_extracted_text`.

2. **Multi-file media-group no-op.** Plan §A defers to 6e. Each
   document in a 10-doc media group dispatches a separate
   `_on_document` and runs the full extract → bridge round-trip. Per-
   chat lock serialises them but each costs a full Claude turn. UX
   tax.

3. **OCR for image-PDFs.** `extract.py:298-313` returns the
   `PDF_NO_TEXT_LAYER_HINT` string. Phase 6e enhancement.

---

## Unknown unknowns

- **Telegram `Document.thumbnail` field** — populated for some uploads.
  We never read it. If the owner uploads a video-as-file and the
  thumbnail is an inline JPEG, we silently ignore it. Unlikely to
  matter for PDF/DOCX/etc.
- **`bot.download` chunk_size=65536 (64 KB)** — fine for SSD-backed
  `/app/.uploads/`, but on a slow disk the synchronous chunk write
  inside the async iterator could block the event loop. Pre-existing
  aiogram default; not phase-6a-introduced.
- **PyPI `pypdf 5.x` is below current LTS 6.10.x.** Wave-1 H6 raised
  this. Plan caps `<6` (`pyproject.toml:18`). Rationale not in
  comments — accept as a known pin.

---

## Verdict

🟢 **Proceed.** No CRITICAL / HIGH new findings. Wave-1 + spike fix-
pack landed correctly. Six MEDIUM concerns are all
defer-able / acceptable under single-owner trust model. Six LOW
concerns are documentation / minor coverage gaps.

**Top 3 things to acknowledge (commit message or follow-up issue,
NOT pre-commit fix):**

1. `F.document` matches animations / videos-as-files; the suffix
   whitelist is the de-facto media filter. Phase 6b should add
   `F.document & ~F.animation` when the photo handler lands.
2. `bot.download` default `timeout=30` may surface opaque
   `TimeoutError` for 19 MB files on slow uplink. Add a `timeout=90`
   arg if owner reports it in 6a smoke.
3. Test coverage gap: `_handle_extraction_failure` OSError-on-rename
   branch is uncovered (`handlers/message.py:438-443`).

**Live RQ1 in-container probe is the ONLY remaining gate** (researcher
flagged in `implementation-v2.md` §9.2). Coder owns the verification;
PASS → ship as Option C; FAIL → flip `_is_pdf_native_read` to
`return False` and ship pypdf-uniform.
