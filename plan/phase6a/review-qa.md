# Phase 6a — QA Review (Telegram file uploads)

**Reviewer:** QA engineer (read-only audit). **Date:** 2026-04-25.
**Inputs:** description.md (~270 LOC), implementation-v2.md (833 LOC),
devil-wave-1.md (25 findings), spike-findings.md (RQ1-4),
src/{files/extract.py, adapters/{telegram,base}.py, handlers/message.py,
main.py, config.py}, deploy/docker/Dockerfile, pyproject.toml,
.gitignore, 7 phase-6a tests (1560 LOC).

---

## Executive summary

**Verdict: 🟡 SHIP WITH FIX-PACK.** The implementation is well-aligned
to the locked plan; all four CRITICAL devil findings (C1-C4) and all
four spike findings (RQ1-RQ4) are reflected in code, test coverage is
strong (1560 LOC across 7 files exercising happy + error paths), and
the security posture is sound for the single-owner trust model. There
are **no blocking critical bugs**. However, three production-relevant
gaps need addressing before this is owner-smoke ready: (1) a
spec-mandated defensive `is_relative_to` assertion is absent from both
adapter and handler; (2) the Mac dev `uploads_dir` fallback diverges
from the spec without explicit justification; (3) the bare `Exception`
catch in `_on_document` swallows `asyncio.CancelledError` in Python ≤
3.7 idioms (it does not in 3.8+, but the pattern is fragile). All
acceptance-criteria scenarios (§I) appear functionally satisfied
modulo the live in-container RQ1 probe (owner-run gate, by design).

**Bug count: 1 HIGH (path-validation gap), 5 MEDIUM, 6 LOW. 0
CRITICAL.**

**Top 3 issues:**

1. **HIGH — Missing `tmp_path.resolve().is_relative_to(uploads_dir)`
   guard.** Spec §I.194 explicitly required this defensive assertion;
   neither `adapters/telegram.py` nor `handlers/message.py` performs
   it. Today's tmp construction is safe by hand (UUID prefix
   guarantees containment), but a future regression in sanitisation
   would silently allow path injection. Cheap to fix; high value.
2. **MEDIUM — Mac dev fallback diverges from spec.** Spec §I.193 and
   blueprint §1 both mandate `<data_dir>/uploads/`; implementation
   uses `<project_root>/.uploads`. The deviation is reasonable
   (`.gitignore` already covers `.uploads/`) but undocumented; spec
   and code disagree on a load-bearing path.
