# Phase 5b Code Review

## Executive summary

Phase 5b is a structurally solid implementation of a cron-driven autonomous
scheduler: the DST/leap-day semantics, prompt-injection triple-layer defence,
boot classification, at-least-once materialisation with UNIQUE dedup, and
per-chat handler lock are all correctly engineered and well-documented.
However, there is a **latent double-fire race** in `SchedulerLoop._tick_once`
where `mark_sent(trig_id)` can clobber a terminal (`acked`/`dropped`/`dead`)
state written by the dispatcher that picked up the trigger in the window
between `put_nowait` and the loop's `await mark_sent`. There are also two
non-trivial correctness issues: `note_queue_saturation` overwrites
`last_error` without a `status=='pending'` guard (could stomp a terminal
state), and `reclaim_pending_not_queued` can re-queue a freshly materialised
trigger from the SAME tick if scheduled_for is >30s behind `now` (common on
catchup fires). Verdict: **fix before owner smoke** — race is real but
probabilistic under owner's 1-3 schedules/day workload; still a ship-blocker
for a 15s tick with overlapping dispatcher work.

**Top 3 risks**: (1) CR-1a race: `mark_sent` unconditional UPDATE can resurrect
a terminal row → next tick's sweep reverts to pending → reclaim → double-fire;
(2) Catchup-induced double-fire: reclaim picks up rows with `scheduled_for`
>30s behind even when loop *just* materialised them this tick during a
catchup walk; (3) Dispatcher `get_schedule_history(limit=200)` per-trigger
DB fetch whose result is then `del`'d — dead code with O(history) cost
per dispatch.

---

## CRITICAL (block ship)

### C-1 | `src/assistant/scheduler/loop.py:189` + `store.py:221` | `mark_sent` races terminal dispatcher states → double-fire

**Issue**: After `put_nowait(trig)` succeeds, loop does
`await self._store.mark_sent(trig_id)`. The `await` yields to the event loop;
the dispatcher — which was blocked on `q.get()` — is now runnable. Under
asyncio's FIFO scheduling, dispatcher task may enter `_process`, race to
`mark_dropped`/`mark_acked`/`mark_dead` on the `_tx_lock`, and complete
FIRST. Then loop's `mark_sent` acquires the lock and unconditionally
writes `status='sent'` — resurrecting a terminal row.

Concretely (store.py:221-229):
```python
await self._conn.execute(
    "UPDATE triggers SET status='sent', sent_at=... WHERE id=?",
    (trigger_id,),
)
```
There is no `AND status='pending'` guard.

Next-tick `sweep_expired_sent` eventually flips this back to `pending`
(attempts bumped); `reclaim_pending_not_queued` re-enqueues; dispatcher
fires the same schedule twice.

**Why it's a bug**: the at-least-once contract relies on UNIQUE(schedule_id,
scheduled_for) to deduplicate materialisation, but state transitions
AFTER materialisation are racy. This breaks the core invariant at
plan §B: "never double-deliver a (schedule, minute) tuple."

**Fix**: Make `mark_sent` idempotent w.r.t. terminal states — guard on
current status:
```python
await self._conn.execute(
    "UPDATE triggers SET status='sent', "
    "sent_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
    "WHERE id=? AND status='pending'",
    (trigger_id,),
)
```
Same guard is appropriate on `sweep_expired_sent` (already has
`status='sent'`, so that one is fine).

Apply the equivalent guard to `mark_acked`/`mark_dropped`/`mark_dead`
too — belt-and-braces against an out-of-order tick.

### C-2 | `src/assistant/scheduler/loop.py:192-208` | Orphan reclaim can re-queue this-tick triggers during catchup walks

**Issue**: `reclaim_pending_not_queued(self._inflight, older_than_s=30)` SQL
filter `julianday('now') - julianday(scheduled_for) > 30/86400.0`. During
catchup (`is_due` walks from `last_fire_at + 1min` forward), materialised
triggers have `scheduled_for` many minutes behind `now`. The loop adds
the id to `self._inflight` BEFORE `put_nowait` (line 170), so the
inflight filter in the reclaim skips it IF the set membership persists
across the same tick. But consider: if the tick materialised TWO triggers
for the same schedule via catchup in one scan, the first's `mark_sent`
runs before the reclaim step. If that `mark_sent` failed transiently
(e.g., SQLite busy timeout → raises) the row stays `pending` → reclaim
picks it up. Also, the `inflight` membership is purely in-memory — if
the dispatcher resets it (see next finding), reclaim will pick up a
row that's being actively processed.

