# Phase 6a — Telegram file uploads (PDF/DOCX/TXT/MD/XLSX)

**Pivot context.** Phase 5d shipped Docker on VPS `193.233.87.118`; Telegram smoke (ping, memory, installer marketplace, scheduler) GREEN in container. Owner unfreezes phase 6 as a 4-subphase media split:

- **6a (this) — file uploads** (PDF, DOCX, TXT/MD, XLSX) — VPS-side, no Mac sidecar.
- **6b** — vision (Telegram photos → Claude vision API).
- **6c** — whisper (voice messages → Mac transcription sidecar).
- **6d** — image-gen (Mac flux-server sidecar → image back to Telegram).

Each subphase ships independently with an owner smoke gate. Stale `plan/phase6/description.md` (subagent-infra wave-0 plan) is **superseded** by 6a/6b/6c/6d; 6a does NOT touch subagent infrastructure.

## A. Goal & non-goals

**Goal.** Owner sends a PDF / DOCX / TXT / MD / XLSX attachment to the bot via Telegram. The adapter validates size + extension, downloads to a per-turn tmp file, the handler converts content to text-or-multimodal-block, hands it to `bridge.ask` alongside the user's caption (or "describe / summarize this" auto-prompt when caption empty), the model replies, the tmp file is deleted in `finally`. INPUT only — phase 6a never has the bot generate or upload an attachment back to Telegram.

**Non-goals (phase 6a):**
- Generating attachments back to Telegram (`bot.send_document` / `send_photo` are phase 6d).
- Persisting uploads into the long-term-memory vault. Phase-7 vault git push must NOT see attachments — tmp dir is sibling to vault, never inside it.
- OCR of scanned PDFs (no text layer → empty extraction → UX hint, no tesseract). Defer to phase 6e.
- Watermark / signature analysis, password-protected files, encrypted DOCX/XLSX (reject with hint).
- Audio / video files (phase 6c — voice — and out of scope for the rest).
- Photo / image attachments (phase 6b).
- Telegram media-group (multi-file in one message) — first attachment processed, rest rejected with hint. Defer multi-file to phase 6e.
- ZIP / TAR archive expansion.
- ANTHROPIC_API_KEY auth path (CLAUDE.md invariant — OAuth only).
- Mac sidecar — all 6a processing is VPS-side inside the Docker container.
- **No `make_file_hook` extension.** The file-tool hook keeps a single `project_root` (`/app`) constraint untouched. Tmp dir lives at `/app/.uploads/` (Option 1 — locked post-RQ1). Option 2 (allow-list extension) is rejected: net-new code in the most security-critical file in the repo (B7/B8/B9/BW1 fixes all live there).

## B. Tool surface — file extraction architecture

Three architectural options were on the table; **post-RQ1 the architecture is locked to Hybrid + Option 1 tmp layout**.

- **Option A — `@tool`-per-format.** New `mcp__files__read_pdf(path)` / `read_docx(path)` / `read_xlsx(path)` decorated tools the model invokes per file. Pros: model decides if/how to read. Cons: 4-5 new `@tool` surfaces; per-call latency in the streaming envelope; adds hook surface. **REJECTED.**
- **Option B — Pre-extract in adapter.** Telegram adapter downloads, runs format-specific extractor synchronously, replaces `IncomingMessage.text` with `"<user caption>\n\n[attached file: X.pdf, N chars]\n\n<extracted text>"`. Model never sees the file path. Pros: deterministic; simplest plumbing; no new SDK contract. Cons: PDF text-extract via pypdf is lossy (no tables, no figures, no layout); Claude's native vision-via-Read is strictly better for PDFs. **Adopted for DOCX/XLSX/TXT/MD; PDF fallback if Option C fails the in-container probe.**
- **Option C — SDK `Read` tool with multimodal PDF.** SDK 0.1.59 `Read` is multimodal — Claude reads the bytes directly. DOCX / XLSX / TXT / MD are NOT multimodal-supported by `Read`. **Adopted for PDFs.**

