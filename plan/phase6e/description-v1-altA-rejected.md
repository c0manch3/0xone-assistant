---
phase: 6e
title: Voice/audio/URL transcription via _spawn_bg (Alt-A, lock-decoupled)
date: 2026-04-28
status: spec v1 (Alt-A) — pre-devil-wave-1
prereqs: phase 6a/6b/6c shipped + phase 6 (subagent infra) shipped 2026-04-28
supersedes: description-v0-subagent-rejected.md (devil rejected v0; Alt-A is handler-owned task pattern)
---

# Phase 6e — Voice/audio/URL handler with `_spawn_bg`

## Goal

Refactor `_handle_audio_turn` (phase 6c) so the per-chat lock is held
for ~50 ms (the dispatch window). The transcribe + bridge.ask + vault
save pipeline runs as a `Daemon._bg_tasks`-anchored asyncio task —
NOT a SDK subagent. Owner can chat during 22 min Whisper / 45 min URL
extract without blocking.

**Why Alt-A and not subagent?**

Voice/audio/URL is owner-recorded I/O orchestration with a
deterministic pipeline (transcribe → optional vault save → one Claude
turn). It does NOT need a model-loop tool agent. Devil wave-1 on the
v0 (subagent path) flagged 5 CRITICAL items: timeout arithmetic
mismatch, audit gap, tool widening (file paths leaked into model
prompt), file lifecycle gap, and boot-sweep race. Alt-A keeps the
existing `_handle_audio_turn` shape and just runs it in a background
asyncio task — none of those concerns apply because nothing crosses
the SDK boundary that wasn't crossing before.

Documents/heavy reasoning use the phase 6 subagent path (already
shipped) via `subagent_spawn(kind="general"|"worker")` — that path
stays unchanged. Voice = handler-owned async task; documents =
model-driven SDK subagent. Two distinct patterns for two distinct
shapes of work.

## Current state (phase 6c)

`_handle_audio_turn` in `handlers/message.py:969-1302`:
1. Pre-flight `transcription.health_check` (implicit via service
   `enabled` check at top).
2. `transcribe_file()` or `extract_url()` — **blocks per-chat lock
   22-45 min**.
3. 3-hour cap reject on returned duration.
4. Optional `_save_voice_to_vault()` (>120s, no save trigger in
   caption).
5. `_compose_voice_user_text()` — caption + transcript composition.
6. `bridge.ask(timeout_override=claude_voice_timeout)` — **blocks
   per-chat lock additional 1-15 min**.
7. Stream chunks via `emit`; persist user-row marker in `finally`.
8. Cleanup tmp file in `finally`.

Total locked time: 23-60 min on long content.

## Phase 6e (Alt-A) architecture

### Pre-flight stays in lock

The per-chat lock holds ONLY through:

- Adapter-level transcription health check (already done at routing
  time in adapter; adapter has emitted offline reply already if Mac
  sidecar is down).
- Path-containment + invariant asserts (already in `_handle_locked`).
- Build the `audio_job` payload (capture all `IncomingMessage` data
  the background task needs).
- Pre-lock ack: `"⏳ получил аудио D:DD, начинаю транскрибацию (~Y мин); ответ придёт отдельным сообщением"`.
- `daemon.spawn_audio_task(audio_job)` — `Daemon._bg_tasks.add(task)`.
- Synthetic turn completion with stop_reason `dispatched_to_bg` so
  the `conversations` row reaches a terminal state.
- Release per-chat lock.

Total: ~50 ms.

### Background task (`_run_audio_job`)

Exact same logic as today's `_handle_audio_turn` body steps 2-8, but
running OUTSIDE the per-chat lock. The task IS NOT re-acquiring the
per-chat lock — it streams its result back to owner WITHOUT lock.

Concurrency safety:
- `bridge.ask` already serialises owner-bridge calls via the
  bridge-level `asyncio.Semaphore` (one outstanding turn per bridge).
  If owner is mid-text-turn when the audio task's `bridge.ask` fires,
  the semaphore queues it. No interleaving.
