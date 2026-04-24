# Phase 5b devil's advocate — wave 3 (shipped code)

**Scope:** uncommitted working tree. Read-only pass over
`src/assistant/scheduler/*`, `src/assistant/tools_sdk/{scheduler,_scheduler_core}.py`,
`src/assistant/{main,handlers/message,bridge/{claude,hooks}}.py`,
`src/assistant/state/db.py`, and new tests.

**Rule:** wave 1/2 findings excluded. Every CRITICAL has a scenario +
concrete fix.

---

## Executive summary

Five new issues surfaced that were not named in wave 1 or wave 2. Two
block commit: (1) **ClaudeBridgeError on a scheduler-fired turn is
silently acked, never retried, never dead-lettered**
(`dispatcher.py:148-150`) — the dispatcher only treats non-bridge
exceptions as retry-worthy, but `ClaudeBridgeError` is caught inside
the handler and swallowed into emit; (2) **in-flight scheduler-fire
during shutdown orphans the inner factory task** when the supervisor
is cancelled (`main.py:359-367` + `stop()` ordering) — the child task
outlives the supervisor, then hits a closed aiosqlite connection.

Two are HIGH: the **dispatcher issues a 200-row history query per
fire and throws the result away** (dead code that violates its own
CR2.2 comment); the **`missed_notify_cooldown_s` config knob is dead
code** — nothing consults it, so recap notifications are not
rate-limited across rapid restart cycles.

One is MEDIUM: **SIGKILL after `write_clean_exit_marker` but before
teardown completes causes B to misclassify boot as `clean-deploy`**,
suppressing a legitimate catchup recap.

---

## CRITICAL

### C1. `ClaudeBridgeError` on scheduler fire is acked, never retried

**Files:** `src/assistant/handlers/message.py:272-278`,
`src/assistant/scheduler/dispatcher.py:148-198`.

**Scenario.** `claude` CLI times out at 300 s (or OAuth session
expires, or SDK raises any non-cancel error). `ClaudeHandler._handle_locked`
catches `ClaudeBridgeError` at line 272, emits
`"\n\n(ошибка: timeout)"` through the `emit` callback, logs
`bridge_error`, and returns normally from `handle()`. No exception
reaches the dispatcher.

Dispatcher sees `handle()` return cleanly, falls through the
`except Exception` block at `dispatcher.py:150`, accumulates
`"(ошибка: timeout)"` into `final`, sends it to the owner as a
Telegram message, and calls `mark_acked` at line 198. `attempts`
stays at 0; the trigger is gone. Dead-letter never fires. Owner gets
a bare "(ошибка: timeout)" with no follow-up and no retry.

With `dead_attempts_threshold=5`, the invariant "five consecutive
bridge failures on the same trigger produce a dead-lettered notify"
is **never reachable** for bridge-level failures — only for errors
raised above the handler's try/except (e.g. a bug inside
`_classify_block`, or a `ConversationStore` crash).

**Fix.** One of:

1. In `dispatcher.py:_process`, after `handle()` returns, detect the
   "emit contains error marker" sentinel (brittle), OR
2. Change `ClaudeHandler._handle_locked` to **re-raise**
   `ClaudeBridgeError` on scheduler-origin turns so the dispatcher's
   outer try/except fires `revert_to_pending`. Telegram-origin turns
   keep the "emit apology, return cleanly" path (the user wants to
   see the error inline, not get no reply).
3. Cleaner: expose a `result` return value from `handle()` carrying
   `{completed, bridge_error}` and let the dispatcher decide.

Option 2 is the smallest diff:

```python
# handlers/message.py
except ClaudeBridgeError as exc:
    log.warning("bridge_error", turn_id=turn_id, error=str(exc))
    if msg.origin == "scheduler":
        raise                     # let dispatcher retry
    await emit(f"\n\n(ошибка: {exc})")
```

**Blast radius.** Every scheduler fire that hits a transient Claude
outage silently loses the trigger. Owner notices only the degraded
reply. With 64 schedules and a 30-minute Claude outage, potentially
dozens of acks-that-shouldn't-be.

---

### C2. Shutdown race: orphaned factory task hits closed DB connection

**Files:** `src/assistant/main.py:336-391` (`_spawn_bg_supervised`),
`src/assistant/main.py:458-501` (`stop()`).

