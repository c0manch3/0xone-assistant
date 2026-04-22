# Phase 4 — Devil's Advocate wave 3 (on shipped code)

Attack target: `src/assistant/tools_sdk/memory.py` (503 LOC),
`src/assistant/tools_sdk/_memory_core.py` (1083 LOC), `config.py`,
`main.py`, `bridge/claude.py`, `bridge/hooks.py`, `bridge/system_prompt.md`,
`skills/memory/SKILL.md`, 16 memory test modules.

Scope: findings NOT already in wave-1, wave-2, code-review, or QA-review.

## Executive summary

Код технически корректен по большинству угроз, которые ловили wave-1/2.
Но три класса проблем остались незакрытыми:

1. **FTS5-индекс стемится нулями** — `notes_fts` использует
   `unicode61 remove_diacritics 2` на индексной стороне, а
   `_build_fts_query` стемит запрос PyStemmer'ом. Префикс `{stem}*`
   работает ровно в пределах одного морфологического правила Snowball,
   значит поиск по «архитектурный» найдёт заметку «архитектурное», но
   «женой» (творит. падеж, стем `жен`) НЕ найдёт заметку «женщина»
   (стем `женщин`) — семантика частично сломана, хотя тесты зелёные.
2. **Audit-log unbounded и пишет model-controlled JSON** — код-ревью
   пометил H1 про body, но сам механизм (`memory-audit.log`, no rotation,
   `ensure_ascii=False` с model-text) влечёт неограниченный рост И
   log-forging угрозу через встраивание `\n{...}\n` в `tool_input`.
3. **Inflationary prompt-surface** — 6 описаний + имён tools добавляются
   в `allowed_tools` и в `mcp_servers`. Каждая отправка ДОБАВЛЯЕТ
   ~350-500 токенов за счёт schema injection SDK'шей стороны. На каждый
   turn при max_turns=20 умножается.

CRITICAL: 2. HIGH: 4. MEDIUM: 5. LOW: 3. **Coder-blocked: NO** —
фикс-пак может прикрыть только CRITICAL+HIGH перед owner smoke.

---

## CRITICAL

### C3-W3 · FTS index-side tokenizer не стемит — префиксные MATCH-ы частично глухие
**Evidence:** `_memory_core.py:130-134`
```
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  path, title, tags, area, body,
  content='notes', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
```
`_build_fts_query` (L370-400) применяет PyStemmer **только к query**:
`"архитектурное"` → `"архитектурн*"`, а в индексе лежит full form
`"архитектурное"`. Prefix-MATCH `архитектурн*` ловит `архитектурное`,
`архитектурный`, `архитектурная` — потому что общий префикс.

Где ломается: когда Snowball даёт стем короче общего префикса двух форм.
Пример:
- query `женой` → Snowball стем `жен` → `жен*` (по H2.1 условию len>=3 — OK)
- query `жён` → после `ё`→`е` даёт `жен` → `жен*` — тоже OK
- НО query `женский` → стем `женск` → `женск*` — НЕ ловит
  `"женщины"` в индексе, хотя смысловая близость есть.

Это не баг в узком смысле, но **семантический контракт «Russian morphology
search» (строка 140 в memory.py description) превышает реальные
возможности**. Модель увидит пустой результат там, где ожидала хит.

Более опасная сторона: **homograph overreach**. Query `стекло` (glass) и
`стекло` (past-tense verb «flowed»). Snowball даст обоим один стем
`стекл`, и `стекл*` вернёт записи про оба. Без контекста модель покажет
owner'у заметку про разбитый стакан в ответ на вопрос о потоке воды.

**Impact:** Низкая вероятность (owner не будет ловить это в smoke),
но уже заложено в поведение — через месяц faith в поиск подорвётся.

**Fix options:**
- (a) установить в index-side `tokenize='porter'` или custom snowball —
  нарушит существующие тесты, требует миграции.
