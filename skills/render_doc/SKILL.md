---
name: render_doc
description: 'Generate PDF/DOCX/XLSX documents from markdown content. Owner-facing reports, tables, summaries that benefit from formatted typography (PDF), Word-compatible editing (DOCX), or spreadsheet review (XLSX). Triggers: «сделай PDF», «сгенерь docx», «дай excel таблицу», «сделай отчёт».'
allowed-tools: ["mcp__render_doc__render_doc"]
---

# Render document

Generate a downloadable PDF / DOCX / XLSX file from markdown source
and deliver it to the owner via Telegram. The bot stages the file to
`<data_dir>/artefacts/`, the Telegram adapter sends it via
`send_document`, and the TTL sweeper reaps the artefact 10 minutes
after delivery.

## Когда использовать

Зови `mcp__render_doc__render_doc(...)` каждый раз, когда хозяин
явно просит файл / отчёт / таблицу для пересылки или сохранения за
пределами чата. Markdown пишешь сам — бот доставит готовый файл.

## Триггеры (Cyrillic)

- «сделай PDF», «сгенерь PDF», «pdf отчёт»,
- «сделай docx», «сгенерь docx», «word документ»,
- «дай excel таблицу», «сделай xlsx», «excel из заметок»,
- «сделай отчёт», «сделай отчёт по vault», «выгрузи в файл»,
- «генерируй документ».

## Как использовать

```
mcp__render_doc__render_doc(
    content_md="# Заголовок\n\n... твой markdown ...",
    format="pdf"     # или "docx" / "xlsx"
    filename="отчёт-vault-2026-05-02"   # optional, sanitised server-side
)
```

`filename` без расширения; бот сам ставит `.pdf` / `.docx` / `.xlsx`.
Если хозяин не назвал файл — оставь `filename=null`, бот подставит
`pdf-<utc-iso>` / `docx-<utc-iso>` / `xlsx-<utc-iso>`.

Для `format="xlsx"` `content_md` должен содержать **ровно одну**
markdown pipe-table:

```
| col A | col B |
|-------|-------|
| val 1 | val 2 |
```

PDF и DOCX рендерят произвольный markdown (заголовки, списки, цитаты,
inline-code, fenced-code, таблицы).

## Ограничения

- Никаких шаблонов / branding / header-footer — голый markdown
  rendering.
- Multi-sheet xlsx — нет; одна pipe-table за один вызов.
- Embedded images **любого** вида (включая `data:` URIs) **запрещены**
  — `safe_url_fetcher` блокирует все схемы.
- `content_md` ≤ 1 MiB (`max_input_bytes`).
- Output ≤ 20 MiB (Telegram cap).
- Только три формата: pdf / docx / xlsx.

## Не вызывай

- Для логирования / saving notes — используй `memory_*` @tools
  (phase 4).
- Для скачивания внешних файлов — используй `WebFetch`.
- Для синхронизации vault — используй `vault_push_now` (phase 8).
- Для voice/audio выгрузок — phase 9 это не поддерживает.

## Failure modes (ok=False reasons)

- `disabled` — субсистема отключена или формат недоступен на хосте
  (`pandoc`/`weasyprint` отсутствует). Объясни хозяину текстом.
- `filename_invalid` — sanitiser отверг имя; пересмотри (см.
  spec §2.4 матрицу).
- `input_too_large` — `content_md` > 1 MiB. Сократи / разбей.
- `render_failed_input_syntax` — markdown не парсится pandoc'ом
  ИЛИ пытается tre fetch URL. Упрости разметку.
- `render_failed_output_cap` — итоговый файл > 20 MiB. Разбей на
  меньшие документы.
- `timeout` — рендер не уложился в `tool_timeout_s` (60s). Упрости.
