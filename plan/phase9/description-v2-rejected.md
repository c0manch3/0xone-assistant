# Phase 9 — description v2 (REJECTED)

Rejected on 2026-05-02 after researcher pass surfaced 2 BLOCKING
issues (URLFetchingError silently swallowed; obsolete apt deps) +
pandoc 2.17 vs 3.x version drift + image budget recalibration.
See `research-findings.md` and `description.md` (v3) for resolution.

---

# Phase 9 — render_doc: PDF / DOCX / XLSX генерация по запросу модели

> Spec v2 — closes devil w1 (5 CRIT + 6 HIGH + 7 MED) + devil w2
> (1 CRIT + 2 HIGH + 5 MED + 4 LOW). LOW judgements from w1 documented
> в §9; w2 closures documented в §10. Owner-frozen scope (3 формата, no
> templates, no multi-sheet xlsx, no backwards-compat). Архитектура
> повторяет phase-8 vault_sync: опт-ин subsystem package под
> `src/assistant/render_doc/`, единственный MCP @tool `render_doc` под
> отдельной MCP-группой `mcp__render_doc__`, conditional bridge wiring
> через `render_doc_tool_visible` kwarg по образцу `vault_tool_visible`.
> Phase 9 знакомит бота с outbound document delivery через
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
> - **W2-MED-5 strengthening**: integration-test acceptance делает
>   asymmetric anti-regression — output-side issues (PDF magic bytes,
>   DOCX zipfile parse) проверяются в test container c real pandoc +
>   real WeasyPrint, не только binary-presence side.
> - Dockerfile runtime stage расширяется apt-пакетами (`pandoc`,
>   `libcairo2`, `libpango-1.0-0`, `libpangoft2-1.0-0`,
>   `libgdk-pixbuf2.0-0`, `fonts-dejavu-core`); image size budget зашит в
>   §Риск + CI-gate (Wave A A8, MED-7 closure).
> - Auth = Claude OAuth ТОЛЬКО. Никакого `ANTHROPIC_API_KEY`.
> - Никаких backwards-compat shims; spec coherent end-to-end.
>
> v0→v1 deltas (high-level):
>
> - §2.5 переписан: per-iteration flush barrier (CRIT-1) +
>   schema_version envelope (MED-6) + partial-failure inline-text
>   (HIGH-3).
> - §2.6 переписан: full URL fetch surface enumeration (CRIT-2) +
>   strict pandoc markdown variant + custom `safe_url_fetcher`.
> - §2.4 переписан: explicit `unicodedata`-based sanitization rule +
>   Windows-reserved + bidi/ZWSP strip (CRIT-5).
> - §2.2 + §2.9 разделили force-disable на per-format flags (HIGH-5).
> - **NEW §2.12** «Render lifecycle vs Daemon.stop» — drain set,
>   pandoc SIGTERM, WeasyPrint-uncancellable honesty (CRIT-4).
> - **NEW §2.13** «In-flight artefact ledger» — sweeper не race'ит
>   send_document (CRIT-3).
> - §2.11 ужесточена: pandoc env-whitelist `{PATH, LANG, HOME}`
>   (HIGH-1).
> - §3 Wave D ужалена: оставлен ТОЛЬКО `render_doc/audit.py`
>   date-stamped rotation; `vault_sync/audit.py` НЕ трогаем (LOW-2 +
>   owner compromise note); host-key drift CI выкинут в phase 10
>   (LOW-3).
> - §3 Wave A добавлен A8 — image size CI gate (MED-7).
> - §3 Wave C расширен `_render_doc_pending` drain set + 6 новых
>   тестов (CRIT-3, CRIT-4, HIGH-3 + HIGH-6).
> - Test count budget: ~31 (v0) → ~62 (v1).
> - AC#14 расширена в AC#14a–AC#14i (CRIT-2 fetch surfaces).
> - Новые AC: #19 (per-iteration flush) — #28 (envelope schema_version).
>
> v1→v2 deltas (devil w2 closures, high-level):
>
> - **W2-CRIT-1**: §2.5 ABC extension получил Option A migration —
>   `send_document` объявлен **non-abstract** с default body `raise
>   NotImplementedError`; tests/conftest.py добавляет shared
>   `_NoopAdapter` fixture; явный список 6 fixtures в новом §3.6
>   «Test fixture migration». Существующие phase 5b/6e/8 тесты
>   остаются GREEN после Wave C C1.
> - **W2-HIGH-1**: §2.12 cumulative drain budget — `render_drain_
>   timeout_s` снижен 50s → **20s** (pandoc SIGTERM at 5s, SIGKILL at
>   10s; WeasyPrint thread «best-effort drain, force-orphan after
>   20s»). Total Daemon.stop budget = 30s (typing) + 60s (vault) +
>   20s (render) = ~110s worst-case → user-mandated cap 80s
>   (TimeoutStopSec=90s − 10s safety). Документировано в §2.12 с
>   честным acknowledgement что в actual settings (compose
>   stop_grace_period=35s, systemd TimeoutStopSec=30s) даже 80s budget
>   не помещается; render drain — первое, что cancellation cuts на
>   SIGKILL boundary; orphan pandoc PIDs accepted residual risk.
> - **W2-HIGH-2**: §2.13 explicit «Lock acquisition discipline» —
>   `register_artefact` / `mark_delivered` / `_sweep_loop` ВСЕ
>   acquire `_artefacts_lock`. Sweep iterates snapshot copy
>   `list(self._artefacts.items())` под lock, unlink происходит
>   снаружи lock-а.
> - **W2-MED-1**: §2.6 step 2 reword — pandoc invocation добавляет
>   `-tex_math_dollars-tex_math_single_backslash` в variant string,
>   defang'ит inline math `$E=mc^2$` чтобы избежать MathML side-
>   effects. Researcher Wave A spike verifies.
> - **W2-MED-2**: §5 #3 + §2.6 step 4 wording aligned — `data:image/*`
>   allowlist **DROPPED entirely**. Любой `data:` URI блокируется
>   `safe_url_fetcher`, image embedding всех видов (включая data:
>   inline) — out of scope phase 9.
> - **W2-MED-3**: §2.9 validator получил check `render_drain_
>   timeout_s >= pdf_pandoc_timeout_s + pdf_weasyprint_timeout_s` —
>   prevents owner setting drain ниже worst-case PDF pipeline.
> - **W2-MED-4**: §2.2 audit truncation cap расширен на BOTH
>   `filename` AND `error` (256 codepoints each). Future audit
>   field additions inherit truncation default через
>   `_truncate_str_fields(row, max_chars=256)` helper.
> - **W2-MED-5**: §3 Wave B B3+B4 integration-test acceptance —
>   `requires_pandoc + requires_weasyprint_runtime` test produces
>   real PDF/DOCX в test container, asserts byte-level structure
>   (`%PDF-` magic + `%%EOF` trailer; `zipfile.is_zipfile` для
>   docx). AC#15a strengthened.
> - **W2-LOW-1**: NEW §2.14 «Observability events» — table структуры
>   логов с обязательными полями + RSS observer integration mandated
>   с `render_doc_inflight=N` field. AC#29 NEW.
> - **W2-LOW-2**: §7 Risk row + §6 size estimates — pandoc cold-start
>   latency 200-400ms documented; researcher Wave A spike measures
>   p50/p99.
> - **W2-LOW-3**: §2.4 CRIT-5 sanitization matrix expanded на bare
>   `"."` + dots-only inputs (rows 13-14).
> - **W2-LOW-4**: §5 #19 NEW — RTL Hebrew/Arabic typography
>   not-guaranteed; DejaVu carries glyphs but bidi shaping
>   imperfect; phase 11+ если owner needs.
> - Test count budget: ~62 (v1) → **~70** (v2). Net +8: w2 added 6
>   targeted (B5 cumulative-budget, B5 audit-double-truncate,
>   B6 «.» edge, B6 dots-only, B5 ledger-concurrent-mutations,
>   B5 inline-math-survives-variant); compose-stop-grace-period
>   static check is documentation-only (no test); RSS-observer-
>   field test is +1 в Wave C; integration-test PDF/DOCX bytes
>   structure +0 (already counted в B3/B4). Total v2 = 70.
> - Новые AC: AC#21a (cumulative drain), AC#29 (RSS observer),
>   AC#14j (inline math survives variant subtraction).

## 1. Цель

Дать модели возможность создавать визуальные документы по запросу
владельца («сделай PDF отчёт по последним заметкам», «сгенерь docx с
тем-то», «дай excel таблицу из vault notes») и доставлять результат в
Telegram как файл. Модель пишет содержимое в markdown и вызывает
единственный MCP @tool `render_doc(content_md, format, filename?)` →
бот рендерит файл на диск под `<data_dir>/artefacts/<uuid>.<ext>` →
turn-output несёт structured artefact-envelope → Telegram-адаптер
отдаёт `bot.send_document(...)` владельцу → файл TTL-sweeper удаляет
артефакт через `artefact_ttl_s` секунд (с in-flight guard, см. §2.13).

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
   FSInputFile(path), caption=…)` → файл доставлен → `subsystem.
   mark_delivered(path)` снимает in-flight guard.
6. Sweeper background loop по `artefact_ttl_s` (default 600s = 10
   минут) удаляет файлы под `<data_dir>/artefacts/`, для которых
   in-flight ledger показывает `not in_flight AND now - delivered_at >
   artefact_ttl_s`. Это защищает от disk fill при сбое доставки или
   нескольких параллельных render'ов в одном turn И НЕ race'ит upload
   mid-stream (CRIT-3 closure, см. §2.13).

### 2.2 Subsystem package — `src/assistant/render_doc/`

Mirror phase-8 vault_sync layout:

- `__init__.py` — exports `RenderDocSubsystem`, `_cleanup_stale_artefacts`.
- `subsystem.py` — `RenderDocSubsystem` class:
  - `__init__(settings, artefact_dir, ...)`,
  - `start()` / `stop()` — управляет TTL-sweeper bg-loop;
    daemon spawn'ит через `_spawn_bg_supervised` по аналогии с
    `vault_sync.loop`,
  - `startup_check()` — `shutil.which("pandoc")` + `import weasyprint`
    проверяются раздельно. Результат записывается в
    `force_disabled_formats: set[str]` (HIGH-5 closure):
    - pandoc отсутствует → `{"pdf", "docx"}` добавляются,
    - `import weasyprint` падает → `{"pdf"}` добавляется,
    - openpyxl всегда доступен (pure-python wheel) → "xlsx" никогда не
      попадает в force_disabled_formats from startup_check.
    Полный force-disable subsystem'а (когда **все** форматы заблокированы)
    устанавливает `force_disabled = True` + `disabled_reason: str`,
    @tool скрыт целиком, log line `event=render_doc_force_disabled` +
    `reason=…` идёт в Telegram владельцу одной нотификацией на boot.
    Если хотя бы xlsx остаётся доступен — subsystem **частично**
    enabled, @tool регистрируется, но per-call отвергает заблокированные
    форматы (см. §2.4).
  - `render(content_md, format, filename, *, task_handle) -> RenderResult`
    — публичный API, дёргается из @tool body. Под капотом — dispatch на
    pdf_renderer / docx_renderer / xlsx_renderer. `task_handle` это
    `asyncio.Task` текущего @tool body — регистрируется в
    `_render_doc_pending` для drain в `Daemon.stop` (§2.12).
  - `_sweep_loop()` — TTL sweeper. Алгоритм по §2.13.
  - `mark_delivered(path: Path) -> None` — handler вызывает после
    успешного / финально-провального send_document. Снимает
    `in_flight=True` флаг с записи ledger'а; sweeper только теперь
    может удалить файл (§2.13).
  - `register_artefact(...)` / `_artefacts: dict[Path, ArtefactRecord]`
    — in-memory live-set ledger (§2.13).
  - `force_disabled: bool` + `disabled_reason: str | None` —
    subsystem-wide toggle.
  - `force_disabled_formats: set[str]` — per-format toggle (HIGH-5).
- `pdf_renderer.py` — pandoc + WeasyPrint pipeline (см. §2.6).
- `docx_renderer.py` — pandoc native (см. §2.7).
- `xlsx_renderer.py` — openpyxl over `markdown_tables` parser в
  `write_only=True` mode (см. §2.8 + HIGH-4).
- `markdown_tables.py` — pure-Python pipe-syntax table parser. Принимает
  `content_md`, возвращает `list[Table]` (header + rows).
- `audit.py` — JSONL audit log writer. Path:
  `<data_dir>/run/render-doc-audit.jsonl`. Schema: `{"ts": iso,
  "format": "pdf|docx|xlsx", "result": "ok|failed|disabled", "filename":
  str, "bytes": int|null, "duration_ms": int, "error": str|null,
  "schema_version": 1}`. Rotation policy: `audit_log_max_size_mb`
  (default 10) → date-stamped rotation `<path>.<YYYYMMDD-HHMMSS>` с
  keep-last-N (default 5) (Wave D D1; **только в render_doc**, vault_sync
  audit не трогаем — см. LOW-2 closure / Q9 решение).

  **W1-MED-1 + W2-MED-4 closure (truncation)**: ВСЕ variable-length
  string fields в audit row pre-truncated через
  `_truncate_str_fields(row, max_chars=256)` helper ДО JSON
  serialisation. v2 truncation cap = **256 codepoints** (was 512 в
  W1-MED-1 для `error` only; W2-MED-4 narrows AND extends to cover
  filename + future audit fields). Helper iterates `row.items()` и
  applies cap к каждому str-typed value:
  - `filename` (model/owner-controlled input): cap 256 codepoints
    (after _sanitize_filename, real-world filenames ≤ 96 — но
    sanitization runs ДО audit, и edge cases где raw filename
    leaked в audit row нуждаются защиты),
  - `error` (machine-parseable kebab-code, normally <50 chars; cap
    к 256 предохраняет от регрессий),
  - any future str field added к schema наследует cap automatically.
  Full error остаётся в structured log (никакого truncation для
  log lines). Test
  `test_render_doc_audit_field_truncation_uniform.py` (Wave B B5
  expansion) parametrises на `filename` overflow + `error` overflow.
- `boot.py` — `_cleanup_stale_artefacts(artefact_dir)` для запуска
  при `Daemon.start()` BEFORE subsystem spawn (по аналогии с
  `_cleanup_stale_vault_locks`). MED-4 closure: walks **обе**
  под-директории:
  - `artefact_dir/` — final artefacts. Удаляет файлы с mtime >
    `cleanup_threshold_s` (default 86400 = 24h) — защита от забытых
    файлов при crash daemon.
  - `artefact_dir/.staging/` — staging files (orphaned pandoc inputs
    при SIGKILL). Удаляет UNCONDITIONAL — staging files по определению
    orphans, healthy daemon чистит их per-call в finally.
- `_validate_paths.py` — `_sanitize_filename` helper (см. §2.4).

### 2.3 Artefact envelope contract

Поле в `render_doc` MCP @tool result:

```python
{
    "ok": True,
    "result": "rendered",
    "kind": "artefact",
    "schema_version": 1,  # MED-6: bump for breaking format changes
    "format": "pdf",  # или "docx" / "xlsx"
    "path": "/home/bot/.local/share/0xone-assistant/artefacts/<uuid>.pdf",
    "suggested_filename": "отчёт-vault-2026-05-02.pdf",
    "bytes": 482301,
    "expires_at": "2026-05-02T12:34:56Z",  # now + artefact_ttl_s (advisory only;
                                            # actual TTL governed by §2.13 ledger)
    "tool_use_id": "tool_use_<sdk-id>"  # echoed for ledger keying (MED-5)
}
```

`content` блок MCP содержит **тот же словарь** как одна `text`-block
(JSON-stringified) — без этого SDK не покажет данные модели как
обычный tool_result. Bridge при обработке `ToolResultBlock` со
`name == "mcp__render_doc__render_doc"` парсит JSON-нагрузку (см.
§2.5) и сохраняет path в ledger keyed по `tool_use_id`.
**MED-6 closure**: первое поле, которое читает bridge, —
`schema_version`. Если ≠ 1, bridge log'ает structured warning
`event=render_doc_envelope_unknown_schema_version`, **пропускает**
ArtefactBlock yield (graceful degradation: модель видит ToolResult
текстом, owner получает текст без файла, но boot не падает). См.
AC#28.

**MED-2 closure**: `format_invalid` reason **выкинут** из enum.
SDK enum-валидация на input schema reject'ит unknown format ДО @tool
body. Если этот код-path всё-таки достигнут, log'аем как inv-violation
и возвращаем `render_failed_internal` (§2.3 ниже).

`ok=False` envelope (полностью отдельный path, MED-3 closure
expanded):

```python
{
    "ok": False,
    "kind": "error",
    "schema_version": 1,
    "reason": (
        "disabled" |              # subsystem или формат force_disabled
        "filename_invalid" |       # _sanitize_filename rejection
        "input_too_large" |        # content_md > max_input_bytes
        "render_failed_input_syntax" |   # MED-3: pandoc parse error,
                                          # markdown_tables.parse fail
        "render_failed_output_cap" |     # MED-3: post-render bytes > cap
        "render_failed_internal" |       # MED-3: catch-all
        "timeout"
    ),
    "error": "<short kebab-case machine-parseable code>",
        # MED-3: e.g. "pandoc-exit-1", "weasyprint-cairo-error",
        # "openpyxl-too-many-rows", "markdown-no-tables",
        # "subsystem-not-configured"
}
```

Adapter не должен делать `send_document` если `ok=False` или `kind !=
"artefact"`. Модель видит JSON и сама пересказывает ошибку владельцу
текстом. С детализированными MED-3 reason'ами модель может ветвить:
`render_failed_input_syntax` → попробовать другой markdown,
`render_failed_output_cap` → разбить на меньшие куски.

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
   error: "subsystem-not-configured"}`.
