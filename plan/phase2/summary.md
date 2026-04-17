# Phase 2 — Summary

Документ подводит итоги завершённой фазы 2 проекта `0xone-assistant`. Источники: `plan/phase2/{description,detailed-plan,implementation,spike-findings,unverified-assumptions}.md`, исходники `src/assistant/`, 5 коммитов `2dcb702 → 0e2bdcd` и 107 тестов в `tests/`.

## 1. TL;DR

Портирован ClaudeBridge поверх `claude-agent-sdk 0.1.59` (OAuth через CLI, без API key), реализована 7-матчер-система PreToolUse hook'ов (Bash argv-allowlist, file-path guard, WebFetch SSRF с DNS-резолвом), развёрнута схема 0002 (`turns` + `block_type` + FK CASCADE) с корректной backfill-миграцией, налажен skill-discovery через `setting_sources=["project"]` + симлинк, smoke-скилл `ping` отвечает end-to-end. Кодовая база — 1854 LOC в 20 `.py`-файлах + 1434 LOC тестов (107 passing, lint + mypy strict чистые). Phase 3 (skill-creator + skill-installer) разблокирован.

## 2. Что реализовано

### 2.1 Новый пакет `src/assistant/bridge/`

| Файл | Роль | Ключевые решения |
|---|---|---|
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/claude.py` (195 LOC) | `ClaudeBridge` — стримит `InitMeta` → `Block`-и → финальный `ResultMessage` | `query(prompt=async_gen, options=...)` streaming-input mode; `asyncio.timeout()` по `settings.claude.timeout`; `Semaphore(max_concurrent)`; `aclose()` SDK-iterator в `finally` — нет zombie CLI subprocess; `InitMeta` sentinel как первый yield'енный item (несёт model/session_id/skills/cwd из `SystemMessage(subtype="init")`); условный wiring `max_thinking_tokens`+`effort` только если `thinking_budget > 0`. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py` (544 LOC) | PreToolUse guards (Bash/File/WebFetch) | **Bash argv-based allowlist** (`shlex.split` + per-program validator для `python`/`uv`/`git`/`ls`/`pwd`/`cat`/`echo`); shell metachar reject перед `shlex`; `cat`-denylist (`.env`, `.ssh/*`, `id_rsa`, `*.db`); `git` — только `status/log/diff`, запрет `-c`/`--upload-pack`; slip-guard regex как defence-in-depth. **File-guard**: `Path.resolve().is_relative_to(project_root_resolved)` (root resolved один раз в closure — symlink-stable); reject любого `..` в `file_path/path/pattern`. **WebFetch-SSRF**: `urlparse` → `ipaddress.ip_address` для literal, иначе `loop.getaddrinfo()` с 3-с таймаутом → `_is_private_address()` блокирует private/loopback/link-local/reserved/multicast/unspecified. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/skills.py` (110 LOC) | Парсер `SKILL.md` + manifest builder | Собственный YAML-frontmatter парсер (без `python-frontmatter`); mtime-кэш `_MANIFEST_CACHE[Path] -> (mtime, str)`; `_manifest_mtime` — `max` по `skills_dir` + всем `*/SKILL.md` (APFS не бампает dir mtime на in-place edit); `_normalize_allowed_tools` принимает и scalar `Bash`, и list; публичные `invalidate_manifest_cache()` + `touch_skills_dir()` — явный API для phase-3 installer. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/bootstrap.py` (40 LOC) | Идемпотентный симлинк `.claude/skills → ../skills` | Толерантен к пустой директории-остатку от spike; отказывается clobber'ить непустую реальную директорию. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/history.py` (102 LOC) | `history_to_user_envelopes(rows, chat_id) -> Iterator[dict]` | Per R1 spike: история подаётся как последовательность `"type":"user"`-envelope'ов; assistant-turn'ы **не** переэмитятся. Tool_use/tool_result блоки **дропаются** и замещаются synthetic русской system-note `[system-note: в прошлом ходе были вызваны инструменты: X, Y. Результаты получены. (ошибки: Z)]` (U1 unverified — задокументировано для phase 4). Thinking-блоки фильтруются (R2 — SDK отказывается от cross-session thinking). |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/system_prompt.md` | Шаблон с `{project_root}` + `{skills_manifest}` | Инжектит динамический manifest на каждый запрос (cache внутри `build_manifest`). |