**Scenario.** A scheduler fire is mid-flight inside
`dispatcher._process` (Claude SDK waiting on the model, up to 300 s).
Operator runs `systemctl --user restart` → `asyncio` gets SIGTERM →
`stop_event.set()` → `stop()` runs:

1. `write_clean_exit_marker(...)` — OK.
2. `self._sched_loop.stop()` / `self._sched_dispatcher.stop()` — sets
   `_stop` events, but `dispatcher.run()` is inside `_process`, not
   at the top-of-loop check. It will NOT see `_stop` until
   `_process` returns, which requires the 300 s Claude timeout or
   model completion.
3. `adapter.stop()` — awaited, completes fast.
4. `for t in list(self._bg_tasks): t.cancel()` — cancels the
   `_supervisor` task.
5. `asyncio.gather(*bg_tasks, return_exceptions=True)`.

Inside `_supervisor`, line 364 is `await task` on the inner
`factory()` task (`dispatcher.run`). The supervisor catches
`CancelledError` at line 366 and **re-raises**. BUT:
`asyncio.create_task(factory())` at line 362 creates an **independent**
task — cancelling the supervisor does NOT cancel the child. The
supervisor task resolves (CancelledError), `gather` returns, `stop()`
proceeds to `conn.close()` at `main.py:491`.

Meanwhile, `dispatcher.run` is still alive inside `_process`, still
holding references to the closed `aiosqlite.Connection` via
`SchedulerStore`. The next DB call raises `sqlite3.ProgrammingError:
Cannot operate on a closed database.` Depending on timing, this may
fire during `mark_acked` / `revert_to_pending` — the trigger is left
in `sent` status, recovered only on next boot via `clean_slate_sent`.

Same failure mode for `scheduler_loop` if it's mid-`_tick_once`.

**Fix.** In `_supervisor`, propagate cancellation to the child task
before re-raising:

```python
except asyncio.CancelledError:
    task.cancel()
    with contextlib.suppress(BaseException):
        await task
    raise
```

Alternatively, structure `stop()` to **wait** for the in-flight trigger
to ack before cancelling bg tasks — add a `dispatcher.drain()` that
awaits an empty queue + empty `inflight` set with a bounded timeout
(say, 60 s) before `stop()` cancels tasks. This also preserves the
CR2.1 invariant (an in-flight trigger completes cleanly on shutdown).

**Blast radius.** Every restart during an in-flight scheduler fire
leaves a `sent`-status orphan + an ugly `sqlite3.ProgrammingError`
in the log. `clean_slate_sent` does recover it on next boot
(`attempts` bumped, reverted to `pending`), so no permanent data
loss — but each graceful restart logs a spurious error and the
trigger is re-fired minutes later. With a daily `systemctl restart`
habit (phase 9 deploy cadence), this is 365 spurious errors/year.

---

## HIGH

### H1. Dispatcher queries 200-row history on every fire, discards result

**File:** `src/assistant/scheduler/dispatcher.py:116-128`.

```python
history = await self._store.get_schedule_history(
    schedule_id=None, limit=200
)
trigger_row = next(
    (r for r in history if r["id"] == trig.trigger_id), None
)
# ...
del trigger_row  # reserved for future expansion
trigger_prompt = trig.prompt
```

The fetched `trigger_row` is immediately `del`'d; the `trigger_prompt`
read falls back to the queue payload. Three problems:

1. **Wasted round-trip on the hot path.** Every scheduler fire runs
   `SELECT id, schedule_id, scheduled_for, status, attempts,
   last_error, sent_at, acked_at FROM triggers ORDER BY id DESC
   LIMIT 200` (`store.py:441-446`). At 1-2 fires/min × 64 schedules,
   that's a 200-row SELECT per fire for zero value.
2. **Violates its own CR2.2 comment.** The docstring at
   `dispatcher.py:110-115` claims the dispatcher "read[s] the prompt
   from the ``triggers`` row (immutable per-fire snapshot)". It does
   not — `get_schedule_history` does **not** select the `prompt`
   column. The dispatcher reads from `trig.prompt` (queue payload),
   which is set by the loop at materialisation time
   (`loop.py:170-175`) and is indeed immutable once enqueued, so the
   **invariant holds**, but the code that claims to implement it is
   dead.
3. **Misleading future reader.** "reserved for future expansion" is
   how dead code graduates to production debt.

**Fix.** Delete lines 116-128 entirely. If a comment is wanted, note
that `trig.prompt` was sourced from `triggers.prompt` via the loop
and is already immutable.