Related: `ScheduledTrigger.scheduled_for_utc` for orphans uses
`str(o["scheduled_for"])` which is DB-serialised ISO-Z — this differs
from the fresh path which uses `due.strftime(...)`. Both produce
`YYYY-MM-DDTHH:MM:SSZ`, so equivalent. No bug there.

**Why it's a bug**: the `older_than_s=30` hardcode + catchup scenario
means the reclaim window overlaps the materialisation window on any
catchup fire. Combined with C-1, this is a realistic double-fire path.

**Fix**: filter reclaim SQL by `sent_at IS NULL AND last_error LIKE
'queue saturated%'` so it only picks up verifiable saturations, OR
require `older_than_s >= tick_interval_s + 15` and pass the settings
value explicitly (not a hardcoded 30). Also change the reclaim filter
from `scheduled_for` to `created_at` (the materialisation time) — plan
§H2.3 was arguing the other direction, but the rationale (note-queue
saturation retries) still works with `created_at` since saturation
records `note_queue_saturation` right after materialisation.

### C-3 | `src/assistant/scheduler/store.py:277-290` | `note_queue_saturation` writes `last_error` without any status guard

**Issue**: same-shape bug as C-1 in the opposite direction:
```python
await self._conn.execute(
    "UPDATE triggers SET last_error=? WHERE id=?",
    (last_error[:500], trigger_id),
)
```
If the dispatcher (on a prior attempt) already marked the row `acked`
or `dead`, this stamps a misleading "queue saturated" string onto a
terminal row. Not a state-machine violation (status untouched), but a
debuggability corruption.

**Fix**: `WHERE id=? AND status='pending'`.

---

## HIGH (fix before owner smoke)

### H-1 | `src/assistant/scheduler/dispatcher.py:116-128` | Dead-code `get_schedule_history(limit=200)` on every dispatch

**Issue**:
```python
history = await self._store.get_schedule_history(
    schedule_id=None, limit=200
)
trigger_row = next(
    (r for r in history if r["id"] == trig.trigger_id), None
)
...
del trigger_row  # reserved for future expansion
```
The 200-row fetch+scan is **unused** — the result is assigned then
deleted. On a steady-state bot this is negligible, but it adds an
avoidable DB round-trip per trigger, and the comment "reserved for
future expansion" does not justify a live query. This is YAGNI and
will confuse the next reader.

**Fix**: delete the entire block (history/trigger_row/del) and the
preceding comment. The CR2.2 invariant (prompt from `triggers.prompt`)
is already satisfied by `trig.prompt` carrying the snapshot — add an
assertion or clarifying comment instead.

### H-2 | `src/assistant/scheduler/loop.py:170-189` | `inflight.add` happens before the work is committed

**Issue**: `self._inflight.add(trig_id)` runs BEFORE `put_nowait` — if
put_nowait raises `QueueFull`, the code `discard`s. But between `add`
and `put_nowait` there's no yield, so this is atomic from asyncio's
POV. However, the `inflight` set is shared mutable state with the
dispatcher task (`SchedulerDispatcher.inflight`). Dispatcher's
`_process` discards on `finally` (dispatcher.py:91). If a dispatcher
is processing trigger X while loop's `_tick_once` is scheduling
trigger Y with the same id (impossible in practice since ids are
unique), no issue. The real hazard: after dispatcher.inflight.discard,
if the loop's reclaim re-queues the same row (because it's still in
`pending` for some reason), no double-dispatch protection exists. The
dispatcher's 256-slot LRU helps, but only within a single process —
a crash+restart re-seeds the LRU from scratch.

**Fix**: extend the LRU to include post-crash protection using a
`(trigger_id, scheduled_for)` key and persist recently-processed
IDs to disk, OR make the dispatcher's `mark_acked`/`mark_dropped`
a precondition check that refuses to process already-terminal rows.

### H-3 | `src/assistant/scheduler/dispatcher.py:82-91` | `run()` uses `asyncio.wait_for(... timeout=0.5)` busy-poll instead of shutdown-aware blocking