### 2.2 Новый пакет `src/assistant/state/`

| Файл | Роль | Ключевые решения |
|---|---|---|
| `/Users/agent2/Documents/0xone-assistant/src/assistant/state/turns.py` (75 LOC) | `TurnStore` с lifecycle `pending → complete \| interrupted` | Shared `asyncio.Lock` с `ConversationStore` (single-writer guarantee для aiosqlite); `start` INSERT; `complete(meta)` UPDATE с `meta_json = JSON(meta)`; `interrupt` — idempotent (`WHERE status != 'complete'`); `sweep_pending()` — startup-cleanup orphan'ов после краха. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/state/conversations.py` (94 LOC) | `ConversationStore.append(..., block_type=...)` + `load_recent(chat_id, limit_turns)` | Turn-based слайс (не row-based): подзапрос `turn_id IN (SELECT … FROM turns WHERE status='complete' ORDER BY COALESCE(completed_at, started_at) DESC, started_at DESC, rowid DESC LIMIT N)`. `rowid DESC` — детерминистичный tiebreaker для turn'ов внутри одной секунды. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/state/db.py` (76 LOC) | Миграционный раннер | `_apply_v1` / `_apply_v2`; `PRAGMA foreign_keys=OFF` на время 0002 (recreate-table pattern), восстановление в `finally`; `BEGIN IMMEDIATE` + rollback on error. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/state/migrations/0002_turns_block_type.sql` | Schema 0002 | Создаёт `turns(turn_id PK, chat_id, status, started_at, completed_at, meta_json)`; backfill — каждый distinct `turn_id` из `conversations` → синтетический `complete` turn; пересоздаёт `conversations` с `block_type` + FK ON DELETE CASCADE; `content_json` остаётся, `meta_json` теперь на `turns`. |

### 2.3 Изменённые модули

| Файл | Что изменилось |
|---|---|
| `/Users/agent2/Documents/0xone-assistant/src/assistant/config.py` (69 LOC) | Nested `ClaudeSettings(env_prefix="CLAUDE_")` с полями `timeout`/`max_turns`/`max_concurrent`/`history_limit`/`thinking_budget`/`effort`. `project_root` (src-relative default) + `data_dir` (XDG_DATA_HOME fallback `~/.local/share/0xone-assistant/`). `env_file=(str(_user_env_file()), ".env")` — prefer `~/.config/0xone-assistant/.env`, fallback к local. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/main.py` (128 LOC) | `_preflight_claude_cli` — `claude --version` с 10-с timeout, exit=3 при отсутствии/сбое. `Daemon.start`: preflight → symlink → mkdir(data_dir) → connect+apply_schema → `TurnStore(lock=conv.lock)` → `sweep_pending()` с логом `startup_swept_pending_turns` → bridge + handler wiring. DI: `Daemon(settings)`, `get_settings()` только в `main()`. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/handlers/message.py` (190 LOC) | `ClaudeHandler` с `emit`-callback контрактом (`handle(msg, emit)`); `_chat_locks: dict[int, asyncio.Lock]` — per-chat сериализация конкурентных turn'ов; `try/finally` с `asyncio.shield(turns.interrupt(...))` — CancelledError не оставляет row'ы `pending`; `_classify(item)` мэпит `TextBlock/ThinkingBlock/ToolUseBlock/ToolResultBlock` → `(role, payload, text_out, block_type)`; `InitMeta` распаковывается в `turns.meta_json` (model + sdk_session_id); ошибки стримлайнятся в единственное русское `⚠ Внутренняя ошибка, детали в логах.`. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/adapters/base.py` (44 LOC) | `IncomingMessage` получает `message_id: int \| None` и `origin: Literal["telegram","scheduler"]` (future-proof для phase 5); `Handler` protocol: `handle(msg, emit) -> None`. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/adapters/telegram.py` (142 LOC) | `split_for_telegram(text, limit=4096)` — предпочитает `\n\n` → `\n` → hard cut; `emit` аккумулирует chunks в handler'е, финальный `send_message` после завершения; пустой ответ → лог `empty_reply_skipped`, без placeholder'а. `parse_mode=None` (phase 2 решение Q3). |

### 2.4 Данные и артефакты