**Severity.** HIGH because (a) the docstring lie is a ticking time
bomb for the next reviewer, and (b) 200-row SELECT per fire is
measurable cost at scale.

---

### H2. `missed_notify_cooldown_s` is dead config; recap not rate-limited

**Files:** `src/assistant/config.py:101`
(`missed_notify_cooldown_s: int = 86400`),
`src/assistant/main.py:310-327`.

`grep -rn missed_notify_cooldown_s src/` returns exactly one hit
(the definition). The recap notify at `main.py:311-327` fires every
non-clean-deploy boot where `catchup_missed >= min_recap_threshold`
(default 2), with no cooldown consulted.

**Scenario.** Network flap causes the daemon to crash-loop every 90
s for an hour. Each boot classifies as `suspend-or-crash` (marker is
unlinked 10 s before crash), every boot computes
`catchup_missed >= 2` (any schedule firing more than hourly), every
boot sends a "пока я спал, пропущено X напоминаний" message. Owner
gets 40 identical recap messages in one hour. Plan's intended
24-hour cooldown is documented in config but never enforced.

**Fix.** Persist last-recap-sent timestamp in a new file (e.g.
`<data_dir>/.last_recap`) or a new column on `schedules` (too
heavyweight) and skip the recap if less than
`missed_notify_cooldown_s` has passed. Alternatively, delete the
config field and document the absence.

**Severity.** HIGH because the trigger (crash loop + many schedules)
is realistic and the spam is visible to the owner during the very
incident that's already stressful.

---

## MEDIUM

### M1. SIGKILL after marker write but before teardown -> false clean-deploy

**Files:** `src/assistant/main.py:458-468` (`stop()` marker write
first), `src/assistant/scheduler/store.py:463-484`
(`_classify_boot_sync`).

`stop()` writes the clean-exit marker **first**, then tears down the
scheduler, adapter, bg tasks, and DB. If the process receives SIGKILL
between the marker write (`os.replace` ~ 1 ms) and the end of
`stop()` (up to ~20 s if in-flight Claude timeout), the marker is
freshly written even though the daemon was killed. Next boot within
`clean_exit_window_s` (120 s default) classifies as `clean-deploy`;
catchup recap is suppressed; owner is told nothing.

In practice, systemd sends SIGTERM, waits `TimeoutStopSec` (default
90 s), then SIGKILL — so the kill arrives after the in-flight trigger
has (probably) completed. But a user explicitly `kill -9`'ing the
process after a hang hits this window cleanly.

Wave 2 credited M2.6 for looking at mtime; M2.6 does not help here
because mtime is **fresh** (written milliseconds before SIGKILL).

**Fix.** Move `write_clean_exit_marker(...)` to the **end** of
`stop()`, after `conn.close()` and the lock release. A SIGKILL
anywhere before the marker write → marker absent → correctly
classified as `suspend-or-crash`. Cost: one `contextlib.suppress`
(OSError) wrapper, no behaviour change on clean exit.

**Severity.** MEDIUM — narrow window, low probability in systemd
deployments, but trivially fixable.

---

### M2. `schedule_add` parses cron twice

**File:** `src/assistant/tools_sdk/scheduler.py:154-188`.

Line 155 calls `parse_cron(cron_raw)` into `expr`; line 185 does
`del expr`; line 186 calls `core.fetch_next_fire_preview(cron_raw,
...)` which internally calls `parse_cron(cron_raw)` **again** (see
`_scheduler_core.py:167`). The second parse is redundant; `expr`
could be passed in directly.

**Fix.** Change `fetch_next_fire_preview` signature to accept a
pre-parsed `CronExpr` (or a pre-parsed `CronExpr | str` union) so
the validated result is reused. Cost: 3 LOC, one signature change.

