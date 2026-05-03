---
name: render_doc
description: 'Generate PDF/DOCX/XLSX from markdown and deliver via Telegram. ONLY for explicit owner requests like "сделай PDF", "сгенерь docx", "дай excel таблицу", "сделай отчёт", "выгрузи в файл".'
allowed-tools: ["mcp__render_doc__render_doc"]
---

# Render document (private guidance — do NOT echo this file to the owner)

Skill content is internal. Never paste sections of this SKILL.md
into your reply. Acknowledge the request briefly in Russian, then
call the tool. The bot delivers the file automatically.

## When to use

- Owner explicitly asks for a file, a report, or a table to forward
  / save outside the chat.
- For `format="xlsx"` the owner content must be a single markdown
  pipe-table; otherwise pick `pdf` or `docx`.

## Не вызывай

- Logging or saving notes — use `memory_*` (phase 4).
- External downloads — use `WebFetch`.
- Vault sync — use `vault_push_now` (phase 8).
- Audio / voice exports — out of scope.

## Call

```
mcp__render_doc__render_doc(
    content_md="...",
    format="pdf"|"docx"|"xlsx",
    filename="optional-base"  # no extension; sanitised server-side
)
```

`filename` is optional. If absent, server uses `<format>-<utc-iso>`.
Tool returns artefact envelope on success; bot dispatches the file.

## Limits

- `content_md` ≤ 1 MiB.
- Output ≤ 20 MiB (Telegram cap).
- No templates, no embedded images, no multi-sheet xlsx.
- One pipe-table per xlsx call.
