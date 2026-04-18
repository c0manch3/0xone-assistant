# Phase 7 — детальный план (Media tools поверх phase-6 subagent infra)

Phase 7 расширяет бот с текстового на мультимедийный контур: голос/фото/документы входят через Telegram, PNG/PDF/DOCX/MP3 выходят. Тяжёлая нагрузка (mlx-whisper, mflux) живёт вне репо на хостовом Mac и проксируется через SSH reverse tunnel — VPS-демон поднимает только тонкие HTTP-клиенты. Долгие медиа-задачи (>30 с) делегируются в фоновый **worker subagent** через phase-6 picker; main turn остаётся свободен для диалога. Новый shared helper `dispatch_reply` детектит абсолютные пути к артефактам в тексте ассистента и отправляет их как photo/document/audio — этот helper используется в трёх местах (TelegramAdapter._on_text, SchedulerDispatcher._deliver, subagent SubagentStop hook), закрывая единую delivery-поверхность для всех turn-инициаторов.

## 0. Mental model — что делает SDK / phase-6 / phase-7

См. §13 в plan/phase6/detailed-plan.md для precedent; phase 7 добавляет:
- Входящий Telegram update → TelegramAdapter media handlers (voice/photo/document/audio/video_note)
- `media/download.py` → `<data_dir>/media/inbox/<chat_id>/<msg_id>.<ext>`
- `MediaAttachment` dataclass tuple в `IncomingMessage.attachments`
- Photo → SDK content-block `{"type":"image","source":{"type":"base64",...}}` (Spike 0 verifies)
- Voice/audio/document → system-note с path
- Модель решает: inline CLI (короткие) vs `task spawn --kind worker` (долгие)
- Thin HTTP CLI: `tools/transcribe/` (→ localhost:9100/mlx-whisper), `tools/genimage/` (→ localhost:9101/mflux)
- Local CLI: `tools/extract-doc/` (pypdf/python-docx/openpyxl/striprtf), `tools/render-doc/` (fpdf2+DejaVu/python-docx/stdlib)
- Артефакт (PNG/PDF/DOCX/MP3) в `<data_dir>/media/outbox/<uuid>.<ext>`
- Детект в тексте ассистента + `send_photo/document/audio` через новый `adapters/dispatch_reply.py`
- Retention sweeper (14d/7d/2GB LRU) piggyback на phase-3 `_sweep_run_dirs`
- Phase-4 `_memlib` sys.path pattern → полный Python package (Q9a tech debt close)

**Phase-7 LOC:** ~2030 src + ~730 modified + ~1460 tests = ~4220 total. 21% reduction vs pre-phase-6 projection — subagent infra не дублируется. Rev-2 delta: +30 LOC dispatch_reply (dedup ledger), +60 LOC memlib regression tests, +~40 LOC modified tests/conftest/system_prompt for memlib scope expansion.

## 1. Phase-6 invariants preserved

Phase 7 НЕ модифицирует `subagent/store.py`, `subagent/picker.py`, `subagent/definitions.py`, migration v4. Единственное изменение в `subagent/` — `adapter.send_text(...)` на `dispatch_reply(...)` в `on_subagent_stop`.

1. SDK version pin `0.1.59`.
2. `subagent_jobs` schema v4 — без изменений.
3. SubagentRequestPicker + AgentDefinition registry — `worker` kind primary transport для async media.
4. `_pending_updates` shield drain pattern в `Daemon.stop()`.
5. CURRENT_REQUEST_ID ContextVar.
6. PreToolUse cancel-flag gate.
7. recover_orphans.

## 2. Spike 0 (BLOCKER) — SDK multimodal envelope

**Артефакт:** `spikes/phase7_s0_multimodal_envelope.py` + `spikes/phase7_s0_findings.md`.

### 2.1. Эмпирические вопросы

| # | Вопрос | Метод | Fallback |
|---|---|---|---|
| Q0-1 | SDK принимает mixed text+image в user envelope | 1 text + 1 image-base64 (64KB JPEG) + 1 system-note; query() | `MEDIA_PHOTO_MODE=path_tool` |
| Q0-2 | `media_type` значения (image/jpeg, image/png, image/webp) | Три query разных types | Filter subset в handler |
| Q0-3 | Max image size inline | 100KB→1MB→3MB→5MB→10MB | Cap `MEDIA_PHOTO_MAX_INLINE_BYTES=5MB` |
| Q0-4 | Multi-photo (Telegram album) | Envelope с 3 images | OUT OF SCOPE phase 7 (Q12) |
| Q0-5 | Photo + system_notes order preserved | Envelope: text → system-note → image → system-note2 | Re-order system_notes FIRST, images LAST |
| Q0-6 | History replay с prior photo | Turn 1 photo → turn 2 text → load_recent envelope shape | Store `[image: <path>]` placeholder, synthetic text note в replay |

### 2.2. Spike exit criteria

- PASS Q0-1, Q0-2 → `MEDIA_PHOTO_MODE=inline_base64` default
- FAIL Q0-1 → `path_tool` fallback (photo через отдельный vision CLI — НЕ shipped phase 7)
- Q0-3 → cap 5MB default
- Q0-4 → always OUT
- Q0-6 → `[image: <path>]` placeholder default

## 3. Per-CLI contracts (~300 LOC в этом разделе)

### 3.1. `tools/transcribe/main.py` (HTTP thin client)

`python tools/transcribe/main.py <path> [--language ru|en|auto] [--timeout-s 60] [--format text|segments] [--endpoint URL]`

| Flag | Default | Env override | Valid |
|---|---|---|---|
| `<path>` | — | — | abs, `.oga|.ogg|.mp3|.wav|.m4a|.flac` |
| `--language` | `auto` | `MEDIA_TRANSCRIBE_LANGUAGE_DEFAULT` | `ru`/`en`/`auto` |
| `--timeout-s` | `60` | `MEDIA_TRANSCRIBE_TIMEOUT_S` | 10..300 |
| `--format` | `text` | — | `text`/`segments` |
| `--endpoint` | — | `MEDIA_TRANSCRIBE_ENDPOINT` | http, localhost-only |

Implementation: stdlib urllib multipart POST на `http://localhost:9100/transcribe`.

Output: `{"ok":true,"text":"...","duration_s":...,"language":"..","segments":[]}`

Exit codes: 0/2/3/4/5.

Path-guard (CLI-level defense in depth): abs path, resolved under `<data_dir>/media/inbox/` OR `project_root`; file size cap 25MB; ext whitelist; endpoint localhost-only (SSRF mirror).

### 3.2. `tools/genimage/main.py` (HTTP thin client + quota)

`python tools/genimage/main.py --prompt TEXT --out PATH [--width 1024] [--height 1024] [--steps 8] [--seed N] [--timeout-s 120]`