**Severity.** MEDIUM (micro-optimisation, but the explicit
`del expr` reads like the author knew it was wasteful and
didn't fix it).

---

### M3. `schedules` CASCADE delete is dead code

**File:** `src/assistant/state/db.py:176-189`.

`triggers.schedule_id REFERENCES schedules(id) ON DELETE CASCADE` —
but the tool surface (`schedule_rm`) is **soft-delete** (flips
`enabled=0`); no path issues `DELETE FROM schedules`. The cascade
never fires in production. Not wrong, but documents an unused
contract; a future phase that introduces hard-delete will need to
think about orphan `triggers` rows (the CASCADE at least flags it).

**Severity.** MEDIUM (documentation / lock-in risk, not a bug).

---

### M4. 27 allowed_tools inflates first-turn ToolSearch cost

**Files:** `src/assistant/bridge/claude.py:158-175`.

Phase 5 adds 6 scheduler tools on top of phase-3/4's 7 installer + 6
memory + 8 builtin = **27 tools** in `allowed_tools`. Per the project
memory note `reference_claude_agent_sdk_gotchas.md`
(`ToolSearch auto-invoke first-turn`), every session start invokes
ToolSearch with the full tool descriptions — larger response payload
and (cached) prefix. No data shows the cost is prohibitive, and the
system prompt preset has `exclude_dynamic_sections=True` (stable
prefix caches). But no measurement was attempted either.

**Severity.** MEDIUM (cost inflation, no regression). Owner should
measure `usage.cache_read_input_tokens` vs `input_tokens` on a
synthetic first turn before phase 6 adds more tools.

---

## LOW

### L1. `wrap_scheduler_prompt` scrubs only `<scheduler-prompt-*>`, not `<untrusted-*>`

**File:** `src/assistant/tools_sdk/_scheduler_core.py:136-141`.

Write-time rejects both sentinel families. Dispatch-time wrap only
scrubs `scheduler-prompt-...`. If write-time validation were ever
weakened (e.g. new code path bypasses `validate_cron_prompt`), the
`<untrusted-*>` family would reach the model un-scrubbed. Defence in
depth gap, not a live bug.

**Fix:** extend the scrub regex to cover `untrusted-(note-body|
note-snippet|scheduler-prompt)` (same pattern as
`_SENTINEL_TAG_RE`).

### L2. `schedule_add` `max_schedules` race

Flagged in wave 2 (CR2.3 advisory cap), carried forward. The check
`COUNT(*) + INSERT` is not atomic within the `_tx_lock` because the
`_tx_lock` is **per-`SchedulerStore` instance**, and daemon owns
exactly one — so in practice this is fine. Only concern is a future
refactor that creates a second `SchedulerStore` on the same
connection. Document the invariant in the class docstring.

### L3. `schedule_add` inline preview can mislead on DST spring-forward

`fetch_next_fire_preview` returns `None` if `next_fire` finds no
match within 1500 days. For a legitimate
`0 2 29 2 *` (leap-day 2 AM) on a tz that DST-skips 02:00, `None` is
returned with no warning in the preview text. The owner-facing text
becomes `"scheduled id=N cron='...'"` with no `next_fire=...`,
silently masking a schedule that will never fire. Cost: 1 log line at
`schedule_add` level on `None` preview to warn the caller.

### L4. `test_scheduler_integration_real_oauth` asserts "send_text called"

**File:** `tests/test_scheduler_integration_real_oauth.py:107-112`.

Assertion: `assert delivered, "adapter.send_text never called within
90s"`. Does NOT check the text content. The test passes even if the
model returned `"error: scheduler broken"` or an empty non-empty
string. Given the schedule prompt is "Reply with the single word
'PONG'", asserting `"PONG" in adapter.sent[0][1]` would make the test
catch a corrupted dispatch path. Minor.

### L5. `test_memory_integration_ask.py` is permissive

**File:** `tests/test_memory_integration_ask.py:62-69`.

Loops until it sees ANY `mcp__memory__*` tool use. Real Q-R10 intent
was `memory_search`. If the model picks `memory_list` instead, the
test passes without exercising the FTS5 path. Change to
`block.name == "mcp__memory__memory_search"` or equivalent.

### L6. `ToolSearch` load inflation from MCP instructions

Unrelated to phase 5 code, but observable in the current environment
reminders: Figma / Playwright MCP servers preload 50+ deferred tools
on every init. In owner-deployed daemon these are absent; the
production tool count stays at 27. Noted for completeness — no
action.

---

## Risks carried to phase 6+

1. **Dispatch recovery logic depends on trigger semantics staying
   identical across phases.** Any phase that introduces per-trigger
   metadata (retry_delay, priority) will need to extend `triggers`
   schema AND `reclaim_pending_not_queued` filter AND dispatcher
   enqueue. Three coupling points; hard to refactor cleanly.
2. **`_chat_locks: dict[int, asyncio.Lock]` grows unboundedly** if
   multi-chat support is ever added. In single-owner deployment this
   is one entry. Document the precondition on the class and add a
   TTL/weakref before any multi-chat migration.
3. **`classify_boot` mtime-based mechanism is timezone-naïve.** It
   compares `datetime.now(dt.UTC).timestamp()` against `st_mtime`,
   which is seconds-since-epoch UTC. Correct on Linux. On macOS APFS
   with sub-second precision the timestamp is float; no loss. But
   deploy-time concerns: if `data_dir` is on a cloud-synced volume
   (Dropbox, iCloud), mtime may drift. Document "data_dir must be on
   a local filesystem" invariant.
4. **Supervised bg task cancellation does not cascade to children**
   (see C2). This is a daemon-wide bug, not scheduler-specific.
   Phase 6 will spawn more supervised tasks (GitHub vault backup);
   same orphan risk.
5. **Scheduler-fired turns persist wrapped prompts into
   `conversations`.** Over 30 days at 1 fire/hour, owner's chat
   history accumulates ~720 wrapped-prompt user rows. Each one
   consumes tokens on every subsequent replay (bounded by
   `history_limit`). Audit `history_limit` default and the "scheduler
   user rows count toward the history budget" invariant before phase 6.

---

## Unknown unknowns

1. **Telegram rate-limits on recap spam.** If the crash-loop
   scenario in H2 fires 40 recap messages in an hour, Telegram may
   throttle the bot and drop genuine owner messages during the
   throttled window. No test covers this. Phase 6 should measure.
2. **SDK behaviour when `prompt_stream` yields both history AND the
   wrapped scheduler prompt.** `history_to_sdk_envelopes` sends prior
   turns; then the final envelope is the wrapped fire. If the SDK's
   streaming-input heuristic decides "this prompt looks like a
   resumed tool_use because history had tool_use blocks" and chooses
   to call a tool **instead** of producing text, the dispatcher's
   `accumulator` stays empty and `mark_acked` fires with no text
   sent to the owner. The spec says "respond proactively; do not
   ask for clarification" in `scheduler_note`, but tool-only
   responses are a third option. Unknown frequency in practice;
   would manifest as "scheduled trigger ran but nothing arrived in
   Telegram".
3. **DST transition during a long-running fire.** If a fire starts
   at 02:58 local Europe/Berlin on fall-back night and the handler
   takes 5 min, `mark_sent`'s `strftime('%Y-%m-%dT%H:%M:%SZ','now')`
   uses SQLite's `now` (UTC), so no ambiguity. But log timestamps
   via `structlog` / Python `datetime.now()` may render wall-clock
   local and look like the fire "happened twice" in the log. Pure
   log aesthetics; not a correctness issue.
4. **The hook sees `[system-note:` literal in `user_text_for_envelope`.**
   No hook currently inspects the user text for injection patterns,
   but if a future PreMessage hook does, it would need to know to
   exempt the harness's own `[system-note:` injections. Add a
   comment at `claude.py:243-246` flagging this.
5. **`create_sdk_mcp_server` with 6 tools — is the MCP JSONRPC
   bus serialised per-server or per-tool?** If per-server, two
   concurrent scheduler tool invocations (owner turn + scheduler
   fire both calling `schedule_list`) may queue behind each other
   on the scheduler server. With the new CR-1 per-chat lock, only
   one turn runs at a time on OWNER_CHAT_ID, so this is theoretical
   for single-user. But multi-chat future would expose it.

---

## Verdict

🔴 **Rethink C1 and C2 before commit.** C1 is a silent correctness
regression: scheduler fires can be swallowed by Claude outages with
no retry visible to the owner. C2 is a shutdown-time race that
surfaces as spurious `ProgrammingError` logs on every restart with
an in-flight fire. Both are small diffs (< 10 LOC each). H1 and H2
are cleanup that can defer to a follow-up but should be tracked as
debt. M1 is a trivial reorder of two lines in `stop()` with no
regression risk; fix alongside C1/C2.

Everything else is yellow or green.

Top 3 fixes if blocked on commit:

1. **C1:** re-raise `ClaudeBridgeError` from `ClaudeHandler._handle_locked`
   when `msg.origin == "scheduler"`.
2. **C2:** inside `_supervisor`'s `CancelledError` branch, cancel +
   await the inner `task` before re-raising.
3. **M1:** move `write_clean_exit_marker(...)` call in `stop()` to
   AFTER `conn.close()` and lock release.
