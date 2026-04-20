# Phase 2 — Detailed Plan

## Подтверждённые решения (обсуждение закрыто)

Все вопросы закрыты в пользу Recommended-варианта в интерактивном обсуждении с пользователем.

| # | Вопрос | Recommended | Альтернативы |
|---|---|---|---|
| Q1 | Контракт `ClaudeBridge.ask` | **`AsyncIterator[Block]`** — стрим блоков; handler агрегирует и сам решает, что слать в БД и пользователю | (a) `list[Block]` batch — проще, но теряем инкрементальные логи и исключает будущий live-edit; (b) `AsyncIterator[str]` — выбрасывает `ToolUseBlock` из чата, но handler теряет возможность писать tool-use в БД как-есть |
| Q2 | Telegram delivery стратегия | **Буферизуем полностью, шлём одним `send_message` в конце (split если >4096)**, typing-ping всё время | (a) edit_message_text каждые N tokens — живой UX, но 429/flood risk и усложняет split; (b) стримить по абзацам (новое сообщение на каждый параграф) — шумно |
| Q3 | Parse mode | **None (plain text)** — изменение с phase 1 (`HTML`); Claude любит markdown, но без валидации выдаёт невалидный Markdown; безопаснее plain | (a) MarkdownV2 с агрессивным escape; (b) HTML с конверсией md→html |
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
| 16 | History budget | `load_recent(limit=CLAUDE_HISTORY_LIMIT, default=20)` — row-based limit: последние N rows из complete turn'ов. Фильтр `turns.status='complete'` (interrupted turn'ы скипаются целиком). Token-budget deferred to phase 4+ when history volume justifies token-counter implementation complexity. |
| 17 | Тех долг | Закрываем #1 (nested Settings), #2 (DI), #3 (streaming contract = emit-callback), #4 (нормализация — таблица `turns`), #6 (parse_mode решение). Долг #5 — остаётся. |
| 18 | Тесты | +6 юнитов: manifest parser, manifest cache invalidation, bridge (mock query), bootstrap symlink, load_recent-respects-limit-and-skips-interrupted (row-limit), interrupted-turn-skipped |
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
| B1 | `history_limit=40 rows` режет ассистент-turn посередине, SDK получает orphan blocks | Row-limit=20 (configurable через `CLAUDE_HISTORY_LIMIT`), но фильтр `turns.status='complete'` гарантирует что interrupted turn'ы не попадают в историю. Оставшийся риск обрезки consistent-turn'а посередине признаётся приемлемым для phase 2; token-budget (cut по turn-границе) deferred to phase 4+ когда volume истории это оправдает — в phase 2 нет token-counter API без нарушения OAuth-only invariant. |
| B2 | Прерванный turn оставляет orphan `tool_use` без `tool_result` → SDK ругается | Колонка `turns.status` (`pending`/`complete`/`interrupted`); `try/finally` в handler; история фильтрует `status='complete'`. Backfill синтетическим `tool_result` — future-work |
| S1 | `Handler.handle → AsyncIterator[str]` плохо ложится на phase 5 scheduler (нет owner-chat в контексте) | Заменить на `emit`-callback; адаптер реализует свой emit (буфер + finalize), scheduler — свой (push в чат owner'а) |
| S6 | Денормализованная схема (`meta_json` на каждом row, нет первичного носителя статуса turn'а) | Миграция 0002: `CREATE TABLE turns(...)`, `ALTER conversations ADD COLUMN block_type`, FK CASCADE; meta переезжает на `turns`. Закрывает техдолг #4 в phase 2 |
| S2 | `claude_max_concurrent=1` создаст deadlock когда phase 5 scheduler начнёт триггерить фоновые turn'ы параллельно с пользовательскими | Поднять до `2` — 0 строк кода, snimaет риск заранее |
| S3 | Manifest пересобирается на каждый запрос → лишний disk IO (мелочь, но уже видно в hot path) | mtime-кэш с составным ключом `max(skills_dir.stat().st_mtime, *SKILL.md.st_mtime)` (Q9): модуль-уровневый dict, инвалидируется при touch самого `skills_dir` ИЛИ любого `SKILL.md` — atomic rename из skill-installer это покрывает (rename меняет mtime родителя). Rebuild на любом mismatch ключа. |

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
│   ├── state/
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
    ├── test_load_recent_respects_limit_and_skips_interrupted.py   # NEW (row-limit)
    └── test_interrupted_turn_skipped.py                             # NEW
```

`.gitignore` дополнительно: `.claude/skills` (симлинк создаётся программно в `Daemon.start()`, пересоздаётся при каждом запуске), `.claude/settings.json`, `.claude/settings.local.json` (локальные SDK settings не должны коммититься — см. §Security / `assert_no_custom_claude_settings` ниже; они могут переопределять hooks и env vars текущей `claude` CLI session). `tools/ping/` ставится upfront как **plain stdlib** — без `tools/ping/pyproject.toml`, без своего venv: запускается как `python tools/ping/main.py` (Q10). Запись `.env` остаётся как legacy (реальный `.env` теперь снаружи репо — задокументировать в README).

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

### 0a. Миграция данных с phase 1 (owner runs ONCE before first phase-2 start)

Phase 1 держал `.env` и `data/assistant.db` **внутри** репо. Phase 2 переносит оба вне `project_root` (`cwd` SDK — см. S5). Миграция — ручная, idempotent:

```bash
mkdir -p ~/.config/0xone-assistant ~/.local/share/0xone-assistant
# .env: перенести если существует (phase 1 local dev)
[ -f .env ] && mv .env ~/.config/0xone-assistant/.env || true
# SQLite: перенести вместе с -wal/-shm (WAL mode)
for f in data/assistant.db data/assistant.db-wal data/assistant.db-shm; do
    [ -f "$f" ] && mv "$f" ~/.local/share/0xone-assistant/ || true
done
rmdir data 2>/dev/null || true
```

Опционально эти команды живут в `scripts/migrate-phase1-to-phase2.sh` (owner запускает вручную, не авто-trigger). README.md добавляет секцию **"Upgrading from phase 1"** с этим скриптом и проверкой `ls ~/.local/share/0xone-assistant/assistant.db`.

После миграции `Daemon.start()` делает `data_dir.mkdir(parents=True, exist_ok=True)` (создаёт каталог при первом запуске на чистой машине); `.env`-parent не трогаем — его создаёт либо owner вручную, либо миграционный скрипт.

### 1. `pyproject.toml`

Добавить в `dependencies`:
```
"claude-agent-sdk>=0.1.59,<0.2",   # spike-verified на 0.1.59
"pyyaml>=6.0",
```
Поднять `aiogram` до реального shipped-pin: `"aiogram>=3.26,<4"` (phase 1 закрепила 3.26; плохая идея понижать обратно на 3.13).
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
    max_concurrent: int = 2              # bumped: см. строку 20 в сводке решений
    history_limit: int = 20              # row-based history cap; configurable via CLAUDE_HISTORY_LIMIT.
                                         # Phase 2 uses row-based limit. Token-budget deferred to phase 4+
                                         # when history volume justifies token-counter implementation complexity.
    thinking_budget: int = 0             # phase 2: thinking OFF by default (cost + U2 replay risk);
                                         # >0 → ClaudeAgentOptions(max_thinking_tokens=N, effort="high")

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

`.env.example` добавляет `CLAUDE_TIMEOUT`, `CLAUDE_MAX_TURNS`, `CLAUDE_MAX_CONCURRENT`, `CLAUDE_HISTORY_LIMIT=20`, `CLAUDE_THINKING_BUDGET`. Phase 2 uses row-based limit. Token-budget deferred to phase 4+ when history volume justifies token-counter implementation complexity (требует native SDK counter или API call — в phase 2 нельзя без нарушения OAuth-only invariant).

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


def assert_no_custom_claude_settings(project_root: Path, logger: logging.Logger) -> None:
    """Verify no unexpected .claude/settings*.json files under project_root.

    SDK session (`setting_sources=["project"]`) merges `.claude/settings.json` and
    `.claude/settings.local.json` at startup; they can override our hooks, permissions
    and env vars. Repo does not commit them (see .gitignore), but a stray local file
    would silently change SDK behaviour. Log a warning and (INFO) dump redacted content
    so the owner knows what the SDK will pick up. Do NOT fail start — only warn.
    """
    for name in ("settings.json", "settings.local.json"):
        path = project_root / ".claude" / name
        if not path.exists():
            continue
        logger.warning(
            ".claude/%s present — SDK may override hooks/env from this file. "
            "Review or delete unless intentionally customised.",
            name,
        )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            redacted = _redact_sensitive(raw)  # strip `env`, `apiKey`, `*token*`, `*secret*`
            logger.info(".claude/%s content (redacted): %s", name, redacted)
        except Exception as exc:  # noqa: BLE001 — diagnostic logging only
            logger.warning("Failed to parse .claude/%s: %s", name, exc)
```

Оба вызываются из `Daemon.start()` до первого `query()`: сначала `assert_no_custom_claude_settings(project_root, logger)` (warning-only, не блокирует старт), потом `ensure_skills_symlink(project_root)`, потом запуск адаптера.

**Rationale:** `.claude/settings.json` / `.claude/settings.local.json` — это механизм Claude Code CLI для локальных hooks/permissions/env vars. `setting_sources=["project"]` заставит SDK их подхватить и merge'нуть в сессию. Если owner случайно положил туда что-то (например, permissive permission rule), это silently обойдёт наши pre-tool hooks. Мы не блокируем старт (owner может иметь легитимный reason), но логируем warning + dump redacted content, чтобы owner мог это увидеть и осознанно решить — удалить, проревьюить, или оставить.

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

### 7. `src/assistant/bridge/history.py` + `ConversationStore.load_recent` (row-limit, turn-filtered)

**`ConversationStore.load_recent(chat_id, limit: int)` — переписать (phase 1 был row-based без фильтра по turn-status):**

Стратегия: **row-limit** с фильтром на complete turn'ы. Phase 2 uses row-based limit. Token-budget deferred to phase 4+ when history volume justifies token-counter implementation complexity — dedicated token counter требует либо HTTP call через `anthropic.count_tokens` либо native SDK API, и phase 2 не может это реализовать без нарушения OAuth-only invariant.

```python
async def load_recent(self, chat_id: int, limit: int) -> list[dict[str, Any]]:
    # SELECT c.*
    # FROM conversations c
    # JOIN turns t ON c.turn_id = t.turn_id
    # WHERE c.chat_id = ? AND t.status = 'complete'
    # ORDER BY t.id DESC, c.id DESC
    # LIMIT ?
    #
    # Результат разворачиваем в хронологический порядок (ASC по id) перед возвратом.
```

**Invariants:**
- Skip `turn_status != 'complete'` — interrupted turn'ы не попадают в историю (фильтр на уровне SQL JOIN).
- Row-limit может обрезать consistent-turn посередине; риск признан приемлемым для phase 2. Token-budget (cut по turn-границе) deferred to phase 4+.

Тесты:
- `test_load_recent_respects_limit_and_skips_interrupted` — 3 complete-turn'а × несколько rows плюс 1 interrupted turn (самый свежий); `load_recent(limit=N)` возвращает ровно N последних rows **только** из complete-turn'ов, в хронологическом порядке (ASC по id); ни одна строка interrupted-turn'а не попадает в выдачу.
- `test_interrupted_turn_skipped_in_history` — turn со `status='interrupted'` отсутствует в результате даже если он самый свежий (дополнительный фокус-тест на инварианте).

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

**Когда:** сейчас, в phase 2 (Q13). Phase 4 уже ничего не мигрирует в этой части — техдолг #4 закрывается здесь.
**Идемпотентность:** файл применяется только если `PRAGMA user_version < 2`; в конце миграция выставляет `PRAGMA user_version=2`. Повторный запуск на mig'рированной БД — no-op.

`src/assistant/state/migrations/0002_turns_block_type.sql`:

```sql
-- fully-idempotent: защищается CREATE/ALTER IF NOT EXISTS + проверкой user_version в раннере
CREATE TABLE IF NOT EXISTS turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | complete | interrupted
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    meta_json    TEXT                              -- model, usage, stop_reason, cost_usd
);
CREATE INDEX IF NOT EXISTS idx_turns_chat_status ON turns(chat_id, status, completed_at);

-- block_type: добавляется только если колонки ещё нет (раннер обязан проверять
-- PRAGMA table_info(conversations) перед ALTER — SQLite не умеет ALTER ... IF NOT EXISTS).
ALTER TABLE conversations ADD COLUMN block_type TEXT;
-- block_type ∈ {text, tool_use, tool_result, thinking}; NULL — для legacy rows phase 1.

-- Backfill turns из существующих conversations (phase 1 rows):
-- ровно строка Q13 — INSERT OR IGNORE, группировка по (chat_id, turn_id),
-- status='complete' для всех legacy turn'ов (они завершились, раз бот ответил).
INSERT OR IGNORE INTO turns (chat_id, turn_id, status, created_at, completed_at)
SELECT chat_id, turn_id, 'complete', MIN(created_at), MAX(created_at)
FROM conversations
GROUP BY chat_id, turn_id;

-- Backfill block_type: единственный надёжный пред-знак — role='user' → 'text'.
-- Остальное (role='assistant' с неизвестной структурой блоков в phase-1 payload)
-- остаётся NULL — history builder трактует NULL как 'text' через fallback.
UPDATE conversations SET block_type = 'text' WHERE block_type IS NULL AND role = 'user';
```

FK `conversations.turn_id REFERENCES turns(turn_id) ON DELETE CASCADE` — добавить через recreate-table если SQLite не поддерживает ALTER ADD CONSTRAINT (стандартный pattern: `CREATE TABLE conversations_new ... ; INSERT INTO conversations_new SELECT ... ; DROP TABLE conversations; ALTER TABLE conversations_new RENAME TO conversations`). Раннер выполняет recreate-блок только если в исходной схеме FK отсутствует (детект через `PRAGMA foreign_key_list(conversations)`).

`meta_json` теперь живёт на `turns` (apply при ResultMessage); со строк `conversations` — снять. Если в phase 1 были non-NULL значения — переезжают backfill'ом в `turns.meta_json` (берём `meta_json` из первой `role='user'` строки turn'а; если там пусто — NULL).

**Закрывает техдолг #4** (нормализация схемы) раньше срока — в phase 4 одной задачей меньше.

`ConversationStore` API дополнения:
- `start_turn(chat_id) -> turn_id` — INSERT в `turns` со `status='pending'`, `started_at=now()`.
- `complete_turn(turn_id, meta: dict)` — `UPDATE turns SET status='complete', completed_at=now(), meta_json=?`.
- `interrupt_turn(turn_id)` — `UPDATE turns SET status='interrupted', completed_at=now()`.
- `append(chat_id, turn_id, role, blocks, *, block_type)` — `block_type` теперь обязательный kwarg.

### 8. `src/assistant/bridge/claude.py`

**⚠ SPIKE-CORRECTED (R5):** авторитетный код — в `implementation.md` §2.1 (`hooks={"PreToolUse":[HookMatcher(matcher="Bash"), HookMatcher(matcher="Read"), ..., HookMatcher(matcher="WebFetch")]}`, **7 матчеров** explicit: Bash + Read/Write/Edit/Glob/Grep + WebFetch). `can_use_tool=` в spike доказано silent-не-файрит когда `allowed_tools=[...]` задан (Bash был auto-approved, callback не вызывался ни разу) — использование `can_use_tool` = dead guard. Ниже остаётся скелет `ClaudeBridge` для ориентира, но **permission-слой НЕ описывай здесь** — он живёт только в `implementation.md`.

Скелет (структурно, не API-contract):

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
    ) -> AsyncIterator[Any]:  # yields SDK blocks (TextBlock / ToolUseBlock / ...)
        options = self._build_options()   # см. implementation.md §2.1 — hooks={"PreToolUse":[7 matchers]}
        async with self._sem:
            prompt_iter = history_to_user_envelopes(history, current=user_text)
            async with asyncio.timeout(self._settings.claude.timeout):
                async for message in query(prompt=prompt_iter, options=options):
                    for block in getattr(message, "content", []) or []:
                        yield block
                    # ResultMessage terminates