- `conversations` writes: the audio task uses a fresh `turn_id`
  (allocated after dispatch ack — see §"Dispatch turn vs result
  turn"). Even though the lock is released, two separate `turn_id`s
  cannot interleave block writes for the same row. The semaphore
  above prevents Claude bridge interleave.
- Tmp file unlink stays inside the bg task `finally`.

### Dispatch turn vs result turn

Two `turn_id`s per audio job:

1. **Dispatch turn** — created by handler on receive; assistant
   message = pre-lock ack; stop_reason = `dispatched_to_bg`. Marker
   appended to user row: `[voice: D:DD | dispatched: bg-job-N]`.
   Closes inside the lock.
2. **Result turn** — created by background task before its
   `bridge.ask` call; stop_reason = whatever the bridge returns;
   assistant chunks accumulated normally; user row of the result
   turn is a NEW user row that mirrors the dispatch user row content
   plus the long-form marker `[voice: D:DD | seen: <first 200 chars> | vault: <path>]`.

Rationale: keeping result-turn creation deferred until the bg task
runs means the `conversations` table reflects "owner spoke at T0"
(dispatch) and "model replied at T1" (result) as two related but
distinct turns. History load on a subsequent owner turn loads BOTH —
model sees full context.

**Alternative considered**: single turn, deferred completion.
Rejected because (a) `complete_turn` post-lock-release means the
turn row is `pending` for 22-45 min — `cleanup_orphan_pending_turns`
on next boot would reap it; (b) history load during the 22-45 min
window would see the user row but no assistant row, making any
intermediate text turn confused.

### Failure path

- Transcription fails: bg task emits Russian fail message (same as
  current `_handle_transcription_failure`), persists user-row marker
  with `seen: (transcribe error: <reason>)`, completes result turn
  with stop_reason `transcription_failed`.
- Bridge fails: same as today (marker `seen: (bridge error)`,
  result turn completes with `claude_bridge_error`).
- Mac sidecar offline DURING the 22 min run (network blip): treat as
  transcription fail with the upstream error string.
- Daemon shutdown DURING bg task: `cancel_bg_tasks` cancels the task;
  `finally` block persists "(cancelled)" marker. Owner sees the
  dispatch ack but no follow-up — acceptable for shutdown semantics.

### Cancel during bg task

Owner-issued cancel during a long transcribe is NOT yet supported in
6c (Cancel during transcribe = cancel-after-current-tool only). Phase
6e keeps that behaviour: no new cancel surface for bg audio jobs in
this phase. Doc'd as carry-forward to phase 7.

### Marker semantics

Two user-row markers per voice/audio/URL:

1. **Dispatch marker** (handler synchronous, dispatch turn user row):
   `[voice: D:DD | dispatched: bg-job-N | scheduled at HH:MM]`
2. **Result marker** (bg task, result turn user row):
   `[voice: D:DD | seen: <transcript-200-chars> | vault: <path>]`
   (or the appropriate `audio:`/`voice-url:` prefix per source)

History load reconstructs: dispatch turn → "(audio dispatched)";
result turn → user content with full marker + assistant reply.

### What stays unchanged

- `TranscriptionService` httpx client + Mac whisper-server FastAPI
  + SSH reverse tunnel.
- `_compose_voice_user_text` (already fixed in 6c hotfix-2).
- `bridge.ask` semantics; semaphore-based serialisation.
- 6a/6b paths (PDF/photo) UNCHANGED.
- Phase 6 subagent path UNCHANGED — `subagent_spawn(kind=general)`
  for documents/heavy reasoning still goes through SDK Task tool.
- 3-hour duration cap, save trigger regex, vault threshold,
  Russian default captions.

### What changes

- New `Daemon.spawn_audio_task(job: AudioJob)` registered task.
- New `AudioJob` dataclass — carries `IncomingMessage` snapshot,
  `chat_id`, `dispatch_turn_id`, `emit` callable bound to chat.
- `_handle_audio_turn` split into `_dispatch_audio_turn` (in lock)
  and `_run_audio_job` (in bg).
- `Daemon.stop` already drains `_bg_tasks` (phase 6) — bg audio
  tasks ride on the same drain.

### Concurrency limit

Add `audio_bg_max_concurrent` setting (default: 2). Owner could in
theory dispatch 5 audio messages back-to-back; without a cap they
all run in parallel and pin Mac CPU + 5x download bandwidth.
`asyncio.Semaphore` inside `_run_audio_job` (acquire BEFORE
transcribe; release on completion). Owner gets pre-lock ack
immediately; the actual transcribe queues if cap hit. Status
visible in logs (`audio_bg_queued` line).

Default 2: matches current Mac sidecar single-instance + 1 spare for
bridge.ask phase of a previous job.

## Acceptance criteria

- AC#1 — owner records 30-sec voice → bot ack → bg task runs ~10 sec
  → result + marker delivered. Total: ~12 sec.
- AC#2 — owner records 5-min voice → bot ack → release lock → owner
  sends `/ping` → bot answers immediately. ~30 sec later bg result
  arrives.
- AC#3 — owner sends 1-hour podcast URL → bot ack → release lock →
  owner does multiple turns over 15 min → bg result + summary +
  vault path arrive when transcribe finishes.
- AC#4 — owner sends voice with caption "сохрани в проект альфа" →
  caption-driven save trigger; bg task saves to `vault/proekt_alfa/...`;
  marker reflects path.
- AC#5 — owner sends 4-hour content → handler rejects pre-dispatch
  (3-hour cap unchanged).
- AC#6 — Mac offline (sidecar down) → handler health-check fails
  BEFORE dispatch → reject reply, no bg task spawned.
- AC#7 — bg task fails mid-transcribe (Whisper crash) → owner gets
  notify "transcribe failed" + result-turn marker with error.
- AC#8 — `Daemon.stop` mid-bg-task → task cancelled; `finally`
  persists cancellation marker; no orphan pending turn after restart.
- AC#9 — owner sends 3 voices back-to-back, each 5 min → 2 run
  immediately, 3rd queues; owner gets dispatch ack for all 3 within
  ~150 ms; results arrive in dispatch order.
- AC#10 — phase 6a (PDF), 6b (photo), 6c-non-audio paths unchanged
  + GREEN.
- AC#11 — phase 6 subagent infra (general spawn / scheduler / cancel)
  unchanged + GREEN.

## Open questions for researcher

- **RQ1**: result-turn creation strategy — should result-turn be
  created BEFORE bridge.ask (so its turn_id is known for marker)
  or AFTER (so dispatch marker doesn't reference a turn that may
  fail to start)?
- **RQ2**: history load semantics during the 22-45 min bg window —
  if owner sends a text turn 5 min into a voice transcribe, the
  history will include the dispatch turn (user message + ack) but
  NOT the result turn (still running). Confirm this is what we want
  (owner's text turn is independent; voice result will arrive
  independently and stand alone in history).
- **RQ3**: `audio_bg_max_concurrent` default — verify 2 is right.
  Mac whisper-server is single-instance internally; a 2nd parallel
  /transcribe call would queue at FastAPI level anyway. So 2 is
  safe but yields no parallelism improvement. Maybe 1 + queue is
  cleaner? Or leave at 2 for the URL extraction (different endpoint)
  + voice transcribe (audio endpoint) parallelism?
- **RQ4**: `Emit` callable lifetime — `emit` in current handler is
  bound to the adapter's per-message context. If the bg task runs
  20 min later, is the emit callable still valid? (It should be —
  it's a closure over `adapter.send_text(chat_id, ...)` which is
  valid for the daemon's lifetime. Confirm.)
- **RQ5**: cancellation semantics on `Daemon.stop` — cancelling
  inside `transcribe_file` (httpx call mid-stream) vs inside
  `bridge.ask` (SDK call). Verify both raise `CancelledError`
  cleanly and the `finally` blocks fire.

## Carry-forwards (debt → phase 7 / later)

- Owner-issued cancel of a running bg audio job (currently no surface).
- Progress feedback during transcribe (typing indicator dropped per
  6c hotfix; no streaming progress yet).
- `audio_bg_max_concurrent` could be raised once Mac sidecar
  parallelism is verified (different endpoints).
- Multi-photo/multi-image batched analysis with same `_spawn_bg`
  pattern (mirror voice for 6b heavy media_groups).
