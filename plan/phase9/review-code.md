# Phase 9 — Code Review

Reviewer: code-quality + low-level security lens.
Scope: working-tree changes for `render_doc` subsystem + @tool wrapper +
bridge / handler / adapter / config / Dockerfile additions and the 16
`tests/test_phase9_*.py` files.

## Verdict

**fix-pack required** (no architecture rethink, no full-blown security
hole, but several non-trivial defense-in-depth gaps and a couple of
correctness bugs that will bite in production).

The implementation mirrors phase-8 `vault_sync` faithfully and the spec
v3 invariants (CRIT-1 ordering, CRIT-3 in-flight skip, CRIT-4 staging
cleanup, CRIT-5 sanitiser) are honoured. Subprocess hygiene (env
whitelist, argv-form, SIGTERM/SIGKILL grace) is correct. No
`os.environ.update`, no `shell=True`, no bare `except:`, no `eval`/`exec`
calls anywhere in the package. Type hygiene is clean — the only `Any`
types are at the WeasyPrint untyped-import boundary and the audit-row
`dict[str, Any]` payload, both justified.

The fix-pack list below is short and surgical; ship-readiness is
gated on at least the four CRITICAL items.

---

## Findings

### CRITICAL

#### CR-1 — `_flush_artefacts` does not validate `art.path` is under `artefact_dir`

**Where:** `src/assistant/handlers/message.py:946-1025`.

**Problem:** The handler trusts `ArtefactBlock.path` verbatim and passes
it to `MessengerAdapter.send_document`, which on the Telegram side
opens the file via `FSInputFile(path, ...)` (`src/assistant/adapters/telegram.py:105`).
The path string ultimately comes from the @tool envelope's `path`
field, which the bridge parser (`bridge/claude.py:_parse_render_doc_artefact_block`)
reads from `envelope["path"]` without any prefix check. A misbehaving
or compromised @tool body — or a future regression that lets a different
MCP tool emit the same envelope shape under a tool_use_id that
collides — could therefore exfiltrate any file the daemon process can
read (e.g. `/etc/passwd`, `~/.claude/.credentials.json`, the
phase-4 `memory-index.db`).

The phase-6a attachment-handling code uses exactly this defense
(`message.py:466`, `is_relative_to(uploads_root)`); render_doc lacks the
analog despite the same threat model.

**Fix:** Resolve the path and verify it sits under the configured
`artefact_dir` (with `.staging` excluded) before calling
`send_document`. If the check fails, log + drop + emit a Russian text
fallback. Keep the check in the handler (defense in depth) AND ideally
also in the bridge parser.

```python
# In _flush_artefacts, before send_document:
artefact_root = self._render_doc._artefact_dir.resolve()
try:
    resolved = art.path.resolve()
except OSError:
    log.warning("render_doc_path_resolve_failed", path=str(art.path))
    continue
if (
    not resolved.is_relative_to(artefact_root)
    or resolved.is_relative_to(artefact_root / ".staging")
):
    log.error("render_doc_path_escape_blocked", path=str(art.path))
    continue
```

---

#### CR-2 — pandoc subprocess timeout path can deadlock on full stderr pipe

**Where:** `src/assistant/render_doc/_subprocess.py:82-107`.

**Problem:** On `TimeoutError` / cancel, the code calls `proc.terminate()`
then `await proc.wait()`. Unlike `proc.communicate()`, `proc.wait()`
does NOT drain stdout/stderr pipes. If pandoc has filled the OS pipe
buffer (typically 64 KiB on Linux) with stderr output and is blocked
trying to write, the kernel won't deliver SIGTERM until the writer
unblocks — which it can't, because we stopped reading. The grace
window then expires and we send SIGKILL, which works, but the
"polite shutdown" branch is effectively dead. Worse, in the cancel
case, the inner `proc.wait()` itself can be cancelled before SIGKILL is
issued, leaking the child.

Pandoc's typical stderr is small, but a bad input (e.g. recursive include
errors, debug build) can spam stderr at >64 KiB.

**Fix:** Drain pipes during the grace window. Use `communicate()` with
a fresh timeout instead of `wait()`:

```python
try:
    await asyncio.wait_for(proc.communicate(), timeout=settings.pandoc_sigterm_grace_s)
except TimeoutError:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.communicate(), timeout=settings.pandoc_sigkill_grace_s)
```

Never call bare `proc.wait()` after `terminate()`/`kill()` when
PIPE was used.

---

