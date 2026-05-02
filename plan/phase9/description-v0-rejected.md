# Phase 9 — description v0 (REJECTED)

Rejected on 2026-05-02 after devil's advocate wave 1 surfaced
5 CRITICAL + 6 HIGH + 7 MEDIUM + 7 LOW closures. See
`devil-w1-findings.md` and `description.md` (v1) for resolution.

---

# Phase 9 — render_doc: PDF / DOCX / XLSX генерация по запросу модели

> Spec v0 — owner-frozen scope (3 формата, no templates, no multi-sheet
> xlsx, no backwards-compat). Архитектура повторяет phase-8 vault_sync:
> опт-ин subsystem package под `src/assistant/render_doc/`,
> единственный MCP @tool `render_doc` под отдельной MCP-группой
> `mcp__render_doc__`, conditional bridge wiring через
> `render_doc_tool_visible` kwarg по образцу `vault_tool_visible`. Phase
> 9 знакомит бота с outbound document delivery через
> `aiogram.Bot.send_document` — раньше outbound media-канал не
> существовал (phase 6a — это **inbound** документы, см. §Развилки Q1),
> поэтому adapter получает первый честный outbound-document path вместе
> с этим phase.
>
> Hard non-negotiables (session memory + owner pre-frozen):
>
> - System-tool smoke test ОБЯЗАТЕЛЕН в Wave A (`shutil.which("pandoc")`
>   + `import weasyprint`) — phase 8 ssh-not-found incident прошёл 4
>   reviewer waves и 1014 mocked-тестов мимо.
> - Dockerfile runtime stage расширяется apt-пакетами (`pandoc`,
>   `libcairo2`, `libpango-1.0-0`, `libpangoft2-1.0-0`,
>   `libgdk-pixbuf2.0-0`); image size budget зашит в §Риск.
> - Auth = Claude OAuth ТОЛЬКО. Никакого `ANTHROPIC_API_KEY`.
> - Никаких backwards-compat shims; spec coherent end-to-end.

## 1. Цель

Дать модели возможность создавать визуальные документы по запросу
владельца («сделай PDF отчёт по последним заметкам», «сгенерь docx с
тем-то», «дай excel таблицу из vault notes») и доставлять результат в
Telegram как файл. Модель пишет содержимое в markdown и вызывает
единственный MCP @tool `render_doc(content_md, format, filename?)` →
бот рендерит файл на диск под `<data_dir>/artefacts/<uuid>.<ext>` →
turn-output несёт structured artefact-envelope → Telegram-адаптер
отдаёт `bot.send_document(...)` владельцу → файл TTL-sweeper удаляет
артефакт через `artefact_ttl_s` секунд.

Phase 9 строго ограничен **тремя форматами**: PDF (pandoc → HTML →
WeasyPrint), DOCX (pandoc native), XLSX (openpyxl over markdown
pipe-tables). Никаких шаблонов, brandинга, header/footer, multi-sheet
xlsx и кастомного CSS — голый markdown rendering. Templates — в §6
Явно НЕ.

Trigger architecture: subsystem полностью stateless относительно
business-логики (никакого scheduler kind-column, никаких daemon-bg
loops для рендеринга). Единственный bg-loop — TTL artefact sweeper,
запускаемый из `RenderDocSubsystem.start()` как
`_spawn_bg_supervised(self._sweep_loop, name="render_doc_sweeper")` по
аналогии с phase-8 vault_sync loop. Сам рендеринг происходит синхронно
внутри @tool body в asyncio task scope SDK (не через
`subprocess.run` — через `asyncio.create_subprocess_exec` для pandoc, и
через `asyncio.to_thread` для WeasyPrint и openpyxl, чтобы не блочить
event loop на CPU-bound работе).

## 2. Архитектура

### 2.1 Поток данных end-to-end

1. Owner: «сгенерь PDF отчёт по последним заметкам vault».
2. Модель (через Claude bridge):
   - читает vault через phase-4 `mcp__memory__memory_search` /
     `memory_get`,
   - формирует markdown содержимое в памяти,
   - вызывает `mcp__render_doc__render_doc(content_md=…,
     format="pdf", filename="отчёт-vault-2026-05-02")`.
3. `render_doc` @tool body (см. §2.4) проверяет включённость subsystem,
   санитизирует filename, dispatch'ит в renderer
   (PDF/DOCX/XLSX) → возвращает MCP-result, содержащий **artefact
   envelope** (см. §2.3).
4. Bridge стримит блоки в `ClaudeHandler` → handler собирает chunks →
   adapter получает финальный текст ИЛИ обнаруживает artefact-envelope
   маркер в текстовом блоке (см. §2.5 contract).
5. Telegram adapter извлекает envelope → `bot.send_document(chat_id,
   FSInputFile(path), caption=…)` → файл доставлен.
6. Sweeper background loop по `artefact_ttl_s` (default 600s = 10
   минут) удаляет файлы старше TTL под `<data_dir>/artefacts/`. Это
   защищает от disk fill при сбое доставки или нескольких параллельных
   render'ов в одном turn.

### 2.2 Subsystem package — `src/assistant/render_doc/`

Mirror phase-8 vault_sync layout:

- `__init__.py` — exports `RenderDocSubsystem`, `_cleanup_stale_artefacts`.
- `subsystem.py` — `RenderDocSubsystem` class:
  - `__init__(settings, artefact_dir, ...)`,
  - `start()` / `stop()` — сейчас управляет TTL-sweeper bg-loop;
    daemon spawn'ит через `_spawn_bg_supervised` по аналогии с
    `vault_sync.loop`,
  - `startup_check()` — `shutil.which("pandoc")` MUST resolve;
    `import weasyprint` MUST succeed; иначе subsystem
    `force_disabled=True` (как vault_sync force-disable на
    отсутствующий ssh-key) → daemon продолжает работать без render_doc,
    @tool скрыт, log line `event=render_doc_force_disabled` +
    `reason=…` идёт в Telegram владельцу одной нотификацией на boot,
  - `render(content_md, format, filename) -> RenderResult` — публичный
    API, дёргается из @tool body. Под капотом — dispatch на
    pdf_renderer / docx_renderer / xlsx_renderer.
  - `_sweep_loop()` — TTL sweeper, удаляет файлы под
    `<data_dir>/artefacts/` с `mtime < now - artefact_ttl_s`. Cron
    interval `sweep_interval_s` (default 60s).
  - `force_disabled: bool` + `disabled_reason: str | None` — для
    daemon-side conditional spawn.
