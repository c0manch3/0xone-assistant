---
phase: 6c
title: Voice / audio / URL transcription via Mac mini Whisper sidecar
date: 2026-04-27
status: spec v1 — devil wave-1 closed (5 CRITICAL addressed), pre-researcher
prereqs: phase 6a (file uploads) + phase 6b (photo vision) shipped 2026-04-27
reference: /Users/agent2/Downloads/midomis-bot/plan/phase-7-voice-transcription/PLAN.md (770 LOC)
---

## Devil wave-1 closures (spec v1)

| # | CRITICAL | Decision |
|---|---|---|
| C1 | `memory_save_note` Python API не существует | Researcher спроектирует wrapper над `core.write_note_tx` (preserved_created, sanitize_body, sentinel-check, frontmatter `source:voice|url`, `duration_sec`, `lang:ru`). Single new public function `assistant/memory/store.py::save_transcript()`. RQ для researcher. |
| C2 | Initial ack механизм отсутствует | В `_on_voice/_on_audio` (и `_on_text` URL-route) send `await bot.send_message(chat_id, ack)` ДО `_handler.handle()` — обходит `chunks.append` accumulator. Lock acquired ПОСЛЕ ack, держится только на transcribe + Claude. Документирован как новый паттерн (не reuse 6a/6b). |
| C3 | Whisper 3600s vs Claude 300s mismatch | Новый `Settings.claude_voice_timeout: int = 900` (15 мин) для voice/url turn'ов. `bridge.ask(...)` принимает kwarg `timeout_override: int | None = None`. Default остаётся 300s; voice/url path передаёт 900s. |
| C4 | URL detection без whitelist | **Explicit trigger required.** URL транскрибируется ТОЛЬКО если message текст начинается с `транскрибируй <URL>` (или `/voice <URL>` slash command). Без trigger'а URL = обычный текст (передаётся в Claude как раньше, обрабатывается через `_URL_RE` для phase-3 installer hint). |
| C5 | Транспорт без bearer token | **Mandatory `Authorization: Bearer <secret>`** на всех FastAPI endpoints (`/transcribe`, `/extract`, `/health`). Secret генерится `setup-mac-sidecar.sh`, сохраняется в `~/.config/whisper-server/.env` на Mac + `~/.config/0xone-assistant/secrets.env` на VPS под `WHISPER_API_TOKEN`. **Hotfix 2026-04-27**: транспорт = SSH reverse tunnel (Mac → VPS) с restricted SSH key (`restrict,permitlisten="9000"`) — заменил Tailscale из-за конфликта с AmneziaVPN. SSH key restrictions = defence-in-depth (см. `whisper-server/README.md`). |

| # | HIGH | Decision |
|---|---|---|
| H4 | yt-dlp size cap отсутствует | yt-dlp invocation: `--format "bestaudio[filesize<500M]/bestaudio[abr<=128]" --max-filesize 500M -x --audio-format mp3`. Pre-check Mac disk free ≥ 2 GB через `shutil.disk_usage`; reject если меньше. |
| H5 | Auto-save vs explicit save двойная семантика | Если caption содержит regex `r"(?i)\b(сохрани|запиши|vault|note)\b"` → **disable auto-save**, передать caption + transcript в Claude как обычно, Claude сам вызовет `memory_write` @tool. Marker для history тогда `[voice: D:DD | "<200 chars>" | (saved by user-request)]`. |
| H6 | Empty caption + voice ≤2 min intent unclear | При empty caption + duration ≤2 min, prefix transcript intent-hint: `user_text = f"[голосовое от owner — отвечай если это вопрос/задача, или просто 'принято' если это reminder/заметка]\n\n{transcript}"`. Дешёво; решает 80% UX edge-case'ов. |
| H7 | Adversarial transcript sentinel-injection | Wrapper `save_transcript()` ВСЕГДА вызывает `core.sanitize_body(body, max_body_bytes)` перед `write_note_tx`. Если sentinel найден в transcript — replace на `[redacted-tag]` + log warning, retry sanitize. Никогда не reject save. |
| H8 | yt-dlp YouTube anti-bot breakage без auto-update | `setup-mac-sidecar.sh` устанавливает второй launchd plist `com.zeroxone.yt-dlp-update.plist` — daily job `~/whisper-server/.venv/bin/pip install -U yt-dlp`. Whisper-server startup проверяет `yt-dlp --version` >= минимальная (e.g. `2026.01.15`); если ниже — лог warning. |
| H9 | FastAPI без auth (= C5 mitigation, см выше) | Closed by C5 bearer token. |
| H1 | Ack message len cap | Single URL per message; ack truncates URL preview к 80 chars + `…` если длиннее. |
| H2 | `ChatActionSender.typing()` rate-limit + cancel risk | Replace `ChatActionSender.typing()` для voice path manual `asyncio.create_task(_periodic_typing(chat_id))` loop с `try/except aiogram errors: log; continue`. Cancel в finally. |
| H3 | `F.audio.mime_type` Optional fallback | Use suffix from `attachment_filename` AS PRIMARY, MIME as secondary fallback. ffmpeg always converts to WAV regardless. |

