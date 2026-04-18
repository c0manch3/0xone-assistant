---
name: extract-doc
description: "Извлечение текста из PDF/DOCX/XLSX/RTF/TXT. Короткие документы (<20 страниц PDF или <5 MB) — inline через Bash. Длинные — через `task spawn --kind worker`. CLI: `python tools/extract_doc/main.py <path> [--max-chars N] [--pages N-M]`."
allowed-tools: [Bash, Read]
---

# extract-doc

Локальный экстрактор: парсит PDF / DOCX / XLSX / RTF / TXT и возвращает
plain-text JSON-ом на stdout. Работает без сети — все библиотеки
(`pypdf`, `python-docx`, `openpyxl`, `striprtf`) установлены в общий
venv через root `pyproject.toml`.

## Когда использовать

- Пользователь прислал документ, и ты видишь system-note вида
  `user attached document '<filename>' at <abs-path>`.
- Пользователь явно попросил "вытащи текст из этого PDF", "покажи
  содержимое таблицы", "summarise .docx".
- Перед `render_doc` нужно переиспользовать часть существующего
  документа (прочитай → ужми → скорми в `--body-file`).

## Inline vs async worker

- **inline (<20 страниц PDF или <5 MB любого типа):** вызови CLI прямо
  через Bash, получи текст в JSON, отвечай пользователю в том же
  turn'е.
- **async worker:** если input >20 MB или PDF >50 страниц — `task
  spawn --kind worker` со скриптом, который вызовет CLI и вернёт
  транскрипт. Иначе один инструмент-вызов может занять 10+ секунд и
  съесть turn.

## Invocation

```
python tools/extract_doc/main.py <path> [--max-chars N] [--pages N-M]
```

- `<path>` — абсолютный путь к файлу (такой, какой пришёл в
  system-note). Suffix (`.pdf` / `.docx` / `.xlsx` / `.rtf` / `.txt`)
  определяет диспетчер.
- `--max-chars N` — ограничение длины извлечённого текста
  (default 200 000; hard cap 2 000 000). Если результат обрезан,
  `truncated: true` и ты обязан сказать пользователю, что показан
  фрагмент.
- `--pages N-M` — **только для PDF**. 1-based inclusive, `5-12` или
  одиночная страница `5`. Для остальных форматов флаг отклоняется с
  exit 3.

## JSON output (stdout)

```json
{
  "ok": true,
  "path": "/abs/path/to/file.pdf",
  "format": "pdf",
  "units": 12,
  "size_bytes": 184320,
  "chars": 42000,
  "truncated": false,
  "text": "…извлечённый текст…"
}
```

- `units`: количество прочитанных страниц (pdf), абзацев+строк (docx),
  непустых строк (xlsx), 1 для rtf/txt. Поле нужно, чтобы отличить
  "документ пустой" от "ничего не извлеклось".
- `truncated`: `true` если сработал `--max-chars` — сообщи
  пользователю, предложи сузить диапазон (`--pages`) или поднять
  лимит.

## Exit codes

| code | смысл                                                       |
| ---- | ----------------------------------------------------------- |
| 0    | ok                                                          |
| 2    | usage (argparse / `--pages` parse / `--max-chars` <=0)      |
| 3    | validation: path / suffix / size / encrypted PDF / zip-bomb |
| 4    | IO: open / read / corrupt archive                           |
| 5    | unknown (внезапный exception библиотеки)                    |

На exit >=3 читай `stderr` — там JSON `{"ok": false, "error": "...",
...}` с подробностями.

## Security

- DOCX/XLSX — ZIP-архивы; CLI проверяет сумму заявленных
  несжатых размеров (zip-bomb guard, cap 64 MB). Превышение → exit 3.
- Все XML-парсеры (включая внутренние вызовы из `python-docx` и
  `openpyxl`) перехвачены `defusedxml.defuse_stdlib()` → billion-
  laughs / XXE / quadratic-blowup блокируются.
- Размер входа capped через env `MEDIA_EXTRACT_MAX_INPUT_BYTES`
  (default 20 000 000). Файл больше → exit 3 до парса.
- Encrypted PDF → exit 3 (NOT silent empty text).

## Path discipline (H-13) — ВАЖНО

Если в ответе пользователю ты упоминаешь absolute path к артефакту
(например, "сохранил в ..."), **ВСЕГДА ставь пробел после `:` перед
путём**. Regex detection в `dispatch_reply` не матчит
`готово:/abs/path.pdf` (чтобы не ловить URL-scheme колоны), и без
пробела путь силёнтно не отправится как вложение.

- Good: `готово: /var/data/outbox/report.pdf`
- Bad:  `готово:/var/data/outbox/report.pdf`

Правило распространяется на любой упоминаемый путь с `.pdf` / `.docx`
/ `.xlsx` / `.txt` / `.rtf` / фото/аудио расширениями.

## Double-delivery guidance (§4.5)

Если ты запустил `task spawn --kind worker …` для длинного
документа, то в финальном тексте main turn'а **НЕ упоминай
абсолютный путь** к будущему артефакту. Subagent доставит файл
отдельным сообщением через `dispatch_reply` из `SubagentStop`
hook'а. Безопасная фразировка:

> "готово, PDF скоро придёт отдельным сообщением."

Если нарушишь — dedup ledger (300s sliding window, ключ
`(resolved_path, chat_id)`) выкинет дубликат на ingress, но
prompt-level mitigation — первая линия обороны.

## Примеры

**Короткий PDF inline:**

```
python tools/extract_doc/main.py /abs/inbox/report.pdf --max-chars 20000
```

→ `{"ok": true, "format": "pdf", "units": 8, "text": "..."}` →
отвечаешь пользователю: "Вот краткое содержание: …"

**Большой PDF — async worker:**

→ `task spawn --kind worker --task "extract PDF at /abs/inbox/big.pdf
and summarise"` → "готово, резюме придёт отдельным сообщением."
(без пути)

**XLSX — только первый лист:**

CLI не поддерживает выбор листа флагом в phase-7; извлеки все листы,
найди нужный в тексте (каждый начинается со строки `# sheet: <name>`).

## Границы

- Изображения внутри DOCX / PDF не извлекаются (OCR вне scope phase-7).
- Formulas в XLSX возвращаются **как вычисленные значения** (load_workbook
  `data_only=True`). Если ячейка хранит формулу без кэшированного
  результата (свежее сохранение из LibreOffice) — будет пустая.
- Большие файлы (>20 MB) отсекаются до парса; передай их через
  `task spawn --kind worker` + собственный скрипт (или попроси
  пользователя разбить).