**Issue**:
```python
while not self._stop.is_set():
    try:
        trig = await asyncio.wait_for(self._q.get(), timeout=0.5)
    except TimeoutError:
        continue
```
This wakes every 500ms purely to check `_stop`. On an idle system
(most of the time) this is wasteful CPU + wakeups that drain battery
on a workstation-deployed bot. Cleaner: race `q.get()` against
`_stop.wait()` with `asyncio.wait(..., return_when=FIRST_COMPLETED)`.

**Fix**:
```python
get_task = asyncio.create_task(self._q.get())
stop_task = asyncio.create_task(self._stop.wait())
done, pending = await asyncio.wait(
    {get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
)
for t in pending:
    t.cancel()
if stop_task in done:
    return
trig = get_task.result()
```

### H-4 | `src/assistant/scheduler/cron.py:240-247` + `281-291` | Minute-by-minute walk is O(n); catchup over 1h window is 60 iterations per tick per schedule

**Issue**: `next_fire` and `is_due` advance cursor by 1 minute in a
while-loop. For a catchup window of 3600s after a multi-hour daemon
sleep, this walks ~60-360 iterations per schedule per tick. With
64 schedules × 60 iterations = 3840 `astimezone` + `_matches` calls
per tick, every 15s. Python 3 can do this in ~10-30ms, but on a
sleep/resume recovery (3-day absence) with `max_lookahead_days=1500`
the worst case is 2.16M iterations per schedule for `next_fire`.
`next_fire` isn't on the hot path (called only at add time for
preview) — acceptable. `is_due` IS on the hot path — fine in normal
operation but can cause a 10s+ stall on boot after a long sleep.

**Fix**: not urgent. Note as a known limitation in
`plan/phase5/spike-findings-v2.md`. A tighter algorithm would be a
field-by-field next-minute-satisfying walk, but that's out of scope
for phase 5b.

### H-5 | `src/assistant/scheduler/store.py:292-322` | Reclaim SQL doesn't serialise with `_tx_lock`

**Issue**: `reclaim_pending_not_queued` is the only state-reading
method that doesn't take `_tx_lock`. That's technically fine for a
SELECT, but the returned rows may have been mutated by a concurrent
writer between SELECT and the Python-side `inflight` filter. A row
that just flipped to `sent` after the SELECT snapshot would still
appear in the returned list. The downstream loop then tries to
`mark_sent` on a row that's already `sent` — with C-1 fix applied
(status guard), this becomes a no-op. Without C-1 fix, it's a
correctness gap.

**Fix**: couple with C-1. No standalone action needed after C-1.

### H-6 | `src/assistant/scheduler/store.py:463-484` | `_classify_boot_sync` swallows `OSError` on mtime read without logging

**Issue**:
```python
try:
    mtime = marker_path.stat().st_mtime
    ...
except OSError:
    try:
        ...json fallback...
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return "first-boot"
```
The outer `except OSError` is unlogged — on a corrupted filesystem
where `stat` fails, the owner gets no diagnostic and the bot
silently behaves as first-boot. M2.6 was supposed to guard against
"ancient leftover marker" but this branch is the one where stat
fails unexpectedly — exactly the case worth logging.

**Fix**: log a warning in both OSError branches with marker_path +
repr(exc); the classification decision itself stays the same.

---

## MEDIUM (open issue)

### M-1 | `src/assistant/tools_sdk/_scheduler_core.py:42-46` | Sentinel regex doesn't cover memory's untrusted-note-body-NONCE literal

