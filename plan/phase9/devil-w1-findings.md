# Phase 9 — Devil's Advocate Wave 1 (RETHINK)

> Stress-test of `description.md` v0 (944 lines). Spec under attack:
> PDF / DOCX / XLSX `render_doc` MCP @tool, `RenderDocSubsystem`,
> `MessengerAdapter.send_document` ABC extension, bridge artefact-block
> yield, TTL sweeper, Dockerfile apt deps.
>
> Project-frozen scope respected: 3 formats fixed; pandoc + WeasyPrint
> + openpyxl stack fixed; single @tool `render_doc(content_md, format,
> filename?)`; no templates / multi-sheet xlsx in v1. Devil targets
> only mutable surface area.

## Verdict

**v0 is competent skeleton-following but has 5 CRITICAL holes that
will burn the same review wave the same way phase-8 v0 burned —
they're identifiable BEFORE coder ships.** Architecture broadly
mirrors phase 8, which is good for orchestrator brain-load; but the
spec under-specifies the most novel surface (artefact-envelope bridge
yield + handler ledger ordering) and hand-waves the most security-
sensitive surface (markdown injection ⊃ {iframe, @import, but ALSO
`<img file://>`, SVG `<foreignObject>`, `@font-face`, MathML xlink,
`<base>`, `<link>`, CSS `url()` in 4 contexts beyond `@import`)).
**Mandatory revisions before coder.** Estimate v1 → 2-3 spec sections
rewritten + 4-6 new tests + 1 spike.

## Closures (severity, category, claim, proposed v1 fix)

### CRITICAL

#### W1-CRIT-1 — Bridge `ArtefactBlock` yield ordering relative to `ResultMessage` is not enforceable as written

- **What's wrong:** §2.5 Option A says bridge "yield'ит специальный
  объект `ArtefactBlock(...)` ПОСЛЕ оригинального ToolResultBlock", and
  handler "ПОСЛЕ финального `await emit_text(...)` ... делает
  `await self._adapter.send_document(...)`". The handler's barrier for
  "send_document time" is **after the async-for exits cleanly** (see
  `handlers/message.py:828`). But the SDK's streaming semantics
  guarantee NEITHER:
  1. that all `TextBlock`s from the model's *terminal* assistant
     message arrive BEFORE the `ResultMessage`, nor
  2. that the final `TextBlock` is emitted **after** the
     `ToolResultBlock` (model can interleave: `tool_use → tool_result
     → text → tool_use → tool_result → text → text → ResultMessage`).
  v0's "after final flush" works only when there is exactly one
  assistant turn per render. With Fix A in `bridge/claude.py:438-447`
  ("SDK may emit multiple ResultMessage instances per query()"),
  multi-iteration tool loops emit MULTIPLE ResultMessage envelopes;
  v0 doesn't say which one is the artefact-flush barrier.

- **Evidence:**
  - Spec §2.5 step 4: "ПОСЛЕ финального `await emit_text(...)`
    (handler уже дофлашил все text-chunks)" — assumes one terminal
    flush, but `handlers/message.py:438-447` documents that the SDK
    may iterate.
  - `bridge/claude.py:447` ("Fix A: do NOT return here") — multiple
    ResultMessages possible per `ask`.
  - Spec §2.3 keys ledger by `tool_use_id` but never specifies what
    happens when 2 `render_doc` calls share an iteration boundary.

- **Proposed v1 fix:** Replace §2.5 Option A with a flush-barrier
  contract:
  > "Handler maintains a `pending_artefacts: list[ArtefactBlock]`. On
  > every `ResultMessage` (not just the last) and on async-for normal
  > exit, handler flushes the list with `for art in pending_artefacts:
  > await adapter.send_document(...); pending_artefacts.clear()`. This
  > preserves owner-visible order: text-then-document, repeated per
  > model iteration."
  Also fold an explicit AC: "AC#19 — multi-iteration model turn
  emitting render_doc tool_use → text → render_doc tool_use → text
  must deliver TWO documents in turn order, each preceded by its text
  preamble." Add test `test_phase9_bridge_artefact_flush_per_iteration.py`.

#### W1-CRIT-2 — Markdown injection mitigation enumerates only 2 of ≥9 fetch surfaces

- **What's wrong:** §Risk CRITICAL row + AC#14 list `<iframe
  src="file://">` and CSS `@import url(...)`. WeasyPrint actually
  resolves URLs from many more places. Researcher pass is supposed
  to verify, but devil should enumerate so researcher knows the
  surface. Missing surfaces:
  1. `<img src="...">` (raster image, both `file://` and
     `http://internal-vps:9090/metrics` SSRF).
  2. CSS `background: url(...)` / `background-image:` / `list-style-
     image:` / `border-image:` / `cursor:` / `content: url(...)` —
     SIX CSS declarations beyond `@import`.
  3. `<link rel="stylesheet" href="...">` (raw HTML mode; pandoc
     `+raw_html` would pass it through).
  4. `<base href="file:///etc/">` — relative URLs then resolve under
     attacker-chosen base. Defeats `base_url` invariant.
  5. `@font-face { src: url("file://...") format("woff2"); }` —
     local-font disclosure if rendered with `presentational_hints=
     True`.
  6. `<svg>` with `<foreignObject>` or `<image href="...">` or
     `xlink:href="..."` — pandoc commonmark+tex_math+attributes can
     emit SVG verbatim depending on extensions.
  7. `<object data="file://...">` / `<embed src="...">`.
  8. `data:` URI to bypass scheme allow-list naively (text/html with
     embedded `<script>`).
  9. CSS `@import` (already covered) + `url()` value in custom
     properties `--bg: url(...);`.
  Spec also says "WeasyPrint executes CSS, не JavaScript" — but
  WeasyPrint *does* support CSS @page, @media, @supports, and CSS
  custom properties. The "не JavaScript" framing is irrelevant; the
  threat is *fetch*, not *exec*.

