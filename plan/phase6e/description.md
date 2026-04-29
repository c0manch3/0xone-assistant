---
phase: 6e
title: Voice/audio/URL transcription via _spawn_bg with isolated audio bridge
date: 2026-04-28
status: spec v3.1 (Alt-B + Alt-C audio bridge, post-researcher) — pre-coder
prereqs: phase 6a/6b/6c shipped + phase 6 (subagent infra) shipped 2026-04-28
supersedes:
  - description-v0-subagent-rejected.md (devil w1 — wrong abstraction)
  - description-v1-altA-rejected.md (devil w1 — emit lifetime + bridge sem + dup user row)
  - description-v2-altB-shared-bridge-rejected.md (devil w2 — shared `max_concurrent` bumped picker too; memory math wrong)
---

# Phase 6e — Single-turn deferred bg task with isolated audio bridge

## Goal

Refactor `_handle_audio_turn` (phase 6c) so the per-chat lock is held
~50 ms (dispatch window). Transcribe + bridge.ask + vault save run as
a `Daemon._bg_tasks`-anchored asyncio task using the SAME `turn_id`
(single-turn deferred complete). Bridge contention with owner-text is
eliminated by routing bg audio through a SEPARATE bridge instance.

## Why this design

- v0 (SDK subagent path) — wrong abstraction. Voice is owner-recorded
  I/O orchestration, not model-loop tool agency.
- v1 Alt-A (split dispatch + result turn) — adapter `emit` lifetime
  bug (chunks buffered then flushed once post-handler-return; bg
  emits go nowhere); history-row duplication.
- v2 Alt-B + shared bridge bump (`max_concurrent: 2 → 3`) — picker
  bridge reads same setting → bump is symmetric → worst case 6 SDK
  subprocess @ ~1150 MB, near 1500m mem_limit.
- **v3 (this spec): Alt-B (single-turn deferred) + Alt-C (separate
  audio bridge with `max_concurrent=1`).** Owner-text never blocked
  by audio; worst case 5 subprocess @ ~750 MB; clean semantic
  separation: user-bridge / picker-bridge / audio-bridge each with
  one purpose.

## Architecture

### 1. Lock-held window (~50 ms)

`_handle_locked` audio branch:
1. Pre-flight (`_send_pre_lock_ack` already sent by adapter BEFORE
   handler entry — unchanged).
2. Path-containment guards (already in `_handle_locked`).
3. `turn_id = await self._conv.start_turn(chat_id)` — turn = `pending`.
4. Build `AudioJob(chat_id, turn_id, msg, origin, meta, emit_direct)`.
5. `daemon.spawn_audio_task(job)` — task registered via
   `Daemon._bg_tasks`.
6. Handler returns. Lock releases.

Handler does NOT call `emit` for audio path. Pre-lock ack already
shipped; nothing else flushes through chunks.

### 2. Bg task `_run_audio_job`

Body inherits 6c `_handle_audio_turn` logic 1026-1302 with these
deltas:
- Uses inherited `turn_id` (no fresh `start_turn`).
- Acquires `audio_bg_sem` (`audio_bg_max_concurrent=1`) BEFORE
  transcribe; releases AFTER bridge.ask. Mac whisper-server is
  single-instance — FIFO is correct default.
- Calls `audio_bridge.ask(...)` (NOT user `bridge.ask`).
- All emits use `emit_direct(text)` — direct adapter.send_text path.
  `emit_direct` is exception-suppressed (see §4).
- `complete_turn` / `interrupt_turn` runs in `finally` under ONE
  shielded async block (see §6).
- `tmp_file.unlink` runs in `finally` (after the shielded block,
  best-effort).

### 3. Bridge isolation (Alt-C)

Three bridges total, each with own semaphore:

| Bridge | `max_concurrent` | Used by |
|---|---|---|
| `bridge` (user) | `claude.max_concurrent=2` (UNCHANGED) | owner-text, scheduler-text, file/photo paths |
| `picker_bridge` | `claude.max_concurrent=2` (UNCHANGED, shares setting) | phase 6 subagent dispatcher |
| `audio_bridge` (NEW) | `claude.audio_max_concurrent=1` (NEW setting) | bg audio jobs |

