# Phase 7 — Media tools (transcribe / genimage / extract-doc / render-doc)

## Цель

Расширить приёмный/выходной контур бота с чисто-текстового до мультимедийного: голос, фото, документы входят; PNG, PDF, DOCX, MP3 выходят. Тяжёлая вычислительная нагрузка (mlx-whisper, mflux) живёт на хостовом Mac и доступна VPS-демону через SSH reverse tunnel — VPS поднимает только тонкие HTTP-клиенты. Долгие медиа-задачи (>30 s) делегируются в фоновый **worker subagent** через phase-6 picker, так что main turn остаётся свободен для диалога.

## Вход

- **Phase 6 shipped:** SDK-native subagent infrastructure (`AgentDefinition` registry, `SubagentRequestPicker`, `subagent_jobs` ledger, SubagentStop hook → `adapter.send_text(OWNER_CHAT_ID, …)`). CLI picker — primary async path; native Task — синхронный RPC только для коротких делегаций. `worker` kind с `tools=["Bash","Read"]` готов к обвязке тонкими CLI-клиентами медиа-инструментов.
- **Phase 5 shipped:** `SchedulerLoop` + `SchedulerDispatcher` + shared `ConversationStore.lock` + `IncomingMessage(origin, meta)` + `_bg_tasks`/`_spawn_bg`.
- **Phase 4 shipped:** `_memlib` sys.path pattern, write-first stage-dir паттерн (`<data_dir>/run/memory-stage`).
- **Phase 3 shipped:** `_validate_python_invocation` dispatcher, `_BASH_PROGRAMS` allowlist, `_net_mirror` SSRF guard pattern.
- **Phase 2 shipped:** `IncomingMessage` + `MessengerAdapter` + per-chat lock.
- **Inherited tech debt:** `_memlib` consolidation (запланирован к закрытию в phase 7), `HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap (ОТЛОЖЕН — phase 9), per-skill `allowed-tools` enforcement (phase 9).
- **Q&A зафиксировано (phase-6 контекст, перенесено в phase 7):** Q1 mlx-whisper backend, Q2 mflux, Q3 photo inline base64 (Spike 0 verifies, fallback path_tool), Q4 `attachments: tuple[MediaAttachment, ...] | None`, Q5 четыре отдельных скилла, Q6 sweeper 14d/7d + 2GB cap, Q7 pypdf, Q8 regex + path-guard для outbound, Q9a `_memlib` рефакторим, Q10 thin CLI HTTP clients на VPS → whisper/flux servers на Mac via SSH tunnel, Q11 genimage cap 1/день + env override, Q12 album out-of-scope, Q13 scheduler-media через shared `dispatch_reply`, Q14 long audio ≥5 min → subagent, Q15 абсолютные пути для артефактов.

## Выход — пользовательские сценарии (E2E)

1. **Voice-in (>30 s) → async transcribe.** Владелец шлёт voice 5 минут → `TelegramAdapter._on_voice` скачивает в `<data_dir>/media/inbox/<chat>/<msg>.oga` → собирает `MediaAttachment(kind="voice", local_path=…, duration_s=300)` → handler передаёт в `IncomingMessage.attachments` → bridge формирует system-note "user attached voice (5m) at <path>" → модель видит длительность → решает делегировать: `python tools/task/main.py spawn --kind worker --task "transcribe /path/voice.oga via tools/transcribe/main.py --language ru"`. Main turn возвращает "окей, расшифровываю в фоне" за ~3 s. Picker подхватывает row → запускает worker subagent → subagent дёргает `python tools/transcribe/main.py /path/voice.oga --language ru` → CLI POST'ит multipart на `http://localhost:9100/transcribe` (SSH reverse tunnel на хостовый whisper-server) → JSON-ответ возвращается subagent'у → SubagentStop hook доставляет финальный текст владельцу.
2. **Voice-in (<30 s) → inline transcribe.** Тот же flow до handler'а; модель видит `duration_s=12` → решает inline: `Bash("python tools/transcribe/main.py …")` прямо в main turn → ответ через ~5 s.
3. **Photo-in → multimodal turn.** Владелец шлёт фото 1 МБ JPEG → `_on_photo` скачивает → handler формирует SDK content-block `{"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":<b64>}}` в текущем user envelope (Spike 0 verifies SDK 0.1.59 принимает) → модель отвечает "вижу X". Если Spike 0 fail — fallback `MEDIA_PHOTO_MODE=path_tool`: handler кладёт путь в system-note + опциональный custom vision CLI.
4. **Text-in → image-out.** Владелец: "нарисуй закат над морем" → модель решает long-task → `task spawn --kind worker --task "генерируй картинку 'закат над морем' через tools/genimage/main.py --out <data_dir>/media/outbox/<uuid>.png"` → main turn возвращается за 3 s → worker subagent дёргает CLI → CLI POST'ит на `http://localhost:9101/generate` (mflux-server на Mac) → возвращает `{"path": "/abs/.../uuid.png"}` → SubagentStop hook доставляет финальный текст с path → новый shared helper `dispatch_reply` детектит абсолютный путь под `<data_dir>/media/outbox/`, удаляет из текста, шлёт через `adapter.send_photo(chat_id, path)`.
5. **Document-in → саммари + render-doc → отправка файлом.** Владелец шлёт PDF 5 страниц → `_on_document` скачивает → handler прокидывает path в system-note → модель решает inline: `Bash("python tools/extract-doc/main.py /path/document.pdf --max-chars 50000")` → получает `{"text": …}` → отвечает саммари (для PDF >20 страниц делегирует worker subagent через `task spawn`). Followup "оформи как docx" → `task spawn --kind worker --task "render docx from <staged-md> to <data_dir>/media/outbox/<uuid>.docx"` → subagent → render-doc CLI (локально на VPS, fpdf2/python-docx) → DOCX в outbox → `dispatch_reply` шлёт через `adapter.send_document`.

