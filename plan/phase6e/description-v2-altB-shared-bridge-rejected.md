---
phase: 6e
title: Voice/audio/URL transcription via _spawn_bg (Alt-B, single-turn deferred)
date: 2026-04-28
status: spec v2 (Alt-B) — pre-devil-wave-2
prereqs: phase 6a/6b/6c shipped + phase 6 (subagent infra) shipped 2026-04-28
supersedes:
  - description-v0-subagent-rejected.md (devil rejected v0; SDK-subagent path wrong abstraction)
  - description-v1-altA-rejected.md (devil rejected v1; emit lifetime + bridge semaphore + history dup)
---

# Phase 6e — Voice/audio/URL via single-turn deferred bg task

## Goal

Refactor `_handle_audio_turn` (phase 6c) so the per-chat lock is held
~50 ms (dispatch window). The transcribe + bridge.ask + vault save
pipeline runs as a `Daemon._bg_tasks`-anchored asyncio task — using
the SAME `turn_id` as the lock-time start (single-turn deferred
complete). Owner can chat during 22-45 min run.

## Why Alt-B (not v0 subagent, not v1 Alt-A)

**v0 (SDK subagent path) — rejected**: voice is owner-recorded I/O
orchestration with deterministic pipeline (transcribe → save → one
Claude turn). Doesn't need model-loop tool agent. SDK subagent path
introduces 5 CRITICAL items (timeout/audit/tool/file/boot) that don't
apply to async I/O.

**v1 Alt-A (handler-owned bg task with split dispatch+result turn)
— rejected**: 4 CRITICAL items in devil wave-1:
1. `Emit` lifetime — adapter buffers chunks and flushes ONCE
   post-handler-return; handler returning in 50 ms means owner gets
   `"(пустой ответ)"` immediately + 22 min silence + lost real reply.
2. `bridge.ask` semaphore (`max_concurrent=2`) — two parallel bg
   audio jobs occupy both slots, owner-text turn blocks 22-45 min on
   semaphore. Lock-decoupling = lock-relocation.
3. History interleaving + duplicate user row (dispatch user row +
   result user row both with transcript) confuses model.
4. Adapter `_dispatch_audio_turn` incompatible with
   handler-returns-immediately (typing_task cancel, fallback
   `"(пустой ответ)"`).

**Alt-B — single-turn deferred complete**: ONE `turn_id`. Handler in
lock starts the turn, spawns bg task with `direct_emit` callback,
releases lock. Bg task does transcribe + bridge.ask using the SAME
`turn_id`, completes the turn in `finally`.

History invariant `load_recent` filters `status='complete'` only —
during the 22-45 min window the voice turn is `pending`, intermediate
text turns DON'T see it, no model confusion. Single user row =
no duplication.

## Architecture

### 1. Lock-held window (~50 ms)

`_handle_locked` audio branch:
1. `health_check` — already done at adapter level. (`pre-lock ack`
   already sent by adapter via `_send_pre_lock_ack` BEFORE entering
   handler — this stays.)
2. Path-containment guards (already in `_handle_locked`).
3. `turn_id = await self._conv.start_turn(chat_id)` — turn = `pending`.
4. Build `AudioJob` payload:
   - `chat_id`, `turn_id`, `IncomingMessage` snapshot, `direct_emit`
     callback (passed from adapter), `origin`, `meta`.
5. `daemon.spawn_audio_task(audio_job)` — registers via
   `Daemon._bg_tasks`. Task starts, but doesn't run yet (asyncio
   schedule).
6. `_handle_locked` returns. Lock releases.

The handler does NOT call `emit` during lock-time for audio path
(pre-lock ack already shipped to chat by adapter; nothing else to
flush). The adapter's post-handler chunks flush will see empty
`chunks` and must skip the `"(пустой ответ)"` fallback for audio
paths (see §"Adapter changes").

### 2. Bg task (`_run_audio_job`)

Same body as today's `_handle_audio_turn` lines 1026-1302, but:
- Uses the inherited `turn_id` (NOT a fresh `start_turn`).
- Acquires `audio_bg_sem` semaphore (`audio_bg_max_concurrent=1`)
  before transcribe, releases after.
- Emit goes through `direct_emit` (NOT chunks-buffer emit).
- `complete_turn` / `interrupt_turn` runs in `finally`.
- `tmp_file.unlink` runs in `finally`.

Failure paths unchanged (TranscriptionError → Russian fail message,
ClaudeBridgeError → `seen: (bridge error)` marker), just running
outside the lock.

### 3. Bridge concurrency fix

Bump `claude.max_concurrent: 2 → 3` in `config.py`. New
`audio_bg_max_concurrent: 1` setting (Mac whisper-server is
single-instance; FIFO is correct default; bump later if URL/audio
parallelism on Mac is verified).

Rationale:
- 1 slot reserved-by-discipline for bg audio bridge.ask.
- 2 slots for main turn flow (text/PDF/photo/scheduler).
- One bg audio job in flight + 1 owner text turn + 1 scheduler turn
  fit. Realistic owner usage: rarely >1 bg audio at a time.