# Phase 6c — Voice / audio / URL transcription

## Goal

INPUT-only audio attachments and URL-extracted audio → transcribed text → standard Claude turn (or auto-summary for long-form). Owner sends Telegram voice / audio file / podcast URL → bot routes to Mac-mini Whisper sidecar → returns transcript → Claude processes. Long-form transcripts (>2 min) auto-saved to memory vault for cross-turn recall.

NO source-code changes to bridge/scheduler/memory/installer subsystems. Phase 6a Option B uniform extraction + phase 6b vision multimodal envelope preserved unchanged. New code path keyed by `attachment_kind in AUDIO_KINDS` BEFORE existing image / extractor branches.

## Architecture

- **Mac mini sidecar** — separate FastAPI service on owner's Apple Silicon Mac mini. NOT in Docker (mlx-whisper requires Metal/MLX direct access). Two endpoints:
  - `POST /transcribe` (multipart) — receives audio file → ffmpeg → mlx-whisper → JSON transcript
  - `POST /extract` (JSON `{url: ...}`) — yt-dlp downloads audio → ffmpeg → mlx-whisper → JSON transcript
  - `GET /health` — model loaded, ffmpeg available
- **SSH reverse tunnel** (hotfix 2026-04-27, replaces Tailscale): autossh on the Mac maintains `ssh -N -R 9000:localhost:9000 0xone@193.233.87.118`. VPS sshd `GatewayPorts yes` re-publishes that listener on the docker bridge (`172.17.0.1:9000`). Bot container reaches it via `host.docker.internal:9000` (compose `extra_hosts: host-gateway`). FastAPI binds to `127.0.0.1:9000` (loopback only). Authorized_keys uses `restrict,permitlisten="9000",permitopen=""` so the key is good for nothing else. Egress is normal port-22 SSH — works regardless of AmneziaVPN routing.
- **Bot side** — new `assistant/services/transcription.py` httpx client. Used from `_handle_locked` audio branch.

## Whisper model + tooling

- `mlx-community/whisper-large-v3-turbo` (~1.6 GB, 4x faster than large-v3 at ~98% accuracy on Russian).
- ffmpeg (Homebrew) for ogg/opus → WAV 16kHz mono pre-conversion.
- yt-dlp (pip) for URL extraction. Pinned with Renovate auto-update minor/patch (anti-bot countermeasures require regular bumps).
- launchd plist for autostart on Mac boot.

## Sources (Telegram)

- `F.voice` — native voice messages (`.ogg/opus`, ≤60 sec typically; longer if forwarded). Always synthetic filename `<uuid>__voice_<msg_id>.ogg`.
- `F.audio` — audio files with metadata (mp3/m4a/etc.). Original filename preserved.
- `_on_document` whitelist extended with `.mp3 .m4a .wav .ogg .opus` for "send as file" path (e.g. iPhone Voice Memos export).
- **URL detection in text messages**: regex match for any URL, route to `/extract` endpoint via yt-dlp. Full yt-dlp coverage (~1500 sites). yt-dlp itself decides which URLs are valid media sources.

## Long-form handling

- **Hard cap: 3 hours**. Reject longer with Russian reply «слишком длинная запись (>3 часа), разбей на части».
- **Always save to vault** when duration >2 min: full transcript persisted as `vault/<area>/transcript-YYYY-MM-DD-HHMM.md` via existing phase 4 `memory_save_note` interface (NOT @tool — direct call from handler). Default area `inbox`. Caption-driven area resolution: caption "проект альфа" → `vault/proekt_alfa/...`; empty caption → `inbox`.
- Marker format in conversations user-row: `[voice: D:DD | "<first 200 chars>" | vault: <path>]` (one line per audio attachment; for URL extraction marker reads `[transcript-url: <truncated url> | D:DD | "<first 200 chars>" | vault: <path>]`).
- For voice ≤2 min: NO vault save, marker simpler `[voice: D:DD | "<first 200 chars>"]`.