**Locked hybrid:**
- **PDF → Option C** (native `Read` tool, multimodal). System-note appended for the turn: `"the user attached a PDF at path={tmp_path}; you may use Read(file_path={tmp_path}) to inspect it."`.
- **DOCX / TXT / MD / XLSX → Option B** (pre-extract → inject into envelope). One extractor module (`assistant/files/extract.py`) with a dispatch table keyed on suffix.

**Hook constraint resolved.** `bridge/hooks.py:421-485` (`make_file_hook`) restricts `Read` to paths *inside* `project_root` (`/app` in container). The tmp upload dir is therefore placed at **`/app/.uploads/`** (Option 1 from spike-findings RQ1). The hook stays untouched — single `project_root` arg, single `is_relative_to(root)` check. The Dockerfile gains one `mkdir -p /app/.uploads && chown 1000:1000 /app/.uploads` line in the runtime stage; phase-5d already chown'd `/app` to uid 1000 so the explicit chown is a defensive belt-and-suspenders.

The vault stays at `<data_dir>/vault/` (e.g. `/home/bot/.local/share/0xone-assistant/vault/`) — fully separate from `/app/.uploads/`. Phase-7's vault git push only walks `vault/`; attachments under `/app/.uploads/` are never seen by the push.

> **Live container probe still required before coder ships.** Static analysis confirms the hook decision; only an in-container `Read(file_path=/app/.uploads/X.pdf)` call from the model can confirm SDK 0.1.59's OAuth-CLI path actually propagates multimodal PDF payloads. Recipe: `spikes/rq1_static_analysis.md` §"What the owner must run on VPS". If that probe FAILS, fall back to **Option B uniform** (pypdf for PDFs too) — already the §F PDF extractor's role, just promoted from "fallback only" to "primary".

**Why not Anthropic Files API?** SDK 0.1.63's `claude_agent_sdk` doesn't expose Files API endpoints; OAuth-CLI auth path only sends content via the prompt envelope. Files API requires API-key auth — CLAUDE.md forbids.

## C. Telegram adapter changes (`src/assistant/adapters/telegram.py`)

Today, dispatcher registers `(F.text, _on_text)` then catch-all `_on_non_text` that replies *"Медиа пока не поддерживаю — это будет в phase 6"*. 6a inserts a `Document` handler ahead of the catch-all.

**Concrete changes:**
- New handler `_on_document(message)` registered with `F.document` filter, handler order: **text → document → catch-all**. Verified PASS by RQ3 spike (`spikes/rq3_aiogram_routing.py`); aiogram 3.27 picks first-match, no double-fire.
- Pre-download size check: `(message.document.file_size or 0) > 20*1024*1024` → reply *"файл больше 20 МБ — это лимит Telegram bot API; пришли поменьше"* and return without downloading. **The `or 0` guard is required** because `aiogram.types.Document.file_size` is typed `int | None` (Telegram occasionally omits the field for forwarded files from old clients); a naked `> 20*1024*1024` would raise `TypeError` (devil C2).
- Suffix whitelist: `.pdf .docx .txt .md .xlsx` (case-insensitive). Reject everything else with *"формат не поддерживается; список: PDF, DOCX, TXT, MD, XLSX"*.
- `file_name` fallback: `Document.file_name` is `Optional[str]`. Use `doc.file_name or "<unnamed>"` and reject if no usable suffix can be derived (devil M1).
- Tmp path: `<settings.uploads_dir>/<uuid4>__<sanitized_orig>.<ext>` where `settings.uploads_dir` defaults to `Path("/app/.uploads")` in container. UUID prevents collision; sanitized original filename (devil M5) eases post-mortem in `.failed/`. Sanitization: `re.sub(r"[^\w.-]", "_", name)` capped at 64 chars.
- Suffix whitelist sufficient for single-user trust model (no `python-magic` in 6a — defer to phase 9 if MIME validation becomes necessary).
- After download succeeds, build `IncomingMessage(...attachment=tmp_path, attachment_kind="pdf"|..., attachment_filename=original_filename)`. Handler responsible for cleanup (try/finally).
- **Download exception envelope:** wrap `bot.download(...)` in `try/except TelegramBadRequest as exc:` (NOT `TelegramEntityTooLarge` — that one is for SEND-side, devil C2). Match `"file is too big" in str(exc).lower()` to surface the size-cap message; on any other download error, reply *"не смог скачать файл: {exc}"* in Russian.
- Caption fallback: empty caption + any whitelisted format (PDF/DOCX/TXT/MD/XLSX) → set `text="опиши содержимое файла"`. Caption present → use as-is. Inclusive of TXT/MD per devil L6 — consistent UX.