- `pdf_renderer.py` — pandoc + WeasyPrint pipeline (см. §2.6).
- `docx_renderer.py` — pandoc native (см. §2.7).
- `xlsx_renderer.py` — openpyxl over `markdown_tables` parser (см.
  §2.8).
- `markdown_tables.py` — pure-Python pipe-syntax table parser. Принимает
  `content_md`, возвращает `list[Table]` (header + rows).
- `audit.py` — JSONL audit log writer (mirror phase-8
  `vault_sync/audit.py`). Path: `<data_dir>/run/render-doc-audit.jsonl`.
  Schema: `{"ts": iso, "format": "pdf|docx|xlsx", "result":
  "ok|failed|disabled", "filename": str, "bytes": int|null,
  "duration_ms": int, "error": str|null}`. Rotation policy:
  `audit_log_max_size_mb` (default 10) → `os.replace` to `.1` (без
  цепочки), идентично phase-8 audit.
- `boot.py` — `_cleanup_stale_artefacts(artefact_dir)` для запуска
  при `Daemon.start()` BEFORE subsystem spawn (по аналогии с
  `_cleanup_stale_vault_locks`). Удаляет всё под `<data_dir>/artefacts/`
  с mtime > `cleanup_threshold_s` (default 86400 = 24h) — защита от
  забытых файлов при crash daemon.
- `_validate_paths.py` — sanitize filename helper (см. §2.4).

### 2.3 Artefact envelope contract

Поле в `render_doc` MCP @tool result:

```python
{
    "ok": True,
    "result": "rendered",
    "kind": "artefact",
    "format": "pdf",  # или "docx" / "xlsx"
    "path": "/home/bot/.local/share/0xone-assistant/artefacts/<uuid>.pdf",
    "suggested_filename": "отчёт-vault-2026-05-02.pdf",
    "bytes": 482301,
    "expires_at": "2026-05-02T12:34:56Z"  # now + artefact_ttl_s
}
```

`content` блок MCP содержит **тот же словарь** как одна `text`-block
(JSON-stringified) — без этого SDK не покажет данные модели как
обычный tool_result. Bridge при обработке `ToolResultBlock` со
`name == "mcp__render_doc__render_doc"` парсит JSON-нагрузку и
сохраняет path в **bridge-level artefact ledger** (новая структура,
см. §2.5) keyed по `tool_use_id`. Когда model-turn завершается
(`ResultMessage`), handler передаёт ledger в adapter перед
`send_text`.

`ok=False` envelope (полностью отдельный path):

```python
{
    "ok": False,
    "kind": "error",
    "reason": "disabled" | "format_invalid" | "filename_invalid" |
              "input_too_large" | "render_failed" | "timeout",
    "error": "<short user-readable message>",
}
```

Adapter не должен делать `send_document` если `ok=False` или `kind !=
"artefact"`. Модель видит JSON и сама пересказывает ошибку владельцу
текстом.

### 2.4 MCP @tool — `mcp__render_doc__render_doc`

**Module location**: `src/assistant/tools_sdk/render_doc.py` (mirror
`tools_sdk/vault.py`). Sibling `_render_doc_core.py` для test helpers
+ `configure_render_doc` / `get_configured_subsystem` /
`reset_render_doc_for_tests` (mirror `_vault_core.py`).

**Tool signature** (Claude SDK `@tool` decorator):

```python
@tool(
    "render_doc",
    (
        "Render markdown content to a downloadable document file. "
        "Used for owner-facing reports, tables, summaries that benefit "
        "from formatted typography (PDF), Word-compatible editing "
        "(DOCX), or spreadsheet review (XLSX). Returns an artefact "
        "envelope; the bot delivers the file via Telegram automatically. "
        "DO NOT call this tool to log internal data — write to memory "
        "instead. Triggers: 'сделай PDF/DOCX/XLSX', 'сгенерь отчёт', "
        "'дай excel/word/pdf', 'render document'."
    ),
    {
        "type": "object",
        "properties": {
            "content_md": {
                "type": "string",
                "description": "Markdown source. For xlsx, must contain "
                               "exactly one pipe-syntax table.",
            },
            "format": {
                "type": "string",
                "enum": ["pdf", "docx", "xlsx"],
            },
            "filename": {
                "type": "string",
                "description": "Optional suggested filename without "
                               "extension. Sanitized server-side; "
                               "path components rejected.",
            },
        },
        "required": ["content_md", "format"],
    },
)
async def render_doc(args: dict[str, Any]) -> dict[str, Any]: ...
```

**Body dispatch** (sketch — implementation в coder phase):

1. `sub = get_configured_subsystem()`. Если `None` или
   `sub.force_disabled` → return `{ok: False, reason: "disabled",
   error: "render_doc subsystem not available"}`.
2. Validate `format ∈ {"pdf","docx","xlsx"}` (SDK enum проверит, но
   defensive).
3. Validate `content_md` length ≤ `RenderDocSettings.max_input_bytes`
   (default 1 MiB) → reject `input_too_large`.
4. Sanitize `filename` через `_sanitize_filename`:
   - reject если содержит `/`, `\\`, `\0`, `..`, leading dot, или
     длиной > 96 codepoints,
   - reject если после strip пустой,
   - filename = `<sanitized>.<ext>` (extension forced server-side из
     `format`),
   - default `<format>-<utc-iso>.<ext>` если `filename` отсутствует.
5. `await sub.render(content_md, format, sanitized_filename)` →
   `RenderResult` со staged path и bytes.
6. Append audit row.
7. Return artefact envelope (см. §2.3).

**Per-call timeout**: `await asyncio.wait_for(sub.render(...),
timeout=RenderDocSettings.tool_timeout_s)` (default 60s). Превышение →
audit `result="failed"`, return `{ok: False, reason: "timeout"}`.

**Concurrency**: tool body захватывает `RenderDocSubsystem._render_sem`
(asyncio.Semaphore, default size 2 — `render_max_concurrent`). Это
чтобы 4-5 параллельных PDF-render'ов не съели всю RAM (WeasyPrint
держит full-page DOM в памяти).

### 2.5 Adapter wiring — outbound document path