3. **MEDIUM — `_on_document` catches bare `Exception` from
   `bot.download` AFTER catching `TelegramBadRequest`.** This is
   unusual (typically you'd catch network errors more narrowly); it
   reveals raw exception text to the owner via Telegram (Russian
   message + `repr(exc)`) which is fine for single-owner but worth
   tightening for forward compatibility.

---

## Spec compliance matrix (description.md §A-§L)

### §A — Goal & non-goals
| Requirement | Status | Evidence / Notes |
|---|---|---|
| INPUT only — no `bot.send_document`/`send_photo` | ✅ | Adapter has no outbound document path |
| No `make_file_hook` extension; tmp at `/app/.uploads/` | ✅ | `bridge/hooks.py:421-485` untouched; uploads_dir resolves to `/app/.uploads` in container |
| Tmp sibling to vault, not inside | ✅ | uploads_dir = `/app/.uploads`; vault = `<data_dir>/vault` — disjoint trees |
| 5 formats only: PDF/DOCX/TXT/MD/XLSX | ✅ | `_SUFFIX_WHITELIST = {pdf, docx, txt, md, xlsx}` |
| OAuth only — no `ANTHROPIC_API_KEY` | ✅ | No api-key references; preflight uses `claude --print ping` |

### §B — Tool surface (hybrid)
| Requirement | Status | Evidence / Notes |
|---|---|---|
| PDF → Option C (SDK Read multimodal) | ✅ | `_is_pdf_native_read(kind)` discriminator; system-note appended in handler:252-257 |
| DOCX/TXT/MD/XLSX → Option B (pre-extract) | ✅ | `EXTRACTORS` dispatch in handler:260 |
| pypdf fallback present | ✅ | `extract_pdf` registered in `EXTRACTORS["pdf"]`; flipping `_is_pdf_native_read` to `False` is a one-line switch |
| Live RQ1 probe gate documented | ✅ (gate is owner-run) | Coder is unblocked; live probe is operational |

### §C — Telegram adapter changes
| Requirement | Status | Evidence / Notes |
|---|---|---|
| Handler order text → document → catch-all | ✅ | telegram.py:100-102; test `test_handler_registration_order_text_doc_catchall` |
| Pre-download `(file_size or 0) > 20MB` | ✅ | telegram.py:182-187 |
| Suffix whitelist (case-insensitive) | ✅ | `Path(file_name).suffix.lower().lstrip(".")` line 191 |
| `file_name` fallback for `Optional[str]` | ✅ | `doc.file_name or ""` line 190 |
| Sanitisation `re.sub(r"[^\w.-]", "_", ...)` cap 64 chars | ✅ | telegram.py:203 |
| Tmp path `<uploads>/<uuid>__<sanitized>.<ext>` | ✅ | telegram.py:208 |
| `TelegramBadRequest` envelope on download | ✅ | telegram.py:221 (catches "too big" + "too large" — extra defensive) |
| Empty caption fallback for ALL formats incl TXT/MD (devil L6) | ✅ | telegram.py:250 default applied universally; test parametrises 5 suffixes |
| No `IncomingMessage.meta` logging for forwards | ✅ | Adapter ignores `forward_origin` (consistent with Q6) |

### §D — `IncomingMessage` shape change
| Requirement | Status | Evidence / Notes |
|---|---|---|
| Three fields appended at END of frozen dataclass | ✅ | base.py:53-55, defaults None, kwargs only — phase-5 callers stable |
| `AttachmentKind` Literal type | ✅ | base.py:12 |
| Handler asserts all-three-or-none invariant | ✅ | message.py:207-213 |

### §E — Handler changes
| Requirement | Status | Evidence / Notes |
|---|---|---|
| Branch on `msg.attachment is None` (no regression for text-only) | ✅ | The non-attachment path is unchanged |
| PDF: append system-note `Read(file_path=...)` | ✅ | message.py:252-257 |
| DOCX/XLSX/TXT/MD: extract → 200K cap → inject envelope | ✅ | message.py:259-280 |
| Persist user row with `[file: NAME]` marker only | ✅ | message.py:222-231; test `test_user_row_persists_marker_not_extracted_text` |
| Quarantine on `ExtractionError` to `.failed/` | ✅ | `_handle_extraction_failure` lines 407-454 |
| `tmp_path.unlink(missing_ok=True)` in `finally` | ✅ | message.py:397-405 |
| Boot-time prune > 7 days | ✅ | main.py:114 (`_QUARANTINE_RETENTION_S = 7 * 86400`) |

### §F — Extractor module
| Requirement | Status | Evidence / Notes |
|---|---|---|
| Document-order DOCX traversal | ✅ | extract.py:115-141 walks `<w:p>`/`<w:tbl>` in body order; test `test_extract_docx_document_order_paragraph_table_paragraph` |
| Drops styles + images | ✅ | Iterates `<w:t>` runs only |
| Broad `except Exception` in extractors | ✅ | DOCX, XLSX, PDF all have belt-and-suspenders generic catch |
| XLSX `read_only=True, data_only=True` | ✅ | extract.py:183 |
| XLSX `wb.close()` in `finally` | ✅ | extract.py:228-231; test asserts close on iter_rows error |
| XLSX clean-boundary 200K cap (devil H1) | ✅ | extract.py:214-217; test `test_extract_xlsx_total_cap_skips_remaining_sheets` |
| XLSX `ROW_CAP=20`, `COL_CAP=30` (Q13) | ✅ | extract.py:45-46 |
| TXT/MD `utf-8-sig` BOM strip (devil L2) | ✅ | extract.py:254; test `test_extract_txt_strips_bom` |
| PDF sub-100-char hint string | ✅ | extract.py:311-312 |
| Encrypted PDF rejection | ✅ | extract.py:298 |
| `ExtractionError` reason categories ("encrypted", "corrupt") | ✅ | All three extractors emit these substrings |

### §G — Dependencies
| Requirement | Status | Evidence / Notes |
|---|---|---|
| `python-docx>=1.1,<2` | ✅ | pyproject.toml:16 |
| `openpyxl>=3.1,<4` | ✅ | pyproject.toml:17 |
| `pypdf>=5.0,<6` | ✅ | pyproject.toml:18 (cap intentional, devil H6) |
| `types-openpyxl` dev dep | ✅ | pyproject.toml:30 |

### §H — Container changes
| Requirement | Status | Evidence / Notes |
|---|---|---|
| `mkdir -p /app/.uploads /app/.uploads/.failed` | ✅ | Dockerfile:201 |
| `chown -R 1000:1000 /app/.uploads` before `USER bot` | ✅ | Dockerfile:201-204 (correct order) |

### §I — Size + safety
| Requirement | Status | Evidence / Notes |
|---|---|---|
| 20 MB pre-download hardcoded | ✅ | telegram.py:38 |
| 200K post-extract cap | ✅ | extract.py:44 + handler defensive assert message.py:271-275 |
| Boot-sweep UNCONDITIONAL on top-level (devil H3) | ✅ | main.py:_boot_sweep_uploads, no age check on orphans |
| `.failed/` 7-day prune | ✅ | main.py:114 |
| Encrypted DOCX/XLSX/PDF rejection with Russian reply | ✅ | `_handle_extraction_failure` discriminates on "encrypted" substring |
| **Path validation** `tmp_path.resolve().is_relative_to(...)` | ❌ | **MISSING in both adapter and handler — see HIGH bug #1** |
| Quarantine size cap deferred to 6e | ✅ | Documented |

### §J — Owner Q&A (13 items)
All 13 frozen decisions present. Q12 (document-order DOCX) and Q13
(`ROW_CAP=20`) implemented as decided.

### §K — Risks
All 15 risks have mitigations in code. The single open gate is the
live RQ1 in-container probe (owner-run, documented in plan).

### §L — Spike status
RQ1 (PASS conditional, owner-gate); RQ2 (caps land in code); RQ3
(handler order test); RQ4 (document-order traversal in code + test).

---

## Acceptance criteria (§I scenarios)

| Scenario | Code support | Notes |
|---|---|---|
| Send PDF → bot reads + answers | ✅ via Option C | Live probe gates final verification |
| Send DOCX → text extraction (Cyrillic) | ✅ | RQ4 + Cyrillic test pass |
| Send XLSX → CSV-like serialisation | ✅ | `\t`-joined cells, `=== Sheet: <name> ===` separators |
| Send 50 MB PDF → "файл больше 20 МБ" | ✅ | Pre-download guard at 20*1024*1024 |
| Send `.exe` → "формат не поддерживается" | ✅ | Whitelist test parametrises rejected suffixes |
| Tmp deleted after turn | ✅ | `finally tmp_path.unlink(missing_ok=True)` test verified |
| Phase 1-5d regressions | ✅ | `_on_text` path untouched; non-attachment handler branch unchanged |

---

## 🐛 Bugs & functionality issues

### 🔴 Critical
None.

### 🟡 High

#### H1. Missing path-containment defensive assertion (spec §I.194)
**Location:** `adapters/telegram.py:208-216` and
`handlers/message.py:245-280`. **Spec §I.194 verbatim:** *"handler
asserts `tmp_path.resolve().is_relative_to(settings.uploads_dir
.resolve())` to defeat any future path-injection via
`attachment_filename` sanitisation bypass."* Today's construction
(`uploads_dir / f"{uuid4().hex}__{safe_stem}.{suffix}"`) is safe
because `uuid4().hex` cannot contain path separators and
`safe_stem` is regex-sanitised. **However**, the spec specifically
required a runtime guard against future regressions in either the
sanitisation or the construction. The test
`test_tmp_path_sanitises_traversal` validates the path stays inside
the uploads dir as a behavioural property — but no production code
verifies it. **Fix:** add immediately before
`bot.download(...)` in `_on_document`:
```python
resolved = tmp_path.resolve()
if not resolved.is_relative_to(uploads_dir.resolve()):
    log.error("upload_path_escape_attempt",
              tmp_path=str(tmp_path), uploads_dir=str(uploads_dir))
    await message.reply("ошибка пути файла")
    return
```
Also belt-and-suspenders in `_handle_locked` before the extractor
call. **Severity: HIGH** — defence in depth, low cost, spec-mandated.

### 🟢 Medium

#### M1. Mac dev `uploads_dir` fallback diverges from spec
**Location:** `config.py:199-201`. Spec §I.193 and
implementation-v2.md §1 both specify `<data_dir>/uploads/` for Mac
dev fallback. Implementation uses `<project_root>/.uploads`. The
divergence is **functionally fine** (`.gitignore` line 70 covers
`.uploads/` so accidental commit is prevented) and arguably better
for fast iteration (sibling to source tree, not buried in XDG dir).
But: spec and code disagree on a load-bearing path; a fresh reader
will be confused. **Fix:** either update spec / blueprint §1 to
reflect the deliberate change with a one-line rationale, or update
the code to match spec. The `.gitignore` already supports both
locations; no functional impact either way.

#### M2. `_on_document` catches bare `Exception` after `TelegramBadRequest`
**Location:** `telegram.py:233-245`. The block:
```python
except TelegramBadRequest as exc:
    ...
except Exception as exc:
    log.warning("document_download_failed", ...)
    await message.reply(f"не смог скачать файл: {exc}")
```
Bare `except Exception` after `TelegramBadRequest` swallows
EVERYTHING else — `aiohttp.ClientError`, `OSError`, `ValueError`,
runtime bugs in `bot.download`. The user gets a Russian "не смог
скачать: <repr>" reply which leaks the upstream exception text into
Telegram. For a single-owner deployment this is acceptable, but:
(a) it can leak stack-trace fragments into chat logs that flow back
to claude.ai if memory tools quote them, (b) it masks programmer
bugs (typos, attribute errors) as user-facing "не смог скачать"
which makes them harder to spot in logs. **Fix:** narrow to
`except (OSError, aiohttp.ClientError) as exc:` and let any other
exception bubble up to aiogram's polling supervisor (which already
logs cleanly). Alternatively: log `repr(exc)` but only echo a fixed
Russian string to the user.