**Forwarded files (devil H4 / Q6).** aiogram exposes `message.forward_from_*` / `message.forward_origin` but `message.document` is populated identically for direct uploads and forwards. Adapter ignores forward metadata; no `IncomingMessage.meta` logging in 6a (revisit in 6b if useful for the model).

**Concurrent upload + scheduler-tick race.** Per-chat lock from phase 5b already serialises owner-turn vs scheduler-turn on same `chat_id`. No new locking needed. Side-effect (devil L4): a 7s XLSX extract delays scheduler ticks queued for `OWNER_CHAT_ID` until the upload turn completes — accepted trade-off for 6a.

## D. `IncomingMessage` shape change (`src/assistant/adapters/base.py`)

Frozen dataclass extension. Three fields — all `None` defaults — appended to the end of the class so positional callers (none today, per phase-5 RQ1 verification) remain stable.

```python
@dataclass(frozen=True)
class IncomingMessage:
    chat_id: int
    message_id: int
    text: str
    origin: Origin = "telegram"
    meta: dict[str, Any] | None = None
    # Phase 6a additions (END-of-class on purpose — devil 'flag is precautionary'):
    attachment: Path | None = None
    attachment_kind: Literal["pdf", "docx", "txt", "md", "xlsx"] | None = None
    attachment_filename: str | None = None
```

Scheduler-origin turns never set these. Handler asserts consistency (all three set or all three None).

**Sub-record refactor deferred (devil M2).** Phase 6b will add a photo attachment, 6c voice. The `AttachmentInfo(path, kind, filename)` dataclass would save one re-add but is a refactor of the most-touched contract in the codebase; deferring keeps phase 6a small and lets 6b own the migration.

## E. Handler changes (`src/assistant/handlers/message.py`)

`ClaudeHandler._handle_locked` branches at top:

1. `msg.attachment is None` → existing path. No regression.
2. `msg.attachment` set:
   - Resolve extractor by `msg.attachment_kind`. Each returns `(text, char_count)` or raises `ExtractionError`.
   - Compose envelope:
     - **PDF (Option C path):** keep `user_text` as caption; append system-note `f"the user attached a PDF at path={msg.attachment}, named {msg.attachment_filename}; use Read(file_path={msg.attachment}) to inspect it before answering."`. Do NOT pre-extract.
     - **DOCX / TXT / MD / XLSX (Option B path):** call extractor; cap output at 200K chars; truncate marker. Replace `user_text_for_sdk = f"{msg.text}\n\n[attached: {filename}]\n\n{extracted}"`.
   - Persist user row with ORIGINAL caption + small marker `[file: {filename}]` (NOT full extracted text — keeps conversations table small).
   - Bridge exception → reply error; success → existing emit path.
   - `finally`: `tmp_path.unlink(missing_ok=True)`. Quarantine on `ExtractionError`: `tmp_path.rename(quarantine_dir / tmp_path.name)`. Boot-time prune > 7 days.

**XLSX dual-cap ordering (devil H1).** The extractor enforces caps in this order — coder MUST follow:

1. **Per-sheet cap at iteration time:** `ROW_CAP=20`, `COL_CAP=30` (RQ2b 7s wall on 20MB → 3s with `ROW_CAP=20`). Owner-frozen Q13.
2. **Total post-extract char cap:** 200_000. Enforced INSIDE the extractor on a **clean sheet boundary** (skip remaining sheets when running total would exceed 200K), then a defensive final assert in the handler:

   ```python
   if len(extracted) > 200_000:
       extracted = extracted[:200_000] + "\n[…truncated at 200000 chars]"
   ```

   The defensive assert handles the worst case where a single sheet exceeds 200K chars on its own (~6700 capped rows × 30 cells × 1 char). Sub-row truncation is acceptable degradation; the truncate marker tells the model.

3. RQ2 verified `read_only=True, data_only=True` keeps peak RSS ≈ 40 MB on adversarial 20 MB input — well below 512 MB target. `wb.close()` in `finally` is mandatory (openpyxl leaks fd otherwise).

**`_classify_block` and replay (devil C4 / RQ1 PASS shape).** Phase 6a does NOT need a `_classify_block` extension. Reasoning:

- The Read tool's `ToolResultBlock` returns text-extracted content for PDFs (the OAuth-CLI path; behavior to be confirmed by the in-container live probe). Even if the SDK ever returns a multimodal-list shape, `_classify_block` already serialises `ToolResultBlock.content` as-is into the `payload["content"]` field; the JSON-encoder handles `list[ContentBlock]` via the dataclass-as-dict round-trip.
- User-side attachment is a **system-note** appended to `user_text_for_sdk`, NOT a multimodal user-content block. The persisted user row is the ORIGINAL caption + `[file: X.pdf]` marker — pure text. Replay path is unchanged.
- Coder verification: explicit unit tests asserting `_classify_block` handles both (a) the empty case where Read tool wasn't invoked and (b) the case where Read tool was invoked with a text-only `ToolResult.content` (string).

## F. Extractor module (`src/assistant/files/extract.py`)

NEW package `assistant/files/`. Tiny: ~180 LOC (RQ4 document-order traversal adds ~10 over the naive iteration).

- `extract_docx(path) -> tuple[str, int]` — `python-docx`. **Document-order traversal** via `doc.element.body.iterchildren()` walking `<w:p>` and `<w:tbl>` children in source order (RQ4 fix). Naive `doc.paragraphs` then `doc.tables` mangles the model's reading sequence on documents that interleave paragraph/table/paragraph blocks. Drops styles + images. Sketch:

  ```python
  from docx.oxml.ns import qn
  for child in doc.element.body.iterchildren():
      if child.tag == qn("w:p"):
          # Walk runs to recover full paragraph text.
          text = "".join(t.text or "" for t in child.iter(qn("w:t")))
          if text.strip():
              out.append(text)
      elif child.tag == qn("w:tbl"):
          for row in child.iter(qn("w:tr")):
              cells = [
                  "".join(t.text or "" for t in cell.iter(qn("w:t")))
                  for cell in row.iter(qn("w:tc"))
              ]
              out.append("\t".join(cells))
  ```

  RQ4 verified 100% character recall on synthetic Cyrillic + headings + bullet/numbered lists + 4×4 table + mixed bold/italic/superscript + special quotes/em-dash/ellipsis. **Caveats not covered** (flag if owner reports issues): tracked changes (`<w:ins>`/`<w:del>`), real footnotes (separate XML part), comments, hyperlinks, embedded images, SmartArt, equations.
- `extract_xlsx(path) -> tuple[str, int]` — `openpyxl.load_workbook(read_only=True, data_only=True)`. **All sheets, `ROW_CAP=20`, `COL_CAP=30` per sheet** (post-RQ2b retune; was 50×30, owner Q13). CSV-like `\t`-separated; `\n\n` between sheets prefixed `=== Sheet: <name> ===\n`. Total post-extract char cap 200K, enforced on a clean sheet boundary inside the extractor (devil H1). `wb.close()` in `finally` is mandatory.
  - `data_only=True` caveat (devil L3): cells with formulas-but-no-cached-result render as `None`. Owner-uploaded XLSX from real Excel will have cached values; script-generated XLSX without an `openpyxl` save-and-reopen pass may show empty sums.