```

**Permission/hooks layer — описано в `implementation.md`:**
- 7 матчеров (1 Bash + 5 file-tools + 1 WebFetch), `hooks={"PreToolUse": [HookMatcher(...), ...]}`.
- Bash: **allowlist-first prefilter** (high-level решение см. §"Пошаговая реализация / Bash prefilter strategy" ниже).
- Read/Write/Edit/Glob/Grep: path-guard разрешает только пути внутри `project_root` + `tools/`.
- WebFetch: SSRF guard (deny private/link-local/metadata hosts: `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `fc00::/7`, `fe80::/10`).
- Thinking: `max_thinking_tokens` + `effort="high"` только если `settings.claude.thinking_budget > 0` (phase 2 default: OFF).

`history_to_user_envelopes`: async generator, yield'ит исторические сообщения как `{"type":"user","message":{"role":"user","content":...},"session_id":...}` envelopes (R1-verified shape), потом текущий user-text. `ThinkingBlock`-блоки и `tool_use`/`tool_result` не реплеются в raw-виде (см. U1/U2 в `unverified-assumptions.md` + synthetic system-note в `implementation.md`). SDK принимает `AsyncIterable[dict]` безусловно в streaming-input mode (R1).

#### 8a. Bash prefilter strategy — **allowlist-first**