- **Evidence:**
  - Spec §2.6: "`<iframe src=...>` или `@import url(...)`" — only 2
    listed.
  - Spec §Risk CRITICAL row 1: same enumeration.
  - WeasyPrint docs (publicly known): every property accepting a
    `<url>` value invokes `url_fetcher`.

- **Proposed v1 fix:** §2.6 PDF renderer + §Risk CRITICAL row 1 +
  AC#14 must enumerate the full fetch surface and mandate a custom
  `url_fetcher` callable that returns an empty body for ANY URL not
  matching a strict scheme allowlist. v1 wording proposal for §2.6
  step 4:
  > "WeasyPrint вызывается с `url_fetcher=safe_url_fetcher` где
  > `safe_url_fetcher(url)` raises `weasyprint.urls.URLFetchingError`
  > для любой схемы кроме `data:` (mime in {`image/png`, `image/jpeg`,
  > `image/gif`, `image/svg+xml`}) — никаких `file://`, `http://`,
  > `https://`, `ftp://`. Это блокирует все фетч-точки разом
  > (img/object/embed/link/base/@import/@font-face/CSS-url())."
  Plus mandate: pandoc `--from=markdown-raw_html-raw_tex-raw_attribute`
  (verify in spike). AC#14 expands to AC#14a–AC#14i, each parametrised
  on one fetch surface (img / @font-face / `<base>` / SVG xlink / CSS
  background / etc.). New test
  `test_phase9_pdf_renderer_url_fetcher_blocks_full_surface.py`.

#### W1-CRIT-3 — TTL sweeper races `send_document` upload mid-flight

- **What's wrong:** §2.1 step 5 + §2.3 envelope: handler hands `path`
  to `adapter.send_document(...)`. `aiogram.types.FSInputFile` opens
  the file lazily at upload time (it's a streaming reader), not at
  construction. If upload is slow (large file, slow Telegram, bot
  retry on 429), the TTL sweeper may delete the file mid-stream.
  `artefact_ttl_s=600` (default 10 min) — for a 19 MB PDF on a
  congested uplink, upload can take 30-60 s; combined with retry
  storms, total elapsed in the upload path can exceed TTL minus the
  margin between artefact creation and adapter dispatch. v0 has NO
  ledger / refcount / "in-flight" guard — sweeper just walks
  `artefact_dir` and deletes by mtime.
  
  Worse: `aiogram.Bot.send_document` on `TelegramRetryAfter` can
  pause for the server-specified retry duration (sometimes 30 s);
  combine with multi-render turn (2 PDFs queued) and the second one
  can sit in handler's local list waiting on the first send to drain
  for several minutes.

- **Evidence:**
  - Spec §2.3: TTL key based on `mtime`, no in-flight bookmark.
  - Spec §2.5 C-flow has no "mark in-flight" step.
  - aiogram `FSInputFile.read()` opens at upload start, not at
    `__init__` — verified in source `aiogram/types/input_file.py`.

- **Proposed v1 fix:** Three options, recommend (b):
  - (a) Skip TTL sweep for files <2× TTL old (degrades to disk-fill).
  - (b) **Move TTL bookkeeping off mtime onto an in-memory "live
    artefact set" owned by `RenderDocSubsystem`.** When @tool returns,
    artefact is added with a `created_at` timestamp + `in_flight:
    bool = True`; handler calls `subsystem.mark_delivered(path)` AFTER
    `send_document` resolves (success OR final failure); sweeper
    deletes only artefacts where `not in_flight AND now - delivered_at
    > artefact_ttl_s`. Boot-time `_cleanup_stale_artefacts` still
    sweeps mtime-based (no in-memory state survives restart).
  - (c) Move sweep to `expires_at` field stored in a sidecar `.json`
    next to each artefact.
  Recommend (b). Add AC#20: "render_doc producing 2 artefacts in one
  turn → both must deliver even if Telegram returns 429 with 60 s
  retry-after on the first." Add `test_phase9_sweeper_skips_inflight.py`
  and `test_phase9_sweeper_collects_after_delivery.py`.

#### W1-CRIT-4 — `Daemon.stop` cancels in-flight `render_doc` mid-pandoc, leaks orphan subprocess + staging files

- **What's wrong:** §2 says render is "synchronous in @tool body" via
  `asyncio.create_subprocess_exec` for pandoc + `asyncio.to_thread`
  for WeasyPrint. Spec §2.1 #5 mentions sweeper for delivery sweep, but
  §3 Wave C "C7 — `test_phase9_daemon_drain_render_inflight.py` (если
  render длится дольше Daemon.stop budget — orphans cleaned by next-
  boot `_cleanup_stale_artefacts`)" treats orphans as "next-boot
  problem". That's wrong on three axes:
  1. **Pandoc subprocess is not anchored in any drain set.** When
     `_bg_tasks` are cancelled in `Daemon.stop` (`main.py:1011`), the
     @tool body's `await proc.wait()` raises `CancelledError` — the
     pandoc child gets SIGTERM via the asyncio cancellation only if
     `asyncio.create_subprocess_exec` was *invoked* with
     proper kill_on_cancel handling. v0 says nothing about this.
     **Real impact:** a pandoc PID survives daemon stop and continues
     writing to a staging file under `<artefact_dir>/.staging/`,
     which then escapes `_cleanup_stale_artefacts` because the spec
     says "удаляет всё под `<data_dir>/artefacts/` с mtime >
     `cleanup_threshold_s`" — wrong if pandoc just touched it.
  2. **WeasyPrint in `asyncio.to_thread` doesn't respond to
     cancellation at all.** `asyncio.to_thread` returns a future that
     can be cancelled, but the underlying thread keeps running until
     `write_pdf()` completes. The daemon then exits with a thread
     still holding cairo/pango RAM, which on shutdown might segfault
     in libpango cleanup paths.
  3. **No `vault_sync_pending`-equivalent drain set for renders.**
     Phase 8 has `_vault_sync_pending` drained BEFORE `_bg_tasks`
     cancel (`main.py:967-1005`). v0 §3 C7 doesn't propose an
     analogous `_render_doc_pending` set.