## Задачи (ordered)

1. **Spike 0 (BLOCKER)** — SDK multimodal envelope проверка (inline base64 mixed content). Без PASS — fallback `path_tool`.
2. **Tech debt close — `_memlib` рефакторинг (Q9a).** `tools/__init__.py` + переход с sys.path на relative imports для memory/schedule/skill-installer/task. Делается ДО первого нового phase-7 tool.
3. **MediaAttachment + IncomingMessage extension + MessengerAdapter.send_photo/document/audio abstracts.**
4. **Media sub-package `src/assistant/media/`** — paths, download, sweeper, artefacts.
5. **`adapters/dispatch_reply.py` shared helper** — детектор outbox-paths в тексте → send_photo/document/audio; wired в TelegramAdapter._on_text, SchedulerDispatcher._deliver, subagent on_subagent_stop.
6. **TelegramAdapter media handlers** (_on_voice/photo/document/audio/video_note + send methods with RetryAfter).
7. **ClaudeHandler + bridge multimodal envelope** (mixed content blocks для photo; system-notes для voice/audio/doc).
8. **`tools/transcribe/`** — thin HTTP CLI (~80 LOC stdlib, urllib multipart POST на SSH-tunneled whisper-server).
9. **`tools/genimage/`** — thin HTTP CLI (~80 LOC; daily cap 1, filebased counter).
10. **`tools/extract-doc/`** — local CLI (pypdf/python-docx/openpyxl/striprtf + defusedxml + zipbomb guard).
11. **`tools/render-doc/`** — local CLI (fpdf2 + DejaVu / python-docx / stdlib txt).
12. **Bash allowlist** — 4 новых script paths + path-guards + URL SSRF guard + dup-flag deny.
13. **MediaSettings config** — env_prefix MEDIA_; caps; endpoints; retention; daily cap.
14. **SchedulerDispatcher._deliver switched to dispatch_reply.**
15. **subagent/hooks.py::on_subagent_stop switched to dispatch_reply.**
16. **Тесты** — ~1400 LOC, ~20 файлов (unit CLI, unit adapter, unit dispatch, integration handler/bridge, cross-system subagent+scheduler+dispatch).

## Критерии готовности

- Voice ≤30 s → handler видит duration_s → модель inline-вызовет transcribe CLI → ответ за ≤8 s.
- Voice ≥30 s → модель `task spawn --kind worker` → main turn возвращает "обрабатываю" за ≤3 s → SubagentStop hook доставляет результат через ≤30-60 s.
- Photo 2 MB JPEG → модель описывает содержимое (Spike 0 PASS).
- "нарисуй закат" → модель `task spawn` → PNG приходит как Telegram photo через ≤30 s; daily cap=1 — второй запрос за тот же день отклоняется с exit 6.
- PDF 5 страниц → модель отвечает саммари inline.
- "оформи как docx" → `.docx` приходит файлом через `adapter.send_document`.
- Bash hook rejects: `--out ../etc/passwd` (path escape), `transcribe http://169.254.169.254/…` (SSRF), `--body-file /etc/passwd` (stage escape), запрещённый endpoint URL.
- Параллельный user voice + scheduler-trigger — sched получает attachments=None, не ломает per-chat lock.
- send_photo падает (FileNotFoundError) → текст без артефакта доставляется + WARN log; файл остаётся в outbox.
- Retention sweeper: inbox >14d, outbox >7d → unlinked; total >2GB → LRU evict.
- Phase-6 regression: `task spawn` для не-медиа task'и работает идентично; picker, on_subagent_stop, native Task — без изменений (только переключение send_text→dispatch_reply).
- `_memlib` рефакторинг: все CLI запускаются из `python -m tools.<name>` ИЛИ `python tools/<name>/main.py` (оба пути работают через tools/__init__.py).