| Flag | Default | Valid |
|---|---|---|
| `--prompt` | — (required) | ≤1024 UTF-8 bytes, no newlines |
| `--out` | — (required) | abs, under outbox, `.png`, hex-filename |
| `--width` | `1024` | {256,512,768,1024} |
| `--height` | `1024` | same enum |
| `--steps` | `8` | 1..20 |
| `--seed` | random | 0..2^31 |
| `--timeout-s` | `120` | 30..600 |

**Daily quota (Q11):** `<data_dir>/run/genimage-quota.json` (format: `{"date":"YYYY-MM-DD","count":N}`); flock-protected; `MEDIA_GENIMAGE_DAILY_CAP=1` default; exit 6 at cap.

Output: `{"ok":true,"path":"<abs>","width":...,"height":...,"seed":...}`.

Exit codes: 0/2/3/4/5/6.

### 3.3. `tools/extract-doc/main.py` (local CLI)

`python tools/extract-doc/main.py <path> [--max-chars 50000] [--pages N-M]`

| Flag | Default | Valid |
|---|---|---|
| `<path>` | — | abs, ext `{.pdf,.docx,.xlsx,.rtf,.txt}` |
| `--max-chars` | `50000` | 1000..500000 |
| `--pages` | all | `^\d+-\d+$` (PDF only) |

Deps: `pypdf>=4.0`, `python-docx>=1.0`, `openpyxl>=3.1`, `striprtf>=0.0.28`, `defusedxml>=0.7`.

Output: `{"ok":true,"text":"...","pages_seen":N,"truncated":bool,"source_ext":".pdf"}`.

Exit codes: 0/2/3 (invalid path/ext/zipbomb)/4/5.

Path-guards: abs, inbox OR project_root; size cap 20MB; ext whitelist; XML via `defusedxml`.

### 3.4. `tools/render-doc/main.py` (local CLI)

`python tools/render-doc/main.py --body-file PATH --out PATH [--title TITLE] [--font DejaVu]`

| Flag | Default | Valid |
|---|---|---|
| `--body-file` | — | abs, under `<data_dir>/run/render-stage/`, `.md`/`.txt` |
| `--out` | — | abs, under outbox, `.pdf`/`.docx`/`.txt` |
| `--title` | `"Document"` | ≤200 chars, no newlines |
| `--font` | `DejaVu` | only `DejaVu` (bundled TTF) |

Stage-dir pattern (phase 4): body >argv cap → model writes в stage-dir через Write → CLI reads через --body-file.

Deps: `fpdf2>=2.7`, `python-docx>=1.0`, DejaVu TTF bundled via package_data.

Output: `{"ok":true,"path":"<abs>","format":"pdf","size_bytes":N}`.

Exit codes: 0/2/3 (body exceeds 512KB, out exceeds 10MB)/4/5.

## 4. SKILL.md per tool (inline vs task spawn guidance)

### 4.1. skills/transcribe/SKILL.md

```yaml
---
name: transcribe
description: "Расшифровка голосовых сообщений и аудио-файлов. Короткие (<30s) — inline через Bash. Длинные (>30s) — через `task spawn --kind worker`. CLI работает через SSH-tunneled mlx-whisper."
allowed-tools: [Bash, Read]
---
```

Body: inline vs async threshold (duration_s=30s из system-note); path discipline (abs from attachments); примеры inline и async; границы (tunnel offline → exit 4); exit codes table.

### 4.2. skills/genimage/SKILL.md

```yaml
---
name: genimage
description: "Генерация изображений (mflux на Mac). ВСЕГДА через `task spawn --kind worker` — 30-120s. Daily cap=1, превышение=отказ."
allowed-tools: [Bash, Read]
---
```

### 4.3. skills/extract-doc/SKILL.md

```yaml
---
name: extract-doc
description: "Извлечение текста из PDF/DOCX/XLSX/RTF. Короткие (<20 pages PDF или <5MB) inline; длинные — task spawn."
allowed-tools: [Bash, Read]
---
```

### 4.4. skills/render-doc/SKILL.md

```yaml
---
name: render-doc
description: "Создание PDF/DOCX/TXT. Body через `Write` в `<data_dir>/run/render-stage/`, потом CLI --body-file."
allowed-tools: [Bash, Read, Write]
---
```

Note: `Write` разрешён; phase-3 file hook расширен чтобы stage-dir тоже writable (§8.6).

### 4.5. Defensive guidance — double-delivery mitigation (prompt-level)

**MANDATORY addition в каждый SKILL.md который документирует async-worker сценарий (genimage, transcribe, extract-doc >20pp, render-doc):**

После `task spawn --kind worker …` main turn's final text MUST NOT include абсолютный путь к артефакту — subagent доставляет его separately через `dispatch_reply` из `SubagentStop` hook. Используй фразировку без path:

> "готово, PDF скоро придёт отдельным сообщением."

Это prompt-level mitigation для double-delivery race (main turn + subagent оба видят артефактный path). §7 idempotency ledger — second line of defense. Оба уровня активны одновременно: prompt-guidance предотвращает mention в 95% случаев, ledger ловит оставшиеся 5% при model disobedience.

## 5. Adapter changes

### 5.1. MediaAttachment dataclass

`adapters/base.py` +40 LOC:

```python
@dataclass(frozen=True, slots=True)
class MediaAttachment:
    kind: Literal["voice","photo","document","audio","video_note"]
    local_path: Path
    mime_type: str | None = None
    file_size: int | None = None
    duration_s: int | None = None
    width: int | None = None
    height: int | None = None
    filename_original: str | None = None
    telegram_file_id: str | None = None
```

### 5.2. IncomingMessage extension

`IncomingMessage` +5 LOC: `attachments: tuple[MediaAttachment, ...] | None = None`.

### 5.3. MessengerAdapter abstracts

+15 LOC: `send_photo`, `send_document`, `send_audio` — все с `TelegramRetryAfter` retry pattern.

### 5.4. TelegramAdapter handlers (+200 LOC)

Register 5 новых filters: `F.voice`, `F.audio`, `F.photo`, `F.document`, `F.video_note`. Download → `MediaAttachment` → pass в handler.

Caps (adapter-level, before download): voice 30min, photo 10MB, doc 20MB, audio 50MB. Over → reply "файл слишком большой" + return.

send_photo/document/audio retry loop mirror send_text.

### 5.5. Media download helper

`media/download.py` (~80 LOC): `download_telegram_file(bot, file_id, dest_dir, suggested_name, max_bytes)` — pre-fetch size check + write to uuid-filename.

### 5.6. Media path resolver

`media/paths.py` (~40 LOC): `inbox_dir`, `outbox_dir`, `stage_dir`, `ensure_media_dirs`.

`Daemon.start()` вызывает `ensure_media_dirs` рядом с `_ensure_vault()`.

## 6. Handler + bridge multimodal envelope