**Текущее состояние (sanity-check)**: `adapters/telegram.py`
обрабатывает inbound documents (phase 6a `_on_document`, phase 6b
photos, phase 6c voice), но **НЕ имеет** outbound document path.
`MessengerAdapter` ABC в `adapters/base.py` декларирует только
`send_text`. Phase 9 расширяет protocol первым outbound media-методом.

**`MessengerAdapter` extension**:

```python
class MessengerAdapter(ABC):
    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None: ...

    @abstractmethod
    async def send_document(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        suggested_filename: str | None = None,
    ) -> None: ...
```

**`TelegramAdapter.send_document`** реализация:

```python
from aiogram.types import FSInputFile

async def send_document(self, chat_id, path, *, caption, suggested_filename):
    if not path.is_file():
        log.error("send_document_missing_path", path=str(path))
        return  # silently — handler уже отдал текстовый блок про ошибку
    if path.stat().st_size > TELEGRAM_DOC_MAX_BYTES:
        await self._bot.send_message(
            chat_id,
            "файл слишком большой для Telegram (>20MB)",
        )
        return
    file = FSInputFile(path, filename=suggested_filename)
    await self._bot.send_document(chat_id, file, caption=caption)
```

Размер cap'ится `TELEGRAM_DOC_MAX_BYTES = 20 * 1024 * 1024` (тот же,
что и для inbound). Превышение → fallback text reply, артефакт
**остаётся** под artefact_dir и удаляется sweeper'ом по TTL.

**Handler-level wiring**: `ClaudeHandler` уже обрабатывает
`ToolResultBlock` через `_classify_block` (`role="user"`,
`block_type="tool_result"`). Phase 9 добавляет hook в bridge или
handler:

- **Опция A (CHOSEN)**: `ClaudeBridge` ведёт per-turn artefact ledger
  (`list[ArtefactRef]`) keyed по `tool_use_id`. На `ToolResultBlock`
  для `name=="mcp__render_doc__render_doc"` парсит JSON, при
  `kind=="artefact"` добавляет в ledger. Bridge публикует ledger
  через новое поле на `ResultMessage` мнимое — НЕТ, через
  callback/yield дополнительного "artefact"-блока в bridge stream.
  Handler пере-emit'ит его в adapter.
- **Опция B (REJECTED)**: handler сам парсит каждый ToolResultBlock —
  лезет в SDK detail и нарушает phase-2 layering.

Concrete A-flow:
1. Bridge при `_safe_query` встретив ToolResultBlock с
   `mcp__render_doc__render_doc` парсит content[0].text как JSON.
2. Если `kind=="artefact"` — bridge yield'ит специальный объект
   `ArtefactBlock(path: Path, format: str, suggested_filename: str)`
   ПОСЛЕ оригинального ToolResultBlock.
3. Handler в loop'е по bridge stream обнаруживает `ArtefactBlock` →
   складывает в local list `artefacts: list[ArtefactBlock]`.
4. ПОСЛЕ финального `await emit_text(...)` (handler уже дофлашил
   все text-chunks) handler делает
   `await self._adapter.send_document(chat_id, art.path,
   caption=None, suggested_filename=art.suggested_filename)` для
   каждого артефакта в порядке появления.
5. Caption = `None` v1; модель пусть сама пишет описание текстом
   ДО артефакта.

Это сохраняет invariant: первая текстовая часть turn'а доходит
владельцу как `send_text`, потом летит документ. Если render_doc
вернул `ok:false`, модель сама пересказывает ошибку текстом — adapter
ничего не знает про `kind:"error"`, потому что bridge не yield'ит
ArtefactBlock в этом случае.

Для не-Telegram адаптеров (потенциальный Yandex в будущем) `send_document`
обязан быть реализован; не-имплементированный stub'ит NotImplementedError
и handler ловит его + посылает text fallback.

### 2.6 PDF renderer — pandoc + WeasyPrint

**Pipeline**:
1. Записать `content_md` во временный файл
   `<data_dir>/artefacts/.staging/<uuid>.md` (sub-directory `.staging/`
   чтобы sweeper их не удалял до завершения; cleaned at staging-step
   exit).
2. `asyncio.create_subprocess_exec("pandoc", "-f", "markdown", "-t",
   "html5", "-o", "<staging_html>", "<staging_md>", env=<scoped>,
   ...)` с timeout `pdf_pandoc_timeout_s` (default 20s). Argv-form, не
   shell. `env=` — explicit copy of `os.environ` (см. §2.10 risk note
   — никаких daemon-wide env mutation).
3. Read HTML, передаём в `weasyprint.HTML(string=html,
   base_url="<staging_dir>").write_pdf("<final_path>")`. Запуск через
   `asyncio.to_thread` чтобы не блочить event loop.
4. WeasyPrint settings: `presentational_hints=False` (no inline
   styles), `optimize_images=True`. Custom CSS — НЕТ в v1.
5. Output PDF size cap: `pdf_max_bytes` (default 25 MiB) — checked
   post-render, `path.stat().st_size > cap` → unlink + return
   `render_failed`.
6. На каждом шаге исключения ловим, audit-row пишем
   `result="failed"` + `error="<exc class>"`. Никаких stack traces в
   model output.

**WeasyPrint security note**: WeasyPrint исполняет CSS, но НЕ
JavaScript. **Однако** `<iframe src="file:///etc/passwd">` или
`@import url(...)` в CSS могут потенциально читать локальные файлы или
делать сетевые запросы. Mitigation: pandoc `-f markdown -t html5` НЕ
пробрасывает raw HTML по умолчанию (требует `-f markdown+raw_html`),
но для рoughness **pandoc invocation MUST**:
- НЕ использовать `+raw_html` extension,
- использовать `--from=markdown_strict` либо
  `--from=markdown-raw_html` (проверить эмпирически в Wave A spike).
- WeasyPrint вызвать с `url_fetcher=<custom>` который reject'ит
  `file://` и любые non-http(s) схемы. См. §Риск (CRITICAL row про
  markdown injection).

### 2.7 DOCX renderer — pandoc native

**Pipeline**:
1. Staging md как §2.6.
2. `pandoc -f markdown -t docx -o <final_path> <staging_md>` через
   `asyncio.create_subprocess_exec` с timeout `docx_pandoc_timeout_s`
   (default 15s).
3. Output cap: `docx_max_bytes` (default 10 MiB).