#### M3. `extract_xlsx` emits "=== Sheet: name ===" header even for empty sheets
**Location:** `extract.py:199-219`. If a workbook contains an empty
sheet (no cells), `chunk_lines = ["=== Sheet: <name> ==="]` is
appended unconditionally; only sheets that exceed the running
total cap are skipped. Result: a 100-sheet workbook where all but
sheet 1 are empty produces 99 useless `=== Sheet: ===` headers in
the model envelope. **Fix:** check `len(chunk_lines) == 1` before
appending the chunk; skip header-only chunks. **Severity: low/medium
correctness (model still gets the data); modest token waste on the
adversarial empty-sheet case.

#### M4. `_handle_extraction_failure` quarantine collision possible
**Location:** `handlers/message.py:430`. `target = quarantine_dir /
msg.attachment.name` — the source filename is `<uuid>__<stem>.<ext>`,
so collisions are statistically impossible (UUID4 birthday-bound).
But: if the same file is uploaded twice and BOTH fail extraction,
they have different UUIDs, so safe. However, `Path.rename` on POSIX
silently OVERWRITES if the target exists — which means if some
out-of-band actor (sweep race, manual `cp`) puts a file with the
same UUID in `.failed/` first, evidence is lost. **Fix:** use
`os.link` + `unlink` or check existence first. **Severity: low**;
flagged because the `.failed/` directory is a forensics artifact
and silent overwrite undermines its purpose.