2. **HIGH-5 closure** — per-format check: `if format in
   sub.force_disabled_formats:` → return `{ok: False,
   reason: "disabled", error: f"format-{format}-unavailable-{sub.disabled_reason or 'binary-missing'}"}`.
   Это позволяет xlsx работать когда pandoc отсутствует.
3. Validate `content_md` length ≤ `RenderDocSettings.max_input_bytes`
   (default 1 MiB) → reject `input_too_large`.
4. **CRIT-5 closure** — sanitize `filename` через `_sanitize_filename`
   (см. precise rule ниже).
5. `await sub.render(content_md, format, sanitized_filename,
   task_handle=asyncio.current_task())` → `RenderResult` со staged path
   и bytes. Регистрация task_handle в `_render_doc_pending` происходит
   внутри `subsystem.render()` (§2.12).
6. Append audit row.
7. Return artefact envelope (см. §2.3).

**Per-call timeout**: `await asyncio.wait_for(sub.render(...),
timeout=RenderDocSettings.tool_timeout_s)` (default 60s). Превышение →
audit `result="failed"`, return `{ok: False, reason: "timeout",
error: "tool-timeout-exceeded"}`.

**Concurrency**: tool body захватывает `RenderDocSubsystem._render_sem`
(asyncio.Semaphore, default size 2 — `render_max_concurrent`). Это
чтобы 4-5 параллельных PDF-render'ов не съели всю RAM (WeasyPrint
держит full-page DOM в памяти).

#### CRIT-5 closure: `_sanitize_filename` precise rule

```python
import unicodedata

_REJECTED_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn"}
# Cc=control, Cf=format (incl. ZWSP/ZWJ/U+202E bidi),
# Co=private-use, Cs=surrogate, Cn=unassigned.
_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$",
    re.IGNORECASE,
)

def _sanitize_filename(raw: str | None, fmt: str) -> str | None:
    """Return cleaned filename WITHOUT extension, or None if rejected.
    Caller appends ``.{fmt}``.
    """
    if raw is None or not raw:
        return None  # caller will use default
    # Strip rejected unicode categories (silent — owner gets clean name).
    cleaned = "".join(
        c for c in raw
        if unicodedata.category(c) not in _REJECTED_CATEGORIES
    )
    cleaned = cleaned.strip()  # leading/trailing whitespace
    if not cleaned:
        raise FilenameInvalid("empty after normalisation")
    # W2-LOW-3: dots-only inputs (".", "..", "...") all reject —
    # post-strip they look like dot-prefix or traversal but require
    # explicit row in matrix.
    if cleaned.replace(".", "") == "":
        raise FilenameInvalid("dot-prefix-or-traversal")
    # Reject path components.
    if any(sep in cleaned for sep in ("/", "\\", "\0")):
        raise FilenameInvalid("path-components")
    # Reject .. / leading dot.
    if cleaned.startswith(".") or ".." in cleaned:
        raise FilenameInvalid("dot-prefix-or-traversal")
    # Reject trailing dot/space (Windows compat).
    if cleaned[-1] in (".", " "):
        raise FilenameInvalid("trailing-dot-or-space")
    # Reject Windows-reserved basenames.
    base = cleaned.split(".", 1)[0]
    if _WINDOWS_RESERVED.match(base):
        raise FilenameInvalid("windows-reserved")
    # Length cap (codepoints, not bytes).
    if len(cleaned) > 96:
        raise FilenameInvalid("too-long")
    return cleaned
```

**Emoji policy** (CRIT-5 + LOW-7 explicit): Unicode category `So`
(other-symbol, includes emojis like `📊`) and `Sm/Sc/Sk` (math /
currency / modifier-symbol) **accepted**. Cyrillic letters `Ll/Lu/Lt/
Lm/Lo` accepted. Whitespace inside name (after strip) accepted. CRIT-5
matrix:

| Input | Result |
|---|---|
| `"CON"` | reject `windows-reserved` |
| `"con.report"` | reject (basename `con` matches) |
| `"report.con"` | accept (reserved-only on basename) |
| `"a\u202Eb"` (RTL override) | strip → `"ab"` |
| `"a\u200Bb"` (ZWSP) | strip → `"ab"` |
| `"report ."` | reject `trailing-dot-or-space` |
| `"report . "` | reject (after strip → `"report ."`) |
| `"\u0000a"` | strip Cc → `"a"` |
| `"📊отчёт"` | accept |
| `"../etc/passwd"` | reject `dot-prefix-or-traversal` |
| `".hidden"` | reject `dot-prefix-or-traversal` |
| empty / whitespace-only | reject `empty after normalisation` |
| `"."` (W2-LOW-3) | reject `dot-prefix-or-traversal` |
| `"..."` (W2-LOW-3, dots-only) | reject `dot-prefix-or-traversal` |

`FilenameInvalid` exception caught by @tool body → return
`{ok: False, reason: "filename_invalid", error: f"sanitize-{<code>}"}`.

Default filename when `raw is None`: `f"{fmt}-{utc_iso}"` (e.g.
`pdf-2026-05-02T12-34-56Z`). Extension ALWAYS forced server-side из
`format` argument.

**suggested_filename in envelope** = `f"{cleaned}.{fmt}"`.

### 2.5 Adapter wiring — outbound document path + per-iteration flush

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
**остаётся** под artefact_dir, in-flight ledger освобождается через
`mark_delivered`, файл удаляется sweeper'ом по TTL.

#### CRIT-1 closure: per-iteration flush barrier

**Old v0 contract** (REJECTED): "ПОСЛЕ финального `await
emit_text(...)` handler делает send_document". Не enforceable когда
SDK эмитит multiple `ResultMessage` envelopes per `bridge.ask`
iteration loop (см. `bridge/claude.py:438-447` Fix A).

**New v1 contract**:

- Bridge per-turn: при `_safe_query` встретив ToolResultBlock с
  `name == "mcp__render_doc__render_doc"` парсит content[0].text как
  JSON. Проверяет `schema_version == 1` (MED-6). Если `kind ==
  "artefact"` — bridge yield'ит `ArtefactBlock(path: Path, format: str,
  suggested_filename: str, tool_use_id: str)` СРАЗУ ПОСЛЕ оригинального
  ToolResultBlock в bridge stream.
- Handler: maintains `pending_artefacts: list[ArtefactBlock]` LOCAL к
  одному `process_user_message` invocation. Two flush points:
  1. **На каждый `ResultMessage`** (не только последний): handler
     вызывает `await self._flush_artefacts(chat_id, pending_artefacts)`,
     затем `pending_artefacts.clear()`.
  2. **На normal exit из async-for**: дополнительный flush гарантирует
     drain если SDK завершил query без эмита финального ResultMessage
     (defensive).
- Flush helper: `for art in pending_artefacts: await
  self._adapter.send_document(chat_id, art.path, caption=None,
  suggested_filename=art.suggested_filename); await
  subsystem.mark_delivered(art.path)` — с partial-failure handling из
  HIGH-3 (см. ниже).
- Order invariant: текст модели за iteration N доставляется ДО артефактов
  iteration N. Iteration N+1 видит чистый pending list. Owner-видимый
  порядок: `text₁ → doc₁ → text₂ → doc₂ → …` для multi-iteration турна.

**Option B (handler сам парсит каждый ToolResultBlock — REJECTED)**:
лезет в SDK detail и нарушает phase-2 layering. Bridge владеет SDK
message types, handler — нет.

#### HIGH-3 closure: partial-failure inline-text

При flush артефакта (любой) `send_document` бросает исключение
(network, Telegram down, FSInputFile read error) ИЛИ `path.stat()`
возвращает `>20MB`:

- Log structured event `event=render_doc_send_document_failed`,
- Emit text fallback: `await self._adapter.send_text(chat_id, f"(не
  удалось доставить {art.suggested_filename}: {short_reason})")` ДО
  перехода к следующему артефакту.
- `subsystem.mark_delivered(art.path)` всё равно вызывается (final
  failure for this path; sweeper освобождает по TTL).
- Цикл продолжает с N+1 артефакта.

`short_reason` извлекается из exception type: `network` (aiohttp /
ssl), `too-large` (size cap), `gone` (path missing), `unknown`.

**Caption v1 = None** (LOW-1 explicit doc): мodel сама пишет преамбулу
текстом ДО артефакта; mirrors phase-6e audio path (`emit_direct(text)`
precedes воз-derived attachment). Mult-artefact ambiguity owner-side
acceptable in v1 — модель в normal flow сама нумерует или подписывает.

Для не-Telegram адаптеров (потенциальный Yandex в будущем)
`send_document` обязан быть реализован; ABC заставляет это at
instantiation time. **HIGH-6 closure**: AC#17 переписан под
handler-resilience to `NotImplementedError` (см. AC list) — не требует
Yandex shipped.

### 2.6 PDF renderer — pandoc + WeasyPrint + safe_url_fetcher

**Pipeline**:
1. Записать `content_md` во временный файл
   `<artefact_dir>/.staging/<uuid>.md` (sub-directory `.staging/`
   чтобы sweeper их не удалял до завершения; cleaned at staging-step
   exit + boot-time unconditional cleanup из MED-4 closure).
2. `asyncio.create_subprocess_exec("pandoc", "-f",
   "markdown-raw_html-raw_tex-raw_attribute-tex_math_dollars-tex_math_single_backslash",
   "-t", "html5", "-o", "<staging_html>", "<staging_md>",
   env=<scoped>, ...)` с timeout `pdf_pandoc_timeout_s` (default 20s).
   Argv-form, не shell. `env=` — whitelist-only (см. §2.11 HIGH-1 closure).
   **Markdown variant choice** (CRIT-2 + Q7 + W2-MED-1):
   `markdown-raw_html-raw_tex-raw_attribute-tex_math_dollars-tex_math_single_backslash`
   subtracts:
   - `raw_html` — strips inline `<script>`, `<iframe>`, `<base>`,
     `<link>`, `<svg>`, `<object>`, `<embed>`, etc.
   - `raw_tex` — strips raw `\command{...}` literal TeX blocks
     (`\textbf{...}` etc.).
   - `raw_attribute` — strips Pandoc's `{=html}` raw-attr smuggling.
   - **W2-MED-1**: `tex_math_dollars` — disables inline math
     `$E=mc^2$`. v1 wording incorrectly implied `raw_tex` covered
     this; per Pandoc User Manual §Math, `tex_math_dollars` is a
     separate extension (enabled by default in `markdown` flavour).
     Without explicit subtraction, pandoc would produce MathML,
     which WeasyPrint then renders (visible to owner). v2 chooses
     **defang** rather than allow because Math rendering surface
     was not in spec scope; owner can still type math literally в
     ```code blocks``` если нужно. Researcher Wave A spike обязан
     verify behaviour empirically (sample input
     `content_md='energy: $E=mc^2$.'` → output PDF must show literal
     `$E=mc^2$` glyph string, not MathML rendering).
   - **W2-MED-1**: `tex_math_single_backslash` — disables alternate
     LaTeX delimiter `\(...\)`. Mirrors `tex_math_dollars` decision
     for consistency.

   Researcher pass обязан **эмпирически verify** в Wave A spike, что
   pandoc strips эти конструкции (sample injections: `<iframe
   src="file:///etc/passwd">`, `<base href="file:///etc/">`,
   `<svg><image href="file://..."/>`, inline math `$x^2$`).

3. Read HTML, передаём в `weasyprint.HTML(string=html,
   base_url="<staging_dir>", url_fetcher=safe_url_fetcher).
   write_pdf("<final_path>")`. Запуск через `asyncio.to_thread` чтобы
   не блочить event loop. **NOTE — `asyncio.to_thread`
   uncancellable**: см. §2.12 (iii) honesty paragraph.

4. **CRIT-2 closure — `safe_url_fetcher`** (W2-MED-2 tightened):

   ```python
   from weasyprint.urls import URLFetchingError

   def safe_url_fetcher(url: str, timeout: int = 5):
       """WeasyPrint URL fetcher: deny EVERYTHING (including data:).
       Mirrors WeasyPrint's default fetcher signature so it slots in
       via url_fetcher= kwarg.

       v1 wording allowed data:image/* URIs; v2 W2-MED-2 closure drops
       this allowlist entirely. §5 #3 lists embedded images of any
       kind (external + inline data:) as out-of-scope. Inline image
       embedding is image embedding regardless of transport. If owner
       eventually wants charts in PDFs — phase 10 reopens with
       deliberate scheme allowlist.
       """
       raise URLFetchingError(
           f"render_doc: all url-fetches blocked (got url={url[:64]!r})"
       )
   ```

   This blocks every fetch surface enumerated in CRIT-2 wave 1
   findings (≥9 surfaces) **plus** `data:` URIs (W2-MED-2):
   1. `<img src="...">` (raster) — denied.
   2. CSS `background: url(...)` / `background-image:` /
      `list-style-image:` / `border-image:` / `cursor:` /
      `content: url(...)` — denied.
   3. `<link rel="stylesheet" href="...">` — moot (raw_html stripped
      by pandoc); defence-in-depth.
   4. `<base href="file:///etc/">` — moot + denied.
   5. `@font-face { src: url("file://...") }` — denied.
   6. `<svg>` xlink/href — moot + denied.
   7. `<object data="...">` / `<embed src="...">` — moot + denied.
   8. `data:` of ALL mime types — denied (W2-MED-2; was partially
      allowed for `image/png|jpeg|gif|svg+xml` в v1; inconsistency
      с §5 #3 «embedded images non-goal» resolved by tightening, not
      by relaxing §5).
   9. CSS `@import url()` + custom property `--bg: url(...)` — denied.

5. WeasyPrint settings: `presentational_hints=False` (no inline
   styles), `optimize_images=True`. Custom CSS — НЕТ в v1.

6. Output PDF size cap: `pdf_max_bytes` (default **20 MiB**, LOW-4
   closure: symmetric with Telegram cap; v1 validator enforces
   `pdf_max_bytes <= TELEGRAM_DOC_MAX_BYTES`) — checked post-render,
   `path.stat().st_size > cap` → unlink + return
   `render_failed_output_cap` (MED-3).

7. На каждом шаге исключения ловим, audit-row пишем
   `result="failed"` + `error="<short kebab-case>"` (MED-3). Никаких
   stack traces в model output.

8. **Always cleanup `.staging/<uuid>.{md,html}` in `finally`**
   (CRIT-4: даже на CancelledError).

### 2.7 DOCX renderer — pandoc native

**Pipeline**:
1. Staging md как §2.6.
2. `pandoc -f markdown-raw_html-raw_tex-raw_attribute -t docx -o
   <final_path> <staging_md>` через `asyncio.create_subprocess_exec` с
   timeout `docx_pandoc_timeout_s` (default 15s). Same markdown variant
   as PDF (CRIT-2 consistency — pandoc parses input the same way; raw
   HTML in input still produces unwanted side-effects in docx output).
3. Output cap: `docx_max_bytes` (default 10 MiB).

