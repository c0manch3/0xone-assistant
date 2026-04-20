# Phase 4 — Detailed Plan (Memory tool + skill)

## Подтверждённые решения (обсуждение закрыто)

> Все вопросы закрыты в пользу Recommended-варианта в интерактивном обсуждении с пользователем (2026-04-17). Ключевые уточнения: Q1 synthetic summary работает на context-level (не Telegram-level); Q4 vault остаётся чистым markdown-хранилищем и open'ается как Obsidian vault; Q8 intersection даёт "safe-only ограничивать, не расширять".
>
> **После wave-1 devil's-advocate review:** план расширен 5 блокерами (B1-B5), 8 gaps (G1-G8), 5 security concerns (S1/S3/S4/S5). Добавлены два Task-0 спайка (SDK per-skill hook + tool_result shape) как prerequisite до researcher implementation.md. Регрессионные тесты для phase-3 compatibility в новой секции "Compatibility with phase-3 skill-installer flow".

| # | Вопрос | Recommended | Альтернативы |
|---|---|---|---|
| Q1 | **History replay strategy** — как phase 4 устраняет phase-2 blocker (tool_result drop в multi-turn)? | **Synthetic summary** — расширить `bridge/history.py`: для каждого tool_result блока в replay'е брать первые 2000 символов (`content[:2000]`) и инжектить вариативной system-note `[system-note: tool X вернул: <snippet>... (полный вывод утерян — вызови снова для деталей)]`. Не зависит от SDK contract, реализуется в существующем коде, риск "модель дёрнет ещё раз" приемлем (single-user, нет cost-pressure). | (a) **U1 empirical replay** — верифицировать что SDK 0.1.59 принимает tool_use+tool_result в async-gen prompt, включить полный replay. Риск: если SDK reject'ит orphan tool_use в середине истории, получаем runtime-ошибку у реального юзера. (b) **`resume=session_id`** — делегировать history SDK; требует ревизии `ConversationStore.load_recent`, turns lifecycle и spike на multi-session resume behaviour. Большой объём phase 4. |
| Q2 | `tools/memory/` — stdlib-only или pyproject (markdown-it-py, python-frontmatter, sqlite-utils)? | **Stdlib-only.** `yaml` уже в main-venv (phase 2 dep); сам memory CLI идёт через `python tools/memory/main.py` (Bash allowlist `python tools/*` уже покрывает) и делит main interpreter. FTS5 — через `sqlite3` (stdlib). Frontmatter parsing дублирует `bridge/skills.py::parse_skill` (регексп + yaml.safe_load) — 20 LOC. | (a) Pyproject с `python-frontmatter` + `sqlite-utils` — удобно, но добавляет `uv sync tools/memory` в `Daemon.start()` / CI и распухает на 30 MB. (b) Переиспользовать `bridge.skills.parse_skill` напрямую — ломает изоляцию CLI от пакета бота (skill-installer B-4 принцип). |
| Q3 | Vault path | **`<data_dir>/vault/`** (XDG `~/.local/share/0xone-assistant/vault/`). Path в `MemorySettings(env_prefix="MEMORY_")` с override `MEMORY_VAULT_DIR`. | (a) `<project_root>/data/vault/` — в git по умолчанию; противоречит phase-2 решению #13 (secrets вне project_root). (b) `~/Obsidian/0xone-assistant-vault/` — удобно открыть из Obsidian desktop, но vendor-lockin на `~/`. **Override поддержан через env**. |
| Q4 | FTS5 index path | **Отдельная БД `<data_dir>/memory-index.db`.** Vault → git backup (phase 7 daily commit); индекс — ephemeral, `memory reindex` восстанавливает. Разделение снимает риск "коммит большого бинарного `.index.db` в git". | (a) `<data_dir>/vault/.index.db` — рядом с файлами, но либо попадёт в git (bloat), либо требует `.gitignore` в vault'е. (b) Общая `assistant.db` — смешивает conversation history и memory-индекс; миграции сложнее. |
| Q5 | Areas (директории в vault) | **Fixed baseline `{inbox, projects, people, daily}`** + модели позволено создавать новые subdir'ы через `--area X` (создаётся lazily). SKILL.md документирует baseline; модель видит существующие areas в `list` и сама решает, куда класть. | (a) Hardcoded enum — модель не может адаптировать структуру под домен. (b) Free-form без baseline — модель будет шарахаться между conventions. |
| Q6 | File naming | **`slug.md` + collision handling.** CLI принимает `PATH` как `area/slug.md`; если file exists → `exit 6` без `--overwrite`. Для `daily/` модель префиксует `YYYY-MM-DD-` сама (документировано в SKILL.md). UUID — только fallback если модель явно попросит `--uuid`. | (a) Автогенерация `YYYY-MM-DD-slug` на все — перегружает структуру. (b) UUID — human-unfriendly при ручном просмотре vault'а. |
| Q7 | Frontmatter required fields | **`title` mandatory.** `tags` / `area` / `created` / `related` optional (CLI добавляет `created` = ISO8601 UTC автоматически если missing). `area` дублируется в directory path — но наличие в frontmatter позволяет reindex после ручных mv. | (a) Только `title` — теряем `created` для time-based queries. (b) Много required полей — friction при быстрых `inbox/` записях. |
| Q8 | Per-skill `allowed-tools` enforcement semantics | **Intersection с global baseline** (safe-only: можно ограничивать, нельзя расширять). Если skill manifest → `{Bash, Read}`, а global baseline → `{Bash, Read, Write, Edit, Glob, Grep, WebFetch}`, эффективный set на turn'е = `{Bash, Read}`. `None` sentinel → baseline целиком (+ существующий `skill_permissive_default` warning). Пустой `[]` → `{}` (lockdown). | (a) Per-skill override (union) — позволяет skill'у требовать redis-client, но разрывает defense-in-depth. (b) Unchanged baseline (оставить phase-3 noop) — блокирует security goal phase 4 (memory skill не должен звать WebFetch). |
| Q9 | FTS5 tokenizer | **`porter unicode61 remove_diacritics 2`**. Porter stemming + unicode normalization + diacritic folding — работает для русского, английского и смешанных. | (a) `trigram` — лучше для частичных совпадений в именах, но взрывает размер индекса (×5) и ухудшает precision на обычный текст. (b) `simple` (default) — нет stemming, "жены" ≠ "жена". |
| Q10 | Concurrent write safety | **`fcntl.flock(LOCK_EX)` на `<data_dir>/memory-index.db.lock`** (отдельный lock-файл, не на сам DB-файл — иначе WAL-mode сойдёт с ума). Per-note локи избыточны в single-user; global lock держится ~50ms на write. | (a) Lock на `.index.db` напрямую — рискует с WAL/SQLite internals. (b) No-lock + SQLite busy_timeout — race между read-index и FS-write. (c) Per-note `.lock` — overhead без выгоды при 1 writer. |

