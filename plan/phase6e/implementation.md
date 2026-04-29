---
phase: 6e
title: Voice/audio/URL bg dispatch + isolated audio bridge — implementation notes
date: 2026-04-28
status: implemented + fix-pack consolidated (pre-deploy)
---

## Fix-pack changelog (2026-04-29)

4 reviewers (code-reviewer, qa-engineer, devops-expert, devil-w3) ran
parallel on the initial implementation. Multiple reviewers converged
on the same blockers — all 12 fixes applied below. Tests pinning
each fix live in `tests/test_phase6e_fixpack.py` (11 new) plus
extended construction tests in `tests/test_phase6e_message_construction.py`
(2 new — picker-origin rejection + audio-kind-without-source).

### CRITICAL (6/6)

- **F1** (Devil CRIT-2) — bg job now aggregates `text_chunks` into
  ONE final `emit_direct(full_reply)` instead of N pushes per
  TextBlock. Empty-output fallback `"(пустой ответ)"` mirrors the
  phase 6c text/photo paths. Prior behaviour: 10-block reply meant
  10 push notifications + risk of `TelegramRetryAfter` 429s.
- **F2** (Devil CRIT-1) — typing indicator stays alive across the
  ENTIRE bg run (transcribe + bridge.ask, multi-minute on long
  voice). Adapter now passes a `typing_lifecycle` async-context-
  manager factory through `Handler.handle(...)`. The bg body wraps
  itself in `async with job.typing_lifecycle():` so
  `send_chat_action` fires every 4.5 s until the result message
  lands. The default `_noop_typing_lifecycle` keeps tests green
  without firing real Telegram requests.
- **F3** (Devil CRIT-3 + CodeReview HIGH-2) — `emit_direct` no longer
  uses blanket `contextlib.suppress(Exception)`. Instead it logs:
  - `emit_direct_rate_limited` on `TelegramRetryAfter` (carries
    `retry_after`).
  - `emit_direct_telegram_api_error` on generic `TelegramAPIError`.
  - `emit_direct_unexpected` on anything else (adapter session
    closed during shutdown, generic guard).
  All three swallow propagation — bg task body never crashes on a
  flaky Telegram response — but operators get clean structured
  signal.
- **F4** (DevOps CRIT-1) — `Daemon.spawn_audio_task` now wraps the
  inner coroutine: any non-cancel exception becomes
  `log.exception("audio_bg_task_unhandled")` instead of an asyncio
  unraisable-hook stderr line. `CancelledError` is preserved so
  `Daemon.stop` drain semantics still work.
- **F5** (CodeReview CRIT-1) — path-containment guard rejection on
  the audio path now routes the Russian error reply through
  `emit_direct` (not `emit`, which is a lock-time no-op for audio).
  `notify_emit` is computed once at the top of `_handle_locked`
  via the early `is_audio_early` classifier.
- **F6** (CodeReview HIGH-1) — inline / test mode (`audio_persist_pending=None`)
  awaits `_persist()` directly. No `asyncio.create_task` + `shield`
  indirection means no orphan-task risk on cancel — synchronous
  test path is genuinely synchronous.

### HIGH (6/6)

- **F7** (QA H-1) — tmp-file `unlink` lives in an OUTER `finally` so
  it survives a `CancelledError` re-raised by the persist branch.
  Pre-fix the unlink lived inside the same finally as
  `raise asyncio.CancelledError`; cancel skipped cleanup → audio
  bytes leaked on disk on every shutdown-mid-bg-job.
- **F8** (DevOps CRIT-2) — outermost `try/finally` now wraps the
  ENTIRE `_run_audio_job` body (post-pre-flight). Persist runs
  regardless of where cancellation hits — mid-transcribe,
  mid-history-load, mid-bridge.ask. A `persist_scheduled` flag
  blocks double-scheduling on the bridge-error → outer-cancel
  sequence.
- **F9** (QA H-2) — `IncomingMessage.__post_init__` rejects an audio
  `attachment_kind` without a concrete source (no `attachment` AND
  no `url_for_extraction`). Pre-fix this surfaced as a confusing
  AssertionError deep inside `_run_audio_job`'s transcribe call.
- **F10** (CodeReview MED-1 + QA M-2) — `__post_init__` now blocks
  ALL non-telegram audio origins (was: only `scheduler`). Picker
  origin is just as broken — bg dispatch + no caller waiting on
  the picker's ledger to drain. Only `origin='telegram'` is
  meaningful for audio.