Construction in `Daemon.start` (`main.py:402-433`):
```python
audio_bridge = ClaudeBridge(
    self._settings,
    extra_hooks=sub_hooks or None,  # Subagent Task tool not enabled by default for audio bridge — see §10
    agents=None,                    # audio path doesn't spawn subagents
    max_concurrent_override=self._settings.claude.audio_max_concurrent,
)
```

Requires `ClaudeBridge.__init__` extension: `max_concurrent_override:
int | None = None`. When set, `self._sem = asyncio.Semaphore(override)`
instead of `Semaphore(settings.claude.max_concurrent)`. Other bridges
unaffected (no override → default).

Worst case memory:
- 2 user + 2 picker + 1 audio = 5 SDK CLI subprocess (~150 MB each
  peak) = ~750 MB.
- Plus Python heap (~250 MB) + sqlite WAL pages.
- Total peak ~1050 MB at 1500m mem_limit. ~30% margin.

### 4. Adapter changes

`_dispatch_audio_turn` (telegram.py:1076-1100) — minimal:

```python
async def _dispatch_audio_turn(self, chat_id, incoming):
    if self._handler is None:
        return

    async def emit(text: str) -> None:
        # Audio path dispatches to bg; nothing flushed via chunks.
        # Kept for Handler Protocol compatibility.
        return

    async def emit_direct(text: str) -> None:
        # Bg-time direct send. Suppress exceptions because adapter
        # session may be closed during Daemon.stop ordering.
        with contextlib.suppress(Exception):
            for part in _split_for_telegram(text, limit=TELEGRAM_MSG_LIMIT):
                await self._bot.send_message(chat_id, part)

    typing_task = asyncio.create_task(self._periodic_typing(chat_id))
    try:
        await self._handler.handle(
            incoming, emit, emit_direct=emit_direct,
        )
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await typing_task
    # No post-handler chunks flush for audio path. Bg task owns
    # all owner-visible output via emit_direct.
```

The `(пустой ответ)` fallback in non-audio code paths (`_on_text`,
`_on_message_with_attachments`) is UNCHANGED.

### 5. `Handler.handle` signature

`adapters/base.py:153-156` updated:
```python
class Handler(Protocol):
    async def handle(
        self,
        msg: IncomingMessage,
        emit: Emit,
        emit_direct: Emit | None = None,
    ) -> None: ...
```

Default-None preserves backward-compat for scheduler dispatcher
(`scheduler/dispatcher.py:136`) and tests
(`test_handler_attachment_branch.py`,
`test_quarantine_rename_oserror_copy_fallback.py`) — none of these
need updating.

Handler audio branch defensive check: if `msg` is audio AND
`emit_direct is None` → log error and complete turn with stop_reason
`audio_no_direct_emit`. Should never happen (scheduler-origin audio
is rejected at construction per §7); but defensive.

### 6. `Daemon.stop` ordering + drain (separate persist task set)

**v3.1 fix (researcher RQ2)**: in-line `asyncio.shield(_persist())`
inside the bg task's `finally` is the textbook orphan-task bug —
when the host bg task is cancelled, the shielded inner task is
detached, no parent awaits it, and exceptions become silent console
warnings. If `Daemon.stop` proceeds to `conn.close()` while the
orphan persist task is mid-write, `aiosqlite.ProgrammingError` is
raised inside an unobservable handler. This re-introduces phase-6
GAP #12.

**Correct pattern (mirror of phase-6 `_sub_pending_updates` drain at
`main.py:728-748`)**: persist runs as a separate task tracked in a
`Daemon._audio_persist_pending` set, drained inside `Daemon.stop`
with bounded timeout BEFORE `conn.close()`.

New settings:
- `claude.audio_max_concurrent: int = 1` (audio bridge semaphore).
- `audio_bg.drain_timeout_s: float = 5.0` (mirror of
  `subagent.drain_timeout_s`).

Wiring in `Daemon.__init__` (next to `_bg_tasks` at `main.py:208`):
```python
self._audio_persist_pending: set[asyncio.Task[Any]] = set()
```