- `extract_txt(path) -> tuple[str, int]` — `Path.read_text(encoding="utf-8-sig", errors="replace")`. **`utf-8-sig`** (NOT `utf-8`) auto-strips a leading UTF-8 BOM (devil L2). `Path.read_text(errors="replace")` on plain `utf-8` would leave `\ufeff` literal in the string.
- `extract_md(path) -> tuple[str, int]` — identical to `extract_txt`.
- `extract_pdf(path) -> tuple[str, int]` — **fallback** for the case where the live RQ1 probe shows the model can't multimodal-Read PDFs over the OAuth-CLI path. `pypdf.PdfReader`; concatenate `page.extract_text()`. Sub-100-char total → return `("[PDF appears to have no text layer; OCR not available in phase 6a]", 0)`. Devil M8 acknowledged: a real text PDF with <100 chars total gets the same hint — accepted false-positive for the single-user trust model.

All extractors raise `ExtractionError(reason)` on encrypted / corrupted files. **Catch broad** (devil M3, M7): wrap the underlying parser in `try: ... except (PackageNotFoundError, BadZipFile, KeyError, ValueError, Exception) as exc: raise ExtractionError(reason="…") from exc` — narrow `except PackageNotFoundError` lets `KeyError` (mangled XML namespaces) and `ValueError` (custom XML) bubble up and crash the handler. Top-level `except Exception` is acceptable because the extractor is the leaf layer; the handler discriminates on `ExtractionError` only.

## G. Dependencies (`pyproject.toml`)

New runtime deps:
```toml
"python-docx>=1.1,<2",   # ~244 KB wheel, pure-python, MIT
"openpyxl>=3.1,<4",      # ~250 KB wheel, MIT
"pypdf>=5.0,<6",         # ~300 KB wheel, fallback path
```

Devil L1: total wheel delta < 1 MB. The earlier "+6 MB" estimate was loose; image-size delta lands at < 2 MB after wheel install + bytecode compile. Well below the 1 GB red line.

`pypdf>=5.0,<6` (devil H6): cap is intentional — pypdf 5.x is the stability line owner trusts; pypdf 6.x has API regressions in `extract_text` (whitespace handling differences). Revisit on a future phase after a smoke against 6.10.2.

Optional, decision pending: `python-magic>=0.4,<0.5` (~30 KB Python + 5 MB libmagic1 apt). **Default: skip in 6a — defer to phase 9** (devil L5; suffix whitelist sufficient for single-user trust model).

## H. Container changes (`deploy/docker/Dockerfile`)

- New deps propagate via `uv sync` automatically (pure-python wheels).
- Image size delta: < 2 MB (was estimated +6 MB; corrected per devil L1). Well below the 1 GB red line.
- **Mandatory:** add the runtime-stage line below to mkdir + chown the uploads root. Phase-5d's existing `RUN chown -R 1000:1000 /app` (Dockerfile line 194) re-asserts ownership recursively, so order does not matter — but explicit chown is belt-and-suspenders against future Dockerfile reorders.

  ```dockerfile
  # Phase 6a — uploads tmp dir for the file-attachment hybrid (Option 1).
  # /app is already chown'd to 1000:1000 by line 194; the explicit chown
  # here defends against future reorders. The hook in bridge/hooks.py
  # only allows Read inside /app, so /app/.uploads/ is the canonical
  # location for downloaded attachments.
  RUN mkdir -p /app/.uploads /app/.uploads/.failed \
      && chown -R 1000:1000 /app/.uploads
  ```

- Bind-mount safety verified by devil H5: `docker-compose.yml` only mounts `~/.claude` and `~/.local/share/0xone-assistant`; `/app` is image-layer only — no bind-mount overlay risk.

## I. Size + safety