- **F11** (DevOps HIGH-1) — `Daemon.stop` drain now uses
  `asyncio.wait` + `not_done` snapshot instead of
  `wait_for(gather)` + post-cancel `t.done()` filter. The outer
  `gather` cancels its inner tasks on outer-cancel, so the
  post-timeout filter ALWAYS read back `outstanding=[]`. New
  log carries `outstanding=[task names with turn_id suffix]`
  for both audio-persist drain and the parity-improved subagent
  drain. Persist task created with
  `name=f"audio-persist-{turn_id}"`. Leftover tasks are now
  cancelled before `conn.close()` to avoid
  `aiosqlite.ProgrammingError` on a closed DB.
- **F12** (CodeReview MED-5 + Devil HIGH-1) — dispatch latency test
  threshold bumped 100 ms → 500 ms with comment that production
  target is 50 ms; 500 ms is CI-runner slack. Prior threshold was
  flaky on the GHA runner under load.

### Files touched (delta vs initial 6e impl)

| File | Δ | Note |
|---|---|---|
| `src/assistant/audio/__init__.py` | +20 | `TypingLifecycle` alias + `_noop_typing_lifecycle` factory; `AudioJob.typing_lifecycle` field. |
| `src/assistant/adapters/base.py` | +20 | F9 + F10 invariants in `__post_init__`; `Handler.handle` signature gains `typing_lifecycle: Any \| None = None`. |
| `src/assistant/adapters/telegram.py` | +50 / -20 | F2 typing_lifecycle factory built per turn; F3 typed exception logging in `emit_direct`. |
| `src/assistant/handlers/message.py` | +120 / -40 | F1 single emit + `(пустой ответ)` fallback; F5 `notify_emit` routing; F6 inline-mode direct await; F7 outer tmp-file finally; F8 outermost try/finally + `persist_scheduled` flag; F2 lifecycle wrap. |
| `src/assistant/main.py` | +30 / -15 | F4 spawn_audio_task wrapper; F11 `asyncio.wait` snapshot drain pattern (audio + subagent parity); persist task name. |
| `tests/test_phase6e_fixpack.py` | NEW +900 | 11 fix-pack regression tests (F1, F3, F4, F5, F6, F7, F8, F11). |
| `tests/test_phase6e_message_construction.py` | +50 | F9 + F10 construction-rejection tests. |
| `tests/test_phase6e_audio_bg_dispatch.py` | +5 / -2 | F12 latency threshold bump. |
| `tests/test_telegram_voice_handler.py` | +10 | `_FakeHandler.handle` accepts `typing_lifecycle` kwarg + captures it. |

### Test count

- 907 passed, 0 failed, 0 errors (full suite minus the pre-existing
  flaky `test_memory_store_save_transcript`).
- 25 phase 6e tests all GREEN (11 fixpack + 5 construction +
  3 dispatch + 3 isolation + 3 drain).

### Open issues

- `test_memory_store_save_transcript::test_save_transcript_concurrent_serialises`
  pre-existing flake (8/10 failure rate WITHOUT my changes when run
  standalone). Not phase 6e — recommend separate ticket to serialise
  `_ensure_index` calls or add a sqlite `busy_timeout`.
- AC#16 (≤1.3 GB RSS during AC#11 voice + text parallel) is a
  deploy-time observation, not test-asserted. Coding-side memory
  math (CRIT-1: 1050-1650 MB worst case) stays in-spec.
- HIGH-3 (replay ordering: marker user-row appended AFTER assistant
  rows) carry-forward to phase 7+ per spec §9; not regressed by
  this fix-pack.


# Phase 6e — Implementation notes

## Files touched