Memory peak: 3× SDK CLI subprocess (~150 MB peak each) instead of
2×. Container `mem_limit: 1500m` survives.

NOT a separate bridge instance (Alt-C from devil) — saves ~150 MB +
no OAuth-state duplication.

### 4. Adapter changes

`_dispatch_audio_turn` (telegram.py:1076-1100) — minimal change:

```python
async def _dispatch_audio_turn(self, chat_id, incoming):
    if self._handler is None:
        return
    chunks = []
    async def emit(text): chunks.append(text)
    async def emit_direct(text):
        for part in _split_for_telegram(text, limit=TELEGRAM_MSG_LIMIT):
            await self._bot.send_message(chat_id, part)

    typing_task = asyncio.create_task(self._periodic_typing(chat_id))
    try:
        await self._handler.handle(incoming, emit, emit_direct=emit_direct)
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await typing_task

    # Audio paths return with empty chunks (dispatched to bg);
    # don't fallback to "(пустой ответ)". Non-audio paths still
    # fallback if they truly produced nothing.
    if not chunks:
        return
    full = "".join(chunks).strip()
    if not full:
        return
    for part in _split_for_telegram(full, limit=TELEGRAM_MSG_LIMIT):
        await self._bot.send_message(chat_id, part)
```

The `(пустой ответ)` fallback is REMOVED for audio paths — caller
(`_dispatch_audio_turn`) is audio-only by definition. Non-audio
adapter paths use the standard `_dispatch_text_turn` (or wherever the
existing fallback is preserved).

`Handler.handle` signature gains `emit_direct: Emit | None = None`.
For audio path the handler stores `emit_direct` in `AudioJob` and
passes it to bg task. For non-audio paths `emit_direct` is unused.

### 5. Pending-turn during 22-45 min

`load_recent` filter (`conversations.py:121`) is
`WHERE status='complete'` — the in-flight pending voice turn is
INVISIBLE to intermediate text turns. Model sees:
- T0: owner sends voice. `pre-lock ack` to chat (NOT in conversations
  table — ack is adapter-level send, not turn assistant row).
- T+5min: owner sends text turn. Bridge load_recent skips voice
  (still pending). Model replies based on ONLY prior completed
  turns. ✅ No confusion.
- T+22min: bg task completes voice turn. From here onward, voice
  turn shows in load_recent.
- T+23min: owner sends text. Bridge load_recent now includes voice
  turn (oldest among recent N). ✅ Coherent narrative.

### 6. Crash / Daemon.stop semantics

`cancel_bg_tasks` cancels in-flight bg tasks. Bg task `finally` block
must use `asyncio.shield` for the final SQL writes
(`interrupt_turn` + marker append) so cancellation does NOT race
with `aiosqlite.Connection.close`.

On boot `cleanup_orphan_pending_turns` (`main.py:347`) marks any
remaining `pending` turn as `'interrupted'`. Owner sees no follow-up
result for the voice; phase 7+ could add a "(прерван перезапуском)"
notify on boot for any reaped voice turn (carry-forward).

### 7. Cancellation while bg running

NOT supported in this phase — same as 6c. Carry-forward to phase 7.
Owner-issued cancel cancels owner's CURRENT turn (text turn), not the
bg voice. Telegram doesn't surface "cancel that voice" anyway.

### 8. Marker semantics (single-row, single-turn)

Same as 6c — one user-row marker appended in bg `finally` after
bridge.ask:
- Success: `[voice: D:DD | seen: <first 200 chars> | vault: <path>]`
- Failure: `[voice: D:DD | seen: (transcribe error: ...)]` /
  `[voice: D:DD | seen: (bridge error)]`