## Empty caption behavior (hybrid duration-based)

- **voice ≤2 min** → full transcript replaces `user_text` (Claude responds AS IF owner typed the transcript). Standard handler flow.
- **voice >2 min** → `user_text` = `"Сделай краткое саммари этого, выдели ключевые тезисы:\n\n<full transcript>"`. Auto-summary via Claude.
- **Non-empty caption** → caption + `\n\n<full transcript>` (caption preserved as user instruction, transcript as content).

## Mac offline behavior

- Pre-flight: bot calls `GET /health` before each transcribe; if fail (TimeoutException, ConnectError, non-200) → reject with Russian reply «транскрипция временно недоступна (Mac sidecar offline), перезапиши через минуту».
- NO queue, NO retry. Voice file boot-sweep'нется, owner re-records.

## UX during long transcribe

- **Initial ack message** sent immediately after voice/URL receipt + duration estimate:
  - For Telegram audio: bot reads duration from `message.voice.duration` / `message.audio.duration` / yt-dlp probe.
  - Reply: `"⏳ получил аудио 1:30:00, начинаю транскрибацию (~22 мин при 4x realtime)"`.
- aiogram `ChatActionSender.typing()` context manager wraps the entire transcribe + Claude call (auto-refreshes typing indicator every ~5 sec).
- On success: bot replies with Claude response (which is the answer/summary).
- Per-chat lock held throughout transcribe + Claude call (no concurrent owner messages from another client).

## Timeouts

- `whisper_timeout` = **3600s** (1 hour). Covers 3-hour audio at conservative 1x realtime.
- `yt_dlp_timeout` = **600s** (URL download portion only; Whisper portion uses `whisper_timeout`).
- `claude.timeout` unchanged (60s for the Claude turn after transcribe).
- aiogram polling: long-poll continues unaffected during transcribe (background task).

## Storage layout

- Flat `/app/.uploads/` (consistent with 6a/6b decisions Q7 v1).
- uuid prefix `<uuid>__<sanitized>.<ext>` gives uniqueness.
- Quarantine `/app/.uploads/.failed/` (shared with 6a) — `_handle_extraction_failure` pattern reused.
- Boot-sweep `_boot_sweep_uploads` UNCHANGED — top-level wipe + 7-day `.failed/` prune covers audio kinds.

## Existing infra reused (zero changes)

- `IncomingMessage.attachment` extended with new fields:
  - `attachment_kind` Literal extended: `"ogg" | "mp3" | "m4a" | "wav" | "opus"`.
  - `audio_duration: int | None` (seconds, from Telegram metadata or yt-dlp probe).
  - `audio_mime_type: str | None`.
- `AUDIO_KINDS: frozenset[str] = frozenset({"ogg", "mp3", "m4a", "wav", "opus"})`.
- Handler branch in `_handle_locked` BEFORE existing IMAGE_KINDS check:
  ```python
  if kind in AUDIO_KINDS or msg.url_for_extraction is not None:
      transcript_text = await transcription_service.transcribe(...)
      if duration > 2 * 60:
          vault_path = await memory_store.save_note(transcript_text, ...)
          marker = f"[voice: {fmt_duration(duration)} | \"{transcript_text[:200]}\" | vault: {vault_path}]"
          if not msg.text:
              user_text = f"Сделай краткое саммари:\n\n{transcript_text}"
          else:
              user_text = f"{msg.text}\n\n{transcript_text}"
      else:
          marker = f"[voice: {fmt_duration(duration)} | \"{transcript_text[:200]}\"]"
          user_text = msg.text or transcript_text
      # Standard bridge.ask call with user_text, marker added to user row
  ```

## New code