### 6.1. Handler — attachments → system-notes / image-blocks

`handlers/message.py` +60 LOC.

```python
image_blocks = []
if msg.attachments:
    for att in msg.attachments:
        if att.kind == "photo" and settings.media.photo_mode == "inline_base64":
            if att.file_size > cap:
                notes.append(f"photo too large...")
                continue
            b64 = base64.b64encode(att.local_path.read_bytes()).decode()
            image_blocks.append({"type":"image","source":{"type":"base64","media_type":att.mime_type,"data":b64}})
            notes.append(f"user attached photo at {att.local_path} ({att.width}x{att.height})")
        elif att.kind in ("voice","audio"):
            notes.append(f"user attached {att.kind} (duration={att.duration_s}s) at {att.local_path}. use tools/transcribe/; if >30s spawn worker.")
        elif att.kind == "document":
            notes.append(f"user attached document '{att.filename_original}' at {att.local_path}. use tools/extract-doc/.")
        elif att.kind == "video_note":
            notes.append(f"user attached video_note (duration={att.duration_s}s) at {att.local_path}. video out of scope phase 7.")
```

### 6.2. Bridge — mixed content envelope

`bridge/claude.py::ask(..., image_blocks: list[dict] | None = None)` +25 LOC. В `prompt_stream`:
```python
if system_notes or image_blocks:
    content_blocks = [{"type":"text","text":user_text}]
    for blk in image_blocks or []:
        content_blocks.append(blk)  # image AFTER text, BEFORE notes
    for note in system_notes or []:
        content_blocks.append({"type":"text","text":f"[system-note: {note}]"})
    user_content = content_blocks
else:
    user_content = user_text
```

Order: text → image(s) → system-notes. Spike 0 Q0-5 verifies.

### 6.3. History replay для photo

`bridge/history.py` +20 LOC: image row → synthetic text note `[user attached photo at <path> on turn N]`. Raw bytes не replay'им.

### 6.4. MEDIA_PHOTO_MODE=path_tool fallback

Если Spike 0 FAIL: handler НЕ строит image_blocks, только system-note с path. Модель вызывает отдельный vision tool (phase 7 НЕ shippит — owner decides).

## 7. dispatch_reply shared helper (~160 LOC; wired в 3 paths)

**Центральный новый модуль** phase 7. `adapters/dispatch_reply.py`:

```python
from collections import OrderedDict

_PHOTO_EXT = (".png",".jpg",".jpeg",".webp")
_AUDIO_EXT = (".mp3",".ogg",".oga",".wav",".m4a",".flac")
_DOC_EXT = (".pdf",".docx",".txt",".xlsx",".rtf")
_ALL_EXT = _PHOTO_EXT + _AUDIO_EXT + _DOC_EXT

_ARTEFACT_RE = re.compile(
    r"(?<![\w/])(/[^\s`\"'<>()\[\]]+"
    rf"(?:{'|'.join(re.escape(e) for e in _ALL_EXT)}))"
    r"(?![\w/])",
    re.IGNORECASE,
)

_DEDUP_TTL_S = 300.0
_DEDUP_MAX_ENTRIES = 256


class _DedupLedger:
    """Per-daemon in-process ledger of recently dispatched artefacts.

    Key: (resolved_path_str, chat_id). Value: monotonic timestamp of last send.
    TTL: 300s sliding window; LRU-capped at 256 entries (~25KB RAM).
    Race window covered: main turn final text + SubagentStop hook both
    mentioning same path within ~seconds.
    """

    def __init__(self, *, ttl_s: float = _DEDUP_TTL_S, max_entries: int = _DEDUP_MAX_ENTRIES) -> None:
        self._entries: OrderedDict[tuple[str, int], float] = OrderedDict()
        self._ttl_s = ttl_s
        self._max_entries = max_entries

    def mark_and_check(self, key: tuple[str, int], now: float) -> bool:
        """Return True if key was seen within TTL (caller SKIPS send);
        otherwise record key and return False.
        Side-effect: evicts expired + LRU-trims to max_entries.
        """
        self._evict_expired(now)
        if key in self._entries:
            last = self._entries[key]
            if now - last < self._ttl_s:
                # Refresh LRU position; signal seen
                self._entries.move_to_end(key)
                return True
        self._entries[key] = now
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return False

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, t in self._entries.items() if now - t >= self._ttl_s]
        for k in expired:
            del self._entries[k]


async def dispatch_reply(
    adapter,
    chat_id,
    text,
    *,
    outbox_root,
    dedup: _DedupLedger,
    log_ctx=None,
) -> None:
    """Extract media artefacts → send as photo/document/audio; send cleaned text.

    Path-guard: resolved.is_relative_to(outbox_root) AND exists().
    Dedup: (resolved_path, chat_id) seen within 300s → skip send BUT STILL
    strip raw token from text (invariant I-7.5).
    Error handling: send fail → log warning; file remains in outbox.
    """
    import time
    cleaned = text
    outbox_resolved = outbox_root.resolve()
    for raw in _ARTEFACT_RE.findall(text):
        try:
            resolved = Path(raw).resolve()
            if not resolved.is_relative_to(outbox_resolved):
                continue
            if not resolved.exists():
                continue
        except (OSError, ValueError):
            continue
        kind = _classify(resolved)
        key = (str(resolved), int(chat_id))
        now = time.monotonic()
        if dedup.mark_and_check(key, now):
            # Seen within TTL — skip network send, but STILL clean text
            # so user doesn't see raw path in the follow-up message.
            log.info("artefact_send_deduped", path=str(resolved), chat_id=chat_id)
            cleaned = cleaned.replace(raw, "")
            continue
        try:
            if kind == "photo":
                await adapter.send_photo(chat_id, resolved)
            elif kind == "audio":
                await adapter.send_audio(chat_id, resolved)
            elif kind == "document":
                await adapter.send_document(chat_id, resolved)
            cleaned = cleaned.replace(raw, "")
        except Exception:
            log.warning("artefact_send_failed", path=str(resolved), exc_info=True)
    if cleaned.strip():
        await adapter.send_text(chat_id, cleaned.strip())
```

**Ledger lifecycle:** одна инстанция на процесс daemon (`Daemon._dedup_ledger = _DedupLedger()`), передаётся в три call-site'а явным параметром. Это даёт cross-path coverage: main-turn-send + subagent-send видят один ledger и один из них будет skipped.

**Wired в 3 paths (все с dedup=self._dedup_ledger):**
1. `TelegramAdapter._on_text` — replace `send_text` на `dispatch_reply`
2. `SchedulerDispatcher._deliver` (dispatcher.py:216) — replace `send_text` на `dispatch_reply`
3. `subagent/hooks.py::on_subagent_stop` — `_deliver` replace `send_text` на `dispatch_reply` (shielded preserved)

**Invariant I-7.5:** `dispatch_reply` идемпотентен по `(resolved_path, chat_id)` в sliding window 300s. Повторный вызов с тем же ключом — text cleaned, network send skipped. Ledger LRU-capped (256 entries) и TTL-expired на каждом mark_and_check.

## 8. Bash allowlist extension (_validate_media_argv)

`bridge/hooks.py` +280 LOC.

### 8.1. Router

```python
if script == "tools/transcribe/main.py":
    return _validate_transcribe_argv(argv[2:], project_root)