- **Evidence:**
  - Spec §2.1 step 5 vs §3 C7 — silence on pandoc subprocess
    lifecycle during shutdown.
  - `main.py:1011` cancels all `_bg_tasks` after vault drain — render
    @tool body running inside SDK query is *NOT* anchored in
    `_bg_tasks`; it lives inside the SDK's MCP server loop, so this
    cancel doesn't even reach it cleanly.
  - WeasyPrint thread cancel: well-known asyncio pitfall.

- **Proposed v1 fix:** New §2.12 "Render lifecycle vs Daemon.stop".
  Mandate three things:
  - (i) Add `_render_doc_pending: set[asyncio.Task]` on Daemon,
    populated from `RenderDocSubsystem.render(...)` callsite. Drain
    on stop AFTER vault_sync drain and BEFORE `_bg_tasks` cancel,
    with budget `render_drain_timeout_s` (default = max(pdf_pandoc +
    pdf_weasyprint timeouts, 30s) = 50s).
  - (ii) Spec wording for pandoc cancellation: "On `CancelledError`
    in @tool body, send SIGTERM via `proc.terminate()`, await
    `proc.wait()` with 5s budget, then `proc.kill()` if still alive.
    Best-effort cleanup of `.staging/<uuid>.{md,html}` in finally
    block."
  - (iii) WeasyPrint thread "uncancellable" honesty: spec wording
    "WeasyPrint render runs in `asyncio.to_thread` and is NOT
    cancellable mid-flight; drain budget must accommodate worst-case
    pdf_weasyprint_timeout_s=30s on top of pandoc timeout."
  Add AC#21: "Daemon.stop while render in-flight: pandoc subprocess
  receives SIGTERM, staging files cleaned, no orphan PID survives."
  Add tests:
  `test_phase9_daemon_stop_terminates_pandoc.py`,
  `test_phase9_daemon_stop_drains_render_pending.py`,
  `test_phase9_render_staging_cleanup_on_cancel.py`.

#### W1-CRIT-5 — Filename sanitization misses Windows-reserved names + Unicode bidi/control chars + RTL spoofing

- **What's wrong:** §2.4 step 4 + AC#7 reject `..`, `/`, `\\`, `\0`,
  leading-dot, length>96. **Missing**:
  1. **Windows reserved basenames**: `CON`, `PRN`, `AUX`, `NUL`,
     `COM1..COM9`, `LPT1..LPT9` (case-insensitive). Owner downloads
     PDF from Telegram on Windows → opening `CON.pdf` fails or hangs.
     Also `CON.pdf.pdf` is technically OK but Windows historically
     mishandles even with extension.
  2. **Trailing space/dot on basename**: Windows strips them silently;
     two files `report.pdf` and `report .pdf` collide on Windows.
  3. **Unicode control chars in BMP**: zero-width space U+200B,
     zero-width joiner U+200D — invisible in Telegram filename, can
     be used to spoof another filename.
  4. **Bidi override**: U+202E (RIGHT-TO-LEFT OVERRIDE), U+2066
     (FIRST STRONG ISOLATE) — classic RTL filename-spoof attack.
     `report\u202Efdp.exe` displays as `reportexe.pdf` in Telegram
     UI. Phase 9 only emits PDF/DOCX/XLSX so attacker payload is
     limited, but the **suggested_filename appears in Telegram chat
     history forever** — owner's caption history shows spoofed name.
  5. **Surrogate halves**: lone UTF-16 surrogate codepoints in BMP
     (U+D800–U+DFFF) — Python str allows them; some filesystems
     reject them. Owner's Mac/Linux disk → fine; Telegram client on
     iOS may fail to render.
  6. **NULL within UTF-8 multi-byte sequence**: spec rejects ASCII
     `\0` but not e.g. an ill-formed UTF-8 byte stream — non-issue
     because Python str is unicode, but spec wording "rejects `\0`"
     implies byte-level which is a confused mental model.
  7. **Cyrillic/emoji "discussion" in Q4** says "разрешить любой
     printable Unicode" but never defines "printable" — Python has
     no `unicodedata.category()` recipe in spec. AC#7 just says
     "Cyrillic + spaces + unicode dashes accepted" without a rule.

- **Evidence:**
  - Spec §2.4 step 4 — 6 reject conditions, 0 of which cover the
    above.
  - Spec Q4 leaves "printable Unicode" undefined.