DOCX-specific: pandoc по умолчанию embed'ит markdown-tables как Word
tables — owner ожидаемо это получит. Image references (`![](...)`) в
v1 НЕ поддерживаются — markdown с image-syntax всё равно прорендерится
(pandoc broken-image placeholder), но base_url для resolving не
выставляется → image будет broken. Это явный non-goal для phase 9 (см.
§Явно НЕ).

### 2.8 XLSX renderer — openpyxl over markdown pipe-tables

**Pipeline**:
1. `markdown_tables.parse(content_md)` → `list[Table]`. Pipe-syntax:

   ```
   | col A | col B | col C |
   |-------|-------|-------|
   | val 1 | val 2 | val 3 |
   ```

   Алгоритм: regex split строк по `|`; первая строка — headers; вторая
   — separator (`---`); остальные — body. Multiple таблиц → multiple
   parser hits.
2. **v1 ограничение**: `len(tables) != 1` → return `render_failed`
   с error `"xlsx requires exactly one markdown table; got N"`.
   Multi-sheet xlsx — §Явно НЕ.
3. `openpyxl.Workbook()` → `ws = wb.active` → write headers + rows.
   Header row bold (стандартный openpyxl `Font(bold=True)`); column
   widths auto-fit (best-effort: `max(len(cell)) + 2`).
4. Per-sheet caps: `xlsx_max_rows` (default 10000),
   `xlsx_max_cols` (default 50). Превышение → `render_failed`.
5. `wb.save(<final_path>)`. Запуск через `asyncio.to_thread`.

XLSX renderer НЕ зависит от pandoc — openpyxl чистый python wheel,
уже в pyproject.toml (phase 6a inbound parsing). Никаких новых
deps для xlsx-only.

### 2.9 Settings — `RenderDocSettings(env_prefix="RENDER_DOC_")`

Mounted на `Settings` как `settings.render_doc`. Mirror phase-8
`VaultSyncSettings`:

```python
class RenderDocSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RENDER_DOC_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    enabled: bool = True              # default True — owner explicitly asked
    artefact_dir: Path | None = None  # derived from data_dir if None
    artefact_ttl_s: int = 600         # 10 min — после доставки artefact уже не нужен
    sweep_interval_s: int = 60
    cleanup_threshold_s: int = 86400  # boot-time stale cleanup
    max_input_bytes: int = 1_048_576  # 1 MiB markdown
    tool_timeout_s: int = 60
    render_max_concurrent: int = 2
    audit_log_max_size_mb: int = 10
    pdf_pandoc_timeout_s: int = 20
    pdf_weasyprint_timeout_s: int = 30
    pdf_max_bytes: int = 25 * 1024 * 1024
    docx_pandoc_timeout_s: int = 15
    docx_max_bytes: int = 10 * 1024 * 1024
    xlsx_max_rows: int = 10000
    xlsx_max_cols: int = 50
    xlsx_max_bytes: int = 10 * 1024 * 1024

    @model_validator(mode="after")
    def _validate(self) -> "RenderDocSettings":
        if self.tool_timeout_s < (
            self.pdf_pandoc_timeout_s + self.pdf_weasyprint_timeout_s
        ):
            raise ValueError(
                "tool_timeout_s must >= pdf_pandoc_timeout_s + "
                "pdf_weasyprint_timeout_s (otherwise PDF path can't fit "
                "the worst-case pipeline)"
            )
        if self.render_max_concurrent < 1:
            raise ValueError("render_max_concurrent must be >= 1")
        return self
```

Default `enabled: True` обоснован: owner явно попросил эту фичу,
default-off сделает фичу невидимой пока кто-то не флипнет env-флаг.
Альтернатива (default `False` как у `vault_sync`) — см. §Развилки Q3.

### 2.10 Bridge wiring — `render_doc_tool_visible`

`ClaudeBridge.__init__` получает новый kwarg
`render_doc_tool_visible: bool = False` (mirror `vault_tool_visible`).
В `_build_options`:

```python
if self._render_doc_tool_visible:
    allowed_tools.extend(RENDER_DOC_TOOL_NAMES)
    mcp_servers["render_doc"] = RENDER_DOC_SERVER
```

`Daemon.start()` для **owner-bridge** только передаёт
`render_doc_tool_visible=settings.render_doc.enabled and not
self._render_doc.force_disabled`. Picker / audio bridges получают
default `False` — никогда не видят @tool, как `vault_push_now`.

### 2.11 Subprocess env scoping (phase-8 H3 carry)

Все pandoc invocations используют `env=` параметр на
`asyncio.create_subprocess_exec`. **НИКОГДА** не мутировать
`os.environ` daemon-wide. Это invariant из phase 8 (§2.3 H3 closure).
Phase 9 не добавляет process-wide env — pandoc нужен только PATH (он
там уже после Dockerfile-патча).

## 3. Задачи (Wave A → B → C → D)

### Wave A — System deps + smoke + skeleton (~700 LOC, 8 тестов)

Goal: **«никакой регрессии phase 8 ssh-not-found incident'а»**. Если
container не поднимается с pandoc или импорт WeasyPrint падает — ловим
в CI, не на live deploy.

A1. **`deploy/docker/Dockerfile` runtime stage** — добавить apt
    packages в `RUN apt-get install ...`:
    - `pandoc`,
    - `libcairo2`,
    - `libpango-1.0-0`,
    - `libpangoft2-1.0-0`,
    - `libgdk-pixbuf2.0-0`,
    - `fonts-dejavu-core` (минимальный набор Unicode fonts для
      кириллицы; без этого WeasyPrint выдаёт квадратики).
    Build-time sanity: `RUN pandoc --version && /opt/venv/bin/python -c
    "import weasyprint; print(weasyprint.__version__)"`.

A2. **`pyproject.toml` deps**: добавить `weasyprint>=63,<70` (CFFI-
    backed, требует cairo/pango). `openpyxl` уже есть. Pandoc — system
    binary, не Python dep.

A3. **`tests/test_phase9_render_doc_binaries.py`** — копия по образцу
    `test_phase8_ssh_binary_available.py`. Тесты:
    - `assert shutil.which("pandoc") is not None`,
    - `import weasyprint` succeeds (запуск smoke probe `WeasyPrint
      HTML("<p>x</p>").write_pdf(io.BytesIO())`).

A4. **`src/assistant/render_doc/` package skeleton** — пустые модули
    с docstrings + class signatures (no impl yet). `__init__.py`
    exports.

A5. **`src/assistant/config.py`** — `RenderDocSettings` class +
    mount на `Settings.render_doc` (default_factory). Validator из §2.9.