**Дополнительные вопросы (второстепенные):**

| # | Вопрос | Решение |
|---|---|---|
| M1 | Где хранить `MemorySettings`? | Nested в `Settings`: `memory: MemorySettings(vault_dir, index_db_path, fts_tokenizer)`. `env_prefix="MEMORY_"`. Закрывает phase-2 техдолг #8/#9. |
| M2 | `reindex` incremental vs full? | **Full.** Простой и ёмкий — для 1000 notes < 5 сек. Incremental — усложнение без ROI в single-user. |
| M3 | Что с `\index.db` при первом запуске? | `_ensure_index()` в каждом CLI-вызове: `CREATE TABLE IF NOT EXISTS` + `CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts`. Idempotent. |
| M4 | Wikilink resolution | Не делаем. CLI возвращает raw `[[link]]` токены в `read`; модель сама решает навигацию (новый `search "link"` или `read target.md`). Phase 8+ для graph-walk. |
| M5 | `memory write` принимает body из stdin или аргументом? | **Stdin** (`--body -` обязательно; no positional body). Избегаем argv size limits + shell-escaping в Bash-hook. Bash allowlist `python tools/memory/main.py ...` не режется длиной stdin'а. |
| M6 | Phase-3 `_fetch_github_tree_fallback` для non-default refs? | **Не расширяем.** Phase 4 scope — memory. Оставляем как есть; phase 7 (GitHub tool) решит. |
| M7 | `cmd_status` phase-3 runtime health | **Не трогаем в phase 4.** Уже возвращает `installed`/`not-installed` честно (phase 3 review fix #8). |

---

## Сводка решений

| # | Решение |
|---|---|
| Q1 | **Synthetic summary в history replay.** `history.py` → `history_to_user_envelopes` при формировании note'а собирает первые 2000 символов каждого `tool_result.content` и включает в note: `[system-note: в прошлом ходе вызваны: memory. Результат memory.search: "...<2KB snippet>...". Для полного вывода вызови снова.]`. Ошибочные tool_result'ы помечаются отдельно. Блокер phase 2 закрыт минимальными изменениями. |
| Q2 | **Stdlib-only.** `tools/memory/main.py` + `_lib/` (fts.py, vault.py, frontmatter.py). Никакого `pyproject.toml`. `python tools/memory/main.py` — bash allowlist уже покрывает. |
| Q3 | `<data_dir>/vault/` (XDG). Override `MEMORY_VAULT_DIR`. |
| Q4 | `<data_dir>/memory-index.db` — отдельная БД. Vault чистый для git. |
| Q5 | Fixed baseline `{inbox, projects, people, daily}` + free-form создание новых. |
| Q6 | `area/slug.md`; collision → `exit 6`; daily prefix `YYYY-MM-DD-` делает модель. |
| Q7 | `title` required; `created` auto-filled ISO8601; `tags/area/related` optional. |
| Q8 | Per-skill enforcement = intersection с global baseline. `None` → baseline + warning; `[]` → lockdown (`{}`). Патч `ClaudeBridge._build_options`. |
| Q9 | `porter unicode61 remove_diacritics 2`. |
| Q10 | Global `fcntl.flock` на `<data_dir>/memory-index.db.lock`. |
| M1 | `MemorySettings(env_prefix="MEMORY_")` в `Settings`. |
| M2 | Full reindex. |
| M3 | `_ensure_index()` idempotent в каждом CLI-вызове. |
| M4 | Raw wikilinks в `read`; resolution — задача модели. |
| M5 | Body через stdin (`--body -`). |
| X1 | **Vault путь в `system_prompt.md`.** Шаблон получает `{vault_dir}` переменную + блок: "Долговременная память живёт в `{vault_dir}` через skill `memory`. Проактивно записывай факты в `inbox/`." |
| X2 | **Memory CLI exit codes:** `0` OK, `2` usage, `3` validation (invalid frontmatter/path), `4` IO (vault missing), `5` FTS5 error, `6` collision, `7` not-found (`read`/`delete` on missing path). |
| X3 | **JSON stdout формат** во всех commands (как skill-installer): `{"ok": true, "data": {...}}` или `{"ok": false, "error": "..."}`. Human-readable — через pipe `| jq` или `--format text`. Phase 4 — только JSON. |
| X4 | **Vault path-guard через phase-2 file-hook.** Модель не вызывает Write в vault напрямую — только через `memory write` (Bash). Но `Read` из vault — позволен для skill'а; `check_file_path` уже проверяет `is_relative_to(project_root)`. **Vault НЕ под project_root** → модель не может `Read` vault-файлы напрямую. Это фича (изоляция через CLI), не баг. |

### Devil's advocate follow-ups (ожидаем на review)

| ID | Возможное замечание | Контраргумент |
|---|---|---|
| D1 | 2000 chars в synthetic note → чат-контекст растёт × N turn'ов; цена взрывается. | Phase 4 single-user; cost не критичен. Truncate настраивается через `HISTORY_TOOL_RESULT_TRUNCATE_CHARS`; 2000 для `memory.search` output (обычно 1-2 KB) достаточно. |
| D2 | Vault вне project_root → модель не может `Glob` файлы напрямую — deadlock "не знаю что есть". | Это intentional (Q3 + X4). `memory list [--area]` закрывает discovery через CLI. Если модель попытается `Read /home/.../vault/file.md` — hook deny'ит (✓). |
| D3 | Per-skill intersection сломает skill'ы, которые реально нуждаются в тулах, не в baseline (напр. будущий `memcache`-skill, запрашивающий `Redis`). | Baseline — универсальный contract; новые "out-of-baseline" тулы пойдут через расширение global baseline (phase 5+ config-driven). Intersection = safe default. |
| D4 | Если `memory write` падает между rename(md) и FTS insert → inconsistent state. | `write` делает FTS-insert **до** rename (rollback дешевле); на failure FTS → unlink tmp-file; на failure rename → `DELETE FROM notes_fts WHERE path = ?`. Либо оборачиваем в единую транзакцию с SAVEPOINT до rename. |
| D5 | FTS5 `DELETE` при `memory delete` не освобождает rowid → sparse index растёт. | Раз в 30 дней `reindex` — полный rebuild. Для 1000 notes overhead незначителен. |
| D6 | `fcntl.flock` на macOS vs Linux — разная семантика наследования FDs при fork. | Мы не fork'аем CLI. Проверено в skill-installer phase 3 на macOS 24.6. |

---

## Дерево файлов (добавляется / меняется)

```
0xone-assistant/
├── tools/
│   └── memory/                           # NEW (stdlib-only, no pyproject)
│       ├── main.py                       # argparse CLI
│       └── _lib/
│           ├── __init__.py
│           ├── frontmatter.py            # parse/serialize YAML frontmatter
│           ├── vault.py                  # atomic write/read/list/delete
│           ├── fts.py                    # _ensure_index, upsert, search, reindex
│           └── paths.py                  # canonical path validation + resolve
├── skills/
│   └── memory/
│       └── SKILL.md                      # NEW (allowed-tools: [Bash, Read])
├── src/assistant/
│   ├── bridge/
│   │   ├── claude.py                     # CHANGED: per-skill allowed-tools intersection
│   │   ├── history.py                    # CHANGED: synthetic summary (Q1)
│   │   └── system_prompt.md              # CHANGED: memory rules, {vault_dir}
│   └── config.py                         # CHANGED: MemorySettings
├── plan/phase4/
│   ├── description.md                    # CHANGED (этот файл §File A)
│   └── detailed-plan.md                  # NEW (этот файл §File B)
└── tests/
    ├── test_memory_cli_search.py
    ├── test_memory_cli_read.py
    ├── test_memory_cli_write.py
    ├── test_memory_cli_list.py
    ├── test_memory_cli_delete.py
    ├── test_memory_cli_reindex.py
    ├── test_memory_fts5_roundtrip.py     # write → search возвращает hit
    ├── test_memory_fts5_cyrillic.py      # unicode61 + porter на русском
    ├── test_memory_concurrent_writes.py  # fcntl.flock prevents corruption
    ├── test_memory_frontmatter_roundtrip.py
    ├── test_memory_wikilinks_preserved.py
    ├── test_memory_collision_exit6.py
    ├── test_memory_atomic_write_fsync.py
    ├── test_memory_vault_path_outside_project.py  # path-guard не ломает
    ├── test_bridge_history_replay_snippet.py      # Q1 decision
    ├── test_bridge_per_skill_allowed_tools.py     # Q8 intersection
    └── test_skill_memory_frontmatter.py           # [Bash, Read] enforced
```

---

## Пошаговая реализация

### 0. Spike SDK per-skill hook semantics (prerequisite)

До написания любого кода phase 4 researcher **обязан** эмпирически проверить как SDK обрабатывает `allowed_tools` в `ClaudeAgentOptions` в связке с установленными skills. Артефакт: `/Users/agent2/Documents/0xone-assistant/spikes/sdk_per_skill_hook.py`.

Вопросы спайка:
- **S1 (active_skills source).** `_build_options` вызывается **до** `query()` — SDK ещё не прислал `SystemMessage(subtype="init").data["skills"]`. Какой контракт используется:
  - (a) Передаём `allowed_tools = union(all installed skills.allowed_tools) ∩ BASELINE` — тогда Q8 intersection применяется **per-skill-set статически**, не per-turn. Memory skill не может сузить global allowed_tools, если активен `skill-installer` с другим set'ом.
  - (b) SDK per-request партиционирует hook execution по `active_skill` — `allowed_tools` применяется per-turn когда модель вызывает tool от имени конкретного skill.
  - (c) SDK игнорирует `allowed_tools` при наличии `setting_sources=["project"]` и берёт напрямую из SKILL.md.
- **S2 (allowed_tools behavior).** Пробник формирует 3 test case'а:
  - Skill A `[Bash]`, Skill B `[Read]`, options.allowed_tools = `[Bash, Read]`. Может ли A вызывать Read? B — Bash?
  - Skill A declares `[Bash]`, B permissive (None). Если options.allowed_tools = `[Bash]` — может ли B использовать Read?
  - `allowed_tools=[]` (lockdown) на опции vs непустой на SKILL — что побеждает?
- **S3 (SKILL.md frontmatter vs options).** Что если SKILL.md говорит `allowed-tools: [Bash]`, а options.allowed_tools = `[Bash, Read]` — какой выигрывает?

**Выход spike:** `spike-findings.md` с эмпирическим ответом + конкретная формула для `_effective_allowed_tools`. План §6 код переписывается researcher'ом под verified API, не angle-brackets sketch.

### 0b. Spike tool_result.content shape (prerequisite)

Второй пробник: `/Users/agent2/Documents/0xone-assistant/spikes/history_toolresult_shape.py`. Задача — запустить реальный turn (через phase 3 ping skill), посмотреть что именно phase 2 пишет в `conversations.content_json` для `block_type='tool_result'`:
- `str` (plain stdout)?
- `list[{"type": "text", "text": "..."}]`?
- `list` с `image` блоками (base64)?
- Nested `content_json` внутри строки?

**Выход:** зафиксировать в spike-findings.md точную форму. `_render_tool_summary` (detailed-plan §5) переписывается под факт, не угадывание. Добавить handler: если content — `bytes` или structured image — не truncate'ить, а ставить `[binary result: <type>, length=N]` placeholder.

### 1. `MemorySettings` в `config.py`

```python
class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMORY_",
        env_file=(str(_user_env_file()), ".env"),
        extra="ignore",
    )
    vault_dir: Path | None = None      # None → default_data_dir/vault
    index_db_path: Path | None = None  # None → default_data_dir/memory-index.db
    fts_tokenizer: str = "porter unicode61 remove_diacritics 2"
    history_tool_result_truncate_chars: int = 2000

class Settings(BaseSettings):
    # ... (existing)
    memory: MemorySettings = Field(default_factory=MemorySettings)

    @property
    def vault_dir(self) -> Path:
        return self.memory.vault_dir or (self.data_dir / "vault")

    @property
    def memory_index_path(self) -> Path:
        return self.memory.index_db_path or (self.data_dir / "memory-index.db")
```

### 2. `tools/memory/_lib/fts.py` — FTS5 layer

```python
_LOCK_PATH_SUFFIX = ".lock"

def _ensure_index(db_path: Path, tokenizer: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notes(
              path TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              tags TEXT,
              area TEXT,
              body TEXT NOT NULL,
              created TEXT NOT NULL,
              updated TEXT NOT NULL
            )""")
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
              path, title, tags, area, body,
              content='notes', content_rowid='rowid',
              tokenize='{tokenizer}'
            )""")
        # FTS triggers to mirror notes → notes_fts
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
              INSERT INTO notes_fts(rowid,path,title,tags,area,body)
              VALUES (new.rowid,new.path,new.title,new.tags,new.area,new.body);
            END;
            CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
              INSERT INTO notes_fts(notes_fts,rowid,path,title,tags,area,body)
              VALUES('delete',old.rowid,old.path,old.title,old.tags,old.area,old.body);
            END;
            CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
              INSERT INTO notes_fts(notes_fts,rowid,path,title,tags,area,body)
              VALUES('delete',old.rowid,old.path,old.title,old.tags,old.area,old.body);
              INSERT INTO notes_fts(rowid,path,title,tags,area,body)
              VALUES (new.rowid,new.path,new.title,new.tags,new.area,new.body);
            END;
        """)
        conn.commit()
    finally:
        conn.close()

@contextmanager
def vault_lock(db_path: Path):
    lock_path = Path(str(db_path) + _LOCK_PATH_SUFFIX)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
```

### 3. `tools/memory/_lib/vault.py` — atomic write

```python
def atomic_write(vault_dir: Path, rel_path: Path, content: str) -> Path:
    target = vault_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = vault_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8",
        dir=str(tmp_dir), delete=False, suffix=".md",
    ) as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
        tmp_path = Path(f.name)
    os.rename(tmp_path, target)  # atomic on same FS
    return target

def validate_path(rel_path: str) -> Path:
    p = Path(rel_path)
    if p.is_absolute():
        raise ValueError("path must be relative to vault")
    if any(part == ".." for part in p.parts):
        raise ValueError("path must not contain '..'")
    if not rel_path.endswith(".md"):
        raise ValueError("path must end with .md")
    return p
```

### 4. `tools/memory/main.py` — CLI

```python
EXIT_OK, EXIT_USAGE, EXIT_VAL, EXIT_IO, EXIT_FTS, EXIT_COLL, EXIT_NOT_FOUND = 0,2,3,4,5,6,7

def cmd_search(args):
    with vault_lock(index_db):
        conn = sqlite3.connect(index_db)
        where = "notes_fts MATCH ?"
        params = [args.query]
        if args.area:
            where += " AND area = ?"
            params.append(args.area)
        rows = conn.execute(
            f"SELECT path,title,tags,area,snippet(notes_fts,4,'<b>','</b>','...',32) "
            f"FROM notes_fts WHERE {where} ORDER BY rank LIMIT ?",
            (*params, args.limit or 10),
        ).fetchall()
    print(json.dumps({"ok": True, "data": {"hits": [...]}}, ensure_ascii=False))

def cmd_write(args):
    rel = validate_path(args.path)
    body = sys.stdin.read()  # M5
    fm = {"title": args.title}
    if args.tags: fm["tags"] = args.tags.split(",")
    if args.area: fm["area"] = args.area
    fm["created"] = datetime.now(UTC).isoformat()
    content = f"---\n{yaml.dump(fm)}---\n\n{body}"
    target = vault_dir / rel
    if target.exists() and not args.overwrite:
        sys.stderr.write(f"collision: {rel} exists; use --overwrite\n")
        return EXIT_COLL
    with vault_lock(index_db):
        atomic_write(vault_dir, rel, content)
        upsert_index(index_db, rel, fm, body)  # trigger handles notes_fts
    print(json.dumps({"ok": True, "data": {"path": str(rel)}}))
    return EXIT_OK

def cmd_reindex(args):
    # G1: reindex wrapped in SAME lock as write + BEGIN IMMEDIATE → blocks
    # concurrent readers-writers; no partial-index window.
    with vault_lock(index_db):
        conn = sqlite3.connect(index_db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM notes_fts")  # or DROP + CREATE
            conn.execute("DELETE FROM notes")
            count = 0
            for md_path in vault_dir.rglob("*.md"):
                if _should_skip_vault_path(md_path, vault_dir):
                    continue
                fm, body = parse_note(md_path)
                conn.execute(
                    "INSERT INTO notes(path,title,tags,area,body,created,updated) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (...),  # serialized fields
                )
                count += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    print(json.dumps({"ok": True, "data": {"reindexed": count}}))
```

**G2 — Vault scan excludes** (`tools/memory/_lib/vault.py`):

```python
_VAULT_SCAN_EXCLUDES = frozenset({
    ".obsidian",      # Obsidian desktop metadata
    ".tmp",           # our atomic-write staging
    ".git",           # if user git-init'нул vault вручную
    ".trash",         # Obsidian trashed notes
    "__pycache__",
    ".DS_Store",
})

def _should_skip_vault_path(path: Path, vault_root: Path) -> bool:
    rel = path.relative_to(vault_root)
    return any(part in _VAULT_SCAN_EXCLUDES for part in rel.parts)
```

Используется в `list`, `reindex`, любом rglob сканировании. Новый тест `test_memory_list_skips_obsidian_metadata`.

**G3 — Frontmatter parse errors.** `memory read` на файл с невалидным frontmatter (не dict, syntax error) → `exit 3` (validation) с JSON-ответом `{"ok": false, "error": "invalid frontmatter: <reason>", "path": "..."}`. **Не** `{"ok": true, "data": {"frontmatter": {}}}` — это маскирует ошибку и приводит к silent data loss.

**G4 — tags normalization.** В `memory write` и `memory read`: если `frontmatter["tags"]` — строка, нормализовать в `[tags]`; если `None` — `[]`. Покрытие в `test_memory_frontmatter_roundtrip`:
- `tags: foo` (YAML string) → read возвращает `["foo"]`.
- `tags: [foo, bar]` (YAML list) → read возвращает `["foo", "bar"]`.
- `tags:` (null) → `[]`.

**G5 — Memory read FS sync semantics.**

Command semantics:
- `memory list`, `memory search`: читают **из FTS5 индекса** (быстро, но могут быть stale если vault изменён вручную в Obsidian).
- `memory read PATH`: читает **из FS напрямую** (frontmatter + body + wikilinks из .md файла). Всегда актуален.
- `memory write PATH`: пишет FS + обновляет индекс (в транзакции под `vault_lock`).
- `memory delete PATH`: удаляет FS + обновляет индекс.
- `memory reindex`: единственная точка sync FS→index. Ручной рестарт после Obsidian mv/rename.

Автоматический mtime-scan **не** делаем в phase 4 (overhead на каждый CLI invoke). В phase 8 — опциональный FS watcher.

**S3 — Frontmatter injection через body.** В `memory write`:

```python
def _sanitize_body(body: str) -> str:
    """Body cannot contain '---' at column 0 (would spoof frontmatter boundary)."""
    # Escape by indenting any literal '---' line
    lines = []
    for line in body.splitlines(keepends=True):
        if line.strip() == "---":
            line = " " + line  # indent by one space, breaks frontmatter regex
        lines.append(line)
    return "".join(lines)
```

Тест `test_memory_write_body_with_frontmatter_marker_sanitized`.

**S4 — FTS5 body size cap.** В `MemorySettings`:

```python
max_body_bytes: int = 1_048_576  # 1 MB default (env: MEMORY_MAX_BODY_BYTES)
```

В `cmd_write`: `if len(body.encode("utf-8")) > settings.memory.max_body_bytes: exit 3 "body exceeds MEMORY_MAX_BODY_BYTES"`. Тест `test_memory_write_rejects_oversize_body`.

### 5. `bridge/history.py` — synthetic summary (Q1)

```python
# CHANGED: phase 4 extends synthetic note with tool_result snippet.
TOOL_RESULT_TRUNCATE = 2000  # injected from settings

def _render_tool_summary(tool_name: str, results: list[dict]) -> str:
    """Build snippet block for synthetic system-note.

    Spike 0b фиксирует точную форму `content` — этот код переписывается под
    verified shape. Ниже — defensive baseline, учитывающий возможные варианты
    (str / list[text-block] / bytes / structured image).
    """
    snippets = []
    for r in results:
        content = r.get("content", "")
        if isinstance(content, bytes):
            snippets.append(f"результат {tool_name}: [binary, {len(content)}B]")
            continue
        if isinstance(content, list):
            # Join text blocks; drop image/binary blocks with placeholder
            parts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    parts.append(b.get("text", ""))
                elif btype in ("image", "image_url"):
                    parts.append(f"[image block: {btype}]")
            content = "".join(parts)
        s = str(content)
        if len(s) > TOOL_RESULT_TRUNCATE:
            # Safe truncate on Python str (char boundary, not byte) — rstrip
            # trailing partial whitespace and annotate.
            s = s[:TOOL_RESULT_TRUNCATE].rstrip() + "...(truncated)"
        snippets.append(f"результат {tool_name}: {s}")
    return "\n".join(snippets)

# внутри history_to_user_envelopes:
# вместо "(ошибки: X)" — полноценный блок:
# note = "[system-note: вызваны: memory. Результаты:\n
#          результат memory: найдено 3 заметки: ...<snippet>\n
#          Если нужен полный вывод — вызови инструмент снова.]"
```

### 6. `bridge/claude.py` — per-skill allowed-tools (Q8)

```python
# В _build_options (или новом _effective_allowed_tools):

GLOBAL_BASELINE = frozenset({"Bash","Read","Write","Edit","Glob","Grep","WebFetch"})

def _effective_allowed_tools(self, active_skills: list[dict]) -> list[str]:
    """Intersection semantics (Q8):
    - list[] (lockdown) → contributes {} (skill allows no tools)
    - list[x,y]          → contributes {x,y}
    - None (missing)     → contributes BASELINE (permissive; warned at manifest build)
    Effective set = ∪ per-skill contributions, then ∩ BASELINE.
    """
    union: set[str] = set()
    for skill in active_skills:
        tools = skill.get("allowed_tools")
        if tools is None:
            union |= GLOBAL_BASELINE
        elif tools == []:
            pass  # lockdown skill contributes nothing
        else:
            union |= {t for t in tools if t in GLOBAL_BASELINE}
    return sorted(union & GLOBAL_BASELINE)
```

**Важно:** SDK передаётся union всех активных скилов, не per-request-фильтр.
Phase-2 hooks (Bash allowlist, file path-guard, WebFetch SSRF) остаются
defence-in-depth при любом `allowed_tools` set'е.

### 7. `skills/memory/SKILL.md`

```markdown
---
name: memory
description: "Долговременная память через Obsidian-vault. Используй для: сохранить факт ('запомни, что X'), найти ('что мы знаем про Y'), листинг ('что в inbox'). Все заметки в `<data_dir>/vault/`, доступ строго через CLI."
allowed-tools: [Bash, Read]
---

# memory

Долговременная память бота. Vault в `{vault_dir}` (подставляется при старте).
Все операции через `python tools/memory/main.py`.

## Когда использовать

- Пользователь просит запомнить факт → `memory write inbox/<slug>.md`.
- Пользователь спрашивает про что-то из прошлого → `memory search <query>`.
- Пользователь интересуется списком → `memory list [--area inbox]`.
- Нужно прочитать конкретную заметку → `memory read <path>`.

## Areas (структура vault'а)

- `inbox/` — сырые факты, не разобранные.
- `projects/<slug>.md` — активные проекты.
- `people/<name>.md` — заметки о людях.
- `daily/YYYY-MM-DD-<slug>.md` — дневниковые. Используй **локальную** дату пользователя для slug (как ассистент видит current date в system prompt). Frontmatter `created` — UTC ISO8601.

Можно создавать новые areas: `--area <name>`.

## Wikilinks

Используй `[[other-note]]` для ссылок. CLI сохраняет их как есть — вызови
`memory read` на target, чтобы перейти.

## Примеры

User: "запомни, что у жены день рождения 3 апреля"
→ `echo "3 апреля" | python tools/memory/main.py write inbox/wife-birthday.md --title "День рождения жены" --tags personal,family --area inbox --body -`

User: "когда у жены день рождения?"
→ `python tools/memory/main.py search "жена день рождения"` → 1 hit → ответ.

**Проактивность:** любой важный факт из диалога (имена, даты, предпочтения)
записывай в `inbox/` сразу — не спрашивая подтверждения. Разбор в `projects/`
/ `people/` — по запросу пользователя.
```

### 8. System prompt update (`bridge/system_prompt.md`)

```markdown
...
Rules:
- Long-term memory lives in the Obsidian vault at {vault_dir}.
  Access it ONLY via the `memory` skill — never via Read/Write directly.
  Proactively save important facts (names, dates, preferences) into `inbox/`
  during conversation without asking confirmation.
- If the `memory` skill is not yet listed above, tell the owner it's missing
  and do NOT simulate memory with ad-hoc files.
...
```

Template рендерится через `template.format(project_root=..., vault_dir=..., skills_manifest=...)`.

**G6 — format safety.** Если `skills_manifest` или `vault_dir` содержат `{` или `}` — `str.format` упадёт с `KeyError` (или, хуже, интерполирует posix-имя переменной). Мы контролируем source:

- `vault_dir` из `Path` — `{` на практике невозможен в POSIX пути. Но для надёжности `escape_format_literal` helper: `s.replace("{", "{{").replace("}", "}}")`.
- `skills_manifest` строится из SKILL.md frontmatter descriptions — если description содержит `{foo}` (маловероятно, но возможно в мануалах по skill-creator), аналогично escape перед interpolate.

Тест `test_system_prompt_render_escapes_braces`.

### 9. Тесты (минимум, 17 штук)

| Файл | Покрывает |
|---|---|
| `test_memory_cli_write.py` | happy-path write + frontmatter serialization + stdin body |
| `test_memory_cli_search.py` | FTS5 MATCH, `--area` filter, `--limit`, empty results |
| `test_memory_cli_read.py` | read existing, not-found exit 7, frontmatter parsing |
| `test_memory_cli_list.py` | list all, `--area inbox`, JSON shape |
| `test_memory_cli_delete.py` | delete + FTS removal + not-found exit 7 |
| `test_memory_cli_reindex.py` | drop notes_fts → reindex → search hits restored |
| `test_memory_fts5_roundtrip.py` | write(title=X) → search(X) returns correct path |
| `test_memory_fts5_cyrillic.py` | `порох` matches `пороха` via porter+unicode61 |
| `test_memory_concurrent_writes.py` | 2 parallel `subprocess.Popen(memory write ...)` (G7 — не forked threads, чтобы `fcntl.flock` работал на macOS) → both visible + index intact |
| `test_lock_released_after_kill.py` | G7 smoke: SIGKILL один процесс посреди lock, второй может продолжить |
| `test_memory_frontmatter_roundtrip.py` | write → read → frontmatter identical |
| `test_memory_wikilinks_preserved.py` | body `[[foo]]` round-trips через read verbatim |
| `test_memory_collision_exit6.py` | write existing без `--overwrite` → exit 6 |
| `test_memory_atomic_write_fsync.py` | kill mid-write (mock) → target либо not-exists либо fully written |
| `test_memory_vault_path_outside_project.py` | `Read <vault>/x.md` hook deny'ит (✓ изоляция) |
| `test_bridge_history_replay_snippet.py` | Q1: tool_result > 2000 chars → snippet truncated + "(truncated)" marker |
| `test_bridge_per_skill_allowed_tools.py` | Q8: skill A `[Bash]` + skill B `[Read]` → effective `{Bash, Read}` |
| `test_skill_memory_frontmatter.py` | `allowed-tools: [Bash, Read]` parsed корректно |

---

## Compatibility with phase-3 skill-installer flow

Phase 3 summary явно зафиксировал: "Аргумент `--bundle-sha` удалён: модель теряла hash между turn'ами" (phase 3 summary §6.10). Phase 4 synthetic summary **вернёт** hash в history — модель может попытаться передать его в `install --confirm`. Phase 3 CLI этого аргумента не принимает → silent TOCTOU bypass либо exit-error.

Регрессионные требования phase 4:
- **R-p3-1**: preview→install flow остаётся на cache-by-URL, не bundle-sha.
- **R-p3-2**: если snippet содержит hash в stdout tool_result — он **не парсится** skill-installer'ом.
- **R-p3-3**: URL detector (phase 3) реагирует только на `msg.text` из Telegram, **не** на URL в synthetic history snippet.
- **R-p3-4**: marker rotation (phase 3 bootstrap notify) не ломается от memory-turn'ов — memory и bootstrap работают с разными marker-файлами.

Новые тесты:
- `tests/test_phase3_flow_after_phase4_summary.py::test_install_ignores_hash_in_history` — mock turn 1 preview с bundle_sha, mock turn 2 с synthetic snippet содержащим hash, убеждаемся что turn 2 install использует URL, не hash.
- `tests/test_phase3_flow_after_phase4_summary.py::test_url_detector_ignores_history_urls` — history содержит URL в snippet, текущий msg.text без URL → detector не фаерит.

---

## Критерии готовности

- Все 17 тестов зелёные; существующие 107 тестов phase 2-3 — не сломаны.
- E2E (manual QA):
  1. Рестарт daemon, `rm -rf <data_dir>/vault <data_dir>/memory-index.db`.
  2. Telegram: "запомни, что у жены день рождения 3 апреля".
  3. Лог: `memory write inbox/...md` через Bash, `data/run/skills.dirty` НЕ
     touch'ится (vault не под project_root — PostToolUse hook не срабатывает;
     это ожидаемо).
  4. `ls <data_dir>/vault/inbox/` — файл существует.
  5. `/stop` daemon, `/start` заново.
  6. Telegram: "когда у жены день рождения?" → модель отвечает "3 апреля".