## Явно НЕ в phase 7

- Видео-процессинг как полноценный файл (только voice/audio/video_note).
- Real-time streaming transcription (mid-edit message).
- Live OCR сканированных PDF.
- Fine-tuning / lora / custom image models.
- TTS / audio reply back (phase 9).
- Telegram media_group / album (Q12 deferred).
- DB migration v5 для `media_attachments`.
- Hosting whisper/flux serverов (out-of-repo — owner ставит руками).
- HISTORY_MAX_SNIPPET_TOTAL_BYTES cap (Q9b отложен — phase 9).
- Per-skill allowed-tools enforcement (Q9c — phase 9).

## Зависимости

- **Phase 6 (КРИТИЧНО):** SubagentRequestPicker, subagent_jobs ledger, worker AgentDefinition, task spawn CLI, SubagentStop hook delivery. Phase 7 НЕ модифицирует subagent/store.py — только подключает dispatch_reply в on_subagent_stop.
- **Phase 5:** MediaSettings как nested BaseSettings; Daemon.start создаёт media-dirs рядом с _ensure_vault; SchedulerDispatcher._deliver switched to dispatch_reply.
- **Phase 4:** write-first stage-dir паттерн (render-doc --body-file); _memlib рефакторинг закрывается здесь (Q9a).
- **Phase 3:** Bash hook pattern (_validate_python_invocation, _validate_schedule_argv шаблон), _bg_tasks/_spawn_bg для sweeper, _net_mirror.py для URL SSRF guard.
- **Phase 2:** IncomingMessage extending, TelegramAdapter send-retry pattern.
- **External (не shipping в этой phase):** whisper-server / flux-server на Mac хосте + SSH reverse tunnel — owner ставит руками; CLI fallback exit 4 если localhost:9100/9101 не отвечают.

## Риск + митигация

| Severity | Risk | Mitigation |
|---|---|---|
| 🔴 | SDK multimodal envelope не подходит → photo-in ломается | Spike 0 BLOCKER; fallback MEDIA_PHOTO_MODE=path_tool + WebFetch-based vision CLI; env-flag переключение |
| 🟡 | SSH reverse tunnel падает → transcribe/genimage CLI висит | CLI таймаут 60 s + exit 4; subagent on_subagent_stop доставляет error message owner; manual restart tunnel — phase 9 auto-reconnect |
| 🟡 | Host deps на Mac (mlx-whisper, mflux) не установлены — out-of-repo | CLI graceful fail (HTTP 502/503 → exit 4); SKILL.md инструктирует "проверь tunnel + сервер"; смерть тоннеля не ломает text-only функциональность |
| 🟡 | Disk fill: media/inbox/outbox blow up | Retention sweeper (14d/7d/2GB LRU); cap'ы на download |
| 🟡 | Outbound artefact regex ложно срабатывает | Path-guard is_relative_to(outbox_dir.resolve()) + Path.exists() check |
| 🟡 | Genimage cycle (model spam'ит запросы) | Daily cap=1 default + filebased counter; exit 6 при превышении |
| 🟢 | Photo >5 MB blow up context | MEDIA_PHOTO_MAX_INLINE_BYTES=5MB, oversize → skip + system-note |
| 🟢 | History replay для photo turn'а | Image-rows хранятся как `[image: <path>]`; history_to_user_envelopes конвертирует в synthetic text note |
| 🟢 | Scheduler-turn получает media artefact | dispatch_reply shared helper закрывает this — единая логика на all delivery paths |
| 🟢 | Subagent worker spawned для media — не видит атачментов | task spawn --task TEXT передаёт path как часть task строки; subagent читает CLI argv, не envelope |

> **Pillow footprint note (S-3 spike correction).** Pillow **is a required
> transitive dependency** through `fpdf2` (`Requires-Dist: Pillow>=8.3.2`),
> not an optional one — earlier plan drafts incorrectly implied fpdf2 could
> render without Pillow. Importing `fpdf` loads ~22 PIL submodules
> unconditionally. Root `pyproject.toml` pins `Pillow>=10.4,<13` (CVE floor
> + API-churn upper bound; pitfall #1 / H-8). Dep footprint (S-4):
> Pillow ≈ 12.5 MB, lxml ≈ 18.8 MB (via `python-docx`), fontTools ≈ 11.8 MB
> (via `fpdf2`) — top 3 contributors to the +48.75 MB venv delta. Operator
> owns Pillow CVE monitoring between phase upgrades.