A6. **`src/assistant/render_doc/subsystem.py`** — `RenderDocSubsystem`
    skeleton: `__init__`, `startup_check` (тест на `shutil.which` +
    `import weasyprint`), `force_disabled` flag, `render()` placeholder
    raising `NotImplementedError`, `_sweep_loop` placeholder.

A7. **Tests**:
    - `test_render_doc_settings_defaults.py`,
    - `test_render_doc_settings_validator.py` (rejected configs),
    - `test_render_doc_subsystem_force_disable_on_missing_pandoc.py`
      (monkeypatch `shutil.which`),
    - `test_render_doc_force_disable_on_weasyprint_import_fail.py`
      (sys.modules monkeypatch),
    - `test_render_doc_disabled_when_settings_off.py`.

### Wave B — Renderers + @tool + audit (~900 LOC, 12 тестов)

B1. **`render_doc/markdown_tables.py`** — pipe-table parser. Pure
    python regex; no external dep. Reject malformed (no separator
    row, mismatched col count).

B2. **`render_doc/xlsx_renderer.py`** — openpyxl impl. Tests:
    single-table → 1 sheet, header bold, dates auto-detected как
    string (без auto-typing — owner может пересохранить и Excel
    сам распарсит). Reject multi-table.

B3. **`render_doc/docx_renderer.py`** — pandoc subprocess wrapper.
    Tests с mocked `asyncio.create_subprocess_exec` + один
    integration-mark тест с реальным pandoc (`pytest.mark.requires_pandoc`,
    skipped если `shutil.which` фейлит — поймает CI deps regression
    отдельно от A3).

B4. **`render_doc/pdf_renderer.py`** — pandoc → HTML5 →
    WeasyPrint(url_fetcher=safe). `safe_url_fetcher` reject'ит
    `file://`, `data://`, всё кроме `https?://` (для будущего; в v1
    images не embed'ятся).

B5. **`render_doc/audit.py`** — JSONL writer + rotation (полная копия
    `vault_sync/audit.py`).

B6. **`render_doc/_validate_paths.py`** — `_sanitize_filename`. Tests:
    `..`, `/`, `\\`, `\0`, leading dot, длина > 96, empty, valid
    cyrillic, valid spaces (replaced с `_`), valid unicode dashes.

B7. **`tools_sdk/_render_doc_core.py` + `tools_sdk/render_doc.py`** —
    @tool wrapper. Mirror phase-8 `_vault_core.py` + `vault.py`.
    `configure_render_doc(subsystem)` / `get_configured_subsystem()` /
    `reset_render_doc_for_tests()`.

B8. **Bridge wiring** — `ClaudeBridge.__init__` kwarg
    `render_doc_tool_visible` + conditional `mcp_servers["render_doc"]
    = RENDER_DOC_SERVER`. `RENDER_DOC_TOOL_NAMES =
    ("mcp__render_doc__render_doc",)`.

B9. **Tests**:
    - `test_markdown_tables_parser.py` (10+ params),
    - `test_xlsx_renderer_basic.py`,
    - `test_xlsx_renderer_multi_table_rejected.py`,
    - `test_docx_renderer_subprocess_mock.py`,
    - `test_docx_renderer_integration.py` (requires_pandoc mark),
    - `test_pdf_renderer_url_fetcher_reject_file.py`,
    - `test_pdf_renderer_integration.py` (requires_pandoc mark),
    - `test_render_doc_audit_rotation.py`,
    - `test_render_doc_filename_sanitization.py`,
    - `test_render_doc_tool_visibility_gated.py`,
    - `test_render_doc_tool_disabled_returns_envelope.py`,
    - `test_render_doc_tool_input_too_large.py`.

### Wave C — Adapter outbound + handler ledger (~500 LOC, 8 тестов)

C1. **`adapters/base.py::MessengerAdapter`** — добавить abstract
    `send_document` method. Update Yandex/Telegram impls / stubs.

C2. **`adapters/telegram.py`** — `send_document` impl с
    `FSInputFile`, size cap, missing-path log. Импорт
    `aiogram.types.FSInputFile`.

C3. **`bridge/claude.py`** — добавить ArtefactBlock yield path. Bridge
    парсит `ToolResultBlock` content[0].text как JSON ТОЛЬКО для
    `tool_use_name == "mcp__render_doc__render_doc"`; неrender_doc
    tool_results — passthrough.

C4. **`handlers/message.py`** — собирать ArtefactBlock'и в local list,
    ПОСЛЕ финального flush emit'ить через
    `adapter.send_document(...)`. Retry policy: send_document failure
    логируем + продолжаем (artefact умрёт через TTL).

C5. **TTL sweeper** — `RenderDocSubsystem._sweep_loop` impl,
    `_spawn_bg_supervised` в `Daemon.start()`.

C6. **Daemon wiring** — `Daemon.__init__` создаёт `_render_doc`
    placeholder; `Daemon.start()` AFTER vault_sync wiring (so phase-8
    ordering invariant preserved):
    - `_cleanup_stale_artefacts(artefact_dir)`,
    - `RenderDocSubsystem(...)`,
    - `await sub.startup_check()` (force_disable on missing deps),
    - `_vault_mod.configure_render_doc(subsystem=sub)` if
      `effective_enabled`,
    - `_spawn_bg_supervised(sub.start, name="render_doc_sweeper")`
      if not `force_disabled`,
    - bridge owner construction passes `render_doc_tool_visible=...`.

C7. **Tests**:
    - `test_phase9_artefact_envelope_yielded_by_bridge.py`,
    - `test_phase9_handler_send_document_after_emit.py`,
    - `test_phase9_telegram_send_document_size_cap.py`,
    - `test_phase9_telegram_send_document_missing_path.py`,
    - `test_phase9_sweeper_removes_old_artefacts.py`,
    - `test_phase9_cleanup_stale_artefacts_at_boot.py`,
    - `test_phase9_daemon_force_disable_continues_phase8_traffic.py`,
    - `test_phase9_daemon_drain_render_inflight.py` (если render
      длится дольше Daemon.stop budget — orphans cleaned by next-boot
      `_cleanup_stale_artefacts`).

### Wave D — phase-8 carry-overs (selectively bundled, ~400 LOC, 6 тестов)

Owner asked judgement on which phase-8 carry-overs to fold in. Я
рекомендую включить ровно ДВА — те, что низкорисковые и руки уже в
`vault_sync/`:

D1. **Audit log date-stamped rotation** (phase-8 carry-over): сейчас
    `os.replace -> .1` overwriting prior `.1` теряет историю при
    второй ротации. Rewrite `audit.py` (общий helper в
    `assistant/audit_helpers.py` или в-line копия) → ротировать в
    `<path>.<YYYYMMDD-HHMMSS>` с keep-last-N policy (default keep 5).
    **Применить к ОБОИМ** subsystem'ам: `vault_sync/audit.py` +
    `render_doc/audit.py` чтобы single source of truth.

D2. **CI host-key drift check** (phase-8 carry-over): добавить
    `.github/workflows/host-key-drift.yml` — еженедельный cron job,
    `gh api meta | jq -r '.ssh_keys[]' | diff -
    deploy/known_hosts_vault.pinned` → fail на расхождение, открывает
    issue. **Не часть phase-9 main flow**, но логически в Wave D потому
    что Dockerfile / CI трогаются в одном passe.

**Defer to phase 10** (намеренно НЕ включаем):
- Real-subprocess `git_ops.py` integration tests — большой объём,
  плохо изолирован.
- Periodic `startup_check` re-run для vault_sync — отдельный design
  question (как часто? что считать transient NFS pin?).
- Bootstrap script `git reset` between re-runs — слишком inv-specific.

D-tests:
- `test_audit_date_stamped_rotation.py` (применяется и к vault_sync
  и к render_doc),
- `test_audit_keep_last_n.py`,
- `test_phase9_dockerfile_apt_packages_present.py` (build-time grep
  по Dockerfile lines as static check).

## 4. Критерии готовности

**AC#1 — happy-path PDF**. Owner: «сгенерь PDF отчёт по последним
заметкам». Модель → `memory_search` → `render_doc(format="pdf",
content_md=…)` → telegram chat показывает (a) текстовый ответ модели,
(b) сразу следом — PDF-файл с suggested_filename. Файл открывается в
любом PDF-viewer'е, кириллица читаема, structure совпадает с input md.

**AC#2 — happy-path DOCX**. Аналогично AC#1 для `format="docx"`.
Word/LibreOffice открывает, structure preserved, кириллица OK.

**AC#3 — happy-path XLSX**. `content_md` = single pipe-table. Excel
открывает, header row bold, кириллица OK, столбцов не больше cap.

**AC#4 — `enabled=False` parity**. `RENDER_DOC_ENABLED=false` →
никакого `_sweep_loop` task spawn'а, никакого `mcp__render_doc__*`
@tool в model catalogue, никакого `<data_dir>/artefacts/` создания
(если не существовала). RSS observer не получает render_doc-related
поле. Model видит: «у меня нет инструмента для PDF, я могу только
текстом» (поведение без render_doc — модель просто не знает про
@tool).

**AC#5 — system-binary force-disable graceful**. Если pandoc binary
отсутствует ИЛИ `import weasyprint` падает на startup, daemon:
- логирует `event=render_doc_force_disabled`,
- one-time Telegram-нотифицирует владельца,
- продолжает работать со всеми остальными phase 1..8 ACs зелёными,
- @tool скрыт (model даже не предлагает вариант),
- запросы owner вроде «сделай PDF» модель должна обрабатывать как
  «не могу — субсистема отключена», но AC не контролирует точную
  формулировку.

**AC#6 — phase 1..8 regression-free** с любым значением
`RENDER_DOC_ENABLED`. Concrete enumeration:
- `/ping` (phase 2),
- `memory_*` @tools (phase 4),
- skill installer @tools (phase 3),
- scheduler @tools (phase 5b),
- file ingestion PDF/DOCX/TXT/MD/XLSX (phase 6a),
- photo / vision (phase 6b),
- voice / audio / URL (phase 6c),
- subagent Task tool (phase 6),
- audio bg dispatch (phase 6e),
- vault_sync `vault_push_now` + cron loop (phase 8).

**AC#7 — filename sanitization**. Adversarial inputs (`../etc/passwd`,
`/abs/path`, `..\..\nt`, `<file>\0`, leading-dot `.hidden`, длина >96)
→ rejected with `reason="filename_invalid"`. Cyrillic + spaces +
unicode dashes — accepted (sanitized к whitespace-stripped). Tests
parametrise.