- (b) **обновить description** — убрать «Russian morphology» fanfare,
  написать честно: *«prefix search with Russian stemming on query
  side»*.
- (c) задокументировать в `SKILL.md` warning: *«stem overreach возможен;
  всегда читай top-3 hits, не полагайся на первый»*.

Рекомендация: (b)+(c) в фикс-паке. (a) отложить в отдельную phase-4.5
миграцию.

### C4-W3 · Audit log — log forging через model-controlled `tool_input`
**Evidence:** `hooks.py:691-701`
```python
entry = {
    "ts": dt.datetime.now(dt.UTC).isoformat(),
    "tool_name": tool_name,
    "tool_use_id": tool_use_id,
    "tool_input": tool_input,          # <- model-controlled dict
    "response": resp_meta,
}
...
fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
```

Проблема: `tool_input` — вложенный JSON объект, JSON-сериализация
безопасна сама по себе (newline'ы внутри string экранируются как `\n`).
НО: body, который модель подаёт в `memory_write`, НЕ ограничен в
audit-log — он пишется **целиком** через `json.dumps(entry)` при
`max_body_bytes=1 MiB`. Это **дубликат H1 из code-review** — уже
отмечено.

**НОВОЕ в W3:** в отличие от body (ограниченного `max_body_bytes`),
**`tool_input` для `memory_search` НЕ ограничен** ничем. Модель может
передать `query` на 10 MiB, ошибка «query has no searchable tokens»
вылезет, но **audit-log уже записал 10 MiB строку**. Аналогично `path`
может быть 10 MiB до того, как `validate_path` отшвырнёт.

**Fix:** в hook обрезать каждый string-value в `tool_input` до
`2048` символов ПЕРЕД `json.dumps`:
```python
def _truncate_strings(obj, max_len=2048):
    if isinstance(obj, str):
        return obj if len(obj) <= max_len else obj[:max_len] + "…"
    if isinstance(obj, dict):
        return {k: _truncate_strings(v, max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_strings(v, max_len) for v in obj]
    return obj
```
Применить: `tool_input_trunc = _truncate_strings(tool_input)`.

---

## HIGH

### H4-W3 · `memory_write` принимает `created` как model-controlled argument — не валидируется
**Evidence:** `memory.py:324-325`
```python
now_iso = dt.datetime.now(dt.UTC).isoformat()
created = str(args.get("created") or now_iso)
```

`created` отсутствует в schema (L266-289 `properties` не содержит
`created`), но код всё равно читает его. Если модель подставит
`created="<script>"` или `created="2500-99-99T99:99:99"`, это уедет:
- в YAML frontmatter (`serialize_frontmatter` не валидирует)
- в `notes.created` column
- обратно в `memory_read.frontmatter.created`

**Почему это важно:** `memory_list` сортирует по `updated DESC` — а если
атакующая нота содержит `updated: "9999-12-31"`, она всегда будет на
вершине списка. Модель «увидит» её первой при каждом list.

Code-review уже пометил H3 (`memory_write drops created`) — это другая
сторона: schema drops, но handler читает. **Double-check: либо schema
добавляет `created` явно, либо handler его игнорирует.** Текущий код —
worst-of-both.

**Fix:** либо убрать `args.get("created")` (всегда `now_iso`), либо
добавить в schema + валидировать ISO-8601 через
`dt.datetime.fromisoformat`.

### H5-W3 · Boot-path `configure_memory` вызывается до `_preflight_claude_auth` прошёл успешно
**Evidence:** `main.py:87-115` — `_preflight_claude_auth` первый,
потом `configure_memory`. **WAIT — actually preflight is first.** Отмена
— неверное утверждение, проверил.

**НО:** реальная HIGH-проблема в `main.py:111-115`:
```python
_memory_mod.configure_memory(
    vault_dir=self._settings.vault_dir,
    index_db_path=self._settings.memory_index_path,
    max_body_bytes=self._settings.memory.max_body_bytes,
)
```
Если `configure_memory` упадёт (vault_dir не creatable — read-only FS,
quota exceeded, etc), **весь daemon падает**, потому что `_fs_type_check`
и `_ensure_index` вызываются без try/except в `main.py`. А Bridge и
сегодняшний Telegram-adapter — ещё не стартовали; owner получит stderr
«FileNotFoundError» вместо вменяемой ошибки в чат.

Сравни с `_preflight_claude_auth`, который через `sys.exit(3)` с
человекочитаемым hint'ом. Memory bootstrap такого нет.

**Fix:** wrap `configure_memory` в try/except OSError → log + sys.exit(4)
с hint `"vault init failed; check MEMORY_VAULT_DIR permissions"`.

### H6-W3 · Двойной daemon — блокировка лок-файла молча деградирует
**Evidence:** `_memory_core.py:579-621` — `vault_lock` при
`blocking=True` поллит 50ms до timeout. **Но auto-reindex на boot
вызывается с `blocking=False` (L848)**. Два работающих daemon'а:
- второй стартует, вызывает `_maybe_auto_reindex` → ловит
  `BlockingIOError` → log warning «memory_auto_reindex_skipped_lock_contention»
- первый daemon держит lock только ВО ВРЕМЯ `write_note_tx` / `reindex`,
  а не весь uptime; значит lock доступен 99% времени между writes.
- **оба daemon'а могут получить lock по очереди, каждый пишет в ТОТ ЖЕ
  vault + ТОТ ЖЕ sqlite!** Результат: удваиваются audit-записи,
  возможны sqlite lock-timeouts при одновременных writes (sqlite WAL
  разруливает, но FTS triggers могут рассинхронизироваться).

`_preflight_claude_auth` не проверяет singleton. В phase-1 (пропустили,
потому что skip-wiped?) нет pidfile + flock на уровне daemon.

**Fix:** добавить `<data_dir>/run/daemon.pid` lock в `Daemon.start()`
ДО configure_memory. Aksиальный check, но защищает от accidental `systemctl
restart` + manual `uv run assistant` overlap.

### H7-W3 · `_fs_type_check` subprocess на Darwin тормозит boot на 5 сек при suspended mount
**Evidence:** `_memory_core.py:485-536`, L499 `timeout=_FS_TYPE_CMD_TIMEOUT_SEC=5.0`

`/sbin/mount` на Darwin иногда зависает при suspended network-volume
(iCloud cache rebuild, Time Machine snapshot в процессе). Таймаут 5 сек
добавляется к boot-time. Если vault **находится** на Suspended FS,
дальнейший `_ensure_index` тоже зависнет (open → sqlite.connect блокирует).

**Impact:** Рары, но детерминирован — Owner будит Mac после ночи с
заснутым iCloud, daemon стартует 30+ секунд вместо 2.

**Fix:** вынести `_fs_type_check` в bg task (fire-and-forget после
configure_memory вернулся), логировать warning если обнаружен unsafe
FS **post-facto**. Не блокировать boot на предупредительном checks.

---

## MEDIUM

### M4-W3 · `_maybe_auto_reindex` на 500-note vault делает ОДНУ transaction, но `rglob` + `stat` ×2
**Evidence:** `_memory_core.py:816-817` — `_scan_vault_stats` делает
первый проход (count + max_mtime_ns), потом если reindex нужен —
`reindex_vault` делает второй проход `rglob` + stat + read + parse.
Для 500 заметок это ~1000 stat() + 500 read_text() + 500 yaml.load().

На macOS APFS это ~200-400ms holistic, не страшно. Но **блокирует
`configure_memory`, который блокирует `Daemon.start()`, который
блокирует Telegram startup**. Owner посылает «привет» — молчание 500ms
от первого turn'а.

**Fix:** reindex в bg task (как skill_creator bootstrap):
```python
self._spawn_bg(asyncio.to_thread(core._maybe_auto_reindex, vault, idx))
```
и убрать `_maybe_auto_reindex(...)` из `configure_memory` тела.

### M5-W3 · Stale tmp-файлы в `<vault>/.tmp/` не подчищаются
**Evidence:** `memory.py:81` создаёт `.tmp/`, `atomic_write` L272 — use
NamedTemporaryFile(dir=tmp_dir, delete=False). При SIGKILL daemon'а
МЕЖДУ `tf.flush()` и `os.replace`, tmp-файл `.tmp/tmp12345.md`
остаётся навсегда. Grep по `_memory_core.py` не находит sweep-логики
для `<vault>/.tmp/` в отличие от installer'а (`sweep_run_dirs`).

За месяц работы с крашами daemon'а накапливается сотня-другая orphan
tmp-файлов (на практике — 3-5, потому что SIGKILL редок).

**Fix:** добавить `_sweep_vault_tmp(vault_dir, max_age_sec=86400)` в
`configure_memory`, удаляет файлы `.tmp/.tmp-*.md` старше суток.
Cheap one-liner.

### M6-W3 · Skill `memory/SKILL.md` имеет `allowed-tools: []` — пустой массив
**Evidence:** `skills/memory/SKILL.md:4`
```
allowed-tools: []
```

Skill не нуждается в tool'ах напрямую (описывает model-поведение), но
`allowed-tools: []` по semantic'е SDK может означать «деактивировать
все tools на время Skill execution». Проверь: модель, находясь внутри
Skill-контекста, может не иметь доступа к `mcp__memory__*`.

Midomis reference (`/Users/agent2/Downloads/midomis-assistent/`) должен
прояснить — grep и верифицировать behavior.

**Fix (если подтвердится):** указать `allowed-tools:
[mcp__memory__memory_search, mcp__memory__memory_read, ...]` явно.

### M7-W3 · Model prompt bloat — 6 tool schemas + descriptions + wrapping instruction
**Evidence:** `bridge/system_prompt.md:25-42` — уже 17 строк про memory.
Плюс SDK инжектит 6 schema объектов из `MEMORY_SERVER` в initial system
message. Оценка: +400-600 токенов на `sdk_init` message per turn.

При `history_limit=20` × 600 токенов = 12k токенов только на memory
schemas — prompt caching их поднимает, но при первом turn всё это
уезжает как cache_creation_input_tokens.

**Fix (long-term):** вынести memory help в динамический Skill body,
который загружается только когда модель явно вызвала Skill(memory).
Короче system_prompt = меньше latency на первый turn.

### M8-W3 · `memory_read` теряет `created`/`updated` из frontmatter если они `dt.date`
**Evidence:** `memory.py:239-254` — `safe_fm` фильтрует ключи, но
`parse_frontmatter` с `IsoDateLoader` (L315) возвращает ISO-строки. Всё
ОК.

Но если **существующая нота** была записана Obsidian'ом с
`created: 2024-01-15` (native date), parse вернёт `"2024-01-15"` string
(IsoDateLoader coerce). Теперь `safe_fm["created"] = "2024-01-15"`.
Отдаётся модели как «date-like string». **Модель может попытаться
сравнивать с ISO-datetime**; строгий `<` работает лексикографически, но
`"2024-01-15" < "2024-01-15T00:00:00"` — True (short string loses).

Не критично для single-user, но если модель строит сортировку по датам —
ordering non-deterministic для mixed notes.

**Fix:** нормализовать даты при `memory_read`: если `len(s)==10`,
добавить `T00:00:00+00:00`.

---

## LOW

### L4-W3 · `extract_wikilinks` молча сливает дубликаты
**Evidence:** `_memory_core.py:1066-1083` — list, не set. Note с
`[[foo]] ... [[foo]] ... [[foo]]` → `wikilinks=["foo","foo","foo"]`.
Модель видит дубликаты. Косметика; `sorted(set(...))` fix.

### L5-W3 · `SKILL.md` упоминает tools без `mcp__memory__` префикса в одном месте
**Evidence:** `SKILL.md:6` — «Six MCP tools» с полным префиксом,
потом `SKILL.md:43` — `handler stems them (жены → жен*)`, но
`SKILL.md:14` говорит «memory_search with Russian morphology». Частично
inconsistent с system_prompt.md:27. Неправильно может подсказать модели
вызвать `memory_search` без префикса. Косметика.

### L6-W3 · `memory_list` hardcoded `limit=100, offset=0` — нет pagination exposed модели
**Evidence:** `memory.py:396-397` — всегда `limit=100, offset=0`. При
500 заметках модель видит только первые 100 по `updated DESC` — OK для
recency, но скрыто от модели (в JSON нет `offset` аргумента). При 2000
notes (edge после вырубленного `_MAX_AUTO_REINDEX`) cut-off молчаливый.

**Fix:** expose `limit`/`offset` в schema, map через `args.get`.

---

## Risks carried to phase 5+

- **Phase 5 (new tools):** любой новый `@tool`, который пишет в
  audit-log, должен naсcledовать truncation-pattern C4-W3.
- **Phase 7 (user CLI):** если вводится `/memory` команда к daemon,
  singleton-lock (H6-W3) должен быть уже на месте. Иначе daemon + CLI
  одновременно держат vault lock → race.
- **Phase 8 (GitHub backup):** ежедневный push vault'а столкнётся с
  `<vault>/.tmp/` — если не .gitignore'ить, push'аем orphan tmp-файлы.
  Добавить `.tmp/` в vault-level `.gitignore` при первом commit.
- **Phase 9 (log rotation):** audit-log unbounded — Q-R4 deferred. Но
  если FTS index на 50 MB нормально, audit на 500 MB bloat'ит vault
  backup. Плановый fix.

## Unknown unknowns

1. **PyStemmer thread-safety через asyncio.to_thread.** Комментарий в
   `_memory_core.py:43` утверждает thread-safe, но ни одного теста не
   запускает 2 concurrent `memory_search` в разных threads. Claim
   unverified. Если false — sporadic crashes под parallel Telegram traffic
   (max_concurrent=2).
2. **`configure_memory` idempotent re-config.** H2.6 говорит
   `max_body_bytes` можно менять. Но если owner меняет `MEMORY_VAULT_DIR`
   в env между restart'ами, **старый vault просто забрасывается, новый
   инициализируется пустым**. Никакого warning'а. Owner теряет заметки,
   не понимая почему. Не баг strictly, но UX-ловушка.
3. **FTS5 `content='notes'` contentless-mode vs. external-content mode.**
   Текущая схема использует external content (`content='notes'`) —
   rebuild при `INSERT INTO notes_fts VALUES('rebuild')` физически пере-
   сканирует `notes` table. Но triggers ТОЖЕ пишут в FTS на каждый
   INSERT/UPDATE/DELETE. Что если триггер и `rebuild` бегут concurrently
   в разных connection'ах? SQLite должен сериализовать через WAL, но
   corner case — reindex + concurrent write. Не protected lock'ом
   (reindex_vault под vault_lock, write_note_tx тоже под vault_lock,
   OK). **Но auto_reindex при boot with `blocking=False` + concurrent
   write в другой daemon instance (H6-W3) — path untested.**

---

## Итого

| Severity | Count |
|----------|-------|
| CRITICAL | 2     |
| HIGH     | 4     |
| MEDIUM   | 5     |
| LOW      | 3     |

**Coder-blocked: NO.** В фикс-пак (task #8) — C4-W3 (audit truncation),
H4-W3 (`created` validation), H5-W3 (boot crash handling) как top-3
quick wins. Остальные — task #9 (phase ship notes) или phase 5 backlog.
C3-W3 требует отдельного решения owner'а: либо мы правим description
и SKILL.md (10 минут), либо принимаем «лучшее, чем grep».