`spike-findings.md §4a` зафиксировал: regex-deny (v1) признан недостаточным — продемонстрированы 4 bypass'а (`env`/`printenv` вместо `cat .env`, octal escape, glob-expansion, base64-decode). Phase 2 использует **allowlist-first** как primary control, regex-deny — как defence-in-depth layer.

**Высокоуровневое решение (детали — researcher в `implementation.md`):**

- **Allowlist prefix** — список safe command-prefix'ов: `ls`, `cat`, `head`, `tail`, `wc`, `echo`, `pwd`, `date`, `which`, `grep`, `rg`, `find`, `git status`, `git diff`, `git log`, `python`, `python3`, `uv`, `sqlite3`, плюс явно `python tools/<name>/main.py` для всех существующих `tools/*`.
- **Аргументы `cat`/`head`/`tail`** должны разрешать путь только внутри `project_root` — если путь не резолвится туда, отказ.
- Всё, что не матчит allowlist, **reject by default** с сообщением `"Bash command not in allowlist. If you need this operation, ask the owner to add it to tools/<name>/ or the allowlist."`
- Defence-in-depth: поверх allowlist прогоняется regex deny `\b(\.env|\.ssh|secrets|\.db\b|token|password)\b` — если в allow-команде попадаются эти токены, тоже отказ.

