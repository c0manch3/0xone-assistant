# Phase 6a — Devil's Advocate Wave 1

> Reviewer: devil's-advocate. Inputs: `plan/phase6a/description.md`,
> existing source under `src/assistant/`, `deploy/docker/{Dockerfile,
> docker-compose.yml}`, project memory, `.venv/.../aiogram` v3.27.0,
> `.venv/.../claude_agent_sdk` v0.1.59 bundled. Frozen owner decisions
> (formats, caps, tmp dir convention, hybrid arch, Russian replies)
> are NOT relitigated below.

## Executive summary

The plan is structurally sound but rests on **one unverified premise
that gates the entire architecture** (RQ1: SDK Read tool reading
arbitrary host paths under OAuth-CLI), and ships **at least four
implementation traps** that will break boot or first-upload smoke if
left for the coder to discover. Specifically:

1. The hook denies reads outside `project_root` regardless of any
   "extra root" knob — there is no Settings-driven allow-list today
   (verified `bridge/hooks.py:421-485`). Plan §B option 1 ("allow-list
   extension") is **net-new code**, not a flip; option 2 ("move tmp
   inside `/app`") is a one-line Dockerfile edit. The plan presents
   them as equivalents — they are not.
2. `IncomingMessage` is a `frozen=True` dataclass with `eq=True`
   (default). Adding `attachment: Path | None` makes the class
   un-hashable in dict-keyed sets only if used that way; the real risk
   is **scheduler-injected `IncomingMessage` construction sites**, all
   of which currently use kwargs (verified phase-5 RQ1) — but a
   `dataclass(frozen=True)` field reorder breaks any positional caller.
   Plan must add three fields ONLY at end-of-class with defaults.
   Verified the plan does the right thing — flag is precautionary.
3. `Document.file_size` is `Optional[int]` per
   `aiogram/types/document.py` — direct `> 20*1024*1024` comparison
   raises `TypeError` when Telegram omits it (rare but real for
   forwarded files from older clients). Plan §C does not handle this.
4. Phase-1 catch-all `_on_non_text` will still fire on document
   messages **only if `F.document` handler is registered AFTER it** —
   plan says "text → document → catch-all" but the existing code
   (`telegram.py:63-64`) registers text first and catch-all second,
   with no handler in between. Insertion point matters; off-by-one
   in registration order is silent.

Coder is **not blocked overall** — the four traps above + RQ1 spike
result are needed before code starts.

## CRITICAL (must resolve pre-coder; could fail boot or first smoke)

### C1 — `make_file_hook` has no extra-root knob today

`bridge/hooks.py:421-485` builds the hook with a single `project_root`
arg from `bridge/claude.py:135-136`:

```
HookMatcher(matcher=t, hooks=[make_file_hook(pr)]) for t in FILE_TOOL_NAMES
```

The hook body asserts
`resolved.is_relative_to(root)` where `root = project_root.resolve()`.
There is no `Settings`-driven extension and no list-of-roots arg.
Plan §B claims unblock path #1 is "allow-list extension to
`make_file_hook`" — that's a **new feature**, not a config flip.
Implementation cost: ~30 LoC in `hooks.py` + `claude.py` + a new
`Settings.upload_root` field, plus tests.

**Recommendation:** lock RQ1 to verify Option C feasibility first. If
PASS, prefer **option 2 (tmp inside `/app/.uploads/`)** — single
`mkdir` line in `Dockerfile`, no hook surgery. The owner-decision
"data_dir/tmp/" in §I/§K then ONLY applies to the Option-B formats
(DOCX/XLSX/TXT/MD); PDFs land in a separate sub-tree under `/app`. The
plan does not split this clearly — it implies a single tmp dir.

**Severity:** CRITICAL. RQ1 result reshapes phase 6a's file layout.

### C2 — `bot.download` of >20MB file: real error path

Plan §C: "pre-download size check `message.document.file_size > 20MB`
→ reply and return". Two gaps:

1. `Document.file_size` is `int | None`
   (`aiogram/types/document.py:25`). `None > N` → `TypeError` on
   Python 3. Pre-download check must be `(file_size or 0) > 20MB`,
   OR explicit `if file_size is None: ...` branch.
2. If `file_size` is missing AND the file is actually >20MB,
   `bot.download(file_id=...)` raises **`TelegramBadRequest`** with
   message `"file is too big"` (verified
   `aiogram/exceptions.py:126`). Not `TelegramEntityTooLarge` — that
   one is for SEND-side. Plan §I "Size + safety" doesn't enumerate
   this exception class.

**Mitigation:** wrap `bot.download` in `try/except TelegramBadRequest`
catching message containing `"too big"`, reply Russian error, return.

**Severity:** CRITICAL. First-day failure case for any forwarded
album-attached doc with stripped metadata.

### C3 — Handler order in dispatcher: F.document MUST precede catch-all

Existing `adapters/telegram.py:62-65`:

```
self._dp.message.register(self._on_text, F.text)
self._dp.message.register(self._on_non_text)
```

Plan §C says insert `F.document` between them. aiogram registers
handlers in declaration order; the **first matching filter wins**. A
catch-all (no filter) matches everything including documents — so if
`_on_document` is appended after `_on_non_text`, every upload hits the
catch-all and gets the "медиа пока не поддерживаю" rejection. The plan
states the order correctly; flag is a code-review checklist item.

**Mitigation:** unit test asserts only `_on_document` fires for a
synthetic `Message(document=Document(...))`.

**Severity:** CRITICAL only if missed; trivial to verify.

### C4 — RQ1 PASS criteria are under-defined

Plan §L RQ1: "drop PDF in tmp dir, call SDK `Read`, PASS = Option C".
Missing: what does the SDK return on PASS? A `ToolResultBlock` with
text? A `ToolResultBlock` with `image`/`pdf` content blocks (i.e. the
multimodal claim)? The handler's response-write code currently
classifies blocks by `isinstance(item, TextBlock | ToolResultBlock |
...)` (`handlers/message.py:50-122`); `ToolResultBlock.content` is
typed `str | list[ContentBlock]` per SDK 0.1.59 — if the PDF returns a
multimodal list, the existing `_classify_block` writes `payload =
{"content": item.content}` which serialises a list-of-blocks dict to
SQLite as JSON. Replay-time parsing has not been exercised for this
shape.

**Mitigation:** RQ1 must observe AND record the exact block shape. If
multimodal list: phase 6a needs a `_classify_block` extension test.

**Severity:** CRITICAL — direct impact on conversations table replay.

## HIGH (likely to surface in smoke if not fixed)

### H1 — Dual cap interaction on adversarial XLSX

Plan §F: per-sheet cap `50×30` AND total cap `200K chars`. With 100
sheets × 1500 cells × 10 chars ≈ 1.5M chars, the 200K cap fires mid-
sheet. Plan does not specify ordering; coder will likely write
"per-sheet first, then total truncate" which produces a final string
that **chops a sheet boundary mid-row** — model gets garbage TSV.

**Mitigation:** total-cap MUST land on a clean boundary. Pseudocode:

```
out = []; total = 0
for sheet in wb.sheetnames:
    chunk = render_sheet(sheet, max_rows=50, max_cols=30)
    if total + len(chunk) > 200_000: break
    out.append(chunk); total += len(chunk)
out.append(f"[…truncated at {total}/200000 chars; {n_remaining} sheets skipped]")
```

**Severity:** HIGH — silent corruption of model input on big workbooks.

### H2 — 200K char cap is char-based; SDK envelope may be byte-billed

200K Cyrillic UTF-8 chars = ~400 KB raw bytes. The Anthropic API
counts tokens (≈ 1 token / 2 chars for Cyrillic = 100K tokens for one
file). A 100K-token user message + history (`history_limit=20` turns)
risks exceeding the model's input budget on a single upload. Plan
§I doesn't mention budget overlap.

**Mitigation:** lower the cap to **80K chars** for non-PDF formats, or
make the cap config-knob `Settings.attachment_extracted_char_cap`.
Truncate marker explains the cut to the model. PDF (Option C) doesn't
hit this — Read tool token cost is reported by the SDK separately.

**Severity:** HIGH — first 200K-char upload could exhaust 200K-token
context window.

### H3 — Boot-sweep race vs handler `finally tmp.unlink`

Plan §I says "boot-time sweep removes `tmp/` files older than 1h".
Edge case: daemon SIGKILL → restart < 1h. File from previous boot is
still < 1h old → skipped. If daemon SIGKILLs again (e.g. OOM loop),
tmp accumulates indefinitely under 1h.

**Mitigation:** boot-sweep should be **unconditional for all files in
`tmp/` at process start** (every file-at-boot is by definition stale —
the previous run is dead). The 1h bound applies to runtime-detected
orphans (from a separate periodic task, not boot). Plan §F12 author
already noted this internally; the description.md text contradicts.

**Severity:** HIGH — quiet disk fill in a crash-loop scenario.

### H4 — Quarantine dir unbounded growth

Plan §E: "quarantine on `ExtractionError`: rename to `tmp/.failed/`.
Boot-prune older than 7 days." Owner sends 30 corrupted PDFs to
debug. 30 × 20 MB = 600 MB. 7-day window. Plan §I lacks a size cap.

**Mitigation:** `du -sh data_dir/tmp/.failed/` at boot; if > 200 MB,
prune oldest first regardless of age until under 200 MB. Add as
boot-sweep secondary policy.

**Severity:** HIGH on adversarial input; LOW on owner-only flow.

### H5 — `/app` writable to uid 1000 BUT `/app/.uploads/` chown timing

Phase-5d Dockerfile already does `RUN chown -R 1000:1000 /app` at
line 194. If RQ1 picks Option 2 (`/app/.uploads/`), plan §H says "add
one `RUN mkdir -p /app/.uploads && chown 1000:1000 /app/.uploads`".
Order matters: `chown -R /app` is at line 194; if the new `mkdir`
lands AFTER it, ownership is correct; BEFORE, the recursive chown
re-asserts. Either is fine. But: the bind-mount may overlay `/app` —
verify compose mounts. Reading `docker-compose.yml`: only
`~/.claude` and `~/.local/share/0xone-assistant` are mounted; `/app`
is image-layer only. Safe.

**Severity:** HIGH if missed (boot crashes on first upload).

### H6 — `pypdf>=5.0,<6` caps below current LTS

PyPI `pypdf` latest is **6.10.2**. Plan caps at `<6`. Reasoning may
be intentional (5.x is the stability line owner trusts) but plan
should cite the rationale. If RQ4 happens to test with an installed
6.x in dev, behavior may diverge from production install.

**Mitigation:** either bump cap to `<7` after a smoke against 6.10.2,
or document why 5.x is pinned (e.g. API regression in 6.x).

**Severity:** HIGH if owner asks "why is this version old".

### H7 — Memory @tool fcntl.flock is sync polling

`tools_sdk/_memory_core.py:605-647` uses `time.sleep(0.05)` in a
fcntl polling loop with default timeout. If a model-issued
`mcp__memory__memory_write` lands during a long PDF analysis turn,
the MCP server worker blocks 50ms × N. Not a phase-6a regression
(predates this phase) but worth noting for the test plan: load test
with concurrent file-upload + memory write to confirm bridge doesn't
deadlock.

**Severity:** HIGH for tail latency; not a correctness bug.

## MEDIUM (worth fixing during impl, not gates)

### M1 — `Document.file_name` is also Optional

`Document.file_name: str | None`. Plan §C extension whitelist relies
on a `.split('.')[-1].lower()` sniff. If `file_name=None`, fallback
to MIME (`Document.mime_type`) or reject with hint "файл без имени —
не могу определить формат".

### M2 — `IncomingMessage` field count growth

Already 5 fields (chat_id, message_id, text, origin, meta). Adding 3
more brings to 8. Phase 6b adds photo, 6c adds voice. Plan should
adopt sub-record pattern NOW: `attachment: AttachmentInfo | None`
where `AttachmentInfo(path, kind, filename)`. Saves three field
re-adds in 6b/6c. Plan §D rejects this implicitly without comment.

### M3 — Persisted user row with `[file: X.pdf]` marker

Plan §E persists a marker in the `conversations` table. Replay path
in `bridge/history.py` will then re-inject the marker on every
subsequent turn — the model sees a stale "[file: yesterday.pdf]"
context with no actual content. Either (a) strip these markers at
replay time, or (b) document that the marker is for owner-side
forensics only and inert at SDK replay (because the file is gone).

### M4 — Distinct from existing `data_dir/run/tmp/`

`tools_sdk/_installer_core.py:776` already uses `data_dir / "run" /
"tmp"` for installer staging. Plan §I uses `data_dir / "tmp"` for
attachments. **Distinct paths**, but coder may conflate. Spell out
the divergence explicitly in the implementation note.

### M5 — UUID-only filenames in quarantine make debugging hard

Plan §C: tmp path = `<uuid>.<ext>`. Quarantined as same name. Owner
runs `ls .failed/` → `e3b0c442.pdf`. Useless. Suggest: append
sanitised original filename:
`<uuid>__<sanitised_orig_name>.<ext>` — sanitise = strip `..`, `/`,
control chars, cap 80 chars. Easier post-mortem.

### M6 — Forwarded docs: plan §J Q6 says "identical for forwards"

True for `message.document` itself, but `message.forward_origin`
(aiogram 3.7+) carries provenance. Plan doesn't say whether to log
it. For the model-context narrative, "the owner FORWARDED a file
from channel @foo" is meaningfully different from "the owner UPLOADED
a file". Suggest including `message.forward_origin` in
`IncomingMessage.meta` if present.

### M7 — `python-docx` raises broad exception types

`python-docx` `Document(path)` for encrypted/corrupt input may raise
`docx.opc.exceptions.PackageNotFoundError`, `KeyError` (mangled
namespace), `ValueError` (custom XML). Plan §F: "raises
`ExtractionError(reason)`". Coder must wrap a broad `except Exception
as exc: raise ExtractionError(...) from exc` — narrow `except
PackageNotFoundError` will let other exceptions blow up the handler.

### M8 — pypdf: empty page text != no text layer

`pypdf.PdfReader.pages[i].extract_text()` returns `""` for both
text-less scanned pages AND pages with text encoded under a
non-standard encoding (some CJK fonts, broken `/Encoding`). Plan §F
"sub-100-char total → OCR hint" lumps these together. The hint is
correct UX; just acknowledge the false-positive: a real text PDF
with 80 chars total (very short) gets the same hint.

## LOW (notes, not action items)

### L1 — Image-size delta estimate

Plan §H "+6 MB". Reality:
- python-docx 1.2.0 wheel ≈ 244 KB.
- openpyxl 3.1.5 wheel ≈ 250 KB.
- pypdf 5.x wheel ≈ 300 KB.

Total < 1 MB. Plan over-estimates 6×. Numbers in §H need updating;
no functional impact.

### L2 — UTF-8 BOM on TXT/MD

Plan §F: "UTF-8 BOM stripped". `Path.read_text(errors="replace")`
does NOT strip BOM (`\ufeff` becomes a literal char in the string).
Use `.lstrip("\ufeff")` after read, or open with
`encoding="utf-8-sig"`.

### L3 — XLSX `data_only=True` caveat

`openpyxl(read_only=True, data_only=True)` returns CACHED formula
results. If the workbook was created and saved without Excel ever
computing the formulas (e.g. python-generated XLSX with `data_only`
fields blank), all formula cells render as `None`. Owner uploads a
script-generated XLSX → all sums show empty. Documented behavior;
flag for the model to know.

### L4 — Concurrent owner-turn vs scheduler-tick on big XLSX

Plan §C "per-chat lock from phase 5b serialises". Correct.
Side-effect: a 30-second extract on a big XLSX delays scheduler
fires. Phase-5b lock has no timeout, so the scheduler's tick will
just wait. Acceptable; flag in §K risks for awareness.

### L5 — `python-magic` deferred is the right call

For single-user trust model, suffix whitelist is fine. Plan §J Q11
defers libmagic; agree. NOT an action item — just confirming the
trade-off is consciously taken.

### L6 — Caption fallback "опиши содержимое файла" for TXT/MD

Plan §C: "empty caption + non-txt file → set
`text='опиши содержимое файла'`". Why is TXT/MD excluded? A bare TXT
upload with no caption: model gets empty user_text + extracted text.
Likely fine — model should infer "the user wants me to read this" —
but inconsistency invites edge case. Either include all 4 formats or
document the exclusion reasoning.

## Assumptions to verify (pre-coder)

| # | Assumption | Status | How to verify |
|---|------------|--------|---------------|
| A1 | SDK `Read` tool can read multimodal PDF from any host path | UNVERIFIED | RQ1 spike |
| A2 | `make_file_hook` denies tmp dir without modification | VERIFIED | `bridge/hooks.py:421-485`, no Settings extension |
| A3 | `IncomingMessage` callers all use kwargs | VERIFIED | phase-5 RQ1 in memory `project_phase5_shipped.md` |
| A4 | `F.document` doesn't conflict with `F.text` filter | LIKELY TRUE | aiogram 3.x `F.text` matches only when `text != None`; document messages have `text=None` |
| A5 | Bind-mount of `data_dir` includes write permission for uid 1000 | VERIFIED | docker-compose.yml: `:rw`, `user: "1000:1000"` |
| A6 | python-docx / openpyxl / pypdf are pure-Python | VERIFIED via PyPI: all three publish pure wheels |
| A7 | OAuth-CLI auth path supports tool-call file paths in the host fs | UNVERIFIED | RQ1 |
| A8 | The 200K-char cap leaves enough budget for `history_limit=20` | UNVERIFIED | back-of-envelope math suggests 80K is safer |

## Scope creep vectors

1. **Allow-list extension in `make_file_hook`** if RQ1 picks option 1
   — net-new code, +1 Settings field, +tests. Drag for phase 6a.
2. **`AttachmentInfo` sub-record** — refactor of `IncomingMessage`
   that's smaller in 6a but bigger total if deferred to 6b.
3. **Multi-file (media-group) handling** — plan §A explicitly defers
   to 6e. Coder MUST ignore subsequent attachments in a media-group;
   silent first-only reduces UX confusion vs. the documented "first
   only" reject path. Consider an explicit reply: "получил 1 из N
   файлов; пришли остальные по одному".
4. **OCR fallback for image-PDFs** (§A "Defer to phase 6e"). Plan is
   firm. No drift expected.
5. **MIME validation via libmagic** (§J Q11). Deferred. No drift
   expected.

## Unknown unknowns

- **Anthropic CLI version inside the bundled `claude` ELF.** The
  multimodal-Read behavior is a CLI feature, not a SDK 0.1.59 feature.
  CLI is bundled at `/opt/venv/lib/python3.12/site-packages/
  claude_agent_sdk/_bundled/claude` (Dockerfile line 110). Its version
  may differ from a `claude --version` on the dev host. RQ1 must run
  inside the Docker image, not on host.
- **Claude OAuth session permissions.** OAuth-CLI sessions may have a
  scope that excludes the multimodal Read tool (similar to how API-
  key auth excludes Files API). Plan does not flag this. RQ1 either
  catches it or skipping the test on dev-host OAuth gives a false
  PASS that fails on VPS.
- **Forwarded docs from large supergroups.** Telegram may rate-limit
  `getFile` for forwarded-from-channel files differently. Edge case,
  unlikely to bite, but uncovered.
- **Symlinks in `/app/.uploads/` (if Option 2 picked).** `Path.resolve()`
  follows symlinks; `is_relative_to(/app)` is preserved. If the bind-
  mount root is symlinked (as on macOS dev `/var → /private/var`),
  resolution may produce a path outside `/app` and the hook denies.
  Not a Linux-VPS issue but bites local dev.

## Verdict

🟡 **Proceed with reservations.** RQ1 must run **inside the Docker
container** (not on dev-host), with the result documenting the exact
shape of the `ToolResultBlock` for a multimodal PDF read. Two
critical traps (C2 file_size None, C3 handler ordering) need
explicit coder checklist items. The fundamental architecture is
sound; the failure modes are all implementation-level, not design.

**Top 3 fixes to bake into the plan before coder starts:**
1. Spell out the **dual tmp-dir layout** if Option 2 is picked
   (`/app/.uploads/` for PDFs vs `data_dir/tmp/` for extracts), and
   the **single-dir layout** if Option 1 is picked.
2. Boot-sweep is **unconditional at process start**, not 1h-bounded.
   The 1h policy applies to a separate periodic sweep.
3. Lower the post-extract cap to **80K chars** (with a `Settings`
   knob) until empirical evidence shows 200K fits the
   `history_limit=20` budget.