#### CR-3 — Render task cancelled mid-flight after `register_artefact` leaks ledger row + on-disk file

**Where:** `src/assistant/render_doc/subsystem.py:425-440` + `_dispatch`
576-580 + `tools_sdk/render_doc.py:197-205`.

**Problem:** The @tool body wraps `sub.render(...)` in
`asyncio.wait_for(timeout=tool_timeout_s)`. There is a race window
between `register_artefact` (line 576) and the @tool body returning the
envelope: if the outer wait_for fires within that window, the inner
task is cancelled AFTER the ledger row was created with `in_flight=True`
and the file is on disk — but no `ArtefactBlock` will ever reach the
handler (timeout reply replaces the envelope). The TTL sweeper then
SKIPS the row forever (`in_flight=True` guard at `_sweep_iteration`,
line 305) until daemon stop, which only flips `in_flight=False` for
"orphans" via `mark_orphans_delivered_at_shutdown`.

Net effect: file lives on disk until daemon restart. Not a security
issue, but a real disk leak when timeouts cluster. Worse, on cancel
mid-`register_artefact`, the @tool body's `finally` discards
`task_handle` from `_pending_set` correctly but does NOT delete the
final file — only the `_dispatch` `except CancelledError` branch unlinks
it, and that branch fires before `register_artefact` runs.

**Fix:** Wrap dispatch+register in try/CancelledError in `render()`:

```python
async with self._render_sem:
    inflight_path = None
    try:
        result = await self._dispatch(...)
        if result.ok and result.path is not None:
            inflight_path = result.path
    except asyncio.CancelledError:
        if inflight_path is not None:
            with contextlib.suppress(OSError):
                inflight_path.unlink(missing_ok=True)
            async with self._artefacts_lock:
                self._artefacts.pop(inflight_path, None)
        raise
    finally:
        if task_handle is not None:
            self._pending_set.discard(task_handle)
```

---

#### CR-4 — `_parse_render_doc_artefact_block` accepts paths without validation

**Where:** `src/assistant/bridge/claude.py:160-224`.

**Problem:** Same as CR-1 from the upstream side. The bridge constructs
`ArtefactBlock(path=Path(envelope["path"]))` from whatever string the
envelope contains. The bridge already gates the parse on
`schema_version == 1`, `ok == True`, `kind == "artefact"`, but does
NOT validate the path is a non-empty filesystem path under the
configured artefact_dir, nor does it strip `..` segments. CR-1's
handler-level fix is necessary; doing it twice (bridge + handler) is
true defense in depth — both layers would have to fail for an escape
to work.

**Fix:** In `_parse_render_doc_artefact_block`, after extracting `path_str`,
reject if `Path(path_str)` is not absolute, contains `..`, or doesn't
match the daemon's configured artefact_dir. The bridge already imports
nothing from the daemon, but it can use the `RenderDocSubsystem`
reference attached via `_render_doc_core._CTX` to read the path.
Alternatively pass `artefact_dir` into `ClaudeBridge.__init__` alongside
`render_doc_tool_visible`.

---

### HIGH

#### CH-1 — `audit_log_keep_last_n` lacks lower-bound validator

**Where:** `src/assistant/config.py:427` + audit.py:121.

**Problem:** `audit_log_keep_last_n: int = 5` has no `>= 1` validator.
A `0` value silently disables retention but the slice
`siblings[keep_last_n:]` returns the full list, deleting every rotated
sibling — fine, but a `-1` value gives `siblings[-1:]`, deleting all
but the most recent ONE rotated file (regardless of `keep_last_n`'s
spirit). Negative input shouldn't reach this code path, but the
validator-mediated config surface elsewhere catches similar values.
Owner-set typo lands as silent data loss.

**Fix:** Add to `_validate_render_doc_consistency`:

```python
if self.audit_log_keep_last_n < 1:
    raise ValueError("audit_log_keep_last_n must be >= 1")
```

---

#### CH-2 — `proc.returncode or 0` masks signal-killed process exit

**Where:** `src/assistant/render_doc/_subprocess.py:108`.

**Problem:** `return (proc.returncode or 0, ...)` returns 0 when
`proc.returncode` is 0 (clean exit) AND when it's `None` (still running,
shouldn't happen here) AND when it's negative (signaled — e.g. -9 for
SIGKILL, -15 for SIGTERM). A pandoc subprocess killed via OOM (-9)
would surface to the caller as `rc == 0`, which the renderer interprets
as success and proceeds to read a non-existent staging HTML.

