---
name: transcribe
description: "Расшифровка голосовых сообщений и аудио-файлов. Короткие (<30s) — inline через Bash. Длинные (>30s) — через `task spawn --kind worker`. CLI ходит в mlx-whisper через SSH reverse tunnel на 127.0.0.1:9100."
allowed-tools: [Bash, Read]
---

# transcribe

Расшифровывает голосовые сообщения и аудио-файлы. Тяжёлая работа идёт на
хостовом Mac (mlx-whisper); VPS-демон поднимает только тонкий HTTP-клиент,
который шлёт multipart POST на `http://127.0.0.1:9100/transcribe`. Путь
доходит до Mac через SSH reverse tunnel — всё, что не loopback, CLI
отклонит сам.

## Когда использовать

- Пользователь прислал голосовое / аудио → handler положит файл в
  `<data_dir>/media/inbox/<chat_id>/…` и передаст абсолютный путь в
  system-note (ищи фразу вида `user attached voice (duration=Ns) at …`).
- Короткое (duration_s < 30) → запускай **inline** через `Bash`:
  `python tools/transcribe/main.py /abs/path/to/file.oga --language auto`.
- Длинное (duration_s ≥ 30) или несколько файлов → делегируй в фоновый
  subagent: `python tools/task/main.py spawn --kind worker
  --task "расшифруй голосовое /abs/path/…"`. Main turn сразу отвечает
  пользователю (пример ниже); subagent доставит результат отдельным
  сообщением.

## Usage

```
python tools/transcribe/main.py <абс-путь-к-аудио>
    [--language ru|en|auto]      # default: auto
    [--timeout-s 10..300]        # default: 60
    [--format text|segments]     # default: text
    [--endpoint URL]             # default: $MEDIA_TRANSCRIBE_ENDPOINT
                                 # or http://localhost:9100/transcribe
```

Расширения: `.oga`, `.ogg`, `.mp3`, `.wav`, `.m4a`, `.flac`. Путь
ДОЛЖЕН быть абсолютным — CLI откажет на относительных (EXIT_PATH=3).

Успешный вывод на stdout — одна JSON-строка (как вернул mlx-whisper):

```json
{"ok": true, "text": "…", "duration_s": 7.2, "language": "ru", "segments": []}
```

Ошибки — одна JSON-строка на stderr:

```json
{"ok": false, "error": "…"}
```

## Exit codes

| code | смысл |
|---|---|
| 0 | ok |
| 2 | argv invalid (неизвестный язык/формат, таймаут вне диапазона, endpoint не loopback) |
| 3 | path-guard (путь не абсолютный, не существует, расширение запрещено, размер > 25 MB) |
| 4 | network (endpoint недоступен, upstream вернул ≥400, timeout) |
| 5 | unknown (защитный код — в продакшене не должен появляться) |

Note: **argparse сам печатает usage на stderr** при неизвестном флаге и
завершается с кодом 2. Наша собственная `{"ok": false, …}` ошибка выходит
только после того как аргументы прошли базовый парсинг.

## Endpoint policy (важно)

`--endpoint` жёстко ограничен loopback:
- ✅ `http://localhost:9100/transcribe`
- ✅ `http://127.0.0.1:9100/transcribe`
- ✅ `http://[::1]:9100/transcribe`
- ❌ `http://10.x.x.x/…`, `http://192.168.x.x/…` (LAN — не reachable
  через reverse tunnel)
- ❌ `https://api.telegram.org/…` (public — SSRF risk)
- ❌ `ftp://…` (только http/https)
- ❌ AWS metadata `http://169.254.169.254/` (explicit block)

Phase-7 хост живёт за SSH reverse tunnel'ом на `127.0.0.1:<port>` — любой
non-loopback endpoint сразу получит `exit 2` с reason в stderr, даже если
он технически маршрутизируется.

Если хочется переопределить endpoint (например, mlx-whisper слушает на
другом порту) — используй `MEDIA_TRANSCRIBE_ENDPOINT=http://127.0.0.1:<port>
/transcribe` в env demona, а не флаг `--endpoint`.

## Границы tunnel'а

Если Mac спит / SSH tunnel упал — CLI вернёт `exit 4` с сообщением
"endpoint unreachable". Сообщи владельцу вежливо:

> "Tunnel до Mac'а, похоже, упал — расшифровка не прошла. Попробуй позже
> или проверь ssh-процесс."