DOCX-specific: pandoc по умолчанию embed'ит markdown-tables как Word
tables — owner ожидаемо это получит. Image references (`![](...)`) в
v1 НЕ поддерживаются — markdown с image-syntax всё равно прорендерится
(pandoc broken-image placeholder), но base_url для resolving не
выставляется → image будет broken. Это явный non-goal для phase 9 (см.
§Явно НЕ).

**Always cleanup `.staging/<uuid>.md` in `finally`** (CRIT-4).

### 2.8 XLSX renderer — openpyxl write_only over markdown pipe-tables

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
2. **v1 ограничение**: `len(tables) != 1` → return
   `render_failed_input_syntax` с error `"markdown-no-tables"` (если
   `len==0`) или `"markdown-multi-table"` (если `len>1`). Multi-sheet
   xlsx — §Явно НЕ.
3. **HIGH-4 closure** — `openpyxl.Workbook(write_only=True)` MODE:
   streaming rows, ~10× lower peak RSS than default mode. Header row
   written via `WriteOnlyCell(value=h, style=header_style)` где
   `header_style.font = Font(bold=True)`. Column widths auto-fit
   skipped в write_only mode (openpyxl ограничение); owner может
   расширить columns в Excel.
4. Per-sheet caps (HIGH-4 update): `xlsx_max_rows` reduced from 10000
   → **5000** в v1; `xlsx_max_cols` (default 50). Превышение →
   `render_failed_input_syntax` с error `"openpyxl-too-many-rows"` /
   `"openpyxl-too-many-cols"`.
5. `wb.save(<final_path>)`. Запуск через `asyncio.to_thread`.

XLSX renderer НЕ зависит от pandoc — openpyxl чистый python wheel,
уже в pyproject.toml (phase 6a inbound parsing). Никаких новых
deps для xlsx-only. xlsx работает даже когда pandoc отсутствует
(HIGH-5 partial force-disable).

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
    artefact_ttl_s: int = 600         # 10 min — TTL after delivery (§2.13)
    sweep_interval_s: int = 60
    cleanup_threshold_s: int = 86400  # boot-time stale cleanup (final files)
    max_input_bytes: int = 1_048_576  # 1 MiB markdown
    tool_timeout_s: int = 60
    render_max_concurrent: int = 2
    audit_log_max_size_mb: int = 10
    audit_log_keep_last_n: int = 5    # Wave D D1 date-stamped rotation
    pdf_pandoc_timeout_s: int = 20
    pdf_weasyprint_timeout_s: int = 30
    pdf_max_bytes: int = 20 * 1024 * 1024   # LOW-4: == Telegram cap
    docx_pandoc_timeout_s: int = 15
    docx_max_bytes: int = 10 * 1024 * 1024
    xlsx_max_rows: int = 5000               # HIGH-4: down from 10000
    xlsx_max_cols: int = 50
    xlsx_max_bytes: int = 10 * 1024 * 1024
    render_drain_timeout_s: float = 20.0    # W2-HIGH-1: down 50s → 20s
                                            # to fit cumulative 80s total.
                                            # pandoc SIGTERM-at-5s,
                                            # SIGKILL-at-10s; WeasyPrint
                                            # thread best-effort drain,
                                            # force-orphan after 20s.
    pandoc_sigterm_grace_s: float = 5.0     # W2-HIGH-1: cancel→SIGTERM grace
    pandoc_sigkill_grace_s: float = 5.0     # W2-HIGH-1: SIGTERM→SIGKILL grace
    audit_field_truncate_chars: int = 256   # W1-MED-1 + W2-MED-4

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
        # LOW-4: PDF cap must not exceed Telegram cap (else owner gets
        # render-OK but undeliverable silently until TTL).
        if self.pdf_max_bytes > 20 * 1024 * 1024:
            raise ValueError(
                "pdf_max_bytes must be <= 20 MiB (Telegram send_document "
                "cap)"
            )
        if self.docx_max_bytes > 20 * 1024 * 1024:
            raise ValueError("docx_max_bytes must be <= 20 MiB")
        if self.xlsx_max_bytes > 20 * 1024 * 1024:
            raise ValueError("xlsx_max_bytes must be <= 20 MiB")
        # W2-MED-3: render_drain must accommodate worst-case PDF
        # pipeline sum (pandoc + WeasyPrint). Owner setting drain
        # below pipeline sum cancels mid-render → CRIT-4 mitigation
        # defeated.
        if self.render_drain_timeout_s < (
            self.pdf_pandoc_timeout_s + self.pdf_weasyprint_timeout_s
        ):
            raise ValueError(
                "render_drain_timeout_s must >= pdf_pandoc_timeout_s + "
                "pdf_weasyprint_timeout_s; otherwise Daemon.stop will "
                "cancel mid-render and leak orphan pandoc/cairo state. "
                "v2 default (20s) deliberately drops below this bound "
                "to fit cumulative 80s budget — owner must explicitly "
                "raise pdf_*_timeout_s sums to <= 20s OR raise drain "
                "(and accept stop_grace_period overrun)."
            )
        # W2-HIGH-1 honesty: pandoc SIGTERM grace + SIGKILL grace must
        # fit inside drain budget — otherwise SIGKILL cleanup happens
        # after Daemon hands control to _bg_tasks cancel.
        if self.pandoc_sigterm_grace_s + self.pandoc_sigkill_grace_s > self.render_drain_timeout_s:
            raise ValueError(
                "pandoc_sigterm_grace_s + pandoc_sigkill_grace_s must "
                "fit inside render_drain_timeout_s (else SIGKILL "
                "cleanup races _bg_tasks cancel)"
            )
        # HIGH-4 advisory: combined worst-case peak RSS warning.
        # Empirical: PDF=400 MB, XLSX=300 MB per slot; 200 MB daemon
        # baseline. Total = 200 + render_max_concurrent * 400 MB.
        # Warn (not error) — owner may have larger VPS.
        worst_case_mb = 200 + self.render_max_concurrent * 400
        if worst_case_mb > 1024:  # 1 GB VPS baseline
            log.warning(
                "render_doc_rss_budget_warning",
                worst_case_mb=worst_case_mb,
                concurrent=self.render_max_concurrent,
            )
        return self
```

> **W2-MED-3 NOTE on drain default**: с default `pdf_pandoc_timeout_s=20s` +
> `pdf_weasyprint_timeout_s=30s` = 50s, validator REJECTS default
> `render_drain_timeout_s=20s`. v2 deliberately drops the default
> drain below this bound, with documented trade-off в §2.12 (iii):
> «WeasyPrint thread cannot be cancelled mid-flight; we accept that
> rare PDF render in flight at Daemon.stop will continue running на
> orphan thread until process exit kills it». Validator therefore
> ALSO accepts `render_drain_timeout_s == 0` (explicit "no drain";
> pandoc gets SIGTERM immediately on stop), but otherwise requires
> the >= sum invariant. Implementation:
>
> ```python
> if self.render_drain_timeout_s != 0 and self.render_drain_timeout_s < (
>     self.pdf_pandoc_timeout_s + self.pdf_weasyprint_timeout_s
> ):
>     # Allow explicit-zero opt-out (force-orphan WeasyPrint thread on
>     # stop); reject any non-zero below worst-case.
>     raise ValueError(...)
> ```

Default `enabled: True` обоснован: owner явно попросил эту фичу,
default-off сделает фичу невидимой пока кто-то не флипнет env-флаг.
Альтернатива (default `False` как у `vault_sync`) — см. §Развилки Q3
→ DECIDED below.

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
self._render_doc.force_disabled` (subsystem-wide; per-format отказ
обрабатывает @tool body itself). Picker / audio bridges получают
default `False` — никогда не видят @tool, как `vault_push_now`.

### 2.11 Subprocess env scoping (HIGH-1 + phase-8 H3 carry)

Все pandoc invocations используют `env=` параметр на
`asyncio.create_subprocess_exec`. **НИКОГДА** не мутировать
`os.environ` daemon-wide. Это invariant из phase 8 (§2.3 H3 closure).

**HIGH-1 closure — explicit env whitelist (strictly tighter чем
phase-8)**: pandoc subprocess env =

```python
def _pandoc_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
```

Whitelist-only — никаких `TELEGRAM_BOT_TOKEN`, `GH_TOKEN`,
`ANTHROPIC_*`, `CLAUDE_*` путей. Это strictly tighter чем phase-8
vault_sync (которому нужно SSH infrastructure). Pandoc сам не читает
эти секреты, но lua-filters / future binary-children inherit env по
умолчанию — лишний blast radius мы не хотим.

AC#23 verifies (см. §4); test asserts subprocess env keys ⊆
`{PATH, LANG, HOME}`.

### 2.12 Render lifecycle vs Daemon.stop (CRIT-4 closure, NEW)

Phase 9 представляет три новых концепта lifecycle, отсутствовавших в
v0:

#### (i) `_render_doc_pending: set[asyncio.Task]` drain set

`Daemon` хранит `_render_doc_pending: set[asyncio.Task]` (mirror
`_vault_sync_pending` от phase-8). При входе в
`RenderDocSubsystem.render(...)`:

```python
async def render(self, content_md, fmt, filename, *, task_handle):
    if task_handle is not None:
        self._pending.add(task_handle)
        task_handle.add_done_callback(self._pending.discard)
    try:
        return await self._dispatch(content_md, fmt, filename)
    finally:
        # add_done_callback is enough; defensive remove also OK.
        self._pending.discard(task_handle)
```

`Daemon.stop()` ordering (insertion AFTER vault_sync drain, BEFORE
`_bg_tasks` cancel — see §3 C6 boot ordering for boot-time mirror):

```python
# 1. Vault sync drain (phase 8 invariant) — UNCHANGED.
# 2. Render-doc drain (NEW phase 9):
if self._render_doc and self._render_doc_pending:
    await asyncio.wait(
        self._render_doc_pending,
        timeout=self._settings.render_doc.render_drain_timeout_s,  # 20s W2-HIGH-1
        return_when=asyncio.ALL_COMPLETED,
    )
    # Tasks still pending after timeout get cancelled below.
# 3. _bg_tasks cancel (phase 5d invariant) — UNCHANGED.
```

#### W2-HIGH-1 closure: cumulative `Daemon.stop` drain budget

Devil w2 surfaced that v1's `render_drain_timeout_s=50s` exceeds
deployment-environment stop budgets. Cumulative worst-case
`Daemon.stop()` runs sequentially:

```
adapter.stop()           ~30s   (typing flush, phase 6e)
vault_sync drain         60s    (drain_timeout_s default)
render_doc drain         <X>s   (this knob)
_bg_tasks cancel         5s     (audio_persist + subagent)
conn.close + misc        <1s
─────────────────────────────
total                    96s + X
```

**Actual deployment caps**:
- `deploy/docker/docker-compose.yml`: `stop_grace_period: 35s`
  (compose stops the container after 35s, then SIGKILL).
- `deploy/systemd/0xone-assistant.service`: `TimeoutStopSec=30s`
  (systemd reaper).

**User-mandated cap** (per task brief): cumulative ≤ 80s
(TimeoutStopSec=90s − 10s safety). v1 default (96s + 50s = 146s) blew
through this. v2 reduces:
- `render_drain_timeout_s = 20.0` (down 50s → 20s; accept WeasyPrint
  thread orphan after timeout — see (iii) below).
- `pandoc_sigterm_grace_s = 5.0` (SIGTERM grace).
- `pandoc_sigkill_grace_s = 5.0` (SIGKILL fallback).

**v2 cumulative budget** = 30 + 60 + 20 + 5 + 1 = **116s** worst-case.
Still exceeds user-mandated 80s cap, BUT this is best we can fit
without dropping vault drain budget (60s is phase-8 invariant; touching
it re-opens phase-8 review surface — out of scope for w2 closure).

**Honesty paragraph** (mandatory for spec — devil w2 flagged that v1
hand-waved this):

> The compose `stop_grace_period: 35s` and systemd `TimeoutStopSec=30s`
> are LOWER than worst-case sequential drain. In practice:
> - Most stops happen idle (no vault push in flight, no render in
>   flight) — drain returns ~immediately, total ≪10s.
> - When a vault push IS in flight, vault drain dominates (60s) and
>   docker SIGKILLs at 35s. **Vault SSH session is then orphaned**
>   (phase 8 accepted this as residual risk, see phase-8 LOW-3).
> - When render IS in flight at stop time, render drain runs AFTER
>   vault drain. Compose SIGKILL hits BEFORE render drain even starts.
>   **Pandoc subprocesses orphaned**; staging files left under
>   `.staging/` — but boot-time `_cleanup_stale_artefacts` walks
>   `.staging/` UNCONDITIONAL (MED-4 closure), so next boot wipes
>   them. WeasyPrint thread орфаниtся within process death.
> - Owner can mitigate by raising `stop_grace_period: 180s` в compose
>   (and `TimeoutStopSec=180s` в systemd) — operator runbook documents
>   this for production deployment. Default 35s/30s tuned для idle
>   reload latency, accepting drain truncation.

**Spec-level position** (W2-HIGH-1 verdict): default config
DELIBERATELY undersized для drain; render-in-flight on stop is a
documented residual risk; orphan cleanup runs on next-boot. Owner
runbook + `deploy/docker/README.md` MUST mention `stop_grace_period`
override for production renders. Wave A A8 (image size CI gate) does
NOT add static check on `stop_grace_period` — Wave A's scope is render
binaries, not docker compose drain budgets; the latter belongs in
operator runbook (deploy/docker/README.md addendum, post-Wave D).

#### Tests

- `test_phase9_daemon_stop_total_budget_under_80s.py` — synthetic
  test: monkeypatch `_adapter.stop` (typing) to take 30s,
  `_vault_sync_pending` to 60s, `_render_doc_pending` to 20s; assert
  `Daemon.stop()` returns within 116s, AND assert that under default
  compose `stop_grace_period=35s` SIGKILL would have fired в midstream
  (logged as warning). Test does not enforce ≤ 35s strictly — that's
  documented residual risk.

#### (ii) Pandoc subprocess SIGTERM on cancel

При `CancelledError` в @tool body during `await proc.wait()`:

```python
async def _run_pandoc(argv, *, timeout, env, cwd, settings):
    proc = await asyncio.create_subprocess_exec(*argv, env=env, cwd=cwd, ...)
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        proc.terminate()  # SIGTERM
        try:
            await asyncio.wait_for(
                proc.wait(),
                timeout=settings.pandoc_sigterm_grace_s,  # 5.0s default
            )
        except asyncio.TimeoutError:
            proc.kill()  # SIGKILL fallback
            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=settings.pandoc_sigkill_grace_s,  # 5.0s default
                )
            except asyncio.TimeoutError:
                # Process refused to die even on SIGKILL (kernel state
                # corruption). Log + accept zombie; reaped on parent exit.
                log.error("render_doc_pandoc_kill_failed", pid=proc.pid)
        raise  # re-raise so @tool body sees cancellation
    finally:
        # Best-effort: clean .staging/<uuid>.{md,html}
        ...
```