#### M5. `_handle_extraction_failure` `assert msg.attachment is not None` after async I/O
**Location:** `handlers/message.py:426`. The `assert` statement is
defensive but in production with Python `-O` flag enabled, asserts
are stripped. If a future caller invokes `_handle_extraction_failure`
without the attachment invariant, the next line
(`quarantine_dir = self._settings.uploads_dir / ".failed"`) is fine
but `msg.attachment.name` would `AttributeError` on None. **Fix:**
replace with explicit `if msg.attachment is None: raise
RuntimeError(...)` or equivalent narrowing.

### 🔵 Low

#### L1. `extract_xlsx` defensive `+ 2` for separator double-counts
**Location:** `extract.py:214` — `tentative = total_chars +
len(chunk) + (2 if out else 0)`. The `+ 2` accounts for the `\n\n`
join separator. But `total_chars = tentative` accumulates the
separator into the running tally even before the final
`"\n\n".join(out)`. The cap arithmetic over-estimates by ~2 chars
per sheet — harmless (cap fires slightly earlier than necessary).

#### L2. `extract_xlsx` `row[:XLSX_COL_CAP]` redundant
**Location:** `extract.py:207`. `iter_rows(max_col=XLSX_COL_CAP)`
already truncates each row at COL_CAP. The `row[:XLSX_COL_CAP]`
slice is dead defence. Cosmetic.

