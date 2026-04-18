---
name: genimage
description: "Генерация изображений через mflux на хостовом Mac (SSH reverse tunnel). ВСЕГДА через `task spawn --kind worker` — одно изображение 30–120 s. Daily cap = 1, превышение → exit 6."
allowed-tools: [Bash, Read]
---

# genimage

Thin HTTP client для mflux-server, который живёт на Mac и доступен демону через
SSH reverse tunnel на `127.0.0.1:9101`. На VPS ничего тяжёлого не крутится —
только `urllib` POST + atomic write в outbox.

## Когда использовать

- Пользователь просит нарисовать / сгенерировать / «сделай картинку» → `task
  spawn --kind worker` с телом задачи вида «генерируй изображение X через
  `tools/genimage/main.py --prompt "…" --out <outbox>/<uuid>.png`».
- Никогда не вызывай CLI inline в main turn: mflux 8-step sampling ~40–90 s,
  воркер освободит main turn для диалога.

## Daily quota

Один запрос в сутки (UTC). Счётчик живёт в `<data_dir>/run/genimage-quota.json`,
`fcntl.flock(LOCK_EX)` защищает от гонки между parallel worker subagent'ами
(spike S-5 R-3 подтверждает: 10 параллельных worker'ов → ровно один выигрывает).

Если пользователь просит второе изображение за сутки → честно скажи «дневной
лимит исчерпан, попробуй завтра после 00:00 UTC». Не пытайся обойти через
`--daily-cap 99` — это флаг для тестов, Bash hook отклонит аргумент.

Клок может отъехать назад через NTP correction (R-4 known jitter ±1); если
вдруг quota reset посреди дня — это не баг, а документированный edge case.

## CLI форма

```
python tools/genimage/main.py \
    --prompt "закат над морем" \
    --out <absolute-path-under-outbox>/<uuid>.png \
    [--width 1024] [--height 1024] [--steps 8] [--seed 42] \
    [--timeout-s 120]
```

| Флаг | Default | Valid |
|---|---|---|
| `--prompt` | — (required) | ≤1024 UTF-8 байт, без переносов строк |
| `--out` | — (required) | абсолютный путь под `<data_dir>/media/outbox/`, расширение `.png` |
| `--width` / `--height` | `1024` / `1024` | `{256, 512, 768, 1024}` |
| `--steps` | `8` | 1..20 |
| `--seed` | server-chosen | 0..2^31-1 |
| `--timeout-s` | `120` | 30..600 |

## Exit codes

| code | смысл |
|---|---|
| 0 | ok — PNG записан в `--out`, stdout JSON с `{ok, path, width, height, size_bytes, quota}` |
| 2 | argv / usage (отсутствует `--prompt`, bad enum, prompt с newline и т.п.) |
| 3 | path-guard (`--out` вне outbox, неабсолютный путь, файл уже существует) |
| 4 | network (endpoint не loopback, tunnel down, timeout, HTTP 5xx, unexpected Content-Type) |
| 5 | unknown (локальный I/O при atomic write, unhandled exception) |
| 6 | **quota exceeded** — дневной лимит исчерпан; повторить через 24 часа |

## После worker завершил задачу

Subagent вернёт абсолютный путь к PNG; `dispatch_reply` в `SubagentStop` hook
задетектит outbox-path и отправит файл как Telegram photo автоматически.

**MANDATORY prompt rule (H-13, double-delivery mitigation):** в finальном тексте
main turn'а ПОСЛЕ `task spawn --kind worker …` НЕ упоминай абсолютный путь к
артефакту — subagent доставит его отдельным сообщением. Используй фразировку
без path:

> «готово, картинка скоро придёт отдельным сообщением.»

**Правило пробела после `:` перед путём.** Если всё-таки приходится упомянуть
артефактный путь в тексте (например для reply-ack в worker log), ВСЕГДА ставь
пробел после двоеточия перед абсолютным путём — regex `_ARTEFACT_RE` v3
отвергает склейку без пробела, чтобы не путать absolute path с URL-schema
двоеточием.

- Good: `готово: /abs/outbox/x.png`
- Bad:  `готово:/abs/outbox/x.png`

Без пробела `dispatch_reply` не найдёт путь → файл не уедет пользователю, хотя
worker его создал.

## Endpoint SSRF guard

CLI отвергает любой `--endpoint` (или env `MEDIA_GENIMAGE_ENDPOINT`), который
разрешается хотя бы в один non-loopback адрес. Допустимы: `127.0.0.0/8`, `::1`.
Всё остальное (`10.x`, `192.168.x`, `169.254.169.254`, публичные хосты) → exit 3.
Это более узкий контракт, чем phase-3 `classify_url` (который разрешал private
LAN); phase-7 host доступен строго через SSH reverse tunnel на `127.0.0.1:<port>`.

## Пример полного flow

User: «нарисуй закат над морем в минималистичном стиле»

```
python tools/task/main.py spawn \
    --kind worker \
    --task "Сгенерируй изображение через tools/genimage/main.py. Prompt: 'minimalist sunset over the sea'. Out: /var/data/0xone/media/outbox/$(uuidgen).png"
```

Main turn: «окей, рисую, картинка скоро придёт отдельным сообщением.»
→ Worker subagent запускает CLI → JSON `{"ok": true, "path": "...", ...}`
→ SubagentStop hook → `dispatch_reply` → `adapter.send_photo` → PNG в чате.

## Ограничения

- Mac off / SSH tunnel down → exit 4 с `endpoint unreachable`. Сообщи
  владельцу, не ретраи автоматически (ресурс может быть выключен намеренно).
- Daily cap можно поднять только через env `MEDIA_GENIMAGE_DAILY_CAP`; CLI flag
  `--daily-cap` — для тестов, через Bash hook он недоступен.
- Width/height строго enum'ом — mflux 8-step sampler оптимизирован под эти
  размеры, произвольные значения server отклонит HTTP 400 → exit 4.
- Seed воспроизводим между запусками только при прочих равных параметрах и
  одном и том же mflux-image; server-side обновления могут сменить детерминизм.
