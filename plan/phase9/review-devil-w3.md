# Phase 9 — Devil's Advocate Wave 3 review (post-implementation, integration-boundary)

Lens: cross-component integration boundaries, ordering invariants, spec-implementation drift, fault-injection gaps. Other reviewers (code-reviewer / qa-engineer / devops) cover pattern compliance, AC coverage, ops readiness — w3 hunts for what they would miss.

## Verdict

**RECONSIDER (yellow) — 1 critical user-visible ordering bug + meaningful test gaps for safety-critical drain/timeout ACs. Not red because failure modes degrade gracefully, but the spec's signature ordering invariant (AC#19 `text₁ → doc₁ → text₂ → doc₂`) is currently provably violated by the Telegram adapter's chunk-buffering pattern, and the ACs that justify the entire `_render_doc_pending` machinery (AC#12, AC#21, AC#21a) have ZERO behavioural tests.**

---

## Findings (CRITICAL / HIGH / MEDIUM / LOW)

### W3-CRIT-1 — `text₁ → doc₁` ordering invariant is structurally impossible with current Telegram adapter

**Spec contract (AC#19, §2.5)**: For a multi-iteration turn `render_doc(pdf) → text → render_doc(docx) → text`, owner sees `text₁ → pdf → text₂ → docx`.

**Implementation reality** (`adapters/telegram.py:295-310`):
```python
chunks: list[str] = []
async def emit(text: str) -> None:
    chunks.append(text)            # text NEVER hits Telegram during streaming
async with ChatActionSender.typing(...):
    await self._handler.handle(incoming, emit)
full = "".join(chunks).strip() or "(пустой ответ)"
for part in _split_for_telegram(full, ...):
    await self._bot.send_message(message.chat.id, part)   # <-- ALL text fires AFTER handle() returns
```

The handler's `_flush_artefacts` calls `adapter.send_document(...)` directly during streaming (`handlers/message.py:973`). So docs leave for Telegram in stream order. But text accumulates in a list and ONLY fires AFTER `handler.handle()` exits.

**Actual owner-visible order**: `[doc₁, doc₂, …, all-docs] → [text₁+text₂+…]`. This is the OPPOSITE of what the spec promises. The model's narrative explanation arrives AFTER the artefact, with no causal continuity ("here's the PDF you asked for: [doc] [silence] some seconds later text comes through explaining what's in it").

**Why other reviewers miss this**: code-reviewer reads files in isolation and sees `_flush_artefacts` is correctly placed. qa-engineer doesn't have an end-to-end Telegram fixture. devops looks at images/runtime. Only an integration-boundary view catches that the emit-callback contract is incompatible with the real-time-flush invariant.

**No test exists for AC#19** (`grep AC#19 tests/` matches only phase-8 files). This bug ships untested.

**Mitigation options** (pick one before deploy):
1. Modify `_flush_artefacts` to first dispatch any pending `chunks` accumulator via `emit_direct` / a new bypass channel, BEFORE calling `send_document`. Requires plumbing a "flush-text-now" callback through the handler.
2. Accept the actual order and update spec + SKILL.md so the model knows "text always trails artefacts in this turn". Document the reversal.
3. Defer single-iteration turns through current pattern; only multi-iteration turns trigger interim text-flush. Most real owner usage is single-iteration, so this is operationally cheap.

Verified by: `adapters/telegram.py:297` defines `emit` as list-append only; no streaming send. `handlers/message.py:822` calls `await emit(text_out)` which is purely list-append. `handlers/message.py:803-806` calls `await self._flush_artefacts(...)` → `adapter.send_document` direct.

---

### W3-CRIT-2 — `Daemon.stop` orders `adapter.stop()` BEFORE `_render_doc_pending` drain

**Location**: `main.py:1043-1118`. Sequence:
1. `await self._adapter.stop()` — calls `bot.session.close()` (`telegram.py:1285`). Aiogram cancels polling task → cascades to in-flight handler tasks.
2. Vault sync drain (line 1055-1083).
3. `_render_doc_pending` drain (line 1089-1118).

**The risk**: by the time the render_doc drain block runs, the @tool body tasks have ALREADY received CancelledError from the polling-task cancel cascade (the @tool body runs as a sub-task of the SDK's MCP dispatch, which lives inside `bridge.ask`, which lives inside the handler task). The `subsystem.render` `try/except asyncio.CancelledError` at `subsystem.py:560-563` catches and re-raises; the `add_done_callback(self._pending_set.discard)` then empties the set.

**Net effect**: `_render_doc_pending` is essentially always empty by the time the drain block runs. The 20s drain budget is dead code in the normal flow. The ONLY scenario where `_render_doc_pending` would be non-empty is a render initiated outside the handler path (scheduler? subagent? — phase 9 doesn't expose this).

But there's a SUBTLER issue: if a render IS in-flight when SIGTERM arrives, `adapter.stop()` cancels the handler synchronously, which propagates to the @tool body which tries to clean up staging files in `try/finally` (`pdf_renderer.py:274-284`). At that exact moment, `bot.session.close()` has already been called or is about to be — IRRELEVANT for the renderer (renderer doesn't touch bot), but the handler's `_flush_artefacts` (which also runs in the handler task) MAY have started a `bot.send_document(...)` call before the cancel hit. That send can fail with `ClientSessionAlreadyClosed`.

**Better order**: drain `_render_doc_pending` BEFORE `adapter.stop()`. Phase-8 vault_sync drains AFTER adapter.stop is fine because vault_sync's pending tasks don't use the adapter. Phase-9 render_doc DOES use the adapter (for send_document), so the order should match the dependency graph.

**Why other reviewers miss this**: each reviewer sees their own slice. The spec says "render drain before _bg_tasks cancel" — this IS satisfied. But it doesn't say "before adapter.stop", which is the actual dependency.

**Probability**: low (single-owner, render seconds, SIGTERM is rare). **Impact**: medium (one-off undelivered artefact + ugly traceback in logs at shutdown).

---

### W3-HIGH-1 — Ledger leak on @tool body tracking failure (`task_handle=None` from subagent path)

**Location**: `subsystem.py:425-440` and `tools_sdk/render_doc.py:191-203`.

```python
task = asyncio.current_task()  # in @tool body
...
result = await asyncio.wait_for(
    sub.render(content_md, fmt, sanitized, task_handle=task),
    timeout=sub._settings.tool_timeout_s,
)
```

`asyncio.current_task()` in @tool body returns the task that's running the MCP @tool dispatch. But a subagent-spawned render call (when subagent path is enabled in future phases) might NOT have a current task (`asyncio.run_in_executor` thread, blocking subprocess wrapper, etc.). In those cases `task=None` → `task_handle=None` → render proceeds but is NOT in `_pending_set` → `Daemon.stop` drain misses it → SIGKILL mid-render.

Currently subagent path doesn't use render_doc, but the spec doesn't ENFORCE this. A future phase that adds it will need to remember to track. Add a defensive check: if `task is None`, log warning + continue (no behavioural change today, but observability for future).

---

### W3-HIGH-2 — Audit log rotation has unhandled second-resolution race for parallel renders

**Location**: `audit.py:115-118`.
```python
now = dt.datetime.now(dt.UTC).replace(microsecond=0)
stamp = now.strftime("%Y%m%d-%H%M%S")
rotated = path.with_suffix(path.suffix + f".{stamp}")
os.replace(path, rotated)  # <-- second os.replace silently overwrites
```

With `render_max_concurrent=2`, two parallel renders can both:
1. Stat the live file at the same time, both see size > cap.
2. Both compute identical `stamp` (1-second resolution).
3. Both call `os.replace(path, rotated)` — the second silently overwrites the first rotated file.

**Data loss**: rotation #1's audit content is lost.

The code comment at audit.py:111-114 acknowledges "audit volume is well under 1/s" — TRUE for delivery rate, but FALSE for rotation events when both parallel renders trigger rotation in the same tick. With `audit_log_max_size_mb=10` MB and a single render pushing kilobytes, this is unlikely. But with adversarial input that bloats audit rows (e.g. very long error messages truncated to 256 chars × concurrent-rate × accumulated history) and a fresh post-rotation pop → both writes near-simultaneous post-rotation grow → second rotation collision possible.

**Lock-free fix**: append microseconds OR uuid suffix to stamp. Trivial change.

**Why other reviewers miss this**: it's a textbook double-rotation race. qa-engineer focused on coverage, not on race scenarios. devops focused on log volume.

---

### W3-HIGH-3 — `RenderDocSubsystem.render` does NOT support `task_handle=None` discovery path for @tool body

**Re-check of W3-HIGH-1**: looking at `tools_sdk/render_doc.py:191`:
```python
task = asyncio.current_task()
task_name = task.get_name() if task is not None else "no-task"
tool_use_id = f"render-doc-{task_name}-{id(task)}"
```

If `task is None`, `id(task) == id(None) == constant`. Two renders with no current task get the SAME synthetic tool_use_id. Then in `bridge.claude._parse_render_doc_artefact_block`, the envelope's tool_use_id is used for the ArtefactBlock. Two ArtefactBlocks with identical tool_use_ids might confuse a future de-dup or reconciliation step.

Currently this is dormant code (current_task is always populated in async @tool dispatch), but it's a latent bug.

---

### W3-MED-1 — `notify_force_disabled_if_needed` is called via `_spawn_bg(...)` but force-disable check inside the helper short-circuits when `force_disabled=False`

**Location**: `main.py:702-711`.
```python
if self._render_doc.force_disabled:
    self._spawn_bg(self._render_doc.notify_force_disabled_if_needed())
else:
    self._spawn_bg_supervised(self._render_doc.loop, name="render_doc_sweeper")
```

The `notify_force_disabled_if_needed` body checks `if not self._force_disabled: return` (subsystem.py:660-661), so the inner check is redundant given the outer `if self._render_doc.force_disabled` gate. Not a bug — just dead defence. Worth a one-line cleanup.

But more importantly: the outer if/else means **if `force_disabled=True` the sweeper does NOT spawn**. This is correct (no point sweeping if we'll never produce artefacts). But there's no log line saying "sweeper not spawned because subsystem fully disabled" — owner debugging an "artefact stuck" report would need to grep `render_doc_force_disabled` separately.

---

### W3-MED-2 — Test naming misleading: `test_both_missing_fully_force_disabled` asserts `force_disabled is False`

**Location**: `tests/test_phase9_subsystem_force_disable.py:100-126`.

The test name says "fully force disabled" but the assertion is `assert sub.force_disabled is False` (line 126). This is INTENTIONAL — the test verifies that even when both pandoc AND weasyprint are missing, xlsx (pure-Python openpyxl) keeps the subsystem partially alive, hence `force_disabled` stays False.

But this is confusing: a future maintainer reads the test name and expects an assertion of `force_disabled is True`. Either:
1. Rename to `test_pandoc_and_weasyprint_missing_keeps_xlsx_alive`.
2. Add a clarifying assertion: when `enabled=False` is the ONLY way to fully force_disable, document that explicitly.

Spec §2.2 lines 323-327 say "Полный force-disable subsystem'а (когда **все** форматы заблокированы)" — i.e. spec expects all-3-blocked case to fully disable. But since openpyxl is pure-Python wheel, this case is unreachable from `startup_check` alone — only `enabled=False` triggers it. Spec ambiguity.

---

### W3-MED-3 — Concurrency cap (AC#11) has zero behavioural test

**Spec AC#11**: 3 parallel `render_doc` calls — third must wait for one of first two. `render_max_concurrent=2`.

**Tests check**: validator rejects `render_max_concurrent=0` (`tests/test_phase9_render_doc_settings.py:84-86`). That's IT.

No test confirms the semaphore actually serialises. If `_render_sem = asyncio.Semaphore(2)` were broken (e.g. someone refactored to use a non-asyncio primitive), this would silently allow unbounded parallelism. With `pdf_max_bytes=20MiB` and a single WeasyPrint instance peaking at ~500MiB RSS, 5+ parallel renders OOM-kill the daemon.

**Recommended test**: spawn 3 mocked renders that hold the semaphore for a controlled duration; assert one is queued.

---

### W3-MED-4 — Per-tool timeout (AC#12) has zero behavioural test

`render_doc.py:197-217` wraps `sub.render` in `asyncio.wait_for(timeout=sub._settings.tool_timeout_s)`. Returns `reason="timeout", error="tool-timeout-exceeded"` envelope on TimeoutError.

But there's NO test that triggers this. `pdf_pandoc_timeout_s` and `pdf_weasyprint_timeout_s` similarly untested for actual firing.

If `wait_for` were accidentally removed, or the inner cancellation propagation were broken (`pandoc_sigterm_grace_s + pandoc_sigkill_grace_s = 10s` exceeds tool_timeout=60s? No, 10s < 60s OK), nothing catches it.

**Recommended test**: mock pandoc subprocess to never resolve; assert envelope shape + audit row.

---

### W3-MED-5 — `Daemon.stop` drain test (AC#21, AC#21a) missing

Spec calls out `test_phase9_daemon_stop_terminates_pandoc.py` and `test_phase9_daemon_stop_drains_render_pending.py` (description.md line 1937-1939, 2335). Neither exists in `tests/`.

The drain code path (`main.py:1089-1118`) is therefore tested only by inference. Without a test that:
1. Starts a render that hangs in pandoc.
2. Triggers `Daemon.stop`.
3. Asserts pandoc receives SIGTERM, dies in <5s, staging cleaned, no orphan PID.

We have no proof the drain works as designed. Combined with W3-CRIT-2, this is the most concerning correctness gap.

---

### W3-MED-6 — `_artefact_dir.mkdir` in `_dispatch` runs OUTSIDE lock — TOCTOU on dir-removal during shutdown

**Location**: `subsystem.py:496-499`.
```python
async def _dispatch(self, ...):
    self._artefact_dir.mkdir(parents=True, exist_ok=True)
    self._staging_dir.mkdir(parents=True, exist_ok=True)
    uid = uuid4().hex
    final_path = self._artefact_dir / f"{uid}.{fmt}"
```

If a sweeper or admin removes `<data_dir>/artefacts/` between the mkdir and the renderer's `final_path.write` / `final_path.stat()`, the renderer fails with FileNotFoundError. Unlikely in practice (no admin tool removes it) but worth a defensive comment.

More relevantly: `_cleanup_stale_artefacts` runs at boot ONLY (main.py:450). It doesn't run during normal operation. So this is a paper risk only.

---

### W3-MED-7 — `_envelope` returns a dict that spreads payload at top level — semantics undefined for MCP transport

**Location**: `tools_sdk/render_doc.py:56-63`.
```python
def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        **payload,
    }
```

MCP @tool result schema expects `{"content": [...]}`. Spreading `**payload` adds top-level keys (`ok`, `kind`, `path`, `bytes`, etc.). Other tools_sdk modules (scheduler, vault) follow the same pattern, so this is established convention. But:

- The bridge's `_parse_render_doc_artefact_block` reads ONLY from `block.content[0]["text"]` (parses JSON) — the spread fields are dead.
- The SDK's MCP transport may or may not strictly validate result schema. If a future SDK upgrade enforces strict schema, the spread fields trigger validation errors.

Document the spread as intentional decoration (matches scheduler/vault convention) so a future maintainer doesn't "clean it up" thinking it's a typo.

---

### W3-LOW-1 — `tool_use_id` in envelope is dead code at bridge layer

**Location**: `tools_sdk/render_doc.py:191-193`.
```python
task = asyncio.current_task()
task_name = task.get_name() if task is not None else "no-task"
tool_use_id = f"render-doc-{task_name}-{id(task)}"
```

The synthetic `tool_use_id` lands in the envelope and bridge re-extracts it (`claude.py:160-162`), preferring envelope's value over `block.tool_use_id`. But the **bridge's matching** (`render_doc_tool_use_ids` set, claude.py:484, populated at line 526) uses the SDK's actual `ToolUseBlock.id`, NOT the envelope's synthetic one. So:

- If the synthetic tool_use_id == SDK's tool_use_id (impossible — synthetic uses task id + name): no effect.
- If they differ (always): the ArtefactBlock's `tool_use_id` field carries the synthetic one but the bridge's routing uses SDK's. ArtefactBlock.tool_use_id is then a useless field — handler doesn't use it either (handlers/message.py:786-788 just appends to pending_artefacts).

**Cleanup recommendation**: use `block.tool_use_id` directly in bridge, drop the synthetic one. Or document that ArtefactBlock.tool_use_id is informational-only.

---

### W3-LOW-2 — Markdown `tex_math_dollars` subtraction in spec/Dockerfile but NOT in `_DOCX_MARKDOWN_VARIANT`?

**Verification**:
- spec §2.6 (line 47-55 pdf_renderer.py): subtracts `raw_html`, `raw_tex`, `raw_attribute`, `tex_math_dollars`, `tex_math_single_backslash`, `yaml_metadata_block`. ✓
- `_DOCX_MARKDOWN_VARIANT` (docx_renderer.py:31-39): IDENTICAL list. ✓
- Dockerfile pandoc smoke (line 159-160): `markdown-raw_html-raw_tex-raw_attribute-tex_math_dollars-tex_math_single_backslash-yaml_metadata_block` then greps `^-(raw_html|raw_tex|raw_attribute|tex_math_dollars|yaml_metadata_block)$`. **Missing `tex_math_single_backslash` from grep**! If pandoc decides not to recognise that subtraction, build still passes.

This is a real but minor Dockerfile sanity-check gap. Tighten the grep to include `tex_math_single_backslash`.

---

### W3-LOW-3 — Default filename uses `replace(":", "-")` but `dt.datetime.strftime` doesn't emit colons

**Location**: `subsystem.py:359-364`.
```python
def _default_filename(self, fmt: str) -> str:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{fmt}-{stamp}"
```

The strftime format already uses `-` instead of `:`. The "replace ':' with '-'" comment is misleading — there's no replace. Code is correct; comment is stale. Spec §2.4 says default should be `f"{fmt}-{utc_iso}"` → e.g. `pdf-2026-05-02T12-34-56Z`. Implementation matches. ✓

---

### W3-LOW-4 — `_validate_paths._sanitize_filename` rejects `Cn` (unassigned codepoints) — may fail-open for new Unicode versions

`Cn` is "unassigned"; future Unicode codepoints land here. Stripping unassigned chars is correct for THIS Unicode version but if the runtime ships a newer unicodedata than the build host, an "unassigned-now-assigned" codepoint passes through as a valid Unicode category (e.g. Lu).

For phase 9 this is a non-issue (single-owner, known input). Worth noting for future HIGH-tier security work.

---

## Cross-component spot-checks

| Component pair | Boundary check | Status |
|---|---|---|
| Bridge → handler ArtefactBlock yield | bridge yields after each ToolResultBlock; handler accumulates into `pending_artefacts` and flushes on ResultMessage. **Correct per spec.** | ✓ |
| Handler → adapter `send_document` | per-artefact try/except + text fallback; `mark_delivered` in finally regardless of success. **Correct per HIGH-3.** | ✓ |
| Handler emit-callback contract → telegram chunks list | **BROKEN** — text accumulates, only sends after handle() returns; defeats AC#19 ordering invariant. | **W3-CRIT-1** |
| Subsystem `_pending_set` → Daemon.stop drain | Drain runs AFTER `adapter.stop` — when adapter cancels handlers, pending_set already empty by callback. Drain is no-op in normal path. | **W3-CRIT-2** |
| Subsystem `_artefacts_lock` → `_sweep_iteration` 3-phase | Phase 1 read under lock, Phase 2 unlink outside, Phase 3 re-acquire. **Correct per W2-HIGH-2.** | ✓ |
| `_artefacts_lock` → `mark_delivered` + `register_artefact` parallel | Lock acquired in both. **Correct.** | ✓ |
| `_audit_path` rotation → parallel render | second-resolution stamp collision possible with `render_max_concurrent=2`. | **W3-HIGH-2** |
| Bridge `render_doc_tool_visible` flag → @tool registration | Owner bridge `visible=enabled AND not force_disabled`; picker/audio default False. **Correct.** | ✓ |
| Subsystem.render → audit append | Single audit row per call (success OR failure); both branches verified. | ✓ |
| Subsystem.render → `_pending_set.add` and `discard` | `add_done_callback` + try/finally `discard` — double-cleanup is idempotent. **Correct.** | ✓ |
| `_cleanup_stale_artefacts` boot → live `_artefacts` | Cleanup runs BEFORE subsystem construction; no race. **Correct.** | ✓ |

---

## Spec-implementation drift findings

| Spec element | Implementation | Status |
|---|---|---|
| §2.4 default filename `f"{fmt}-{utc_iso}"` | `subsystem._default_filename` UTC + strftime `%Y-%m-%dT%H-%M-%SZ` (Windows-friendly `-` vs `:`) | ✓ matches spec rationale (LOW-3 stale comment) |
| §2.13 sweeper `delivered_at + ttl_s < now` (i.e. `now - delivered_at > ttl`) | `subsystem._sweep_iteration:307` `now - rec.delivered_at > ttl` | ✓ exact match |
| §2.6 step 5 PDF size cap POST-render unlink | `pdf_renderer.py:258-266` `if bytes_out > settings.pdf_max_bytes: unlink + raise` | ✓ unlinks orphan |
| §2.10 `render_doc_tool_visible=enabled AND NOT force_disabled` | `main.py:470-474` AND-NOT logic correct | ✓ |
| §2.12 SIGTERM 5s + SIGKILL 5s | `RenderDocSettings.pandoc_sigterm_grace_s=5.0`, `pandoc_sigkill_grace_s=5.0` | ✓ exact match |
| R1.2 `FatalURLFetchingError` extending BaseException | `pdf_renderer.py:81, 102` imports from `weasyprint.urls`, raises identically named class | ✓ class name verified in code |
| Pandoc variant `markdown-raw_html-raw_tex-raw_attribute-tex_math_dollars-tex_math_single_backslash-yaml_metadata_block` | `_PDF_MARKDOWN_VARIANT` and `_DOCX_MARKDOWN_VARIANT` use this exact string | ✓ |
| Dockerfile apt list `pandoc, libpango-1.0-0, libpangoft2-1.0-0, libharfbuzz-subset0, fonts-dejavu-core` | Dockerfile lines 134-140 match | ✓ |
| Dockerfile pandoc grep validates subtractions | grep regex MISSING `tex_math_single_backslash` | LOW-2 minor drift |
| AC#11 concurrency cap test | NOT IMPLEMENTED | W3-MED-3 |
| AC#12 timeout test | NOT IMPLEMENTED | W3-MED-4 |
| AC#19 multi-iteration ordering test | NOT IMPLEMENTED | W3-CRIT-1 untested |
| AC#21 Daemon.stop pandoc terminate test | NOT IMPLEMENTED | W3-MED-5 |
| AC#21a cumulative budget test | NOT IMPLEMENTED | W3-MED-5 |

---

## Failure-mode coverage gaps

| Failure mode | Coverage |
|---|---|
| WeasyPrint shared lib missing AT RUNTIME (apt failed silently) | startup_check catches via `import weasyprint` — ✓ |
| Pandoc subprocess hangs (pipe buffer full because `-o file` redirect doesn't drain stderr) | `wait_for(proc.communicate())` drains both pipes — ✓ |
| Disk full mid-render | `wb.save()` raises OSError → caught as `render_failed_internal` `openpyxl-error`. Audit STILL written (audit.py wraps OSError separately at subsystem.py:642-647). ✓ |
| aiogram `bot.send_document` throws `TelegramRetryAfter(30)` | NOT specifically handled. Falls into generic `except Exception` in `_flush_artefacts`, logs as "unknown" reason, emits text fallback. Owner sees no retry. **Consider extending the if-chain at handlers/message.py:1003-1009 to detect TelegramRetryAfter and surface as "rate-limited"**. |
| OOM during 5K-row XLSX + concurrent PDF | No load test. **Gap**. |
| TTL sweeper falls behind (interval=60s, ttl=600s, owner generates 1 doc/s for 10 min) | 600 docs in ledger, sweeper batches. Untested. |
| Crash mid-`mark_delivered` (handler dies between send_document success and mark_delivered call) | `mark_orphans_delivered_at_shutdown` covers next-boot case. But mid-run crash (exception in mark_delivered itself) leaves `in_flight=True` forever → file kept until next boot. With `cleanup_threshold_s=86400` (24h), orphan file lives ≤ 24h. Acceptable. |

---

## Final-gut-check — would I deploy this on Friday at 5pm?

**No.** Three reasons:

1. **W3-CRIT-1 is a spec-violating user-visible bug**. Owner will see PDFs arrive BEFORE the model's narrative explanation, every single time. It's cosmetic, not data-corrupting, but it's wrong-as-shipped and the spec explicitly promises the opposite. Ship-on-Friday means owner discovers it Saturday morning when they can't reach me. Either fix the order OR update spec/SKILL.md.

2. **W3-MED-3/4/5 zero-test gap on safety-critical paths**. The `_render_doc_pending` machinery, `tool_timeout_s` wait_for, and `pandoc_sigterm_grace_s/pandoc_sigkill_grace_s` are the entire response to CRIT-4 (orphan pandoc PID + staging files at SIGTERM). They have ZERO behavioural tests. If a refactor in phase 10 silently breaks one of them, we ship a daemon that leaks pandoc PIDs on every redeploy. Friday-deploy = Monday discovery + Tuesday-Wednesday hotfix.

3. **W3-CRIT-2 ordering bug** is latent but real. Order should be `drain render_doc → adapter.stop()`, not the reverse. Right now docker SIGTERM during a render produces aiogram-side `ClientSessionAlreadyClosed` traceback in logs + an undelivered artefact. Visible cost: one pretty-bad log line per shutdown-during-render. Probability low (single-owner). Fix is one-line reorder in main.py.

**Deploy-Monday recommendation**: 
- Fix W3-CRIT-1 (either change emit-flush semantics, or update spec acknowledging actual order).
- Reorder W3-CRIT-2 (1-line change).
- Add ONE behavioural test each for AC#12 and AC#21 (covers the SIGTERM/timeout machinery — most ROI).
- Fix W3-HIGH-2 audit rotation second-collision (5-line change: append microseconds to stamp).

Skip the rest until phase 10 / dedicated cleanup. None are blocking once deployed; the daemon will work.

---

## Top-3 fix-pack priorities (BEFORE deploy)

1. **Fix W3-CRIT-1 ordering bug**. Pick mitigation:
   - **Option A (preferred)**: in `_flush_artefacts`, before the per-artefact loop, drain any pending `chunks` accumulator via a new `flush_text_now` callback plumbed from the adapter through the handler. Then `send_document` per artefact. After flush, `chunks.clear()`. Gives owner-visible text → doc → text → doc.
   - **Option B (lazy)**: update spec §2.5 + AC#19 + SKILL.md to say "all text fires AFTER all artefacts in a turn". Document explicitly.
   - **Option C**: skip multi-iteration concern entirely (most owner usage is single-iteration). Acknowledge in spec residual risk; revisit if owner complains.

2. **Reorder Daemon.stop drain**. Move `_render_doc_pending` drain (main.py:1089-1118) BEFORE `await self._adapter.stop()` (line 1044). Mirror phase-8 vault_sync which drains after adapter is fine because vault doesn't depend on adapter; render_doc DOES depend on adapter for `send_document` + the handler-side `_flush_artefacts`. Add a single test that exercises this ordering with a mocked render that tries `send_document` after adapter.stop and asserts no `ClientSessionAlreadyClosed`.

3. **Add ONE behavioural test for SIGTERM termination** (covers W3-MED-5 + AC#21). Mock `asyncio.create_subprocess_exec` to return a process that ignores SIGTERM (simulates real misbehaving pandoc). Trigger `Daemon.stop`. Assert: SIGTERM sent within 5s of `_render_doc_pending` drain start, SIGKILL fallback at 10s, audit row written for cancelled render. This single test exercises the entire `_subprocess.run_pandoc` cancellation machinery + drain budget. ROI is highest among test-gap fixes.

Lower-priority fixes (deploy-Monday acceptable):
- Fix W3-HIGH-2 audit rotation collision (microsecond stamp).
- Tighten Dockerfile pandoc grep to include `tex_math_single_backslash` (W3-LOW-2).
- Drop the dead synthetic `tool_use_id` synthesis (W3-LOW-1).
- Rename `test_both_missing_fully_force_disabled` (W3-MED-2).
- Add log line "sweeper not spawned because force_disabled" (W3-MED-1).

---

## Files referenced (absolute paths)

- `/Users/agent2/Documents/0xone-assistant/src/assistant/main.py` (Daemon.stop ordering, lines 1043-1125)
- `/Users/agent2/Documents/0xone-assistant/src/assistant/adapters/telegram.py` (chunks-emit pattern, lines 295-310)
- `/Users/agent2/Documents/0xone-assistant/src/assistant/handlers/message.py` (`_flush_artefacts`, lines 946-1026; emit accumulation, line 822)
- `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/claude.py` (ArtefactBlock yield, lines 540-555)
- `/Users/agent2/Documents/0xone-assistant/src/assistant/render_doc/subsystem.py` (drain, lock, default filename)
- `/Users/agent2/Documents/0xone-assistant/src/assistant/render_doc/audit.py` (rotation race, lines 109-126)
- `/Users/agent2/Documents/0xone-assistant/src/assistant/render_doc/pdf_renderer.py` (post-render cap unlink, lines 258-266)
- `/Users/agent2/Documents/0xone-assistant/src/assistant/tools_sdk/render_doc.py` (envelope spread, synthetic tool_use_id, lines 56-63, 191-193)
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/Dockerfile` (pandoc grep, lines 159-160)
- `/Users/agent2/Documents/0xone-assistant/plan/phase9/description.md` (spec §2.4, §2.5 AC#19, §2.6 step 5, §2.10, §2.12, §2.13)
- `/Users/agent2/Documents/0xone-assistant/tests/test_phase9_*.py` (test gap audit)