- `/Users/agent2/Documents/0xone-assistant/skills/ping/SKILL.md` — frontmatter (`name`, `description`, `allowed-tools: [Bash]`) + body.
- `/Users/agent2/Documents/0xone-assistant/tools/ping/main.py` — stdlib-only, печатает `{"pong": true}`.
- `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe{,2,3}.py` + `*_report.json` — исполняемые spike-пробники (сохранены в репо как reference).

## 3. Ключевые архитектурные решения phase 2

1. **Auth через OAuth (Claude Code CLI).** `~/.claude/` хранит токен; `ANTHROPIC_API_KEY` **не появляется** в `Settings`/`.env`. Единственный прод-контакт с auth — `claude --version` preflight в `main.py:23-66`.
2. **`hooks={"PreToolUse":[...]}` вместо `can_use_tool`.** Spike R5 доказал: `can_use_tool` молчит, когда tool есть в `allowed_tools` (а он там есть, иначе CLI спросит user prompt). Hooks фирят безусловно. Deny-shape: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"<msg>"}}`.
3. **Bash allowlist-first, slip-guard regex — defence-in-depth.** `shlex.split` токенизирует, metachars `;&|` ``` ` ``` `$(` `${` `>` `<` `\n` `\r` `\x00` отбиваются до `shlex`; per-token whitelist: только `python tools/*`, `uv run tools/*`, `git {status,log,diff}` без опасных флагов, `ls`/`pwd`/`echo` без флагов, `cat` c path-guard + denylist (`.env`, `.ssh/`, `*.db`). Slip-guard regex `(\benv\b|...|base64\s+-d|...)` — последний барьер.
4. **WebFetch SSRF с полноценным DNS-резолвом.** `urlparse` → `ipaddress.ip_address(hostname)` для IP-literal, иначе `loop.getaddrinfo(hostname, None, type=SOCK_STREAM)` под `asyncio.timeout(3s)`. `_is_private_address` блокирует `private/loopback/link_local/reserved/multicast/unspecified` (метаданные AWS 169.254.169.254 покрыты через `link_local`). Не http/https → reject. DNS-failure → reject (safer default).
5. **File path-guard с `.resolve().is_relative_to(project_root_resolved)`.** `project_root_resolved` — single resolve в closure, symlink-stable; reject любого `..` в частях пути ДО resolve (defence-in-depth).
6. **Glob/Grep: reject любых `..` в `pattern`.** Unix glob может `**/../../..` — hook проверяет `pattern` тем же `check_file_path`.
7. **History-replay: drop tool_use/tool_result + synthetic русская note.** Вместо того чтобы рисковать SDK-rejection'ом при реплее tool-блоков (U1 unverified), эмитим `[system-note: в прошлом ходе были вызваны инструменты: X, Y. Результаты получены. (ошибки: Z)]`. **Trade-off:** в multi-turn memory-диалогах модель не видит предыдущий tool_result — риск задокументирован в `plan/phase4/description.md:35-46` как blocker для планировщика phase 4.
8. **SDK async-gen `aclose()` в `finally`.** Без этого timeout/exception оставляют CLI subprocess как zombie. `claude.py:187-195` всегда вызывает `aclose()`.
9. **`InitMeta` sentinel — первый item из `ClaudeBridge.ask`.** Несёт `model`/`session_id`/`skills`/`cwd` из `SystemMessage(subtype="init")`. Handler unpacks в `turns.meta_json` до любого block'а; `ResultMessage.model` — Anthropic-internal alias (не всегда совпадает с init-time ID), поэтому init важнее.
10. **`turn_status` lifecycle + startup sweeper.** `pending → complete \| interrupted`. `interrupt` wrapped в `asyncio.shield` — `CancelledError` не оставляет row'ы в `pending`. `sweep_pending()` при старте `Daemon` — рекавери после краха предыдущего процесса.
11. **Per-chat `asyncio.Lock` в handler.** `ClaudeHandler._chat_locks` сериализует turn'ы внутри одного `chat_id` (готово к phase 5: scheduler и реальный пользователь могут ударить одновременно). Разные `chat_id` параллельны до `claude.max_concurrent`.
12. **Manifest cache с публичным invalidation API.** `invalidate_manifest_cache()` + `touch_skills_dir(skills_dir)` — именно то, что phase-3 skill-installer должен вызывать после atomic rename (задокументировано в docstring `build_manifest`).
13. **Secrets & data вне `project_root` (XDG).** `.env` → `~/.config/0xone-assistant/.env` (`XDG_CONFIG_HOME`); `data_dir` → `~/.local/share/0xone-assistant/` (`XDG_DATA_HOME`). Даже если Bash сумеет прорваться через allowlist, ему нечего читать в `cwd=project_root`.
14. **`IncomingMessage.origin` + `message_id: int \| None`.** Future-proof к phase 5 scheduler — тот инжектит turn'ы без реального Telegram envelope; handler логгирует `origin=` для observability.
15. **Schema 0002: отдельная `turns` + `block_type` на `conversations` + FK CASCADE.** Миграция — recreate-table pattern с `foreign_keys=OFF` на время скрипта; backfill синтезирует `complete` turn на каждый distinct phase-1 `turn_id`. Закрыт техдолг #4 phase 1.

