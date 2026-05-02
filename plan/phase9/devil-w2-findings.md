# Phase 9 — Devil's Advocate Wave 2 (Reconsider)

> Stress-test of `description.md` v1 (1767 lines) AFTER w1 closures
> applied. v1 closes 5 CRIT + 6 HIGH + 7 MED + 6 LOW from w1; LOW-3
> deferred to phase 10. New §2.12 (lifecycle) + §2.13 (in-flight ledger)
> introduced.

## Verdict

**Ship-ready as v2 after small revisions** — 1 CRIT + 2 HIGH + 5 MED +
3 LOW. The CRIT is a migration-blocker that will fail >100 existing
tests at first `pytest` run if not addressed; the HIGHs surface
performance / lifecycle holes v1 introduced; the MEDs are spec gaps
that researcher pass should hit. None of the issues require an §-level
rewrite — most are 1-3-paragraph spec deltas.

w1 hit the architecturally-novel surfaces (artefact envelope, fetch
surface, sweeper race, daemon-stop drain, filename sanitization). w2
finds primarily what v1's *closures* missed, plus orthogonal angles w1
didn't pursue (test-fixture migration, cumulative drain budget,
observability hand-waves). Spec is otherwise mature; researcher can
proceed with the v2 deltas folded in.

Estimate: ~150 spec lines delta + 4 new tests + 1 explicit migration
list for the abstract-method extension.

---

## Closures (severity, category, claim, proposed v2 fix)

### CRITICAL  [W2-CRIT-N]

#### W2-CRIT-1 — `MessengerAdapter.send_document` ABC extension breaks 6+ existing test fixtures (no migration list)

- **Category:** Migration / backwards-compat. (w1 didn't catch — w1
  focused on whether AC#17 was *testable*, missed whether existing
  tests *break*.)
- **What's wrong:** §2.5 adds `send_document` as `@abstractmethod` to
  `MessengerAdapter`. Spec confirms every concrete adapter must
  implement (or `TypeError: Can't instantiate abstract class`).
  `tests/` already has **6 distinct test fixtures** subclassing
  `MessengerAdapter`:
  - `tests/test_phase8_edge_trigger_notify.py:28` (`_FakeAdapter`)
  - `tests/test_subagent_hooks.py:22` (`_FakeAdapter`)
  - `tests/test_scheduler_dispatcher_reads_trigger_prompt.py:27`
    (`_CapturingAdapter`)
  - `tests/test_scheduler_dispatcher_lifecycle.py:28` (`_Adapter`)
  - `tests/test_scheduler_dispatcher_empty_output_reverts.py`
  - `tests/test_scheduler_integration_real_oauth.py`
  Each implements only `start/stop/send_text`. The moment Wave C C1
  lands, ALL six fixtures fail at instantiation and every test that
  depends on them goes red. None of these tests have anything to do
  with render_doc — they cover phase 5b/8/6e/3 surfaces. Spec test
  count budget «~62 NEW tests» implicitly assumes existing tests
  remain green; this assumption is false.
- **Evidence:**
  - Spec §2.5 `class MessengerAdapter(ABC): @abstractmethod async def
    send_document(...)`.
  - 6 test files grep'd above all subclass and only override the
    phase-1 ABC trio.
  - Phase-8 added `vault_push_now` @tool **without** extending the ABC
    (the pattern was bridge-only); phase 9 is the first ABC extension
    since phase 1.
