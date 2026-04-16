# Phase 2 — Detailed Plan

## Подтверждённые решения (обсуждение закрыто)

Все вопросы закрыты в пользу Recommended-варианта в интерактивном обсуждении с пользователем.

| # | Вопрос | Recommended | Альтернативы |
|---|---|---|---|
| Q1 | Контракт `ClaudeBridge.ask` | **`AsyncIterator[Block]`** — стрим блоков; handler агрегирует и сам решает, что слать в БД и пользователю | (a) `list[Block]` batch — проще, но теряем инкрементальные логи и исключает будущий live-edit; (b) `AsyncIterator[str]` — выбрасывает `ToolUseBlock` из чата, но handler теряет возможность писать tool-use в БД как-есть |
| Q2 | Telegram delivery стратегия | **Буферизуем полностью, шлём одним `send_message` в конце (split если >4096)**, typing-ping всё время | (a) edit_message_text каждые N tokens — живой UX, но 429/flood risk и усложняет split; (b) стримить по абзацам (новое сообщение на каждый параграф) — шумно |
| Q3 | Parse mode | **None (plain text)** — Claude любит markdown, но без валидации выдаёт невалидный Markdown; безопаснее plain | (a) MarkdownV2 с агрессивным escape; (b) HTML с конверсией md→html |
| Q4 | Persistence блоков | **Один turn_id → N строк, по строке на SDK-блок (`role` ∈ {user, assistant, tool_use, tool_result, thinking})** | (a) Одна строка на весь ассистент-turn с массивом блоков — меньше rows, но сложнее селективно исключать `thinking` при репродусе истории |
| Q5 | DI vs singleton `get_settings` | **DI: `Daemon(settings)`, bridge/handler получают `settings` параметром**, `get_settings()` остаётся только как фабрика в `main()` | Оставить singleton — меньше diff, но тесты продолжают бить cache |
| Q6 | Nested Settings секции | **Да: `ClaudeSettings` sub-model с `env_prefix="CLAUDE_"`** + плоские `telegram_bot_token`, `owner_chat_id` пока оставить | Оставить плоско до phase 3 |
| Q7 | `tools/ping` runtime | **Одиночный `main.py` на stdlib (без своего `pyproject.toml`)**, запускается как `python tools/ping/main.py` | Отдельный `uv`-проект — избыточно для smoke |
| Q8 | `cwd` для SDK | **Корень проекта** (`settings.project_root`, дефолт `Path.cwd()`) | `tools/` — но тогда `.claude/skills/` не найдётся рядом |
| Q9 | Path-guard scope | **Read/Write/Edit/Glob/Grep** только, Bash/WebFetch без ограничений | Полный guard включая Bash — ломает скилы |
| Q10 | `thinking` блоки в БД | **Писать с `role="thinking"`**, но НЕ подавать обратно в SDK при репродусе истории (SDK сам отказывается от чужих thinking) | Не писать вовсе — теряем трассировку |

## Сводка решений