- **Proposed v1 fix:** §2.4 step 4 rewrite with explicit rule:
  > "Sanitize: strip `unicodedata.category(c) in {'Cc','Cf','Co',
  > 'Cs','Cn'}` (control / format / private-use / surrogate /
  > unassigned). Reject after strip if: contains any of `/\\\\\\0`,
  > starts with `.` or whitespace, ends with `.` or whitespace
  > or contains trailing dot/space sequence, casefold() basename
  > matches `^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$`, length > 96
  > codepoints, or empty after strip."
  Add `_sanitize_filename` table-driven test parameters:
  - `"CON"` → reject
  - `"con.report"` → accept (matches as part, not basename)
  - `"a\u202Eb"` → strip U+202E, accept "ab"
  - `"a\u200Bb"` → strip ZWSP, accept "ab"
  - `"report ."` → reject trailing dot/space
  - `"\u0000a"` → reject (existing)
  - emoji surfaces: `"📊отчёт"` → accept (emojis are Cf-or-So,
    decide explicitly which). Spec must declare emoji policy in §2.4
    not in Q4.
  Add AC#22: "RTL-spoof filename rejected; Windows-reserved basename
  rejected; ZWSP stripped silently."

### HIGH

#### W1-HIGH-1 — Subprocess `env=` paragraph (§2.11) is incomplete vs phase-8 H3 precedent

- **What's wrong:** §2.11 says "pandoc нужен только PATH" and
  "никогда не мутировать `os.environ`". Doesn't specify *exactly*
  what `env=` is: full `os.environ.copy()`? Just `{"PATH": ...}`?
  Phase 8 used `{**os.environ, "GIT_SSH_COMMAND": ...}` (full env +
  override), see `description.md:248`. If phase 9 mirrors that, then
  `TELEGRAM_BOT_TOKEN`, `GH_TOKEN`, every secret in process env
  leaks into pandoc child process env table. Pandoc itself doesn't
  read these, but a malicious filter (`pandoc --filter ./evil.sh`,
  blocked by pandoc invocation but theoretically) or pandoc itself
  spawning subprocesses (lua filters, future binary) inherit them.
  Lower risk than CRIT, but the precedent (phase 8 H3 was a CRITICAL
  closure) means we should be explicit.

- **Evidence:**
  - Spec §2.11 last sentence: "phase 9 не добавляет process-wide
    env — pandoc нужен только PATH" — but doesn't say env is reduced
    TO {PATH}, just doesn't ADD.
  - Phase 8 §2.3 line 248: `env = {**os.environ, "GIT_SSH_COMMAND":
    GIT_SSH_COMMAND}`.

- **Proposed v1 fix:** §2.11 add explicit recipe:
  > "Pandoc subprocess env: `env={'PATH': os.environ.get('PATH', ''),
  > 'LANG': os.environ.get('LANG', 'C.UTF-8'), 'HOME':
  > os.environ.get('HOME', '/tmp')}`. Whitelist-only — никаких
  > Telegram tokens / GH tokens / Anthropic OAuth путей. Это
  > strictly tighter чем phase-8 vault_sync (которому нужно SSH
  > infrastructure)."
  Add AC#23 + test `test_phase9_pandoc_env_minimal.py` asserting
  pandoc subprocess env has no TELEGRAM_BOT_TOKEN / GH_TOKEN /
  CLAUDE_* / ANTHROPIC_*.

#### W1-HIGH-2 — Force-disable Telegram notify ordering invariant unspecified

- **What's wrong:** §2.2 says "log line `event=render_doc_force_
  disabled` + Telegram notify on boot if `force_disabled`". But
  `Daemon.start` sequence (per `main.py:594-642` for vault_sync
  precedent) constructs adapter BEFORE vault_sync subsystem; for
  render_doc, spec §3 Wave C C6 says "AFTER vault_sync wiring".
  Adapter exists at this point, so notify path *should* work.
  However:
  1. Adapter `start()` (polling task spawn) hasn't been called yet
     at this insertion point (vault_sync force-disable notify in
     phase 8 hits the same issue and uses `adapter.send_text` which
     doesn't require polling — see `vault_sync/notify.py`).
  2. `adapter.send_text` requires `self._bot` which exists from
     `__init__`, OK.
  3. But the sequencing comment is missing from the spec.

- **Evidence:**
  - Spec §2.2 force-disable wording.
  - `main.py:594-595` "AFTER subagent picker spawn" + "BEFORE
    scheduler boot" — phase 8 ordering pattern.
  - Spec §3 C6 "AFTER vault_sync wiring" — 1-line, no detail.

- **Proposed v1 fix:** Phase 8 gives a concrete model. v1 §3 C6
  rewrite to:
  > "Insertion: AFTER vault_sync block (so phase-8 boot-order
  > invariant is preserved) and BEFORE scheduler block (catchup-recap
  > notify must not race the render_doc force-disable notify; both
  > use `adapter.send_text`). Use `adapter.send_text` directly
  > (`adapter._bot` exists post-`__init__`, no polling required for
  > outbound). Notify wraps in `asyncio.wait_for(timeout=10s)` per
  > phase-8 F9 precedent."
  Add AC#24: "force-disable notify lands in Telegram even if adapter
  polling task hasn't started yet (boot-time send via `_bot`)."

#### W1-HIGH-3 — `CRIT-3 fix` interacts with multi-render-per-turn invariant; spec says nothing about ordering or atomicity

- **What's wrong:** §2.5 C-flow step 4 says "для каждого артефакта в
  порядке появления". But model can call render_doc N times in one
  turn (PDF + DOCX same content, OR sequential reports). Bridge
  yields `ArtefactBlock` after each `ToolResultBlock`; handler
  collects in list. ALL deliveries happen AFTER text-flush. Two
  problems:
  1. **Caption v1 = None**: spec §2.5 step 5 says "модель пусть сама
     пишет описание текстом ДО артефакта". For ONE artefact this
     works. For TWO artefacts in a row, owner sees: text → PDF →
     DOCX, with no caption distinguishing them. UX issue.
  2. **Partial failure semantics**: send_document fails for artefact
     #1 (file too large, Telegram down). Spec §2.5 Wave C C4 says
     "Retry policy: send_document failure логируем + продолжаем
     (artefact умрёт через TTL)". But "продолжаем" means send #2
     anyway — so owner gets text + PDF (silently failed) + DOCX.
     They'll think the PDF wasn't generated.

- **Evidence:**
  - Spec §2.5 C-flow step 4-5.
  - Spec §3 C4: "send_document failure логируем + продолжаем".

- **Proposed v1 fix:** §2.5 add explicit fallback:
  > "On `send_document` failure for artefact N (any reason), emit
  > `await adapter.send_text(chat_id, f'(не удалось доставить
  > {suggested_filename}: {short_reason})')` BEFORE moving to N+1.
  > This keeps artefact-delivery ordering self-explanatory in the
  > owner's chat log."
  Plus optional v1 nice-to-have: caption from suggested_filename
  ("file: <name>.pdf") for multi-artefact turns. Add AC#25: "2
  artefacts; first send fails; owner sees text + failure-line +
  second artefact." Add test
  `test_phase9_handler_partial_send_failure.py`.