**AC#8 — input size cap**. `content_md` > `max_input_bytes` →
`reason="input_too_large"`. Никаких promтption attempts (модель не
видит staged path).

**AC#9 — TTL sweeper removes old artefacts**. Plant artefact с mtime
старше `artefact_ttl_s + 5`s → следующий sweep tick его удалит. Audit
log row не пишется (sweeper — silent operation).

**AC#10 — boot-time stale cleanup**. Plant artefact с mtime > 24h
до daemon-start → `_cleanup_stale_artefacts` его удалит, free disk
наполняется.

**AC#11 — concurrency cap**. `render_max_concurrent=2`; 3 параллельных
вызова `render_doc` — третий ждёт пока один из первых двух завершит
(verify через mocked semaphore acquire trace).

**AC#12 — per-tool timeout**. PDF render с pandoc subprocess
hang'нувшимся (test mock returns never-resolving) → `tool_timeout_s`
истекает, audit `result="failed"` с error timeout, subprocess killed
cleanly (no zombies).

**AC#13 — Telegram size cap**. Render produces 21 MB PDF → adapter
видит `path.stat().st_size > TELEGRAM_DOC_MAX_BYTES` → text fallback
«файл слишком большой для Telegram»; sweeper удалит файл по TTL.

**AC#14 — markdown injection blocked at fetch layer**. content_md
с `<iframe src="file:///etc/passwd">` или `@import
url("file:///etc/shadow")` в CSS — `safe_url_fetcher` reject'ит,
WeasyPrint fall-through на пустую ссылку, в финальном PDF ничего
не leak'нуто. Audit row пишется с пометкой что fetch_url был
заблокирован (для post-mortem).

**AC#15 — system-binary smoke test green в CI**. Test
`test_phase9_render_doc_binaries.py` зелёный в test container.
Удаление `pandoc` из Dockerfile → CI идёт red БЕЗ deploy.

**AC#16 — @tool gated by `render_doc_tool_visible`**. Picker / audio
bridges не видят `mcp__render_doc__render_doc` в allowed_tools.
Owner-bridge видит только если
`settings.render_doc.enabled and not force_disabled`.