- **Pre-download cap:** 20 MB hardcoded (Telegram bot API limit). Russian reject message. `(file_size or 0) > 20*1024*1024` to handle `Optional[int]` gracefully (devil C2).
- **Post-extract cap:** 200K chars. Truncate marker. RQ2b verified the cap with `ROW_CAP=20`/`COL_CAP=30` keeps a 20 MB XLSX extract at ~3s wall-clock and well under 200K. Cap stays at 200K (under Sonnet's 200K-token window comfortably) — owner-frozen, not 80K. The 80K-char downshift recommended by devil H2 is rejected: it is a back-of-envelope token-budget concern; any actual context-window pressure surfaces via the SDK's own `usage` and the model can summarise.
- **Tmp dir:** `/app/.uploads/` (single layout — Option 1). `settings.uploads_dir` exposes the path; defaults to `Path("/app/.uploads")` in container. For Mac dev where `/app` does not exist, fall back to `<data_dir>/uploads/` (with the understanding that PDF Read tool will be denied off-VPS — develop the extract path on Mac, ship-test PDFs only via VPS smoke).
- **Path validation:** Adapter generates UUID inside `settings.uploads_dir`; handler asserts `tmp_path.resolve().is_relative_to(settings.uploads_dir.resolve())` to defeat any future path-injection via `attachment_filename` sanitisation bypass. Devil "Symlinks on macOS dev" is accepted dev-only flakiness — production VPS has no symlinked `/app`.
- **File leak prevention:** `try/finally tmp_path.unlink(missing_ok=True)`. Open-file-handle race on `unlink` is safe on Linux (devil H6): `os.unlink` removes the inode entry; the SDK subprocess does not retain the fd after `Read` returns; eventual GC reclaims the inode.
- **Quarantine:** `ExtractionError` → `<uploads_dir>/.failed/<uuid>__<sanitized_orig>.<ext>` (devil M5 — sanitized original filename appended for post-mortem ergonomics).
- **Boot-sweep policy (devil H3 — UNCONDITIONAL):** every file directly under `/app/.uploads/` at `Daemon.start()` is by definition stale (the prior daemon died — its in-flight uploads are orphaned). Wipe ALL top-level entries unconditionally; for `.failed/` entries, prune those older than 7 days. The 1h-bound policy from the prior plan is DROPPED (was open to crash-loop disk fill). Sketch in the implementation blueprint §6.
- **Quarantine size cap (devil H4 partial):** 7-day prune is the primary policy. A secondary boot-time cap "if `/app/.uploads/.failed/` total size > 200 MB → prune oldest first regardless of age until under 200 MB" is **deferred to phase 6e** unless the owner reports `.failed/` growth in 6a smoke. Justification: single-owner deployment + suffix whitelist + 20 MB pre-download cap together bound adversarial growth at ≤ 1.4 GB / 7d in the worst case; in practice 0 unless owner deliberately spams corrupted files.
- **Encrypted file rejection:** `ExtractionError("encrypted")` → reply *"файл зашифрован — пришли расшифрованный"*.
- **MIME spoofing risk (devil L5):** accepted via single-user trust model. Suffix whitelist + extractor-side parser failure on bad MIME is the safety net; libmagic deferred to phase 9.

## J. Owner Q&A (13 items — 10 phase-5 distinct + 3 spike-surfaced)

| # | Question | Recommendation |
|---|----------|----------------|
| **Q1** | Architecture — hybrid (Read-for-PDF + extract-rest) vs all-extract, vs all-tool-per-format? | **Hybrid LOCKED.** RQ1 static-analysis PASS conditional; live in-container probe required before coder commits. If live FAIL → pypdf-based all-extract uniform. |
| **Q2** | XLSX — first sheet only, all sheets? Cap rows? | **All sheets, 20×30 per sheet hard cap** (was 50×30; Q13 retune), `[…truncated…]` markers. |
| **Q3** | DOCX include images? | **Text-only in 6a**; images dropped silently. Phase 6b vision will handle inline images. |
| **Q4** | Failed extraction — quarantine + Telegram reply, or silent log? | **Quarantine + Telegram reply** (`"не смог прочитать файл: <reason>"`). |
| **Q5** | Size > 20 MB — Russian or English message? | **Russian**. |
| **Q6** | Forwarded files? | **Allow** — `message.document` identical for forwards. No `IncomingMessage.meta` logging in 6a. |
| **Q7** | Concurrent burst (5 files) — sequential or parallel? | **Sequential** via per-chat lock from phase 5b. |
| **Q8** | Persist filename in conversation history? | **Yes, marker only** (`[file: X.pdf]` in user row). Not extracted bytes. |
| **Q9** | File without caption — auto-prompt? | **Yes, default Russian "опиши содержимое файла"** for ALL whitelisted formats including TXT/MD (devil L6 consistency). |
| **Q10** | Image-size delta on Docker — CI passes if 6a adds new wheels? | **Yes** — actual delta < 2 MB (was estimated ~6 MB; corrected per devil L1). Compressed image well below red line. |
| **Q11** | Container test for RQ1 — when can owner run the in-container PDF Read test? | **Owner runs before coder commits.** Recipe in `spikes/rq1_static_analysis.md`. PASS → ship hybrid; FAIL → pypdf-uniform fallback path already exists in §F. |
| **Q12** | DOCX reading-order: paragraphs-then-tables (simpler) or document-order traversal (RQ4-recommended)? | **Document-order traversal** via `doc.element.body.iterchildren()` (~10 LOC delta; same dependency). Owner-frozen. |
| **Q13** | Worst-case 20 MB XLSX takes ~7s wall-clock at `ROW_CAP=50`. Acceptable UX, or drop to 20 (~3s)? | **Drop to `ROW_CAP=20`** for 3s SLA; `COL_CAP=30` keeps. Owner-frozen. |

## K. Risks

1. **🟢 SDK `Read` tool path-resolution inside container** — RESOLVED via Option 1 (move tmp to `/app/.uploads/`). Hook untouched. RQ1 static analysis PASS; live in-container probe still required before coder commits — owner runs the recipe in `spikes/rq1_static_analysis.md`. If live FAIL, fall back to pypdf-uniform via the §F `extract_pdf` already-implemented path.
2. **🟢 openpyxl OOM on adversarial XLSX** — RESOLVED. RQ2 verified peak RSS 42 MB on a 308 MB worst-case workbook with `read_only=True, data_only=True`. Plus `wb.close()` in `finally`. `ROW_CAP=20`, `COL_CAP=30` per Q13 retune.
3. **🟢 Devil C2 `Document.file_size` `Optional[int]`** — RESOLVED via `(file_size or 0) > 20*1024*1024` guard + `try/except TelegramBadRequest` envelope around `bot.download(...)`. See §C.
4. **🟢 Devil C3 handler-registration order** — RESOLVED. RQ3 verified `text → document → catch-all` registration order works in aiogram 3.27 with no double-fire.
5. **🟢 Devil C4 multimodal Read replay shape** — RESOLVED. `_classify_block` does not need extension; user-side attachment is a system-note (text), and Read tool's `ToolResultBlock.content` is JSON-serialised round-trip safely. Coder writes explicit unit tests for the empty-Read and text-only-ToolResult cases.
6. **🟡 PDF with no text layer (scanned).** pypdf returns empty string. **Mitigation:** sub-100-char detection → Russian hint. OCR phase 6e.
7. **🟡 Concurrent upload + scheduler tick on `OWNER_CHAT_ID`.** Per-chat lock serialises. No new race; side-effect: 7s XLSX extract delays scheduler ticks (devil L4 — accepted).
8. **🟡 File MIME spoofing.** Single-user trust model accepts risk; libmagic deferred to phase 9 (devil L5).
9. **🟢 python-docx Cyrillic mangling** — RQ4 verified 100% char recall + document-order traversal fix.
10. **🟢 Tmp file leak on SIGKILL** — boot-sweep is **unconditional** at `Daemon.start()`; the 1h-bound was open to crash-loop disk fill (devil H3) and is dropped.
11. **🟢 Phase-7 vault git push leak.** Vault at `<data_dir>/vault/`, tmp at `/app/.uploads/`. Fully separate trees; phase-7 plan only walks `vault/`.
12. **🟢 Image-size creep.** 6a < 2 MB delta (devil L1 — earlier "+6 MB" was loose); cumulative monitored phase-by-phase.
13. **🟡 Quarantine `.failed/` unbounded growth** — 7-day prune is the primary policy; devil H4 size-cap deferred to phase 6e unless owner reports growth in 6a smoke. Bounded at ≤ 1.4 GB / 7d worst-case (single-owner + 20 MB cap).
14. **🟡 Memory @tool fcntl polling latency** (devil H7) — not a phase-6a regression; load-test concurrent file upload + memory write to confirm no deadlock. Test plan §8.
15. **🟡 Replay-time `[file: X.pdf]` marker drift** (devil M3) — accepted as forensics-only marker; the actual file is gone post-cleanup, so the marker is inert at SDK replay (model sees the marker as plain text in user history, no Read call attempted).

## L. Spikes status

All four spikes ran (see `spike-findings.md` and `spikes/`). Summary:

- **RQ1 — SDK `Read` tool with PDF inside Docker.** **PASS (conditional).** Static analysis confirms Option 1 (`/app/.uploads/`) is the right unblock path; live in-container probe is owner-run pre-coder. Recipe in `spikes/rq1_static_analysis.md`. NOT a coder blocker — pypdf-uniform fallback path is in §F as a primary if the live probe fails.
- **RQ2 — openpyxl OOM.** **PASS.** Adversarial 308 MB / 100-sheet workbook → peak RSS 42 MB. At-cap (~19 MB) workbooks extract in ~7s with `ROW_CAP=50`, ~3s with `ROW_CAP=20` (Q13 frozen). `read_only=True, data_only=True, wb.close() in finally` mandatory.
- **RQ3 — aiogram routing.** **PASS.** Registration order text → document → catch-all routes correctly with no double-fire; verified via `Dispatcher.feed_update`.
- **RQ4 — python-docx Cyrillic.** **PASS.** 100% char recall on synthetic complex DOCX; reading-order caveat fixed by `doc.element.body.iterchildren()` traversal (Q12 frozen).

**Coder blockers:** none from the spikes. The only remaining gate is the live in-container RQ1 probe, which the owner runs immediately before coder commits the first attachment-handling code.

## M. Phase 6b/6c/6d prerequisites unlocked by 6a

- **6b vision** reuses `IncomingMessage.attachment` + new `_on_document`/`_on_photo` handler split.
- **6c whisper** voice via `F.voice`. Tmp-dir + per-turn-cleanup pattern carries over.
- **6d image-gen** outbound — `bot.send_photo`. Independent of 6a INPUT path.

Cross-cutting: `assistant/files/extract.py` reused by 6e (multi-file). `tmp_path` discipline (boot-sweep + per-turn cleanup) is the contract every media subphase shares.

## Critical files

- `src/assistant/config.py` — add `uploads_dir: Path` setting (default `Path("/app/.uploads")` in container with Mac-dev fallback).
- `src/assistant/adapters/telegram.py` — register `F.document`; size cap with `or 0` guard; whitelist suffixes; `TelegramBadRequest` envelope.
- `src/assistant/adapters/base.py` — extend `IncomingMessage` with three optional fields appended at end of class.
- `src/assistant/handlers/message.py` — branch on `msg.attachment`; compose envelope; tmp-cleanup `finally`; quarantine on `ExtractionError`.
- `src/assistant/files/extract.py` (NEW) — DOCX (document-order)/XLSX (20×30, dual-cap)/TXT/MD/PDF extractors + `ExtractionError`.
- `src/assistant/main.py` — add boot-sweep (unconditional) + 7-day `.failed/` prune in `Daemon.start()`.
- `src/assistant/bridge/hooks.py` — **UNCHANGED.** Single `project_root` constraint preserved.
- `pyproject.toml` — add `python-docx>=1.1,<2`, `openpyxl>=3.1,<4`, `pypdf>=5.0,<6`.
- `deploy/docker/Dockerfile` — `RUN mkdir -p /app/.uploads /app/.uploads/.failed && chown -R 1000:1000 /app/.uploads`.