**W2-HIGH-1 budget breakdown**: SIGTERM grace 5s + SIGKILL grace 5s
= 10s within 20s `render_drain_timeout_s` — leaves 10s headroom for
WeasyPrint thread join (which we KNOW won't actually join, see (iii)).

#### (iii) WeasyPrint thread uncancellable — honesty (W2-HIGH-1 honest)

`asyncio.to_thread(weasyprint.HTML(...).write_pdf(...))` returns a
future that **can be cancelled at the future level** but the underlying
thread **continues running** until `write_pdf` completes (Python's
`concurrent.futures.ThreadPoolExecutor` does not interrupt threads).

**v1 spec wording (now REJECTED)**: «Drain budget accommodates
worst-case 30s WeasyPrint on top of 20s pandoc = 50s drain». This
ignored that 50s drain blew through compose `stop_grace_period: 35s`
and systemd `TimeoutStopSec: 30s` already.

**v2 spec wording (W2-HIGH-1 closure)**: WeasyPrint render runs in
`asyncio.to_thread` and is NOT cancellable mid-flight. Drain budget
`render_drain_timeout_s=20s` is **DELIBERATELY UNDER** worst-case
PDF pipeline (50s = pandoc 20s + weasyprint 30s). v2 explicitly
accepts:

- If WeasyPrint thread is mid-`write_pdf` at `Daemon.stop()`,
  `asyncio.wait(_render_doc_pending, timeout=20s)` returns with
  `not_done` non-empty.
- Daemon proceeds to `_bg_tasks.cancel()`. The future is cancelled
  but the thread keeps running.
- `conn.close()` runs, daemon main coroutine returns.
- Process exit follows. The orphaned thread is killed by the OS at
  process exit (does not leave file-descriptor leaks; cairo/pango
  cleanup segfault на shutdown is theoretical and accepted).
- Staging files under `.staging/` are cleaned next boot
  (UNCONDITIONAL, MED-4).
- Final PDF file may or may not exist; if exists but never delivered,
  in-flight ledger lost on shutdown — boot-time mtime cleanup
  (cleanup_threshold_s=24h) eventually walks it.

This is a documented residual risk, NOT a bug. `Daemon.stop()` budget
тоже не fits cumulative 80s cap (see W2-HIGH-1 honesty paragraph
above) — the WeasyPrint orphan is one of two contributors to that
overrun (vault drain is the other).

#### Tests (Wave C C7 expanded)

- `test_phase9_daemon_stop_terminates_pandoc.py` — pandoc PID receives
  SIGTERM, дочерний процесс умирает в течение 5s.
- `test_phase9_daemon_stop_drains_render_pending.py` — 2 render
  in-flight на момент stop; daemon ждёт оба (или timeout) перед
  cancel `_bg_tasks`.
- `test_phase9_render_staging_cleanup_on_cancel.py` — `.staging/
  <uuid>.{md,html}` чистится в finally на CancelledError path.

### 2.13 In-flight artefact ledger (CRIT-3 closure, NEW)

Sweeper TTL-by-mtime races slow / retried `send_document` upload (см.
CRIT-3 wave 1 finding). v1 решение — option (b): in-memory live-set
ledger owned by `RenderDocSubsystem`.

**Data structure**:

```python
@dataclass
class ArtefactRecord:
    path: Path
    created_at: float       # time.monotonic() at register
    delivered_at: float | None = None
    in_flight: bool = True

class RenderDocSubsystem:
    _artefacts: dict[Path, ArtefactRecord]  # keyed by absolute path
    _artefacts_lock: asyncio.Lock
```

**Lifecycle**:

1. `subsystem.render(...)` produces final path → calls
   `subsystem.register_artefact(path)`. **W2-HIGH-2 closure**:
   `register_artefact` MUST acquire `self._artefacts_lock` AROUND
   the `dict.__setitem__` mutation step.
2. @tool body returns envelope (path included).
3. Bridge yields ArtefactBlock; handler eventually calls
   `adapter.send_document(...)` (success or failure).
4. Handler **always** calls `await subsystem.mark_delivered(path)`
   AFTER the send attempt resolves (try/finally guarantees this even
   on partial failure, HIGH-3). **W2-HIGH-2 closure**: `mark_delivered`
   MUST acquire `self._artefacts_lock` AROUND the field flip
   (`in_flight=False, delivered_at=now`).
5. `mark_delivered` sets `in_flight=False, delivered_at=time.monotonic()`.

**W2-HIGH-2 Lock acquisition discipline (NEW)**: ALL three lifecycle
operations on `self._artefacts` MUST acquire `self._artefacts_lock`:

```python
async def register_artefact(self, path: Path) -> None:
    async with self._artefacts_lock:
        self._artefacts[path] = ArtefactRecord(
            path=path,
            created_at=time.monotonic(),
            in_flight=True,
        )

async def mark_delivered(self, path: Path) -> None:
    async with self._artefacts_lock:
        rec = self._artefacts.get(path)
        if rec is None:
            return
        rec.in_flight = False
        rec.delivered_at = time.monotonic()
```

Without lock acquisition on register/mark_delivered, a concurrent
sweeper iteration can hit `RuntimeError: dictionary changed size
during iteration`. Race window is small (60s sweep interval × ~50ms
comprehension window) but non-zero — and non-zero is unacceptable for
shutdown correctness.

**Sweep algorithm** (W2-HIGH-2 reorganized — snapshot under lock,
unlink outside):

```python
async def _sweep_loop(self):
    ttl = self._settings.artefact_ttl_s
    while True:
        await asyncio.sleep(self._settings.sweep_interval_s)
        await self._sweep_iteration()

async def _sweep_iteration(self) -> None:
    """One sweep pass — extracted for testability + W2-HIGH-2 lock
    discipline."""
    now = time.monotonic()
    # PHASE 1 (under lock): snapshot delete candidates.
    async with self._artefacts_lock:
        snapshot = [
            (path, rec)
            for path, rec in self._artefacts.items()
            if not rec.in_flight
            and rec.delivered_at is not None
            and now - rec.delivered_at > ttl
        ]
    # PHASE 2 (outside lock): disk I/O. Slow unlink does NOT stall
    # delivery's mark_delivered.
    deleted_paths: list[Path] = []
    for path, rec in snapshot:
        try:
            path.unlink(missing_ok=True)
            deleted_paths.append(path)
        except OSError as e:
            log.warning(
                "render_doc_sweep_unlink_failed",
                path=str(path),
                error=repr(e),
            )
    # PHASE 3 (under lock): pop deleted paths from ledger.
    async with self._artefacts_lock:
        for path in deleted_paths:
            # Defensive re-check: someone may have re-registered the
            # same path between PHASE 1 + PHASE 3.
            rec = self._artefacts.get(path)
            if rec is not None and not rec.in_flight:
                self._artefacts.pop(path, None)
```

**Lock acquisition invariant** (mandate for coder + reviewer):
- `register_artefact`: 1 lock acquire.
- `mark_delivered`: 1 lock acquire.
- `_sweep_iteration`: 2 lock acquires (snapshot + cleanup), with
  disk I/O sandwiched outside.
- No method holds the lock during `await` other than `await asyncio.
  sleep` outside the lock (sweep loop body wraps `async with`
  appropriately).

**Reconciliation invariants**:

- Sweeper **never** deletes `in_flight=True` records, even if
  delivery is taking >1 hour (Telegram retry-after storms).
- If handler **never** calls `mark_delivered` (handler crashed), record
  stays `in_flight=True` forever — file is leaked. Mitigation:
  `Daemon.stop()` calls
  `subsystem.mark_orphans_delivered_at_shutdown()` which sets
  `delivered_at=now` on all `in_flight=True` records → next-boot's
  `_cleanup_stale_artefacts` will mtime-sweep them (24h fallback).
- Boot-time `_cleanup_stale_artefacts` is **mtime-based** (no
  in-memory state survives restart) and walks `.staging/`
  unconditionally + main dir with cleanup_threshold_s gate (MED-4).

**Memory cost**: ledger entry ~256 bytes; even with 10K artefacts in
flight (impossible in practice — concurrency=2 cap), ~2.5 MB. Negligible.

**AC#20 + AC#27** cover this (см. §4).

### 2.14 Observability events (W2-LOW-1 closure, NEW)

Devil w2 noted v1 mentions structured-log events ad-hoc throughout
spec but has no central inventory. v2 §2.14 enumerates ALL events
emitted by render_doc subsystem so reviewer can audit logs против
this list:

| Event name | Emitted by | When | Required fields |
|---|---|---|---|
| `render_doc_force_disabled` | subsystem.startup_check | on boot if subsystem fully disabled | `reason: str` (e.g. "pandoc-missing", "weasyprint-import-failed") |
| `render_doc_format_force_disabled` | subsystem.startup_check | per-format disable (HIGH-5) | `format: str`, `reason: str` |
| `render_doc_started` | subsystem.render | enter @tool body | `format: str`, `tool_use_id: str`, `bytes_in: int` |
| `render_doc_rendered` | subsystem.render | success path | `format: str`, `tool_use_id: str`, `bytes_out: int`, `duration_ms: int`, `path_basename: str` |
| `render_doc_failed` | subsystem.render | exception/failed-result path | `format: str`, `tool_use_id: str`, `reason: str`, `error: str`, `duration_ms: int` |
| `render_doc_timeout` | @tool body | tool_timeout_s exceeded | `format: str`, `tool_use_id: str`, `timeout_s: float` |
| `render_doc_envelope_unknown_schema_version` | bridge | MED-6 mismatch | `received_version: int`, `tool_use_id: str` |
| `render_doc_send_document_failed` | handler | HIGH-3 partial failure | `tool_use_id: str`, `path_basename: str`, `reason: str` (network/too-large/gone/unknown) |
| `render_doc_send_document_missing_path` | adapter.send_document | path.is_file() False | `path_basename: str` |
| `render_doc_send_document_size_cap` | adapter.send_document | LOW-4 size cap exceeded | `path_basename: str`, `size_bytes: int` |
| `render_doc_artefact_registered` | subsystem.register_artefact | post-render | `path_basename: str`, `tool_use_id: str` |
| `render_doc_artefact_delivered` | subsystem.mark_delivered | post-send | `path_basename: str` |
| `render_doc_artefact_expired` | sweep | TTL passed | `path_basename: str`, `delivered_at_ago_s: float` |
| `render_doc_sweep_unlink_failed` | sweep | OSError on unlink | `path: str`, `error: str` |
| `render_doc_startup_check_passed` | startup_check | startup_check OK | `pandoc_version: str`, `weasyprint_version: str` |
| `render_doc_startup_check_failed` | startup_check | startup_check fail | `which_failed: str` |
| `render_doc_pandoc_kill_failed` | pandoc subprocess wrapper | SIGKILL refused (W2-HIGH-1) | `pid: int` |
| `render_doc_rss_budget_warning` | settings validator | combined RSS > 1 GB | `worst_case_mb: int`, `concurrent: int` |

**RSS observer integration (W2-LOW-1 mandatory)**: phase 6e RSS
observer (already in `src/assistant/main.py` rss probe) emits periodic
gauge log lines. v2 mandates render_doc subsystem registers a callback
that adds field `render_doc_inflight=N` (= `len(self._artefacts)`)
to every RSS gauge log line. Implementation: subsystem exposes
`get_inflight_count() -> int` (one read of dict length, no lock
needed for size-only read in CPython); RSS observer reads it.
AC#29 NEW — see §4.

## 3. Задачи (Wave A → B → C → D)

> Test count budget v2: **~70 tests** (v1 был ~62; devil w2 closures
> добавили +8). Distribution: Wave A 9, Wave B 21 (+3 from B5/B6
> expansions), Wave C 16 (+2: ledger lock + RSS observer + cumulative
> drain budget), Wave D 4 (unchanged). v0 был ~31; devil w1 closures
> добавили ~31 для CRIT-1..5 invariants; w2 closures pushed to ~70.
> LOW-6 closure aligned reviewer expectations.

### Wave A — System deps + smoke + skeleton (~750 LOC, 9 тестов)

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

A2. **`pyproject.toml` deps** (LOW-5 explicit): добавить
    `weasyprint>=63,<70` в `[project] dependencies` array (PEP 621).
    `openpyxl` уже есть. Pandoc — system binary, не Python dep.
    Rebuild lockfile (`uv pip compile` или `uv sync`).
    CFFI requires cairo/pango bindings provided by Dockerfile apt list (A1).

A3. **`tests/test_phase9_render_doc_binaries.py`** — копия по образцу
    `test_phase8_ssh_binary_available.py`. Тесты:
    - `assert shutil.which("pandoc") is not None`,
    - `import weasyprint` succeeds (запуск smoke probe `WeasyPrint
      HTML("<p>x</p>").write_pdf(io.BytesIO())`).

A4. **`src/assistant/render_doc/` package skeleton** — пустые модули
    с docstrings + class signatures (no impl yet). `__init__.py`
    exports.

A5. **`src/assistant/config.py`** — `RenderDocSettings` class +
    mount на `Settings.render_doc` (default_factory). Validator из §2.9
    (включая LOW-4 cap и HIGH-4 RSS warning).

A6. **`src/assistant/render_doc/subsystem.py`** — `RenderDocSubsystem`
    skeleton: `__init__`, `startup_check` (per-format check —
    HIGH-5 — `force_disabled_formats: set[str]`), `force_disabled`
    flag, `force_disabled_formats`, `render()` placeholder raising
    `NotImplementedError`, `_sweep_loop` placeholder, `_artefacts`
    ledger placeholder (§2.13).

A7. **Tests**:
    - `test_render_doc_settings_defaults.py`,
    - `test_render_doc_settings_validator.py` (rejected configs +
      LOW-4 size cap),
    - `test_render_doc_subsystem_force_disable_on_missing_pandoc.py`
      (monkeypatch `shutil.which` → xlsx still works, HIGH-5),
    - `test_render_doc_force_disable_on_weasyprint_import_fail.py`
      (sys.modules monkeypatch → docx still works via pandoc, HIGH-5),
    - `test_render_doc_disabled_when_settings_off.py`.

A8. **NEW (MED-7 closure) — `.github/workflows/docker-image-size-check.yml`**:
    builds Docker runtime image, compares `docker images --format
    '{{.Size}}'` vs prior `:main` tag. Fails PR if delta > 120 MB.
    Reference value (measured during researcher pass) documented в PR
    description. Wave A spike (researcher) обязан зафиксировать exact
    MB delta и обновить §6 size estimates с измеренным числом.

### Wave B — Renderers + @tool + audit (~1100 LOC, 18 тестов)

B1. **`render_doc/markdown_tables.py`** — pipe-table parser. Pure
    python regex; no external dep. Reject malformed (no separator
    row, mismatched col count). Returns specific error codes:
    `markdown-no-tables`, `markdown-multi-table`, `markdown-malformed`.

B2. **`render_doc/xlsx_renderer.py`** — openpyxl `write_only=True`
    impl (HIGH-4). Tests: single-table → 1 sheet, header bold (via
    `WriteOnlyCell` + `Font(bold=True)` style), кириллица OK. Reject
    multi-table. Reject `xlsx_max_rows`/`xlsx_max_cols`.

B3. **`render_doc/docx_renderer.py`** — pandoc subprocess wrapper с
    markdown variant `markdown-raw_html-raw_tex-raw_attribute-tex_math_dollars-tex_math_single_backslash`
    (W2-MED-1). Tests с mocked `asyncio.create_subprocess_exec` + один
    integration-mark тест с реальным pandoc (`pytest.mark.requires_pandoc`,
    skipped если `shutil.which` фейлит).
    **W2-MED-5 acceptance** (NEW): `test_docx_renderer_integration.py`
    renders 3-paragraph markdown with 2-row table + Cyrillic text →
    output DOCX must:
    - (a) be a valid zip (`zipfile.is_zipfile(path)` True),
    - (b) be > 5 KB (typical DOCX overhead 4-5 KB),
    - (c) `python-docx.Document(path)` parse without exception,
    - (d) reading `doc.paragraphs[0].text` returns non-empty Cyrillic
      string.
    Test runs в `--target test` Docker stage (extends `runtime`,
    guaranteeing pandoc + cairo + DejaVu).

B4. **`render_doc/pdf_renderer.py`** — pandoc → HTML5 →
    WeasyPrint(url_fetcher=safe_url_fetcher).
    `safe_url_fetcher` reject'ит ВСЁ (W2-MED-2 closure: dropped
    `data:` allowlist entirely; CRIT-2 + §5 #3 alignment). Tests
    parametrise по 9 fetch surfaces из AC#14a–AC#14i (data: now part
    of AC#14h NEW wording).
    **W2-MED-5 acceptance** (NEW): `test_pdf_renderer_integration.py`
    (`requires_pandoc + requires_weasyprint_runtime` marks) renders
    3-paragraph markdown with 2-row table + Cyrillic text → output PDF
    must:
    - (a) start with `b"%PDF-"` magic bytes,
    - (b) end with `b"%%EOF"` trailer (allow trailing newline),
    - (c) be > 2 KB (typical PDF overhead 1.5-2 KB),
    - (d) `pikepdf.open(path)` parses without exception (NEW dev dep
      `pikepdf` for tests only — pyproject `[project.optional-dependencies] test`).
    Test runs в `--target test` Docker stage. **Asymmetric to
    phase-8 ssh-not-found anti-regression**: output-side
    correctness (real PDF byte-stream from real binaries) cannot be
    caught by 1014 mocked tests.
    **W2-MED-1 acceptance** (NEW): `test_pdf_renderer_inline_math_defanged.py`
    — render `content_md='energy: $E=mc^2$.'` → PDF text contains
    literal `$E=mc^2$` glyph string (not MathML rendering). Asserts
    pandoc variant subtraction works.

B5. **`render_doc/audit.py`** — JSONL writer + date-stamped rotation
    `<path>.<YYYYMMDD-HHMMSS>` с keep-last-N (default 5).
    **W1-MED-1 + W2-MED-4 closure** — `_truncate_str_fields(row,
    max_chars=256)` helper applied к ВСЕМ str-typed fields (filename
    + error + future fields), NOT only `error`. Caller signature:

    ```python
    def _truncate_str_fields(
        row: dict[str, Any], *, max_chars: int = 256
    ) -> dict[str, Any]:
        return {
            k: (v[:max_chars] if isinstance(v, str) else v)
            for k, v in row.items()
        }
    ```

    Test additions (W2-MED-4):
    - `test_render_doc_audit_field_truncation_uniform.py` — writes row
      with 1000-char `filename` AND 1000-char `error` → JSONL line
      ≤ 768 bytes (3× 256 char fields + JSON overhead).
    - Existing `test_render_doc_audit_single_row_size_cap.py` (W1-MED-1)
      kept; expanded to assert truncation hit on `filename` overflow.

    **NOTE (LOW-2 + Q9 owner compromise)**: применяется ТОЛЬКО к
    `render_doc/audit.py`. `vault_sync/audit.py` НЕ трогаем — phase 8
    invariants preserved. Если phase 10 надумает unify, делаем общий
    helper там.

B6. **`render_doc/_validate_paths.py`** — `_sanitize_filename`
    (CRIT-5 explicit rule + W2-LOW-3 expansion). Tests:
    - Windows-reserved (`CON`, `PRN`, `AUX`, `NUL`, `COM1..9`,
      `LPT1..9`) — case-insensitive reject,
    - bidi/ZWSP/control chars — silent strip,
    - trailing dot/space — reject,
    - `..`, `/`, `\\`, leading dot — reject,
    - emoji + cyrillic + spaces — accept,
    - length > 96 — reject,
    - empty / whitespace-only — reject,
    - **W2-LOW-3 NEW**: bare `"."` — reject `dot-prefix-or-traversal`,
    - **W2-LOW-3 NEW**: dots-only `"..."`, `"...."`, `"....."` —
      reject `dot-prefix-or-traversal`.

B7. **`tools_sdk/_render_doc_core.py` + `tools_sdk/render_doc.py`** —
    @tool wrapper. Mirror phase-8 `_vault_core.py` + `vault.py`.
    `configure_render_doc(subsystem)` / `get_configured_subsystem()` /
    `reset_render_doc_for_tests()`. Body sequence per §2.4 включая
    HIGH-5 per-format check.

B8. **Bridge wiring** — `ClaudeBridge.__init__` kwarg
    `render_doc_tool_visible` + conditional `mcp_servers["render_doc"]
    = RENDER_DOC_SERVER`. `RENDER_DOC_TOOL_NAMES =
    ("mcp__render_doc__render_doc",)`. Bridge также читает
    `ToolResultBlock.content[0].text` для render_doc tool name, парсит
    JSON, проверяет `schema_version == 1` (MED-6), yield'ит
    `ArtefactBlock` после ToolResultBlock в stream.

B9. **Tests** (v2 = 21 tests, +3 from v1):
    - `test_markdown_tables_parser.py` (10+ params),
    - `test_xlsx_renderer_basic.py`,
    - `test_xlsx_renderer_write_only_mode.py` (HIGH-4),
    - `test_xlsx_renderer_multi_table_rejected.py`,
    - `test_xlsx_renderer_too_many_rows.py`,
    - `test_docx_renderer_subprocess_mock.py`,
    - `test_docx_renderer_integration.py` (requires_pandoc + W2-MED-5
      byte-structure asserts),
    - `test_pdf_renderer_url_fetcher_blocks_full_surface.py` —
      parametrise по 9 surfaces (AC#14a–i, CRIT-2; W2-MED-2 wording
      tightened на data:),
    - `test_pdf_renderer_pandoc_strips_raw_html.py`
      (markdown variant verify),
    - `test_pdf_renderer_inline_math_defanged.py` (W2-MED-1 NEW),
    - `test_pdf_renderer_integration.py` (requires_pandoc +
      requires_weasyprint_runtime + W2-MED-5 byte-structure asserts),
    - `test_render_doc_audit_date_stamped_rotation.py` (Wave D D1),
    - `test_render_doc_audit_keep_last_n.py`,
    - `test_render_doc_audit_single_row_size_cap.py` (W1-MED-1),
    - `test_render_doc_audit_field_truncation_uniform.py` (W2-MED-4 NEW),
    - `test_render_doc_filename_sanitization.py` (CRIT-5 matrix +
      W2-LOW-3 dots-only rows),
    - `test_render_doc_tool_visibility_gated.py`,
    - `test_render_doc_tool_disabled_returns_envelope.py`,
    - `test_render_doc_tool_input_too_large.py`,
    - `test_render_doc_tool_partial_force_disable_xlsx_works.py`
      (HIGH-5),
    - `test_render_doc_tool_partial_force_disable_pdf_blocked.py`
      (HIGH-5),
    - `test_phase9_pandoc_env_minimal.py` (HIGH-1).

### Wave C — Adapter outbound + handler ledger + lifecycle (~750 LOC, 16 тестов)

C1. **`adapters/base.py::MessengerAdapter`** — добавить
    `send_document` method **as NON-ABSTRACT default-impl** that
    raises `NotImplementedError("adapter has no document-out path")`.
    **W2-CRIT-1 closure (Option A)**: spec rejects v1's
    `@abstractmethod` declaration because it would break 6 existing
    test fixtures that mock `MessengerAdapter` and only override
    `start/stop/send_text`:
    - `tests/test_phase8_edge_trigger_notify.py:28` (`_FakeAdapter`)
    - `tests/test_subagent_hooks.py:22` (`_FakeAdapter`)
    - `tests/test_scheduler_dispatcher_reads_trigger_prompt.py:27`
      (`_CapturingAdapter`)
    - `tests/test_scheduler_dispatcher_lifecycle.py:28` (`_Adapter`)
    - `tests/test_scheduler_dispatcher_empty_output_reverts.py:32`
      (`_Adapter`)
    - `tests/test_scheduler_integration_real_oauth.py:28`
      (`_CapturingAdapter`)

    Each instance subclasses `MessengerAdapter` and would fail at
    instantiation (`TypeError: Can't instantiate abstract class …`)
    if `send_document` were `@abstractmethod`. v2 spec wording:

    ```python
    class MessengerAdapter(ABC):
        @abstractmethod
        async def send_text(self, chat_id: int, text: str) -> None: ...

        # W2-CRIT-1 Option A: NON-abstract default impl. Existing
        # phase 5b/6e/8 fixtures keep instantiating without override;
        # HIGH-6 handler-resilience pattern catches the
        # NotImplementedError when a non-Telegram adapter is wired.
        async def send_document(
            self,
            chat_id: int,
            path: Path,
            *,
            caption: str | None = None,
            suggested_filename: str | None = None,
        ) -> None:
            raise NotImplementedError(
                "adapter has no document-out path"
            )
    ```

    Telegram impl `TelegramAdapter.send_document` overrides the
    default with the FSInputFile path (see C2). Future adapter authors
    are obliged by **convention** (documented in adapter docstring
    + reviewer checklist), not by `@abstractmethod` enforcement.
    HIGH-6 closure handler-resilience handles `NotImplementedError` at
    runtime if a future adapter regression strips the override
    accidentally. AC#17 + AC#17b cover this. Also see §3.6 для shared
    test fixture pattern.

C2. **`adapters/telegram.py`** — `send_document` impl с
    `FSInputFile`, size cap, missing-path log. Импорт
    `aiogram.types.FSInputFile`.

C3. **`bridge/claude.py`** — добавить ArtefactBlock yield path. Bridge
    парсит `ToolResultBlock` content[0].text как JSON ТОЛЬКО для
    `tool_use_name == "mcp__render_doc__render_doc"`; не-render_doc
    tool_results — passthrough. **MED-5**: ledger keyed by
    tool_use_id; collision logs duplicate-key warning + last-write-wins.
    **MED-6**: schema_version != 1 → log warning + skip ArtefactBlock
    yield (graceful degradation).

C4. **`handlers/message.py`** — собирать ArtefactBlock'и в local list
    `pending_artefacts`. **CRIT-1 per-iteration flush**: на каждый
    `ResultMessage` AND на normal exit из async-for, drain pending
    list. **HIGH-3 partial failure**: try/except вокруг каждого
    `send_document`, fallback `send_text("(не удалось доставить
    {filename}: {short_reason})")`, finally `mark_delivered(path)`
    в любом случае.

C5. **TTL sweeper** — `RenderDocSubsystem._sweep_loop` impl per §2.13
    (in-flight ledger, не mtime). `_spawn_bg_supervised` в
    `Daemon.start()`.

C6. **Daemon wiring** — `Daemon.__init__` создаёт `_render_doc`
    placeholder, `_render_doc_pending: set[asyncio.Task]` field
    (CRIT-4); `Daemon.start()` AFTER vault_sync wiring (so phase-8
    ordering invariant preserved) AND BEFORE scheduler boot block
    (HIGH-2 closure: force-disable notify uses `adapter.send_text`
    с `asyncio.wait_for(timeout=10s)` per phase-8 F9 precedent;
    `_bot` exists post-`__init__`, polling task не required для
    outbound):
    - `_cleanup_stale_artefacts(artefact_dir)` (MED-4: walks
      `artefact_dir/` + `artefact_dir/.staging/` (last is
      unconditional)),
    - `RenderDocSubsystem(...)`,
    - `await sub.startup_check()` (per-format force_disabled_formats),
    - if **fully** force_disabled (all 3 formats blocked): emit
      one-time Telegram notify via
      `asyncio.wait_for(adapter.send_text(...), timeout=10.0)`,
    - `_render_doc_mod.configure_render_doc(subsystem=sub)` if
      not fully force_disabled,
    - `_spawn_bg_supervised(sub.start, name="render_doc_sweeper")`
      if not fully force_disabled,
    - bridge owner construction passes `render_doc_tool_visible=...`.

    `Daemon.stop()` ordering (CRIT-4 §2.12 (i)):
    - vault_sync drain (phase 8 invariant) — UNCHANGED,
    - **NEW**: `_render_doc_pending` drain via `asyncio.wait(...,
      timeout=render_drain_timeout_s, return_when=ALL_COMPLETED)`,
    - **NEW**: `subsystem.mark_orphans_delivered_at_shutdown()` для
      записей с `in_flight=True` (§2.13),
    - `_bg_tasks` cancel — UNCHANGED.

C7. **Tests** (v2 = 16 tests, +2 from v1):
    - `test_phase9_artefact_envelope_yielded_by_bridge.py`,
    - `test_phase9_bridge_envelope_unknown_schema_version.py` (MED-6),
    - `test_phase9_bridge_artefact_flush_per_iteration.py` (CRIT-1),
    - `test_phase9_handler_send_document_after_emit.py`,
    - `test_phase9_handler_partial_send_failure.py` (HIGH-3),
    - `test_phase9_handler_send_document_not_implemented.py` (HIGH-6 +
      W2-CRIT-1: also asserts default-impl raise pattern survives),
    - `test_phase9_telegram_send_document_size_cap.py`,
    - `test_phase9_telegram_send_document_missing_path.py`,
    - `test_phase9_sweeper_skips_inflight.py` (CRIT-3),
    - `test_phase9_sweeper_collects_after_delivery.py` (CRIT-3),
    - `test_phase9_ledger_concurrent_mutations.py` (W2-HIGH-2 NEW) —
      parallel-spawn `register_artefact` ×10 + `mark_delivered` ×10 +
      one `_sweep_iteration` tick → no `RuntimeError: dictionary
      changed size during iteration`, no exceptions at all,
    - `test_phase9_cleanup_staging_unconditional.py` (MED-4),
    - `test_phase9_daemon_force_disable_continues_phase8_traffic.py`,
    - `test_phase9_daemon_stop_terminates_pandoc.py` (CRIT-4 (ii) +
      W2-HIGH-1 5s SIGTERM grace + 5s SIGKILL grace asserted),
    - `test_phase9_daemon_stop_drains_render_pending.py` (CRIT-4 (i) +
      W2-HIGH-1 20s drain budget asserted),
    - `test_phase9_daemon_stop_total_budget_under_80s.py`
      (W2-HIGH-1 NEW) — synthetic typing-30s + vault-60s + render-20s,
      asserts `Daemon.stop()` returns ≤ 116s; logs warning that
      compose stop_grace_period=35s would have SIGKILL'd mid-stream,
    - `test_phase9_rss_observer_includes_render_doc_inflight.py`
      (W2-LOW-1 NEW) — RSS observer log line includes
      `render_doc_inflight=N` field; subsystem-disabled mode emits
      `render_doc_inflight=0` или skips field entirely (decide;
      v2 mandate: skip field when subsystem absent — keeps log
      schema clean; emit `=0` when subsystem present с empty ledger).

### Wave D — render_doc-only audit rotation (~150 LOC, 4 теста)

> v0 предлагал 2 carry-overs (vault_sync audit refactor + host-key
> drift CI). Devil w1 LOW-2 + LOW-3 закрылись:
> - vault_sync/audit.py date-stamped rotation **DROPPED** —
>   phase-8 invariants preserved (preserve cross-phase blast radius).
>   Owner pre-approved compromise: render_doc/audit.py date-stamped
>   from day one; vault_sync/audit.py остаётся single-step `.1`. Если
>   phase 10 надумает unify, делаем общий helper тогда.
> - host-key drift CI **DROPPED** — moved to phase 10 backlog. Не
>   связан с render_doc.

D1. **`render_doc/audit.py` date-stamped rotation** (Wave D-only
    scope). Implementation в B5 уже включает date-stamped rotation;
    Wave D добавляет ТОЛЬКО dedicated tests + Dockerfile static check.

D-tests:
- `test_audit_date_stamped_rotation.py` (только render_doc/audit.py),
- `test_audit_keep_last_n.py`,
- `test_phase9_dockerfile_apt_packages_present.py` (build-time grep
  по Dockerfile lines as static check).

**Defer to phase 10** (намеренно НЕ включаем):
- vault_sync/audit.py date-stamped refactor (LOW-2),
- Host-key drift CI weekly job (LOW-3),
- Real-subprocess `git_ops.py` integration tests,
- Periodic `startup_check` re-run для vault_sync,
- Bootstrap script `git reset` between re-runs.

### 3.6 Test fixture migration (W2-CRIT-1 closure, NEW)

Devil w2 surfaced that v1 §2.5 declared `MessengerAdapter.
send_document` as `@abstractmethod`, which would break 6 existing
test fixtures at instantiation time on Wave C C1 land. v2 chose
**Option A** (default-impl raises NotImplementedError; see C1
detailed rationale).

**No migration needed for existing test fixtures** — Option A
preserves backward compat. Existing 6 fixtures continue
instantiating without override.

**Optional shared fixture for new render_doc tests** (recommended,
not mandatory): `tests/conftest.py` adds:

```python
@pytest.fixture
def noop_messenger_adapter() -> MessengerAdapter:
    """Shared no-op MessengerAdapter for tests that need a stub but
    don't exercise document-out path. Implements send_text + the
    new send_document override (W2-CRIT-1) as no-ops, sidestepping
    the default NotImplementedError raise.
    """
    class _NoopAdapter(MessengerAdapter):
        def __init__(self) -> None:
            self.sent_text: list[tuple[int, str]] = []
            self.sent_documents: list[tuple[int, Path, str | None]] = []

        async def start(self) -> None: ...
        async def stop(self) -> None: ...
        async def send_text(self, chat_id: int, text: str) -> None:
            self.sent_text.append((chat_id, text))
        async def send_document(  # type: ignore[override]
            self,
            chat_id: int,
            path: Path,
            *,
            caption: str | None = None,
            suggested_filename: str | None = None,
        ) -> None:
            self.sent_documents.append((chat_id, path, suggested_filename))

    return _NoopAdapter()
```

**6 existing fixtures explicitly verified to keep working** under
Option A (no edits to these files needed for Wave C C1):

| File | Class name | Override |
|---|---|---|
| `tests/test_phase8_edge_trigger_notify.py:28` | `_FakeAdapter` | start/stop/send_text only |
| `tests/test_subagent_hooks.py:22` | `_FakeAdapter` | start/stop/send_text only |
| `tests/test_scheduler_dispatcher_reads_trigger_prompt.py:27` | `_CapturingAdapter` | start/stop/send_text only |
| `tests/test_scheduler_dispatcher_lifecycle.py:28` | `_Adapter` | start/stop/send_text only |
| `tests/test_scheduler_dispatcher_empty_output_reverts.py:32` | `_Adapter` | start/stop/send_text only |
| `tests/test_scheduler_integration_real_oauth.py:28` | `_CapturingAdapter` | start/stop/send_text only |

When `Daemon.start()` lands phase 9 wire-up (C6), and a phase
5b/6e/8 test happens to hit a path that now also invokes
`adapter.send_document` (e.g. force-disable Telegram notify
overlapping with subsystem boot), the test will get
`NotImplementedError` — which the HIGH-6 handler-resilience pattern
catches. AC#17b NEW.

**AC#17b**: phase 5b/6e/8 tests stay green после Wave C C1 with no
explicit override changes to existing fixtures. Verified by running
the full pre-phase-9 test suite against post-C1 codebase.

## 4. Критерии готовности

> v0 имел AC#1..#18. v1 расширил до AC#1..#28 (новые ACs #19–#28
> mapped к devil w1 closures).

