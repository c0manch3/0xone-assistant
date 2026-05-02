# Phase 9 — QA Review

Reviewer: QA agent
Date: 2026-05-03
Spec: `plan/phase9/description.md` v3 (3301 lines, AC#1..AC#33).
Implementation: working tree (uncommitted), 16 phase-9 test files, 106 passed + 4 skipped on Mac dev (pandoc/weasyprint absent).

## Verdict

**CONDITIONAL GO with 2 CRITICAL + 5 HIGH gaps that must close before live deploy.**

The implementation is structurally sound and most invariants are exercised by tests. However, the AC coverage matrix exposes meaningful gaps in the **security-critical** branches (AC#14a-i markdown-injection sub-cases reduced to a single mocked surface), the **lifecycle-critical** branches (AC#19, AC#21, AC#21a, AC#24 not exercised), and the **CI gate** the spec explicitly mandates (AC#15a image-size workflow absent). The flake `test_save_transcript_concurrent_serialises` is confirmed pre-existing (phase 6c, commit 6830ad9) and unrelated.

The body of the @tool, the subsystem ledger, the audit log, the filename sanitiser, and the bridge envelope parser are all correctly implemented and well-tested. The pandoc subprocess SIGTERM/SIGKILL recipe is correctly wired. The Dockerfile installs pandoc + WeasyPrint runtime libs and verifies them at build time. Settings validators are robust.

Despite high test count, several ACs are tested **only via the @tool body forwarding `reason`/`error` from a mocked subsystem** rather than driving the real renderer through the failing branch. Some ACs are entirely uncovered. Two ACs are unreachable as currently specified vs implemented.

## Findings

### CRITICAL

#### QC-1 — AC#14a-AC#14i: 9 markdown-injection fetch surfaces collapsed to 1 mocked test
**Files:** `tests/test_phase9_render_doc_tool.py:163-182`, `tests/test_phase9_render_doc_binaries.py:87-110`
**Severity:** CRITICAL — security-critical AC. Spec §4 explicitly enumerates 9 sub-cases and §3 Wave B B3+B4 says "parametrise по 9 fetch surfaces из AC#14a–AC#14i".
**Observation:** The only assertion that an `<img src=file:///...>` payload triggers `weasyprint-url-fetch-blocked` is a **mocked** test where `_FakeSubsystem._next_result` is hand-set to `error="weasyprint-url-fetch-blocked"`. The fetcher itself is never invoked end-to-end with adversarial markdown. None of the other 8 surfaces — `<base>`, `<svg xlink>`, CSS `@import`, CSS `background:url()`, CSS `@font-face`, `<iframe>/<object>/<embed>`, `data:` URIs, CSS custom property `var(url())` — appear anywhere in the test suite.
**Impact:** A regression in the WeasyPrint version pin or the `SafeURLFetcher.fetch` body that allows ONE of the 8 untested surfaces to silently fall through (e.g., a future WeasyPrint that parses `data:` URIs before consulting the fetcher) would ship to prod undetected. Defence-in-depth claim of pandoc variant subtraction + fetcher allowlist needs adversarial validation per surface.
**Fix:** Add `tests/test_phase9_url_fetcher_surfaces.py` with `pytest.mark.parametrize` covering 9 distinct adversarial markdown payloads, each rendered through the real PDF pipeline (with `requires_pandoc + requires_weasyprint` markers), each asserting the result envelope returns `error="weasyprint-url-fetch-blocked"` AND the staging files were cleaned. Skip-on-host-missing pattern matches `test_phase9_render_doc_binaries.py`.

#### QC-2 — AC#15a: docker-image-size-check.yml workflow absent
**Files:** `.github/workflows/docker.yml` only; no `docker-image-size-check.yml`
**Severity:** CRITICAL — spec §4 AC#15a requires "CI step `docker-image-size-check.yml` builds runtime image, comparing с `:main`. Delta > 300 MB → CI red, PR blocked".
**Observation:** No image-size-delta gate exists in CI. The runtime image grows by ~164 MB (pandoc) + ~25 MB (libpango stack) + ~11 MB (DejaVu fonts) ≈ 200 MB; without the gate, a future apt-list expansion could push delta past 300 MB without triggering CI failure.
**Impact:** Mass-image-bloat regressions ship without gating. Spec §4 says "PR blocked"; that contract is currently unenforced.
**Fix:** Add `.github/workflows/docker-image-size-check.yml` that builds `runtime` target and compares `docker image inspect <new>` vs the `:main`-tagged image's compressed-size delta; fail when delta > 300 MB. Reference value should be documented post-build in `plan/phase9/baseline-image-size.md`.

### HIGH

#### QH-1 — AC#19 multi-iteration ordering not exercised
**Files:** `tests/test_phase9_handler_artefact_flush.py` covers `_flush_artefacts` only as a single-batch call.
**Severity:** HIGH — CRIT-1 was the marquee phase-9 risk; spec §4 AC#19 says "Test asserts ordering invariant via mocked adapter capture" with explicit pattern `text₁ → render_doc(pdf) → text₂ → render_doc(docx)` across 2 iterations (2 ResultMessage envelopes).
**Observation:** No test plants a multi-iteration scenario where the bridge yields ResultMessage twice. The handler's `if pending_artefacts:` flush on every ResultMessage at `handlers/message.py:803-807` is reachable only by hand-crafting a mock bridge stream. None of the existing tests do this.
**Impact:** A regression in the bridge's "do-NOT-return-on-first-ResultMessage" contract (Fix A from incident S13) would silently re-introduce the doc₁→doc₂→text₁→text₂ ordering bug that CRIT-1 was originally raised to prevent.
**Fix:** Add `tests/test_phase9_multi_iteration_order.py` that constructs a fake `ClaudeBridge.ask` async iterator yielding `text₁, ToolResultBlock(render_doc id=t1), ArtefactBlock(t1), ResultMessage, text₂, ToolResultBlock(render_doc id=t2), ArtefactBlock(t2), ResultMessage` in order; assert the capture-adapter records the `[text, doc, text, doc]` sequence.

#### QH-2 — AC#20 sweeper-vs-mark_delivered race not stress-tested
**Files:** `tests/test_phase9_artefact_ledger.py:84-118` (`test_concurrent_mutations_no_dict_size_error`)
**Severity:** HIGH — CRIT-3 closure depends on `_artefacts_lock` being acquired on EVERY ledger touch.
**Observation:** The existing concurrent test asserts only "no `RuntimeError: dictionary changed size during iteration` raises". It does NOT plant the actual race the AC describes: render produces 2 artefacts, first send_document hits Telegram 429 + retry-after 60s, second artefact is in-flight, sweeper ticks — both records have `in_flight=True` so neither reaped. The test never observes the sweeper concurrent with `mark_delivered`.
**Impact:** A subtle bug — e.g., the sweeper iteration's `snapshot` taken under lock but the disk-unlink phase runs outside lock, allowing a parallel `register_artefact` to recreate the same path between snapshot and unlink — would not be caught.
**Fix:** Add a deterministic stress test using `asyncio.sleep(0)` + `asyncio.create_task` that starts a sweeper iteration on artefact A while a parallel coroutine calls `mark_delivered(B)` and `register_artefact(C)`; assert the post-race ledger contains exactly `{B (delivered), C (in-flight)}` and A is reaped.

#### QH-3 — AC#21: Daemon.stop SIGTERM-to-pandoc not tested end-to-end
**Files:** `tests/test_phase9_subsystem_render.py:102-128` (`test_pending_task_drains_under_lock`)
**Severity:** HIGH — CRIT-4 (ii) closure for orphan PIDs.
**Observation:** The existing drain test uses XLSX format (no subprocess) and only verifies the `_pending_set` discard callback. There is NO test that:
  1. Mocks `asyncio.create_subprocess_exec` to return a never-resolving subprocess.
  2. Calls `Daemon.stop()` (or simulates the cancel propagation).
  3. Asserts `proc.terminate()` was called within drain_timeout, then `proc.kill()` after sigterm_grace.
**Impact:** A regression in `_subprocess.run_pandoc` (e.g., dropping the `try/except (CancelledError, TimeoutError)` block) would leak pandoc PIDs across daemon restarts in production. This is exactly the orphan-PID class of bug the AC was raised to prevent.
**Fix:** Add `tests/test_phase9_pandoc_cancel.py` that mocks `asyncio.create_subprocess_exec` to return a `_HangProc` whose `communicate()` blocks forever; call `run_pandoc` inside `asyncio.wait_for(timeout=2)`; on TimeoutError assert `proc.terminate()` was called + `proc.kill()` was called after `pandoc_sigterm_grace_s`.

#### QH-4 — AC#21a cumulative Daemon.stop budget not tested
**Files:** No matching test file.
**Severity:** HIGH — W2-HIGH-1 closure; spec §4 AC#21a is explicit about a synthetic test with 30s adapter + 60s vault + 20s render and budget assertion ≤ 116s.
**Observation:** No test exercises the cumulative drain ordering across `_adapter.stop` + `_vault_sync_pending` + `_render_doc_pending`. The implementation IS correct (sequential `await asyncio.wait` calls in `main.py:1080-1118`), but a regression that re-orders or parallelises the drains would not be caught.
**Impact:** Live deploy under default compose `stop_grace_period: 35s` exposes the documented residual SIGKILL risk; a regression that silently makes the budget MUCH larger (e.g., adding a 600s drain not contemplated by the spec) would also be undetected.
**Fix:** Add `tests/test_phase9_daemon_stop_budget.py` with monkeypatched adapter.stop / vault loop / render task awaits sleep their declared durations; assert `Daemon.stop` returns within 116s and emits `event=render_doc_stop_budget_exceeded` warning when not_done is non-empty.

#### QH-5 — AC#11 concurrency cap (3-parallel + semaphore-acquire trace) not tested
**Files:** `tests/test_phase9_render_doc_settings.py:84-86` covers only validator rejecting `render_max_concurrent=0`; no run-time test.
**Severity:** HIGH — spec §4 AC#11 explicit: "3 параллельных вызова `render_doc` — третий ждёт пока один из первых двух завершит (verify через mocked semaphore acquire trace)". Plus HIGH-4 expansion: "2 parallel 5K-row XLSX renders complete без OOM-killer."
**Observation:** No test launches 3 parallel `subsystem.render(...)` calls and observes the third blocked on the semaphore.
**Impact:** A regression that drops the `async with self._render_sem` (e.g., refactor to per-format semaphore) would not be caught; OOM risk on 1 GB VPS revives.
**Fix:** Add `tests/test_phase9_concurrency_cap.py` that constructs subsystem with `render_max_concurrent=2`, launches 3 parallel `render(...)` tasks each backed by a slow `_dispatch` mock, asserts via instrumented `_render_sem._value` (or a side-channel trace) that the third blocks until one of the first two completes.

### MEDIUM

#### QM-1 — AC#5 spec/implementation discrepancy — "fully force-disabled on missing binaries" unreachable
**Files:** `src/assistant/render_doc/subsystem.py:201` + `tests/test_phase9_subsystem_force_disable.py:100-127`
**Severity:** MEDIUM — spec contradiction. AC#5 text says "Если pandoc binary отсутствует И import weasyprint падает → fully force_disabled subsystem". But xlsx renderer doesn't depend on pandoc/weasyprint, and `startup_check` never adds xlsx to `force_disabled_formats`. Therefore `force_disabled_formats >= ALL_FORMATS` is impossible via host-binary probes; only `enabled=False` triggers the fully-disabled branch.
**Observation:** Test `test_both_missing_fully_force_disabled` documents the discrepancy: it asserts `sub.force_disabled is False` even when both pandoc + weasyprint are missing.
**Impact:** AC#24 (boot-time notify before polling start) **never fires in production** for "binary missing" scenarios — only for `RENDER_DOC_ENABLED=false`. The owner-visible signal "render_doc force-disabled" is silently lost when, e.g., an apt update breaks pandoc on an upgraded VPS.
**Fix:** Either (a) update spec AC#5 to clarify "fully" means "all 3 formats blocked, which requires explicit enabled=False since xlsx is binary-free", OR (b) extend startup_check to detect openpyxl import failure and add xlsx → force_disabled_formats accordingly. Option (a) is honest with the architecture; option (b) would close the notify-on-binary-loss gap.

#### QM-2 — AC#24 boot-time notify never tested
**Files:** No test invokes `notify_force_disabled_if_needed` with a fake adapter.
**Severity:** MEDIUM — HIGH-2 closure. spec §4 AC#24 explicit: "Force-disable notify lands in Telegram даже если adapter polling task ещё не запустился".
**Observation:** The implementation at `subsystem.py:652-682` correctly wraps in `asyncio.wait_for(timeout=10s)`, but no test plants `_force_disabled=True` + a fake adapter that records the `send_text` call.
**Impact:** A regression in the wait_for / try-except wrapping could silently swallow a Telegram timeout and leave the owner unaware that PDF/DOCX generation is dead. Edge case: under QM-1 this AC isn't reachable at all in prod; combined, it's a double blind spot.
**Fix:** Add `tests/test_phase9_force_disable_notify.py` covering: (a) `force_disabled=True` + working adapter → text sent + `_notified_force_disable=True`; (b) `force_disabled=False` → no notify; (c) adapter raising `TimeoutError` → warning logged + no crash; (d) idempotency: second call to `notify_force_disabled_if_needed` is a no-op.

#### QM-3 — AC#1 + AC#2 happy-path PDF/DOCX have no end-to-end test
**Files:** No `test_phase9_pdf_renderer.py` or `test_phase9_docx_renderer.py` exists.
**Severity:** MEDIUM — happy-path coverage gap. XLSX has dedicated renderer tests (`test_phase9_xlsx_renderer.py`); PDF + DOCX rely on the build-time Dockerfile RUN line.
**Observation:** `test_pandoc_binary_available` and `test_weasyprint_smoke_probe` skip on Mac dev hosts; the actual render pipeline (`pandoc → html → weasyprint → pdf`) has no `requires_pandoc + requires_weasyprint` test that asserts "tiny markdown → valid PDF byte structure (`b"%PDF-"` + `b"%%EOF"`) AND artefact registered in ledger AND audit row written".
**Impact:** A regression in the variant-subtraction string, the staging-html intermediate, or the WeasyPrint cffi/cairo init would only surface at first live render. Phase-8 ssh-not-found taught that "real subprocess" tests prevent prod-only failures.
**Fix:** Add `tests/test_phase9_pdf_renderer_smoke.py` + `tests/test_phase9_docx_renderer_smoke.py` with `requires_pandoc` markers and real renderer invocation. Verify magic bytes + EOF trailer + ledger entry. CI Docker test stage already installs pandoc + WeasyPrint, so these tests would run there.

#### QM-4 — AC#13 post-render output cap (`pdf-too-large`) not tested
**Files:** No test plants a large render.
**Severity:** MEDIUM — output cap is a defensive layer. Validator forbids `pdf_max_bytes > 20 MiB`, but the code path `bytes_out > settings.pdf_max_bytes → render_failed_output_cap` is uncovered.
**Observation:** Implementation uses strict `>` comparison (`pdf_renderer.py:259`). No test exercises a render that produces > cap bytes.
**Impact:** A regression in the cap check (e.g., dropped, or `>=` changed to `>`) would let oversized files reach the Telegram adapter where they fail with a less-actionable error.
**Fix:** Add a test that constructs a `PDFRenderError("render_failed_output_cap", "pdf-too-large")` via mocking `asyncio.to_thread` to write a >cap file at `final_path`, then assert the @tool envelope carries `reason="render_failed_output_cap"`.

#### QM-5 — AC#27 same-tool_use_id collision (last-write-wins) not tested
**Files:** `tests/test_phase9_handler_artefact_flush.py` uses `t1`/`t2` (different ids).
**Severity:** MEDIUM — defensive branch in `bridge.claude.py` `render_doc_tool_use_ids: set[str]`.
**Observation:** Spec §4 AC#27 says "Same tool_use_id collision (SDK contract violation) → log warning + last-write-wins". The bridge uses a `set`, so add-on-collision is a no-op. The handler `pending_artefacts: list[ArtefactBlock]` would carry both, sending both files. No test plants this collision.
**Impact:** Low-frequency bug class. SDK contract guarantees uniqueness; a defensive contract violation would not corrupt state but might double-send.
**Fix:** Document the actual collision behaviour in the docstring (the set-based dedup means a duplicate `tool_use_id` ToolUseBlock is silently a no-op for tracking purposes; the ArtefactBlock yield happens once per ToolResultBlock). If spec wants "log warning on collision", add the warning at the SET-add site.

#### QM-6 — pandoc `stdout=PIPE` + `proc.communicate()` deadlock risk on stderr overflow
**Files:** `src/assistant/render_doc/_subprocess.py:75-85`
**Severity:** MEDIUM — code path potentially fragile.
**Observation:** Pandoc's HTML output is written via `-o staging_html`, so stdout is empty. Stderr is captured via `PIPE` + `proc.communicate()`. `communicate()` reads both pipes concurrently into memory, so the classic "stdout-pipe-buffer-deadlock" doesn't apply. However, if pandoc emits >RAM stderr (e.g., a malformed-input error spam loop), the process will use unbounded memory before TimeoutError fires. The 256-codepoint truncation in error message is applied AFTER `communicate()` returns.
**Impact:** Adversarial markdown could OOM the daemon before timeout. Low likelihood in practice; pandoc rarely emits more than a few KB of stderr.
**Fix:** Use a streaming subprocess pattern that bounds stderr at, e.g., 64 KiB by closing stderr early. Or document the residual risk in §2.12.

#### QM-7 — Boot cleanup uses `time.time()` (wall clock); ledger uses `time.monotonic()`
**Files:** `src/assistant/render_doc/boot.py:51` (`time.time()`) vs `subsystem.py:249, 266, 281, 299` (`time.monotonic()`)
**Severity:** MEDIUM — defensible split but worth flagging.
**Observation:** Boot cleanup compares filesystem mtime (wall clock) to `time.time()` — correct. Ledger TTL compares `time.monotonic()` (intervals) — correct. The mix is appropriate. NTP skew during boot does NOT affect ledger TTL (boot cleanup runs once before subsystem construction, then ledger uses monotonic). No bug.
**Impact:** None — the split is correct. Documenting here so future readers don't "fix" it incorrectly.

### LOW

#### QL-1 — `test_pandoc_binary_available` SKIP on Mac dev silently passes
**Files:** `tests/test_phase9_render_doc_binaries.py:33`
**Severity:** LOW — coverage gap on dev only. CI Docker test stage runs with pandoc installed and the test asserts `result.returncode == 0`.
**Observation:** The owner running pytest on Mac without `brew install pandoc` sees "skipped" not "passed". CI skipping silently when the pkg is missing IS a phase-8 regression pattern (the ssh-not-found incident was caused by `requires_ssh` skipping).
**Fix:** Verify in CI workflow that the pytest job FAILS if `test_pandoc_binary_available` is skipped (check the skip-count from pytest's exit summary + grep for "test_pandoc_binary_available SKIPPED").

#### QL-2 — Empty / whitespace-only markdown not exercised
**Files:** No test for `content_md=""` or `content_md="   \n\n"`.
**Severity:** LOW — edge case.
**Observation:** Pandoc accepts empty input; WeasyPrint produces a valid (single empty page) PDF. xlsx renderer raises `markdown-no-tables` for empty. Unclear behaviour for PDF/DOCX edge case.
**Fix:** Add edge-case tests; document expected behaviour in spec.

#### QL-3 — `RENDER_DOC_ARTEFACT_TTL_S=0` accepted by validator (owner footgun)
**Files:** `src/assistant/config.py:420` — no validator on `artefact_ttl_s`.
**Severity:** LOW — operator footgun; would delete artefacts immediately on next sweep.
**Observation:** The settings object accepts `artefact_ttl_s=0`, leading to immediate deletion the next time the sweeper ticks. With the `in_flight=True` guard, freshly-rendered artefacts survive until `mark_delivered`, so the practical impact is delayed-by-one-sweep deletion after delivery. Acceptable for testing but could surprise.
**Fix:** Add validator `artefact_ttl_s >= 1`. OR document footgun in operator runbook.

#### QL-4 — Stderr leakage in audit row could carry filename or input snippet
**Files:** `src/assistant/render_doc/_subprocess.py:43` (`stderr=stderr[:512]`) + `audit.py:_truncate_str_fields` (256 codepoints).
**Severity:** LOW — leak is bounded.
**Observation:** `PandocError.stderr` is truncated to 512 chars, then audit truncation drops to 256. Pandoc stderr CAN include input filename. Filenames are sanitised so no path traversal — but the audit log is on disk; reading it requires shell access. Codepoint slicing is UTF-8-safe (Python str slicing is codepoint-based per R3.14).
**Fix:** No action needed; documented for review trail.

#### QL-5 — Cyrillic in `suggested_filename` not verified end-to-end via real Telegram FSInputFile
**Files:** Implementation accepts Cyrillic + emoji (sanitiser test); adapter passes the name unchanged.
**Severity:** LOW — Telegram supports RFC 7578 multipart filename* extension which encodes UTF-8.
**Observation:** The aiogram codepath uses `FSInputFile(path, filename=suggested_filename)`. RFC 7578 + aiogram 3.x correctly encode non-ASCII filenames. Owner smoke test should verify in live deploy.
**Fix:** Document in §4 owner smoke checklist: "send a PDF with Cyrillic suggested_filename via Telegram and verify it arrives as Cyrillic, not mojibake".

## AC coverage matrix

Legend: ✅ tested with real invariant assertion · 🟡 mocked / partial · ❌ missing
Where multiple files contribute, the dominant test file is named.

| AC | Status | Test file | Notes |
|----|--------|-----------|-------|
| AC#1 happy-path PDF | ❌ | none | QM-3: no real PDF render test (Mac SKIPS, CI Docker has pandoc but no test invokes it) |
| AC#2 happy-path DOCX | ❌ | none | QM-3 |
| AC#3 happy-path XLSX | ✅ | `test_phase9_xlsx_renderer.py` + `test_phase9_subsystem_render.py:59` | end-to-end xlsx works |
| AC#4 enabled=False parity | 🟡 | `test_phase9_subsystem_force_disable.py:44` | force_disabled=True asserted; @tool unregistration via bridge gating not directly asserted |
| AC#5 fully force-disabled | 🟡 | `test_phase9_subsystem_force_disable.py:100` | unreachable per QM-1; test documents the discrepancy |
| AC#5a partial force-disable | ✅ | `test_phase9_subsystem_force_disable.py:54,77` | both surfaces |
| AC#6 phase 1..8 regression-free | 🟡 | (whole regression suite) | not specifically asserted by phase 9 tests; pre-existing tests carry the load |
| AC#7 filename sanitization | ✅ | `test_phase9_filename_sanitization.py` | 16 reject + 7 accept rows; matches §2.4 ≥13-row matrix requirement |
| AC#8 input size cap | ✅ | `test_phase9_subsystem_render.py:38` | |
| AC#9 TTL sweeper reaps | ✅ | `test_phase9_artefact_ledger.py:69` | |
| AC#10 boot-time stale cleanup | ✅ | `test_phase9_boot_cleanup.py` | 5 tests including staging dir |
| AC#11 concurrency cap | ❌ | none (validator only) | QH-5 |
| AC#12 per-tool timeout | 🟡 | `test_phase9_render_doc_tool.py` | TimeoutError mapping tested via mock; subprocess SIGTERM path NOT |
| AC#13 Telegram size cap | 🟡 | `test_phase9_render_doc_settings.py:65-72` | validator-level only; runtime cap branch untested (QM-4) |
| AC#14 markdown injection (anchor) | 🟡 | `test_phase9_render_doc_tool.py:163` | mocked subsystem only; real fetcher not exercised |
| AC#14a `<img>` | 🟡 | (same as AC#14) | mocked single surface; no real fetcher invocation |
| AC#14b CSS `@import` | ❌ | none | QC-1 |
| AC#14c CSS `background:url()` (5 props) | ❌ | none | QC-1 |
| AC#14d `<base>` | ❌ | none | QC-1 |
| AC#14e `@font-face` | ❌ | none | QC-1 |
| AC#14f SVG xlink | ❌ | none | QC-1 |
| AC#14g `<object>/<embed>` | ❌ | none | QC-1 |
| AC#14h `data:` URI | ❌ | none | QC-1 |
| AC#14i CSS `var(url())` | ❌ | none | QC-1 |
| AC#14j inline math defanged | 🟡 | `test_phase9_render_doc_binaries.py:52` | variant subtraction listed in --list-extensions; actual render with `$E=mc^2$` not asserted |
| AC#15 system-binary smoke green | ✅ | `test_phase9_render_doc_binaries.py` + Dockerfile RUN line | covered both at test+ build time |
| AC#15a image size CI gate | ❌ | none — workflow file absent | QC-2 |
| AC#16 @tool gated by render_doc_tool_visible | 🟡 | `test_bridge_subagent_options.py` (existing) | indirect — bridge `_build_options` flag tested in phase 6 file; phase 9 specific assertion missing |
| AC#17 NotImplementedError handler resilience | ✅ | `test_phase9_handler_artefact_flush.py:146` | |
| AC#17b phase 5b/6e/8 regression | ✅ | (running phase 8 + subagent + scheduler suites; 16 tests passed standalone) | |
| AC#18 audit log date-stamped rotation | ✅ | `test_phase9_audit.py:60-101` | rotation + keep-last-N |
| AC#19 multi-iteration flush | ❌ | none | QH-1 |
| AC#20 sweeper skips in-flight | 🟡 | `test_phase9_artefact_ledger.py:53,84` | single-tick test; actual race not stressed (QH-2) |
| AC#21 Daemon.stop terminates pandoc | ❌ | none | QH-3 |
| AC#21a cumulative drain budget | ❌ | none | QH-4 |
| AC#22 filename adversarial (RTL/ZWSP/CON) | ✅ | `test_phase9_filename_sanitization.py:64-67` | RTL `\u202E`, ZWSP `\u200B`, CON, trailing-dot all asserted |
| AC#23 pandoc env minimal | ✅ | `test_phase9_pandoc_env_minimal.py` | 4 tests including secret-leak negative |
| AC#24 boot-time notify before polling | ❌ | none | QM-2 |
| AC#25 partial-failure inline-text | ✅ | `test_phase9_handler_artefact_flush.py:117` | |
| AC#26 render_failed reason granularity | 🟡 | `test_phase9_xlsx_renderer.py` (xlsx codes) + `test_phase9_render_doc_tool.py:172` (mocked) | xlsx codes covered; pandoc-exit-1 + pdf-too-large NOT covered (QM-4) |
| AC#27 tool_use_id ledger | 🟡 | `test_phase9_handler_artefact_flush.py:91` | different-ids covered; same-id collision not |
| AC#28 envelope schema_version | ✅ | `test_phase9_bridge_artefact_block.py:66` | |
| AC#29 RSS observer integration | 🟡 | `test_phase9_artefact_ledger.py:122` | `get_inflight_count` returns 0/1; full RSS log line schema (subsystem-disabled OMITS field) NOT directly asserted |
| AC#30 SKILL.md present + valid | ✅ | `test_phase9_skill_md_present.py` | 5 tests covering frontmatter, triggers, MCP ref, anti-pattern |

**Summary:** 33 ACs; ✅ 14 fully covered, 🟡 13 partial/mocked, ❌ 6 missing.

## Spot-checks performed

### Code-level deep dives

#### 1. `RenderDocSubsystem.render` flow ordering
File: `src/assistant/render_doc/subsystem.py:366-440`. Verified that:
- subsystem force-disable check fires BEFORE semaphore acquire (correct — disabled calls must not consume slots);
- per-format check fires before semaphore (same reason);
- input size cap encoded as `len(content_md.encode("utf-8")) > max_input_bytes` — correct strict `>` semantics;
- `task_handle` registered in `_pending_set` with `add_done_callback(self._pending_set.discard)` — correct discard discipline (the explicit `self._pending_set.discard(task_handle)` in `finally` is redundant-but-safe).

One subtle issue: when `task_handle` is None (caller's choice), the @tool body is NOT registered for drain. The @tool body at `tools_sdk/render_doc.py:191-202` always passes `asyncio.current_task()` — never None — so this is fine, but a future caller could trip the gap. Recommend documenting "caller MUST pass current task or accept no-drain semantics".

#### 2. Pandoc subprocess env (HIGH-1) — AC#23
`_pandoc_env()` returns exactly `{PATH, LANG, HOME}`; secret env vars never propagate. Test `test_phase9_pandoc_env_minimal.py:18` plants `TELEGRAM_BOT_TOKEN`/`GH_TOKEN`/`ANTHROPIC_API_KEY`/`CLAUDE_OAUTH_TOKEN` in os.environ and asserts they don't appear in any value. PATH inheritance, default `LANG=C.UTF-8` and `HOME=/tmp` fallbacks verified. Returned dict is a fresh copy — mutating doesn't pollute `os.environ`. ✅

#### 3. Filename sanitiser matrix (CRIT-5) — AC#7 / AC#22
`_sanitize_filename` uses `unicodedata.category()` strip; test parametrize covers Windows-reserved (4: CON, con.report, LPT5, COM1.pdf), path components (3: `../etc/passwd`, `/abs/path`, `a\b`), leading-dot/dots-only (5: `.hidden`, `.`, `..`, `...`, `....`), trailing dot/space (2: `report .`, `report.`), length cap (1: `a*97`), null-after-norm (1: `\x00`), Cyrillic+emoji+ZWSP+RTL (5 accepted). 22 explicit cases. ≥13-row spec matrix met. Edge cases verified: trailing space handling — `"report "` strips to `"report"` and is accepted; RTL-spoof `report\u202Efdp` silently strips → `reportfdp`; ZWSP `a\u200Bb` → `ab`. ✅

#### 4. PDF magic bytes (R2.12) — AC#15
`test_weasyprint_smoke_probe` asserts `data.startswith(b"%PDF-")` AND `b"%%EOF" in data[-256:]`. Skips when WeasyPrint unavailable; runs in CI Docker stage where pandoc + WeasyPrint are guaranteed. Confirms actual byte structure of WeasyPrint output, not just "no exception raised". ✅ (subject to QL-1: silent skip on Mac dev)

#### 5. In-flight ledger thread safety (W2-HIGH-2) — AC#20
`_artefacts_lock: asyncio.Lock` acquired in `register_artefact` (subsystem.py:244), `mark_delivered` (261), `mark_orphans_delivered_at_shutdown` (280), AND in two phases of `_sweep_iteration` (301 snapshot under lock → unlink loop OUTSIDE → 326 remove-from-dict under lock). The two-phase lock pattern is correct: long-running disk I/O doesn't hold the lock; only fast snapshot/remove operations do. Concurrent test plants 21 simultaneous coroutines (10 register + 10 mark + 1 sweep) → no `dictionary changed size during iteration` raises. ✅

#### 6. Pandoc subprocess SIGTERM/SIGKILL recipe — AC#21
`_subprocess.run_pandoc` (lines 61-108) catches `(asyncio.CancelledError, TimeoutError)`, calls `proc.terminate()`, awaits with `pandoc_sigterm_grace_s` (5s default), falls through to `proc.kill()` if still alive, awaits `pandoc_sigkill_grace_s` (5s default), then re-raises. `ProcessLookupError` is suppressed via `contextlib.suppress` (correct: pandoc may have exited between timeout and SIGTERM). The re-raise of CancelledError preserves cancel propagation. ✅ implementation correct; ❌ no end-to-end test (QH-3).

#### 7. Markdown variant subtraction — CRIT-2 / W2-MED-1
`_PDF_MARKDOWN_VARIANT` is `"markdown-raw_html-raw_tex-raw_attribute-tex_math_dollars-tex_math_single_backslash-yaml_metadata_block"`. Same string in DOCX renderer (consistency). Dockerfile build-time RUN includes `pandoc --list-extensions=...` + `grep -E '^-(raw_html|raw_tex|raw_attribute|tex_math_dollars|yaml_metadata_block)$'` to catch silent-no-op syntax errors. R-Pandoc closure verified — pandoc accepts the subtraction syntax and shows each removed extension in its list-extensions output. ✅

#### 8. Audit log truncation — W2-MED-4
`_truncate_str_fields` slices every str value at 256 codepoints (codepoint-correct per R3.14 — Python str slicing operates on codepoints, not bytes; multi-byte UTF-8 handled correctly). `error` field carries pandoc stderr truncated to 512 chars at `_subprocess.PandocError.__init__`, then 256 by audit. Total bound: 256 codepoints in audit row. Future str-typed field additions inherit the cap automatically. ✅

#### 9. Boot cleanup wall clock vs ledger monotonic — design split
Boot uses `time.time()` (filesystem mtime comparison, correct semantics); ledger uses `time.monotonic()` (interval semantics, correct). NTP skew during daemon uptime does NOT affect ledger TTL because monotonic clock isn't NTP-influenced. Boot cleanup runs ONCE before subsystem construction; ledger always starts empty after boot. ✅ (QM-7 — split is correct, flagged for documentation)

#### 10. Settings validator cross-field consistency
`tool_timeout_s >= pdf_pandoc + pdf_weasyprint` enforced (default 60 vs 20+30=50); cap-vs-Telegram-cap enforced (`pdf_max_bytes <= 20 MiB`); `render_drain_timeout_s == 0` opt-out preserved (DEFAULT path skips validator since model_fields_set excludes default-default values); pandoc grace must fit drain when drain > 0. 9 tests in `test_phase9_render_doc_settings.py` cover all branches including W2-MED-3 default-vs-owner-set distinction. ✅

#### 11. Bridge artefact envelope parser
`_parse_render_doc_artefact_block` (claude.py:104-168) handles `content=None`, `content="bare string"`, `content=[non-dict]`, missing `text` key, malformed JSON, non-dict envelope, missing `path`/`format`/`suggested_filename`, schema_version mismatch, `ok=False`, `kind != "artefact"`. 7 tests covering each branch. Falls back to `block.tool_use_id` when envelope's tool_use_id is missing/non-str — defensive correctness. ✅

#### 12. Daemon.stop drain ordering
main.py:1028-1135 sequence: `_adapter.stop` → `_audio_persist_pending` drain → `_vault_sync_pending` drain → `_render_doc_pending` drain → `mark_orphans_delivered_at_shutdown` → `_bg_tasks` cancel → SQLite close. Render drain uses `asyncio.wait(..., timeout=render_drain_timeout_s, return_when=ALL_COMPLETED)`; non-completed tasks get `t.cancel()` + `gather(return_exceptions=True)` mop-up. Ordering correct: render drain BEFORE bg_tasks cancel — so the @tool body's `proc.terminate()` fires inside drain budget, not racing the sweeper task cancel. ✅

#### 13. Pre-existing flake confirmation
`test_save_transcript_concurrent_serialises` lives in `tests/test_memory_store_save_transcript.py` first added in commit `6830ad9` (phase 6c, 2026-04-27). Pre-dates phase 9 by 6 days. Confirmed unrelated to phase 9. ✅

#### 14. Dockerfile pandoc + WeasyPrint runtime libs
apt list at line 137-149: `pandoc, libpango-1.0-0, libpangoft2-1.0-0, libharfbuzz-subset0, fonts-dejavu-core`. Build-time RUN at lines 161-164 verifies pandoc binary, variant subtraction syntax, and `import weasyprint` succeed. Phase-8 ssh-not-found pattern correctly applied — fail-LOUD if any dep missing. ✅

#### 15. SKILL.md frontmatter + body
File at `skills/render_doc/SKILL.md` (86 lines). Frontmatter has `name: render_doc`, `description: <multi-line Cyrillic>`, `allowed-tools: ["mcp__render_doc__render_doc"]`. Body contains all 4 Cyrillic trigger phrases («сделай PDF», «сгенерь docx», «дай excel таблицу», «сделай отчёт») + MCP tool reference + «Не вызывай» anti-pattern section warning against memory_*, WebFetch, vault_push_now misuse. AC#30 ✅

## Top-3 fix-pack priorities

### P1 — Close AC#14 markdown-injection coverage (QC-1)
Add `tests/test_phase9_url_fetcher_surfaces.py` with 9 parametrised cases driving the real PDF pipeline through each documented adversarial fetch surface. Skip-on-host-missing pattern. CI Docker test stage runs all 9; Mac dev skips. Without this, the most-cited security AC of phase 9 has effectively zero adversarial validation.

### P2 — Add `docker-image-size-check.yml` + workflow trigger (QC-2)
Spec §4 AC#15a explicitly mandates this workflow with a 300 MB delta gate. Mass-image-bloat regressions currently ship without gating. ~30 LOC YAML; reference baseline image size after first build.

### P3 — End-to-end lifecycle tests for AC#19 + AC#21 + AC#21a (QH-1, QH-3, QH-4)
The three "stop / cancel / drain" ACs are entirely unexercised at the integration level. A single new test file `tests/test_phase9_lifecycle.py` covering: (a) multi-iteration ordering with mock bridge stream; (b) Daemon.stop with hung pandoc subprocess + assertion that proc.terminate() then proc.kill() were called; (c) cumulative drain budget across mocked adapter.stop + vault + render. Targets the highest-prior bug classes (CRIT-1, CRIT-4, W2-HIGH-1) which were the very reasons these ACs were added in v1→v2.

## Bug-risk pattern audit (from QA brief)

1. **Off-by-one size caps** — `>` strict greater across all 3 formats. Spec "превышение" matches. ✅
2. **`_artefacts_lock` race** — held on every mutation including `mark_delivered`, `register_artefact`. Sweeper splits read/write phases under lock. ✅ structural, ❌ stress-tested (QH-2)
3. **Resource leaks on render failure** — staging cleanup in `finally` block of `render_pdf`; `final_path.unlink(missing_ok=True)` in every except branch. ✅
4. **Error-message leak** — codepoint truncation at 256 in audit, 512 in PandocError. UTF-8-safe (Python str slicing). ✅
5. **Time-based bugs** — wall vs monotonic split documented (QM-7). ✅
6. **Filename collision** — uuid4 path component (10^-36 collision rate); `suggested_filename` collision is owner-visible only (Telegram delivers both files with same name). ✅
7. **aiogram retry semantics for send_document** — handler does NOT loop-retry on transient 5xx; relies on single-attempt + Telegram client library. Sweeper TTL is 600s default; aiogram default timeout 60s. Combined window: artefact alive ≥600s after delivery, retry within window if needed. ✅ acceptable.
8. **Cyrillic suggested_filename via Telegram** — RFC 7578 multipart filename* encoding handles UTF-8. aiogram passes through. Owner smoke needed (QL-5).
9. **Pandoc stdout/stderr deadlock** — output goes to `-o` file; stdout PIPE empty; stderr captured via `communicate()`. PIPE buffer 64 KiB only matters if stdout were used heavily. ✅ acceptable; QM-6 flags adversarial-stderr OOM risk as MEDIUM.
10. **Empty markdown** — untested (QL-2).
11. **Whitespace-only markdown** — untested (QL-2).
12. **DOCX 0-byte output** — pandoc rc=0 + missing file → `pandoc-no-output` raised. Test would mock pandoc returning rc=0 without writing file. Not directly tested but defensive branch present.
13. **TTL=0 footgun** — accepted by validator (QL-3).
14. **Concurrent enabled toggle** — boot cleanup with 24h threshold catches half-rendered artefacts; ledger empty on boot. Half-rendered staging files unconditionally wiped. ✅ structurally sound.

## Notes on test-suite hygiene

- **`requires_pandoc` skip marker** — ✅ defined in `pyproject.toml` (verified via grep). CI Docker test stage installs pandoc; tests run there. Mac dev SKIPS visibly. Recommend adding a CI-side "no test SKIPPED" assertion for `test_pandoc_binary_available` per QL-1.
- **Test isolation** — Phase 9 tests use `tmp_path` for `artefact_dir`; conftest `_reset_render_doc_ctx` resets per test. ✅ no global pollution.
- **Pre-existing flake** — `test_save_transcript_concurrent_serialises` confirmed phase 6c, unrelated to phase 9.

## Test file coverage map

| Test file | Tests | Lines | ACs covered |
|-----------|-------|-------|-------------|
| `test_phase9_artefact_ledger.py` | 5 | 130 | AC#9, AC#20 (partial), AC#29 (partial) |
| `test_phase9_audit.py` | 5 | ~110 | AC#18 (rotation+keep-last-N+truncation+schema_version) |
| `test_phase9_boot_cleanup.py` | 5 | ~100 | AC#10 (final + .staging unconditional) |
| `test_phase9_bridge_artefact_block.py` | 7 | 119 | AC#28, parser branches |
| `test_phase9_dockerfile_apt_packages.py` | 2 | ~50 | AC#15 (deprecated pkg negative) |
| `test_phase9_filename_sanitization.py` | 5 (with parametrize: 22 cases) | 99 | AC#7, AC#22 |
| `test_phase9_handler_artefact_flush.py` | 3 | 164 | AC#17, AC#25 |
| `test_phase9_markdown_tables_parser.py` | 12 | ~150 | parser correctness for AC#3 |
| `test_phase9_pandoc_env_minimal.py` | 4 | 60 | AC#23 |
| `test_phase9_render_doc_binaries.py` | 4 (3 skip on Mac) | 134 | AC#15, AC#14j (variant subtraction visibility) |
| `test_phase9_render_doc_settings.py` | 9 | ~120 | settings validator (7 cross-field branches) |
| `test_phase9_render_doc_tool.py` | 7 | ~210 | @tool body forwarding, AC#26 (mocked) |
| `test_phase9_skill_md_present.py` | 5 | ~85 | AC#30 |
| `test_phase9_subsystem_force_disable.py` | 4 | 127 | AC#5 (partial, see QM-1), AC#5a |
| `test_phase9_subsystem_render.py` | 6 | ~130 | AC#3, AC#8, AC#5a |
| `test_phase9_xlsx_renderer.py` | 6 | ~110 | AC#3, AC#26 (xlsx codes) |

**Total:** 16 test files (coder report says "15"; off-by-one), 89 phase-9 test functions discovered, 106 test cases when expanding parametrize, 4 SKIP on Mac dev (pandoc/weasyprint).

## Severity totals

- 🔴 CRITICAL: 2 (QC-1, QC-2)
- 🟠 HIGH: 5 (QH-1, QH-2, QH-3, QH-4, QH-5)
- 🟡 MEDIUM: 7 (QM-1..QM-7)
- 🟢 LOW: 5 (QL-1..QL-5)

Total: 19 findings.

Test pass count: 106 phase-9 tests pass + 4 skip on Mac dev. Coder claim of "1120 passed" not independently verified in this review (full suite run blocked by long-running async fixtures); phase-9 subset PASS confirmed.