Bg task `finally`:
```python
finally:
    async def _persist() -> None:
        await self._persist_voice_user_row(...)
        # complete_turn on success path; interrupt_turn on cancel/error.
        await self._conv.interrupt_turn(turn_id)

    persist_task = asyncio.create_task(_persist())
    self._daemon._audio_persist_pending.add(persist_task)
    persist_task.add_done_callback(
        self._daemon._audio_persist_pending.discard
    )
    try:
        await asyncio.shield(persist_task)
    except asyncio.CancelledError:
        # Re-raise — Daemon.stop will drain persist_task via the set.
        raise
    except Exception as exc:
        log.exception("audio_bg_persist_failed", error=repr(exc))
    # Tmp file cleanup (best-effort; no shield).
    if msg.attachment is not None:
        with contextlib.suppress(OSError):
            msg.attachment.unlink(missing_ok=True)
```

`Daemon.stop` order (extends `main.py:716-752`):
1. `adapter.stop()` — UNCHANGED.
2. `_bg_tasks.cancel()` + `gather(return_exceptions=True)` —
   UNCHANGED (cancellation propagates out of bg audio task into the
   `finally`, which schedules its persist task and re-raises).
3. **NEW**: drain `_audio_persist_pending` with
   `audio_bg.drain_timeout_s` budget:
   ```python
   if self._audio_persist_pending:
       pending = list(self._audio_persist_pending)
       try:
           await asyncio.wait_for(
               asyncio.gather(*pending, return_exceptions=True),
               timeout=self._settings.audio_bg.drain_timeout_s,
           )
       except TimeoutError:
           log.warning("daemon_audio_persist_drain_timeout",
                       count=sum(1 for t in pending if not t.done()))
   ```
4. Subagent pending updates drain — UNCHANGED.
5. `conn.close()` — UNCHANGED.

On drain timeout: turn stays `pending` in DB → boot reaper handles
on next start.

### 7. Scheduler-origin audio rejection (CRIT-3 close)

**v3.1 fix (researcher)**: `AUDIO_KINDS` constant doesn't yet exist
in the codebase — add it explicitly to avoid coder-improvised gaps.

Define near `IncomingMessage` in `adapters/base.py`:
```python
AUDIO_KINDS: frozenset[str] = frozenset(
    {"ogg", "mp3", "m4a", "wav", "opus"}
)
```

Extend `IncomingMessage.__post_init__` (`adapters/base.py:121-135`):
```python
def __post_init__(self) -> None:
    # Existing F10 mutex check.
    if self.attachment is not None and self.url_for_extraction is not None:
        raise AssertionError(
            "attachment and url_for_extraction are mutually exclusive"
        )

    # Phase 6e: scheduler-origin audio not supported.
    is_audio = (
        (self.attachment_kind in AUDIO_KINDS)
        or self.url_for_extraction is not None
    )
    if is_audio and self.origin == "scheduler":
        raise AssertionError(
            "scheduler-origin audio/URL turns are not supported "
            "(phase 6e: bg dispatch has no caller to dead-letter)"
        )
```

Test rewrite (researcher confirmed via exhaustive `tests/*.py`
grep — only ONE fixture needs deletion):
- DELETE `tests/test_phase6c_fixpack.py:431-460`
  (`test_scheduler_origin_audio_turn_passes_scheduler_note`).
- ADD `tests/test_phase6e_message_construction.py`:
  - `test_scheduler_origin_audio_rejected_at_construction` (ogg
    attachment).
  - `test_scheduler_origin_url_extraction_rejected` (URL).
  - `test_telegram_origin_audio_passes` (control).

Side effect: `_handle_audio_turn`'s `scheduler_note` injection block
(`message.py:1149-1166`) becomes unreachable. Coder must DELETE it
during refactor (otherwise confusing dead branch).

This explicitly reverts F7 (phase 6c fix-pack scheduler-note
injection for audio path); F7 was relevant when audio path could be
scheduled, but Alt-B doesn't support that flow.

### 8. Pending-turn during 22-45 min window (single-turn semantics)

`load_recent` (`conversations.py:121`) filters `WHERE status='complete'`
— the in-flight pending voice turn is invisible to intermediate text
turns. Model sees:
- T0: owner sends voice. Pre-lock ack to chat (NOT a turn assistant
  row — adapter-level send only).
- T+5min: owner text turn fires. Bridge `load_recent` skips voice
  (still pending). Clean context. ✅
