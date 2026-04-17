# Phase 6 — Media tools (transcribe / genimage / extract-doc / render-doc)

**Цель:** заменить midomis HTTP-сайдкары (whisper-server / flux-server / document-server) на набор локальных CLI-инструментов под `tools/`, запускаемых моделью через Bash, а также добавить поддержку голоса / фото / документов / аудио в приёмный контур бота. После фазы 6 бот умеет: принять голосовое → расшифровать и ответить; увидеть фото (SDK multimodal); сгенерировать картинку по запросу; извлечь текст из PDF/DOCX/ODT/RTF; собрать markdown в PDF/DOCX и отправить файлом.

**Вход:** phase 5 (scheduler daemon + `asyncio.Queue` dispatcher + per-chat lock в `ClaudeHandler` + `IncomingMessage.meta` dict + `TurnStore.sweep_pending` + `_bg_tasks`/`_spawn_bg` + marker-file notify pattern + `_memlib`/`_schedlib` sys.path.append pattern + phase-5 inherited tech debt: `_memlib` refactor, HISTORY_MAX_SNIPPET_TOTAL, per-skill allowed-tools enforcement, mid-chunk send resumption).

**Выход:** четыре end-to-end сценария через Telegram:

1. **Voice-in → text-turn.** Владелец шлёт voice message (`.oga`, Opus) → адаптер скачивает в `<data_dir>/media/inbox/<chat_id>/<msg_id>.oga` → кладёт **ссылку** на файл в `IncomingMessage.attachments` → handler прокидывает путь в `system_notes` → модель вызывает `python tools/transcribe/main.py transcribe <path> --lang ru` → получает `{"text": "…"}` → отвечает как на обычное текстовое.
2. **Photo-in → multimodal-turn.** Владелец шлёт фото → адаптер скачивает → handler формирует SDK content-block `{"type":"image","source":{"type":"base64","data":…}}` в user envelope → модель видит картинку напрямую (проверено spike'ом в задаче 0).
3. **Text-in → image-out.** Владелец говорит "нарисуй закат над морем" → модель зовёт `python tools/genimage/main.py generate --prompt "…" --out <data_dir>/media/outbox/<uuid>.png` → возвращает путь → handler детектит **артефакт-путь** в ответе модели и шлёт его владельцу через `adapter.send_photo(chat_id, path)`.
4. **Document-in → извлечение → саммари / перегенерация.** Владелец шлёт PDF → адаптер скачивает → handler кладёт путь в `attachments` → модель зовёт `python tools/extract-doc/main.py <path>` → получает `{"text": "…", "meta": {…}}` → отвечает саммари. Обратный путь: "оформи как docx" → `python tools/render-doc/main.py --out <data_dir>/media/outbox/<uuid>.docx` (body из stdin stage-файла, по write-first pattern фазы 4) → `adapter.send_document`.

## Задачи

1. **Spike 0 (blocker) — multimodal envelope + provider fan-out.** До любого coding'а: (a) проверить эмпирически, принимает ли `claude-agent-sdk 0.1.59` user-envelope с `content=[{"type":"image","source":{"type":"base64",…}}, {"type":"text",…}]` и как CLI делит это на сообщения; (b) проверить, что mlx-whisper / mflux реально запускаются на M-series под `uv`-managed проектом — без этого решение "локальная генерация vs API" не принимается. Артефакты кладём в `spikes/phase6_multimodal.py` + `spikes/phase6_mlx.py`.
2. **Media sub-package `src/assistant/media/`** — shared helpers: `download.py` (скачивание `file_id` через `TelegramAdapter.bot.download`), `paths.py` (`<data_dir>/media/{inbox,outbox}/<chat_id>/…`), `registry.py` (кросс-процессный lock на GPU если локально mlx), `sweeper.py` (piggyback на phase-3 `_sweep_run_dirs`: retention `inbox` >14d, `outbox` >7d; cap по размеру — например 2 GB суммарно, LRU-эвикция).
3. **`tools/transcribe/` — CLI** (`tools/transcribe/main.py` + `_translib/`). Subcommands: `transcribe <path_or_url> [--language ru] [--model small]`. Вывод `{"ok":true,"data":{"text","language","duration","segments":[…]}}`. Вариант реализации (решается в Q&A): локальная mlx-whisper (per-tool venv через `uv`) vs `whisper.cpp` (CPU, универсально) vs OpenAI Whisper API (платный, трафик). URL-mode с SSRF guard re-use `_lib/_net_mirror.py` паттерна phase-3.
4. **`tools/genimage/` — CLI.** Subcommands: `generate --prompt TEXT --out PATH [--seed N] [--steps N]`. Вывод `{"ok":true,"data":{"path","seed","elapsed_s"}}`. Реализация — решение Q&A: local mflux (M-series, большой diskspace) vs API (OpenAI / Replicate / Stability). Выходной файл в `<data_dir>/media/outbox/`. Дневной cap (или total-cost cap) через отдельную SQLite-таблицу `media_quota` (опционально: детали в §Q).
5. **`tools/extract-doc/` — CLI.** Stdlib-минимум, но допускает внешние зависимости за per-tool venv. Subcommands: `extract <path> [--max-chars N]`. Вывод `{"ok":true,"data":{"text","truncated","format","meta":{"pages":…}}}`. PDF — `pymupdf4llm` или `docling`; docx/xlsx/odt — `python-docx`/`openpyxl`/`odfpy`; rtf — `striprtf`; html/md — stdlib. Безопасность — `defusedxml` monkey-patch (portfolio от midomis), zip-bomb guard, size cap 20 MB, char-cap 50 k (truncate + флаг).
6. **`tools/render-doc/` — CLI.** Input: markdown body через `--body-file <stage-path>` (write-first pattern из phase 4). Subcommands: `render --format {pdf,docx,txt} --out PATH [--title …]`. Вывод `{"ok":true,"data":{"path","bytes","format"}}`. Реализация — `pandoc` на хосте (единый бинарь для всех трёх форматов) **или** `fpdf2 + python-docx` (pure-python, хуже типография). Cyrillic-fonts обязательно (DejaVu).
7. **Расширение `IncomingMessage.attachments: list[MediaAttachment] | None`** в `src/assistant/adapters/base.py`. `MediaAttachment` — frozen dataclass с полями `kind: Literal["voice","audio","photo","document","video_note"]`, `local_path: Path`, `mime: str | None`, `size: int | None`, `duration_s: int | None`, `width/height`. Все Telegram-origin сообщения без вложений оставляют `attachments=None` (backwards-compat). Scheduler-origin не шлёт вложений (остаётся `None`).
8. **TelegramAdapter: приём медиа.** Новые handler'ы `_on_voice`, `_on_photo`, `_on_document`, `_on_audio`, `_on_video_note`. Общий flow: early-reject по размеру/длительности → download в `media/inbox/…` → собрать `MediaAttachment` → передать в `ClaudeHandler.handle`. Для фото — только последняя (наибольшая) версия `photo[-1]`. Для документов — MIME-whitelist из midomis +fallback на расширение. Rate-limit reply для превышающих cap.
9. **`ClaudeHandler` — формирование user-envelope с media.** При наличии `attachments` строить content-block list: (a) `{"type":"image","source":{...base64…}}` для `photo`, (b) `{"type":"text","text":"…"}` + system-note `"user attached voice message at /abs/path; call transcribe skill if appropriate"` для `voice/audio/document/video_note`. История ChatStore хранит текст + путь (не base64 — экономим DB). Scheduler-turn получает `attachments=None`.
10. **TelegramAdapter: outbound media.** Детектор артефактов в финальном ответе модели: регекспом ловим пути вида `<data_dir>/media/outbox/**.(png|jpg|docx|pdf|mp3|txt)` и **заменяем их** на `send_photo`/`send_document`/`send_audio`. Без детектора модель шлёт только путь строкой, что бесполезно для владельца. Путь-guard — строго `is_relative_to(<data_dir>/media/outbox)`.
11. **Скилы.** Четыре отдельных (`transcribe`, `genimage`, `extract-doc`, `render-doc`) **или** один зонтичный `media` — решение в Q&A. Каждый SKILL.md c `allowed-tools: [Bash, Read]` (`Read` для stage-файлов / промежуточных markdown'ов). Примеры диалогов с конкретной bash-строкой.
12. **Bash allowlist.** Добавить `python tools/{transcribe,genimage,extract-doc,render-doc}/main.py <sub> …` в `_BASH_PROGRAMS` валидатор с структурными проверками (по паттерну `_validate_schedule_argv`): длина prompt, whitelist subcommand'ов, path-guard на `--out` (только `<data_dir>/media/outbox`), на `--body-file` (только `<data_dir>/run/*-stage/`), на `FILE_OR_URL` (URL — SSRF guard; path — только `<data_dir>/media/inbox` или `outbox`).
13. **Config / env.** `MediaSettings(BaseSettings, env_prefix="MEDIA_")`: paths (`inbox_dir`, `outbox_dir`), caps (`max_inbox_bytes`, `max_voice_duration_s`, `max_document_bytes`, `max_photo_bytes`), provider-ключи (для опционального external-API варианта). Регистрируется как `Settings.media: MediaSettings`.
14. **Tests.** Unit для каждого CLI (mock mlx-whisper / mock subprocess / fixtures для PDF/DOCX); integration для handler с fake-MediaAttachment; E2E full: stub-фото → bridge envelope шлёт image-block. Regression для phase-2 path-guard: попытка прочитать `<data_dir>/media/outbox` через Read без whitelist должна остаться denied.
15. **Закрытие tech-debt из phase 5 (optional, решается в Q&A):** консолидация `_memlib`/`_schedlib` импорта через `tools/__init__.py` + `from tools.<name>._lib import …`, так как phase 6 добавляет четыре новых tool-пакета — хорошая точка. Альтернативно — остаётся отложенным.

## Критерии готовности

- Voice ≤ 30 сек → расшифровка ≤ 10 сек (warm model) / ≤ 25 сек (cold) → текстовый ответ.
- Photo 2 МБ JPEG → модель описывает его содержимое (доказательство multimodal).
- "нарисуй закат" → PNG 512×512 приходит владельцу как photo в Telegram (не файл-путь строкой).
- PDF 5 страниц → модель отвечает саммари (500–1000 символов).
- "оформи как docx" после текстового ответа → `.docx` приходит файлом.
- Bash hook rejects: `--out ../../etc/passwd` (path escape), `transcribe http://169.254.169.254/…` (SSRF), `--body-file /etc/passwd` (stage-dir escape).
- Параллельный user-turn + scheduler-turn с голосовым — sched приходит **без** вложений, не ломает per-chat lock.
- `send_photo` / `send_document` падают → turn завершается с `⚠ …`, artefact-file остаётся в outbox (юзер достанет руками, не теряем).
- Retention sweeper: inbox >14d / outbox >7d → файлы удалены; DB rows в `conversations` ссылаются на несуществующий путь — handler ignores безболезненно.
- Regression phase-5: scheduler-turn без attachments работает идентично как в phase 5 (scheduler_note остаётся первым system-note'ом).

## Явно НЕ в phase 6

- **Видео-процессинг / video file > 20 MB / video transcription** — voice/audio/video_note принимаем, но video (полноценный файл с контейнером) отклоняем.
- **Real-time streaming transcription** (промежуточный edit message текста) — буферим, шлём финалом, как в phase 2.
- **Live OCR** (screenshot → поиск в тексте) — SDK multimodal решает это нативно; свой CLI не делаем.
- **Fine-tuning / lora / custom image models** — генерируем только с дефолтной моделью.
- **TTS / audio generation back** (voice reply) — потенциально phase 8 ops polish (eleventlabs/piper).
- **Per-user rate limit** — single-user, не нужен; **cost cap дневной для genimage** — opt-in, решение в Q&A.
- **Marketplace-install нового media-скилла модели** — модель уже умеет это через phase-3 skill-installer, но phase 6 не завозит третьих скиллов.
- **`IncomingMessage.attachments` хранение в `conversations`** — хранение ограничено: текст + путь-строка, без base64 (blob в SQLite — плохо, в phase 8 возможна миграция на S3-like).
- **Scheduler-triggered media** ("отправь утром саммари с pdf") — scheduler может вызвать render-doc, но отправка файла через scheduler-deliver требует доработки `SchedulerDispatcher` — откладываем, если не выпадет в Q&A.
- **Seed-установка media-скилов через `tools/skill-installer`** — media-скилы приносим in-repo (как `memory`, `scheduler`), а не через marketplace.

## Зависимости

- **Phase 2:** `TelegramAdapter` + `MessengerAdapter.send_text` (добавляем `send_photo`/`send_document` как новые абстрактные методы), `IncomingMessage` (extending with `attachments`), per-chat lock в handler, path-guard для Read/Write (media/outbox — новый allowed prefix).
- **Phase 3:** Bash allowlist pattern (`_validate_python_invocation` → `_validate_media_argv`), PostToolUse hook (PostWrite в outbox touch'ит outbox sentinel? — рассматриваем, вероятно не нужно), `_bg_tasks` / `_spawn_bg` для retention sweeper.
- **Phase 4:** write-first pattern через `data/run/*-stage/` (использует `render-doc --body-file`), `_memlib` sys.path discipline (наследует tech-debt `_memlib`→`tools/__init__.py` refactor — возможно закрываем).
- **Phase 5:** `MediaSettings` как nested `BaseSettings` (по паттерну `SchedulerSettings`), `Daemon.start` создаёт media-dirs как сейчас создаёт run/vault, scheduler не трогаем (его `origin="scheduler"` handler игнорирует `attachments`).
- **Не зависит от phase 7 (gh tool), phase 8 (ops polish).**

## Риск

**Средний-высокий.** Две параллельные оси риска: (1) **эмпирический контракт SDK multimodal** — spike 0 обязателен: phase 2 история через `history_to_user_envelopes` шлёт строки `text`; если SDK не валидирует envelope с mixed image+text в user content, вся ось photo-in ломается; (2) **зависимости от хоста** — ffmpeg, mlx-whisper (Apple-only), mflux (Apple-only), pandoc. Phase 5 держал stdlib-only; phase 6 этот принцип ломает на уровне per-tool venv (consistent с README § "Per-tool venv vs single venv"). Выбор "локальная mlx vs API" определяет прод-footprint (20 GB моделей vs $-бюджет).

**Митигация:** (a) spike 0 и spike mlx перед coder-wave; (b) каждый CLI имеет **graceful-fallback path**: если per-tool venv не развернут — exit 8 с чётким сообщением "run `uv sync --directory tools/transcribe`", модель информирует владельца; (c) для multimodal — если spike 0 показывает, что SDK не принимает base64, fallback: handler сохраняет фото в inbox и вставляет в system-note путь (`"user attached photo at /abs/path"`) + модель зовёт "extract-image-description tool" (новый CLI на OpenAI Vision / Anthropic Vision API через WebFetch — это запасной путь, не основной); (d) Document-extract наследует midomis'овский defusedxml + zip-bomb guard; (e) retention sweeper предотвращает disk-fill.