The `if not staging_html.exists()` guard at `pdf_renderer.py:206` would
catch this specific case, but only because we happen to use a separate
output file. For DOCX where `final_path` is the pandoc output, OOM
between stages could produce a corrupt/zero-byte DOCX that passes
`final_path.exists()` and returns to the caller as "success".

**Fix:**

```python
rc = proc.returncode
if rc is None:
    rc = -1
return (rc, stdout_b, stderr_b)
```

---

#### CH-3 — `_render_doc_core._CTX` typed `dict[str, object]` defeats static checks

**Where:** `src/assistant/tools_sdk/_render_doc_core.py:18,49-63`.

**Problem:** The pattern mirrors `_subagent_core` but is type-loose:
`_CTX: dict[str, object]` requires runtime `isinstance` re-checks at
every read. The phase-8 `_vault_core.py` uses the same shape, so this
is a project-wide pattern; the cost is minor unless the type-hygiene
goal is "no Any/object in API surface". The current implementation
correctly does the runtime check at `get_configured_subsystem`.

**Fix:** Optional. If pursuing strict hygiene, use a module-level
`_subsystem: RenderDocSubsystem | None = None` instead of a string-keyed
dict. Low priority — change the whole tools_sdk pattern at once or not
at all.

---

#### CH-4 — `notify_force_disabled_if_needed` swallows `BaseException` via `Exception`

**Where:** `src/assistant/render_doc/subsystem.py:678`.

**Problem:** `except (TimeoutError, Exception) as exc:` is redundant
(`TimeoutError` is an `Exception` subclass under modern Python). More
importantly, it catches every non-`KeyboardInterrupt`/`SystemExit`
exception, including programming errors. Phase-8's
`vault_sync.notify` has the same shape, so this matches precedent, but
the shape is sloppy.

**Fix:** Just `except Exception as exc:`. Optionally narrow to
`(TimeoutError, OSError, RuntimeError)`.

---

#### CH-5 — DOCX/XLSX renderers reuse `PDFRenderError` via type-aliasing

**Where:** `src/assistant/render_doc/docx_renderer.py:46`, `xlsx_renderer.py:41`.

**Problem:** `DOCXRenderError = _PDFRenderError` and `XLSXRenderError = _PDFRenderError`
mean these are the SAME class object. Catching `PDFRenderError` in the
subsystem dispatch (line 540) catches all three by design — works, but
the names lie. A reader of `xlsx_renderer.py` sees "raises
XLSXRenderError" and assumes a distinct exception hierarchy. The
inline comment ("rename in phase 10") acknowledges the debt.

**Fix:** Either rename now (`RenderError` in a shared module), or
make them real subclasses (`class DOCXRenderError(_PDFRenderError):
pass`) so the `except` clause still works but the names are honest.
The class-aliasing is worse than the rename because mypy still treats
them as the same type but humans don't.

---

#### CH-6 — Bridge "defensive log on collision" comment doesn't match code

**Where:** `src/assistant/bridge/claude.py:482-484`.

**Problem:** Comment claims "we defensively log on collision (last-write-wins)"
but the implementation is just `render_doc_tool_use_ids.add(block.id)`
on a `set` — silent on duplicates. The set semantics make the comment
false. Either the comment should be removed or a duplicate-id
detection branch added.

**Fix:** Drop the comment, or:

```python
if block.id in render_doc_tool_use_ids:
    log.warning("render_doc_tool_use_id_collision", id=block.id)
render_doc_tool_use_ids.add(block.id)
```

---

### MEDIUM

#### CM-1 — Audit `truncate_chars` truncates `ts` field too

**Where:** `src/assistant/render_doc/audit.py:49-61`.

**Problem:** `_truncate_str_fields` truncates EVERY str-typed value
unconditionally. `ts` is an ISO-8601 timestamp (~25 chars) and `format`
is a 3-4 char enum — both are below the 256-char cap, so the truncation
is harmless TODAY. But the contract "all str fields capped at 256
codepoints" is explicit in the docstring; if a future change widens the
cap to 1024 (or narrows to 32), `ts` becomes corruptible. Also, the
docstring says "Future str field additions inherit the cap automatically",
but the addition could be a structured-data field where truncation would
silently corrupt downstream parsers.

**Fix:** Either keep an explicit allowlist of fields that must NOT be
truncated (`ts`, `result`, `format`), or document that future fields
must use non-str types if they require full-fidelity preservation.

---

#### CM-2 — Audit rotation timestamp can collide silently within a 1-second window