- `assistant/services/transcription.py` (~150 LOC) — `TranscriptionService` httpx client. Methods: `transcribe(audio_bytes, mime_type) -> TranscriptionResult`, `extract_url(url) -> TranscriptionResult`, `health_check() -> bool`. Sanitized error replies on timeout/connect/HTTP errors.
- `assistant/adapters/telegram.py` extension:
  - `_on_voice` handler — `F.voice` filter, download via `bot.get_file` + `download_file`, build `IncomingMessage(audio_file_id=..., audio_duration=..., audio_mime_type="audio/ogg")`.
  - `_on_audio` handler — `F.audio` filter, similar.
  - `_on_text` URL detection — regex `r"https?://\S+"`, if matched route to extract path.
  - `_on_document` whitelist extension for audio suffixes.
  - Initial ack `_send_initial_ack(chat_id, duration)` helper.
- `assistant/handlers/message.py` audio branch in `_handle_locked` (per snippet above).
- `whisper-server/` — separate subdirectory in this repo (not separate repo per midomis):
  - `main.py` — FastAPI with `/transcribe`, `/extract`, `/health`.
  - `config.py` — env-driven settings.
  - `requirements.txt` — fastapi, uvicorn, mlx-whisper, yt-dlp, python-multipart, pydantic-settings.
  - `com.zeroxone.whisper-server.plist` — launchd autostart plist.
  - `setup-mac-sidecar.sh` — bootstrap script (brew + pip + autossh + ed25519 keygen + plist install).
  - `README.md` — Mac mini setup walkthrough.
- `Settings` extension (`assistant/config.py`):
  - `whisper_api_url: str | None = None` (e.g. `http://host.docker.internal:9000`; cross-host reach via SSH reverse tunnel + GatewayPorts).
  - `whisper_timeout: int = 3600`.
  - `yt_dlp_timeout: int = 600`.
  - `voice_vault_threshold_seconds: int = 120`.
  - `voice_meeting_default_area: str = "inbox"`.
- `pyproject.toml`: new dep `httpx>=0.27,<1` (likely already present from prior phases — verify).

## Acceptance criteria

- AC#1 — owner records short voice (10 sec) → bot transcribes + Claude responds. End-to-end ≤30 sec.
- AC#2 — owner records long voice (3 min) → bot ack-message + transcribes + auto-summary + vault save. Vault note exists at expected path.
- AC#3 — owner sends iPhone Voice Memo m4a (10 min meeting) via "send as file" → transcribed + summary + vault.
- AC#4 — owner sends YouTube URL of 30-min lecture → bot ack + extracts + transcribes + summary + vault.
- AC#5 — Mac mini powered off → bot replies "Mac sidecar offline, перезапиши через минуту"; voice file boot-sweep'нется на следующем restart.
- AC#6 — owner sends 4-hour audiobook URL → reject "слишком длинная запись".
- AC#7 — owner sends voice with caption "переведи на английский" → transcript + caption combined → Claude translates.
- AC#8 — phase 6a regressions (PDF/DOCX/TXT/MD/XLSX) ALL GREEN.
- AC#9 — phase 6b regressions (photo vision + media_group) ALL GREEN.
- AC#10 — phase 1-5d regressions ALL GREEN.

## Open questions for researcher (RQ list)

- **RQ1** — mlx-whisper version + model download flow on M4. Compat with macOS Sequoia (15.x)? Cold-start latency on first call?
- **RQ2** — Transport layer Mac↔VPS. **Resolved (hotfix 2026-04-27)**: SSH reverse tunnel via autossh on Mac, VPS sshd `GatewayPorts yes` + `authorized_keys` `restrict,permitlisten="9000",permitopen=""`. Tailscale was the original answer but conflicts with AmneziaVPN.
- **RQ3** — yt-dlp anti-bot resilience 2026: cookie/oauth requirements for YouTube? PoToken extractor? Recommended dependency pin range.
- **RQ4** — ffmpeg invocation: spawn subprocess via asyncio vs synchronous (FastAPI thread pool). Edge cases with very short audio (<1 sec).
- **RQ5** — Telegram bot API audio download size limit confirmation (20 MB or larger?). What about audio files via "send as file" route?
- **RQ6** — yt-dlp output format selection: best audio (`bestaudio[ext=m4a]/best`)? extract-audio post-processor? mp3 vs m4a vs opus output?
- **RQ7** — mlx-whisper progress callbacks for `(c)` periodic feedback (deferred but worth knowing).
- **RQ8** — vault save invocation from handler — direct call to `memory_store.save_note` (sync API exists?) or via @tool? Preserves phase 4 invariants.
