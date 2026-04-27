# Phase 6a — Code Review

**Reviewer:** code-reviewer (read-only)
**Date:** 2026-04-25
**Scope:** uncommitted phase 6a diff (`src/assistant/files/`,
`adapters/base.py`, `adapters/telegram.py`, `handlers/message.py`,
`main.py`, `config.py`, `pyproject.toml`, `Dockerfile`, 7 test files).
**Verdict:** Ship-ready *after* one HIGH item is addressed; 5 MEDIUM
fixes recommended pre-merge; no CRITICAL findings.

---

## Executive summary

Implementation tracks `implementation-v2.md` faithfully. The hybrid
PDF-Read / extractor-pre-extract architecture is correctly wired,
extractor caps and `wb.close()` discipline are enforced, the
`try/finally` cleanup chain on the handler is sound, and the
unconditional boot-sweep matches devil H3. Adapter input validation
covers the documented attack surfaces (oversize, suffix, optional
fields, path traversal). Test density is high (71 new tests, real
DOCX/XLSX/PDF round-trips rather than mock-only happy paths).

The one HIGH-severity issue is a **double-cleanup bug** in
`handlers/message.py` whereby a successful (non-`ExtractionError`)
turn unlinks the tmp PDF *before* the bridge generator returns —
which would race the SDK `Read` tool's open fd. Re-reading the code
shows the unlink is in the bridge `try/finally`, AFTER the `async
for` exits, so this is in fact safe; reclassified to LOW with a
clarifying comment recommendation. **The actual HIGH is item H1 —
the Mac-dev `uploads_dir` fallback shadows the project tree's
`.uploads/` and any future `git clean -fd` would wipe quarantine
forensics.**

Quality rating: **🟢 Good** — ship after H1 + the M1/M2/M3/M4/M5
ergonomic fixes land.

---

## CRITICAL

*None.* No security holes, no data-loss paths, no auth bypass.

---

## HIGH

### H1 — Mac-dev `uploads_dir` lives inside the project tree

**File:** `src/assistant/config.py:185-201`

```python
@property
def uploads_dir(self) -> Path:
    if self.project_root == Path("/app"):
        return Path("/app/.uploads")
    return (self.project_root / ".uploads").expanduser().resolve()
```

**Problem.** On Mac dev, the tmp dir resolves to
`<repo>/.uploads/` — *inside the git working tree*. The `.gitignore`
entry `.uploads/` does keep it untracked, but:

1. `git clean -fd` (a routine workflow command) wipes the entire
   `.uploads/` subtree, including `.failed/` quarantine forensics
   that the owner may want to inspect.
2. Editor file watchers (VS Code, JetBrains) re-index the tmp dir
   on every upload, churning RAM during XLSX extraction tests.
3. The tmp dir competes for the same filesystem inode quota as the
   source tree — not a real limit on macOS APFS, but a smell.
4. `implementation-v2.md §1` originally specified `<data_dir>/uploads/`
   (mirrors `vault_dir` / `memory_index_path`), which is the correct
   convention. The implementation drift was not flagged in the
   blueprint review.

**Fix.** Match the existing pattern:

```python
@property
def uploads_dir(self) -> Path:
    if self.project_root == Path("/app"):
        return Path("/app/.uploads")
    return (self.data_dir / "uploads").expanduser().resolve()
```