if script == "tools/genimage/main.py":
    return _validate_genimage_argv(argv[2:], project_root)
if script == "tools/extract-doc/main.py":
    return _validate_extract_doc_argv(argv[2:], project_root)
if script == "tools/render-doc/main.py":
    return _validate_render_doc_argv(argv[2:], project_root, data_dir)
```

**Factory signatures (keyword-default, backward-compat):**

```python
def make_pretool_hooks(
    project_root: Path,
    data_dir: Path | None = None,
) -> dict: ...

def make_bash_hook(
    project_root: Path,
    data_dir: Path | None = None,
): ...

def make_file_hook(
    project_root: Path,
    data_dir: Path | None = None,
): ...
```

Единственный callee требующий `data_dir != None` — `_validate_render_doc_argv` (ему нужен stage_dir = `<data_dir>/run/render-stage/`). Если `data_dir is None` и поступил аргумент для render-doc, hook возвращает explicit deny:

```json
{"hookSpecificOutput":{"permissionDecision":"deny","permissionDecisionReason":"render-doc requires data_dir-bound hooks; factory called without data_dir"}}
```

**Test-fixture inventory — backward-compat (9 existing test files unaffected because data_dir keyword-default is None):**

| Test file | Call site | Status |
|---|---|---|
| `tests/test_bridge_hooks_bash_guard.py` | `make_bash_hook(project_root)` | green (None default) |
| `tests/test_bridge_hooks_file_guard.py` | `make_file_hook(project_root)` | green |
| `tests/test_bridge_hooks_webfetch.py` | `make_pretool_hooks(project_root)` | green |
| `tests/test_bridge_hooks_integration.py` | `make_pretool_hooks(project_root)` | green |
| `tests/test_bridge_hooks_allowlist.py` | `make_bash_hook(project_root)` | green |
| `tests/test_handler_message_flow.py` | indirect через Daemon | green |
| `tests/test_daemon_startup.py` | `Daemon.start()` → updated internally | updated once |
| `tests/test_bridge_hooks_skills_path.py` | `make_pretool_hooks(project_root)` | green |
| `tests/test_bridge_hooks_bash_deny.py` | `make_bash_hook(project_root)` | green |

Новые phase-7 тесты `test_bash_hook_{transcribe,genimage,extract_doc,render_doc}_allowlist.py` ПЕРЕДАЮТ `data_dir=tmp_path / "data"` для render-doc и любых data_dir-sensitive кейсов.

**Daemon.start internal update** (single site):
```python
hooks = make_pretool_hooks(
    project_root=self._project_root,
    data_dir=self._settings.data_dir,
)
```

### 8.2-8.5. Per-tool validators

- transcribe: path ext whitelist; language enum; endpoint localhost-only (SSRF); dup-flag deny.
- genimage: --prompt ≤1024 UTF-8, no newlines; --out outbox + .png + hex; width/height enum; daily cap check at CLI not hook.
- extract-doc: path ext whitelist; max-chars range; pages regex; dup-flag.
- render-doc: --body-file stage-dir (refuses /etc/passwd); --out outbox + ext whitelist; --title no newlines; --font only DejaVu; dup-flag.

≥4 allow + ≥4 deny tests per tool.

### 8.6. File hook extension

`make_file_hook(project_root, stage_dir)` расширяет writable roots: `project_root` OR `<data_dir>/run/render-stage/`.

## 9. MediaSettings config

`config.py` +50 LOC:

```python
class MediaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEDIA_", ...)

    photo_mode: Literal["inline_base64", "path_tool"] = "inline_base64"
    photo_max_inline_bytes: int = 5_242_880
    photo_download_max_bytes: int = 10_485_760

    voice_max_sec: int = 1800
    voice_inline_threshold_sec: int = 30
    audio_max_bytes: int = 50_000_000

    document_max_bytes: int = 20_971_520

    transcribe_endpoint: str = "http://localhost:9100/transcribe"
    transcribe_language_default: str = "auto"
    transcribe_timeout_s: int = 60
    transcribe_max_input_bytes: int = 25_000_000

    genimage_endpoint: str = "http://localhost:9101/generate"
    genimage_daily_cap: int = 1
    genimage_steps_default: int = 8
    genimage_timeout_s: int = 120

    extract_max_input_bytes: int = 20_000_000
    render_max_body_bytes: int = 512_000
    render_max_output_bytes: int = 10_485_760

    retention_inbox_days: int = 14
    retention_outbox_days: int = 7
    retention_total_cap_bytes: int = 2_147_483_648
    sweep_interval_s: int = 3600
```

Wire: `Settings.media: MediaSettings = Field(default_factory=MediaSettings)`.

## 10. Retention sweeper

`media/sweeper.py` (~140 LOC):

```python
async def sweep_media_once(data_dir, settings, log) -> dict:
    """1. Age-based: inbox >14d, outbox >7d → unlink
       2. LRU: if total>2GB → evict oldest (outbox first, then inbox)
    """

async def media_sweeper_loop(data_dir, settings, stop_event, log) -> None:
    while not stop_event.is_set():
        try:
            stats = await sweep_media_once(...)
            if stats["removed_old"] or stats["removed_lru"]:
                log.info("media_sweep_done", **stats)
        except Exception:
            log.warning("media_sweep_failed", exc_info=True)
        await asyncio.wait_for(stop_event.wait(), timeout=settings.sweep_interval_s)