#### W1-HIGH-4 — XLSX renderer: openpyxl `wb.save()` peak RSS, no concurrency budget

- **What's wrong:** §2.8 + §2.9 — `xlsx_max_rows=10000`,
  `xlsx_max_cols=50`. 10K × 50 = 500K cells. openpyxl in-memory
  workbook ~250–500 MB peak RSS for a workbook this size. With
  `render_max_concurrent=2`, 2 parallel xlsx renders peak ~1 GB.
  VPS is 1 GB RAM (per `reference_vps_deployment.md`). One xlsx
  render + one PDF render = OOM-killer territory. Spec §Risk HIGH
  row "WeasyPrint OOM" mentions adding `render_doc_inflight=N` to
  RSS observer but this is monitoring, not prevention.
  
  Also openpyxl default mode loads entire workbook in memory; the
  alternative `write_only=True` mode streams rows but spec doesn't
  specify which mode is used. v1 must pick.

- **Evidence:**
  - Spec §2.8 caps.
  - Spec §2.9 `render_max_concurrent: int = 2`.
  - Spec §Risk HIGH "WeasyPrint OOM" — addresses PDF, not XLSX.

- **Proposed v1 fix:** §2.8 add: "Use `openpyxl.Workbook(write_only
  =True)` for xlsx pipeline (streaming rows; ~10× lower peak RSS).
  Caps reduce from `xlsx_max_rows=10000` to `xlsx_max_rows=5000`
  unless write_only proves stable on VPS in spike."
  §2.9 add validator: "If `render_max_concurrent > 1`, then
  combined worst-case peak RSS estimate (PDF=400 MB + XLSX=300 MB
  per slot) must be ≤ available RAM minus 200 MB daemon baseline.
  Otherwise validator emits warning (not error) at startup."
  AC#11 expand: "with `render_max_concurrent=2`, 2 parallel
  10K-row XLSX renders complete without OOM-killer." Add
  `test_phase9_xlsx_write_only_mode.py`.

#### W1-HIGH-5 — Absent: render_doc Settings cannot disable per-format. Owner kills xlsx → entire subsystem off.

- **What's wrong:** §2.9 has `enabled: bool` (whole subsystem) but
  no `pdf_enabled / docx_enabled / xlsx_enabled` per-format toggles.
  Yet pandoc binary failure (apt update breaks pandoc) should not
  kill xlsx (pure-python openpyxl, no pandoc). Spec §2.6 says PDF
  needs pandoc; §2.7 DOCX needs pandoc; §2.8 XLSX is pandoc-free.
  If pandoc smoke-test fails in `startup_check`, force-disable kills
  ALL three formats — including xlsx which doesn't need pandoc.

- **Evidence:**
  - Spec §2.2 `startup_check`: "shutil.which('pandoc') MUST resolve;
    `import weasyprint` MUST succeed; иначе subsystem `force_disabled
    =True`".
  - Spec §2.8 first sentence: "XLSX renderer НЕ зависит от pandoc".

- **Proposed v1 fix:** §2.2 + §2.9 split force-disable into per-
  format flags:
  > `force_disabled_formats: set[str]` populated by startup_check —
  > if pandoc missing → add {pdf, docx}; if weasyprint import fails →
  > add {pdf}. xlsx-only mode supported.
  @tool body checks `format in force_disabled_formats` → returns
  `reason="disabled", error="format <X> unavailable: <reason>"`.
  AC#5 expand: "pandoc missing but openpyxl OK → xlsx renders
  succeed, pdf/docx return `reason=disabled`."
  Add tests:
  `test_phase9_partial_force_disable_xlsx_works.py`,
  `test_phase9_partial_force_disable_pdf_blocked.py`.

#### W1-HIGH-6 — `Yandex stub NotImplementedError` AC#17 is untestable as written

- **What's wrong:** §2.5 + AC#17: "Yandex stub'ит
  NotImplementedError и handler ловит его + посылает text fallback."
  But:
  1. There is no Yandex adapter in repo today (verified via
     `adapters/` dir only has `base.py`, `media_group.py`,
     `telegram.py`).
  2. ABC extension forces ANY future adapter to implement abstract
     `send_document` — `NotImplementedError` is what abstract methods
     raise by default. If owner adds Yandex stub WITHOUT
     `send_document`, Python rejects instantiation (`TypeError: Can't
     instantiate abstract class Yandex with abstract method
     send_document`). So "stub'ит NotImplementedError" requires an
     **explicit** `def send_document(...): raise
     NotImplementedError(...)`. Spec doesn't enforce this pattern.
  3. AC#17 is impossible to test without a Yandex adapter.

- **Evidence:**
  - `adapters/` directory: 3 files, no Yandex.
  - Spec §2.5 last paragraph + AC#17.

- **Proposed v1 fix:** Drop AC#17 entirely OR replace with:
  > "AC#17 — handler resilience to adapter NotImplementedError. Test
  > monkeypatches `TelegramAdapter.send_document` to raise
  > `NotImplementedError`; handler logs structured event +
  > `adapter.send_text(chat_id, '(не могу прислать файл — отправь по
  > запросу)')` instead of crashing. No Yandex implementation
  > required."
  Add `test_phase9_handler_send_document_not_implemented.py`. This
  also covers a real future scenario: someone disables `send_
  document` for testing or Telegram bot API breaks the endpoint.