## 4. Процесс и что сработало

**Участники (≥14 агент-вызовов за phase 2):** planner v1 → user Q1–Q10 Q&A → devil's-advocate wave 1 (plan) → planner v2 (B1/B2/S1/S2/S3/S5/S6 + Task 0 spike) → researcher SDK-spike (3 пробника) → researcher implementation.md v1 → devil's-advocate wave 2 (implementation) → researcher implementation.md v2 → coder wave 1 (`2dcb702`) → parallel code-reviewer + devil's-advocate на commit → fix-pack пре-оркестрация → coder wave 2 (4 коммита `1846380 → 0e2bdcd`).

**Что сработало:**

- **Interactive Q&A (Q1–Q10)** перед devil's advocate — по 10 вопросам закрыли `ask`-контракт, delivery-стратегию Telegram, parse_mode, persistence-модель, DI, nested Settings, ping-runtime, cwd, path-guard scope, thinking-блоки. Каждый Recommended-ответ задокументирован в `detailed-plan.md:3-19`.
- **Live SDK spike (Task 0 blocker)** спас от 5 неверных допущений: (a) `can_use_tool` не фирит при `allowed_tools=[...]` — пришлось перейти на hooks; (b) `thinking={"type":"enabled","budget_tokens":N}` не работает — только `max_thinking_tokens`+`effort`; (c) `AssistantMessage.content` — уже собранный список, не per-token; (d) streaming-input mode через `async_gen` а не `list[dict]`; (e) `HookMatcher count = 7` (не 5).
- **Researcher с empirical артефактами** (`spike-findings.md`, `sdk_probe*_report.json`) — coder не стартовал без verified API.
- **2 волны devil's advocate** (на plan + на code) — каждая волна вскрыла неочевидные security-проблемы (wave 1: SSRF hook пропущен; wave 2: 7 🔴 bypass-путей в Bash regex).
- **Parallel reviewers** (code-review + devil's-advocate) на commit `2dcb702` дали непересекающиеся issues — первый про code quality (handler lock, shielded interrupt, empty-reply polish), второй про security (Bash bypass-матрица, WebFetch SSRF слабый, file-path `..` slip).
- **Отказоустойчивость через коммиты.** Coder wave 1 падал на API 500 дважды — work tree при этом сохранялся, coder wave 2 подхватил с ~60% состояния без потери работы. Итоговый fix-pack разбит на 4 семантически-чистых коммита (extract hooks → handler polish → startup sweep → phase-4 risks doc).

**Размер diff'ов:**

| Коммит | Файлов | +/- | Суть |
|---|---|---|---|
| `2dcb702` | 39 | +4469 / −72 | Начальная имплементация (bridge + skills + migration 0002) |
| `1846380` | 12 | +1260 / −329 | Extract `bridge/hooks.py` + argv-allowlist Bash + DNS SSRF + `..` reject + `aclose` lifecycle + security тесты |
| `b27f7d4` | 5 | +330 / −31 | Per-chat handler lock + `IncomingMessage.origin` + shielded interrupt |
| `54d41b0` | 5 | +155 / −36 | Startup sweeper + `claude --version` preflight + skill parser polish |
| `0e2bdcd` | 1 | +15 / −0 | Phase-4 risks doc |

## 5. Security-hardening breakdown

Review-пары pre/post fix-pack. Каждый 🔴 issue был bypass'ом в коде commit `2dcb702`; все закрыты к `0e2bdcd`.

| # | Issue | Bypass в wave 1 | Фикс (wave 2) | Тест |
|---|---|---|---|---|
| 🔴 1 | Bash: `env`/`printenv` стандалоне читали secrets | `_BASH_PREFILTER_RE` только искал `.env` substring | Argv-allowlist: `env`/`printenv` не в `_BASH_PROGRAMS` | `tests/test_bash_allowlist_security.py` (6 cases) |
| 🔴 2 | Bash: octal escape `$'\\47'` → `'` оминал substring-check | Regex видела литерал `\47`, не `'` | Metachars reject до shlex; slip-guard octal regex | `tests/test_bash_allowlist_security.py::test_shell_metacharacters` |
| 🔴 3 | Bash: glob `cat .e??` читал `.env` | Regex искала буквальное `.env` | `cat`-validator: `_path_safely_inside` + basename denylist | `tests/test_bash_allowlist_security.py::test_cat_denylist_basenames` |
| 🔴 4 | Bash: `base64 -d` мог расшифровать secrets из stdin | Не ловилось | Whitelist не содержит `base64`; slip-guard `base64\s+-d` | `tests/test_bash_allowlist_security.py::test_slip_guard_encoded_payloads` |
| 🔴 5 | WebFetch SSRF: `169.254.169.254` (AWS metadata) обходил string-match | Hardcoded tuple `_WEBFETCH_BLOCKED_HOSTS` | `ipaddress.is_link_local` + DNS resolve с `asyncio.timeout(3s)` | `tests/test_webfetch_ssrf_defense.py` (4 cases) |
| 🔴 6 | WebFetch SSRF: DNS rebinding (public host → private IP) | Статичный список хостов | `_resolve_hostname` + классификация каждого A/AAAA | `tests/test_webfetch_ssrf_defense.py::test_hostname_resolves_to_private` |
| 🔴 7 | File-path: `../../etc/passwd` + symlink escape | `str(resolved).startswith(str(root))` (trailing-slash issue) | `Path.resolve().is_relative_to(root.resolve())` + `..`-reject | `tests/test_file_hook_path_guard.py` |
| 🟡 8 | Handler: конкурентные turn'ы одного chat_id интерливились | Нет lock'а | `_chat_locks: dict[int, asyncio.Lock]` | `tests/test_handler_chat_lock.py` |
| 🟡 9 | Handler: `CancelledError` в `finally` оставлял `pending` row | Нет shield | `asyncio.shield(self._turns.interrupt(turn_id))` | `tests/test_handler_chat_lock.py` |
| 🟡 10 | `ResultMessage.model` не совпадал с init-time model ID | Брали из `ResultMessage` | `InitMeta` — первый yield, `ResultMessage` merge-on-top | `tests/test_handler_meta_propagation.py` |
| 🟡 11 | Pending-row после краха daemon'а засоряла `load_recent` | Нет cleanup'а | `TurnStore.sweep_pending()` в `Daemon.start()` | `tests/test_turns_sweep.py` |
| 🟡 12 | CLI отсутствует → turn падает без контекста | Нет preflight | `_preflight_claude_cli` — `claude --version`, exit=3 | manual QA |
| 🟡 13 | SDK zombie subprocess на timeout | Не вызывали `aclose` | `await aclose()` в `finally` | `tests/test_bridge_lifecycle.py::_HangingGen` |
| 🟡 14 | Empty-reply → `"(пустой ответ)"` placeholder | Заглушка | Skip send + `log.info("empty_reply_skipped")` | `tests/test_bridge_mock.py` |
| 🟡 15 | Bridge errors → truncated traceback в чат | Прямое `except` → emit | Единое русское сообщение, full context в structured log | — |
| 🟡 16 | Skill `allowed-tools: Bash` (scalar) → `[]` | `meta.get("allowed-tools", [])` | `_normalize_allowed_tools` принимает str/list | `tests/test_skills_manifest.py` |
| 🟢 17 | Fake `xfail(raise AssertionError)` тесты для U1/U2/U5 давали false green | 3 fake-тесты в wave 1 | Удалены; `plan/phase2/unverified-assumptions.md` — честный manual QA checklist | — |
| 🟢 18 | Manifest cache не инвалидировался извне | Нет API | `invalidate_manifest_cache()` + `touch_skills_dir()` | `tests/test_skills_manifest_cache.py` |
| 🟢 19 | Tool-result с `is_error=True` не различался в history-note | Все инструменты одинаковы | `(ошибки: <names>)` segment в synthetic note | `tests/test_bridge_mock.py` (implicit) |
| 🟢 20 | Phase-4 не знал про history-replay risk и cache-race | Not documented | `plan/phase4/description.md:35-48` — оба риска + 3 пути решения | — |

## 6. Отложенный технический долг для phase 3+

| # | Приоритет | Замечание | Файл:строка | Фаза закрытия |
|---|---|---|---|---|
| 1 | 🔴 | History-replay drop'ает tool_use/tool_result — multi-turn memory теряет контекст | `src/assistant/bridge/history.py:14-28` + `plan/phase4/description.md:35-46` | **Phase 4**: один из 3 путей — verify U1 live и включить replay; перейти на `resume=session_id`; расширить synthetic note кратким summary tool_result'а |
| 2 | 🟡 | Manifest cache 1-секундное FS granularity окно | `src/assistant/bridge/skills.py:44-53` + `plan/phase4/description.md:48` | **Phase 3** installer обязан вызывать `invalidate_manifest_cache()` + `touch_skills_dir()` после add/remove/replace |
| 3 | 🟡 | U3 (symlink skill discovery SDK-side) verified только на bot-side (manifest builder) | `plan/phase2/unverified-assumptions.md:61-83` | **Phase 2 manual QA** (первый реальный запуск с symlink'ом): `just run` → `use the ping skill` → проверить `sdk_init` лог |
| 4 | 🟡 | U5 (`HookMatcher(matcher=regex)`) не подтверждено — 7 отдельных matcher'ов вместо 2 | `plan/phase2/unverified-assumptions.md:87-102`, `src/assistant/bridge/hooks.py:531-544` | **Phase 3+**: попробовать `matcher="Re.*"` — если работает, схлопнуть в 2 matcher'а |
| 5 | 🟡 | U2 (cross-session ThinkingBlock replay) не стресс-тестирован | `plan/phase2/unverified-assumptions.md:41-59` | **Phase 4** если решим включить thinking по дефолту |
| 6 | 🟡 | parse_mode=None — Claude любит markdown, а мы шлём как plain text | `src/assistant/adapters/telegram.py:51-55` | **Phase 3+**: решение HTML vs MarkdownV2, escaping md→target |
| 7 | 🟡 | Streaming handler (edit_message_text каждые N токенов) не реализован — Q2 решено в пользу "буферизуем и шлём в конце" | `src/assistant/handlers/message.py:144-174` | Не в MVP; revisit после UX-сигналов phase 6+ |
| 8 | 🟡 | `Settings` дробить дальше (`ScheduleSettings`, `MemorySettings` и т.д.) | `src/assistant/config.py:49-64` | **Phase 4**: `memory.*` env vars; **Phase 5**: `schedule.*` |
| 9 | 🟢 | `data/vault/` путь захардкожен как `data_dir / "vault"` потом в phase 4 — нужно `vault_dir: Path` в settings | `src/assistant/config.py` (отсутствует) | **Phase 4** |
| 10 | 🟢 | Bash allowlist не расширяем из конфига — phase-5 scheduler может захотеть `cron` / `gh` | `src/assistant/bridge/hooks.py:77-85` | **Phase 5**: config-driven extension или per-skill `allowed-tools`-расширение |
| 11 | 🟢 | `owner-filter` только на `dp.message` — не покрывает `callback_query`, `inline_query` | `src/assistant/adapters/telegram.py:62` | **Phase 3**: inline-кнопки confirm+preview для skill-installer → нужен общий auth-middleware |
| 12 | 🟢 | Long-line в split_for_telegram падает на hard cut посреди слова | `src/assistant/adapters/telegram.py:38-39` | **Phase 3+** (если появятся длинные tool outputs) |

## 7. Метрики

**LOC исходников (без тестов):** 1854 строки в 20 `.py`-файлах. Разбивка:
- `bridge/` — 991 LOC (hooks.py: 544, claude.py: 195, skills.py: 110, history.py: 102, bootstrap.py: 40).
- `state/` — 245 LOC (conversations.py: 94, db.py: 76, turns.py: 75) + 58 LOC SQL migration.
- `handlers/` — 190 LOC.
- `adapters/` — 186 LOC (telegram.py: 142, base.py: 44).
- `main.py` / `config.py` — 128 + 69 LOC.
- `logger.py` / `__main__.py` — 32 + 13 LOC.

**LOC тестов:** 1434 строки в 16 файлах, 107 тестов:
- `test_bridge_mock.py` (180), `test_bash_allowlist_security.py` (163), `test_webfetch_ssrf_defense.py` (154), `test_handler_chat_lock.py` (147), `test_bridge_lifecycle.py` (139), `test_handler_meta_propagation.py` (105), `test_db.py` (95), `test_skills_manifest_cache.py` (87), `test_file_hook_path_guard.py` (71), `test_interrupted_turn_skipped.py` (62), `test_load_recent_turn_boundary.py` (60), `test_bootstrap.py` (53), `test_skills_manifest.py` (48), `test_turns_sweep.py` (38), `test_u3_symlink_skill_discovery.py` (32).

**Коммиты phase 2:** 5 (`2dcb702`, `1846380`, `b27f7d4`, `54d41b0`, `0e2bdcd`). Суммарный diff: ~45 файлов, +6200 / −470.

**CI-gates:** `uv sync` OK, `just lint` зелёный (ruff check + format-check + mypy strict), `just test` — 107/107 passed in 3.63s. Zero `xfail`, zero `skip`.

**Статус 20 review-items:** 🔴 1–7 all closed; 🟡 8–16 all closed; 🟢 17–20 — 17/18/19 closed, 20 записан в `plan/phase4/description.md`.

## 8. Готовность к phase 3

**Можно начинать без новых архитектурных решений (заложено в phase 2):**

- `invalidate_manifest_cache()` + `touch_skills_dir(skills_dir)` — публичный API для skill-installer (`src/assistant/bridge/skills.py:90-106`).
- `parse_skill(path)` с `_normalize_allowed_tools` — skill-creator может использовать как валидатор перед commit'ом нового SKILL.md.
- `ensure_skills_symlink` идемпотентен — installer не конфликтует с Daemon startup.
- `PreToolUse` hooks API проверены на 7 инструментах (Bash + 5 file + WebFetch) — installer может добавлять новые skill-specific hooks через тот же `HookMatcher` паттерн.
- Schema 0002 — `turns` + FK CASCADE — ready для skill-installer'ских turn'ов с `origin="scheduler"` (phase 5 сможет отличать от user turn'ов).
- `IncomingMessage.origin: Literal["telegram","scheduler"]` — installer может симулировать scheduler-origin для fire-and-forget команд.

**Потребует новых решений в phase 3 planning:**

- **Sandbox для чужого кода при install.** Phase-3 skill-installer будет принимать SKILL.md + tools/<name>/*.py от модели — нужно решить: (a) allowlist-on-commit (сразу запускать `ruff`/`mypy` на новый tool); (b) запускать в subprocess с ограниченным `PATH`; (c) доверять модели.
- **Digest/signature для SKILL.md.** После записи — сверять hash при каждом `build_manifest` или доверять FS. Для single-user бота избыточно, но стоит явно зафиксировать.
- **UI/UX preview+confirm.** Owner должен видеть diff перед commit'ом нового skill (inline-кнопки Telegram? или отдельное сообщение с `/confirm <id>`?). → зависит от решения #11 из §6 (owner-filter для `callback_query`).
- **Atomic rename с fsync.** `tempfile + rename()` гарантирует atomicity на POSIX, но не durability до fsync parent dir — решить нужна ли строгость.
- **Rollback-стратегия.** Installer должен уметь rollback при провале валидации — пустой dir / partial write. Git-commit-per-skill или backup-copy?
- **Parse mode для installer-фидбэков.** Связано с #6: если installer выдаёт код-preview, plain text нечитаем — это может форсировать решение HTML/MarkdownV2.

---

Phase 2 закрыт. Security-surface сведён к минимуму (argv-allowlist Bash, DNS-SSRF WebFetch, strict path-guard), lifecycle рекавери на startup работает, 107 тестов фиксируют инварианты. Готовность к phase 3 (skill-creator + skill-installer) — полная на уровне bridge/skills API; открытые вопросы только в UX-плоскости (preview/confirm) и в sandbox-политике для пользовательского кода.