```

Daemon integration: `_media_sweep_stop = asyncio.Event()`; `_spawn_bg(media_sweeper_loop(...), name="media_sweep_loop")`; stop event set в `Daemon.stop`.

## 11. Phase-6 _memlib refactor (Q9a closes tech debt)

### 11.1. Target

- `tools/__init__.py` (new, 5 LOC empty package marker)
- `tools/<name>/__init__.py` (8 total — 4 existing tools + 4 new) — empty markers
- `tools/<name>/_lib/__init__.py` — rename from `_memlib/__init__.py` (consistent naming)
- CLI imports: `from tools.<name>._lib.foo import bar` (instead of `sys.path.insert` + `from _memlib.foo`)
- **Dir rename:** `tools/skill-installer/` → `tools/skill_installer/` (hyphen невалиден как Python identifier для `from tools.skill_installer import …`). Bash allowlist обновлён соответственно.

### 11.2. Invocation — Option A (pragmatic)

Оба entry-point'а должны работать:
- `python tools/<name>/main.py` — cwd-based (owner типично запускает так)
- `python -m tools.<name>.main` — canonical (tests + subprocess spawn)

**Implementation:** каждый `tools/<name>/main.py` начинается с explicit sys.path pragma:

```python
# tools/<name>/main.py — первые строки
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.<name>._lib.foo import bar  # now resolves in both modes
```

Это решает direct-invocation ambiguity: `__package__` пуст при прямом запуске, и sys.path insert делает project root impotrable. При `python -m …` `__package__` уже = `tools.<name>`, pragma no-op.

Bash hook `_PYTHON_ALLOWED_PREFIXES = ("tools/", "skills/")` — unchanged.

### 11.3. Full blast-radius inventory (27 files — atomic commit)

| # | Path | Change |
|---|---|---|
| 1 | `tools/__init__.py` | NEW (empty marker) |
| 2 | `tools/memory/main.py` | rewrite imports + add sys.path pragma |
| 3 | `tools/schedule/main.py` | rewrite imports + add sys.path pragma |
| 4 | `tools/task/main.py` | rewrite imports + add sys.path pragma |
| 5 | `tools/skill_installer/main.py` | rewrite imports + add sys.path pragma (NOTE: dir renamed) |
| 6 | `tools/memory/_lib/__init__.py` | rename from `_memlib/__init__.py` |
| 7 | `tools/memory/_lib/…` (all modules inside) | move from `_memlib/` to `_lib/` |
| 8 | `tools/schedule/_lib/__init__.py` | rename from `_memlib/__init__.py` |
| 9 | `tools/schedule/_lib/…` | move from `_memlib/` to `_lib/` |
| 10 | `tools/task/_lib/__init__.py` | rename from `_memlib/__init__.py` |
| 11 | `tools/task/_lib/…` | move from `_memlib/` to `_lib/` |
| 12 | `tools/skill_installer/_lib/__init__.py` | rename from `_memlib/__init__.py` |
| 13 | `tools/skill_installer/_lib/…` | move from `_memlib/` to `_lib/` |
| 14 | `tests/test_memory_cli_*.py` (7 files) | `from _memlib` → `from tools.memory._lib` |
| 15 | `tests/test_skill_installer_*.py` (8 files) | `from _memlib` → `from tools.skill_installer._lib` |
| 16 | `tests/test_task_subprocess_spawn.py` | drop embedded `sys.path.insert` hack; use canonical invocation |
| 17 | `pyproject.toml` | drop `_lib`, `_memlib` из `[tool.ruff.isort] known-first-party` |
| 18 | `tests/conftest.py` | drop `_INSTALLER_DIR`, `_MEMORY_DIR` shims |
| 19-22 | 4 × `skills/<name>/SKILL.md` | update `python tools/skill-installer/main.py` → `python tools/skill_installer/main.py` в body text |
| 23 | `src/assistant/bridge/system_prompt.md` | обновить skill-installer path mentions (hyphen → underscore) |
| 24-27 | misc doc references (README, plan/*) | spot-update any `skill-installer` path references |

**Atomic discipline:** всё в одном commit (commit #2 в §19.2). Если dry-run коммита ломает любой из ~15 затронутых test файлов — abort + split. Feature flag НЕ используется (один этап, idempotent на диске — git revert atomic).

### 11.4. Backward-compat strategy

НЕТ deprecation shim. Пользователь запускает CLI через **SKILL.md-prescribed** path string, который мы обновляем в том же commit'е. Single source of truth. Phase-6 tests переписаны в том же commit'е — green pre/post merge.

### 11.5. Test coverage — expanded

`tests/test_memlib_refactor_regression.py` расширяется с ~20 до **~60 LOC**. Параметризация 4 tools × 2 invocation forms = 8 smoke tests + import sanity:

```python
import subprocess
import sys
import pytest