### MEDIUM

#### W1-MED-1 — Audit single-row > rotation budget infinite-loop risk

- **What's wrong:** Spec §2.2 + §3 D1 — audit JSONL rotates at 10 MB
  → `.1` (or date-stamped if D1 chosen). What if a single audit row
  is >10 MB? Possible: error stack `pandoc: command timeout: <very
  long stderr>`, or filename = 96 codepoints × 4 bytes UTF-8 = 384
  bytes (small) but `error` field unbounded. After rotation `.1`,
  next write again exceeds → instant rotation again → log file
  permanently 1-line. Phase-8 audit has same issue.

- **Evidence:**
  - Spec §2.2 audit schema: `error: str | null` no length cap.
  - `vault_sync/audit.py:39-66` writes raw row, no per-line cap.

- **Proposed v1 fix:** §2.2 audit schema add: "`error` field
  truncated to 512 chars in audit row; full error stays in
  structured-log only." Apply same to phase-8 vault_sync/audit.py
  if D1 carry-over chosen. Add `test_audit_single_row_size_cap.py`.

#### W1-MED-2 — `format_invalid` reason is dead code

- **What's wrong:** §2.3 ok=False enum lists `"format_invalid"`. SDK
  enforces `format` enum at JSON-schema validation level (input
  schema `"enum": ["pdf","docx","xlsx"]`); SDK rejects pre-body. So
  `format_invalid` reason from @tool body is unreachable. Either
  drop or document as defensive.

- **Evidence:**
  - Spec §2.3 reasons enum.
  - Spec §2.4 input schema enum.

- **Proposed v1 fix:** Drop `format_invalid` from §2.3 enum; OR
  keep + add wording: "`format_invalid` is a defensive impossible-
  state branch; SDK enum validation should reject before @tool body
  is invoked. Reaching it indicates SDK contract drift."
  Recommend dropping for simplicity.

#### W1-MED-3 — `render_failed` is too coarse for model retry

- **What's wrong:** §2.3 ok=False enum — `render_failed` lumps:
  pandoc syntax error, WeasyPrint css parse fail, openpyxl write
  error, post-render size cap exceeded, output file disappeared.
  Model gets `{ok:false, reason:"render_failed", error:"<short
  msg>"}` and "пересказывает ошибку" (§2.3 last paragraph). But
  model has no actionable signal — should it retry differently?
  Truncate input? Try DOCX instead of PDF? Spec §Phase 8 invariant
  for `vault_push_now` returns `result="failed", error=str(exc)`
  similarly coarse, but vault_push has owner-driven retry; render
  doesn't.

- **Evidence:**
  - Spec §2.3 reason list.
  - `vault_push_now` precedent — opaque `error` field.

- **Proposed v1 fix:** §2.3 enum expand:
  - `render_failed_input_syntax` (pandoc parse error, openpyxl
    table parse error)
  - `render_failed_output_cap` (post-render size > cap)
  - `render_failed_internal` (catch-all for unexpected)
  Plus `error` field becomes machine-parseable: short kebab-case
  string (e.g. `"pandoc-exit-1"`, `"weasyprint-cairo-error"`,
  `"openpyxl-too-many-rows"`). Model can branch on these in retry
  flow. AC#26: "render_failed_input_syntax → model can retry with
  simpler markdown."

#### W1-MED-4 — `_cleanup_stale_artefacts` mentions only artefact_dir, not staging dir

