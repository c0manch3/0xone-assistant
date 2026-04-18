---
name: render_doc
description: "Render plain-text bodies to PDF or DOCX under <data_dir>/media/outbox/. Use when the user asks for a document export (PDF/DOCX). CLI: `python tools/render_doc/main.py --body-file PATH --out PATH`."
allowed-tools: [Bash, Write]
---

# render_doc

Рендерит plain-text body в PDF (через `fpdf2`) или DOCX (через `python-docx`)
и кладёт результат в `<data_dir>/media/outbox/`, откуда phase-7
`dispatch_reply` сам доставит файл в Telegram.

## Когда использовать

- Пользователь просит «сделай PDF», «экспорт в DOCX», «сохрани как файл».
- Нужно вернуть длинный артефакт не текстом, а вложением.
- Формат выбирается по суффиксу `--out` (`.pdf` / `.docx`). Другие
  суффиксы CLI отклонит (`exit 3`).

## Exit codes

| code | смысл |
|---|---|
| 0 | ok |
| 2 | usage (argparse / пустой body / неизвестные флаги) |
| 3 | path guard (`--body-file` вне `<data_dir>/run/render-stage/`, `--out` вне `<data_dir>/media/outbox/`, неподдерживаемый суффикс, превышен размер) |
| 4 | I/O (read/write/rename failure) |
| 5 | unknown (fpdf2 / python-docx упали — см. `traceback` поле) |

## Two-step pattern (как передать body)

Bash hook запрещает pipe/heredoc (`|`, `<`, `>`, `` ` ``, `$(`, …),
поэтому напрямую `echo "..." | render_doc` **не работает**. Двухшаговый
pattern:

1. **Stage body через Write tool** в
   `data/run/render-stage/<unique>.txt` (директорию создаёт `Daemon.start`
   с mode 0700; phase-2 path-guard разрешает Write под `project_root`).
2. **Запусти CLI** `python tools/render_doc/main.py --body-file
   data/run/render-stage/<unique>.txt --out
   data/media/outbox/<unique>.pdf --title "..."`.

Уникальный suffix — предсказуемый (`<turn_id>.txt` / `<epoch>.txt`). Не
хардкодь `body.txt` — между turn'ами модель может конкурировать сама с
собой.

## ⚠️ Space after `:` before outbox path (H-13)

Когда в финальном ответе цитируешь путь outbox'а пользователю — ставь
**пробел после `:`**. `dispatch_reply` использует regex v3, который
пропускает случаи без пробела (чтобы не ловить URL-схемы типа
`https://…`) — без пробела файл **не отправится** как attachment,
только текстом.

- **Хорошо:** `Готово: /abs/data/media/outbox/report.pdf`
- **Плохо:** `Готово:/abs/data/media/outbox/report.pdf`

Правило действует для всех четырёх phase-7 скиллов (`transcribe`,
`genimage`, `extract_doc`, `render_doc`).

## Dedup guidance

Основной turn и `on_subagent_stop` hook оба умеют извлекать outbox-пути
из финального текста, поэтому `_DedupLedger` дедуплицирует доставку в
пределах 300-секундного окна. НО — если ты спавнишь worker через
`task spawn --kind worker`, **не** цитируй outbox-путь в финальном
ответе main turn'а: worker сам упомянет путь в своём stop-hook, и
двойной текст с путём всё равно будет скип'нут ledger'ом, но лишний
round-trip — это лишний deliver'ov'ский log. Достаточно сказать
«готово, отправил документ».

## Примеры

### User: «сделай мне PDF с отчётом о продажах»

Шаг 1 (Write):
- `file_path: data/run/render-stage/stage-sales-<turn_id>.txt`
- `content: "Отчёт о продажах за Q1 2026..."`

Шаг 2 (Bash):
```
python tools/render_doc/main.py \
    --body-file data/run/render-stage/stage-sales-<turn_id>.txt \
    --out data/media/outbox/sales-q1-2026-<turn_id>.pdf \
    --title "Отчёт о продажах Q1 2026"
```

Шаг 3 (финальный текст модели): `Готово: /abs/data/media/outbox/sales-q1-2026-<turn_id>.pdf`

### User: «оформи это в DOCX»

Поменяй `.pdf` → `.docx` в `--out`. Всё остальное — идентично.

## Лимиты

- `--body-file`: ≤ 512_000 байт (по умолчанию; переопределяется env
  `RENDER_DOC_MAX_BODY_BYTES`).
- `--out`: итоговый файл ≤ 10_485_760 байт (по умолчанию;
  `RENDER_DOC_MAX_OUTPUT_BYTES`).
- Пустой body отклоняется (exit 2) — пустой PDF/DOCX бесполезен и
  провоцирует mis-routing.

## Cyrillic / Unicode

PDF использует `DejaVuSans.ttf` (vendored в `tools/render_doc/_lib/` —
S-3 verified: 11 KB PDF с mixed Cyrillic/Latin/punctuation). DOCX
полагается на системные шрифты читателя (Word / LibreOffice / Pages
справляются с Cyrillic «из коробки»).

## Границы

- Изображений / таблиц / кастомного layout **нет** — CLI рендерит
  только plain-text body (+ опциональный заголовок). Для сложных
  шаблонов — отдельный tool в будущих фазах.
- Не принимает абсолютные пути вне санкционированного дерева. Если
  модель хочет «сохранить в Desktop» — это за пределами контракта,
  сошлись на outbox + объясни владельцу, что файл доставится в чат.
- `MIME`-детект из `--body-file` **не** делается — body всегда
  трактуется как UTF-8 plain text. Если нужно рендерить HTML/Markdown
  → preprocess до plain text на шаге Write.