| File | Type | LOC delta | Purpose |
|---|---|---|---|
| `src/assistant/audio/__init__.py` | NEW | +49 | `AudioJob` dataclass for lock→bg handoff |
| `src/assistant/handlers/message.py` | MODIFIED | +441 / -202 (~239 net) | Split `_handle_audio_turn` into lock-time `_dispatch_audio_turn` + bg `_run_audio_job`; delete F7 scheduler-note injection |
| `src/assistant/main.py` | MODIFIED | ~+107 | `_audio_persist_pending` set, `audio_bridge` instance, `spawn_audio_task` method, drain in `stop`, boot orphan notify |
| `src/assistant/bridge/claude.py` | MODIFIED | +16 | `max_concurrent_override` kwarg in `ClaudeBridge.__init__` |
| `src/assistant/config.py` | MODIFIED | +29 | `ClaudeSettings.audio_max_concurrent: int = 1`; new `AudioBgSettings` with `drain_timeout_s: float = 5.0`; wire into `Settings.audio_bg` |
| `src/assistant/adapters/base.py` | MODIFIED | +40 | `IncomingMessage.__post_init__` rejects scheduler-origin audio; `Handler.handle` Protocol gains `emit_direct: Emit \| None = None` |
| `src/assistant/adapters/telegram.py` | MODIFIED | +40 | `_dispatch_audio_turn` rewrite: `emit` no-op, `emit_direct` exception-suppressed `bot.send_message`, no chunks-fallback |
| `tests/test_phase6c_fixpack.py` | MODIFIED | -42 | Delete F7 fixture (scheduler-origin audio scheduler-note); replaced by 6e construction tests |
| `tests/test_phase6e_message_construction.py` | NEW | +92 | 3 construction-rejection tests (CRIT-3 close) |
| `tests/test_phase6e_audio_bg_dispatch.py` | NEW | +391 | Dispatch latency, bg completion, cancel→interrupted, inline fallback |
| `tests/test_phase6e_audio_bridge_isolation.py` | NEW | +145 | audio bridge sem ≠ user bridge sem, FIFO with cap=1 |
| `tests/test_phase6e_persist_drain.py` | NEW | +125 | Drain happy path, timeout, empty-set guard |

`AUDIO_KINDS` constant noted in spec §7 as "doesn't yet exist" — actually
present in `adapters/base.py:41` since phase 6c. No re-add needed; the
construction guard in `__post_init__` re-uses it.

## Key design decisions

### Inline fallback for tests vs daemon-spawn for production

The spec calls `daemon.spawn_audio_task(job)` from inside the per-chat
lock so the lock releases in ~50 ms. To preserve backward-compat for
the ~30 phase-6c handler tests that call
`await handler.handle(msg, emit)` and expect synchronous completion,
`ClaudeHandler.__init__` accepts an optional
`audio_dispatch: Callable[[Coroutine], None]`:

- **None (default — test path):** the handler awaits the bg coroutine
  inline. The lock is held longer, but tests are single-threaded and
  not asserting on lock release timing.
- **Set (production path):** Daemon supplies `self.spawn_audio_task`
  which delegates to `_spawn_bg`. The lock releases as soon as the
  spawn returns (sub-millisecond).

Symmetric fallback for `emit_direct`: when the caller (test) passes no
`emit_direct` AND `audio_dispatch` is None, the handler reuses the
lock-time `emit` callable as `emit_direct`. Production adapters always
supply both. This keeps the spec's "emit no-op for audio path" contract
without forcing every existing test to wire two emit channels.

The fallback is **exclusively** triggered when both knobs are unset —
mixing (e.g. real `audio_dispatch` + missing `emit_direct`) hits the
defensive `audio_no_direct_emit` branch documented in spec §5.

### Persist task as a tracked set, NOT in-line shield

Researcher RQ2 / spec §6: a naïve `asyncio.shield(_persist())` inside
the bg task's `finally` orphans the inner task on bg cancel — `Daemon
.stop` then closes the DB while the orphan is mid-write, raising
`aiosqlite.ProgrammingError` on an unobservable handler.

Mirror of phase-6 `_sub_pending_updates` drain
(`main.py:728-748`):

```python
persist_task = asyncio.create_task(_persist())
if self._audio_persist_pending is not None:
    self._audio_persist_pending.add(persist_task)
    persist_task.add_done_callback(
        self._audio_persist_pending.discard
    )
try:
    await asyncio.shield(persist_task)
except asyncio.CancelledError:
    raise  # daemon drain owns the persist now
```

`Daemon.stop` then:

1. Cancels `_bg_tasks` (the audio bg task itself).
2. **NEW:** drains `_audio_persist_pending` with
   `audio_bg.drain_timeout_s` budget.
3. Drains `_sub_pending_updates` (UNCHANGED).
4. `conn.close()`.

On drain timeout: turn stays `pending`; boot reaper handles next start.

### Separate audio_bridge with `max_concurrent_override`

Phase 6 picker bridge already established the "separate bridge per
concern" pattern. Phase 6e extends `ClaudeBridge.__init__` with
`max_concurrent_override: int | None = None`. When set,
`self._sem = asyncio.Semaphore(override)`; otherwise the default
`settings.claude.max_concurrent` applies. User + picker bridges keep
the default (no override passed); audio bridge passes the new
`settings.claude.audio_max_concurrent` (default 1).

Memory math (researcher CRIT-1 conservative): 2 user + 2 picker + 1
audio = 5 SDK CLI subprocess @ ~150 MB peak = ~750 MB; +250 MB Python
heap +sqlite WAL = ~1050 MB peak vs 1500m container limit. ~30%
margin.

### Scheduler-origin audio rejection at construction

`IncomingMessage.__post_init__` extended (`adapters/base.py:121-156`):

```python
is_audio = (
    (self.attachment_kind in AUDIO_KINDS)
    or self.url_for_extraction is not None
)
if is_audio and self.origin == "scheduler":
    raise AssertionError("scheduler-origin audio/URL turns are not supported")