- T+22min: bg task completes voice turn. `complete_turn` writes meta.
- T+23min: owner text turn. `load_recent` includes voice (oldest of
  recent N). Coherent. ✅

### 9. Replay ordering (HIGH-3, accepted as carry-forward)

User marker is appended in bg `finally` AFTER assistant rows from
`bridge.ask` streaming. SQLite assigns `c.id` monotonically → marker
row gets a higher id than assistant rows → replay shows
`assistant: <reply>` BEFORE `user: [voice marker]`.

This is **pre-existing 6c behaviour** (`_persist_voice_user_row` is
already in `finally`). Alt-B does NOT regress this. Carry-forward to
phase 7+: optionally append placeholder user row at lock-time and
UPDATE on completion (requires new `ConversationStore.update_row`).

### 10. Boot orphan notify (MED-5 in scope)

**v3.1 fix (researcher RQ6)**: at `main.py:347` (where
`cleanup_orphan_pending_turns` is called), the adapter is NOT yet
constructed (it's built later at `main.py:391/400`). Place the
notify AFTER `await self._adapter.start()` (`main.py:482`),
mirroring the existing phase-6 subagent orphan notify
(`main.py:488-496`).

`cleanup_orphan_pending_turns` is indiscriminate (covers text,
photo, audio, file turns). Use generic wording, not audio-specific:

```python
# Right after await self._adapter.start() at main.py:482,
# alongside the existing subagent orphan notify (main.py:488-496):
if orphans > 0 and self._adapter is not None:
    self._spawn_bg(
        self._adapter.send_text(
            self._settings.owner_chat_id,
            f"⚠️ daemon перезапущен: {orphans} turn(s) прерван(ы). "
            "Если ждал результат — повтори запрос."
        )
    )
```

`orphans` variable is already in scope at line 482 (declared at
`main.py:347` from `cleanup_orphan_pending_turns` return).

### 11. What stays unchanged

- `TranscriptionService` httpx client + Mac whisper-server FastAPI
  + SSH reverse tunnel.
- `_compose_voice_user_text` (already 6c hotfix-2).
- `bridge.ask` internals.
- 6a/6b paths (PDF/photo) UNCHANGED.
- Phase 6 subagent path UNCHANGED.
- 3-hour duration cap, save trigger regex, vault threshold,
  Russian default captions.
- `_send_pre_lock_ack` callers (telegram.py:256, 845, 924, 1042).
- Picker bridge `max_concurrent=2` (no change).
- User bridge `max_concurrent=2` (no change).
- `(пустой ответ)` fallback for non-audio paths.

### 12. What changes

- NEW `Daemon.spawn_audio_task(job: AudioJob)` registered via
  `_bg_tasks`.
- NEW `Daemon._audio_persist_pending` task set + drain in
  `Daemon.stop` (mirrors `_sub_pending_updates`).
- NEW `AudioJob` dataclass.
- `_handle_audio_turn` split: `_dispatch_audio_turn` (in lock,
  ~50 ms) + `_run_audio_job` (in bg). DELETE dead `scheduler_note`
  injection in audio branch (`message.py:1149-1166`).
- NEW `Handler.handle(msg, emit, emit_direct=None)` signature.
- NEW `audio_bridge` ClaudeBridge instance + `max_concurrent_override`
  init kwarg.
- NEW config: `claude.audio_max_concurrent: int = 1`,
  `audio_bg.drain_timeout_s: float = 5.0`.
- NEW `AUDIO_KINDS: frozenset[str]` constant in `adapters/base.py`.
- NEW `IncomingMessage.__post_init__` rejection of scheduler-origin
  audio.
- DELETE F7 test; ADD 3 construction-rejection regression tests.
- NEW boot orphan notify (placed AFTER `adapter.start()` at
  `main.py:482`, alongside existing subagent orphan notify).
- Adapter `_dispatch_audio_turn` updated: no chunks fallback, passes
  `emit_direct` (exception-suppressed for shutdown safety).

## Acceptance criteria

- AC#1 — owner records 30-sec voice → ack immediately → bg task ~10s
  → result + marker delivered.
- AC#2 — owner records 5-min voice → ack → release lock → owner sends
  `/ping` → bot answers immediately → ~30s later voice result arrives.
- AC#3 — owner sends 1-hour podcast URL → ack → release → owner does
  multiple turns over 15 min (user bridge slots free) → bg result +
  summary + vault path arrive.
- AC#4 — voice with caption "сохрани в проект альфа" → bg saves to
  `vault/proekt_alfa/...`; marker reflects path.
- AC#5 — 4-hour content → handler rejects pre-dispatch.
- AC#6 — Mac offline → adapter health-check fails BEFORE dispatch →
  reject reply, no bg task.
- AC#7 — bg task fails mid-transcribe → owner gets Russian fail
  message via `emit_direct`; turn completed with stop_reason
  `transcription_failed`.
- AC#8 — `Daemon.stop` mid-bg-task within drain timeout → shielded
  `interrupt_turn` succeeds; on boot reaper is a no-op.
- AC#9 — owner sends 3 voices back-to-back → 1 runs, 2 queue on
  `audio_bg_sem(1)`. Owner gets ack for all 3 within 3 sec.
- AC#10 — intermediate text turn during pending voice — `load_recent`
  skips pending voice; clean context.
- AC#11 — voice + text turn parallel — user_bridge has 2 slots free
  (audio uses audio_bridge); both fire concurrently; owner sees text
  reply within seconds.
- AC#12 — scheduler-origin trigger with audio attachment → rejected
  at `IncomingMessage` construction (`AssertionError`).
- AC#13 — daemon boot after crash with N pending audio turns → owner
  receives orphan notify with N count.
- AC#14 — phase 6a/6b/6c-non-audio paths unchanged + GREEN.
- AC#15 — phase 6 subagent infra unchanged + GREEN; picker bridge
  semaphore behaviour unaffected.
- AC#16 — VPS post-deploy `docker stats` shows daemon RSS ≤ 1.3 GB
  during AC#11 (voice + text parallel) for 5 min observation window.
  Memory invariant (researcher CRIT-1 conservative estimate gives
  worst-case 1050-1650 MB; container limit is 1500m).

## Open questions for researcher

- **RQ1**: `bridge.ask` does NOT re-load current-turn rows during
  streaming (`history` is parameter; `user_text` is parameter). Verify
  in `bridge/claude.py:328-347` and `bridge/history.py`.
- **RQ2**: `asyncio.shield` semantics under `Daemon.stop` cancel — does
  shield protect through nested awaits in `_persist`? aiosqlite worker
  thread interaction. Verify via prototype or aiosqlite source.
- **RQ3**: Mac whisper-server FastAPI — single-instance assumption for
  `audio_bg_max_concurrent=1` default. Check `~/whisper-server/` source
  on Mac via Bash.
- **RQ4**: `audio_bridge` separate ClaudeBridge instance — does it need
  separate OAuth credentials, or share `~/.claude/.credentials.json`?
  Phase 6 picker bridge already shares; precedent says "shares OAuth".
  Confirm for audio_bridge.
- **RQ5**: CancelledError propagation through httpx multipart upload —
  bg cancel during `transcribe_file` should raise CancelledError into
  bg task's await within ~5s (httpx `__aexit__` flush). Verify with
  small probe.
- **RQ6**: boot orphan notify ordering — `_spawn_bg(adapter.send_text)`
  scheduled BEFORE `adapter.start()` completes. Does aiogram bot.session
  open during `start_polling` synchronously, or is there a window where
  send_text fails? Verify.

## Carry-forwards (debt → phase 7 / later)

- Owner-issued cancel of running bg audio job (no surface in this
  phase; same as 6c).
- Replay ordering fix (lock-time placeholder user row + UPDATE on
  completion).
- Progress feedback during transcribe (no streaming hook from Mac
  sidecar).
- ~~`audio_max_concurrent` could be raised once Mac sidecar
  parallelism is verified~~ → **CLOSED-NEGATIVE** (researcher RQ3):
  Mac whisper-server enforces hard `Semaphore(1)` for GPU-memory
  reasons (`/Users/agent2/whisper-server/main.py:60-61`); raising
  client-side cap is pointless until sidecar is rearchitected
  multi-instance.
- Multi-photo/multi-image batched analysis with same `_spawn_bg`
  pattern.
