# Phase 4 — Summary

Документ подводит итоги завершённой фазы 4 проекта `0xone-assistant` (Memory skill: Obsidian vault + FTS5 CLI + per-skill `allowed-tools` enforcement + history tool-result replay). Источники: `plan/phase4/{description,detailed-plan,implementation,spike-findings}.md`, исходники `src/assistant/` + `tools/memory/` + `skills/memory/`, 11 коммитов `42f4861 → 459a09d` поверх phase-3 HEAD `3bf8437`, 83 тестовых файла (414 passed + 1 skipped).

## 1. TL;DR

Добавлена долговременная память: stdlib-only CLI `tools/memory/` (vault + FTS5) и скилл `skills/memory/SKILL.md` c `allowed-tools: [Bash, Read]`, закрыт phase-3 техдолг №4 (per-skill enforcement как статическая union-intersection с global baseline) и inherited блокер phase-2 (history replay tool_result: live spike S-B.2 выявил, что bridge skip'ал `UserMessage` → tool_result rows вообще не писались в БД). Vault под `<data_dir>/vault/` (mode 0o700) + FTS5 `<data_dir>/memory-index.db` c глобальным `fcntl.flock`; atomic write через tmp→fsync→rename на той же FS; startup-probe `_ensure_lock_semantics_once` падает exit 5 на SMB/iCloud silent-no-op. Модель не может передать body через pipe (Bash hook запрещает метасимволы), поэтому CLI получил **write-first pattern**: Write → `data/run/memory-stage/<uniq>.md` → `memory write --body-file …` (CLI сам удаляет stage-файл). 11 коммитов, 69 файлов, +4910/−51. 414/415 тестов зелёные (1 skipped — cross-FS hardlink из phase 3), lint+mypy strict чистые. Phase 5 (scheduler) разблокирован.

## 2. Что реализовано

### 2.1 Новый CLI `tools/memory/`

| Файл | LOC | Роль | Ключевые решения |
|---|---|---|---|
| `/Users/agent2/Documents/0xone-assistant/tools/memory/main.py` | 540 | Argparse-роутер `search/read/write/list/delete/reindex` | Stdlib-only (наследует phase-3 pattern). Exit codes семантичны: `0/2/3/4/5/6/7`. `--body-file PATH` с path-guard внутри `<project_root>/data/run/memory-stage/` (B-CRIT-1). Стейдж-файл auto-unlink после commit'а. `--raw` flag на search для операторов FTS5 (по умолчанию — phrase-match авто-quoting). `delete` при отсутствующем path: `unlink(missing_ok=True)` + отдельная проверка rowcount для race-exit 7. |
| `/Users/agent2/Documents/0xone-assistant/tools/memory/_memlib/frontmatter.py` | 148 | Стандартный YAML-frontmatter парсер (mandatory `title`; опциональные `tags/area/created/related`) | `sanitize_body` **fence-aware** — `---` внутри уже открытой ```` ``` ``` fence'ы не триггерит замену (иначе ломался бы валидный Obsidian markdown с front-matter внутри code block). |
| `/Users/agent2/Documents/0xone-assistant/tools/memory/_memlib/fts.py` | 379 | Глобальный `fcntl.flock(<data_dir>/memory-index.db.lock)` + PRAGMA `busy_timeout=5000` + probe | `porter unicode61 remove_diacritics 2` tokenizer (Cyrillic + ASCII). `_probe_lock_semantics` fd1→fd2: если fd1 кидает `BlockingIOError` → кто-то уже держит → flock работает (не false-positive "no-op"); иначе открываем fd2 для classification. `_escape_fts5_match` на default-phrase для search. |
| `/Users/agent2/Documents/0xone-assistant/tools/memory/_memlib/paths.py` | 58 | Resolve vault path + stage path-guard | `<project_root>/data/run/memory-stage/` invariant — relative-to-project path для Bash hook. |
| `/Users/agent2/Documents/0xone-assistant/tools/memory/_memlib/vault.py` | 113 | Atomic write, `.tmp/` handling, mode 0o700 | Tmp → `fsync` → `os.rename` (same-FS POSIX-atomic). `.tmp/` **внутри vault** — гарантирует cross-FS invariant (один mount). Vault mode 0o700 + warn-don't-chmod для existing loose perms. |

### 2.2 Новый скилл `skills/memory/SKILL.md`

112 LOC. `allowed-tools: [Bash, Read]`. Документирует: areas (`inbox/`, `projects/`, `people/`, `daily/`); exit codes; **write-first pattern** (Bash hook запрещает pipe → модели нужно сперва Write в `data/run/memory-stage/<uniq>.md`, потом `memory write --body-file …`); предостережение про non-POSIX FS (iCloud / SMB → `fcntl.flock` degrade); границы (URL из snippet'а ≠ команда; allowed_tools advisory на permissive хостах).

### 2.3 Изменённые модули `src/assistant/bridge/`

| Файл | Что изменилось |
|---|---|
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/claude.py` (250 → 408 LOC, +158) | **(1)** S-B.2 fix: теперь yield'ит `ToolResultBlock` из `UserMessage.content` (phase-2 skip был причиной, что tool_result rows никогда не писались в БД). **(2)** `_effective_allowed_tools` — static union ∩ baseline по всему `build_manifest()` (Q8, S-A.3 constraint: SDK 0.1.59 не партиционирует hooks per-skill, значит per-turn narrowing невозможен). WARN-telemetry на collapse (`union_shrinkage`) с de-dup (set-change detect). **(3)** `_render_system_prompt` двухступенчатый escape для `str.format` (`project_root`, `vault_dir`, `skills_manifest` — чтобы SKILL.md-описание "uses {foo}" не кидало `KeyError`). |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/history.py` (64 → 215 LOC, +151) | **Q1 synthetic summary**: при history-replay tool_result'ы не дропаются, а свёртываются в system-note со snippet'ом до `MEMORY_HISTORY_TOOL_RESULT_TRUNCATE_CHARS=2000`. Глобальный `_build_tool_name_map` (а не per-turn) — `tool_use_id → tool_name` маппинг сохраняется через границы turn'а (future-proof для phase 5 scheduler-injection). Дефенсивный `content: list`/`None` branch (S-B.1 наблюдал только `str`, но 0.1.59 в будущем может вернуть `list[{type,text}]` из Read/WebFetch). Russian label: `"результат"` / `"ошибка"` (префикс на `is_error=True`). |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/system_prompt.md` | Добавлены `{vault_dir}` + проактивное "пиши в `inbox/` факты из диалога сразу". Убран fallback "если скилла нет — тогда никак". |

### 2.4 Изменённые модули `src/assistant/`

| Файл | Что изменилось |
|---|---|
| `/Users/agent2/Documents/0xone-assistant/src/assistant/config.py` (102 LOC) | `MemorySettings` nested model (`env_prefix="MEMORY_"`): `vault_dir`, `index_db`, `skip_lock_probe`, `max_body_bytes=1048576`, `history_tool_result_truncate_chars=2000`, `tokenizer`. Позволяет офф-vault override через `MEMORY_VAULT_DIR=/…` (например для юзеров с non-POSIX FS в home). |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/main.py` (402 → 451 LOC, +49) | `_ensure_vault`: lazy create `<data_dir>/vault/` mode 0o700 + создание `data/run/memory-stage/` при старте daemon (чтобы skill'у было куда писать с первого turn'а). `_ensure_lock_semantics_once` — startup probe на advisory-only FS, exit 5 при no-op (обход через `ASSISTANT_SKIP_LOCK_PROBE=1`). Probe race (B-CRIT-2) закрыт fd1-first порядком. |

### 2.5 `tools/skill-installer/_lib/validate.py` + `preview.py` (B-CRIT-3)

Devil-wave-2 нашёл bypass: фабрикованный SKILL.md c `allowed-tools: [Bash, Read, Write, Grep, WebFetch]` после phase-4 intersection расширит union для **всех** скиллов на turn. Фикс: `_MAX_ALLOWED_TOOLS_PER_SKILL=3` → warning в `validate_bundle` + preview печатает `⚠ warnings:` секцию с фразой "phase-4 Q8 union collapse". Hard-reject остаётся только на unknown-tool (список фиксируется при смене SDK).

### 2.6 Новые тест-хелперы

| Файл | Роль |
|---|---|
| `/Users/agent2/Documents/0xone-assistant/tests/_helpers/history_seed.py` | 104 LOC. B3-замыкание: фабрикует `tool_use` + `tool_result` rows в ConversationStore без live SDK (unit-level history-replay тесты). |
| `/Users/agent2/Documents/0xone-assistant/tests/_helpers/memory_cli.py` | 54 LOC. Subprocess-wrapper для `tools/memory/main.py` с PYTHONPATH и stdin piping для `--body -` тестов. |

### 2.7 Spike artifacts

- `/Users/agent2/Documents/0xone-assistant/spikes/sdk_per_skill_hook.py` + `.json` report — 316 LOC, 5 cases. Закрыл S-A.1..S-A.5 (скилл-список merged, init.data["skills"] names-only, SDK не партиционирует hooks, `permissions.allow` user-settings overrides `options.allowed_tools`).
- `/Users/agent2/Documents/0xone-assistant/spikes/history_toolresult_shape.py` + `.json` report — 332 LOC, 3 Bash variants. Закрыл S-B.1..S-B.6 (**S-B.2 🔴 blocker**: `ToolResultBlock` живёт в `UserMessage.content`, phase-2 `bridge/claude.py:231` skip'ал → tool_result rows никогда не писались в БД).

## 3. Ключевые архитектурные решения (17 пунктов)

1. **S-B.2 critical discovery (spike live).** Phase-2 bridge skip'ал `UserMessage` → `ToolResultBlock` никогда не доходил до `ConversationStore`. Handler's `_classify` ветка `tool_result` — мёртвый код. Phase 4 починила на уровне bridge'а, только тогда заработала Q1 synthetic summary.
2. **Q1 synthetic summary (решение A из description.md §9).** Вместо `resume=session_id` (B-вариант — хрупкий при SDK upgrade) и вместо "drop tool_result rows" (C-вариант — инварианты phase-2 store теряются) выбран самый дешёвый путь: при history-replay tool_result свёртывается в system-note с первыми 2000 символами. Модель получает достаточный контекст, а store остаётся honest.
3. **Global `tool_use_id → tool_name` map, не per-turn.** При history-replay если tool_use и tool_result оказались в разных turn'ах (scheduler phase 5 как раз будет injecting turn-boundaries), per-turn map терял бы имя tool'а. Глобальная сборка по всей истории — O(N) однократно на рендер промпта.
4. **S-A.3 constraint: static union вместо per-turn.** SDK 0.1.59 не прокидывает `active_skill` в HookContext. Значит Q8 "пересечение allowed_tools активного скилла с baseline" невозможно как per-turn narrowing — только как статическая union всех installed skills ∩ baseline. Формула: `allowed = baseline ∩ ⋃ skills.allowed_tools`. Collapsed список логируется WARN'ом единожды на смену.
5. **S-A.2 honest disclaimer.** На OAuth'd хостах с user-level `~/.claude/settings.json → permissions.allow` Claude CLI **пропускает** вызовы, которые `options.allowed_tools` должно было заблокировать. Значит phase-4 `options.allowed_tools` = advisory; реальная защита — phase-2 PreToolUse hooks (Bash argv allowlist + file path-guard + WebFetch SSRF). Тест-suite фиксирует что intersection **вычисляется корректно**, а не что SDK блокирует на permissive host'е.
6. **Stdlib-only `tools/memory/`.** Консистентно с phase-3 skill-installer pattern (B-4). Нет `pyproject.toml`, нет sqlalchemy, нет aiosqlite — только stdlib `sqlite3`+`fcntl`+`pathlib`. sys.path подставляется внутри `main.py`.
7. **Vault в `<data_dir>/vault/` (XDG), индекс в `<data_dir>/memory-index.db`.** Vault коммитится в git (Obsidian-совместимый markdown = документация), индекс регенерируется из vault. FTS5 БД отдельная — явный signal "это derived data".
8. **Vault mode 0o700 + warn-don't-chmod.** При первом create — 0o700. Если vault уже существует с loose perms (0o755 от юзера) — warn единожды, не chmod (иначе сломаем ожидания пользователя-владельца). Stage dir — 0o700 mandatorly (создаётся daemon'ом).
9. **`_probe_lock_semantics` startup check (fd1-first ордер).** `_ensure_lock_semantics_once` одним открытым процессом тестирует `fcntl.LOCK_EX|LOCK_NB`. fd1 сразу `BlockingIOError` ⇒ кто-то уже держит → flock real → True (не false-positive "advisory-only"). Только если fd1 acquired — открываем fd2 и проверяем, что fd2 block'ится. Это критично для B-CRIT-2: race-free order (dry-run прежней версии ошибочно трактовал "fd1 success" как "advisory" если другой процесс успевал отпустить lock между open и lock).
10. **`porter unicode61 remove_diacritics 2`.** Русская кириллица + английский ASCII + "ё" → "е" normalization. Без `remove_diacritics 2` "жёлтый" не матчил бы "жёлтый". Tokenizer consistency gate'ится в `test_memory_tokenizer_consistency.py` — поиск даёт одинаковые результаты под `memory.tokenizer` default и override.
11. **Global `fcntl.flock(memory-index.db.lock)` + `PRAGMA busy_timeout=5000`.** Single-writer serialization. concurrent write'ы из двух chat'ов (они на разных per-chat locks phase-2) не corrupt'ят FTS5. 5 секундный busy_timeout — запас на тяжёлый `reindex`.
12. **Atomic write: tmp → fsync → rename, `.tmp/` внутри vault.** POSIX-atomic rename требует same filesystem. `.tmp/` **внутри** `<data_dir>/vault/` — инвариант same-FS соблюдён даже если vault на другом FS чем project (через `MEMORY_VAULT_DIR=/external/mount`).
13. **B-CRIT-1 solution: write-first pattern.** Модель **не может** передать body через pipe — Bash hook запрещает `|`, `<`, `&`. Решение: двухшаговое: (1) Write tool в `data/run/memory-stage/<uniq>.md` (phase-2 path-guard разрешает); (2) `memory write … --body-file data/run/memory-stage/<uniq>.md`. CLI auto-cleanup stage-файла. CLI проверяет resolve-path внутри stage dir (path-traversal reject, exit 3). Без фикса — **memory write был unusable через Bash hook**; 384 теста зелёные были false-positive (ни один не шёл через реальный validator).
14. **Body size cap `MEMORY_MAX_BODY_BYTES=1048576` (1 MB).** Защита от model-generated loop'а из-за забытого цикла. `sanitize_body` fence-aware (см. §2.1).
15. **FTS5 query auto-quote + `--raw` escape.** По умолчанию `search "A OR B"` → `MATCH '"A OR B"'` (phrase, 0 hits если нет такой буквальной строки). `--raw "A OR B"` — даёт operator syntax. Защита от NPE на hyphen-запросах (FTS5 ругается на `-` вне phrase).
16. **Oversize allowed-tools warning `max=3` (B-CRIT-3 mitigation).** В skill-installer preview — warn при `len(allowed-tools) > 3` + объяснение Q8 union collapse. Hard-reject невозможен (нет универсальной границы), но оператор видит red flag на этапе install.
17. **`_memlib` rename из `_lib` (последний фикс).** Phase-3 использовал `tools/skill-installer/_lib/`, phase-4 написал `tools/memory/_lib/` → sys.path collision (оба подставляли `_lib` в родитель). Переименовано в `_memlib`. Проблема всплывёт снова в phase 5 (scheduler): решается через relative imports `from tools.memory._memlib import …`.

## 4. Процесс

**Участники (20+ агент-вызовов):**

1. Plan v1 (planner) — `description.md` + `detailed-plan.md` (467 LOC).
2. Interactive Q&A Q1–Q10 + M1–M7. "Поясняй"-итерации: Q1 (snippet contextlevel vs Telegram), Q4 (Obsidian compat — как frontmatter + wikilinks не ломаются), Q8 (intersection semantics), M1/M2/M5 (MemorySettings, reindex recovery, stdin body).
3. Devil's-advocate wave 1 на plan — 5 blockers + 8 gaps + 5 security. Обновление plan до 630 LOC (Task 0/0b spikes, Compatibility section, G1–G8, S1–S5).
4. Researcher 2 live spikes:
   - `sdk_per_skill_hook.py` — доказал **S-A.3** (SDK 0.1.59 не партиционирует hooks) и **S-A.2** (user-level `permissions.allow` > `options.allowed_tools`).
   - `history_toolresult_shape.py` — **S-B.2 🔴 blocker** (UserMessage skip → нет tool_result в БД) и **S-B.1** (content: str на Bash в 0.1.59).
5. Implementation.md v1 (840 LOC) — переформулированы Q1 и Q8 под empirical constraints.
6. Devil's-advocate wave 2 на implementation — 4 blockers + 6 strategic + 3 security (в т.ч. B-CRIT-3: fabricated SKILL.md bypass).
7. Researcher fix-pack → implementation.md v2 (1347 LOC, +507).
8. Coder wave 1 → 6 коммитов (`42f4861` → `d544441`).
9. Parallel code-reviewer (**fix-then-ship**) + devils-advocate (**🔴 REWORK**). Devils нашёл **B-CRIT-1**: memory CLI unusable через Bash hook из-за запрета shell-метасимволов. Ни один из 384 тестов это не ловил — все шли мимо real hook.
10. Fix-pack (5 🔴 + 4 🟡 + 3 🟢 = 12 items) → 5 финальных коммитов (`de13d70` → `459a09d`).
11. Researcher summary (этот документ).

**Что сработало:**

- **Spike before impl.** Обе ключевые ошибочные презумпции плана (Q8 per-turn, Q1 tool_result rows существуют) были снесены за час работы spike'ов до того, как coder начал кодить.
- **Parallel reviewers на код.** Непересекающиеся findings: reviewer — code quality / test coverage; devils — architecture bypass. **B-CRIT-1 нашёл только devils**, reviewer поставил "fix-then-ship".
- **False-positive test suite как принцип.** 384 passed на коммите `d544441` — но ни один не шёл через real Bash hook validator. Принцип "если ни один тест не вызывает production code path (Bash subprocess через hook) — тесты не фиксируют ничего". Новый тест `test_memory_write_via_hook_gated_bash.py` гоняет real validator.
- **Probe race ордер.** B-CRIT-2 в дровне-версии probe был обратный (fd2-first → рандомная классификация при contention). fd1-first устраняет.

**Размер diff'ов:**

| Коммит | Файлов | +/− | Суть |
|---|---|---|---|
| `42f4861` | 3 | +381 / −2 | bridge: UserMessage tool_result persistence (S-B.2 + B1) |
| `6a9f859` | 8 | +611 / −36 | MemorySettings + global tool_name map + synthetic summary (Q1) |
| `66663b8` | 5 | +276 / −4 | per-skill allowed-tools intersection (Q8 static union) |
| `b892832` | 27 | +2256 / −1 | tools/memory CLI + 19 test-файлов |
| `8a19b29` | 26 | +230 / −31 | skills/memory/SKILL.md + Daemon bootstrap vault + lock probe |
| `d544441` | 2 | +226 / 0 | phase-3 compat + security regression tests |
| `de13d70` | 4 | +311 / −8 | fix: --body-file + SKILL.md write-first (B-CRIT-1) |
| `9dcdef3` | 2 | +56 / −7 | fix: probe race fd1-first (B-CRIT-2) |
| `5bd46b3` | 3 | +120 / 0 | fix: skill-installer oversize warning (B-CRIT-3) |
| `921949a` | 3 | +199 / −6 | fix: FTS5 escape + delete race exit 7 |
| `459a09d` | 9 | +312 / −24 | fix: sanitize_body fence + tokenizer consistency + misc |
| **Итого** | **69** | **+4910 / −51** | |

## 5. Security-hardening breakdown

Wave-1 devils на plan + wave-2 devils на code + reviewer'ы закрыли 12 items. Каждый **🔴** — реальный bypass в предыдущем коде коммита `d544441` или в plan'е.

| # | Pri | Issue | Bypass | Фикс | Тест |
|---|---|---|---|---|---|
| 1 | 🔴 | B-CRIT-1: Bash hook запрещает `|`, модель не может передать body через stdin, CLI unusable | Старый SKILL.md предлагал `echo "…" \| memory write --body -`; Bash validator отклонял | `--body-file` флаг + stage-dir path-guard + write-first pattern в SKILL.md | `tests/test_memory_write_via_hook_gated_bash.py` (5 cases real hook) |
| 2 | 🔴 | B-CRIT-2: probe race — параллельные daemons могли classify flock как "advisory" если один отпускал lock между open и acquire | fd2-first ордер в первой версии probe | fd1-first: если fd1 BlockingIOError → flock real; fd2 только когда fd1 acquired | `tests/test_memory_lock_probe.py` (4 cases, incl. busy-fd1) |
| 3 | 🔴 | B-CRIT-3: фабрикованный SKILL.md c `allowed-tools=[Bash,Read,Write,Grep,WebFetch]` расширяет union для **всех** скиллов через Q8 static intersection | Hard-reject только на unknown tool, нет cap на размер | skill-installer warns при `len>3`, preview печатает `⚠ warnings:` секцию | `tests/test_skill_installer_warns_on_oversize_allowed_tools.py` (6 cases) |
| 4 | 🔴 | S-B.2: tool_result rows никогда не писались в БД (phase-2 skip UserMessage) → Q1 synthetic summary не работает | bridge/claude.py:231 `UserMessage -- skip` | yield `ToolResultBlock` из `UserMessage.content` (def. branch для non-list) | `test_bridge_persists_toolresult_from_usermessage.py` + `test_bridge_yields_user_tool_result.py` |
| 5 | 🔴 | FTS5 query с `-` или `"` ломается, эффект depends on input | Default MATCH без escape | `_escape_fts5_match` default-phrase + `--raw` flag | `tests/test_memory_search_escape.py` (4 cases) |
| 6 | 🟡 | Vault на non-POSIX FS (iCloud/Dropbox/SMB) — `fcntl.flock` silent no-op → corruption | Нет startup check | `_ensure_lock_semantics_once` exit 5, `ASSISTANT_SKIP_LOCK_PROBE=1` escape hatch | `test_memory_lock_probe.py::test_ensure_exits_5_on_advisory_no_op` |
| 7 | 🟡 | Body size unbounded — model-generated loop может заполнить vault | Нет cap | `MEMORY_MAX_BODY_BYTES=1048576` + exit 3 | `test_memory_write_rejects_oversize_body.py` |
| 8 | 🟡 | `---` в теле замыкает frontmatter (Obsidian валидный markdown ломается) | `sanitize_body` до phase-4 fix'а был line-based | Fence-aware: `---` внутри ```` ``` ``` не триггерит sanitize | `test_sanitize_body_fence_awareness.py` (3 cases: fence/mixed/unclosed) |
| 9 | 🟡 | Delete race — два parallel `memory delete X.md` → один успешно, другой падает ambiguous | `unlink()` без `missing_ok` + exit mapping | `unlink(missing_ok=True)` + rowcount check → exit 7 | `test_memory_delete_race_exit_7.py` (2 popen) |
| 10 | 🟡 | Vault perm 0o700 — если existing loose perms, chmod может сломать owner-setup | По-умолчанию create 0o700 | Warn-don't-chmod на existing, идемпотентный warn-once | `test_memory_vault_dir_mode_0o700.py` + `test_tmp_dir_chmods_loose_perms.py` |
| 11 | 🟢 | Stage-dir path-traversal — `--body-file ../../etc/passwd` | Нет resolve+prefix check | `resolve()` + `is_relative_to(stage_dir)` + exit 3 | `test_memory_write_via_hook_gated_bash.py::test_body_file_outside_stage_dir` |
| 12 | 🟢 | Tokenizer config drift между CLI и daemon | Нет shared validator | `_resolve_tokenizer` в `_memlib/fts.py`, unit-gate | `test_memory_tokenizer_consistency.py` (4 cases, default + override) |

## 6. Отложенный технический долг (для phase 5+)

| # | Pri | Замечание | Файл:строка | Фаза закрытия |
|---|---|---|---|---|
| 1 | 🟡 | UNVERIFIED: strict-env (CI, no user `permissions.allow`) — `options.allowed_tools` должен гейтить, но на этом host'е не подтверждено | `plan/phase4/implementation.md §7` + `bridge/claude.py:160-200` | Phase 5 / ops: flag env var + Telegram alert на drift |
| 2 | 🟡 | `ToolResultBlock.content` для Read/WebFetch — defensive list-branch не verified live (spike был только Bash) | `bridge/history.py:88-110` | Phase 5 когда Read/WebFetch активно используются в multi-turn |
| 3 | 🟡 | Obsidian in-place edit (юзер правит vault напрямую) → FTS5 index drift → `memory search` возвращает stale | `tools/memory/main.py` (нет FS watcher) | Phase 5 deferred. Mitigation: `memory reindex` manual. Либо принять stale index как known limitation. |
| 4 | 🟡 | `_memlib` rename не масштабируется на 3+ tool packages — phase 5 `tools/scheduler/` создаст третий sys.path entry | `tools/memory/main.py:1-15` | Phase 5: relative imports `from tools.memory._memlib import …` + `__init__.py` в `tools/` |
| 5 | 🟡 | Cross-FS atomic rename: если vault на другом FS чем project (`MEMORY_VAULT_DIR=/external/mount`) — `.tmp/` внутри vault спасает, но edge случай не везде описан | `tools/memory/_memlib/vault.py:70-95` | Phase 5 doc + integration test |
| 6 | 🟡 | Phase-3 compat test `test_model_does_not_emit_bundle_sha_in_install_command.py` — argparse-based (проверяет exit 2 на `--bundle-sha`), не LLM-turn mock. Реальная регрессия (модель учит флаг из snippet'а) infeasible в unit test | `tests/test_phase3_flow_after_phase4_summary.py` | Phase 5: integration test через real turn замарозка — после phase 5 scheduler |
| 7 | 🟢 | `HISTORY_MAX_SNIPPET_TOTAL_CHARS` cap отсутствует — каждый tool_result truncate'ится до 2000, но сумма всех snippets в одном history может blow out context | `bridge/history.py:150-200` | Phase 5: scheduler будет heavier history → добавить total cap |
| 8 | 🟢 | Marker rotation в `_bootstrap_notify_failure` (phase 3) — metrics ручные, не Prometheus | `src/assistant/main.py:220-298` | Phase 5+ ops |
| 9 | 🟢 | URL detector `_URL_DETECT_MAX=3` (phase 3) — если пользователь присылает 10 URL, 7 silent | `src/assistant/handlers/message.py:34` | Phase 6+ UX |

## 7. Метрики

**LOC исходников (без тестов):**
- `src/assistant/` — **2890** LOC в 20 `.py` (+296 vs phase 3 end):
  - `bridge/claude.py`: 250 → **408** (+158 — UserMessage persistence + static union + safe format).
  - `bridge/history.py`: 64 → **215** (+151 — Q1 synthetic summary + global tool_name map).
  - `config.py`: 75 → **102** (+27 — MemorySettings nested).
  - `main.py`: 402 → **451** (+49 — `_ensure_vault` + lock probe + stage dir).
- `tools/memory/` — NEW **1238** LOC в 6 `.py`:
  - `main.py`: 540. `_memlib/fts.py`: 379. `_memlib/frontmatter.py`: 148. `_memlib/vault.py`: 113. `_memlib/paths.py`: 58. `_memlib/__init__.py`: 0.
- `skills/memory/SKILL.md` — **112** LOC.

**LOC тестов:** **7381** строк в **83** файлах (было 4936 LOC / 58 файлов; +2445 LOC / +25 файлов). **414 passed + 1 skipped** (+132 vs phase 3). Новых тест-файлов в phase 4: 25+ (все `tests/test_memory_*` + 4 `test_bridge_*` + `test_sanitize_body_fence_awareness` + `test_system_prompt_render` + `test_phase3_flow_after_phase4_summary` + `test_tmp_dir_chmods_loose_perms` + `test_skill_installer_warns_on_oversize_allowed_tools`).

**Коммиты phase 4:** 11 (`42f4861`, `6a9f859`, `66663b8`, `b892832`, `8a19b29`, `d544441`, `de13d70`, `9dcdef3`, `5bd46b3`, `921949a`, `459a09d`). Total diff: 69 файлов, +4910 / −51.

**Spike artifacts:** 4 файла (~650 LOC + 2 JSON reports). `sdk_per_skill_hook.py` (316 LOC) + `history_toolresult_shape.py` (332 LOC).

**CI-gates:** `uv sync` OK, `just lint` зелёный (ruff check + format-check + mypy strict), `just test` — **414 passed + 1 skipped** в ~11s.

## 8. Готовность к phase 5

**Готово без новых архитектурных решений:**

- **Sentinel hot-reload** (phase-3 `ClaudeBridge._check_skills_sentinel`) — любой новый скилл, поставленный scheduler'ом (например `tools/scheduler/` push'нет `skills/daily-digest/SKILL.md`), регистрируется без рестарта.
- **Path-guard** (`_is_inside_skills_or_tools`, `check_file_path`) — scheduler сможет писать skill/tool-файлы если их путь попадает под allowed prefix.
- **Write-first pattern через stage dir** — scheduler-injected turn может использовать тот же `data/run/memory-stage/` pattern.
- **`IncomingMessage.origin="scheduler"`** (phase-2 enum placeholder) — scheduler может инжектить turn'ы без telegram envelope; handler уже знает origin.
- **Per-chat `asyncio.Lock`** (phase-2) — scheduler-turn vs telegram-turn не race'ят на том же chat'е.
- **`_bg_tasks` pattern** (phase-3) — scheduler-task ставится как fire-and-forget через `_spawn_bg(..., name="scheduler_tick")`.
- **Global tool_name map в history** — tool_use/tool_result из scheduler-turn'а корректно сопоставятся с именем даже через turn-boundary.
- **Memory CLI invocable через write-first** — scheduler может писать факты в vault тем же механизмом что и модель.

**Требует решения в phase 5 planning:**

1. **Scheduler injection path.** UDS (Unix Domain Socket) vs internal `asyncio.Queue`? UDS — надёжнее между процессами (если scheduler отдельный daemon), queue — проще если scheduler внутри того же Daemon'а. Оба жизнеспособны — решить в phase 5 Q&A.
2. **`HISTORY_MAX_SNIPPET_TOTAL_CHARS` cap.** Phase 4 truncate'ит каждый tool_result до 2000, но scheduler heavier history → сумма может blow out context. Phase 5: добавить total cap в `bridge/history.py`.
3. **`_memlib` → relative imports refactor.** Phase 5 `tools/scheduler/` создаст sys.path collision. Решение: `from tools.memory._memlib import …` + `__init__.py` в `tools/`. До 20 LOC, сделать до первого scheduler-файла.
4. **Obsidian FS watcher.** Юзер правит vault вручную в Obsidian → FTS5 index drift. Варианты: (a) `fsnotify`/`watchdog` демон в Daemon'е (weight — extra dep); (b) принять stale index и документировать `memory reindex` workflow; (c) hybrid — on-read mtime check. Phase 5 решить.
5. **Sandbox runtime для чужого кода.** Phase 4 не решает. Memory-скилл написан in-house, но если phase 5+ ставит 3rd-party скиллы с `Bash` — нужен namespace isolation / nsjail. Phase 7+.

---

Phase 4 закрыт. Долговременная память работает E2E: "запомни что у жены день рождения 3 апреля" → `inbox/wife-birthday.md` → рестарт Daemon → `search "жена"` → hit → ответ. Phase-3 техдолг №4 (per-skill enforcement) и phase-2 inherited блокер (history replay tool_result) закрыты. Security-surface расширен до memory flow (write-first, path-guard, fence-aware sanitize, delete-race, FTS5 escape, allowed-tools cap). 11 коммитов, 414 тестов. Phase 5 (scheduler) разблокирован на уровне API.