Researcher в `implementation.md` фиксирует точный набор prefix'ов, парсер команды (shlex / AST / naive startswith) и тестовую матрицу bypass-проверок.

**Ошибки:**
- `asyncio.TimeoutError` → raise `ClaudeBridgeError("timeout")`; handler шлёт "⏱ Claude не ответил за Xс".
- `CLIJSONDecodeError`/`ProcessError` → `ClaudeBridgeError(str(exc))`; handler шлёт "⚠ внутренняя ошибка SDK, детали в логах".
- При partial stream перед timeout — handler уже записал частичные блоки в БД (по мере прихода) и шлёт пользователю то, что успел накопить + маркер ошибки.

### 9. `src/assistant/handlers/message.py`

**Миграция с phase 1:** `EchoHandler` (phase 1 stub) **удаляется полностью**. `src/assistant/handlers/message.py` переписывается с нуля в `ClaudeHandler`. Phase 1 тесты, завязанные на echo (`tests/test_echo_handler.py` и аналогичные echo-смоуки) — переписываются или удаляются; phase 1 `tests/test_db.py` (ConversationStore basics) остаётся и расширяется новыми тестами из §13. Явно: **никаких "EchoHandler как fallback"** — вырезаем под корень, одна точка входа в handler.

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
            msg.chat_id, limit=self._settings.claude.history_limit
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

