# Phase 6a — spike findings

Date: 2026-04-25. Researcher run from a Mac dev box (no Docker
daemon present). Live container test for RQ1 deferred to owner; all
other RQs ran end-to-end against fresh deps in `/tmp/.spike6a-venv`.

| RQ | Verdict | Notes |
|---|---|---|
| RQ1 — SDK Read inside Docker | **PASS (conditional)** — static analysis only; owner must run the in-container drop + send-from-Telegram test | Recommend Option 1 (tmp dir → `/app/.uploads/`) over Option 2 (hook allow-list extension) |
| RQ2 — openpyxl OOM | **PASS** | Adversarial 308 MB / 100-sheet workbook → peak RSS 42 MB. At realistic 20 MB cap → 7 s extract, 40 MB RSS |
| RQ3 — aiogram routing | **PASS** | Text → text-handler. Doc → doc-handler. Voice → catch-all. No double-fire |
| RQ4 — python-docx Cyrillic fidelity | **PASS** | 100 % char recall on synthetic complex DOCX (Russian, headings, lists, table, mixed bold/italic, superscript) — but **reading order mangled**: tables emit AFTER all paragraphs |

## RQ1 — SDK Read tool with PDF inside Docker (BLOCKER, partially blocked)

Static review of `src/assistant/bridge/hooks.py:421-485` confirms the
plan's diagnosis: `make_file_hook` enforces
`resolved.is_relative_to(project_root)` on every `Read`/`Write`/`Edit`/
`Glob`/`Grep`. In container, `project_root = /app` and the tmp upload
dir at `/home/bot/.local/share/0xone-assistant/tmp/` is OUTSIDE that
root → every model-issued `Read(file_path=...)` will be denied.

The plan offers two unblocks:

1. **Move tmp dir to `/app/.uploads/`** — zero hook change; one new
   Settings knob (`upload_tmp_dir: Path`); one Dockerfile line
   (`RUN mkdir -p /app/.uploads && chown 1000:1000 /app/.uploads`).
2. **Allow-list extension** — `make_file_hook` gains an `extra_root:
   Path | None = None` parameter; `Settings.extra_read_root` plumbs
   `data_dir/"tmp"` exactly.

**Recommendation: Option 1.** Reasons:

- Smaller hook surface. The hook is the most security-critical file
  in the repo (B7/B8/B9/BW1 fixes all live there); we don't want to
  widen it for a feature with a clean alternative.
- Phase-5d already chowned `/app` to uid 1000 — minimal Dockerfile
  delta.
- Vault separation preserved (vault stays at `<data_dir>/vault`,
  outside `/app`).

**What the owner must run on VPS** to actually close RQ1 (full recipe in
`spikes/rq1_static_analysis.md`):

1. Apply Option 1 patch + restart the daemon.
2. `docker exec` a small text-PDF into `/app/.uploads/test.pdf`.
3. Send a Telegram message attaching `test.pdf` + caption `"what does
   it say"`.
4. Watch bridge log: expect `pretool_decision tool_name=Read
   decision=allow`. If `decision=deny`, the patch isn't wired —
   revisit Settings.
5. If `allow` but the model says it can't read PDFs → SDK 0.1.59 OAuth
   path doesn't propagate multimodal PDF payloads. **Fallback: Option
   B uniform** (pypdf pre-extract for PDFs too), drop hybrid.

**Open Q for owner:** does SDK 0.1.59's `Read` actually do multimodal
PDFs over the OAuth-CLI path? Plan §B claims yes; nothing off-VPS can
verify. This is the single linchpin that decides hybrid vs all-extract.

## RQ2 — openpyxl OOM on adversarial XLSX

Script: `spikes/rq2_xlsx_oom.py` (worst-case) and `spikes/rq2b_xlsx_at_cap.py`
(realistic 20 MB cap).

Worst-case (100 sheets × 50K rows × 20 cols → 308 MB on disk, ~6 ×
the 20 MB Telegram pre-download cap):

```
file size:        308.3 MB
sheets:           100
cells read:       100 000   (50×30 cap × 100 sheets, but capped at 50×20 actual)
chars extracted:  530 000
extract time:     117.73 s
peak RSS:         42.2 MB
VERDICT: PASS (target < 512 MB)
```

At-cap (~19 MB on disk, three shapes — wide, tall, single-tall):

```
shape: 100 sheets × 3000 rows × 20 cols → 18.9 MB → extract 7.32 s, RSS 37.9 MB
shape:  50 sheets × 6000 rows × 20 cols → 18.8 MB → extract 7.21 s, RSS 38.3 MB
shape:  10 sheets × 30 000 rows × 20 cols → 18.6 MB → extract 7.16 s, RSS 39.9 MB
```

**Findings:**

- `read_only=True` keeps memory flat regardless of file size — well
  below the 512 MB target.
- ~7 s wall time on a 20 MB worksheet is the realistic worst case;
  Telegram typing-indicator (`ChatActionSender.typing`) keeps the
  user calm. If owner wants tighter SLA, lower row cap (`ROW_CAP=20`
  → ~3 s).
- `chars extracted` at 530 K well exceeds the 200 K post-extract
  text cap from plan §I — the extractor MUST honour the 200 K char
  truncation, not just the 50×30 cell cap. Coder must wire both.

**Parameter pins:**

- `ROW_CAP = 50` — keep.
- `COL_CAP = 30` — keep.
- `read_only=True, data_only=True` on `load_workbook` — keep both.
- Add `wb.close()` in `finally` (already in spike) — openpyxl leaks fd
  on read-only workbooks otherwise.
- Output cap: enforce post-cell-loop, abort early once `chars >=
  200_000`.

## RQ3 — aiogram F.document coexists with F.text