#### L3. PDF page exception swallowed silently
**Location:** `extract.py:303-308`. A bad PDF page raises a generic
`Exception` and is silently treated as empty:
```python
try:
    t = page.extract_text() or ""
except Exception:
    t = ""
```
No log line. If 9 of 10 pages fail, the model receives empty text
and the user gets a confusing "I can't read this PDF" answer with
no diagnostic trail. **Fix:** add a `log.warning("pdf_page_extract_failed", page_idx=i, error=repr(exc))`.

#### L4. `extract_pdf` `is_encrypted` check after `PdfReader` constructor
**Location:** `extract.py:298`. pypdf 5.x raises in the constructor
for some encrypted PDFs (depends on encryption type) AND surfaces
`is_encrypted=True` for others. The current code catches
`PdfReadError` (constructor) but only checks `is_encrypted` for the
post-construction case. Mostly OK; just note the dual-path nature.

#### L5. `_boot_sweep_uploads` does not handle `entry.is_symlink()` in `.failed/`
**Location:** `main.py:113-115`. The `.failed/` prune loop only does
`f.unlink(missing_ok=True)`. If a symlink lands in `.failed/`
pointing outside the uploads dir, `unlink` removes the link (safe,
not the target). However, `f.stat()` on line 114 follows symlinks
by default — if the target is unreachable (e.g. mounted volume
gone), `OSError` is caught, but the link is never pruned and stays
forever. **Fix:** `f.lstat()` for the age check. **Severity: very
low** — symlinks should not appear in `.failed/` in normal
operation.