- **Proposed v2 fix:** Either:
  - **Option A (recommended)**: provide a default impl in the ABC
    that raises `NotImplementedError`. AC#17 (HIGH-6 closure) already
    expects the adapter-resilience handler to catch this exception, so
    a default-raises-NotImplementedError perfectly matches the
    handler contract. v2 §2.5 wording: «`send_document` is declared on
    `MessengerAdapter` as a non-abstract method with default body
    `raise NotImplementedError("adapter has no document-out path")`.
    This permits existing test fixtures to remain valid without
    forcing every fixture to add a no-op stub.» Trade-off: ABC no
    longer *forces* future Yandex/Discord adapters to implement —
    but Q1 reasoning ("phase 10+ adapters obliged") is preserved by
    convention + a comment, and HIGH-6 closure makes runtime resilient
    anyway.
  - **Option B**: enumerate explicit migration in Wave C C1: list all
    test fixtures that need a stub and require Wave C to add `async
    def send_document(self, *_, **__): pass` to each. Brittle (next
    test added between phases breaks again).
  Add to v2 spec: «§3 Wave C C1 enumerates 6 test fixtures impacted
  by ABC extension; `tests/conftest.py` gets a shared `FakeAdapter`
  base class implementing the new method.» AC list addition: «AC#17b
  — phase 5b/6e/8 test suites green after `send_document`
  extension.»

---

### HIGH  [W2-HIGH-N]

#### W2-HIGH-1 — Cumulative `Daemon.stop` drain budget runs past Telegram TCP idle / `docker stop` `TimeoutStopSec`