Script: `spikes/rq3_aiogram_routing.py`. Used `Dispatcher.feed_update`
to drive synthetic text / PDF / DOCX-with-caption / voice updates
through the planned 3-handler stack.

```
[PASS] plain text:        text:'hello'                                                ↦ on_text
[PASS] PDF no caption:    document:'report.pdf':caption=None                          ↦ on_document
[PASS] DOCX with caption: document:'doc.docx':caption='summarize this'                ↦ on_document
[PASS] voice:             catchall:voice                                              ↦ on_catchall
VERDICT: PASS (0 failed of 4)
```

**Findings:**

- Handler-order in plan §C is correct: register `F.text` first, then
  `F.document`, then catch-all. aiogram 3.27 picks first-match; no
  double-fire observed.
- `Document` filter does NOT swallow text-with-attachment: a doc with
  caption hits `on_document` only, caption is on `message.caption`
  (NOT `message.text`).
- `message.content_type` is `aiogram.enums.ContentType` enum in 3.x,
  not bare string. Use `.value` or `str(...)` when logging.
- Owner filter (`F.chat.id == settings.owner_chat_id`) at the
  router-level still rejects non-owner before any handler — no change
  needed.

**Parameter pins:**

- Registration order: `dp.message.register(_on_text, F.text); 
  dp.message.register(_on_document, F.document); 
  dp.message.register(_on_non_text)`.

## RQ4 — python-docx Cyrillic + complex layout fidelity

Script: `spikes/rq4_docx_cyrillic.py`. No Russian DOCX corpus on this
machine, so the script *generates* a complex DOCX (Russian heading,
mixed-style runs, bullet/numbered lists, 4×4 Cyrillic table, special
quotes/em-dash/ellipsis), and treats the source string as ground
truth.

```
gt char count:       674
ext char count:      674
char recall:         100.00%
token recall:        99/99 (100.00%)
VERDICT: PASS (target ≥ 95% char recall)
```

**Findings:**

- python-docx 1.2.0 round-trips Cyrillic, em-dash, French quotes,
  ellipsis without mangling.
- Mixed-style runs (bold, italic, superscript) flatten cleanly to
  joined text — exactly what the plan extractor wants.
- **Reading-order caveat:** the plan extractor iterates
  `doc.paragraphs` first, then `doc.tables`. For documents that
  interleave paragraph/table/paragraph blocks, the table content
  appears *after* all body text in the extracted output. Char
  recall is unaffected, but the model sees a re-ordered narrative.
  Mitigation options:
  1. Document-order traversal via `doc.element.body` iteration over
     `<w:p>` and `<w:tbl>` children (loses python-docx's nice API but
     keeps order).
  2. Annotate the dispatch: `f"[table follows paragraph {n}]"` markers.
  3. Accept the loss — single-user, mostly short reports — and
     document the limitation.

  Recommend: **Option 1** (document-order traversal). ~10 LOC delta;
  no new dependency. Sketch:

  ```python
  def extract_docx_ordered(path):
      doc = Document(path)
      out = []
      from docx.oxml.ns import qn
      for child in doc.element.body.iterchildren():
          if child.tag == qn("w:p"):
              text = child.text or ""
              # walk runs to recover full paragraph text
              ...
              if text.strip():
                  out.append(text)
          elif child.tag == qn("w:tbl"):
              for row in child.iter(qn("w:tr")):
                  cells = [
                      "".join(t.text or "" for t in cell.iter(qn("w:t")))
                      for cell in row.iter(qn("w:tc"))
                  ]
                  out.append("\t".join(cells))
      return "\n".join(out)
  ```

- **Caveats not covered by this synthetic test** (real DOCX may have
  these; flag if owner reports issues):
  - Tracked changes / revisions (`<w:ins>`, `<w:del>`).
  - Real footnotes (separate XML part — python-docx ignores by
    default).
  - Comments, hyperlinks, embedded images.
  - SmartArt / equations.
  - Encrypted DOCX → python-docx raises `PackageNotFoundError`.
    Wrap in `ExtractionError("encrypted")`.

**Parameter pins:**

- Use document-order traversal (sketch above) instead of plan's
  paragraphs-then-tables order — same dependency, ~10 extra LOC.

## Owner Q's surfaced by spikes (additions to plan §J)

- **Q11** — Container test for RQ1: which day can the owner run the
  in-container PDF Read test? Phase 6a coder is blocked on this
  result.
- **Q12** — Reading-order: accept paragraphs-then-tables (simpler) or
  switch to document-order traversal (10 LOC, RQ4-recommended)?
- **Q13** — Worst-case 20 MB XLSX takes ~7 s wall-clock. Acceptable
  UX, or should `ROW_CAP` drop to 20 (≈3 s)?

## Parameter pins (consolidated)

| Param | Value | Source |
|---|---|---|
| `XLSX_ROW_CAP` | 50 | plan §F + RQ2b |
| `XLSX_COL_CAP` | 30 | plan §F + RQ2b |
| `XLSX_OUTPUT_CHAR_CAP` | 200 000 | plan §I — must be enforced post-cell-loop |
| `openpyxl.load_workbook` flags | `read_only=True, data_only=True` | RQ2 |
| Telegram pre-download cap | 20 MB | plan §I |
| `python-docx` extraction order | document-order | RQ4 caveat |
| aiogram register order | text → document → catch-all | RQ3 |
| Tmp upload dir (recommendation) | `/app/.uploads/` | RQ1 Option 1 |

## Hard rules followed

- VPS daemon untouched (no `systemctl`, no `docker exec` against
  production).
- No GHCR push.
- Spike scripts under `plan/phase6a/spikes/`.
- RQ1 live test documented as owner-only; static analysis only here.