TOOLS = ["memory", "schedule", "task", "skill_installer"]
INVOCATIONS = [
    ("cwd", lambda t: [sys.executable, f"tools/{t}/main.py", "--help"]),
    ("module", lambda t: [sys.executable, "-m", f"tools.{t}.main", "--help"]),
]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("mode,cmd_fn", INVOCATIONS)
def test_cli_invocation_works(tool, mode, cmd_fn, project_root):
    """Both `python tools/X/main.py --help` and `python -m tools.X.main --help`
    exit 0 with expected stdout anchor."""
    result = subprocess.run(
        cmd_fn(tool),
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"{tool}/{mode} failed: {result.stderr}"
    assert "usage:" in result.stdout.lower()


def test_import_sanity():
    """Package imports resolve without sys.path tricks (pytest sets them)."""
    from tools.memory._lib import config as mem_config  # noqa: F401
    from tools.schedule._lib import cron as sch_cron  # noqa: F401
    from tools.task._lib import spawn as task_spawn  # noqa: F401
    from tools.skill_installer._lib import manifest as si_manifest  # noqa: F401
```

**MUST land BEFORE any phase-7 tool commit** (§19 commit #2).

## 12. Integration с phase-6 SubagentStop hook + phase-5 SchedulerDispatcher

### 12.1. SchedulerDispatcher._deliver switch

`scheduler/dispatcher.py:216`:
```python
# Before:
if joined:
    await self._adapter.send_text(self._owner, joined)
# After:
if joined:
    await dispatch_reply(
        self._adapter, self._owner, joined,
        outbox_root=outbox_dir(self._settings.data_dir),
        log_ctx={"trigger_id": t.trigger_id, "schedule_id": t.schedule_id},
    )
```

### 12.2. subagent/hooks.py::on_subagent_stop switch

`subagent/hooks.py:270`:
```python
# Before:
await asyncio.shield(adapter.send_text(callback_chat_id, body))
# After:
await asyncio.shield(
    dispatch_reply(adapter, callback_chat_id, body,
                   outbox_root=outbox_root, log_ctx={"job_id": job_id})
)
```

Factory: `make_subagent_hooks(..., outbox_root: Path)` новый param. `Daemon.start` binds `outbox_root=outbox_dir(settings.data_dir)`.

### 12.3. Double-delivery race — two-level mitigation

Сценарий: owner запускает долгий `tools/genimage/` через `task spawn --kind worker`. Main turn видит абсолютный путь `<outbox>/abc123.png` (subagent ещё не закончил; path известен из worker CLI contract который генерит имя заранее). Если модель упомянет этот path в своём final text, `TelegramAdapter._on_text` → `dispatch_reply` отправит файл. Через несколько секунд `SubagentStop` hook отдельно вызовет `dispatch_reply` с тем же path'ом → второй send.

**Two-level mitigation:**

1. **Prompt-level (SKILL.md §4.5 + system_prompt.md):** модели явно сказано НЕ mention'ить absolute path в main-turn final text после `task spawn --kind worker`. Используется фраза "готово, PDF скоро придёт отдельным сообщением." Это cuts ≥95% race window (model follows guidance).

2. **Runtime idempotency (§7 dedup ledger):** `dispatch_reply` memoizes `(resolved_path, chat_id)` в 300s sliding window. Если оба call-site'а (main-turn + subagent-hook) видят один path — второй skipped (invariant I-7.5). Это защищает даже при model disobedience или скрытых retry paths.

Оба уровня активны одновременно — defense in depth.

### 12.4. Scheduler-media concurrency (Q13)

Scheduler-turn → `attachments=None` (scheduler никогда не инжектит media). Если scheduler-turn spawn'ит subagent + subagent вернул media path → `dispatch_reply` в `on_subagent_stop` доставит файл. Per-chat lock phase-2 serialises параллельные turns на одном chat_id. Dedup ledger общий на daemon — scheduler-turn + regular-turn не double-send один outbox path.

## 13. File tree

### 13.1. New (~2000 LOC)

| Путь | LOC | Role |
|---|---|---|
| `spikes/phase7_s0_multimodal_envelope.py` | 180 | Spike 0 |
| `spikes/phase7_s0_findings.md` | (md) | Verdict + decisions |
| `tools/__init__.py` | 5 | Package root |
| `tools/memory/__init__.py` | 0 | marker |
| `tools/schedule/__init__.py` | 0 | marker |
| `tools/skill_installer/__init__.py` | 0 | marker (dir renamed from `skill-installer`) |
| `tools/task/__init__.py` | 0 | marker |
| `tools/transcribe/__init__.py` | 0 | marker |
| `tools/transcribe/main.py` | 150 | HTTP client |
| `tools/transcribe/pyproject.toml` | 20 | deps |
| `tools/genimage/__init__.py` | 0 | marker |
| `tools/genimage/main.py` | 180 | HTTP client + quota |
| `tools/genimage/pyproject.toml` | 20 | deps |
| `tools/extract_doc/__init__.py` | 0 | marker (underscore for Python importability) |
| `tools/extract_doc/main.py` | 220 | Local extractor |
| `tools/extract_doc/pyproject.toml` | 25 | deps |
| `tools/render_doc/__init__.py` | 0 | marker (underscore for Python importability) |
| `tools/render_doc/main.py` | 200 | Local renderer |
| `tools/render_doc/_lib/DejaVuSans.ttf` | (binary) | ~700KB |
| `tools/render_doc/pyproject.toml` | 25 | deps |
| `skills/transcribe/SKILL.md` | 90 | manifest (+ §4.5 guidance) |
| `skills/genimage/SKILL.md` | 85 | manifest (+ §4.5 guidance) |
| `skills/extract-doc/SKILL.md` | 85 | manifest (+ §4.5 guidance) |
| `skills/render-doc/SKILL.md` | 95 | manifest (+ §4.5 guidance) |
| `src/assistant/media/__init__.py` | 5 | marker |
| `src/assistant/media/paths.py` | 50 | helpers |
| `src/assistant/media/download.py` | 110 | telegram download |
| `src/assistant/media/sweeper.py` | 140 | retention |
| `src/assistant/media/artefacts.py` | 30 | regex + classify |
| `src/assistant/adapters/dispatch_reply.py` | 160 | shared helper + dedup ledger (§7) |
| Tests (~21 files) | 1460 | see §14 (memlib regression expanded) |

**Bash allowlist**: `tools/extract_doc/main.py`, `tools/render_doc/main.py` (underscore) в router checks (§8.1). Hyphenated dir names оставлены только там где НЕ импортируются из Python (skills/ — markdown discovery, no import).

### 13.2. Modified (~700 LOC)

| File | Δ | Purpose |
|---|---|---|
| `adapters/base.py` | +60 | MediaAttachment + IncomingMessage.attachments + send_* abstracts |
| `adapters/telegram.py` | +200 | 5 handlers + 3 send methods |
| `handlers/message.py` | +60 | attachments → notes/image-blocks |
| `bridge/claude.py` | +25 | ask(..., image_blocks=...); envelope order |
| `bridge/history.py` | +20 | photo row → synthetic text |
| `bridge/hooks.py` | +280 | 4 validators + data_dir plumbing + stage_dir file_hook |
| `bridge/system_prompt.md` | +15 | attachments guidance |
| `config.py` | +50 | MediaSettings |
| `main.py` | +40 | ensure_media_dirs + sweep_bg + outbox_root |
| `scheduler/dispatcher.py` | +10 | dispatch_reply switch |
| `subagent/hooks.py` | +10 | dispatch_reply switch + outbox_root |
| `tools/memory/main.py` | -15/+15 | _lib import rewrite + sys.path pragma |
| `tools/schedule/main.py` | -15/+15 | same |
| `tools/skill_installer/main.py` | -15/+15 | same + dir renamed |
| `tools/task/main.py` | -10/+12 | sys.path rewrite + pragma |
| `pyproject.toml` | -3/+1 | drop `_lib`,`_memlib` from known-first-party |
| `tests/conftest.py` | -8/+0 | drop `_INSTALLER_DIR`,`_MEMORY_DIR` shims |
| `src/assistant/bridge/system_prompt.md` | ~3 | `skill-installer` → `skill_installer` references |

## 14. Tests (~1400 LOC, ~20 files)

### 14.1. Unit (~800 LOC)

- `test_media_attachment_dataclass.py` (50) — frozen + tuple
- `test_media_paths.py` (40)
- `test_media_download.py` (100) — bot mock + cap
- `test_media_sweeper.py` (120) — age + LRU + empty
- `test_dispatch_reply_regex.py` (130) — 20 positive + 15 negative
- `test_dispatch_reply_classify.py` (80)
- `test_dispatch_reply_path_guard.py` (100)
- `test_dispatch_reply_integration.py` (140) — mock adapter
- `test_dispatch_reply_dedup_ledger.py` (90) — TTL expiry + LRU eviction + cross-path (main+hook) dedup + text cleaning when send skipped
- `test_bash_hook_transcribe_allowlist.py` (30)
- `test_bash_hook_genimage_allowlist.py` (30)
- `test_bash_hook_extract_doc_allowlist.py` (30)
- `test_bash_hook_render_doc_allowlist.py` (50)

### 14.2. CLI (~400 LOC)

- `test_tools_transcribe_cli.py` (120) — urllib mock
- `test_tools_genimage_cli.py` (120) — urllib mock + quota
- `test_tools_extract_doc_cli.py` (80) — pypdf/docx fixtures
- `test_tools_render_doc_cli.py` (80) — fpdf2 smoke Cyrillic

### 14.3. Integration (~200 LOC)

- `test_telegram_adapter_media_handlers.py` (120)
- `test_handler_multimodal_envelope.py` (80)

### 14.4. Cross-system (~100 LOC)

- `test_scheduler_dispatch_reply_integration.py` (40)
- `test_subagent_hooks_dispatch_reply.py` (40)
- `test_memlib_refactor_regression.py` (60) — 4 tools × 2 invocation forms + import sanity (§11.5)
- `test_bash_hook_factory_backward_compat.py` (40) — `make_pretool_hooks(project_root)` (data_dir omitted) still builds hooks; render-doc argv → explicit deny

### 14.5. Phase-6 regression smoke

- `test_task_spawn_media_worker.py` (20)

**Total +125 tests over phase-6 765 baseline → ~890 total.**

## 15. Risk register (16 items)

| # | Risk | Sev | Mitigation |
|---|---|---|---|
| 1 | SDK multimodal envelope FAIL | RED | Spike 0 BLOCKER; `path_tool` fallback |
| 2 | SSH tunnel падает mid-transcribe | YEL | CLI timeout 60s + exit 4; subagent error notify |
| 3 | Host deps не установлены | YEL | CLI graceful fail; text-only functional не ломается |
| 4 | Disk fill media | YEL | Sweeper 14d/7d/2GB LRU |
| 5 | dispatch_reply regex FP | YEL | Path-guard + exists() + boundaries (spike verified) |
| 6 | Genimage spam | YEL | Daily cap=1 + exit 6 |
| 7 | Photo >5MB blow up context | GRN | Cap + system-note skip |
| 8 | History photo bloat | GRN | `[image:<path>]` placeholder |
| 9 | Scheduler subagent media artefact | GRN | dispatch_reply в scheduler path |
| 10 | Subagent не видит attachments | GRN | task spawn передаёт path в task string |
| 11 | Bash hook false deny | YEL | >=4 allow/deny tests per tool |
| 12 | _memlib refactor ломает existing | YEL | Regression test + full CI |
| 13 | Telegram RetryAfter burst | GRN | Cap preserved для send_* |
| 14 | fpdf2 DejaVu missing | YEL | Bundle as package_data |
| 15 | Parallel voice + scheduler race | GRN | Per-chat lock phase-2 |
| 16 | Double-delivery: scheduler parent-turn + worker-subagent оба ref same artefact path | YEL | Two-level mitigation: (a) prompt-level SKILL.md §4.5 "не mention'ить path", (b) §7 dedup ledger 300s sliding window (invariant I-7.5). Ledger shared per daemon process. |

## 16. Invariants (12 items)

1. `IncomingMessage.attachments` — tuple, not list.
2. `MediaAttachment.local_path` — ALWAYS abs, under `<data_dir>/media/inbox/<chat_id>/`.
3. Scheduler-origin → `attachments is None`.
4. Photo blocks AFTER text, BEFORE system-notes в envelope.
5. `dispatch_reply` refuses paths outside `outbox_root`.
6. `dispatch_reply` single-pass; multiple artefacts → individual sends.
7. Retention: age → LRU, outbox first.
8. `_memlib` refactor BEFORE any new tool.
9. CLI localhost-only SSRF mirror.
10. Genimage daily cap file flock-protected.
11. Photo inline max = 5MB; voice >30s → worker; PDF >20 pages → worker (thresholds в system_prompt + SKILL.md).
12. **I-7.5 — `dispatch_reply` idempotency:** helper идемпотентен по `(resolved_path, chat_id)` в sliding window 300s. Повторный вызов с тем же ключом: network send skipped, raw token всё равно удаляется из cleaned text. Ledger LRU-capped (256 entries), per-daemon shared across всех трёх call-site'ов (TelegramAdapter._on_text, SchedulerDispatcher._deliver, SubagentStop hook).

## 17. Skeptical notes

- Spike 0 BLOCKER: FAIL = `path_tool` + отдельный vision CLI. Ship phase 7 без photo-in если FAIL? Owner decides post-Spike-0.
- Thin HTTP != production: нет health-check tunnel before call; первый retry = full timeout. Phase 9 auto-reconnect.
- Daily cap=1 aggressive: env override `MEDIA_GENIMAGE_DAILY_CAP=10`.
- `_memlib → _lib` rename: rollback painful; full test run ДО merge.
- Retention sweeper mtime-based: user nostalgic click через 5 дней — файла нет.
- dispatch_reply regex: phase 9 может усилить до token-based если FP observed.
- Bash hook `data_dir` plumbing: `make_pretool_hooks(project_root, data_dir)` ripple в тесты.
- Scheduler-media: subagent из scheduler-turn возвращает path → dispatch_reply ловит.
- `worker` kind primary: `researcher` не нужен для media; `general` wasteful.

## 18. Open questions for Q&A

Locked:
| # | Q | Decision |
|---|---|---|
| Q-7-1 | SDK multimodal shape | Spike 0; default `inline_base64` |
| Q-7-2 | Photo max inline | 5MB (env override) |
| Q-7-3 | Voice inline threshold | 30s |
| Q-7-4 | Daily genimage cap | 1/day (env override) |
| Q-7-5 | Retention | 14d/7d/2GB LRU |
| Q-7-6 | Scheduler-to-media | dispatch_reply helper |
| Q-7-11 | Dedup strategy для double-delivery race | In-process LRU+TTL ledger: 256 entries, 300s sliding window, keyed on `(resolved_path, chat_id)`. Prompt-level SKILL.md §4.5 guidance complements runtime ledger. Ledger instance per-daemon, shared across все три dispatch_reply call-site'а. |
| Q-7-12 | `_memlib` refactor scope | 27 files atomic commit: 4 main.py rewrites + dir rename `skill-installer→skill_installer` + `_memlib→_lib` move (4 tools) + 15 test files import rewrites + pyproject.toml + conftest.py + 5 doc references. Option A (sys.path pragma в main.py) для cwd-invocation support. |
| Q-7-13 | Hook factory signature ripple | Keyword-default `data_dir: Path | None = None` на `make_pretool_hooks`, `make_bash_hook`, `make_file_hook`. 9 existing test files unaffected (keyword-default). Render-doc argv без `data_dir` → explicit deny. Один update site: `Daemon.start` passes `data_dir`. |
| Q-7-14 | CLI invocation form | Обе формы поддерживаются: `python tools/<name>/main.py` (cwd-invocation via sys.path pragma в main.py) AND `python -m tools.<name>.main` (canonical). Tests параметризованы по обеим (§11.5). |

Open (not blocking):
| # | Q | Status |
|---|---|---|
| Q-7-7 | Vision CLI fallback if Spike 0 FAIL | deferred; owner decides |
| Q-7-8 | Album support | NOT phase 7 (Q12) |
| Q-7-9 | OGG/Opus → WAV convert? | NO — whisper accepts OGG |
| Q-7-10 | Persist transcript → memory note? | deferred; user asks → model calls memory separately |

## 19. Commit order (для parallel-split agent'а)

### 19.1. Dependency graph (ASCII)

```
Spike-0 ────────────────────────────┐
                                    │
_memlib refactor (seq blocker)      │
       │                            │
       ├──> tools/transcribe/ ──┐   │
       ├──> tools/genimage/  ───┤   │
       ├──> tools/extract-doc/ ─┤   │  PARALLEL WAVE A (4 tools, <=4 agents)
       └──> tools/render-doc/  ─┘   │
                    │               │
                    ▼               │
            Bash allowlist  <───────┘  (depends on script paths)
                    │
                    │
MediaAttachment+IncomingMessage (adapters/base.py) ──┐
                    │                                │
                    ▼                                │
   media/ sub-package (paths/download/sweeper/art) ──┤  PARALLEL WAVE B
                    │                                │
   dispatch_reply.py (depends on adapter abstracts) ─┤
                    │                                │
   MediaSettings config ────────────────────────────┘
                    │
                    ▼
   TelegramAdapter handlers (depends on #3, #5)
                    │
                    ▼
   Handler + bridge multimodal envelope (depends on #3)
                    │
                    ├──> SchedulerDispatcher switch (depends on #5)
                    ├──> SubagentStop hook switch (depends on #5)
                    │
                    ▼
   Integration E2E tests
                    │
                    ▼
   Documentation update
```

### 19.2. Commits (19 total, with parallel wave markers)

| # | Commit | Depends | Wave |
|---|---|---|---|
| 1 | Spike 0 findings + report | — | standalone |
| 2 | _memlib refactor | #1 | seq BLOCKER |
| 3 | MediaSettings config | #2 | — |
| 4 | MediaAttachment + IncomingMessage + adapter abstracts | #2 | — |
| 5 | media/ sub-package | #3 | **Wave B** |
| 6 | adapters/dispatch_reply.py | #4 | **Wave B** (parallel #5) |
| 7 | tools/transcribe/ + skill | #2 | **Wave A** |
| 8 | tools/genimage/ + skill | #2 | **Wave A** |
| 9 | tools/extract-doc/ + skill | #2 | **Wave A** |
| 10 | tools/render-doc/ + skill | #2 | **Wave A** |
| 11 | Bash allowlist extension | #7,#8,#9,#10 | seq after Wave A |
| 12 | TelegramAdapter handlers + send methods | #4, #6 | — |
| 13 | Handler + bridge multimodal envelope | #4, #11 | — |
| 14 | SchedulerDispatcher → dispatch_reply | #6 | **Wave C** |
| 15 | SubagentStop → dispatch_reply | #6 | **Wave C** (parallel #14) |
| 16 | Daemon.start integration | #3, #5, #14, #15 | seq |
| 17 | Integration E2E tests | #11–#16 | — |
| 18 | Unit tests (20 files) | all code | **Wave D** (per-file parallel up to 12) |
| 19 | Documentation update | all | — |

### 19.3. Parallel wave breakdown

- **Wave A (post-Spike-0, post-memlib):** 4 CLI в отдельных dirs — полная изоляция. 4 agents × ~200 LOC = ~800 LOC parallel.
- **Wave B (post-config/adapter):** media/ + dispatch_reply — 2 independent files. 2 agents.
- **Wave C (post-dispatch-reply):** two one-line switches. 2 agents.
- **Wave D (tests):** per-file parallelisable up to 12.

### 19.4. Critical path

1 → 2 → (Wave A || Wave B) → 11 → 12 → 13 → (Wave C) → 16 → 17 → 19.

Без parallel: 19 sequential steps. With parallel: **~9 sequential waves**. Calendar savings ~50%.

### 19.5. Parallel-split agent manifest generator

Подробная спецификация sub-agent'а, который превращает §19.1 dependency graph + §19.2 commit table + этот раздел в concrete `wave-plan.md` с worktree-assignments и per-commit agent-prompts: см. [`plan/phase7/parallel-split-agent.md`](./parallel-split-agent.md).

Ключевые свойства (full details в parallel-split-agent.md):
- Read-only sub-agent (читает только implementation.md v2 + detailed-plan.md §19, пишет один файл `wave-plan.md`).
- Max 4 concurrent coders per wave (Q locked).
- Spike 0 и `_memlib` refactor — forced sequential (Wave 1, Wave 2).
- Wave 10 (tests) — параллельные partitions по 4.
- Fallback в sequential-coder режим через `PHASE7_PARALLEL_DISABLED=1` если parallel execution стабильно ломается.
- Mandatory human review `wave-plan.md` перед first merge.

## 20. Acceptance checklist

- [ ] Spike 0 completed; verdict PASS/FAIL documented
- [ ] `MEDIA_PHOTO_MODE` default reflects Spike 0 outcome
- [ ] `_memlib` refactor: existing 4 CLI работают из ОБОИХ entry points (cwd + module)
- [ ] `_memlib` regression: 8-case parametrized test (4 tools × 2 invocation forms) + import sanity green
- [ ] MediaAttachment tuple + IncomingMessage.attachments shipped
- [ ] 4 CLI tools pass unit tests
- [ ] TelegramAdapter media handlers + send_photo/document/audio work
- [ ] dispatch_reply detects outbox paths; send fail → text still delivered
- [ ] dispatch_reply dedup ledger: same `(path, chat_id)` within 300s → second call skips send, text still cleaned (I-7.5)
- [ ] dispatch_reply dedup ledger: TTL expiry + LRU eviction tested
- [ ] Bash hook factory backward-compat: `make_pretool_hooks(project_root)` без data_dir still works; render-doc argv → explicit deny
- [ ] Scheduler-media: subagent from scheduler returns outbox path → send_photo
- [ ] Subagent-media: task spawn worker → Stop hook → dispatch_reply → DOCX delivered
- [ ] Double-delivery race: main-turn mentions path + SubagentStop hook both fire → exactly ONE send (ledger dedup)
- [ ] SKILL.md §4.5 guidance present in all 4 phase-7 skills + system_prompt.md mentions "не mention'ить path после task spawn"
- [ ] Bash hook rejects all listed cases
- [ ] Retention sweeper: inbox >14d / outbox >7d / total >2GB → evicted
- [ ] Photo >5MB → skip + system-note
- [ ] Parallel user voice + scheduler trigger: per-chat lock preserved
- [ ] send_photo FileNotFoundError → WARN log + text unchanged
- [ ] Phase-6 regression: task spawn non-media OK
- [ ] Daily genimage cap=1: second call → exit 6
- [ ] ~890 tests passing; lint + mypy strict green
- [ ] SDK version pin 0.1.59 preserved
- [ ] parallel-split-agent.md spec present; wave-plan.md generation path documented (§19.5)

### Critical Files for Implementation

- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/base.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/telegram.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/dispatch_reply.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/handlers/message.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py