- **Category:** Lifecycle / re-bootability. (w1 CRIT-4 looked at
  pandoc-orphan side; w1 didn't sum the budget.)
- **What's wrong:** v1 §2.12 adds 50s `render_drain_timeout_s` AFTER
  `vault_sync.drain_timeout_s` (60s, `config.py:244`) AFTER
  `_adapter.stop()` (typing flush ~30s — phase-6e), with
  `audio_persist.drain_timeout_s` (5s) and `subagent.drain_timeout_s`
  (5s) following. Worst-case sequential drain = 30 + 60 + 50 + 5 + 5
  = **150s**.
  - `docker stop` default `--time=10` (Docker compose default 10s,
    explicit override required in compose.yml).
  - systemd `TimeoutStopSec` default = 90s.
  - When PID 1 doesn't exit within timeout → SIGKILL → drain
    half-completes → orphan pandoc processes (CRIT-4 mitigation
    defeated), partially-written audit rows, and per phase-6e
    incident, lost `conversations.user_row` writes.
- **Evidence:**
  - `src/assistant/main.py:983-1005` — vault_sync drain.
  - `src/assistant/main.py:1011-1014` — `_bg_tasks` cancel.
  - phase-6e shipped 30s typing flush in `_adapter.stop()`.
  - `deploy/docker/docker-compose.yml` — owner / reviewer should
    confirm `stop_grace_period`.
- **Proposed v2 fix:** §2.12 add explicit cumulative budget paragraph:
  «Worst-case total `Daemon.stop()` budget = 30s adapter + 60s vault +
  50s render + 5s audio + 5s subagent = **150s**. Confirm
  `docker-compose.yml` `stop_grace_period: 180s` and systemd unit
  `TimeoutStopSec=180s` (if fallback used). If owner runs default
  Docker `stop` without override, render drain is the first to be
  cut short on SIGKILL; orphan pandoc PIDs accepted as residual risk
  documented in §2.12 (iii) honesty paragraph (extended to cover this
  scenario).»
  Add AC: «AC#21a — `docker-compose.yml stop_grace_period >= 180s`
  static check + Wave A test
  `test_phase9_compose_stop_grace_period.py` greps yaml.»
  Or alternative: shrink `render_drain_timeout_s` to 30s and accept
  that worst-case PDF mid-WeasyPrint cancels uncleanly (drops to
  CRIT-4 (iii) honesty zone).

#### W2-HIGH-2 — `_artefacts` dict mutation without lock on `register_artefact` / `mark_delivered` paths

- **Category:** v1-introduced (§2.13). Concurrency.
- **What's wrong:** §2.13 declares `_artefacts_lock: asyncio.Lock` and
  shows the **sweeper** holding it in `_sweep_loop`. But:
  - `register_artefact(path)` (called from `subsystem.render`) is not
    shown acquiring the lock.
  - `mark_delivered(path)` (called from handler post-`send_document`)
    is not shown acquiring the lock.
  - Both mutate `self._artefacts` (insertion / field flip).
  Single-event-loop dict mutations are not UB, BUT iteration during
  mutation is. The sweeper's `to_delete = [rec for rec in
  self._artefacts.values() if ...]` is INSIDE the lock acquire — so
  if `register_artefact` doesn't acquire, it can `dict.__setitem__`
  while sweeper is comprehending → RuntimeError (changed-during-
  iteration). With `render_max_concurrent=2` and a 60s sweep
  interval, the race window is small but non-zero (every ~60s × ~50ms
  comprehension window).
  Worse: **handler calls `mark_delivered` from `send_document`'s
  finally inside the per-iteration flush loop**, which could fire
  EXACTLY when sweeper is iterating.
- **Evidence:**
  - Spec §2.13 lock declaration line 895.
  - Sweep algorithm shows `async with self._artefacts_lock` (line 919).
  - Lifecycle steps 1-5 (lines 900-909) — no `async with` shown.
- **Proposed v2 fix:** §2.13 explicit «Lock acquisition discipline»
  paragraph:
  «`register_artefact` and `mark_delivered` MUST `async with
  self._artefacts_lock` around the mutation step. `_sweep_loop`
  acquires for its critical section. Sweep `unlink()` calls happen
  OUTSIDE the lock (release before disk-I/O loop) so a slow unlink
  doesn't stall delivery: build `to_delete` under lock, release, then
  unlink+pop in a second pass.»
  Add test `test_phase9_ledger_concurrent_mutations.py`:
  parallel-spawn `register_artefact` x10 + `mark_delivered` x10 +
  one `_sweep_loop` tick — no exceptions raised.

---

### MEDIUM  [W2-MED-N]

#### W2-MED-1 — `tex_math_dollars` extension survives `markdown-raw_html-raw_tex-raw_attribute` variant

- **Category:** v1-introduced (CRIT-2 closure). Pandoc semantics.
- **What's wrong:** §2.6 says `markdown-raw_html-raw_tex-raw_attribute`
  «strips inline LaTeX math (irrelevant to PDF but defensive)». This
  is **wrong**: `raw_tex` only controls raw `\command{...}` literal
  TeX blocks. **Inline math `$E=mc^2$`** is governed by separate
  `tex_math_dollars` extension (enabled by default in Pandoc's
  `markdown` flavour). Subtracting `raw_tex` does NOT disable
  `tex_math_dollars`. Owner asks «put the equation in the report» →
  model emits `$E=mc^2$` → pandoc renders as MathML (HTML5 output
  default) → WeasyPrint renders. So inline math DOES survive — which
  is **good for owner** but the spec wording implies otherwise.
  Researcher needs to verify on real bookworm pandoc 2.17.x and
  document expected behaviour.
- **Evidence:** Pandoc User Manual §Math (publicly known) — extensions
  list. Spec §2.6 step 2 wording.
- **Proposed v2 fix:** §2.6 step 2 reword: «`raw_tex` strips raw
  literal TeX blocks (`\textbf{...}`). Inline math `$x^2$` and display
  `$$\int...$$` are governed by `tex_math_dollars` extension and
  remain enabled — owner-visible MathML output is intentional.»
  Researcher Wave A spike adds explicit verification: «Render
  `content_md='energy: $E=mc^2$.'` → output PDF must contain MathML
  rendering, not literal `$E=mc^2$` glyph string.» AC#14j NEW: «inline
  math survives variant subtraction.»

#### W2-MED-2 — `data:image/*` allowlist contradicts §5 #3 «embedded images non-goal»

- **Category:** v1-introduced inconsistency. CRIT-2 closure ↔ §5 #3.
- **What's wrong:** §2.6 step 4 `safe_url_fetcher` allows `data:`
  URIs with image MIMEs (png/jpeg/gif/svg+xml). §5 #3 says «Embedded
  images в PDF/DOCX. `![](path)` markdown syntax рендерится как
  broken-image placeholder». But model can write `![](data:image/png;
  base64,iVBOR...)` and that **WILL** render — fetcher allows it.
  So embedded images are partially in scope (data: only) while §5 #3
  declares them out of scope. Reviewer reading §5 will conclude image
  embedding doesn't work and not test it; CRIT-2 closure says it does.
  Inconsistency confuses test surface.
- **Evidence:** §2.6 lines 550-575 vs §5 #3 lines 1418-1421.
- **Proposed v2 fix:** §5 #3 reword: «External image references
  (`https://...`, `file://...`, `<img src=>`) — out of scope, blocked
  by `safe_url_fetcher`. **Inline `data:image/*` URIs are
  intentionally permitted** as they avoid network fetch (CRIT-2
  closure §2.6); model can embed small icons / charts. Large images
  count toward `max_input_bytes=1MiB` markdown cap — practical limit
  ~700KB base64 image.» AC#1 expansion: «owner asks for PDF with chart
  → model emits `data:image/png;base64,...` → PDF renders image
  correctly.»

#### W2-MED-3 — Validator does not enforce `render_drain_timeout_s >= pdf_pandoc + pdf_weasyprint`

- **Category:** v1-introduced (§2.9 + §2.12). Settings-validator gap.
- **What's wrong:** §2.9 validator ensures `tool_timeout_s >=
  pdf_pandoc_timeout_s + pdf_weasyprint_timeout_s` but does NOT
  enforce same on `render_drain_timeout_s`. Owner can set
  `RENDER_DOC_RENDER_DRAIN_TIMEOUT_S=10` → `Daemon.stop` cancels
  mid-PDF render → CRIT-4 mitigations defeated. Comment at §2.12 (i)
  says «50s default = pdf_pandoc 20s + pdf_weasyprint 30s» — but
  validator doesn't make this an invariant.
- **Evidence:** §2.9 lines 697-731 (no render_drain check); §2.12 (i)
  line 822-825.
- **Proposed v2 fix:** §2.9 validator add:
  ```python
  if self.render_drain_timeout_s < (
      self.pdf_pandoc_timeout_s + self.pdf_weasyprint_timeout_s
  ):
      raise ValueError(
          "render_drain_timeout_s must >= worst-case PDF pipeline; "
          "Daemon.stop will cancel mid-render otherwise"
      )
  ```
  Update test `test_render_doc_settings_validator.py` (Wave A A7)
  with parametrise on this rejected config.

#### W2-MED-4 — Audit log boundary: single row >10 MB after MED-1 truncation can still occur via filename + format + payload sum

- **Category:** Boundary condition. MED-1 closure narrowed `error`
  field to 512 chars, but other fields remain unbounded.
- **What's wrong:** Spec §2.2 audit schema:
  ```
  {"ts": iso, "format": ..., "result": ..., "filename": str,
   "bytes": int|null, "duration_ms": int, "error": str|null,
   "schema_version": 1}
  ```
  After MED-1: `error` ≤ 512 chars. But `filename` is up to **96
  codepoints × 4 bytes UTF-8 = 384 bytes**, and JSON overhead is
  small. Single row never approaches 10 MB. **However**, if a future
  fix-pack adds `stderr` field to audit (likely — phase-8 has
  `error: str(exc)` precedent), MED-1 closure doesn't propagate. Spec
  doesn't actually specify «no future audit field is uncapped» — it's
  a one-time fix on `error` only.
  Phase-8 vault_sync/audit single-step `.1` rotation sees same risk
  but accepts it.
- **Evidence:** §2.2 audit schema; MED-1 closure scope.
- **Proposed v2 fix:** §2.2 audit writer wording: «All variable-length
  string fields in audit row are pre-truncated to 512 chars. Helper
  `_truncate_str_fields(row, max_chars=512)` applied before JSON
  serialisation. Future audit field additions inherit truncation by
  default.» Test `test_render_doc_audit_single_row_size_cap.py`
  (already in Wave B B5) parametrised on `filename` overflow scenario.

#### W2-MED-5 — `requires_pandoc`-marked integration test acceptance unspecified

- **Category:** Test surface gap. Reminiscent of phase-8 ssh-not-found
  trap.
- **What's wrong:** Wave B B3+B4 mention
  `test_docx_renderer_integration.py` and
  `test_pdf_renderer_integration.py` with `pytest.mark.requires_pandoc`.
  Spec doesn't say what these tests *assert*. Phase-8 lesson: 1014
  mocked tests + 4 reviewer waves missed live-deploy `ssh: command not
  found`. Spec says «container test target extends runtime stage» but
  doesn't mandate that the integration test produces a real PDF/DOCX
  and validates output bytes.
- **Evidence:** Wave B B3+B4 wording lines 1034-1041; AC#15 only
  asserts `shutil.which("pandoc") is not None`.
- **Proposed v2 fix:** §3 Wave B B3+B4 specify:
  «`test_pdf_renderer_integration.py` (requires_pandoc): renders a
  3-paragraph markdown with a 2-row table and Cyrillic text →
  output PDF (a) starts with `b'%PDF-'` magic bytes, (b) is >2KB,
  (c) `pikepdf.open(...)` parses without error. Test runs in
  `--target test` container which extends `runtime`, guaranteeing
  pandoc + cairo + DejaVu installed.»
  Same for DOCX (`zipfile.is_zipfile(...)` + `python-docx` parse).
  Add to AC#1+AC#2: «happy-path AC includes byte-level structure
  assertion in CI, not only owner visual smoke.»
  This is the «phase-8 ssh-not-found anti-regression» applied to the
  output side, not just binary-presence side.

---

### LOW  [W2-LOW-N]

#### W2-LOW-1 — Observability: spec mentions `event=*` log lines but doesn't enumerate

- **What's wrong:** §2.2 mentions `event=render_doc_force_disabled`,
  §2.3 `event=render_doc_envelope_unknown_schema_version`, §2.5
  `event=render_doc_send_document_failed`, §2.13 `event=render_doc_
  sweep_unlink_failed`. No central inventory; no mention whether RSS
  observer integration (§Risk «add `render_doc_inflight=N` field» —
  phase-6e RSS observer) is mandatory or hand-wave. Reviewer can't
  audit logs against a list.
- **Proposed v2 fix:** §2.14 NEW «Structured logging events» — table
  of event names + fields + when emitted. Mandate RSS observer
  field `render_doc_inflight` (= `len(_artefacts)`) for phase-6e
  observability parity. ~20-line addition. AC#29 NEW: «RSS observer
  log lines include `render_doc_inflight` after subsystem boot.»

#### W2-LOW-2 — Performance: pandoc cold-start latency unmeasured for chat UX

- **What's wrong:** Pandoc 2.17.x on Debian bookworm has ~150-300ms
  cold-start (Haskell runtime). Two pandoc invocations per PDF = up to
  600ms before any actual rendering. Owner perception: «render took
  3s for a 1-page doc» — half is JIT cost. Not breaking, but
  WeasyPrint also cold-starts (cffi binding load ~200ms first call,
  cached on second).
- **Proposed v2 fix:** Researcher Wave A spike measures cold-start +
  warm-call latency on real bookworm container; document p50/p99 in
  §6 size estimates. No code change required; owner expectation
  setting only.

#### W2-LOW-3 — `filename = "."` edge case explicit handling missing from CRIT-5 matrix

- **What's wrong:** CRIT-5 matrix at §2.4 enumerates `".."`, `".hidden"`,
  trailing dot, etc. — but NOT bare `"."`. Trace:
  - input `"."` → category-strip → `"."` → `.strip()` → `"."` →
    startswith(".") → reject `dot-prefix-or-traversal`. Behaviour is
    correct, just not in test matrix.
- **Proposed v2 fix:** §2.4 matrix add row: `"."` → reject
  `dot-prefix-or-traversal`. AC#22 test parametrise on this. 1-line
  addition.

#### W2-LOW-4 — Internationalization: Hebrew/Arabic RTL not addressed (out-of-scope but worth note)

- **What's wrong:** §5 #14 says «Russian locale not guaranteed».
  DejaVu Sans covers Latin + Cyrillic + Greek + Hebrew + Arabic
  glyphs, but RTL paragraph direction requires CSS `direction: rtl`
  / `unicode-bidi: bidi-override` which markdown→pandoc doesn't emit.
  If owner pastes Arabic text → glyphs render but visually
  left-to-right (broken). Out of phase 9 scope but worth surfacing
  for future locale phase.
- **Proposed v2 fix:** §5 add #19: «Bidi text direction support (RTL
  paragraphs for Arabic/Hebrew). v1 markdown produces LTR-only
  output; CJK and RTL languages may render glyphs but layout is
  Latin-style. Phase 11+ adds CSS direction injection.» No code or
  test change.

---

## Spot-checks performed

- **§9 closure mapping vs w1 findings (3 random spot-checks):**
  - W1-CRIT-1 (per-iteration flush) → §2.5 closure paragraph + AC#19.
    **Maps 1:1 ✓**.
  - W1-MED-5 (tool_use_id collision) → §2.3 + §2.5 «collision logs
    duplicate-key warning + last-write-wins». **Maps ✓** but the
    paragraph is light — no test for collision behaviour explicitly
    enumerated separate from AC#27.
  - W1-LOW-2 (vault_sync audit refactor scope creep) →
    Closed-modified, dropped from Wave D. §3 D-wave shrunk; §5 #15
    + §6 explicitly preserve phase-8 invariants. **Maps ✓**.
- **`MessengerAdapter` ABC test-fixture inventory** — 6 subclasses
  found in tests/, none of which override `send_document` (because
  it doesn't exist yet). All would break on Wave C C1. **Confirmed
  W2-CRIT-1.**
- **`Daemon.stop` drain budget cumulative arithmetic** — read
  `main.py:960-1104`. Sequence: `adapter.stop()` → vault drain (60s)
  → `_bg_tasks.cancel` → audio_persist drain (5s) → subagent drain
  (5s) → conn.close. Inserting render drain (50s) between vault and
  `_bg_tasks` ⇒ 30 + 60 + 50 + 5 + 5 = 150s sequential best-case
  upper bound. **Confirmed W2-HIGH-1.**
- **`_artefacts_lock` discipline** — §2.13 lifecycle steps lines
  900-909 vs lock-protected sweep at line 919. **Confirmed
  W2-HIGH-2.**
- **Pandoc markdown variant `tex_math_dollars` separation from
  `raw_tex`** — could not run `pandoc --list-extensions=markdown`
  locally (binary not on dev box). Public Pandoc User Manual §Math
  documents these as separate extensions; CRIT-2 closure wording is
  imprecise. **Confirmed W2-MED-1 by spec inspection alone;
  researcher Wave A spike must verify on real bookworm.**
- **Validator coverage on render_drain_timeout** — read §2.9 lines
  697-731. No check on render_drain_timeout. **Confirmed W2-MED-3.**
- **Test target inheritance** — read `deploy/docker/Dockerfile` lines
  115-228. `FROM runtime AS test` confirmed → integration tests will
  see pandoc. So real-PDF assertion in integration test would catch
  output-side regressions; spec just doesn't mandate it. **Confirmed
  W2-MED-5.**
- **Carry-over backlog tracking (phase-8 follow-ups)** — Wave D D2
  (host-key drift) deferred to phase 10 per LOW-3. Phase-8 follow-up
  list (real-subprocess `git_ops` integration tests, periodic
  `startup_check` re-run, bootstrap `git reset`) explicitly listed in
  §3 Wave D defer block lines 1203-1208. **Backlog visibly tracked
  ✓.** Not raising as W2 finding — surfaced for orchestrator phase-10
  planning only.
- **What I COULDN'T verify:**
  - Real pandoc behaviour on `markdown-raw_html-raw_tex-raw_attribute`
    against the 9 fetch-surface payloads — researcher spike-only.
  - Actual bookworm Docker image size delta with new apt list — A8
    spike-only.
  - Whether `asyncio.current_task()` from inside `@tool` body returns
    the SDK MCP server's task or the @tool wrapper's task —
    SDK-internals dependent; spec assumes it returns something
    cancellable from `_render_doc_pending`. Coder phase will discover
    if not.
  - WeasyPrint version range `>=63,<70` actually installs on bookworm
    Python 3.12 with apt-shipped cairo — A2/A3 verify.

---

## What v2 looks like

**Top-3 spec deltas for v2:**

1. **§2.5 ABC extension migration paragraph (CRIT-1)** — change
   `send_document` from `@abstractmethod` to default-raises-
   `NotImplementedError`. Spec wording: «`MessengerAdapter.send_
   document` declared with default impl `raise NotImplementedError`
   to preserve backward-compat with 6 existing test fixtures
   (`tests/test_phase8_edge_trigger_notify.py:28`,
   `tests/test_subagent_hooks.py:22`,
   `tests/test_scheduler_dispatcher_*.py` ×3,
   `tests/test_scheduler_integration_real_oauth.py`). HIGH-6 closure
   handler-resilience pattern catches the exception; future Yandex/
   Discord adapters must override by convention not enforcement.»
   AC#17b NEW. ~10 lines + 1 inventory list.

2. **§2.13 lock-discipline paragraph (HIGH-2)** — explicit «register_
   artefact, mark_delivered, _sweep_loop ALL acquire `_artefacts_
   lock`. Sweep releases lock before disk-I/O loop». Plus §2.9
   validator add for `render_drain_timeout_s` lower-bound (MED-3).
   ~15 lines combined.

3. **§2.12 cumulative drain paragraph (HIGH-1) + §2.6 step 2 reword
   (MED-1) + §5 #3 reword (MED-2) + §3 Wave B integration-test
   acceptance (MED-5)** — collectively ~50 lines. Each is a localised
   clarification; no architectural change.

**Carry-over Wave D justification rechecked:** Wave D shrunk to
render_doc-internal only (date-stamped audit rotation + Dockerfile
static check). Phase-8 follow-up backlog (real-subprocess git_ops
integration, periodic startup_check, bootstrap git reset, host-key
drift) tracked in §3 line 1203-1208 «Defer to phase 10». **Surfaced
in this wave only as an observability gap (W2-LOW-1) — not a
blocking concern for phase 9.** Net: phase 9 leaves phase-8 backlog
unchanged; nothing reopened, nothing new added except W2-LOW-3 (host-
key drift was already there).

**§8 Развилки → DECIDED column accuracy:** Spot-checked Q1, Q3, Q4,
Q7, Q9. All map to corresponding §spec sections + ACs. **Q4** is the
only one with potentially-unclear scope: «filename sanitization
агрессивность» now references §2.4 CRIT-5 rule + matrix table — fine.
**No force-closures detected.**

**Researcher-pass critical paths to verify:**
- Pandoc actually strips raw HTML / SVG / raw-attrs on bookworm 2.17.x
  (spec mandate; CRIT-2 closure depends on this).
- WeasyPrint 63.x on Python 3.12 + apt cairo/pango works on bookworm
  (W2 finding W2-LOW-2 implies measurement).
- Real Docker image size delta vs reference (A8 mandate; MED-7
  closure has unbounded budget until measured).
- Inline math `$E=mc^2$` survives variant subtraction (W2-MED-1).

After v2 deltas land, 4-reviewer wave should converge in 1-2 passes
(was 4 passes in phase 8). Risk of phase-8-style 16-hotfix train is
low because the surfaces w1 + w2 collectively attacked are
comprehensive: artefact lifecycle, fetch surface, sweeper race,
shutdown drain, sanitization, ABC migration, observability.

---

## Verdict summary

**🟡 Reconsider** — mandatory v2 revision (1 CRIT + 2 HIGH) before
researcher pass. Estimated total v1 → v2 spec delta: ~150 lines + 4
new tests. Architecturally sound; v1 closures work; v2 fixes are
localised paragraph edits.