- **What's wrong:** §2.2 boot.py: "удаляет всё под `<data_dir>/
  artefacts/` с mtime > `cleanup_threshold_s` (default 86400)".
  But §2.6 step 1 mentions `<artefact_dir>/.staging/<uuid>.md` for
  pandoc input. If daemon SIGKILL during pandoc invocation, .staging/
  has orphan files. Spec doesn't say boot cleanup walks .staging/.
  Same pattern as phase-8 `_cleanup_stale_vault_locks` — stale lock
  cleanup is well-formalized; stale staging cleanup is silent.

- **Evidence:**
  - Spec §2.2 `_cleanup_stale_artefacts` description.
  - Spec §2.6 step 1 mentions staging path.

- **Proposed v1 fix:** §2.2 boot.py wording rewrite:
  > "_cleanup_stale_artefacts(artefact_dir): walks `artefact_dir/`
  > AND `artefact_dir/.staging/`. Top-level final artefacts: delete
  > if mtime > cleanup_threshold_s (default 86400). Staging files:
  > delete UNCONDITIONAL — by definition orphans (no daemon process
  > would have left them under .staging/ during a healthy run)."
  Add AC#10 expansion: "boot cleanup wipes .staging/ unconditionally."
  Add `test_phase9_cleanup_staging_unconditional.py`.

#### W1-MED-5 — Per-`tool_use_id` ledger keying assumes uniqueness; SDK doesn't strictly guarantee that across iterations

- **What's wrong:** §2.3 says bridge stores in "ledger keyed по
  `tool_use_id`". SDK assigns tool_use_ids per call; Anthropic API
  guarantees uniqueness within a single message but spec relies on
  it across the entire `bridge.ask` lifetime (multi-iteration). 99%
  fine; but spec must call out the assumption.

- **Evidence:** Spec §2.3.

- **Proposed v1 fix:** §2.3 add note: "tool_use_id is unique within
  the SDK conversation as enforced by Anthropic API. Bridge ledger
  uses dict keyed by tool_use_id; collision asserts via duplicate-key
  log + last-write-wins (defensive). AC#27."

#### W1-MED-6 — Schema-version field absent from envelope; bridge JSON-parse will silently break on SDK ToolResult format change

- **What's wrong:** §2.3 envelope = bare dict. §2.5 Option A: bridge
  parses `content[0].text` as JSON. If MCP/SDK ever wraps tool_result
  content (e.g. content[0].text becomes nested under `{"data": ...}`,
  or content array changes shape), bridge silently fails to detect
  artefact → file stays in artefact_dir → owner never receives. No
  schema_version bumps.

- **Evidence:** Spec §2.3 envelope shape.

- **Proposed v1 fix:** Add `schema_version: int = 1` field to
  envelope. Bridge ledger code asserts `schema_version == 1` and
  logs structured event on mismatch. v1 spec wording: "Envelope is
  versioned. Future format changes bump schema_version; bridge logs
  warning + skips artefact handling on unknown version (graceful
  degradation: model still gets text result, owner doesn't see file
  but knows render happened)."

#### W1-MED-7 — Image size budget verification mechanism not concrete

- **What's wrong:** §Risk CRITICAL row 2: "Hard budget: image size
  delta ≤ 100 MB после Wave A. CI step `docker images --format`
  фиксирует размер; PR template содержит «before/after image size
  MB»". But:
  1. CI step doesn't exist yet — added in Wave A? Spec doesn't
     propose specific GH Actions YAML.
  2. PR template change isn't a Wave task either.
  3. apt-cache size estimates (§6: "pandoc ~85 MB, libcairo2 ~1 MB,
     ...") sum to ~95 MB for installed PACKAGES; actual Docker layer
     cost is install size + apt cache + dpkg metadata, often 1.5–2×.
     No source for the number.

- **Evidence:**
  - Spec §6 Dependencies size estimates.
  - Spec §Risk row 2.

- **Proposed v1 fix:** Wave A add concrete A8 task:
  > "A8 — `.github/workflows/docker-image-size-check.yml`. After
  > Docker build, compare `docker images --format '{{.Size}}'` of
  > `ghcr.io/.../0xone-assistant:<TAG>` vs `:main`. Fail PR if
  > delta > 120 MB. Reference value documented in PR description."
  Spike (researcher pass): build runtime image with new apt
  packages, record exact MB delta, update §6 with measured number.
  AC#15a: "image size delta ≤ 120 MB CI green; raising apt list adds
  > 30 MB → CI red, PR blocked."

### LOW

#### W1-LOW-1 — Default caption=None inconsistent with phase-6a/6b paradigm

- **What's wrong:** §2.5 step 5: "Caption = None v1; модель пусть
  сама пишет описание текстом ДО артефакта". But phase-6b inbound
  captions default to `"что на фото?"` (`telegram.py:93`). v0 spec
  is consistent on outbound (no caption forced) but inconsistent in
  spirit with the inbound default-caption UX. Non-issue if owner OK.

- **Proposed v1 fix:** Q10 surfaces this; recommend keep
  `caption=None` for v1, doc explicitly: "Outbound caption=None
  intentional — model writes preamble. Mirrors phase 6e audio path
  where `emit_direct(text)` precedes any voice-derived attachment."

#### W1-LOW-2 — `Q9 vault_sync/audit.py refactor` inside phase 9 = scope creep

- **What's wrong:** §3 Wave D D1 proposes touching phase-8
  `vault_sync/audit.py` retroactively. Carries phase-8 reviewer
  discussion forward. Pro: single source of truth (general
  `audit_helpers.py`). Con: phase 9 review wave now needs to certify
  a phase-8 file change without phase-8 invariants in scope. Phase
  10 would naturally be the right home (when scope explicitly
  includes vault_sync revisit).

- **Proposed v1 fix:** Move D1 to phase 10, OR shrink scope to
  "render_doc/audit.py only; vault_sync/audit.py keeps single-step
  `.1`. Helper extracted later if a third subsystem needs audit
  rotation." This keeps phase 9 review surface small. Q9 v1 answer:
  NO, don't bundle.

#### W1-LOW-3 — Wave D D2 (host-key drift CI) does NOT belong in phase 9 at all

- **What's wrong:** §3 D2: "Add `.github/workflows/host-key-drift
  .yml` — еженедельный cron job, `gh api meta | jq -r '.ssh_keys
  []'` ...". This is purely a phase-8 follow-up; it has no
  relationship to render_doc / pandoc / WeasyPrint / outbound
  document path. Spec §3 D2 itself acknowledges "не часть phase-9
  main flow, но логически в Wave D потому что Dockerfile / CI
  трогаются в одном passe" — that's reaching.

- **Proposed v1 fix:** Move D2 entirely to phase 10 backlog. Phase
  9 D-wave drops to "audit log helper change + Dockerfile static
  test" only.

#### W1-LOW-4 — `pdf_max_bytes=25 MB` exceeds Telegram cap; redundancy