**Where:** `src/assistant/render_doc/audit.py:115-118`.

**Problem:** Rotation stamp uses `%Y%m%d-%H%M%S` (1-second granularity).
If two rotations happen within the same second, `os.replace(path,
rotated)` overwrites the prior rotated file. Comment acknowledges
this ("audit volume is well under 1/s"), but a burst from a fix-pack
deploy that triggers many disabled-error rows can plausibly hit it.
The real consequence is one lost rotated archive — not catastrophic,
since the rotation already implies size-based pruning.

**Fix:** Append microseconds (`%Y%m%d-%H%M%S-%f`), or fall back to a
collision suffix:

```python
rotated = path.with_suffix(path.suffix + f".{stamp}")
i = 0
while rotated.exists():
    i += 1
    rotated = path.with_suffix(path.suffix + f".{stamp}-{i}")
```

---

#### CM-3 — Sweeper double-lock acquire across phases is correct but heavy

**Where:** `src/assistant/render_doc/subsystem.py:301-330`.

**Problem:** The sweeper acquires `_artefacts_lock` twice (snapshot phase
then deletion-update phase) with disk I/O between. This is the right
pattern (W2-HIGH-2), but each register/mark_delivered call between the
two phases creates a window where the deletion-update phase pops paths
that were re-registered in between. Currently mitigated by the
`if rec is not None and not rec.in_flight:` re-check (line 329). Good
defensive coding; just confirm the test suite covers the
"register-mid-sweep" race.

**Fix:** None required — current code is correct. Optionally add a test
that explicitly schedules `register_artefact` between the two lock
acquires.

---

#### CM-4 — `audit_log_keep_last_n` parameter passed positionally vs keyword

**Where:** `src/assistant/render_doc/audit.py:82` (signature) and
`subsystem.py:639-641`.

**Problem:** `write_audit_row(path, row, *, max_size_bytes, keep_last_n=5,
truncate_chars=DEFAULT_TRUNCATE_CHARS)` requires keyword-only — caller
correctly uses kwargs. But `vault_sync/audit.py:write_audit_row` has a
different signature (`path, row, max_size_bytes` positional). The
phase-8/9 audit functions share a name but diverge in shape — minor
maintainer-trap.

**Fix:** Either rename render_doc's variant to `write_audit_row_v2` or
align signatures. Or extract a shared helper. Documenting the
divergence in both modules' docstrings is the minimum.

---

#### CM-5 — `force_disabled_formats` is a public mutable `set`

**Where:** `src/assistant/render_doc/subsystem.py:148`.

**Problem:** `self.force_disabled_formats: set[str] = set()` is exposed as
a public attribute (no leading underscore) and mutated in-place by
`startup_check`. Tests use `.add()` on it directly (`test_phase9_subsystem_render.py:50`)
to simulate runtime force-disable. This works but breaks encapsulation —
runtime callers could mutate the set and corrupt state. The phase-8
parallel (`vault_sync.subsystem._force_disabled` flag) is private.

**Fix:** Either make it `frozenset` after `startup_check` returns, or
keep it `set` but document that only `startup_check` may mutate.
Test-only mutation can use a separate `_inject_force_disabled` helper.

---

#### CM-6 — `f"weasyprint-import-failed: {exc!s}"[:96]` truncates to 96 chars but error field cap is 256

**Where:** `src/assistant/render_doc/subsystem.py:557`.

**Problem:** Two different truncation caps in the same subsystem (`[:96]`
in `_dispatch`, `[:256]` in `pdf_renderer.py:254`, `[:256]` in audit).
The 96 is tighter than necessary; rationale isn't documented. Likely a
leftover from an earlier draft. The 256 is the spec'd cap (W2-MED-4).

**Fix:** Use the same cap (256) consistently, or extract a constant.

---

### LOW

#### CL-1 — `_PDF_MARKDOWN_VARIANT` and `_DOCX_MARKDOWN_VARIANT` are identical strings

**Where:** `pdf_renderer.py:47-55` and `docx_renderer.py:31-39`.

**Problem:** Two copies of the same 6-line string. Easy place to forget
to update one of them in a security fix-pack.

**Fix:** Extract to `_subprocess.py` or a `constants.py`:

```python
PANDOC_MARKDOWN_VARIANT = (
    "markdown-raw_html-raw_tex-raw_attribute"
    "-tex_math_dollars-tex_math_single_backslash"
    "-yaml_metadata_block"
)
```

---

#### CL-2 — `subsystem.py` imports `asyncio` inside function body