- `memory search "жена"` в CLI напрямую возвращает JSON с hit'ом.
- `memory reindex` после ручного удаления `.index.db` восстанавливает search.
- Per-skill enforcement: лог `build_manifest` показывает `effective_tools`
  для каждого активного skill'а; `memory` turn → SDK видит
  `allowed_tools=["Bash","Read"]`.
- Мэнифест-sentinel не крутится на vault-файлы (они не под `skills/` или
  `tools/` — фильтр `_is_inside_skills_or_tools` ✓).

---

## Явно НЕ в phase 4

- Scheduler daily vault commit — phase 5/7.
- MCP Obsidian server — вне архитектуры (все тулы — CLI).
- Embeddings, semantic search, LLM-driven reranking.
- Inline editing (`memory edit`) — делается через `read → write --overwrite`.
- Multi-user vault isolation — single-user.
- Markdown rendering, wikilink graph walk, backlinks.
- Config-driven Bash allowlist extension (phase 5 техдолг #10).
- Remote vault sync (Obsidian Sync, Yandex Disk, Dropbox) — phase 8 opt-in.

---

## Риски и митигации

| # | Риск | Митигация |
|---|---|---|
| R1 | Q1 synthetic summary → контекст взрывается на длинных `memory search` | `TOOL_RESULT_TRUNCATE_CHARS = 2000` — настраивается; для `memory` обычно 1-3 KB. В phase 5 — revisit если cost'ы станут заметны. |
| R2 | Vault вне project_root → `Read` deny'ится — модель может попробовать `cat <vault>` | Bash `cat` validator phase 2 проверяет `_path_safely_inside(project_root)` — тоже deny. Только путь — `memory read` CLI. Это intentional (X4). |
| R3 | FTS5 `UPDATE` trigger при rename/mv файла в vault (Obsidian desktop) → indexer stale | Mtime scan в `_ensure_index`: если mtime заметки > mtime индекса → auto-reindex для этой записи. Phase 5 — FS watcher. |
| R4 | Corrupt SKILL.md поломает manifest build → все skills исчезают | `parse_skill` уже try/except, returns `{}`. Phase 4 — добавить лог `skill_parse_failed` + skip. |
| R5 | Concurrent `memory write` из user turn'а и phase 5 scheduler | `fcntl.flock` + atomic rename — OK. Phase 5 scheduler тестируется против этого invariant. |
| R6 | Per-skill intersection сломает существующие skills (ping, skill-installer) | Оба имеют `allowed-tools: [Bash]` → intersection `{Bash}`, совместимо. Tests: `test_skill_ping_frontmatter` (существует), `test_skill_installer_frontmatter` (новый). |
| R7 | SDK не понимает `allowed_tools=[]` (полный lockdown на skill) | В phase 4 lockdown-only-skill нереалистичен (хоть один Bash нужен для CLI). Если передать `[]` → SDK отклонит turn; покрыто тестом + warning `skill_lockdown_not_enforced`. |
| R8 | `sqlite3` stdlib без row-factory по умолчанию → boilerplate в CLI | Используем `conn.row_factory = sqlite3.Row` в `_ensure_index`. |
| R9 | Vault path с пробелами / unicode ломает shell escaping в SKILL.md примерах | CLI принимает path через argparse — нет shell-expansion; SKILL.md документирует `--body -` и stdin pipe. |
| R10 | Переход с phase 2 drop'а на Q1 summary ломает уже работающие multi-turn диалоги (например, phase 3 skill-installer preview+confirm) | Snippet заменяет drop **в note** (не добавляет отдельный envelope); размер note <= 2 KB × N tool_uses. Покрыто `test_bridge_history_replay_snippet`. |
| R11 | Злонамеренный skill создаёт symlink `skills/memory/data -> <vault_dir>` — попытка обойти изоляцию и прочитать vault через Read | Phase-2 `check_file_path.resolve() + is_relative_to(project_root)` отклонит симлинк выходящий за project_root. Защита уже есть. Добавить регрессионный тест `test_malicious_skill_symlink_to_vault_rejected.py`. |