| # | Вопрос | Решение |
|---|---|---|
| 1 | SDK пакет | `claude-agent-sdk>=0.1` (проверить последнюю версию во время реализации) |
| 2 | ask signature | `async def ask(chat_id, user_text, history) -> AsyncIterator[Block]` |
| 3 | Handler signature | `async def handle(msg) -> AsyncIterator[str]` |
| 4 | Адаптер | агрегирует AsyncIterator в финальный текст; split 4096; parse_mode=None |
| 5 | cwd SDK | `settings.project_root` (корень репо) |
| 6 | setting_sources | `["project"]` |
| 7 | Skills discovery | Симлинк `.claude/skills → ../skills`, создаётся в `Daemon.start()` идемпотентно |
| 8 | Manifest | Собирается на каждый запрос, внедряется через `{skills_manifest}` в system prompt |
| 9 | path-guard | Только Read/Write/Edit/Glob/Grep, base = `project_root`; Bash — allow + pre-hook regex deny на secrets; WebFetch — allow |
| 10 | Semaphore | `claude_max_concurrent=2` (см. строку 20) |
| 11 | Timeout | `claude_timeout=300` (сек) |
| 12 | max_turns | `claude_max_turns=20` |
| 13 | Persistence | 1 turn_id → N rows, `role` ∈ {user, assistant, tool}, `block_type` ∈ {text, tool_use, tool_result, thinking}; meta вынесена в `turns` |
| 14 | meta_json | На `turns`-row: `{model, usage:{input_tokens, output_tokens, cache_read, cache_creation}, stop_reason, cost_usd}` (apply на ResultMessage). На `conversations`-rows — нет. |
| 15 | parse_mode | `None` (plain text) в phase 2, ревью в phase 3+ |
| 16 | History limit | `load_recent(limit=20)` — последние **20 turn'ов** (turn-based, не rows); фильтр `turns.status='complete'` |
| 17 | Тех долг | Закрываем #1 (nested Settings), #2 (DI), #3 (streaming contract = emit-callback), #4 (нормализация — таблица `turns`), #6 (parse_mode решение). Долг #5 — остаётся. |
| 18 | Тесты | +6 юнитов: manifest parser, manifest cache invalidation, bridge (mock query), bootstrap symlink, load_recent turn-boundary, interrupted-turn-skipped |
| 19 | Secrets & data | `.env` и `data/` вне `project_root` (`~/.config/0xone-assistant/.env`, `~/.local/share/0xone-assistant/`); Bash pre-hook блокирует чтение `.env*`, `*.db`, `.ssh`, `secrets`, `token`, `password` |
| 20 | Concurrency bump | `claude_max_concurrent=1 → 2` — снимает deadlock-риск phase 5 scheduler (0 строк кода) |
| 21 | Handler contract | `Handler.handle(msg, emit: Callable[[str], Awaitable[None]]) -> None` — bridge всё ещё стримит блоки, handler передаёт текст в `emit`, адаптер аккумулирует |
| 22 | Spike | Task 0 (R1–R5) blocker; артефакт `plan/phase2/spike-findings.md` |
| 23 | Turn lifecycle | `turns.status` ∈ {pending, complete, interrupted}; `try/finally` в `ClaudeHandler.handle` |

## Решения по итогам devil's advocate review (фаза 2, волна 2)

