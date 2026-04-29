---
phase: 6e
title: Voice/audio/URL transcription via subagent (lock-decoupled)
date: 2026-04-28
status: spec v0 — pre-devil-wave-1
prereqs: phase 6a/6b/6c shipped + phase 6 (subagent infra) shipped 2026-04-28
---

# Phase 6e — Lock-decoupled voice/audio/URL transcription

## Goal

Refactor `_handle_audio_turn` (phase 6c) so it delegates ALL of `(transcribe + Claude turn + vault save + marker persist)` to a background subagent. Per-chat lock is held for ~50 ms (the dispatch window). Owner can chat during 22 min Whisper / 45 min URL extract without blocking.

## Current state (phase 6c)

`_handle_audio_turn` in `handlers/message.py:~1000-1200`:
1. Reads `IncomingMessage.attachment` (audio path) or `url_for_extraction`.
2. Calls `transcription.transcribe(...)` or `extract_url(...)` — **blocks per-chat lock 22-45 min**.
3. Builds `user_text` per duration (intent prefix dropped in 6c hotfix-2; auto-summary prompt for >120s; caption + transcript otherwise).
4. Optionally `save_transcript()` to vault (>120s threshold).
5. Calls `bridge.ask(user_text=..., timeout_override=900)` — **blocks per-chat lock additional 1-15 min for Claude**.
6. Persists user-row marker `[voice: D:DD | seen: ... | vault: ...]`.
7. Emits assistant response chunks to owner.

Total locked time: 23-60 min on long content.

## Phase 6e architecture

### Audio @tool

New module `src/assistant/tools_sdk/audio.py` mirroring scheduler/memory @tool pattern:
- `transcribe_audio(path: str, mime_type: str | None = None) -> dict` — wraps `TranscriptionService.transcribe()`. Returns `{"text", "duration", "language"}`.
- `extract_url_audio(url: str) -> dict` — wraps `TranscriptionService.extract_url()`. Returns `{"text", "duration", "language", "title", "channel"}`.
- `save_transcript_to_vault(transcript_text: str, area: str, title: str, source: str, duration_sec: int, lang: str = "ru") -> dict` — wraps `memory.store.save_transcript()`. Returns `{"vault_path"}`.
- `configure_audio(transcription_service, vault_dir, index_db_path)` — boot-time wiring from `Daemon.start`.

Add to `worker` agent's `tools` list. Optional: also `general` (more autonomy).

### Handler refactor

`_handle_audio_turn` becomes ~30 LOC:

1. **Pre-flight**: `transcription.health_check()` → if fail, send Russian "Mac sidecar offline" reply, complete turn, return. (Stays in handler so we don't burn a subagent for a known-offline state.)

2. **Build delegation prompt** based on `IncomingMessage` content:
   - Single audio file: `"Транскрибируй аудио по пути {path} (mime={mime}, duration={dur} sec). После получения transcript: {auto_summary_or_caption_action}. Если duration>120s — сохрани полный transcript в vault через save_transcript_to_vault. В конце ответа добавь маркер `[voice: D:DD | seen: <первые 200 chars>]` чтобы я понял что произошло (этот маркер уйдёт owner'у)."`
   - URL extraction: same shape with `extract_url_audio(url)`.
   - Caption-driven action: pre-determined per current 6c logic (empty → caption-only/summary; non-empty → caption + transcript).

3. **Spawn subagent**: `subagent_spawn(kind="worker", task=delegation_prompt)` from inside the handler turn. The handler IS the main turn at this point — it can call the @tool directly via the bridge (or via a new `subagent_store.record_pending_request` direct call to skip the model loop).

4. **Persist user-row marker** synchronously: `[voice: D:DD | seen: (delegated to job N) | vault: (pending)]`. Marker provides forensic trail; vault path filled by subagent post-completion.

5. **Send pre-lock ack** (already done): `"⏳ получил аудио D:DD, начинаю транскрибацию (~Y мин при 4x realtime); ответ придёт отдельным сообщением"`.

6. **Complete turn** with stop_reason `delegated_to_subagent`. Release per-chat lock.

7. **Subagent flow** (in worker SDK session):
   - Reads task prompt.
   - Calls `transcribe_audio(path, mime)` or `extract_url_audio(url)`.
   - Builds final text (auto-summary or direct response per caption).
   - If duration > 120s: calls `save_transcript_to_vault(...)` → gets `vault_path`.
   - Final assistant turn = answer + marker line.
   - SubagentStop hook delivers to owner via `adapter.send_text(callback_chat_id, body)`.

### Direct picker dispatch (skip model spawn loop)

Per devil's likely concern: spawning subagent VIA `subagent_spawn` @tool means handler's main Claude turn runs through SDK. That itself takes ~3 sec (SDK init + first turn).

Alternative: handler calls `subagent_store.record_pending_request(kind="worker", task=prompt, callback_chat_id=chat_id, spawned_by_kind="telegram", spawned_by_ref=str(message_id))` directly. Picker picks up on next tick. Skips main Claude turn entirely.

**Trade-off:** direct path = ~50 ms handler latency (DB INSERT) vs ~3 sec via @tool. Direct also bypasses per-chat history (handler turn skipped from `conversations` table — no audit). For audit trail, owner row marker `[voice: ... | (delegated job N)]` carries enough.

**Recommend: direct path** — picker dispatches, owner unblocked instantly.

### Marker semantics

Two markers per voice/audio/URL:

1. **At dispatch time** (handler synchronous):
   - `[voice: D:DD | (job N delegated to worker)]`

2. **After subagent completes** (SubagentStop hook in subagent's SDK session, NOT main):
   - `[voice: D:DD | seen: <transcript-200-chars> | vault: <path>]` appended to user row OR persisted as separate user marker row.

Either model emits marker as part of its final assistant text + handler+hook persists it. Coder picks idiom.

### Vault path race

`save_transcript_to_vault` runs INSIDE subagent. If subagent crashes mid-save, vault row may be partial OR incremental fields written out of order. Phase 4 vault_lock + atomic write protect against torn writes; subagent's failure → SubagentStop with `task_status=failed` → owner gets failure notify. Acceptable.

## What stays unchanged

- `TranscriptionService` httpx client.
- Mac whisper-server FastAPI.
- SSH reverse tunnel architecture.
- Per-chat lock pattern (just held for shorter duration in audio path).
- Pre-lock ack message.
- Manual `_periodic_typing` task — DROPPED for audio path now (lock released, no need to refresh typing for 22 min).
- 6a/6b paths (PDF/photo) UNCHANGED.

## Acceptance criteria

- AC#1 — owner records 30-sec voice → bot ack → subagent runs ~10 sec → result + marker delivered. Total: ~12 sec.
- AC#2 — owner records 5-min voice → bot ack → release lock → owner sends `/ping` → bot answers immediately. ~30 sec later subagent result arrives.
- AC#3 — owner sends 1-hour podcast URL → bot ack → release lock → owner does multiple turns over 15 min → subagent finishes ~15 min later, result + summary + vault path arrive.
- AC#4 — owner sends voice with caption "сохрани в проект альфа" → caption-driven save trigger; subagent saves to `vault/proekt_alfa/...`; marker reflects path.
- AC#5 — owner sends 4-hour content → handler rejects pre-dispatch (3-hour cap unchanged).
- AC#6 — Mac offline (sidecar down) → handler health-check fails BEFORE dispatch → reject reply, no subagent spawned.
- AC#7 — subagent fails mid-transcribe (Whisper crash) → owner gets notify "transcribe failed".
- AC#8 — phase 6a (PDF), 6b (photo), 6c-non-audio paths unchanged + GREEN.
- AC#9 — phase 6 subagent infra (general spawn / scheduler / cancel) unchanged + GREEN.

## Open questions for researcher

- **RQ1**: subagent's SDK session — does it need separate OAuth credentials? Or shares parent's? Verify.
- **RQ2**: `transcribe_audio` @tool — should it stream progress events back via `TaskNotificationMessage` (typing indicator) or just block subagent's turn until result?
- **RQ3**: `save_transcript_to_vault` already partial fix-pack F5'd into `memory/store.py`. Just exposing as @tool — minimal new code. Or is there a different idiom for direct-call vs MCP?
- **RQ4**: direct-picker-dispatch path bypasses `_handle_locked` history audit. Is that acceptable? Phase 6 has `Origin = "picker"` literal added but never used (devil M-1 carry-forward).
- **RQ5**: subagent transcript file path security — `transcribe_audio(path)` accepts arbitrary path from prompt. Validate `path.resolve().is_relative_to(uploads_dir)` inside @tool. Same guard as handler.

## Carry-forwards (debt → phase 7 / later)

- Voice transcription progress feedback (TaskNotificationMessage stream) — currently typing indicator dropped; owner sees only final notify. Could add periodic progress notify ("transcribed 50%, continuing") if Whisper exposes streaming.
- Multi-photo / multi-image batched analysis through subagent (mirror voice pattern for 6b heavy media_groups).