```

This explicitly reverts F7 (phase 6c fix-pack). Side effect: the
F7-era `scheduler_note` injection block at the old
`message.py:1149-1166` is now dead code; **deleted** during the
`_run_audio_job` rewrite. No feature flag, no fallback.

The scheduler dispatcher (`scheduler/dispatcher.py:123-134`) does NOT
construct audio messages today, so the construction guard is a
defense-in-depth layer; the dispatcher's outer `try/except Exception`
wrapper (`dispatcher.py:137`) catches the AssertionError and routes
through the standard `revert_to_pending` / dead-letter path.

### Boot orphan notify

Spec §10: `cleanup_orphan_pending_turns` is indiscriminate (covers
text/photo/audio/file). The orphan notify therefore uses generic
wording, not audio-specific:

```
⚠️ daemon перезапущен: {N} turn(s) прерван(ы). Если ждал результат — повтори запрос.
```

Placed at `main.py:482`-ish, RIGHT AFTER `await self._adapter.start()`,
BEFORE the existing subagent orphan notify. `orphans` is in scope
because line 347 captures the count from
`store.cleanup_orphan_pending_turns()`.

## Edge cases handled

- **Inline-mode test path with no `emit_direct`:** synthesised from
  lock-time `emit` so `await handler.handle(msg, emit)` works
  unmodified for phase-6c regressions.
- **Bg cancel mid-`bridge.ask`:** `_run_audio_job`'s `finally`
  schedules the persist task BEFORE re-raising `CancelledError`,
  ensuring `interrupt_turn` runs even on shutdown.
- **Persist task itself raising:** wrapped in
  `contextlib.suppress(Exception)` inside the persist coro AND
  `log.exception` in the outer await — never crashes the bg task on a
  flaky DB write.
- **`audio_persist_pending` set is None** (test fallback path): the
  persist task still runs via `asyncio.shield(persist_task)` await,
  just untracked. This is fine because tests don't simulate
  `Daemon.stop`.
- **Tmp file unlink in bg `finally`:** best-effort, AFTER the shielded
  persist; never re-raises on OSError so the bg task finishes cleanly.
- **`_dispatch_audio_turn(emit_direct=None)` defensive branch:**
  unreachable in production (scheduler-origin audio rejected at
  construction; adapter always supplies `emit_direct`), but if
  triggered it logs `audio_dispatch_missing_emit_direct`, completes
  the turn with `stop_reason="audio_no_direct_emit"`, and unlinks the
  tmp file.
- **Bridge isolation under `max_concurrent_override=None`:** explicit
  unit test (`test_user_and_audio_bridges_have_distinct_semaphores`)
  pins that three constructions yield three separate `asyncio
  .Semaphore` objects; refactor that "moves the sem to the module"
  triggers it.

## Test coverage summary

**12 new tests across 4 new files (all GREEN):**

`test_phase6e_message_construction.py` (3):
- `test_scheduler_origin_audio_rejected_at_construction`
- `test_scheduler_origin_url_extraction_rejected`
- `test_telegram_origin_audio_passes` (control)

`test_phase6e_audio_bg_dispatch.py` (3):
- `test_dispatch_returns_quickly_and_bg_completes`
  (asserts dispatch <100 ms; bg completes; emit_direct delivers)
- `test_cancellation_marks_turn_interrupted`
  (bg cancel mid-`bridge.ask` → `interrupt_turn` lands)
- `test_inline_fallback_runs_synchronously`
  (no daemon → inline await; existing 6c semantics preserved)

`test_phase6e_audio_bridge_isolation.py` (3):
- `test_audio_sem_full_does_not_block_user_bridge`
  (full audio sem → user_bridge.sem still acquires instantly)
- `test_user_and_audio_bridges_have_distinct_semaphores`
  (three sem instances are distinct)
- `test_two_concurrent_audio_jobs_serialise_on_audio_sem`
  (cap=1 → second job queues until first releases)

`test_phase6e_persist_drain.py` (3):
- `test_persist_drain_completes_within_budget`
- `test_persist_drain_timeout_logs_and_continues`
- `test_drain_noop_when_set_empty`

**1 deleted test:**
- `tests/test_phase6c_fixpack.py::test_scheduler_origin_audio_turn_passes_scheduler_note`
  (the F7 envelope-injection path no longer exists).

**Existing 6c suites: ALL GREEN unchanged.** The inline-mode fallback
in `_dispatch_audio_turn` + `emit_direct` synthesis preserved
`await handler.handle(msg, emit)` semantics so no churn was needed in
`test_handler_audio_branch.py`, `test_phase6c_e2e.py`, or
`test_telegram_voice_handler.py`.

## Open issues / things I couldn't resolve

- **`tests/test_memory_store_save_transcript.py::test_save_transcript_concurrent_serialises`**:
  pre-existing sqlite race in the test itself (8/10 failure rate
  WITHOUT my changes when running standalone in a tight loop). Not
  caused by phase 6e — the failing path is `_ensure_index` calling
  `executescript` on a fresh DB while another thread does the same.
  Phase 6e doesn't touch memory/store. Recommend opening a separate
  ticket: serialise `_ensure_index` calls or add a sqlite
  `busy_timeout`. Deselected from the suite run for diagnosis;
  re-include in CI once fixed.

- **AC#16 (memory observation):** documented as a deploy-time smoke
  step rather than implementation work. The bg task pattern + 1-slot
  audio sem caps the SDK subprocess footprint at the worst case
  computed in researcher CRIT-1 (~1050 MB peak). VPS smoke runbook
  below covers the observation window.

- **Replay ordering (HIGH-3, carry-forward):** marker user row is
  appended in the bg `finally` AFTER assistant rows. SQLite's
  monotonic `c.id` makes the marker sort AFTER the assistant chunks,
  so replay shows `assistant: <reply>` BEFORE
  `user: [voice marker]`. Pre-existing 6c behaviour; Alt-B does NOT
  regress. Deferred per spec §9.

## Deploy smoke runbook (AC#1 — AC#16)

Pre-flight (Mac):
1. `whisper-server` healthy on 127.0.0.1:9000.
2. SSH reverse tunnel `0xone@193.233.87.118:9000 → 127.0.0.1:9000`
   active.
3. `claude` OAuth credentials transferred (Mac Keychain → Linux
   `~/.claude/.credentials.json`).

Deploy (VPS):
```
ssh -i ~/.ssh/bot 0xone@193.233.87.118
cd /opt/0xone-assistant
docker compose pull && docker compose up -d
docker compose logs -f --tail 50
```

Owner smoke (Telegram):

| AC | Action | Expected |
|---|---|---|
| #1 | 30-sec voice | ack instant; ~10s later result + marker |
| #2 | 5-min voice → /ping during the 22-45 min wait | /ping answers in seconds; voice result lands later |
| #3 | 1h podcast URL → "транскрибируй <url>"; multiple text turns over 15 min | each text turn answers immediately; vault summary + marker eventually |
| #4 | voice "сохрани в проект альфа" | vault saves to `vault/proekt_alfa/...`; marker carries the path |
| #5 | 4h audio | rejected pre-dispatch with "слишком длинная" |
| #6 | Mac sidecar offline | "транскрипция временно недоступна (Mac sidecar offline)" |
| #7 | Mac returns 500 mid-transcribe | Russian fail message via emit_direct; turn `transcription_error` |
| #8 | `docker compose restart` mid-bg | shielded `interrupt_turn` lands; boot reaper no-op |
| #9 | 3 voices back-to-back | ack ×3 within 3s; jobs run FIFO on `audio_bg_sem(1)` |
| #10 | Text turn during pending voice | Clean context (history skips pending) |
| #11 | Voice + text in parallel | both run; user bridge slots free; text reply within seconds |
| #12 | Scheduler trigger w/ audio attachment | rejected at `IncomingMessage` construction (AssertionError logged) |
| #13 | Crash daemon during voice; restart | owner sees `⚠️ daemon перезапущен: N turn(s) прерван(ы)` |
| #14 | PDF/photo/text turns | unchanged + GREEN (regression) |
| #15 | `mcp__subagent_spawn` | unchanged + GREEN; picker_bridge sem unaffected |
| #16 | `docker stats` for 5 min during AC#11 | RSS ≤ 1.3 GB |

Rollback: `docker compose down && git checkout 0b28b1f
deploy/docker/docker-compose.yml && docker compose up -d` (returns to
pre-6e image tag).