`DefaultBotProperties(parse_mode=None)` — **изменение с phase 1**, где стоял `parse_mode=ParseMode.HTML`. Phase 2 плейн-текст: Claude часто выдаёт невалидный Markdown, HTML-escape на каждом ответе не оправдан до phase 3+. Coder проверяет что ни в адаптере, ни в handler'е больше нет HTML-тэгов — любые `<b>`/`<i>` из phase 1 удаляются.

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

- **Удаляются phase-1 echo-тесты** (если они были, по образцу `tests/test_echo_handler.py`): `EchoHandler` больше нет → его тесты устаревают. `tests/test_db.py` (ConversationStore CRUD, phase 1) остаётся и **обновляется** под новую схему (block_type колонка, Turn API).
- `test_skills_manifest.py`: создаёт tmp `skills/foo/SKILL.md` с frontmatter → `build_manifest` содержит `foo`.
- `test_skills_manifest_cache.py`: `build_manifest` дважды с одним содержимым → один stat-проход (mock на `Path.stat`); добавление нового `skills/bar/SKILL.md` (с touch чтобы поднять mtime директории) → следующий вызов вернёт обновлённый manifest.
- `test_bootstrap.py`: вызывает `ensure_skills_symlink` дважды → линк валиден, readlink == `../skills`.
- `test_bridge_mock.py`: monkeypatch `claude_agent_sdk.query` на async-gen yielding fake messages; вызывает `ClaudeBridge.ask` с мок-историей → проверяет, что в `prompt_iter` попали все rows + current text; что text-блок вышел наружу.
- `test_load_recent_respects_limit_and_skips_interrupted.py`: 3 complete-turn'а с несколькими rows каждый плюс 1 interrupted turn (свежайший); `load_recent(chat_id, limit=N)` возвращает ровно N последних rows **только** из complete-turn'ов, в хронологическом порядке (ASC по id); ни одна строка interrupted-turn'а не попадает в выдачу.
- `test_interrupted_turn_skipped.py`: 2 complete-turn'а + 1 interrupted (свежайший) → `load_recent(limit=большой)` возвращает только rows двух complete-turn'ов; interrupted физически в БД (проверяем прямым SELECT) но не в выдаче.