**AC#17 — outbound document path для Yandex stub'а**. Если адаптер
не реализует `send_document` (например, hypothetical Yandex skipped
implementation), handler ловит `NotImplementedError` → text fallback.
(В phase 9 Yandex не shipped, но contract checked.)

**AC#18 — audit log rotation date-stamped (Wave D)**. После 2-х
ротаций под нагрузкой сохранены 5 последних
`<path>.<YYYYMMDD-HHMMSS>` файлов; шестая ротация удаляет самый
старый. Test parametrise across `vault_sync` и `render_doc` — single
helper.

## 5. Явно НЕ в phase 9

1. **Templates / branding / header-footer**. Голый markdown rendering.
   Custom CSS, brand-colors, owner letterhead — phase 10+.
2. **Multi-sheet xlsx**. Single markdown table → single sheet only.
   Несколько таблиц в одном `content_md` → reject.
3. **Embedded images в PDF/DOCX**. `![](path)` markdown syntax
   рендерится как broken-image placeholder. Image embedding требует
   resolution path-policy + safe_url_fetcher с whitelist'ом — phase
   10+.
4. **PPTX, ODT, EPUB, HTML, plain TXT**. Только PDF / DOCX / XLSX.
5. **Async rendering / job queue**. Render синхронный в @tool body
   (с asyncio.to_thread для CPU-bound). Long-form renders >60s →
   timeout. Если owner регулярно превышает — phase 10 добавляет
   subagent-style background renderer.
6. **Output streaming**. Model не получает progress updates во
   время рендера; либо вернётся artefact envelope, либо ошибка.
7. **Custom fonts**. DejaVu из apt — единственный шрифт. Owner
   bundle'ит свои — phase 10.
8. **PDF/A archival format / signed PDF / encrypted PDF**.
9. **OCR на input PDF** (phase 6a уже отказался — symmetric решение).
10. **Render directly into vault** (без Telegram доставки). v1: только
    путь Telegram. Если owner хочет в vault — отдельная команда
    «save», phase 10.
11. **Render history retrieval** — owner: «дай тот pdf что был на
    прошлой неделе». Sweeper удалил → реgenerate. Persistent storage
    artefact'ов — phase 10.
12. **Render via subagent**. Subagent Task-pool НЕ имеет render_doc
    @tool в catalogue (`build_agents` уже не передаёт vault_push_now;
    тот же паттерн для render_doc).
13. **CSV / TSV format**. `format` enum закрыт.
14. **Russian locale в WeasyPrint**. Default Latin-1 fallback OK для
    Cyrillic через DejaVu; spec'ом не гарантируем locale-specific
    typography.

## 6. Зависимости

- **Phase 8 (КРИТИЧНО):**
  - Pattern для opt-in subsystem (`vault_sync` package shape).
  - Audit JSONL writer + rotation policy (`vault_sync/audit.py` —
    общий helper в Wave D).
  - `_spawn_bg_supervised` invariant + `force_disabled` graceful
    degradation pattern.
  - Boot-cleanup pattern (`_cleanup_stale_vault_locks` →
    `_cleanup_stale_artefacts`).
  - Conditional bridge wiring kwarg (`vault_tool_visible` →
    `render_doc_tool_visible`).
  - Per-subprocess `env=` scoping invariant (H3 closure carry).
- **Phase 6a:** `openpyxl>=3.1.5` уже в deps; XLSX renderer
  использует тот же wheel.