**Issue**: `_SENTINEL_TAG_RE` matches
`<(/?)\s*(scheduler-prompt|untrusted-(note-body|note-snippet|scheduler-prompt))`
but the memory server wraps content in
`<untrusted-note-body-NONCE>...` with the NONCE directly
concatenated. A prompt containing the LITERAL string
`<untrusted-note-body-abc123>` is rejected (good). But a prompt
containing `<untrusted-note-body>` without NONCE would still be
rejected (since the regex doesn't require the NONCE). Actually
the regex is fine; the concern is just that any future sentinel
family (e.g., phase 8 `<github-clone-result-NONCE>`) will need
to be added here. Document this as a checklist item.

**Fix**: add a comment near `_SENTINEL_TAG_RE` listing all
sentinel-tag families project-wide and pointing to a single
source of truth.

### M-2 | `src/assistant/tools_sdk/_scheduler_core.py:121-153` | `wrap_scheduler_prompt` only scrubs `scheduler-prompt` fragments, not `untrusted-*`

**Issue**: The scrub regex (line 137):
```python
r"(<)(/?)(scheduler-prompt[-0-9a-fA-F]*)"
```
handles only `scheduler-prompt`. Per docstring ("scrub any literal
sentinel tags that slipped in at write time"), it should also cover
`untrusted-note-body`, `untrusted-note-snippet`, and any other
sentinel fragment the wrapper regex recognises. Since
`validate_cron_prompt` rejects `untrusted-*` at write time, this is
defence-in-depth redundancy; still, defence-in-depth should be
complete.

**Fix**: extend the scrub regex to mirror `_SENTINEL_TAG_RE`:
```python
r"(<)(/?)(scheduler-prompt[-0-9a-fA-F]*|untrusted-(?:note-body|note-snippet|scheduler-prompt)[-0-9a-fA-F]*)"
```

### M-3 | `src/assistant/scheduler/loop.py:109-113` | Per-tick outer try/except catches `Exception` and logs at WARNING

**Issue**:
```python
try:
    await self._tick_once()
except Exception:
    _log.exception("scheduler_loop_tick_error")
await self._clock.sleep(float(tick))
```
A systemic failure (DB locked for 30s, corrupted schedules row)
will loop every 15s emitting ERROR logs indefinitely. With the
supervised-spawn supervisor in main.py (max_respawn_per_hour=3),
this loop KEEPS running without counting against the supervisor
budget because the supervisor only sees coroutine termination,
not per-tick errors.

**Fix**: if `_tick_once` raises 5 times in a row, `raise` to let
the supervisor give up + notify. Use a rolling counter pattern.

### M-4 | `src/assistant/scheduler/dispatcher.py:117-121` | LRU dedup uses O(1) OrderedDict, but memory is unbounded only by `popitem` guard

**Issue**: `self._lru[trigger_id] = None; if len > 256: popitem(last=False)`.
On a stream of unique trigger_ids (normal case), LRU stays at 256.
On a bug path that calls `_process` with the SAME trigger_id many
times (e.g., a retry storm), `move_to_end` keeps it alive forever —
fine, that's the LRU semantics. No actual issue, just noting that
the LRU never shrinks below 256 once populated.

**Fix**: none. Note.

### M-5 | `src/assistant/tools_sdk/scheduler.py:185-192` | `schedule_add` recomputes `parse_cron` THREE TIMES on happy path

**Issue**:
1. Line 155: `expr = parse_cron(cron_raw)` — validation
2. Line 185: `del expr` + the comment says "preview re-parses once more"
3. Line 186: `fetch_next_fire_preview` calls `parse_cron` internally
4. Line 173: `store.add_schedule` inserts the cron string — SQL does
   NOT re-parse, but the loop's `_tick_once` will call `parse_cron`
   again on every tick.

So the add-time parses are 2x (once for validation, once for preview).
Minor perf waste, but confusing: why `del expr`? Keep it and pass
`expr` into `fetch_next_fire_preview` to save one parse.

**Fix**:
```python
try:
    expr = parse_cron(cron_raw)
except CronParseError as exc:
    return core.tool_error(str(exc), CODE_CRON_PARSE)
...
next_fire_utc = core.fetch_next_fire_preview_from_expr(
    expr, tz_obj, dt.datetime.now(dt.UTC)
)
```
Add a new helper that takes a pre-parsed `CronExpr`.

### M-6 | `src/assistant/handlers/message.py:149-166` | `_chat_locks` dict is never pruned on chat removal

**Issue**: Single-owner bot → 1 entry ever. Documented in-line (line
148-150). Not a bug for phase 5; flag for phase 8+ multi-chat.

**Fix**: Track as a known limitation. No action for phase 5.

### M-7 | `src/assistant/main.py:303-309` | `_spawn_bg_supervised(factory)` discards the task returned by `factory()` — race on very-fast failure

**Issue**: In the supervisor:
```python
task: asyncio.Task[None] = asyncio.create_task(factory())
try:
    await task
```
If `factory()` itself raises synchronously (should never happen for
bound methods), the `create_task` wouldn't catch it. In practice
`SchedulerDispatcher.run` / `SchedulerLoop.run` always return
coroutines, so synchronous-raise is impossible.

**Fix**: none strictly needed, but add a defensive `try/except` in
the supervisor around `create_task` for belt-and-braces.

### M-8 | `src/assistant/scheduler/cron.py:160-179` | `is_existing_local_minute` uses `fold=0` only — misses the rare "both folds differ from the naked" case

**Issue**: Minor. The function compares the fold=0 round-trip to the
naked input. If the tz has an exotic transition where fold=0 maps to
a different wall clock but fold=1 maps back to the same naked, the
function incorrectly reports non-existence. In practice, Python's
zoneinfo + IANA DST data doesn't produce such cases for real zones.
Documented behaviour is correct for the 2 main DST cases (spring
skip, fall fold).

**Fix**: none; behaviour matches RQ3 spec.

### M-9 | `src/assistant/tools_sdk/_scheduler_core.py:71-104` | `validate_cron_prompt` checks control-chars on original string, not `stripped`

**Issue**: `_CTRL_CHAR_RE.search(prompt)` — if `prompt = "   \x00hi"`,
`stripped = "\x00hi"` (strip removes trailing/leading whitespace,
not ctrl chars). The ctrl-check runs on `prompt` (original), catches
it. Correct. But the UTF-8 byte cap also runs on the original
(`prompt.encode`), which is technically inconsistent with returning
the ORIGINAL prompt (not stripped) on success. Minor: callers that
rely on "prompt as stored" vs "prompt as entered" may see trailing
whitespace preserved. Not a bug.

**Fix**: none; document the contract explicitly in the docstring.

---

## LOW (style / polish)

### L-1 | `src/assistant/scheduler/store.py:487-511` | `write_clean_exit_marker` uses `__import__("os")` instead of top-level `os` import

`os` is not imported at the module top. Fix: add `import os` at top;
drop `__import__` calls.

### L-2 | `src/assistant/scheduler/dispatcher.py:128` | `del trigger_row` produces a pylint-unused-variable warning masker

Reads as "I know this is unused, shut up linter". See H-1 above.

### L-3 | `src/assistant/scheduler/loop.py:148-155` | Redundant nested try/except for `fromisoformat`

A simpler helper `_parse_iso_z` already exists in `store.py` — import
and reuse instead of inlining the replace+fromisoformat logic.

### L-4 | `src/assistant/tools_sdk/scheduler.py:147-203` | `schedule_add` is 57 LOC — on the edge of the CLAUDE.md 30-40 LOC suspicion threshold

Consider extracting the validation chain into
`_scheduler_core.validate_schedule_add(args, settings)` returning a
`(cron, prompt, tz, error_dict | None)` tuple so the `@tool` handler
body is a thin driver.

### L-5 | `src/assistant/scheduler/store.py:324-354` | `sweep_expired_sent` has `julianday` subtraction that's not TZ-robust

SQLite `julianday('now')` returns UTC, `julianday(sent_at)` parses
ISO as UTC if the `Z` suffix is present — which `_iso_z` ensures.
OK in practice. But if any row gets written via direct SQL (tests)
without `Z`, comparisons misbehave. Add a CHECK constraint or
document the invariant in the schema.

### L-6 | `src/assistant/bridge/claude.py:244-246` | `system_notes` joined with `[system-note: ...]` wrapping — the wrapper is also what `validate_cron_prompt` rejects

Slight architectural smell: the bridge emits text the scheduler's
own validator rejects, so the defence layer relies on contextual
interpretation (bridge = trusted envelope wrapper; validator =
writes untrusted body). This is correct but fragile; document the
distinction in a high-level comment.

### L-7 | `src/assistant/tools_sdk/_scheduler_core.py:36-38` | `_SYSTEM_NOTE_RE` is case-insensitive but permits arbitrary whitespace (including unicode) before `system`/`system-note`

Python `re` `\s` with default flags matches unicode whitespace; a
clever attacker could prefix with a no-break-space (U+00A0), which
`str.strip()` at line 87 doesn't remove but `\s*` at the regex
matches. Actually `\s*` matches U+00A0 too — so the regex WOULD
catch this. But the `stripped` value is what's stored; if
`stripped.startswith("[system:`, the rejection works, but the
regex runs on `prompt` (not `stripped`). Check that the first
non-whitespace char is what you think it is.

**Fix**: run `_SYSTEM_NOTE_RE` on `stripped.lstrip()` to normalise
whitespace treatment.

### L-8 | `src/assistant/scheduler/__init__.py:1-17` | Empty package init with a rationale comment

Well-done; no action.

---

## Commendations

1. **Cron parser is remarkably clean** (cron.py:128-157): single-pass
   per field, fail-fast error messages citing the field name, explicit
   handling of `@`-aliases and Quartz extensions. The Vixie OR
   semantics is correct and the `raw_dom_star`/`raw_dow_star` flags
   are a neat solution to the "was this field literally `*`?" question
   that tripped up many open-source cron libraries.

2. **DST handling is correct** (cron.py:160-190, 218-248): the
   existence-before-ambiguity ordering per RQ3 is right; the
   `is_existing_local_minute` round-trip trick is the textbook way
   to detect spring-skip. The tests in test_scheduler_cron_semantics.py
   exercise both spring-skip-drop and fall-fold-fire-once cases.

3. **Prompt-injection defence is the right shape**: 3 layers
   (write-time reject, dispatch-time scrub, wrap-with-nonce) modelled
   exactly on phase 4's memory pattern. The nonce'd envelope is a
   proven technique from the memory server and the system prompt
   already primes the model to honour it.

4. **Per-chat lock is correctly double-checked** (handlers/message.py:152-166):
   the `_locks_mutex` wraps only the cold-path allocation; hot path
   is a lock-free dict read. Nice work.

5. **Boot classification M2.6 refinement** (store.py:463-484): using
   mtime before falling back to the embedded JSON timestamp correctly
   handles the "old marker with future timestamp" and "new marker
   with corrupted body" cases.

6. **Settings warning for tight `sent_revert_timeout_s`** (config.py:131-153):
   warn-don't-fail is the right tradeoff; the hint text is actionable.

7. **UNIQUE(schedule_id, scheduled_for)** as the at-least-once contract:
   the `IntegrityError → None` idiom in `try_materialize_trigger` is
   the textbook at-least-once primitive.

8. **Singleton-flock + `.last_clean_exit` marker** combo correctly
   distinguishes first-boot / clean-deploy / suspend-or-crash — the
   unlink-after-classify order is right.

9. **FakeClock fixture** (tests/conftest.py:60-86): minimal but
   complete — `now()` + `sleep()` + `advance()` + `slept` trace.
   Tests that use it (test_scheduler_loop_fakeclock.py,
   test_scheduler_queue_full_put_nowait.py) are clean and focused.

10. **Scheduler audit hook mirrors memory audit exactly** — DRY-ish
    (repeated pattern but clear ownership) and consistent file
    permissions (0o600) + rotation policy (deferred).

---

## Metrics

### LOC by file

| File | LOC |
|------|-----|
| `src/assistant/scheduler/__init__.py` | 17 |
| `src/assistant/scheduler/cron.py` | 292 |
| `src/assistant/scheduler/dispatcher.py` | 204 |
| `src/assistant/scheduler/loop.py` | 208 |
| `src/assistant/scheduler/store.py` | 511 |
| `src/assistant/tools_sdk/_scheduler_core.py` | 168 |
| `src/assistant/tools_sdk/scheduler.py` | 427 |
| **Production subtotal** | **1827** |
| tests/test_scheduler_*.py (15 files) | ~3500 |
| tests/test_handler_per_chat_lock_serialization.py | 147 |
| tests/test_daemon_clean_exit_marker.py | 86 |
| tests/test_scheduler_integration_real_oauth.py | 113 (gated) |

### Test count

~19 scheduler-related test files. Reviewed: 13 files covering
parser (+22 valid/5 invalid fixtures), DST semantics, store CRUD,
sweep expired sent, recovery (clean-slate + catchup-miss), dispatcher
lifecycle (dropped/dead-letter), dispatcher reads trigger prompt
(CR2.2), loop FakeClock tick, queue-full put-nowait, MCP registration,
origin branch, tool handlers (add/list/rm/enable/disable/history),
clean-exit marker round-trip, prompt rejection, dispatch marker, and
optional real-OAuth integration.

**Coverage gaps identified**:
- No test for the C-1 race (mark_sent vs terminal dispatcher state)
- No test for C-2 (reclaim picking up this-tick catchup triggers)
- No test for H-3 busy-poll cadence
- No test for supervisor backoff / crash-count cap in
  `_spawn_bg_supervised` (main.py:336-391)
- No test for `missed_notify_cooldown_s` (config has it but no
  codepath uses it — dead setting?)

### Complexity hotspots

| File:Function | Approx complexity | Note |
|---|---|---|
| `store.py:SchedulerStore` | 14 methods, 511 LOC | Big but cohesive. Could split CRUD vs recovery vs history into mixins. |
| `tools_sdk/scheduler.py:schedule_add` | 57 LOC | On the edge; see L-4. |
| `dispatcher.py:_process` | 90+ LOC | Try-except nested with two send paths (error + success) + dead-code history fetch. Extract a `_finalise_success` helper. |
| `cron.py:next_fire` / `is_due` | O(catchup_window_s / 60) per call | See H-4. |
| `loop.py:_tick_once` | Single method, 70 LOC | Readable but doing 4 distinct things (sweep, scan, materialise+enqueue, reclaim). Consider extracting the reclaim loop. |

### Known / acknowledged items (not re-flagged)

- `test_memory_search_seed_flowgent` — pre-existing failure per coder.
- Integration test gating via `ENABLE_SCHEDULER_INTEGRATION=1` — correct.
- `tool_error` copy across installer / memory / scheduler — deferred.
- `missed_notify_cooldown_s` is wired in config but I couldn't find a
  usage site — flag as a dead setting or wire up the recap-cooldown.

---

## Appendix: Critical-path trace for the C-1 race

Worst-case interleaving (asyncio single-threaded, cooperative):

```
tick N — loop task:
  1. sweep_expired_sent (lock, release)
  2. scan schedules, is_due → due minute M for schedule S
  3. try_materialize_trigger(S, prompt, M) → returns tid
     (UNIQUE write, lock acquired+released, row now 'pending')
  4. inflight.add(tid)            # sync, no yield
  5. q.put_nowait(trig)            # sync, no yield — dispatcher now runnable
  6. await mark_sent(tid)          # yields here
     ↓ event loop runs dispatcher task
     dispatcher:
       6a. q.get() returns trig (sync after put_nowait)
       6b. _lru_seen(tid) → False, adds to LRU
       6c. await get_schedule(S) — DB read, no _tx_lock
       6d. get_schedule returns sch with enabled=False
           (because owner disabled it via @tool call earlier)
       6e. await mark_dropped(tid) — acquires _tx_lock (loop hasn't yet)
       6f. row is now 'dropped'. Dispatcher returns.
       6g. finally: inflight.discard(tid)
     ↑ loop task resumes
  7. mark_sent (continuation) — acquires _tx_lock
  8. UNCONDITIONAL UPDATE: row 'dropped' → 'sent'
     row.sent_at = now

tick N+1 — loop task:
  1. sweep_expired_sent — row sent_at is fresh (<360s), not swept
  ... row sits in 'sent' until 360s...

tick N + ceil(360/15) = tick N+24 — loop task:
  1. sweep_expired_sent reverts row: 'sent' → 'pending', attempts++
  2. scan: schedule S is disabled (enabled=0), so is_due isn't run
     → no new materialisation for S in this tick
  3. reclaim_pending_not_queued finds the newly-reverted row
     (status='pending', scheduled_for is now very old, NOT in inflight)
     → re-enqueues it
  dispatcher picks it up:
    get_schedule(S) → enabled=False
    mark_dropped → row to 'dropped'
```

End state: status 'dropped' after 1 extra sweep cycle, but the model
never saw a double-fire because the schedule is disabled. **However**,
substitute the scenario with a successful `mark_acked`:
- Dispatcher handles the trigger, emits text, calls mark_acked
- Loop's mark_sent clobbers to 'sent'
- sweep reverts to 'pending' after 360s
- reclaim re-enqueues
- dispatcher dedups via LRU (in-process) → calls mark_dropped
- **Owner receives ONE message** (no double-fire in this specific
  process lifetime thanks to LRU)

But after a daemon RESTART before the 360s sweep:
- clean_slate_sent reverts 'sent' → 'pending' (attempts++)
- dispatcher LRU is empty post-restart
- reclaim re-enqueues
- dispatcher processes, calls bridge.ask, **OWNER RECEIVES DUPLICATE
  MESSAGE**

This is the actual double-fire path. It requires a daemon restart
within 360s of the first fire + the C-1 race to trigger. Low
probability per fire; non-zero over the lifetime of the bot.

**The fix (status guard on mark_sent) eliminates the entire class.**