**AC#1 — happy-path PDF**. Owner: «сгенерь PDF отчёт по последним
заметкам». Модель → `memory_search` → `render_doc(format="pdf",
content_md=…)` → telegram chat показывает (a) текстовый ответ модели,
(b) сразу следом — PDF-файл с suggested_filename. Файл открывается в
любом PDF-viewer'е, кириллица читаема, structure совпадает с input md.

**AC#2 — happy-path DOCX**. Аналогично AC#1 для `format="docx"`.
Word/LibreOffice открывает, structure preserved, кириллица OK.

**AC#3 — happy-path XLSX**. `content_md` = single pipe-table. Excel
открывает, header row bold, кириллица OK, столбцов не больше cap.
Verify openpyxl `write_only=True` mode используется (peak RSS measured).

**AC#4 — `enabled=False` parity**. `RENDER_DOC_ENABLED=false` →
никакого `_sweep_loop` task spawn'а, никакого `mcp__render_doc__*`
@tool в model catalogue, никакого `<data_dir>/artefacts/` создания
(если не существовала). RSS observer не получает render_doc-related
поле. Model видит: «у меня нет инструмента для PDF, я могу только
текстом».

**AC#5 — system-binary force-disable graceful (HIGH-5 expanded)**.
Если pandoc binary отсутствует И `import weasyprint` падает →
**fully** force_disabled subsystem, daemon:
- логирует `event=render_doc_force_disabled`,
- one-time Telegram-нотифицирует владельца (через
  `asyncio.wait_for(adapter.send_text(...), timeout=10s)` —
  HIGH-2 closure),