| ID | Замечание | Фикс |
|---|---|---|
| Task 0 | До spike'а нет верифицированных подписей SDK — researcher/coder работают вслепую | Новая prerequisite-задача №0 (R1–R5), артефакт `spike-findings.md`; coder не стартует без неё |
| S5 | `.env` и `data/*.db` лежат внутри `project_root`, а `cwd` SDK = `project_root` → модель может читать через Bash | Вынос `.env` → `~/.config/0xone-assistant/.env` (pydantic-settings ищет в обоих, первый wins); `data_dir` → `~/.local/share/0xone-assistant/`; Bash pre-hook regex `\b(\.env|\.ssh|secrets|\.db\b|token|password)\b` |
| B1 | `history_limit=40 rows` режет ассистент-turn посередине, SDK получает orphan blocks | Заменить на `history_limit=20 turns`; SQL по `turn_id IN (... ORDER BY MAX(id) DESC LIMIT N)`; синтетический тест границы |
| B2 | Прерванный turn оставляет orphan `tool_use` без `tool_result` → SDK ругается | Колонка `turns.status` (`pending`/`complete`/`interrupted`); `try/finally` в handler; история фильтрует `status='complete'`. Backfill синтетическим `tool_result` — future-work |
| S1 | `Handler.handle → AsyncIterator[str]` плохо ложится на phase 5 scheduler (нет owner-chat в контексте) | Заменить на `emit`-callback; адаптер реализует свой emit (буфер + finalize), scheduler — свой (push в чат owner'а) |
| S6 | Денормализованная схема (`meta_json` на каждом row, нет первичного носителя статуса turn'а) | Миграция 0002: `CREATE TABLE turns(...)`, `ALTER conversations ADD COLUMN block_type`, FK CASCADE; meta переезжает на `turns`. Закрывает техдолг #4 в phase 2 |
| S2 | `claude_max_concurrent=1` создаст deadlock когда phase 5 scheduler начнёт триггерить фоновые turn'ы параллельно с пользовательскими | Поднять до `2` — 0 строк кода, snimaет риск заранее |
| S3 | Manifest пересобирается на каждый запрос → лишний disk IO (мелочь, но уже видно в hot path) | mtime-кэш по `skills_dir`: модуль-уровневый dict, инвалидируется при atomic rename из skill-installer |

## Дерево файлов (добавляется / меняется)

```
0xone-assistant/
├── .claude/
│   └── skills -> ../skills          # симлинк, создаётся программно; в .gitignore
├── .env.example                     # (+CLAUDE_*; коммент про ~/.config/0xone-assistant/.env)
├── pyproject.toml                   # (+claude-agent-sdk, pyyaml)
├── skills/
│   └── ping/
│       └── SKILL.md                 # NEW: frontmatter + инструкция
├── tools/
│   └── ping/
│       └── main.py                  # NEW: prints {"pong": true}
├── spikes/                          # NEW (опц.) — артефакт Task 0
│   └── sdk_probe.py                 # либо вместо него plan/phase2/spike-findings.md
├── plan/phase2/
│   └── spike-findings.md            # NEW: ответы R1–R5
├── src/assistant/
│   ├── config.py                    # CHANGED: nested ClaudeSettings, project_root, _default_data_dir, env_file=[~/.config/.../​.env, .env]
│   ├── main.py                      # CHANGED: DI settings, вызов bootstrap
│   ├── bridge/                      # NEW
│   │   ├── __init__.py
│   │   ├── claude.py                # ClaudeBridge + permission_callback + bash_pre_hook
│   │   ├── skills.py                # frontmatter parser + mtime-cached manifest builder
│   │   ├── bootstrap.py             # ensure_skills_symlink()
│   │   ├── system_prompt.md         # шаблон
│   │   └── history.py               # load_recent → SDK; фильтр turns.status='complete'
│   ├── store/
│   │   ├── conversations.py         # CHANGED: load_recent turn-based; Turn API
│   │   └── migrations/
│   │       └── 0002_turns_block_type.sql   # NEW
│   ├── handlers/
│   │   └── message.py               # CHANGED: ClaudeHandler принимает emit, try/finally turn_status
│   └── adapters/
│       ├── base.py                  # CHANGED: Handler protocol → handle(msg, emit)
│       └── telegram.py              # CHANGED: реализует emit, агрегация, split >4096
├── README.md                        # CHANGED: где живут .env и data/
└── tests/
    ├── test_skills_manifest.py                       # NEW
    ├── test_skills_manifest_cache.py                 # NEW
    ├── test_bridge_mock.py                           # NEW
    ├── test_bootstrap.py                             # NEW
    ├── test_load_recent_turn_boundary.py             # NEW
    └── test_interrupted_turn_skipped.py              # NEW
```

`.gitignore` дополнительно: `.claude/skills` (симлинк пересоздаётся). Запись `.env` остаётся как legacy (реальный `.env` теперь снаружи репо — задокументировать в README).

## Пошаговая реализация

### 0. SDK spike (blocker, 30–60 мин)

До любых правок в `src/assistant/bridge/*.py` запустить probe против актуальной версии `claude-agent-sdk` и зафиксировать ответы:

| ID | Вопрос | Что проверить |
|----|--------|---------------|
| R1 | Multi-turn history | Принимает ли `query()` prompt-итерируемое; формат dict (`{"type":"user","message":{"role":"user","content":[...]}}` vs плоский); или нужен `ClaudeSDKClient` streaming loop / `resume=session_id` |
| R2 | `ThinkingBlock` | Приходит автоматически на supported-модели или требует `extra_args={"thinking": {...}}` |
| R3 | Skills auto-discovery | `setting_sources=["project"]` действительно читает `.claude/skills/*/SKILL.md`; если нет — какой параметр (`plugins`, `skills`, что-то другое) |
| R4 | `AssistantMessage.content` | Целиком в одном message или стримом блоков |
| R5 | Permission-callback | `can_use_tool=fn` или `hooks={"PreToolUse": [...]}`; точная сигнатура (sync/async, аргументы, return shape) |

**Артефакт:** `spikes/sdk_probe.py` (исполняемый smoke) или `plan/phase2/spike-findings.md` с ответами + версией SDK + ссылками на источники. После spike researcher пересобирает `implementation.md` поверх verified API; coder/реализатор не стартует без артефакта.

### 1. `pyproject.toml`

Добавить в `dependencies`:
```
"claude-agent-sdk>=0.1",
"pyyaml>=6.0",
```
Dev: `types-pyyaml`.

### 2. `.env.example` + `config.py`

```python
def _user_env_file() -> Path:
    return Path.home() / ".config" / "0xone-assistant" / ".env"

def _default_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "0xone-assistant"

class ClaudeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_",
        env_file=[_user_env_file(), Path(".env")],   # первый найденный wins
        extra="ignore",
    )
    timeout: int = 300
    max_turns: int = 20
    max_concurrent: int = 2          # bumped: см. строку 20 в сводке решений
    history_limit: int = 20          # turns, not rows

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    telegram_bot_token: str
    owner_chat_id: int
    data_dir: Path = Field(default_factory=_default_data_dir)
    log_level: str = "INFO"
    project_root: Path = Path(__file__).resolve().parents[2]
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)  # type: ignore[arg-type]

    @property
    def db_path(self) -> Path: ...
```

`.env.example` добавляет `CLAUDE_TIMEOUT`, `CLAUDE_MAX_TURNS`, `CLAUDE_MAX_CONCURRENT`, `CLAUDE_HISTORY_LIMIT`.

**Файлы вне репо:**
- `~/.config/0xone-assistant/.env` — реальный prod `.env` (создаёт пользователь вручную; pydantic-settings найдёт первым).
- `~/.local/share/0xone-assistant/` — `data_dir`, тут живёт SQLite. `Daemon.start()` делает `data_dir.mkdir(parents=True, exist_ok=True)`.
- `./.env` остаётся допустимым fallback'ом для dev-режима. `.gitignore` запись `.env` сохраняется (защита от случайного коммита).
- README в phase 2 описывает где лежит `.env` и `data/`, как мигрировать с phase-1 локального `.env`.

### 3. DI-рефактор

`Daemon.__init__(self, settings: Settings)` — принимает явно.
`main()` собирает: `settings = get_settings(); setup_logging(...); d = Daemon(settings)`.
`TelegramAdapter`, `ClaudeBridge`, `ClaudeHandler` — `settings` параметром.
`get_settings()` остаётся только в `main()`.

### 4. `src/assistant/bridge/bootstrap.py`

```python
def ensure_skills_symlink(project_root: Path) -> None:
    target = project_root / "skills"
    link = project_root / ".claude" / "skills"
    link.parent.mkdir(exist_ok=True)
    if link.is_symlink():
        if link.readlink() == Path("../skills"):
            return
        link.unlink()
    elif link.exists():
        raise RuntimeError(f".claude/skills exists and is not a symlink: {link}")
    link.symlink_to("../skills", target_is_directory=True)
```

Вызывается из `Daemon.start()` до запуска адаптера.

### 5. `src/assistant/bridge/skills.py`

Парсер frontmatter (собственный — не нужен весь `python-frontmatter`, YAML достаточно):

```python
_FRONT_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

def parse_skill(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    m = _FRONT_RE.match(text)
    if not m:
        return {}
    meta = yaml.safe_load(m.group(1)) or {}
    return {"name": meta.get("name", path.parent.name),
            "description": meta.get("description", "").strip(),
            "allowed_tools": meta.get("allowed-tools", [])}

_MANIFEST_CACHE: dict[Path, tuple[float, str]] = {}

def _skills_dir_mtime(skills_dir: Path) -> float:
    """Наибольший mtime среди samого `skills_dir` и всех `SKILL.md` (рекурсивно через glob)."""
    mtimes = [skills_dir.stat().st_mtime]
    for p in skills_dir.glob("*/SKILL.md"):
        mtimes.append(p.stat().st_mtime)
    return max(mtimes)

def build_manifest(skills_dir: Path) -> str:
    mtime = _skills_dir_mtime(skills_dir)
    cached = _MANIFEST_CACHE.get(skills_dir)
    if cached and cached[0] == mtime:
        return cached[1]
    entries = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        meta = parse_skill(skill_md)
        if not meta.get("description"):
            continue
        entries.append(f"- **{meta['name']}** — {meta['description']}")
    manifest = "\n".join(entries) if entries else "(no skills registered yet)"
    _MANIFEST_CACHE[skills_dir] = (mtime, manifest)
    return manifest
```

**Rationale:** mtime-кэш снимает disk-IO в hot path, сохраняя свежесть. Race-safe: phase-3 skill-installer пишет через `tempfile + rename()` (atomic), что меняет mtime директории `skills/<name>/` и инвалидирует кэш на следующем `build_manifest`. Тест `test_skills_manifest_cache_invalidates_on_new_skill`: создать skill → manifest A; создать второй skill (с актуальным mtime touch) → manifest B содержит оба.

### 6. `src/assistant/bridge/system_prompt.md`

```
You are 0xone-assistant, a personal Claude-Code-powered assistant for your owner.

Identity & style:
- Default language: Russian unless the user writes in another language.
- Be concise; avoid filler.

Capabilities:
- You have access to the project at {project_root}.
- You extend your own capabilities through Skills (self-contained CLI tools).

Available skills (rebuilt on every request):
{skills_manifest}

Rules:
- Long-term memory lives in an Obsidian vault accessible only through the `memory` skill.
  If the `memory` skill is not yet listed above, tell the owner you cannot persist long-term
  memory yet and do NOT try to simulate it with ad-hoc files.
- Do not invent skills that are not in the list above.
- Bash is allowed, but never run destructive commands (rm -rf, git push --force, dd, ...)
  without explicit confirmation from the owner.
- File edits are sandboxed to {project_root}.
```

Шаблон читается при каждом запросе (дёшево), подставляются `{project_root}` и `{skills_manifest}`.

### 7. `src/assistant/bridge/history.py` + `ConversationStore.load_recent` (turn-based)

**`ConversationStore.load_recent(chat_id, limit_turns)` — переписать (phase 1 был row-based):**

```sql
SELECT c.*
FROM conversations c
WHERE c.chat_id = ?
  AND c.turn_id IN (
    SELECT t.turn_id
    FROM turns t
    WHERE t.chat_id = ? AND t.status = 'complete'
    ORDER BY t.completed_at DESC NULLS LAST, t.started_at DESC
    LIMIT ?
  )
ORDER BY c.id ASC
```

Возвращает все rows из последних N **complete** turn'ов в хронологическом порядке. Interrupted turn'ы скипаются целиком (они физически в БД для трассировки, но не подаются в SDK).

Тесты:
- `test_load_recent_turn_boundary` — 3 turn'а × 5 rows; `load_recent(limit=2)` → ровно 10 rows последних двух turn'ов, никаких обрезков.
- `test_interrupted_turn_skipped_in_history` — turn со `status='interrupted'` отсутствует в результате даже если он самый свежий.

**`history.py::history_to_sdk_messages`:**

```python
def history_to_sdk_messages(rows: list[dict[str, Any]]) -> list[dict]:
    """Convert ConversationStore rows (already filtered to complete turns) to SDK messages.

    Skip rows with block_type='thinking' (SDK refuses cross-session thinking).
    Collapse rows with same (turn_id, role) into one message with content list.
    Keep tool_use/tool_result blocks verbatim for continuity.
    Точный формат dict определяется ответом R1 из spike (Task 0).
    """
```

Соседние rows с одинаковым `(turn_id, role)` объединяются — SDK ожидает message-per-turn с списком контент-блоков.

**Примечание:** этот раздел требует доработки `ConversationStore.load_recent`, написанного в phase 1 (row-based вариант). Изменение обязательно входит в changeset phase 2.

### 7a. Миграция 0002 — `turns` + `block_type` + `turn_status`

`src/assistant/store/migrations/0002_turns_block_type.sql`:

```sql
CREATE TABLE IF NOT EXISTS turns (
    turn_id      TEXT PRIMARY KEY,
    chat_id      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | complete | interrupted
    started_at   TEXT NOT NULL,
    completed_at TEXT,
    meta_json    TEXT                              -- model, usage, stop_reason, cost_usd
);
CREATE INDEX IF NOT EXISTS idx_turns_chat_status ON turns(chat_id, status, completed_at);

ALTER TABLE conversations ADD COLUMN block_type TEXT;
-- block_type ∈ {text, tool_use, tool_result, thinking}; NULL — для legacy rows phase 1

-- Backfill: каждый существующий turn_id из phase 1 → синтетический row в `turns(status='complete')`
INSERT OR IGNORE INTO turns (turn_id, chat_id, status, started_at, completed_at)
SELECT turn_id, chat_id, 'complete', MIN(created_at), MAX(created_at)
FROM conversations
GROUP BY turn_id;

-- Backfill block_type: единственный известный пред-знак — role='user' → 'text'; остальное NULL (legacy).
UPDATE conversations SET block_type = 'text' WHERE block_type IS NULL AND role = 'user';
```

FK `conversations.turn_id REFERENCES turns(turn_id) ON DELETE CASCADE` — добавить через recreate-table если SQLite не поддерживает ALTER ADD CONSTRAINT (стандартный pattern: `CREATE TABLE conversations_new ... ; INSERT INTO conversations_new SELECT ... ; DROP TABLE conversations; ALTER TABLE conversations_new RENAME TO conversations`).

`meta_json` теперь живёт на `turns` (apply при ResultMessage); со строк `conversations` — снять. Если в phase 1 были non-NULL значения — переезжают backfill'ом в `turns.meta_json` (берём `meta_json` из первой `role='user'` строки turn'а; если там пусто — NULL).

**Закрывает техдолг #4** (нормализация схемы) раньше срока — в phase 4 одной задачей меньше.

`ConversationStore` API дополнения:
- `start_turn(chat_id) -> turn_id` — INSERT в `turns` со `status='pending'`, `started_at=now()`.
- `complete_turn(turn_id, meta: dict)` — `UPDATE turns SET status='complete', completed_at=now(), meta_json=?`.
- `interrupt_turn(turn_id)` — `UPDATE turns SET status='interrupted', completed_at=now()`.
- `append(chat_id, turn_id, role, blocks, *, block_type)` — `block_type` теперь обязательный kwarg.

### 8. `src/assistant/bridge/claude.py`

```python
class ClaudeBridge:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.claude.max_concurrent)

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:  # yields SDK blocks
        system = self._render_system_prompt()
        options = ClaudeAgentOptions(
            system_prompt=system,
            cwd=str(self._settings.project_root),
            setting_sources=["project"],
            max_turns=self._settings.claude.max_turns,
            can_use_tool=_make_path_guard(self._settings.project_root),
        )
        async with self._sem:
            sdk_messages = history_to_sdk_messages(history)
            prompt_iter = _build_prompt_iter(sdk_messages, user_text)
            async with asyncio.timeout(self._settings.claude.timeout):
                async for message in query(prompt=prompt_iter, options=options):
                    for block in getattr(message, "content", []) or []:
                        yield block
                    # ResultMessage terminates
```

**Permission/hooks layer (точная форма зависит от R5 spike):**
- `_make_path_guard(project_root)` — guard для `Read/Write/Edit/Glob/Grep`: разрешает только пути внутри `project_root`. Остальные tool'ы пропускает.
- `_make_bash_pre_hook()` — Bash pre-hook: матчит regex `\b(\.env|\.ssh|secrets|\.db\b|token|password)\b` (case-insensitive) против command-string. При совпадении — early deny с пояснением "Reading secrets/credentials/DB files via Bash is blocked. Ask the owner directly if you need a value." Сигнатура коллбэка/hook'а — по результату R5 (`can_use_tool=` vs `hooks={"PreToolUse": [...]}`).
- `WebFetch` — allow.

`_build_prompt_iter`: async generator, который сначала yield'ит каждое историческое сообщение, потом текущий user-message. Точный формат yield'ов — по R1 (см. spike). SDK принимает `AsyncIterable[dict]` когда `can_use_tool` задан (если R5 покажет иное — формат подачи истории пересматриваем в `implementation.md`).

**Ошибки:**
- `asyncio.TimeoutError` → raise `ClaudeBridgeError("timeout")`; handler шлёт "⏱ Claude не ответил за Xс".
- `CLIJSONDecodeError`/`ProcessError` → `ClaudeBridgeError(str(exc))`; handler шлёт "⚠ внутренняя ошибка SDK, детали в логах".
- При partial stream перед timeout — handler уже записал частичные блоки в БД (по мере прихода) и шлёт пользователю то, что успел накопить + маркер ошибки.

### 9. `src/assistant/handlers/message.py`

```python
Emit = Callable[[str], Awaitable[None]]

class ClaudeHandler:
    def __init__(self, settings, conv, bridge): ...

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None:
        turn = await self._conv.start_turn(msg.chat_id)   # turns(status='pending')
        await self._conv.append(msg.chat_id, turn, "user",
                                [{"type":"text","text":msg.text}],
                                block_type="text")
        history = await self._conv.load_recent(
            msg.chat_id, self._settings.claude.history_limit
        )
        # turn только что создан со status='pending', load_recent его не вернёт (фильтр complete)

        final_meta: dict[str, Any] = {}
        completed = False
        try:
            async for block in self._bridge.ask(msg.chat_id, msg.text, history):
                role, payload, text, btype = _classify_block(block)
                await self._conv.append(msg.chat_id, turn, role, [payload],
                                        block_type=btype)
                if text:
                    await emit(text)
                if role == "result":
                    final_meta = payload
            await self._conv.complete_turn(turn, meta=final_meta)
            completed = True
        except ClaudeBridgeError as e:
            await emit(f"\n\n⚠ {e}")
        finally:
            if not completed:
                await self._conv.interrupt_turn(turn)
```

`_classify_block` возвращает `(role, block_dict, text_to_emit_or_None, block_type)`:
- `TextBlock` → `("assistant", {...}, block.text, "text")`
- `ToolUseBlock` → `("assistant", {...}, None, "tool_use")` — tool_use исходит от ассистента
- `ToolResultBlock` → `("tool", {...}, None, "tool_result")`
- `ThinkingBlock` → `("assistant", {...}, None, "thinking")`
- `ResultMessage` (пришёл как message, не block — отдельно) → `("result", {usage, model, stop_reason, cost_usd}, None, None)` — НЕ пишется в `conversations`, идёт в `complete_turn(meta=...)`.

`role` ∈ {`user`, `assistant`, `tool`} по новой схеме (S6); тип блока живёт в `block_type`.

### 10. `src/assistant/adapters/base.py` + `telegram.py`

`base.py`:
```python
Emit = Callable[[str], Awaitable[None]]

class Handler(Protocol):
    async def handle(self, msg: IncomingMessage, emit: Emit) -> None: ...
```

**Rationale (Q1 уточнение):** `AsyncIterator[Block]` остаётся на стороне `ClaudeBridge` (для потокового логирования и ранней остановки). Но `Handler.handle` принимает `emit`-callback вместо возврата `AsyncIterator[str]` — это два разных слоя. Phase 5 scheduler передаёт свой `emit`, который через `TelegramAdapter.send_text` кладёт результат в чат owner'а. Адаптер — реализует свой `emit` (буфер + finalize).

`telegram.py::_on_text`:
```python
async with ChatActionSender.typing(bot=self._bot, chat_id=message.chat.id):
    chunks: list[str] = []

    async def emit(text: str) -> None:
        chunks.append(text)

    await self._handler.handle(incoming, emit)
    full = "".join(chunks).strip() or "(пустой ответ)"
    for part in _split_for_telegram(full, limit=4096):
        await self._bot.send_message(message.chat.id, part)
```

`_split_for_telegram`: режет по `\n\n`, иначе по `\n`, иначе по 4096 hard.

`DefaultBotProperties(parse_mode=None)` — убираем HTML.

### 11. `skills/ping/SKILL.md`

```markdown
---
name: ping
description: Healthcheck skill. Runs the ping CLI which prints {"pong": true}. Use when the user says "use the ping skill" or asks to verify skill discovery.
allowed-tools: [Bash]
---

# ping

Run `python tools/ping/main.py` via Bash. The tool prints a single JSON line
`{"pong": true}`. Report the parsed value back to the user.
```

### 12. `tools/ping/main.py`

```python
import json, sys
sys.stdout.write(json.dumps({"pong": True}) + "\n")
```

### 13. Тесты

- `test_skills_manifest.py`: создаёт tmp `skills/foo/SKILL.md` с frontmatter → `build_manifest` содержит `foo`.
- `test_skills_manifest_cache.py`: `build_manifest` дважды с одним содержимым → один stat-проход (mock на `Path.stat`); добавление нового `skills/bar/SKILL.md` (с touch чтобы поднять mtime директории) → следующий вызов вернёт обновлённый manifest.
- `test_bootstrap.py`: вызывает `ensure_skills_symlink` дважды → линк валиден, readlink == `../skills`.
- `test_bridge_mock.py`: monkeypatch `claude_agent_sdk.query` на async-gen yielding fake messages; вызывает `ClaudeBridge.ask` с мок-историей → проверяет, что в `prompt_iter` попали все rows + current text; что text-блок вышел наружу.
- `test_load_recent_turn_boundary.py`: 3 turn'а × 5 rows (все complete); `load_recent(chat_id, limit=2)` → ровно 10 rows последних двух turn'ов в порядке возрастания id; ничего из 3-го (старшего) turn'а нет, ни одна строка в выборке не обрезана.
- `test_interrupted_turn_skipped.py`: 2 complete-turn'а + 1 interrupted (свежайший) → `load_recent(limit=10)` возвращает только rows двух complete-turn'ов; interrupted физически в БД (проверяем прямым SELECT) но не в выдаче.

## Критерии готовности

1. **Spike-артефакт существует** — `plan/phase2/spike-findings.md` или `spikes/sdk_probe.py` с зафиксированными ответами R1–R5 и pinned-версией SDK.
2. `just lint` + `just test` зелёные (3 старых + 6 новых: manifest, manifest-cache, bridge-mock, bootstrap, load_recent-turn-boundary, interrupted-turn-skipped).
3. Ручной smoke: "use the ping skill" → ответ содержит "pong" / `true`; в логе SDK виден tool_use `Bash` с `tools/ping/main.py`.
4. **Ручной smoke безопасности:**
   - Из чата: попросить модель `Read .env` → отказ path-guard'ом (если файл вне `project_root`, его попросту нет; если всё же есть локальный dev `.env` — guard обязан запретить чтение через Read/Edit/Glob/Grep).
   - Из чата: попросить модель выполнить `bash -c 'cat .env'` (или `cat ~/.config/0xone-assistant/.env`, или открыть `*.db`/`.ssh`) → Bash pre-hook делает early deny с пояснительным сообщением.
5. **Secrets/data вне репо:**
   - `~/.config/0xone-assistant/.env` существует и читается; локальный `./.env` отсутствует или используется только в dev-режиме.
   - `~/.local/share/0xone-assistant/assistant.db` существует; в `project_root` нет ни `.env` (prod), ни `data/`.
6. **Миграция 0002 применена:** `SELECT COUNT(*) FROM turns` ≥ числу distinct `turn_id` в `conversations`; все backfill'ed turn'ы имеют `status='complete'`. FK CASCADE проверен (удаление turn → удаляются rows).
7. **`load_recent` turn-based:** второе сообщение через минуту — SDK получает rows последних complete-turn'ов (проверить по логу "prompt length" / dump). Interrupted turn (симулировать прерыванием через KeyboardInterrupt в середине ask) → не появляется в следующей истории.
8. Сообщение вызывающее длинный ответ (>4096 символов) — разбит на 2+ Telegram-сообщения, ничего не потеряно.
9. Перезапуск бота между сообщениями — контекст восстановлен.
10. Отсутствие токена CLAUDE/SDK → процесс не падает молча, логирует понятную ошибку.

## Явные не-цели

- Не делаем живой `edit_message_text` стриминг.
- Не переходим на MarkdownV2/HTML.
- Не реализуем `tools/memory` (только упоминание в prompt).
- Не пишем skill-creator/installer.
- **Не делаем backfill синтетическим `tool_result {is_error:true, content:"interrupted"}`** для orphan `tool_use` после прерванного turn'а — в phase 2 достаточно скипа всего interrupted turn'а из истории. Расширенная семантика recovery — future-work.
- (Техдолг #4 — нормализация — теперь **закрыт** в phase 2 миграцией 0002.)

## Риски

| Риск | Вероятность | Митигация |
|---|---|---|
| `setting_sources=["project"]` не подхватывает `.claude/skills/` или сигнатура `can_use_tool`/`hooks` отличается | Средняя | **Task 0 spike (R1–R5) выполнен ДО написания `bridge/*.py`** — иначе researcher/coder не стартуют. Артефакт обязателен. Fallback — инжект описаний в system prompt (manifest готов). |
| Synthesis secrets через Bash (модель читает `.env`/`*.db`/`.ssh`/`token`/`password`) | Средняя | (1) `.env` физически в `~/.config/0xone-assistant/`, `data/` в `~/.local/share/0xone-assistant/` — вне `project_root`/`cwd` SDK; (2) Bash pre-hook regex `\b(\.env|\.ssh|secrets|\.db\b|token|password)\b` с early deny; (3) ручной smoke в критериях готовности. |
| SDK API меняется в новой версии | Средняя | Запинить `claude-agent-sdk` в момент реализации; verified-сигнатуры из spike. |
| Сохранение `tool_use`/`tool_result` блоков ломает SDK при репродусе истории | Низкая | `turns.status='interrupted'` → весь turn скипается; complete turn'ы консистентны (есть и tool_use и tool_result). Fallback — фильтр tool-блоков feature-flag'ом. |
| Plain-text ответы теряют форматирование списков/кода | Низкая | Принято осознанно до phase 3. |
| Semaphore-1 блокирует параллельные scheduler-триггеры (появятся в phase 5) | Низкая | **Закрыто превентивно** — `claude_max_concurrent=2` уже в phase 2 (см. строку 20 сводки). |
| Манифест-кэш отдаёт устаревшее значение после установки нового скила | Низкая | mtime-проверка `skills_dir` + рекурсивно по `*/SKILL.md`; phase-3 installer пишет atomic rename, mtime директории меняется. Тест на инвалидацию. |
| Симлинк на Windows | N/A | Проект только под macOS/Linux (single-user). |