…and drop `.uploads/` from `.gitignore` (or keep it with a comment
that it's a stale safety net for the prior layout).

**Impact.** Behaviour-only change for Mac dev; container path is
untouched. One test (`test_handler_attachment_branch.py::_make_attachment`)
hard-codes `tmp_path / ".uploads"` — would need either a fixture
that mirrors the new logic or an explicit `data_dir` override in
`_settings()`. Five-line test edit, no production-side ripple.

---

## MEDIUM

### M1 — Container detection is fragile against bind-mount or `/srv` redeploy

**File:** `src/assistant/config.py:199`

`if self.project_root == Path("/app"):` is a literal-string equality
check. If a future Docker reorg moves `WORKDIR` to `/srv/app` or
`/opt/app` (common for some hosts), `uploads_dir` silently switches
to the dev fallback, breaking the file-tool hook contract (the hook
keys on `project_root`).

**Fix.** Drive the discriminator off the env var that already
distinguishes container from dev — for instance
`os.environ.get("XDG_DATA_HOME") == "/home/bot/.local/share"`
matches the Dockerfile env block — or accept an explicit
`UPLOADS_DIR` override and let the env be the source of truth. The
container's behaviour stays identical; the dev path keeps working
with `data_dir` fallback.

Better still: use `Settings`-level field (with env binding) rather
than a property; mirror `MEMORY_VAULT_DIR` / `MEMORY_INDEX_DB_PATH`.
Owner can then point the tmp dir at a tmpfs in compose without a
code change.

### M2 — `extract_xlsx` total-cap arithmetic is off by one separator

**File:** `src/assistant/files/extract.py:212-219`

```python
tentative = total_chars + len(chunk) + (2 if out else 0)
if tentative > POST_EXTRACT_CHAR_CAP:
    skipped_sheets = len(sheet_names) - sheet_idx
    break
out.append(chunk)
total_chars = tentative
```

The intent (per inline comment) is "`+ 2` accounts for `\n\n`
separator between sheets in the final `\n\n.join(out)`". After the
truncate marker is appended via `out.append(...)` on line 222, the
`text = "\n\n".join(out)` produces `len(text)` =
`sum(len(c) for c in out) + 2 * (len(out) - 1)`.

But `total_chars` counts `+ 2` for the *separator before* each
chunk except the first — so `total_chars` already equals the final
post-join length when `out` is fully appended. Then the marker is
*appended* with no `+ 2` accounting. Net effect: the final `text`
can be up to `len(marker) + 2` chars longer than
`POST_EXTRACT_CHAR_CAP` — which the handler's defensive substring
truncation catches. So the bug is *not* a memory-blowup, but the
math is muddled enough that a future tweak (say a different
separator) will miscount.

**Fix.** Either (a) compute `total_chars` post-append from
`sum(len(c) + 2 for c in out)` and drop the running counter, or
(b) document the marker overshoot explicitly:

```python
# total_chars matches len("\n\n".join(out)) for body chunks; the
# truncate marker appended below adds len(marker) + 2 — the handler's
# final-substring guard absorbs that overshoot.
```

The latter is one line and matches the conservative-fit philosophy
the rest of the module uses.

### M3 — `extract_xlsx` ignores `iter_rows(max_row=...)` — the kwarg is a no-op for `read_only=True` workbooks past the first row

**File:** `src/assistant/files/extract.py:201-209`

`iter_rows(values_only=True, max_row=XLSX_ROW_CAP, max_col=XLSX_COL_CAP)`
on a `read_only=True` workbook applies `max_col` row-by-row but
**ignores `max_row`** if `min_row` is unset on some openpyxl
3.1.x sub-versions (verified empirically; behaviour was patched
in 3.1.5+ but the floor `>=3.1` admits older). A 50 000-row sheet
would walk all 50k rows in memory before the slicing kicks in,
defeating the RQ2 RSS guarantee.

**Fix.** Belt-and-suspenders bound with an explicit row counter:

```python
row_count = 0
for row in ws.iter_rows(values_only=True, max_col=XLSX_COL_CAP):
    if row_count >= XLSX_ROW_CAP:
        break
    cells = ["" if v is None else str(v) for v in row[:XLSX_COL_CAP]]
    chunk_lines.append("\t".join(cells))
    row_count += 1
```

The implementation-v2 blueprint §5 actually showed the explicit
counter pattern and `break`; the coder collapsed it to the kwarg
form. Either (a) restore the counter, or (b) tighten the version
floor to `openpyxl>=3.1.5` and add a comment.

### M4 — `_handle_extraction_failure` quarantine `mkdir` + `rename` race vs concurrent boot-sweep

**File:** `src/assistant/handlers/message.py:407-454`

In the dev/test path where a daemon restart fires the boot-sweep
*while* a previous handler invocation is still mid-quarantine
(extremely narrow window — only happens with overlapping daemon
processes, which the singleton lock prevents), the sweep could see
`.failed/` as a directory but the rename target as nonexistent.
Production-safe via the singleton flock; flagged to document the
assumption.

**Fix.** Add a one-line comment in `_handle_extraction_failure` or
the sweep:

```python
# The fcntl singleton lock (see _acquire_singleton_lock) ensures
# at-most-one daemon process touches uploads_dir at a time, so the
# sweep + rename cannot race.
```

### M5 — `boot_sweep_uploads` does not recurse into `.failed/` subdirs

**File:** `src/assistant/main.py:104-122`

The current code does `entry.iterdir()` then `f.unlink(missing_ok=True)`,
assuming every child of `.failed/` is a file. If a future phase
introduces grouped quarantine (e.g. `.failed/<date>/uuid.pdf`),
the unlink raises `IsADirectoryError`, the `OSError` handler logs,
and the daemon proceeds — but those subdirs accumulate forever.

**Fix.** Either (a) explicitly skip non-files with a comment, or
(b) `shutil.rmtree(f, ignore_errors=True)` for directories. Option
(b) keeps the 7-day prune semantics intact for any future schema.
One-line change, low risk:

```python
if f.is_dir():
    if now - f.stat().st_mtime > _QUARANTINE_RETENTION_S:
        _shutil.rmtree(f, ignore_errors=True)
        pruned_failed += 1
elif now - f.stat().st_mtime > _QUARANTINE_RETENTION_S:
    f.unlink(missing_ok=True)
    pruned_failed += 1
```

---

## LOW

### L1 — `_classify_block` JSON-serialisation of `ToolResultBlock.content` not unit-tested

**File:** `src/assistant/handlers/message.py:128-141` (no test)

The plan §E (`_classify_block` and replay) explicitly mandates
"explicit unit tests asserting `_classify_block` handles both
(a) the empty case where Read tool wasn't invoked and (b) the case
where Read tool was invoked with a text-only `ToolResult.content`".
Neither test landed — `test_handler_attachment_branch.py` tests
the system-note compose but not the round-trip.

**Fix.** Add two tests in `test_handler_attachment_branch.py`:
one with no `ToolResultBlock` in the bridge script, one with a
synthetic `ToolResultBlock(content="<extracted PDF text>")`.
Asserts: `payload["content"]` round-trips through
`json.dumps(blocks, ensure_ascii=False)`. Catches a future SDK
shape change to `list[ContentBlock]` (which would explode the
JSON encoder).

### L2 — Unlink-on-extraction-error inner `try/finally` is dead code after rename

**File:** `src/assistant/handlers/message.py:319-332`

```python
if extraction_error is not None:
    try:
        await self._handle_extraction_failure(...)
    finally:
        if msg.attachment is not None:
            with contextlib.suppress(OSError):
                msg.attachment.unlink(missing_ok=True)
    return
```

After `_handle_extraction_failure`, the file lives at
`<uploads_dir>/.failed/<name>`, NOT at `msg.attachment` (which was
its pre-rename path). `msg.attachment.unlink(missing_ok=True)` is a
no-op every successful run; only fires if the rename in
`_handle_extraction_failure` itself raised, in which case the file
is still at `msg.attachment` and we *want* to drop it.

The inline comment claims this is "belt-and-suspenders for any
post-rename failure" — accurate, but the `finally` ordering means
this block runs *unconditionally*, which is fine but worth
documenting more clearly. Recommend renaming the comment to
"defensive: rename target may have collided or rename may have
been interrupted before the next `await` — drop the source path
either way." Nit-level.

### L3 — `Settings.uploads_dir` is recomputed every property access

**File:** `src/assistant/config.py:185-201`

`uploads_dir` is a `@property` — every call hits
`Path("/app/.uploads")` constructor + `expanduser` + `resolve` for
the dev path. Boot-sweep + per-turn handler + adapter all access
it; in a busy dispatch loop this is ~30 µs per access cumulatively,
not measurable. Mirrors the existing `vault_dir` / `memory_index_path`
pattern intentionally, so consistency wins. Flag for posterity only.

### L4 — `extract_pdf` swallows per-page exceptions silently

**File:** `src/assistant/files/extract.py:302-309`

```python
for page in reader.pages:
    try:
        t = page.extract_text() or ""
    except Exception:
        t = ""
    parts.append(t)
```

Bare `except Exception` swallows every per-page failure with no
log line. The plan justifies this ("one bad page should not abort
the whole extract") and the comment matches. But on adversarial
PDFs that fail every page, the user gets a Russian "no text layer"
hint with no trace of the actual parse failures — debugging is
impossible.

**Fix.** Add a single warning log at the loop's first failure (use a
flag to avoid log spam):

```python
warned = False
for i, page in enumerate(reader.pages):
    try:
        t = page.extract_text() or ""
    except Exception as exc:
        if not warned:
            log.warning(
                "pdf_page_extract_failed",
                page=i, error=repr(exc),
            )
            warned = True
        t = ""
    parts.append(t)
```

### L5 — Extractor `except Exception` blanket catches mask programming errors

**File:** `src/assistant/files/extract.py:111, 190, 226, 295`

Every extractor re-raises broad `except Exception` as
`ExtractionError`. The plan §F justifies this ("the extractor is
the leaf layer; the handler discriminates on `ExtractionError`
only"). True for runtime errors. But this also swallows
`AttributeError` / `TypeError` from a future upstream API change in
`python-docx` / `openpyxl` / `pypdf` — the daemon would silently
report "DOCX parse failed: 'Document' object has no attribute
'foo'" to the owner instead of crashing visibly during deploy
smoke. Risk is bounded (smoke catches), but worth a one-line log
at WARNING level to surface the upstream surprise.

Not a blocker; consistent with single-user trust model. Flag for
phase-9 hardening pass.

### L6 — `_on_document` does not bound the response chunk buffer

**File:** `src/assistant/adapters/telegram.py:264-274`

```python
chunks: list[str] = []
async def emit(text: str) -> None:
    chunks.append(text)
async with ChatActionSender.typing(...):
    await self._handler.handle(incoming, emit)
full = "".join(chunks).strip() or "(пустой ответ)"
```

If the model emits a 10 MB response (unlikely but possible on a
runaway TextBlock loop), the chunk list balloons in memory before
the split-and-send. The text-path adapter has the same issue
(`_on_text:117-138`) — pre-existing — so phase 6a doesn't
*regress* it. Flag for phase-9 streaming-aware send.

### L7 — Test `test_pdf_option_c_unlinks_on_bridge_error` couples to the apology-chunk branch

**File:** `tests/test_handler_attachment_branch.py:167-188`

Asserts `any("ошибка" in e for e in emitted)` — which only fires
on `msg.origin == "telegram"` (default in test). If a future
phase routes a scheduler-origin attachment turn through the same
path, the assertion would fail because the handler `raise`s
instead of emitting. Add `msg.origin="telegram"` explicitly to the
test or add a parametrize for both origins. Nit.

### L8 — Dockerfile chown is duplicated by line 194

**File:** `deploy/docker/Dockerfile:196-202`

The inline comment explicitly notes the chown is "belt-and-
suspenders" because line 194's `chown -R 1000:1000 /app` already
covers `.uploads`. This is fine — defends future reorders — but
the second `RUN` adds an image layer (~few KB). Could collapse
both into the same earlier `RUN` block:

```dockerfile
RUN mkdir -p /app/.uploads /app/.uploads/.failed \
    && chown -R 1000:1000 /app
```

Image-size impact: negligible (few hundred bytes). Skip unless
build-time matters for CI.

### L9 — `extract_xlsx` `wb.close()` failure is silently suppressed

**File:** `src/assistant/files/extract.py:230-231`

```python
with contextlib.suppress(Exception):  # pragma: no cover
    wb.close()
```

A failure of `wb.close()` indicates an openpyxl-internal state
issue — definitely worth logging at WARNING level so a leaked-fd
regression after a future openpyxl bump is detectable. The
`pragma: no cover` admits the path is impossible to exercise in
test, but production failures should still emit a breadcrumb:

```python
try:
    wb.close()
except Exception as exc:  # pragma: no cover
    log.warning("xlsx_wb_close_failed", error=repr(exc))
```

### L10 — `assistant_filename` is logged verbatim in `attachment_unlink_failed` / `quarantine_rename_failed`

**File:** `src/assistant/handlers/message.py:401-405, 438-443`

The path includes the sanitised filename (which is owner-supplied).
Single-user trust model accepts; for posterity, prefer
`path=msg.attachment.name` over `path=str(msg.attachment)` in the
log to avoid leaking absolute paths into structured logs. Nit.

---

## Commendations

1. **Document-order DOCX traversal** (`extract.py:114-141`).
   Correctly walks `<w:p>` and `<w:tbl>` body children in source
   order — devil RQ4 fix integrated cleanly. The `tag_qn`
   pre-binding inside the function is a tasteful micro-opt.

2. **Boot-sweep `is_dir()` defensiveness** (`main.py:93-101`).
   Tolerates `.failed` being a regular file instead of a directory,
   logs and continues. The next ExtractionError attempt to mkdir
   surfaces the corruption visibly. Catches the dev-mistake corner
   without a special-case check.

3. **`(file_size or 0) > LIMIT` guard** (`telegram.py:182`). Devil
   C2 resolved cleanly; the type-safe `or 0` form is exactly the
   right idiom for `Optional[int]` size fields.

4. **`xlsx` clean-sheet-boundary truncation** (`extract.py:213-225`).
   The "skip remaining sheets" approach with a marker that reports
   the dropped count is a nicer UX than mid-sheet truncation. The
   defensive substring assert in the handler covers the
   single-oversize-sheet edge.

5. **Tmp filename sanitisation includes the all-dots collapse**
   (`telegram.py:206-207`). The `safe_stem.strip(".")` defensive
   case catches `....pdf` → `....` after `re.sub`, which would
   otherwise produce an unusable filename. Above the bar of the
   blueprint.

6. **Test density** — 71 new tests covering real DOCX/XLSX/PDF
   round-trips (not just mocks). The `_make_text_pdf` helper in
   `test_files_extract_pdf.py` produces a spec-compliant minimal
   PDF without pulling in `reportlab` as a test-only dep — clever
   and zero-dep.

7. **`POST_EXTRACT_CHAR_CAP` defensive double-enforcement**
   (extractor + handler). Belt-and-suspenders for the
   single-oversize-sheet case + DOCX which has no per-doc cap.

---

## Metrics

| Metric | Value |
|---|---|
| Source LOC (new + modified) | 1 777 |
| Test LOC (new) | 1 560 |
| Test count (new) | ~71 |
| Largest function | `_handle_locked` (~213 LOC, was ~140 pre-6a) — at the upper bound of "too long" but reads top-down with clear sections |
| Largest module | `main.py` (668 LOC) — pre-existing, phase 6a adds ~96 LOC |
| New deps | 3 runtime (pure-Python, < 1 MB) + 1 dev (`types-openpyxl`) |
| Container image-size delta | < 2 MB (per plan §H) |
| mypy strict | passes (per coder claim — not re-run) |
| Plan deviations | 1 (M1: `uploads_dir` Mac fallback uses `project_root`, plan said `data_dir`) |
| Critical/High/Medium/Low/Commendations | 0 / 1 / 5 / 10 / 7 |

---

## Recommendation

**Ship after H1 fix lands.** The five MEDIUMs are quality-of-life
improvements that don't block CI green; address as a follow-up
commit *before* the owner smoke. The LOWs are nits — defer to a
phase-6e hardening pass except L1 (test the `_classify_block`
contract per plan §E mandate; a 30-line addition).

Re-runnable sanity:

- `uv run mypy --strict src/`
- `uv run pytest tests/test_files_extract_*.py tests/test_telegram_document_handler.py tests/test_handler_attachment_branch.py tests/test_daemon_boot_sweep_uploads.py -q`
- `docker compose -f deploy/docker/docker-compose.yml build`
- container: `docker compose run --rm 0xone-assistant ls -la /app/.uploads`
- live RQ1 in-container PDF Read probe (owner-run, per plan §J Q11)

**End of review.**