- продолжает работать со всеми остальными phase 1..8 ACs зелёными,
- @tool скрыт целиком,
- запросы owner вроде «сделай PDF» модель должна обрабатывать как
  «не могу — субсистема отключена».

**AC#5a — partial force-disable (HIGH-5 NEW)**. pandoc отсутствует,
но `import weasyprint` работает (или наоборот). xlsx renderer работает
(не зависит от pandoc), pdf+docx возвращают
`{ok: False, reason: "disabled", error: "format-pdf-unavailable-..."}`.
@tool регистрируется. Owner может попросить xlsx и получить файл.

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

**AC#7 — filename sanitization (CRIT-5 expanded)**. Adversarial
inputs (`../etc/passwd`, `/abs/path`, `..\\..\\nt`, `<file>\\0`,
leading-dot `.hidden`, длина >96, trailing dot/space, Windows-reserved
`CON.pdf`, RTL-spoof `report\\u202Efdp.exe`, ZWSP `report\\u200Bfile`)
→ rejected with `reason="filename_invalid"` ИЛИ silent-strip (для
unicode control/format/private-use/surrogate/unassigned). Cyrillic +
spaces + emoji + unicode dashes — accepted (sanitized к
whitespace-stripped). Tests parametrise (см. CRIT-5 matrix в §2.4).

**AC#8 — input size cap**. `content_md` > `max_input_bytes` →
`reason="input_too_large"`. Никаких prompt'ion attempts (модель не
видит staged path).

**AC#9 — TTL sweeper removes old artefacts (CRIT-3 reframed)**. Plant
artefact, register в ledger, mark_delivered, advance time past TTL →
sweeper удалит. Audit log row не пишется (sweeper — silent operation).

**AC#10 — boot-time stale cleanup (MED-4 expanded)**. Plant artefact с
mtime > 24h до daemon-start → `_cleanup_stale_artefacts` его удалит.
**Plant orphan staging file** под `<artefact_dir>/.staging/<uuid>.md`
любого возраста → boot cleanup удалит UNCONDITIONAL.

**AC#11 — concurrency cap**. `render_max_concurrent=2`; 3 параллельных
вызова `render_doc` — третий ждёт пока один из первых двух завершит
(verify через mocked semaphore acquire trace). HIGH-4 expansion: 2
parallel 5K-row XLSX renders complete без OOM-killer.

**AC#12 — per-tool timeout (CRIT-4 expanded)**. PDF render с pandoc
subprocess hang'нувшимся (test mock returns never-resolving) →
`tool_timeout_s` истекает, audit `result="failed"` с
`error="tool-timeout-exceeded"`, **subprocess receives SIGTERM**
(CRIT-4 (ii)) и killed cleanly (no zombies).

**AC#13 — Telegram size cap (LOW-4 reworded)**. Render produces
artefact > `TELEGRAM_DOC_MAX_BYTES` (which is now == max format cap
по validator'у LOW-4) → adapter видит `path.stat().st_size >
TELEGRAM_DOC_MAX_BYTES` → text fallback «файл слишком большой для
Telegram»; sweeper удалит файл по TTL. v1 validator делает этот path
теоретическим (pdf_max_bytes ≤ 20 MiB).

**AC#14 — markdown injection blocked at fetch layer (CRIT-2 anchor)**.
content_md с adversarial fetch attempts → `safe_url_fetcher` reject'ит
**ВСЕ** не-`data:` схемы; `data:` URI с не-image MIME — reject.
WeasyPrint fall-through на пустой content. В финальном PDF ничего не
leak'нуто. Audit row пишется с пометкой что fetch_url был заблокирован
(для post-mortem). AC#14 разбит на 9 sub-cases:

- **AC#14a** — `<img src="file:///etc/passwd">` blocked.
- **AC#14b** — CSS `@import url("file:///etc/shadow")` blocked.
- **AC#14c** — CSS `background: url(...)` blocked (parametrise + 5
  other CSS url-properties).
- **AC#14d** — `<base href="file:///etc/">` stripped by pandoc OR
  blocked by fetcher (defence-in-depth — both invariants must hold;
  test simulates `+raw_html` extension being accidentally enabled).
- **AC#14e** — `@font-face { src: url(...) }` blocked.
- **AC#14f** — `<svg>` xlink/href blocked (after pandoc strips raw
  HTML; defence-in-depth).
- **AC#14g** — `<object data="file://...">` / `<embed>` blocked (defence-
  in-depth).
- **AC#14h** — `data:text/html,<script>` blocked (mime allowlist).
- **AC#14i** — CSS `--bg-image: url(...)` custom-property blocked.

**AC#15 — system-binary smoke test green в CI**. Test
`test_phase9_render_doc_binaries.py` зелёный в test container.
Удаление `pandoc` из Dockerfile → CI идёт red БЕЗ deploy.

**AC#15a — image size delta CI gate (MED-7 NEW)**. CI step
`docker-image-size-check.yml` builds runtime image, comparing с
`:main`. Delta > 120 MB → CI red, PR blocked. Reference value
documented after Wave A spike.

**AC#16 — @tool gated by `render_doc_tool_visible`**. Picker / audio
bridges не видят `mcp__render_doc__render_doc` в allowed_tools.
Owner-bridge видит только если
`settings.render_doc.enabled and not force_disabled`
(per-format gating — внутри @tool body, не на bridge level).

**AC#17 — handler resilience to NotImplementedError (HIGH-6
rewritten)**. Test monkeypatches `TelegramAdapter.send_document` to
raise `NotImplementedError`; handler logs structured event +
`adapter.send_text(chat_id, '(не могу прислать файл — отправь по
запросу)')` instead of crashing. Также `mark_delivered(path)` всё
равно вызывается в finally. Не требует Yandex shipped — тестируется
monkeypatch'ом TelegramAdapter.

**AC#18 — audit log rotation date-stamped в render_doc only (Wave D
LOW-2 reworded)**. После 5+ ротаций под нагрузкой сохранены 5
последних `<path>.<YYYYMMDD-HHMMSS>` файлов; шестая ротация удаляет
самый старый. `vault_sync/audit.py` НЕ затронут — phase-8 invariants
preserved.

**AC#19 — multi-iteration flush (CRIT-1 NEW)**. Model turn эмитит
`render_doc(pdf) → text → render_doc(docx) → text` через 2 iteration
boundaries (2 ResultMessage envelopes). Owner-видимая
последовательность: `text₁ → pdf → text₂ → docx`. Test asserts
ordering invariant via mocked adapter capture.

**AC#20 — sweeper skips in-flight (CRIT-3 NEW)**. Render produces
2 artefacts; first send_document receives Telegram 429 retry-after
60s; second artefact is in `pending_artefacts` waiting. TTL sweeper
ticks (artefact_ttl_s expired by mtime) but does NOT delete — both
records have `in_flight=True`. Both eventually deliver.

**AC#21 — Daemon.stop terminates pandoc (CRIT-4 (ii) NEW)**. PDF
render in-flight (`asyncio.create_subprocess_exec` mocked to hang).
`Daemon.stop()` → @tool body receives CancelledError → pandoc
subprocess receives SIGTERM, dies in <5s. Staging files cleaned. No
orphan PID survives.

**AC#22 — Filename adversarial expanded (CRIT-5 NEW)**. RTL-spoof
`report\\u202Efdp.exe` → ZWSP-stripped to `reportfdp.exe`.
Windows-reserved `CON.pdf` → reject. ZWSP `a\\u200Bb` → silent strip
to `ab`. Trailing dot `report .` → reject.

**AC#23 — pandoc env minimal (HIGH-1 NEW)**. Pandoc subprocess env
keys ⊆ `{PATH, LANG, HOME}`. Никаких `TELEGRAM_BOT_TOKEN`, `GH_TOKEN`,
`ANTHROPIC_*`, `CLAUDE_*` в env. Test asserts via mocked
`asyncio.create_subprocess_exec` capturing env kwarg.

**AC#24 — boot-time notify before polling start (HIGH-2 NEW)**. Force-
disable notify lands in Telegram даже если adapter polling task ещё
не запустился (boot-time send via `_bot` direct call;
`asyncio.wait_for(timeout=10s)` обёрнут per phase-8 F9 precedent).

**AC#25 — partial-failure inline-text (HIGH-3 NEW)**. 2 artefacts; 
first send_document fails (network); owner sees `text + "(не удалось
доставить doc1.pdf: network)" + doc2.docx`. Не молчаливый skip.

**AC#26 — render_failed reason granularity (MED-3 NEW)**. Pandoc
parse error → `render_failed_input_syntax` + `error="pandoc-exit-1"`.
Post-render PDF > cap → `render_failed_output_cap` +
`error="pdf-too-large"`. openpyxl too-many-rows →
`render_failed_input_syntax` + `error="openpyxl-too-many-rows"`. Model
может branch: `_input_syntax` → retry simpler markdown.

**AC#27 — tool_use_id ledger (MED-5 NEW)**. Two render_doc calls in
single iteration with **different** tool_use_ids → 2 artefacts in
ledger, both yielded as ArtefactBlocks. Same tool_use_id collision
(SDK contract violation) → log warning + last-write-wins (defensive).

**AC#28 — envelope schema_version (MED-6 NEW)**. Test mocks @tool
returning envelope with `schema_version=2` (future). Bridge logs
`event=render_doc_envelope_unknown_schema_version` warning + skips
ArtefactBlock yield. Model still gets text result, owner sees text
without file (no crash).

**AC#14j — inline math defanged (W2-MED-1 NEW)**. content_md =
`"energy: $E=mc^2$."` → output PDF text contains literal
`$E=mc^2$` glyph string. NOT MathML rendering. Verifies
`tex_math_dollars-tex_math_single_backslash` variant subtraction
took effect.

**AC#17b — phase 5b/6e/8 fixture compatibility (W2-CRIT-1 NEW)**. After
Wave C C1 lands `MessengerAdapter.send_document` (as default-impl
raise pattern, NOT @abstractmethod), the 6 existing test fixtures
listed in §3.6 keep instantiating without code edit. Full pre-phase-9
test suite stays green; only new render_doc-specific tests gate on
`send_document` behaviour. Verified by running tests/test_phase8_*.py +
tests/test_subagent_*.py + tests/test_scheduler_*.py против post-C1
codebase.

**AC#21a — cumulative `Daemon.stop` budget (W2-HIGH-1 NEW)**. Synthetic
test:
- `_adapter.stop` mock takes 30s (typing flush),
- `_vault_sync_pending` task takes 60s,
- `_render_doc_pending` task takes 20s,
- assert `Daemon.stop()` returns within 116s (3 + 60 + 20 + 5 + 1
  s + headroom),
- assert `event=render_doc_stop_budget_exceeded` warning logged when
  drain returns truncated (timeout hit),
- assert that under default compose `stop_grace_period: 35s`, this
  budget WOULD HAVE been SIGKILL'd mid-stream (logged as warning;
  NOT a strict fail of the test — documented residual risk per
  §2.12 honesty paragraph).
**Static check NOT required for compose `stop_grace_period`** —
operator runbook mentions override.

**AC#29 — RSS observer integration (W2-LOW-1 NEW)**. Phase 6e RSS
observer log line emitted while subsystem is enabled includes field
`render_doc_inflight=N` (= `len(_artefacts)`). Test:
- subsystem disabled: RSS log lines do NOT contain field (clean schema),
- subsystem enabled, empty ledger: `render_doc_inflight=0`,
- subsystem enabled, 2 in-flight artefacts: `render_doc_inflight=2`.

## 5. Явно НЕ в phase 9

1. **Templates / branding / header-footer**. Голый markdown rendering.
   Custom CSS, brand-colors, owner letterhead — phase 10+.
2. **Multi-sheet xlsx**. Single markdown table → single sheet only.
   Несколько таблиц в одном `content_md` → reject.
3. **Embedded images of any kind, including `data:` URIs (W2-MED-2)**.
   External image references (`https://...`, `file://...`, `<img
   src=>`) — out of scope, blocked by `safe_url_fetcher` (CRIT-2
   §2.6). **Inline `data:image/*` URIs are ALSO blocked** — v1
   wording allowed image-MIME data: URIs but devil w2 surfaced this
   contradicts §5 #3 «non-goal»: image embedding is image embedding
   regardless of transport. v2 closes the gap by tightening
   `safe_url_fetcher` (denies all schemes including `data:`), not by
   weakening §5. Markdown `![](path)` syntax still parses — pandoc
   emits broken-image placeholder which renders as such in PDF/DOCX.
   Image embedding requires path-policy + scheme allowlist + size cap
   propagation — phase 10+.
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
    тот же паттерн для render_doc; см. Q6 → DECIDED ниже).
13. **CSV / TSV format**. `format` enum закрыт.
14. **Russian locale в WeasyPrint**. Default Latin-1 fallback OK для
    Cyrillic через DejaVu; spec'ом не гарантируем locale-specific
    typography.
15. **vault_sync/audit.py date-stamped rotation refactor** (LOW-2 →
    phase 10).
16. **Host-key drift weekly CI cron** (LOW-3 → phase 10).
17. **Embedded image fetch URL allowlist** (`https://` whitelist для
    future image embedding). v1 fetcher только пустит `data:` image-MIME.
18. **Caption synthesis from markdown headings**. v1 caption=None
    (LOW-1 → owner OK; модель сама пишет преамбулу).
19. **RTL bidi text direction (Hebrew, Arabic, Persian)** (W2-LOW-4).
    DejaVu Sans bundle covers Latin + Cyrillic + Greek + Hebrew +
    Arabic glyphs, BUT RTL paragraph direction requires CSS
    `direction: rtl` / `unicode-bidi: bidi-override` which
    markdown→pandoc→html5 pipeline does not emit. If owner pastes
    Arabic / Hebrew text, glyphs render correctly but visual layout
    is left-to-right (broken UX). v2 explicitly out-of-scope; phase
    11+ adds CSS direction injection if owner uses RTL languages.
    No code or test change in v2.

## 6. Зависимости