- **What's wrong:** §2.9 `pdf_max_bytes=25*1024*1024` (25 MiB).
  Telegram bot API caps `send_document` at 20 MiB. So a 22-MiB PDF
  passes pdf_max_bytes but gets rejected by adapter (AC#13 path).
  v0 already plans for this case — but the configured PDF cap above
  Telegram cap means owner gets "render OK but undeliverable"
  silently (until TTL).

- **Proposed v1 fix:** §2.9 set `pdf_max_bytes=20*1024*1024`,
  symmetric with Telegram cap. Same for docx/xlsx caps if they were
  >20 (they aren't; xlsx_max_bytes=docx_max_bytes=10 MiB). v1
  validator: `pdf_max_bytes <= TELEGRAM_DOC_MAX_BYTES`. AC#13
  rewording.

#### W1-LOW-5 — `Wave A A2` spec format inconsistent with `pyproject.toml`

- **What's wrong:** §3 A2 says "добавить `weasyprint>=63,<70`
  (CFFI-backed, требует cairo/pango). `openpyxl` уже есть. Pandoc —
  system binary, не Python dep." Just minor format note: real
  `pyproject.toml` uses PEP 621 with `dependencies = [...]`. Spec
  should say where it lives (`[project] dependencies` not `[tool
  .poetry]`).

- **Proposed v1 fix:** §3 A2 add: "Add to `[project] dependencies`
  array in pyproject.toml; rebuild lockfile (`uv pip compile`). v1
  pin: `weasyprint>=63,<70` confirmed available on PyPI for Python
  3.12. CFFI requires cairo/pango bindings provided by Dockerfile
  apt list (A1)." Trivial wording.

#### W1-LOW-6 — Mockup test count vs actual surface delta is light

- **What's wrong:** Wave A 8 tests, B 12, C 8, D 3 = 31 tests for
  ~2500 LOC + new bridge surface + new adapter surface + new
  subsystem. Phase 8 shipped 1014 tests; phase 4 shipped 387.
  Phase 9 surface is comparable to phase 6a (603 tests). Phase 9
  will need ~50–80 tests if devil-w1 closures land (∼20 new tests
  added by closures above).

- **Proposed v1 fix:** Spec §3 final test count budget rewrite:
  "After devil-w1 closures, expected test count ~55–70." This
  aligns reviewer expectations.

#### W1-LOW-7 — `Q4 Cyrillic + emoji policy` answered loosely

- **What's wrong:** §Q4 says "разрешить любой printable Unicode
  после strip control chars; cap 96 codepoints". "printable" is
  not a Python concept; closest is `str.isprintable()` which
  excludes Cf/Cc/Cs/Co/Cn — same surface CRIT-5 calls out. Q4
  should reference the canonical CRIT-5 rule.

- **Proposed v1 fix:** Q4 v1 answer: "See §2.4 sanitization rule
  (CRIT-5 closure). Emoji (Unicode So) accepted. Cyrillic accepted.
  ZWSP/U+202E stripped. Trailing space/dot rejected." 1-liner.

## Alternatives I considered and REJECTED

- **Concurrency cap = 1 instead of 2.** Considered for Q8. Rejected:
  serialised renders means owner waiting 30+ s for second PDF when
  first is mid-WeasyPrint. Phase 6e audio uses Sem(1) but voice is
  inherently serial; render_doc isn't.
- **Drop XLSX format.** Considered: openpyxl is the only non-pandoc
  format and adds peak-RSS pressure. Rejected: owner-frozen scope
  explicitly includes XLSX. Devil HIGH-4 mitigation (write_only mode)
  is enough.
- **Drop the bridge ArtefactBlock yield in favour of handler-side
  ToolResultBlock parse.** Considered: simpler. Rejected: Option B
  in spec §2.5 is correctly identified as "lezет в SDK detail и
  нарушает phase-2 layering". Bridge owns SDK message types; handler
  shouldn't.
- **Force `caption=suggested_filename` for ALL outbound documents.**
  Considered: defends against multi-artefact ambiguity (HIGH-3).
  Rejected: model already writes preamble text; double-tagging is
  noise.
- **Use `aiogram.types.BufferedInputFile` (in-memory) instead of
  `FSInputFile`.** Considered: avoids file-on-disk + TTL race
  entirely (CRIT-3). Rejected: 25 MB PDF in memory = 25 MB extra
  RSS; with concurrency=2 that's 50 MB. CRIT-3 fix (b) lets us keep
  FSInputFile + add in-flight bookkeeping.
- **Include phase-10 follow-up "render to vault" as feature flag
  in v1.** Rejected: §5 #10 lists it explicitly out-of-scope. Don't
  re-litigate.
- **Add a NEW MCP server for "list artefacts" / "get artefact by
  hash" (model can introspect what's been delivered).** Rejected:
  §5 #11 lists "Render history retrieval" out of scope. v1 keeps
  artefact ephemeral.

## What v1 looks like

Top-3 spec changes for v1:

1. **§2.5 (bridge artefact ledger contract) rewritten** with explicit
   per-iteration flush barrier (CRIT-1), schema_version field
   (MED-6), and partial-failure inline-text behaviour (HIGH-3). New
   ACs #19, #25, #27.
2. **§2.6 (PDF renderer) hardened** with full fetch-surface
   enumeration (CRIT-2): mandate `url_fetcher` blocks every URL not
   in scheme allow-list `{data: with image mime}`, pandoc invocation
   uses `--from=markdown-raw_html-raw_tex-raw_attribute`, AC#14
   expanded to 9 sub-cases (img / @import / @font-face / `<base>` /
   SVG xlink / CSS url() ×4). Researcher pass MUST verify pandoc
   strips raw HTML AND raw attributes.
3. **§2.12 (NEW) "Render lifecycle vs Daemon.stop" + §2.4
   (sanitization) rewritten** + per-format force-disable §2.2/§2.9
   (HIGH-5). CRIT-3 in-flight ledger (subsystem-owned set), CRIT-4
   subprocess SIGTERM + drain set + WeasyPrint-uncancellable note,
   CRIT-5 explicit unicodedata rule + Windows-reserved + bidi/ZWSP.

Wave-D D1 (vault_sync audit refactor) and D2 (host-key drift CI)
moved to phase 10 backlog (LOW-2/LOW-3). Wave A gains image-size
CI gate (MED-7). Wave C gains `_render_doc_pending` drain set + 6
new tests (CRIT-3, CRIT-4, HIGH-3). Test count budget revised to
~55–70.

After v1: 4-reviewer wave can converge in one pass instead of two.
The risk of a phase-8-style 16-hotfix train drops sharply because
the most novel surfaces (artefact-block ordering + URL fetch
surface) are now behind explicit invariants and ACs.