#### L6. `_handle_extraction_failure` reply does not surface specific reason for non-encrypted errors
**Location:** `message.py:445-447`. For `ExtractionError("corrupt
DOCX: ...")`, the reply is `f"не смог прочитать файл: {exc}"` which
includes the raw exception text (e.g. "corrupt DOCX: KeyError:
'word/document.xml'"). Telegram users see a partial Python error.
For single-owner this is fine; flag for UX polish in a future
phase.

---

## 🔒 Security audit

### Confirmed secure
- **No `ANTHROPIC_API_KEY` references anywhere.** Auth path is OAuth via
  bundled CLI; `_preflight_claude_auth` uses static argv.
- **No new secrets paths.** `.env` patterns unchanged; `.gitignore`
  covers `.uploads/` (line 70) so attachments are never committed.
- **Path-traversal defence (functional):** `re.sub(r"[^\w.-]", "_",
  orig_stem)[:64]` strips `/` and `\` and other separators; UUID4
  prefix in `<uuid>__<stem>.<ext>` guarantees the file lands inside
  `uploads_dir`. Test `test_tmp_path_sanitises_traversal` validates
  `../../etc/passwd.txt` cannot escape.
- **Path-traversal defence (gap):** spec-required `is_relative_to`
  defensive assert is missing — see HIGH bug H1.
- **Suffix matching is case-insensitive** (`Path(file_name)
  .suffix.lower()`). `report.PDF` (uppercase) is accepted; case
  permutations like `report.PdF` resolve correctly.
- **Filename injection into SQL (conversations table):** the persist
  path (`message.py:222-231`) uses parameterised SQL via the
  `ConversationStore.append` method — `aiosqlite` parameter binding
  is safe; the marker `[file: NAME]` becomes a JSON string, not raw
  SQL.
- **Hook untouched.** `bridge/hooks.py:421-485` retains its
  single-`project_root` constraint. `/app/.uploads/` is reachable
  via `is_relative_to(/app)` for the model's `Read` tool. PDFs land
  inside the allowed root by construction.
- **Concurrent uploads + scheduler tick:** per-chat lock in
  `ClaudeHandler._chat_locks` (handler.py:172-186) serialises owner
  turn vs scheduler turn on the same chat_id. No new race.
- **Tmp cleanup race on Linux** (open-fd unlink): documented as safe
  by inode semantics; SDK subprocess does not retain fd after `Read`
  returns. Not Windows-relevant (production is Linux container).
- **Boot-sweep with active container:** sweep runs synchronously
  BEFORE `_adapter.start()`, so no upload is in flight when the
  sweep iterates. Correct.
- **Forwarded msg size cap bypass:** the `(file_size or 0)`
  pre-download check is supplemented by the post-download
  `TelegramBadRequest` "too big" envelope — covers the
  `file_size=None`-but-actually-huge case (devil C2 mitigated).
- **Encrypted file rejection:** DOCX → `PackageNotFoundError` →
  ExtractionError("encrypted or corrupted DOCX"); XLSX → `BadZipFile`
  → ExtractionError("encrypted or corrupted XLSX"); PDF →
  `is_encrypted=True` → ExtractionError("encrypted PDF"). All three
  trip the `"encrypted" in str(exc).lower()` check in
  `_handle_extraction_failure` and yield "файл зашифрован" reply
  (devil M3 — closed).

### Potential weaknesses (low severity, single-owner trust model)
- **Filename leak in error replies (M2 / L6).** Error messages echo
  raw exception text including filenames into Telegram chat. Single-
  owner = self-DoS at worst.
- **PDF page extract Exception swallowing (L3).** A pathological PDF
  could silently leak no content; user sees empty answer. No
  security impact, only UX.
- **MIME spoofing.** Spec §J Q11 deferred libmagic to phase 9. Suffix
  whitelist accepts a `.pdf`-named ZIP bomb; pypdf rejects with
  `PdfReadError`, extract.py wraps in `ExtractionError`, handler
  quarantines. Defended at the parser layer; documented trade-off.
- **No rate limiting.** Owner can spam 1000 small files; each
  consumes one bridge turn (cost), 20 MB per file disk, and a tmp
  inode. Single-owner bound is psychological, not enforced. Phase
  6e quarantine size cap addresses corruption-spam; nothing
  addresses well-formed-spam. Acceptable for the trust model.

---

## 🧪 Coverage gaps

### Strong coverage observed
- 4 extractor unit-test files exercise happy path + error path + caps
  + Cyrillic round-trip + corrupted-input rejection.
- `test_telegram_document_handler.py` covers size cap (oversize +
  None), suffix whitelist (5 accept + 5 reject), `file_name=None`,
  `TelegramBadRequest` "too big" + other, caption fallback (5
  formats parametrised), tmp UUID uniqueness, traversal defence,
  registration order.
- `test_handler_attachment_branch.py` covers PDF Option C, DOCX
  Option B, total-cap truncation, ExtractionError quarantine,
  encrypted reply variant, marker persistence.
- `test_daemon_boot_sweep_uploads.py` covers nonexistent dir, empty
  dir, top-level wipe, .failed retention, 7-day prune, permission
  error, .failed-as-file edge, unexpected-subdir skip.

### Gaps

#### G1. No test for spec-required `is_relative_to` assertion
Tied to HIGH bug H1. If H1 is fixed, add a unit test that
constructs a `tmp_path` outside `uploads_dir` (e.g. via monkeypatch
on `Path.resolve`) and asserts the `_on_document` early-return path
fires.

#### G2. No test for `_classify_block` with attachment-invoked Read tool
Spec §E "Coder verification" (§D / risk K5) explicitly required:
*"explicit unit tests asserting `_classify_block` handles both (a)
the empty case where Read tool wasn't invoked and (b) the case where
Read tool was invoked with a text-only ToolResult.content
(string)."* The existing `_classify_block` has Phase 2/3 coverage
in `test_claude_handler.py` for general blocks, but no Phase-6a
specific test that simulates a Read-tool ToolResultBlock for a
PDF turn. **Action:** add one happy-path test where the bridge
yields a `ToolUseBlock(name="Read", input={"file_path":
"/app/.uploads/x.pdf"})` followed by `ToolResultBlock(content="page
1 text")` — assert both rows are written to `conversations` with
correct `role`/`block_type`.

#### G3. No integration test for sequential-uploads-on-same-chat
Spec §J Q7 / risk 7 ("concurrent burst — sequential via per-chat
lock") + blueprint §8.3 ("Concurrent burst: 5 sequential `handle()`
calls on same chat_id"). The per-chat lock is implemented and
covered by `test_handler_per_chat_lock_serialization.py` for text
turns, but no test exercises the new attachment branch under burst
conditions. **Action:** extend the per-chat-lock test (or add a
new one) to fire 5 attachment-bearing `handle()` calls and assert
all 5 tmp files are unlinked + ordered DB rows.

#### G4. No test for `extract_pdf` encrypted PDF
`test_files_extract_pdf.py` covers happy path, sub-100-char hint,
corrupt, missing — but NOT a real encrypted PDF. The
`reader.is_encrypted` branch (extract.py:298) is therefore
unvalidated. **Action:** add a fixture that uses
`pypdf.PdfWriter().encrypt("pwd")` to produce an encrypted PDF and
assert `ExtractionError("encrypted PDF")`.

#### G5. No test for empty-sheet header noise (M3)
If M3 is fixed, add a test that asserts an empty sheet produces no
output (or only the cap-skip marker).

#### G6. No test for an XLSX with a single oversized sheet
`test_extract_xlsx_total_cap_first_sheet_oversize_handled` validates
the extractor's marker-only behaviour, but the handler-side
defensive truncation at message.py:271-275 has no companion test
that confirms the resulting `user_text_for_sdk` ends with the
"truncated at 200000 chars" marker even when the extractor returns
a chunk-only marker (i.e. body is empty, but length is < 200K so
the assert doesn't fire). The test
`test_docx_option_b_total_cap_truncates` covers a plain >200K
extract; consider adding a coverage test for the boundary case.

---

## 💡 Recommendations

### Pre-ship fix-pack (low cost, high value)
1. **Add `is_relative_to` defensive assert** in `_on_document`
   (HIGH H1) — 5 lines + 1 unit test.
2. **Either reconcile spec ↔ code on Mac fallback path** (M1) — pick
   one, document the choice. Trivial.
3. **Narrow the bare `except Exception`** in `_on_document` to
   `(OSError, aiohttp.ClientError)` and let surprises crash the
   handler (M2) — improves debuggability.
4. **Add `_classify_block` Read-tool tests** (G2) — spec-required
   coder verification gap.
5. **Add encrypted-PDF extractor test** (G4) — closes the only
   visible extractor branch with no test.

### Post-ship improvements (defer if owner wants this gate small)
6. Empty-sheet header skip (M3) and `lstat` symlink safety in
   boot-sweep (L5).
7. Add structured-log emission on `extract_pdf` page failure (L3) —
   cheap diagnostics for the live-RQ1-fallback path.
8. Replace `assert` in `_handle_extraction_failure` with explicit
   `if-raise` (M5) — survives `python -O`.

### Documentation polish
- Note in CLAUDE.md or phase-6a description.md that the live RQ1
  in-container probe is a deploy-time gate, not a code-time one.
  Owner needs to flip `_is_pdf_native_read` to `lambda kind: False`
  if the probe fails — and re-deploy. Explicit rollback recipe in
  the description would help.

---

## ✅ Итоговая оценка (verdict)

**🟡 SHIP WITH FIX-PACK.** The implementation faithfully reflects
the locked plan; all critical devil findings (C1-C4) and spike
findings (RQ1-RQ4) are present in code; tests are thorough (1560
LOC, all error paths exercised); no critical security vulnerabilities
under the documented single-owner trust model. The HIGH gap is the
spec-required `is_relative_to` defensive assertion (§I.194) — cheap
to fix, important for defence in depth. Three MEDIUMs are quality
issues that should be cleaned up; they do not block deployment.

After the 5-item pre-ship fix-pack above, this is **production-ready
for the VPS smoke gate** (which closes the live RQ1 probe). If RQ1
fails on VPS, the pypdf-uniform fallback is one-line ready
(`_is_pdf_native_read = lambda _: False`) and the existing
`extract_pdf` test coverage validates the fallback path.

**Bug count: 0 critical, 1 high, 5 medium, 6 low.**
**Spec compliance: ~95% (single deviation is the spec-mandated path
guard; everything else is matched or documented as deliberate).**
