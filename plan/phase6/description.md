# Phase 6 — Media tools (transcribe / genimage / extract-doc / render-doc)

**Цель:** заменить midomis HTTP-сайдкары на локальные CLI.

**Вход:** phase 2.

**Выход:** четыре скилла работают end-to-end.

## Задачи (параллелизуемы, 5a–5d)

1. **`tools/transcribe/`** — собственный venv с mlx-whisper + yt-dlp + ffmpeg.
   - CLI: `transcribe FILE_OR_URL [--model] [--language]` → JSON `{text, segments, duration}`.
   - SSRF guard для URL (whitelist схем, reject private IPs).
   - Очистка temp-dir.
2. **`tools/genimage/`** — mflux FLUX.1-schnell.
   - CLI: `generate --prompt --seed --steps --out PATH` → `{path, seed, elapsed}`.
   - Разделяемый с transcribe file-lock на GPU: `data/run/gpu.lock`.
3. **`tools/extract-doc/`** — docx/pdf/xlsx/csv/html/rtf/odt → plain text.
   - Лимиты: 20 MB / 50k символов.
   - JSON `{text, truncated, meta}`.
4. **`tools/render-doc/`** — md → docx/pdf/txt.
   - fpdf2 + DejaVu для кириллицы.
   - Body из stdin → путь к файлу на stdout.
5. Скилы: `transcription`, `image-generation`, `documents` (combine extract + render). Каждый `SKILL.md` перечисляет конкретные Bash-вызовы и примеры парсинга вывода.
6. Telegram-адаптер скачивает voice/audio/video_note/photo/document в `data/users/<chat_id>/inbox/` *до* передачи модели (модель читает через `Read`/`Bash` в рамках path-guard).
7. Rate-limits: transcribe 3/час, genimage 10/день (в `RateLimiter` в handler, не в CLI).

## Критерии готовности

- Voice → text (скилл transcription).
- "нарисуй кота" → изображение в ответе.
- Загрузка pdf → саммари.
- "оформи в docx" → файл отправлен.

## Зависимости

Phase 2.

## Риск

**Средний.** MLX memory pressure, наличие ffmpeg в системе, размер per-tool venv.

**Митигация:** документировать требования к хосту (Apple Silicon, ffmpeg, system Python 3.12+).