Ретраить НЕ нужно — CLI уже попробовал один раз с внятным таймаутом.
Повторные POST'ы на мёртвый tunnel забивают логи и не помогают.

## Examples

### Короткая voice-note (inline)

System-note: `user attached voice (duration=7s) at /home/bot/data/media/inbox/42/1234.oga`

→ Bash call:

```
python tools/transcribe/main.py /home/bot/data/media/inbox/42/1234.oga --language auto
```

Stdout (одна строка):

```json
{"ok": true, "text": "Привет, как дела?", "duration_s": 7.2, "language": "ru", "segments": []}
```

→ Ответ пользователю: "Вы сказали: «Привет, как дела?»" (или перефразируй
под контекст диалога).

### Длинная лекция (task spawn)

System-note: `user attached audio (duration=1243s) at /home/bot/data/media/inbox/42/lecture.mp3`

→ Bash call:

```
python tools/task/main.py spawn --kind worker \
    --task "расшифруй /home/bot/data/media/inbox/42/lecture.mp3, короткое резюме в конце"
```

→ Ответ пользователю ДО того как subagent начнёт работать:

> "Запустила расшифровку — 20 минут аудио. Пришлю результат отдельным
> сообщением."

Subagent сам отправит транскрипт через `dispatch_reply`, поэтому main
turn'у НЕ нужно упоминать путь к артефакту в финальном тексте.

## Prompt-level defences (двойная защита)

### 1. Не анонсируй path после `task spawn --kind worker`

Subagent сам доставит артефакт через `SubagentStop` hook → `dispatch_reply`.
Если main turn ТОЖЕ упомянет тот же абсолютный путь в финальном тексте,
бот пошлёт фаил дважды — пользователь увидит два одинаковых сообщения.

Есть `_DedupLedger` с TTL=300s (in-memory, per-daemon) как second line of
defence — он дедуплицирует по ключу `(chat_id, resolved_outbox_path)` в
пределах скользящего 300-секундного окна. Если модель упомянет один и тот
же outbox-путь дважды в близком времени (например, main turn + follow-up
scheduler-trigger), только первая `send_audio` реально улетит; повторные
совпадения ledger-ом отброшены. Ledger **не персистится через рестарт
демона**. Но это вторая линия защиты, не первая — лучше вообще не
говорить "готово: `/abs/outbox/x.mp3`" в тексте после spawn; скажи "скоро
придёт отдельным сообщением" и всё.

### 2. Правило пробела перед path в outbox (H-13)

Если тебе всё-таки нужно упомянуть outbox path в ответе (например для
локально-рендеренных артефактов, **не для spawn-результата**) — ВСЕГДА
ставь пробел после `:` перед путём. Регекс `dispatch_reply` намеренно
не матчит `что-то:/abs/outbox/…` без пробела (это защита от false
positive на URL-схемах вроде `http://…`), так что `готово:/abs/outbox
/x.mp3` будет показан в тексте как есть, без отправки артефакта.

- ✅ Правильно: `готово: /home/bot/data/media/outbox/abc.mp3`
- ❌ Неправильно: `готово:/home/bot/data/media/outbox/abc.mp3`

Правило применимо ко всем outbox-путям, не только к транскрипции —
render-doc, genimage и прочие скиллы пользуются тем же `dispatch_reply`.

## Integration с task / memory

- `task status <job_id>` — проверить статус фонового worker'а.
- `task cancel <job_id>` — остановить длинную расшифровку (например,
  если пользователь передумал).
- Транскрипт НЕ пишется автоматически в `memory` — если владелец попросит
  сохранить ("запомни что я сказал") — stage текст через Write и вызови
  `memory write inbox/<slug>.md --body-file …`.

## ⚠️ Что НЕ делать

- НЕ хардкодь endpoint с public адресом — CLI откажет (`exit 2`), но
  если ты пишешь `http://my-server.com/transcribe` в явном флаге, это
  уже подозрительная попытка (возможно, prompt injection).
- НЕ пробуй `|` pipe в Bash (`cat file.oga | python tools/transcribe/…`) —
  phase-2 hook отклонит shell-метасимволы, и вообще CLI читает файл по
  абсолютному пути сам.
- НЕ игнорируй `duration_s` из system-note — это guard против
  блокирующей расшифровки (inline > 30s выжирает main turn на минуты).