## Критерии готовности

1. **Spike-артефакт существует** — `plan/phase2/spike-findings.md` или `spikes/sdk_probe.py` с зафиксированными ответами R1–R5 и pinned-версией SDK (`claude-agent-sdk==0.1.59`).
2. `just lint` + `just test` зелёные (3 старых + 6 новых: manifest, manifest-cache, bridge-mock, bootstrap, load_recent-respects-limit-and-skips-interrupted, interrupted-turn-skipped).
3. **Ping smoke (owner)**: в Telegram `"use the ping skill"` → Claude вызывает `tools/ping/main.py` через Bash → возвращает `{"pong": true}` → бот отвечает "pong" / `true`; в логе SDK видно tool_use `Bash` с `python tools/ping/main.py`.
4. **Ручной smoke безопасности:**
   - Из чата: попросить модель `Read .env` → отказ path-guard'ом (если файл вне `project_root`, его попросту нет; если всё же есть локальный dev `.env` — guard обязан запретить чтение через Read/Edit/Glob/Grep).
   - Из чата: попросить модель выполнить `bash -c 'cat .env'` (или `cat ~/.config/0xone-assistant/.env`, или `env | grep TOKEN`, или открыть `*.db`/`.ssh`) → Bash allowlist-first делает early deny с пояснительным сообщением; ни один из 4 known-bypass'ов (env/printenv, octal, glob, base64-decode) не проходит.
5. **Secrets/data вне репо:**
   - `~/.config/0xone-assistant/.env` существует и читается; локальный `./.env` отсутствует или используется только в dev-режиме.
   - `ls ~/.local/share/0xone-assistant/assistant.db` → файл существует (post-migration check).
   - В `project_root` нет ни `.env` (prod), ни `data/`.
6. **Миграция 0002 применена:**
   - `sqlite3 ~/.local/share/0xone-assistant/assistant.db 'PRAGMA user_version'` → `2`.
   - `SELECT COUNT(*) FROM turns` ≥ числу distinct `turn_id` в `conversations`; все backfill'ed turn'ы имеют `status='complete'`. FK CASCADE проверен (удаление turn → удаляются rows).
7. **`load_recent` row-limit**: второе сообщение через минуту — SDK получает не более `CLAUDE_HISTORY_LIMIT` последних rows из complete-turn'ов (проверить по логу "prompt length" / dump). Interrupted turn (симулировать прерыванием через KeyboardInterrupt в середине ask) → не появляется в следующей истории. Token-budget deferred to phase 4+.
8. Сообщение вызывающее длинный ответ (>4096 символов) — разбит на 2+ Telegram-сообщения, ничего не потеряно.
9. Перезапуск бота между сообщениями — контекст восстановлен.
10. Отсутствие токена CLAUDE/SDK → процесс не падает молча, логирует понятную ошибку.
11. **parse_mode=None**: в Telegram ответ рендерится как plain text (phase 1 был HTML) — сообщение со спецсимволами (`<`, `>`, `&`) не ломает рендер.
12. **EchoHandler удалён**: `grep -r "EchoHandler" src/` — пусто; `grep -r "echo" src/assistant/handlers/` — только в ClaudeHandler, если вообще есть.

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