**Where:** `src/assistant/render_doc/pdf_renderer.py:173`, `xlsx_renderer.py:53`.

**Problem:** `import asyncio` inside `render_pdf` and `render_xlsx`. The
module already needs asyncio. The local import is a small idiomatic
oddity — unnecessary, no harm.

**Fix:** Move to module top.

---

#### CL-3 — `markdown_tables.parse` accepts header rows where every cell is whitespace-only

**Where:** `src/assistant/render_doc/markdown_tables.py:135`.

**Problem:** `if not header_cells or all(not c for c in header_cells):
i += 1; continue` — but `_split_pipes` strips whitespace, so all
whitespace-only cells become empty strings; line skipped. OK. But what
about `| | foo |` with one empty header cell? Currently accepted as
header `["", "foo"]`. The XLSX renderer would emit a blank header
column. Not invalid but unusual.

**Fix:** Optional — reject pipe-rows with any empty header cell:

```python
if any(not c for c in header_cells):
    i += 1
    continue
```

Or document the current behavior in spec §3 Wave B B1.

---

#### CL-4 — `render_doc.py` collapses two distinct error states into one envelope

**Where:** `src/assistant/tools_sdk/render_doc.py:142-149`.

**Problem:** `sub = get_configured_subsystem(); if sub is None or
sub.force_disabled: return ...`. Combines two distinct error states
("not configured" vs "force_disabled") into a single envelope with
identical reason+error strings. The audit log entry will be the same
in both cases, so post-hoc forensics can't distinguish a startup_check
failure from a missing-configure call.

**Fix:** Distinguish reasons:

```python
if sub is None:
    return _envelope(_fail_envelope(reason="disabled", error="subsystem-not-configured"))
if sub.force_disabled:
    return _envelope(_fail_envelope(reason="disabled",
        error=f"subsystem-force-disabled-{sub.disabled_reason or 'unknown'}"))
```

---

#### CL-5 — `_pandoc_env`'s `HOME` fallback to `/tmp` may break pandoc's user-data lookups

**Where:** `src/assistant/render_doc/_subprocess.py:57`.

**Problem:** `"HOME": os.environ.get("HOME", "/tmp")` — pandoc reads
`$HOME/.pandoc/` for user data files. If `HOME` is unset (rare, e.g.
in some container init contexts), `/tmp` is used. Pandoc would silently
miss user-configured templates/filters. Acceptable since the daemon
runs with a real `HOME`, but documenting the behaviour would help.

**Fix:** Document the fallback in `_pandoc_env`'s docstring.

---

#### CL-6 — Tests use private attribute access (`sub._artefacts`, `sub._settings`, `sub._force_disabled`)

**Where:** `tests/test_phase9_artefact_ledger.py:45,61,77`,
`test_phase9_subsystem_render.py:67,68,71`,
`test_phase9_subsystem_force_disable.py:31,46`.

**Problem:** Tests reach into the subsystem's private state freely.
Refactoring the internal representation will require updating ~20 test
sites. Not a bug, but a maintenance friction.

**Fix:** Optional — add probe methods (`get_artefact_record`,
`is_in_flight`) and use those in tests.

---

#### CL-7 — `lock contention test` exercises the lock but doesn't reliably create contention

**Where:** `tests/test_phase9_artefact_ledger.py:84-118`.

**Problem:** The "concurrent_mutations_no_dict_size_error" test gathers
21 tasks but `asyncio.gather` runs them as fast as the event loop can,
which on a single-threaded asyncio loop means strict serialization. The
sweeper's two-phase lock pattern is exercised, but a true contention
window (one task suspended on `await self._artefacts_lock` while
another holds it) requires explicit yield points or a real `to_thread`
sweeper. Test passes today because the lock isn't strictly necessary
for this access pattern; the test's value is bounded.

**Fix:** Inject explicit `await asyncio.sleep(0)` inside the sweeper's
critical section under test, or use `pytest-asyncio`'s `--asyncio-mode=auto`
+ `gather_in_taskgroup` pattern to actually interleave.

---

## Spot-checks performed

- **Pattern compliance vs phase-8 `vault_sync/`** — `__init__.py`,
  `boot.py`, `audit.py`, `subsystem.py`, `_validate_paths.py` all match
  phase-8 layout. Divergence: `audit.py` adds date-stamped rotation +
  `keep_last_n` + str-field truncation (justified by spec §2.2 / W2-MED-4).