No dispatch marker (the pre-lock ack is the user-facing dispatch
signal; conversations table doesn't need a duplicate).

### 9. Scheduler-origin audio turns

Spec phase 6c re-raises `ClaudeBridgeError` from
`_handle_audio_turn` for scheduler-origin so dispatcher
(`scheduler/dispatcher.py`) can dead-letter / retry. With Alt-B,
handler returns successfully in 50 ms — dispatcher CANNOT see
downstream bridge failure.

**Decision**: scheduler-origin audio turns BANNED in phase 6e.
Adapter / scheduler dispatcher rejects audio-origin scheduler trigger
upfront with explicit log. Phase 7+ if needed: scheduler can
materialise a fail/notify path independently. `IncomingMessage`
constructor (or scheduler's `materialise_*` helpers) must reject
`origin='scheduler'` with `attachment_kind in AUDIO_KINDS or
url_for_extraction`.

### 10. What stays unchanged

- `TranscriptionService` httpx client + Mac whisper-server FastAPI
  + SSH reverse tunnel.
- `_compose_voice_user_text` (already 6c hotfix-2).
- `bridge.ask` internals (only semaphore size changes).
- 6a/6b paths (PDF/photo) UNCHANGED.
- Phase 6 subagent path UNCHANGED.
- 3-hour duration cap, save trigger regex, vault threshold,
  Russian default captions.
- pre-lock ack (`_send_pre_lock_ack`).

### 11. What changes

- `Daemon.spawn_audio_task(job: AudioJob)` registered via
  `_bg_tasks`.
- `AudioJob` dataclass.
- `_handle_audio_turn` split into `_dispatch_audio_turn`
  (in lock, ~50 ms) and `_run_audio_job` (in bg).
- `Handler.handle(msg, emit, emit_direct=None)` signature
  change.
- Adapter `_dispatch_audio_turn` skip-fallback for empty chunks.
- `claude.max_concurrent: 2 → 3`, new
  `audio_bg_max_concurrent: 1` setting.
- Scheduler reject of audio-origin triggers.

## Acceptance criteria

- AC#1 — owner records 30-sec voice → `pre-lock ack` immediately →
  bg task runs ~10 sec → result + marker delivered.
- AC#2 — owner records 5-min voice → ack → release lock → owner
  sends `/ping` → bot answers immediately → ~30 sec later voice
  result arrives.
- AC#3 — owner sends 1-hour podcast URL → ack → release lock →
  owner does multiple turns over 15 min (bridge.ask works in
  parallel via `max_concurrent=3`) → bg result + summary + vault
  path arrive when transcribe finishes.
- AC#4 — owner sends voice with caption "сохрани в проект альфа" →
  bg task saves to `vault/proekt_alfa/...`; marker reflects path.
- AC#5 — owner sends 4-hour content → handler rejects pre-dispatch
  (3-hour cap unchanged).
- AC#6 — Mac offline (sidecar down) → adapter `_ensure_sidecar_health`
  fails BEFORE dispatch → reject reply, no bg task spawned.
- AC#7 — bg task fails mid-transcribe (Whisper crash) → owner gets
  Russian fail message via `direct_emit`; turn completed with
  stop_reason `transcription_failed`.
- AC#8 — `Daemon.stop` mid-bg-task → task cancelled; `finally`
  shielded `interrupt_turn` succeeds; on boot
  `cleanup_orphan_pending_turns` is a no-op for this turn (already
  interrupted).
- AC#9 — owner sends 3 voices back-to-back → 1 runs immediately, 2
  others queue on `audio_bg_max_concurrent=1`. Owner gets ack for
  all 3 within 1 sec.
- AC#10 — intermediate text turn during pending voice — `load_recent`
  in text-turn bridge.ask returns turns BEFORE the voice (voice is
  still `pending`), model has clean context, no duplicate transcript.
- AC#11 — voice + text turn in parallel — `claude.max_concurrent=3`
  permits both bridge.ask calls to fire concurrently. Owner sees text
  reply within seconds; voice result arrives at its own pace.
- AC#12 — scheduler-origin trigger with audio attachment / URL →
  rejected upfront with explicit error log. Existing scheduler text
  triggers UNAFFECTED.
- AC#13 — phase 6a (PDF), 6b (photo), 6c-non-audio paths unchanged
  + GREEN.
- AC#14 — phase 6 subagent infra (general spawn / scheduler / cancel)
  unchanged + GREEN.

## Open questions for researcher

- **RQ1**: `load_recent` history-load timing — when bg task calls
  `bridge.ask` 5 sec into its run, the current turn IS the voice
  turn, but its user row hasn't been appended yet (marker is
  appended in `finally` AFTER bridge.ask). Bridge passes
  `user_text` directly + history (prior turns). Confirm bridge.ask
  doesn't re-load current-turn rows from DB during streaming.
- **RQ2**: `asyncio.shield` for `interrupt_turn` + marker append in
  bg task `finally` — does shield actually protect from
  `Daemon.stop` cancellation, or does the conn close out from
  under it? Verify with `aiosqlite` shutdown order in `main.py`.
- **RQ3**: `audio_bg_max_concurrent` default = 1 — verify Mac
  whisper-server FastAPI single-instance assumption (check
  `whisper-server/main.py` on Mac via Bash).
- **RQ4**: `bridge.ask` semaphore impact of `max_concurrent: 2 → 3`
  on phase 6 subagent picker — picker has its own bridge instance
  with own semaphore, so no impact. Confirm.
- **RQ5**: scheduler audio-origin reject — where exactly to enforce
  (adapter-level message construction, scheduler `materialise_*`,
  or handler-level `assert msg.origin != 'scheduler'`)? Find the
  cleanest layer.

## Carry-forwards (debt → phase 7 / later)

- Owner-issued cancel of running bg audio job (no surface).
- "(прерван перезапуском)" notify for reaped voice turns on boot.
- Progress feedback during transcribe (no streaming hook from
  Whisper sidecar).
- `audio_bg_max_concurrent` could be raised once Mac sidecar
  parallelism is verified.
- Multi-photo/multi-image batched analysis with same `_spawn_bg`
  pattern.