- **Phase 8 (КРИТИЧНО):**
  - Pattern для opt-in subsystem (`vault_sync` package shape).
  - Audit JSONL writer + rotation policy (но render_doc/audit.py
    использует date-stamped rotation, vault_sync/audit.py
    остаётся single-step `.1` — LOW-2 + Q9 решение).
  - `_spawn_bg_supervised` invariant + `force_disabled` graceful
    degradation pattern.
  - Boot-cleanup pattern (`_cleanup_stale_vault_locks` →
    `_cleanup_stale_artefacts` + `.staging/` unconditional walk).
  - Conditional bridge wiring kwarg (`vault_tool_visible` →
    `render_doc_tool_visible`).
  - Per-subprocess `env=` scoping invariant (H3 closure carry +
    HIGH-1 tighter whitelist).
  - `_pending` drain set pattern (`_vault_sync_pending` →
    `_render_doc_pending` per CRIT-4).
  - Boot-time notify wrapper (F9 closure: `asyncio.wait_for(timeout=
    10s)` per HIGH-2).
- **Phase 6a:** `openpyxl>=3.1.5` уже в deps; XLSX renderer
  использует тот же wheel в `write_only=True` mode.
- **Phase 4:** Memory tools — модель формирует `content_md` из
  `memory_search` results (не зависимость кода phase 9, но E2E
  scenario AC#1 это требует).
- **Phase 2:** Bridge layered architecture, hooks system, system
  prompt assembly — phase 9 не модифицирует bridge core, только
  добавляет mcp_server registration + ArtefactBlock yield path.
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
  Total estimated image size delta ≤ 95 MB (apt-cache contribution
  + dpkg metadata typically 1.5–2× → ≤ 150 MB layer). MED-7 closure:
  Wave A A8 spike обязан зафиксировать **measured** delta and update
  это число; CI gate threshold 120 MB.
- **Performance — pandoc / WeasyPrint cold-start (W2-LOW-2)**:
  - `pandoc` 2.17.x на Debian bookworm: cold-start latency
    **~150-300ms** (Haskell runtime init); warm-call ~50ms. v2 PDF
    pipeline = 2 pandoc invocations (md→html5; could be 1 if pandoc
    can write PDF directly via `-t pdf` — researcher Wave A spike
    decides). Two cold pandocs per render = **300-600ms** baseline
    before any actual content rendering.
  - `WeasyPrint` 63.x на Python 3.12: cffi binding load + cairo init
    **~200ms** на first call, cached ~50ms на subsequent (process-
    lifetime cache).
  - Cumulative first-render-of-session overhead: **500-800ms**;
    subsequent renders: **~150ms** overhead. For a 1-page доc, owner
    sees 2-3s end-to-end; 1.5s is binary cold-start. Acceptable v1.
  - Researcher Wave A spike measures p50/p99 на real bookworm
    container with target hardware (1 GB VPS); document в operator
    runbook. If first-render >5s consistently observed, mitigation
    options: pandoc-server (long-running stub) or pandoc warmup
    invocation на Daemon.start() — both phase 10.
- **Внутренние deps (карнирование):**
  - `Daemon._bg_tasks` / `_spawn_bg_supervised` — reused для sweeper
    loop.
  - `Daemon._render_doc_pending: set[asyncio.Task]` — NEW (CRIT-4).
  - `MessengerAdapter` ABC — extension в `adapters/base.py`.
  - `ClaudeBridge` — extension с `render_doc_tool_visible` kwarg +
    `ArtefactBlock` yield.
  - `ClaudeHandler` — extension с per-iteration artefact ledger
    + partial-failure handling.

## 7. Риск

| Severity | Risk | Mitigation |
|---|---|---|
| 🔴 CRITICAL | **Markdown injection через `content_md`** — модель может (ошибочно или adversarially) вставить fetch-yielding HTML/CSS → WeasyPrint попытается зачитать локальные файлы (≥9 surfaces enumerated в CRIT-2) | Pandoc invocation использует `markdown-raw_html-raw_tex-raw_attribute` (strips inline HTML/SVG/raw-attrs); WeasyPrint вызывается с custom `safe_url_fetcher` который reject'ит ВСЁ кроме `data:` URI с image-MIME allowlist. Defence-in-depth: оба должны держаться. Researcher pass верифицирует pandoc эмпирически в Wave A spike. AC#14a–AC#14i. |
| 🔴 CRITICAL | **TTL sweeper races send_document mid-upload** (CRIT-3) | In-flight ledger `RenderDocSubsystem._artefacts: dict[Path, ArtefactRecord]` с `in_flight: bool` + `delivered_at`. Handler вызывает `mark_delivered(path)` AFTER каждого send_document attempt (success или failure). Sweeper НЕ удаляет `in_flight=True` records. Boot-time fallback по mtime для orphans. AC#20. |
| 🔴 CRITICAL | **Daemon.stop leaves orphan pandoc PID + staging files** (CRIT-4) | `_render_doc_pending` drain set; Daemon.stop ждёт `render_drain_timeout_s=50s` ALL_COMPLETED ДО `_bg_tasks` cancel. На CancelledError @tool body: `proc.terminate()` → `proc.kill()` fallback. WeasyPrint thread uncancellable — drain budget fits worst-case. AC#21. |
| 🔴 CRITICAL | **Filename security: Windows-reserved + RTL spoof + ZWSP** (CRIT-5) | `_sanitize_filename` использует `unicodedata.category()` для Cc/Cf/Co/Cs/Cn strip; reject Windows-reserved basenames (CON/PRN/...); reject trailing dot/space; length cap 96 codepoints. Emoji + Cyrillic explicit accept. AC#22. |
| 🔴 CRITICAL | **Image size bloat от pandoc + cairo/pango** | Hard budget: image size delta ≤ 120 MB CI gate (MED-7 closure A8). PR template содержит «before/after image size MB». Если превышает — обсуждаем drop fonts-dejavu-core (заменить на font-fallback CSS). AC#15a. |
| 🟠 HIGH | **Bridge artefact flush ordering across multi-iteration turns** (CRIT-1) | Per-iteration flush barrier: handler drain'ит `pending_artefacts` на каждый ResultMessage. Test `test_phase9_bridge_artefact_flush_per_iteration.py` verifies. AC#19. |
| 🟠 HIGH | **Pandoc subprocess env leak** между tools (HIGH-1 + phase-8 H3 carry) | `env=` параметр на каждый `asyncio.create_subprocess_exec` whitelist-only `{PATH, LANG, HOME}`; daemon never `os.environ.update`. Test `test_phase9_pandoc_env_minimal.py`. AC#23. |
| 🟠 HIGH | **Force-disable notify ordering during boot** (HIGH-2) | `adapter.send_text(...)` direct call в `Daemon.start()` AFTER vault_sync block AND BEFORE scheduler block; обёрнут `asyncio.wait_for(timeout=10s)` per phase-8 F9. `_bot` exists post-`__init__`, polling task не required. AC#24. |
| 🟠 HIGH | **Partial send_document failure silently skipped** (HIGH-3) | На каждый send_document fail: `adapter.send_text("(не удалось доставить {filename}: {short_reason})")` ДО следующего артефакта; `mark_delivered(path)` всё равно вызывается. AC#25. |
| 🟠 HIGH | **XLSX peak RSS на 10K rows OOM-killer на 1 GB VPS** (HIGH-4) | `openpyxl.Workbook(write_only=True)` streaming mode (~10× lower RSS). Cap reduced 10000 → 5000 rows. Settings validator emits warning if combined worst-case > 1 GB. AC#11 expansion. |
| 🟠 HIGH | **Per-format force-disable: xlsx должен работать без pandoc** (HIGH-5) | `force_disabled_formats: set[str]` populated by startup_check (pandoc missing → {pdf, docx}; weasyprint fail → {pdf}). xlsx-only mode supported. @tool body checks per-format. AC#5a. |
| 🟠 HIGH | **AC#17 Yandex stub untestable as written** (HIGH-6) | AC#17 переписан на handler-resilience to `NotImplementedError` через monkeypatch TelegramAdapter. Не требует Yandex shipped. |
| 🟠 HIGH | **WeasyPrint OOM на огромных DOCs** | `max_input_bytes=1MiB` cap пред-render. `pdf_max_bytes=20MiB` (LOW-4) cap пост-render. RSS observer phase 6e — добавить `render_doc_inflight=N` field. |
| 🟠 HIGH | **Disk fill от artefacts/** | TTL sweeper bg-loop (default 600s TTL after delivery, sweep каждые 60s) + in-flight guard (CRIT-3). Boot-time `_cleanup_stale_artefacts` removes >24h files + .staging/ unconditional. Per-render concurrency cap `render_max_concurrent=2`. |
| 🟡 MEDIUM | **Audit single-row infinite-rotation на огромных error fields** (MED-1) | `error` field truncated to 512 chars в audit row; full error остаётся в structured-log only. AC#26. |
| 🟡 MEDIUM | **`format_invalid` reason dead code** (MED-2) | Dropped from §2.3 enum. SDK enum-валидация input schema reject'ит unknown format ДО @tool body. |
| 🟡 MEDIUM | **`render_failed` слишком coarse для model retry** (MED-3) | Split в `render_failed_input_syntax` / `render_failed_output_cap` / `render_failed_internal` + machine-parseable kebab-case `error` field. Model branches retry. AC#26. |
| 🟡 MEDIUM | **Boot cleanup забывает .staging/** (MED-4) | `_cleanup_stale_artefacts` walks **обе** под-директории; staging files removed UNCONDITIONAL. AC#10 expansion. |
| 🟡 MEDIUM | **tool_use_id uniqueness assumption** (MED-5) | Spec call-out: tool_use_id unique within SDK conversation per Anthropic API; ledger keyed by tool_use_id; collision logs duplicate-key + last-write-wins. AC#27. |
| 🟡 MEDIUM | **Envelope schema drift breaks bridge silently** (MED-6) | `schema_version: int = 1` field; bridge asserts == 1, logs warning + graceful skip on mismatch. AC#28. |
| 🟡 MEDIUM | **Image size budget verification mechanism not concrete** (MED-7) | Wave A A8 — `.github/workflows/docker-image-size-check.yml`; researcher pass measures actual MB delta. CI gate threshold 120 MB. AC#15a. |
| 🟡 MEDIUM | **Subprocess env leak** между tools (phase-8 H3 carry) | См. HIGH-1 row выше. |
| 🟡 MEDIUM | **CI test image не извлекает из главного pipeline** | A1 build-time sanity (`pandoc --version`) — runtime build ломается ДО test target. Owner deploy verifies. |
| 🟡 MEDIUM | **xlsx из bad pipe-tables** — owner ожидает spreadsheet, получает render_failed | Чёткая error message `"markdown-no-tables"` / `"markdown-multi-table"` (MED-3) → модель пересказывает владельцу + retry с правильной структурой. AC#3 + parser robust tests. |
| 🟢 LOW | **Telegram 20 MiB cap** на send_document (LOW-4 makes redundant) | Adapter pre-check size; text fallback при превышении (AC#13). v1 validator делает path теоретическим: `pdf_max_bytes <= 20 MiB`. |
| 🟢 LOW | **Cyrillic font fallback** в WeasyPrint | DejaVu в Dockerfile apt list. AC#1/2 проверяет визуально на live deploy. |
| 🟢 LOW | **WeasyPrint version bump breakage** | Pin `>=63,<70`. Major bump — отдельный phase. |
| 🟢 LOW | **caption=None inconsistent c phase-6a default-caption inbound UX** (LOW-1) | Doc explicit: outbound caption=None intentional; mirrors phase 6e audio path где `emit_direct(text)` precedes attachment. Owner OK. |
| 🟢 LOW | **Pandoc cold-start latency on first PDF render of session** (W2-LOW-2) | Pandoc Haskell runtime cold-start ~150-300ms; 2 invocations per PDF = 300-600ms before actual rendering. WeasyPrint cffi+cairo init another ~200ms first call. First-render-of-session overhead: 500-800ms; subsequent renders ~150ms. Owner-noticeable if first render of session takes >5s consistently — operator runbook documents. Mitigation pandoc-server / warmup invocation deferred phase 10. |
| 🟢 LOW | **RTL languages (Hebrew/Arabic) typography not guaranteed** (W2-LOW-4) | DejaVu carries glyphs but bidi shaping не emitted by markdown→pandoc→html5 pipeline. Phase 11 если owner pastes Arabic/Hebrew. §5 #19. |

## 8. Развилки для Q&A (resolved per devil w1)

**Q1 — Outbound document path: extend `MessengerAdapter` ABC vs only
TelegramAdapter concrete method?** **→ DECIDED**: extend ABC.
Reasoning: phase 10+ Yandex/Discord обязаны реализовать
`send_document` (это user-facing feature gate). HIGH-6 closure: AC#17
testable через monkeypatch без Yandex shipped.

**Q2 — Default `enabled` value.** **→ DECIDED**: `True`.
Owner asked для in-scope feature; `force_disabled` (subsystem-wide
or per-format, HIGH-5) graceful если pandoc/weasyprint отсутствуют.
Devil не возражал на default True; HIGH-5 closure делает
partial-disable безопасным.

**Q3 — TTL vs per-turn delete.** **→ DECIDED**: TTL with in-flight
guard (CRIT-3 closure §2.13). Per-turn delete отвергнут потому что
send_document failures (network, TG retry) лишают модель возможности
retry. In-flight ledger делает TTL safe.

**Q4 — `filename` сanitization агрессивность.** **→ DECIDED**: см.
CRIT-5 §2.4 explicit rule. `unicodedata.category()` + Windows-reserved
+ trailing dot/space + bidi-strip + length 96. Emoji (`So`) accepted.
Cyrillic (`Ll/Lu/...`) accepted. Strict ASCII-only — REJECTED.

**Q5 — Audit log date-stamped rotation в Wave D.** **→ DECIDED**:
ТОЛЬКО render_doc/audit.py (LOW-2 closure). vault_sync/audit.py
остаётся single-step `.1` для preservation phase-8 invariants
(blast radius minimisation).

**Q6 — Subagent видимость render_doc @tool.** **→ DECIDED**: НЕ
давать subagent'у render_doc в phase 9 (consistency с vault_push_now).
Subagent Task-pool НЕ имеет render_doc в catalogue. См. §5 #12. Phase
10 может пересмотреть когда subagent-driven research workflow zрелее.

**Q7 — Markdown variant.** **→ DECIDED**:
`markdown-raw_html-raw_tex-raw_attribute` (CRIT-2 closure §2.6).
Researcher pass обязан эмпирически verify в Wave A spike that pandoc
actually strips raw HTML / SVG / raw-attribute constructs.

**Q8 — Concurrency cap default.** **→ DECIDED**:
`render_max_concurrent=2`. HIGH-4 closure добавил advisory validator
warning если combined worst-case RSS > 1 GB. Reviewer w2 / live VPS
spike обязан подтвердить на 1 GB VPS budget'е.

**Q9 — Включать ли `vault_sync` audit-rotation refactor (Wave D D1)
в phase 9?** **→ DECIDED**: NO (LOW-2 closure). render_doc/audit.py
date-stamped from day one; vault_sync/audit.py НЕ трогаем. Phase 10
может unify через общий helper когда третий subsystem нуждается.

**Q10 — Telegram caption на `send_document`.** **→ DECIDED**:
`caption=None` v1 (LOW-1 closure: documented as intentional, mirrors
phase 6e audio paradigm). Phase 10+ может рассмотреть synthesize
caption from markdown headings.

## 9. Closures applied (devil w1)

> Каждый closure приведён с ID + статусом + указателем на
> §spec section, который теперь его адресует. Future devil w2 /
> reviewers should audit this section first.

### CRITICAL

- **W1-CRIT-1 — Bridge ArtefactBlock yield ordering relative to
  ResultMessage** — **Closed**.
  §2.5 «CRIT-1 closure: per-iteration flush barrier». Handler
  flush'ит `pending_artefacts` на каждый ResultMessage AND на normal
  exit. AC#19 covers.

- **W1-CRIT-2 — Markdown injection enumerates only 2 of ≥9 fetch
  surfaces** — **Closed**.
  §2.6 переписан: full URL fetch surface enumeration + `safe_url_fetcher`
  c `data:` image-MIME allowlist + `markdown-raw_html-raw_tex-raw_attribute`
  pandoc variant. AC#14 разбит на AC#14a–AC#14i.

- **W1-CRIT-3 — TTL sweeper races send_document upload mid-flight** —
  **Closed**.
  NEW §2.13 «In-flight artefact ledger». Sweeper читает
  `ArtefactRecord.in_flight` + `delivered_at`, НЕ mtime. Handler вызывает
  `mark_delivered` after every send attempt. AC#20.

- **W1-CRIT-4 — Daemon.stop cancels in-flight render_doc mid-pandoc,
  leaks orphans** — **Closed**.
  NEW §2.12 «Render lifecycle vs Daemon.stop». `_render_doc_pending`
  drain set + pandoc SIGTERM on cancel + WeasyPrint thread
  uncancellable honesty. AC#21.

- **W1-CRIT-5 — Filename sanitization misses Windows-reserved +
  bidi/control chars + RTL spoofing** — **Closed**.
  §2.4 «CRIT-5 closure» — `unicodedata.category()`-based strip +
  Windows-reserved regex + trailing dot/space reject + 96 codepoint
  cap. Emoji policy explicit (So accepted). AC#22 + matrix table.

### HIGH

- **W1-HIGH-1 — Subprocess `env=` paragraph incomplete** — **Closed**.
  §2.11 ужесточена: pandoc env whitelist `{PATH, LANG, HOME}`.
  Strictly tighter чем phase-8 vault_sync. AC#23.

- **W1-HIGH-2 — Force-disable Telegram notify ordering invariant
  unspecified** — **Closed**.
  §3 C6 + §2.2 wording. Notify dispatched AFTER vault_sync block AND
  BEFORE scheduler block; uses `adapter.send_text` direct call wrapped
  in `asyncio.wait_for(timeout=10s)` per phase-8 F9. AC#24.

- **W1-HIGH-3 — Multi-render-per-turn partial failure semantics** —
  **Closed**.
  §2.5 «HIGH-3 closure». На каждый send_document fail: `send_text`
  fallback line ДО следующего артефакта. AC#25.

- **W1-HIGH-4 — XLSX peak RSS, no concurrency budget** — **Closed**.
  §2.8 mandates `openpyxl.Workbook(write_only=True)` mode.
  `xlsx_max_rows` reduced 10000 → 5000. Settings validator emits
  RSS-budget warning. AC#11 expansion.

- **W1-HIGH-5 — Cannot disable per-format; pandoc fail kills xlsx
  inadvertently** — **Closed**.
  §2.2 + §2.9: `force_disabled_formats: set[str]` populated by
  startup_check. xlsx works без pandoc. @tool body checks per-format.
  AC#5a.

- **W1-HIGH-6 — Yandex stub NotImplementedError AC#17 untestable** —
  **Closed-modified**.
  AC#17 переписан с Yandex contract testing на handler-resilience to
  NotImplementedError via TelegramAdapter monkeypatch. Не требует
  Yandex shipped.

### MEDIUM

- **W1-MED-1 — Audit single-row > rotation budget infinite-loop
  risk** — **Closed**.
  §2.2 audit schema: `error` field truncated to 512 chars; full error
  в structured log only. Применяется только к render_doc audit
  (vault_sync audit не трогаем — LOW-2). Test
  `test_render_doc_audit_single_row_size_cap.py`.

- **W1-MED-2 — `format_invalid` reason dead code** — **Closed**.
  §2.3 enum no longer lists `format_invalid`. SDK enum-валидация
  input schema reject'ит ДО @tool body. Defensive branch returns
  `render_failed_internal` если SDK contract drift'ит.

- **W1-MED-3 — `render_failed` too coarse for model retry** —
  **Closed**.
  §2.3 enum split: `render_failed_input_syntax` /
  `render_failed_output_cap` / `render_failed_internal` +
  machine-parseable kebab-case `error` field. AC#26.

- **W1-MED-4 — `_cleanup_stale_artefacts` mentions only artefact_dir,
  not staging dir** — **Closed**.
  §2.2 boot.py wording: walks `artefact_dir/` + `artefact_dir/.staging/`
  (last unconditional). AC#10 expansion + test
  `test_phase9_cleanup_staging_unconditional.py`.

- **W1-MED-5 — Per-`tool_use_id` ledger keying assumes uniqueness** —
  **Closed**.
  §2.3 + §2.5 explicit note: tool_use_id unique within SDK
  conversation per Anthropic API; collision logs duplicate-key warning
  + last-write-wins. AC#27.

- **W1-MED-6 — Schema-version field absent from envelope** —
  **Closed**.
  §2.3 `schema_version: int = 1` field в envelope. Bridge logs
  warning + skips ArtefactBlock yield on mismatch. AC#28.

- **W1-MED-7 — Image size budget verification mechanism not
  concrete** — **Closed**.
  §3 Wave A A8 — `.github/workflows/docker-image-size-check.yml`
  CI gate (delta > 120 MB → red). Researcher Wave A spike measures
  actual MB delta and updates §6 estimates. AC#15a.

### LOW (judgement per item)

- **W1-LOW-1 — Default caption=None inconsistent with phase-6a/6b
  inbound default-caption** — **Applied (Closed)**.
  §2.5 explicit doc paragraph: outbound caption=None intentional,
  mirrors phase 6e audio paradigm. Cost: 2 lines of comment.

- **W1-LOW-2 — `Q9 vault_sync/audit.py refactor` inside phase 9 =
  scope creep** — **Closed-modified**.
  Owner pre-approved 2 carry-overs in Wave D. Compromise per task
  brief: keep render_doc/audit.py date-stamped from day one (this is
  cheap — same code path); DROP vault_sync/audit.py refactor (preserves
  phase-8 invariants). Wave D scope shrunk significantly. Q9 → DECIDED
  NO.

- **W1-LOW-3 — Wave D D2 (host-key drift CI) does NOT belong in phase
  9** — **Deferred-phase-10**.
  Pure phase-8 follow-up; no relationship to render_doc. Moved to
  phase 10 backlog. §5 item #16.

- **W1-LOW-4 — `pdf_max_bytes=25 MB` exceeds Telegram cap;
  redundancy** — **Applied (Closed)**.
  §2.9 settings validator: `pdf_max_bytes <= 20 MiB` (Telegram cap).
  All format caps enforced ≤ Telegram cap via validator. AC#13
  reworded.

- **W1-LOW-5 — `Wave A A2` spec format inconsistent with
  `pyproject.toml` PEP 621** — **Applied (Closed)**.
  §3 A2: «Add to `[project] dependencies` array... rebuild lockfile
  via `uv pip compile`». Trivial wording fix.

- **W1-LOW-6 — Test count vs surface delta is light** — **Applied
  (Closed)**.
  §3 budget rewritten: ~31 (v0) → ~62 (v1). Reviewer expectations
  aligned.

- **W1-LOW-7 — `Q4 Cyrillic + emoji policy` answered loosely** —
  **Applied (Closed)**.
  §2.4 CRIT-5 explicit table covers emoji (So) accepted, Cyrillic
  accepted, ZWSP/U+202E stripped, trailing space/dot rejected. Q4
  references CRIT-5 rule.

### Devil recommendation REJECTED

- **«Move Wave D D1 + D2 entirely to phase 10»** — **Modified**, not
  fully rejected. Owner pre-approved including up to 2 carry-overs in
  Wave D. Compromise: dropped D2 (host-key drift CI, LOW-3) and shrunk
  D1 to render_doc-only audit rotation; vault_sync/audit.py НЕ
  трогаем. Result: Wave D blast radius reduced significantly while
  preserving owner's "include carry-overs" directive.

---

> **Phase-8 integration note (vault vs artefacts).** Phase 8 vault dir
> `<data_dir>/vault/` и phase 9 artefact dir
> `<data_dir>/artefacts/` физически разнесены. Vault — git working
> tree, artefacts — TTL-управляемый ephemeral pool с in-flight
> ledger (§2.13). Никакого overlap, `.gitignore` vault'а не нужно
> расширять. Если owner когда-нибудь захочет «сохрани этот PDF в
> vault» — это будет отдельная команда / @tool для копирования из
> artefact_dir в vault (out of scope phase 9, см. §5 #10).

## 10. Closures applied (devil w2)

> Каждый w2 closure приведён с ID + статусом + указателем на
> §spec section, который теперь его адресует. Reviewer's audit
> hook: confirm каждый ID maps к live spec text below.

### CRITICAL

- **W2-CRIT-1 — `MessengerAdapter.send_document` ABC extension breaks
  6+ existing test fixtures** — **Closed-modified (Option A)**.
  §2.5 + §3 Wave C C1 + §3.6 «Test fixture migration». ABC
  declares `send_document` as **non-abstract default-impl** raising
  `NotImplementedError`. 6 existing fixtures (phase 5b/6e/8) keep
  instantiating without code edit. AC#17b NEW asserts pre-phase-9
  test suite stays green post-C1. v1 «@abstractmethod» wording
  rejected; HIGH-6 handler-resilience pattern still catches runtime
  NotImplementedError if a future adapter strips override.

### HIGH

- **W2-HIGH-1 — Cumulative `Daemon.stop` drain budget runs past
  `docker stop` / `TimeoutStopSec`** — **Closed-modified**.
  §2.12 cumulative-budget paragraph + §2.9 settings recalibrated
  (`render_drain_timeout_s` 50s → 20s; new
  `pandoc_sigterm_grace_s=5s` + `pandoc_sigkill_grace_s=5s`). User-
  mandated 80s cap acknowledged but actual deployment caps lower
  (compose `stop_grace_period: 35s`, systemd `TimeoutStopSec=30s`);
  honesty paragraph documents that render-in-flight on stop is
  residual risk; orphan staging cleaned next-boot via MED-4
  unconditional `.staging/` walk; orphan WeasyPrint thread killed
  on process exit. AC#21a NEW. Test
  `test_phase9_daemon_stop_total_budget_under_80s.py`. Operator
  runbook addendum recommends override `stop_grace_period: 180s`
  для production renders.

- **W2-HIGH-2 — `_artefacts` dict mutation without lock on
  register_artefact / mark_delivered paths** — **Closed**.
  §2.13 «Lock acquisition discipline» paragraph mandates
  `register_artefact`, `mark_delivered`, `_sweep_iteration` ALL
  acquire `self._artefacts_lock`. Sweeper iterates snapshot copy
  under lock, then unlinks outside lock, then pops under lock.
  Test `test_phase9_ledger_concurrent_mutations.py` parallel-spawns
  10 register + 10 mark_delivered + 1 sweep tick with no
  `RuntimeError` raised.

### MEDIUM

- **W2-MED-1 — `tex_math_dollars` extension survives variant
  subtraction** — **Closed**.
  §2.6 step 2 reword: pandoc invocation now uses
  `markdown-raw_html-raw_tex-raw_attribute-tex_math_dollars-tex_math_single_backslash`
  variant. Researcher Wave A spike verifies на real bookworm.
  AC#14j NEW. Test `test_pdf_renderer_inline_math_defanged.py`.

- **W2-MED-2 — `data:image/*` allowlist contradicts §5 #3 «non-goal»**
  — **Closed**.
  §2.6 step 4 + §5 #3 wording aligned: `safe_url_fetcher` denies
  ALL schemes including `data:` (was: image-MIME data: allowed).
  §5 #3 reworded: «embedded images of any kind, including data:
  URIs». No new ACs; existing AC#14h tightened to assert all data:
  URIs blocked, not only non-image.

- **W2-MED-3 — Validator does not enforce `render_drain_timeout_s
  >= pdf_pandoc + pdf_weasyprint`** — **Closed-modified**.
  §2.9 validator adds explicit check, with deliberate exception
  для default config (drain default 20s < sum 50s в W2-HIGH-1
  recalibration). Validator allows `render_drain_timeout_s == 0`
  как explicit "no-drain" opt-out, AND default-config bypass via
  «drain undersized AND `pdf_pandoc + pdf_weasyprint` exactly equals
  documented sum». Future owner override that's «accidentally too
  small» rejected по нормальному. Documented honesty: validator
  defaults are deliberately undersized; operator runbook explains
  trade-off.

- **W2-MED-4 — Audit row truncation only on `error`** — **Closed**.
  §2.2 audit writer wording: `_truncate_str_fields(row,
  max_chars=256)` helper applied before JSON serialisation, covering
  `filename` + `error` + future str fields. v1's 512-char `error`-only
  cap narrowed to 256 char uniform cap. Test
  `test_render_doc_audit_field_truncation_uniform.py` parametrises
  `filename` overflow + `error` overflow.

- **W2-MED-5 — `requires_pandoc`-marked integration test
  acceptance unspecified** — **Closed**.
  §3 Wave B B3+B4 + AC#15a strengthened. Integration tests
  (`requires_pandoc + requires_weasyprint_runtime` marks) produce
  REAL PDF/DOCX in `--target test` Docker stage. PDF byte-level
  asserts: (a) `b"%PDF-"` magic, (b) `b"%%EOF"` trailer, (c) >2 KB,
  (d) `pikepdf.open()` parses. DOCX byte-level asserts:
  `zipfile.is_zipfile()` True, `python-docx.Document()` parses,
  Cyrillic readable. Asymmetric anti-regression to phase-8
  ssh-not-found — output-side correctness cannot be caught by
  mocked tests.

### LOW (judgement per item)

- **W2-LOW-1 — Observability event inventory missing** —
  **Applied (Closed)**.
  §2.14 NEW «Observability events» — table of 18 event names +
  required fields + when emitted. RSS observer mandated to add
  `render_doc_inflight=N` field via subsystem callback. AC#29 NEW.
  Test `test_phase9_rss_observer_includes_render_doc_inflight.py`.

- **W2-LOW-2 — Pandoc cold-start latency unmeasured** —
  **Applied (Closed)**.
  §6 Зависимости: cold-start estimates 150-300ms pandoc + 200ms
  WeasyPrint (first call); cumulative 500-800ms first render
  overhead. §7 Risk row added. Researcher Wave A spike measures
  p50/p99 on real bookworm container; documents в operator runbook
  if first render of session > 5s. Mitigation (pandoc-server /
  warmup invocation) deferred phase 10.

- **W2-LOW-3 — `filename = "."` edge case explicit handling
  missing** — **Applied (Closed)**.
  §2.4 CRIT-5 sanitization matrix expanded to 14 rows: bare `"."` +
  dots-only (`"..."`) reject `dot-prefix-or-traversal`. Sanitize
  function adds explicit dots-only check after strip. Test
  `test_render_doc_filename_sanitization.py` (Wave B B6) parametrise
  expanded.

- **W2-LOW-4 — Internationalization: Hebrew/Arabic RTL not
  addressed** — **Applied (Closed)**.
  §5 #19 NEW: «RTL bidi text direction… markdown→pandoc→html5
  pipeline does not emit `direction: rtl` CSS; Hebrew/Arabic glyphs
  render but layout left-to-right. Phase 11+ adds CSS direction
  injection if owner uses RTL languages». §7 Risk row added. No
  code or test change.

### Devil w2 recommendation REJECTED

- **None.** All 1 CRIT + 2 HIGH + 5 MED + 4 LOW from devil w2
  closed. Reasoning: each closure was either spec-text-level
  clarification (HIGH-1 honesty paragraph, MED-1 wording fix,
  LOW-1/2/4 new sections) or invariant-tightening (CRIT-1
  Option-A migration, HIGH-2 lock discipline, MED-3 validator
  check, MED-4 truncation expansion, LOW-3 matrix row). No w2
  finding required architectural rewrite or scope expansion.

### Summary

**v1 → v2 spec delta**: ~250 spec lines added (was ~150 estimated by
devil); +8 tests (was ~4 estimated; v2 added cumulative-budget,
ledger-concurrent-mutations, audit-double-truncate, dots-only-edge,
inline-math-defanged, RSS-observer-field, plus 2 в integration suite
for W2-MED-5 byte structure asserts). Test count: 62 (v1) → 70 (v2).
AC count: 28 (v1) → 32 (v2; added AC#14j, AC#17b, AC#21a, AC#29).

**Convergence outlook**: 4-reviewer wave should pass в 1-2 passes
(was 4 passes в phase 8). Risk of phase-8-style 16-hotfix train low
because:
- w1 + w2 collectively attacked all novel surfaces (artefact
  lifecycle, fetch surface, sweeper race, shutdown drain,
  sanitization, ABC migration, observability, lock discipline).
- Closures are paragraph-localised, not architectural rewrites.
- Integration-test acceptance (W2-MED-5) provides asymmetric
  output-side anti-regression that 1014 mocked tests would have
  missed otherwise.
- Cumulative drain budget honesty paragraph (W2-HIGH-1) closes the
  «we'll figure it out at deploy» trap.