- **Phase 4:** Memory tools — модель формирует `content_md` из
  `memory_search` results (не зависимость кода phase 9, но E2E
  scenario AC#1 это требует).
- **Phase 2:** Bridge layered architecture, hooks system, system
  prompt assembly — phase 9 не модифицирует bridge core, только
  добавляет mcp_server registration.
- **External Python deps (новые):**
  - `weasyprint>=63,<70` — PDF rendering. CFFI-backed; pyproject pin
    разумный; bookworm wheel поддерживает Python 3.12.
- **External system deps (новые apt пакеты в Dockerfile runtime):**
  - `pandoc` (~85 MB на bookworm),
  - `libcairo2` (~1 MB),
  - `libpango-1.0-0` (~1 MB),
  - `libpangoft2-1.0-0` (<1 MB),
  - `libgdk-pixbuf2.0-0` (~1 MB),
  - `fonts-dejavu-core` (~3 MB).
  Total estimated image size delta ≤ 95 MB. Budget caps в §Риск.
- **Внутренние deps (карнирование):**
  - `Daemon._bg_tasks` / `_spawn_bg_supervised` — reused для sweeper
    loop.
  - `MessengerAdapter` ABC — extension в `adapters/base.py`.
  - `ClaudeBridge` — extension с `render_doc_tool_visible` kwarg.
  - `ClaudeHandler` — extension с artefact ledger collection.

## 7. Риск

| Severity | Risk | Mitigation |
|---|---|---|
| 🔴 CRITICAL | **Markdown injection через `content_md`** — модель может (ошибочно или adversarially) вставить `<iframe src="file://...">` или CSS `@import url(...)` → WeasyPrint попытается зачитать локальные файлы (CVE-style local file disclosure) | Pandoc invocation БЕЗ `+raw_html` extension; WeasyPrint вызывается с custom `url_fetcher` который reject'ит всё кроме `https?://`; ОТДЕЛЬНЫЙ researcher pass требуется до coder phase для verify (RQ: подтвердить что pandoc default markdown variant action effectively strips raw HTML). AC#14. |
| 🔴 CRITICAL | **Image size bloat от pandoc + cairo/pango** | Hard budget: image size delta ≤ 100 MB после Wave A. CI step `docker images --format` фиксирует размер; PR template содержит «before/after image size MB». Если превышает — обсуждаем drop fonts-dejavu-core (заменить на font-fallback CSS). |
| 🟠 HIGH | **Pandoc crash / hang на malformed markdown** | Per-subprocess timeout `pdf_pandoc_timeout_s` / `docx_pandoc_timeout_s`; kill-on-timeout через `proc.kill()` + `await proc.wait()`. Test parametrize over malformed inputs (unclosed code-block, deep nesting, 100k repeating chars). |
| 🟠 HIGH | **WeasyPrint OOM на огромных DOCs** | `max_input_bytes=1MiB` cap пред-render. `pdf_max_bytes=25MiB` cap пост-render. RSS observer phase 6e уже есть — добавить `render_doc_inflight=N` field (по аналогии с `vault_sync_pending`). |
| 🟠 HIGH | **Disk fill от artefacts/** | TTL sweeper bg-loop (default 600s TTL, sweep каждые 60s). Boot-time `_cleanup_stale_artefacts` removes >24h files. Per-render concurrency cap `render_max_concurrent=2`. |
| 🟡 MEDIUM | **Filename path-traversal** через model-provided `filename` | `_sanitize_filename` reject'ит `/`, `\\`, `..`, `\0`, leading-dot, length>96. Extension forced server-side из `format`. AC#7. |
| 🟡 MEDIUM | **Subprocess env leak** между tools (phase-8 H3 carry) | `env=` параметр на каждый `asyncio.create_subprocess_exec`; daemon never `os.environ.update`. Test `test_phase9_pandoc_env_scope.py` mirroring phase-8 `test_phase8_git_ssh_command_scope.py`. |
| 🟡 MEDIUM | **CI test image не извлекает из главного pipeline** — Dockerfile test target наследует runtime, но если runtime сломан, test тоже. | A1 build-time sanity (`pandoc --version`) — runtime build ломается ДО test target. Owner deploy verifies. |
| 🟡 MEDIUM | **xlsx из bad pipe-tables** — owner ожидает spreadsheet, получает render_failed | Чёткая error message `"xlsx requires exactly one markdown table; got N"` → модель пересказывает владельцу + retry с правильной структурой. AC#3 + parser robust tests. |
| 🟢 LOW | **Telegram 20 MiB cap** на send_document | Adapter pre-check size; text fallback при превышении (AC#13). |
| 🟢 LOW | **Cyrillic font fallback** в WeasyPrint | DejaVu в Dockerfile apt list. AC#1/2 проверяет визуально на live deploy. |
| 🟢 LOW | **WeasyPrint version bump breakage** | Pin `>=63,<70`. Major bump — отдельный phase. |

## 8. Развилки для Q&A (before detailed-plan)

**Q1 — Outbound document path: extend `MessengerAdapter` ABC vs only
TelegramAdapter concrete method?** Default: extend ABC + Yandex stub
NotImplementedError. Reasoning: phase 10+ Yandex/Discord должны
обязаны реализовать `send_document` (это user-facing feature gate).
Альтернатива — leave ABC text-only, telegram-specific метод вызывать
через `isinstance(adapter, TelegramAdapter)` проверку из handler. Это
решение хочется одобрить ДО coder phase.

**Q2 — Default `enabled` value.** Owner asked для in-scope feature →
default `True` сильнее. Однако: subsystem делает apt-install delta +
WeasyPrint memory pressure; opt-in мог бы дать боту safer first-deploy.
Default proposal: `True` (но `force_disabled` graceful если pandoc/
weasyprint отсутствуют). Альтернатива: `False`, owner flip'нёт после
verify. Reviewer wave может разойтись.

**Q3 — TTL vs per-turn delete.** v1 spec'ом — TTL sweeper. Альтернатива
— delete-immediately-after-`send_document`. Pro per-turn delete: zero
disk footprint после доставки. Con: send_document failures (network,
TG retry) лишают модель возможности retry. TTL — safer для retry-on-
failure scenarios. Default proposal: TTL. Окончательно подтвердить.

**Q4 — `filename` сanitization агрессивность.** Cyrillic supported,
но что насчёт emojis (`📄отчёт`)? Telegram caps suggested filename
length и filesystem может не любить некоторые codepoints (NTFS
reserves). Default proposal: разрешить любой printable Unicode после
strip control chars; cap 96 codepoints. Альтернатива — strict ASCII
only.

**Q5 — Audit log date-stamped rotation в Wave D.** Применять к
`vault_sync` retroactively значит трогать phase-8 invariants (изменить
audit format). Если owner хочет minimal-blast, оставить
`vault_sync/audit.py` как есть, новую rotation policy ввести только в
`render_doc/audit.py`. Default proposal: общий helper применить к
ОБОИМ — но это чуть-чуть expand scope phase 9.

**Q6 — Subagent видимость render_doc @tool.** Phase-8 `vault_push_now`
скрыт от subagent (`build_agents` не пробрасывает в Task pool). Логика
для render_doc — subagent теоретически мог бы рендерить отчёт по
исследованию. Default proposal: НЕ давать subagent'у render_doc в
phase 9 (consistency с vault). Но это закрывает useful use-case.
Окончательно — owner.

**Q7 — Markdown variant.** `pandoc -f markdown` (по умолчанию
`pandoc_markdown` со всеми extensions) vs `pandoc -f markdown_strict`
(commonmark-like) vs `pandoc -f markdown-raw_html`. От этого зависит
markdown injection mitigation (см. §Риск CRITICAL). Default proposal:
`-f markdown-raw_html` (отключает inline HTML), плюс researcher
verifies в Wave A spike.

**Q8 — Concurrency cap default.** `render_max_concurrent=2` — pulled
из воздуха. WeasyPrint single-page обычно ≤200 MB peak RSS; 2
параллельных безопасно для 1 GB VPS. 3 — рискованно если PDF большой.
Default OK, но reviewer пусть проверит на live VPS RSS-budget'е.

**Q9 — Включать ли `vault_sync` audit-rotation refactor (Wave D D1) в
phase 9?** Если YES — двойной touch (vault_sync + render_doc helper);
если NO — только render_doc/audit.py получает date-stamped, vault_sync
остаётся single-step `.1`. Default proposal: YES (общий helper, без
него получится копи-паст с расхождениями).

**Q10 — Telegram caption на `send_document`.** v1: `caption=None`,
модель сама пишет текстовый контекст ДО артефакта. Альтернатива —
synthesize caption из markdown headings (первый h1). Default
proposal: None (модель в курсе context, owner получает text+file
последовательно).

---

> **Phase-8 integration note (vault vs artefacts).** Phase 8 vault dir
> `<data_dir>/vault/` и phase 9 artefact dir
> `<data_dir>/artefacts/` физически разнесены. Vault — git working
> tree, artefacts — TTL-управляемый ephemeral pool. Никакого
> overlap, `.gitignore` vault'а не нужно расширять. Если owner
> когда-нибудь захочет «сохрани этот PDF в vault» — это будет
> отдельная команда / @tool для копирования из artefact_dir в vault
> (out of scope phase 9, см. §5 #10).