- **`mypy --strict` cleanliness (visual)** — only `Any` types are at
  WeasyPrint cffi boundary (`pdf_renderer._build_safe_url_fetcher`) +
  audit row payload — both justified.
- **Subprocess hygiene** — `_subprocess.run_pandoc` uses
  `asyncio.create_subprocess_exec(*argv, env=env, ...)` (argv-form,
  scoped env). NEVER `os.environ.update`. Pipes captured. Test
  `test_phase9_pandoc_env_minimal.py` verifies whitelist excludes
  `TELEGRAM_BOT_TOKEN`, `GH_TOKEN`, `ANTHROPIC_API_KEY`.
- **WeasyPrint URLFetcher** — uses `URLFetcher` subclass form with
  `def fetch(self, url, headers=None)` signature, raises
  `FatalURLFetchingError` (extends `BaseException`). Test
  `test_phase9_render_doc_binaries.py::test_weasyprint_url_fetcher_hierarchy`
  verifies `issubclass(FatalURLFetchingError, BaseException) and not
  issubclass(_, IOError)`.
- **Filename sanitisation** — sanitiser implements all 14 spec §2.4
  matrix rows. Cyrillic + emoji accepted. ZWSP/U+202E silently stripped.
  Trailing dot, dots-only, Windows-reserved, leading dot, `/`/`\\`/`\\0`,
  >96 codepoints all rejected with the right `code`. Test file is the
  matrix verbatim.
- **Audit log truncation + rotation** — `_truncate_str_fields` caps every
  str value at 256 codepoints. Date-stamped rotation triggers at
  `max_size_bytes`. `keep_last_n` prunes older siblings. Tests cover
  all four behaviors.
- **Concurrency** — `_artefacts_lock` acquired in `register_artefact`,
  `mark_delivered`, `_sweep_iteration` (twice — snapshot + delete-update).
  Sweeper iterates snapshot under lock, deletes outside lock, re-acquires
  to pop. Correct pattern.
- **Bridge ArtefactBlock yield** — gated by `block.tool_use_id in
  render_doc_tool_use_ids`. Other ToolResultBlocks pass through unchanged.
  Schema-version=1 enforced; `ok=False` blocks skipped.
- **Handler flush ordering** — drains `pending_artefacts` on EVERY
  `ResultMessage` AND on async-for normal exit. Multi-iteration ordering
  is `text-1 -> flush(art-1) -> text-2 -> flush(art-2)`.
- **`MessengerAdapter.send_document`** — base class default raises
  `NotImplementedError` (W2-CRIT-1 Option A). `TelegramAdapter.send_document`
  uses `FSInputFile(path, filename=suggested_filename)` with kw-args.
  Size check + missing-file check correct.
- **Daemon lifecycle** — `_render_doc_pending` drains BEFORE `_bg_tasks`
  cancel via `asyncio.wait(timeout=render_drain_timeout_s)`. Mirror of
  phase-8 `_audio_persist_pending`. `mark_orphans_delivered_at_shutdown`
  flips `in_flight=False` on remaining records (CR-3 still leaks).
- **Force-disable** — `startup_check` covers `shutil.which("pandoc") is
  None` AND `import weasyprint` (try `(ImportError, OSError)`). Partial
  force-disable preserves XLSX. Settings-disabled path also covered.
- **Test fidelity** — most tests genuinely exercise their invariants.
  CL-7 noted the lock-contention test is weaker than its docstring
  claims.

---

## Top-3 fix-pack priorities

1. **CR-1 + CR-4 — path validation defense in depth.** Highest impact
   on the security posture, cheapest change. Add `is_relative_to(artefact_root)`
   check in BOTH the bridge parser and the handler flush. Without this,
   a single bug in the @tool body or the SDK envelope shape becomes a
   file-exfil vector.

2. **CR-3 — leak the ledger row + on-disk file on timeout-cancel.**
   Currently a real disk leak that grows with timeout frequency. Wrap
   `register_artefact` in a `CancelledError` handler that pops the row
   and unlinks the file. Without this fix, every timeout leaves a
   stale ledger row + on-disk file until daemon restart.

3. **CR-2 — pandoc subprocess pipe-drain on terminate.** The current
   timeout path is technically correct only when stderr is small.
   Replace `proc.wait()` with `proc.communicate()` (with a fresh
   timeout) so pipe buffers drain. Without this fix, a pandoc
   subprocess emitting >64 KiB stderr on bad input deadlocks the
   SIGTERM grace path and depends on SIGKILL — which works, but
   defeats the point of the grace window.
