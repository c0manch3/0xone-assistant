---
phase: 2
title: ClaudeBridge + Skills plumbing + schema migration 0002
date: 2026-04-20
commits:
  - 6bd2807  # phase 2: claude bridge + skills plumbing
  - 10c641b  # hotfix: raise preflight timeout 15s -> 45s
status: shipped (local commit + owner smoke pending)
---

# Phase 2 — Summary

Phase 2 превратил phase-1 echo-бота в живой Claude Code прокси поверх
`claude-agent-sdk==0.1.59`. OAuth-only (сессия `~/.claude/`, никаких API-ключей);
bridge поднят с `setting_sources=["project"]`, 7 PreToolUse-хуков, schema-0002 c
`turns` + `block_type`; smoke-skill `ping` подхватывается моделью и возвращает
`{"pong": true}`.

---

## 1. Что shipped

Ключевые артефакты phase 2:

- **`src/assistant/bridge/claude.py`** — `ClaudeBridge.ask`: streaming-input
  mode, `AsyncIterator[Block]` outward, `asyncio.Semaphore(max_concurrent=2)`,
  `asyncio.timeout(CLAUDE_TIMEOUT=300)`, cost/cache_read логирование на
  `ResultMessage`.
- **`src/assistant/bridge/hooks.py`** — 7 PreToolUse-хуков:
  - 1 × Bash: allowlist-first prefilter + hardened slip-guard regex
    (`env`/`printenv`/`set`, `.env`/`.ssh`/`.aws`/secrets/`.db`/`token`/
    `password`, octal/hex/base64 decode, compound-chaining `;&|` `` ` `` `$()`).
  - 5 × file-tools (Read/Write/Edit/Glob/Grep): path-guard с
    `Path.is_relative_to(project_root)`, резолв относительных путей.
  - 1 × WebFetch: два слоя — literal hostname blocklist + `socket.getaddrinfo`
    → `ipaddress.is_private/loopback/link_local/reserved` (DNS в
    `asyncio.to_thread`).
- **`src/assistant/bridge/skills.py`** — frontmatter-parser + mtime-max
  manifest cache (ключ = `max(skills_dir.mtime, *SKILL.md.mtime)`).
- **`src/assistant/bridge/bootstrap.py`** — идемпотентный абсолютный symlink
  `.claude/skills → <project_root>/skills`;
  `assert_no_custom_claude_settings` блокирует старт при `.claude/settings.json`
  с ключами `hooks`/`permissions.deny` (sys.exit 3, cosmetic keys allowed).
- **`src/assistant/bridge/history.py`** — `history_to_sdk_envelopes`:
  заменил v2 synthetic-note shim на verbatim-replay user+assistant envelopes
  (R13-verified); `thinking`-фильтр сохранён; defensive NULL→"text" на
  `block_type`.
- **`src/assistant/bridge/system_prompt.md`** — identity + `{skills_manifest}`
  + правила (долговременная память только через skill `memory`, Bash без
  деструктива без подтверждения, file-edit sandboxed).
- **`src/assistant/state/migrations/0002_turns_block_type.sql`** (reference
  only) + **`src/assistant/state/db.py::_apply_0002`** (authoritative,
  statement-by-statement, внутри `BEGIN EXCLUSIVE` без `executescript` —
  см. B2).
- **`src/assistant/state/conversations.py`** — Turn API:
  `start_turn`/`complete_turn`/`interrupt_turn`/`cleanup_orphan_pending_turns`;
  `load_recent` — **turn-LIMIT** через CTE (B6), фильтр `turns.status='complete'`.
- **`src/assistant/handlers/message.py::ClaudeHandler`** — заменяет
  `EchoHandler`: `start_turn → append(user) → load_recent → bridge.ask →
  classify+append → complete_turn` с `try/finally` маркирующим interrupted.
  `ToolResultBlock → role='user'` (B5, Anthropic tools API contract).
- **`src/assistant/adapters/telegram.py`** — `parse_mode=None`, emit-callback
  pattern, `_split_for_telegram` >4096.
- **`src/assistant/config.py`** — XDG paths: `~/.config/0xone-assistant/.env`
  + `~/.local/share/0xone-assistant/`; nested `ClaudeSettings` (timeout,
  max_turns, max_concurrent, history_limit, thinking_budget).
- **`skills/ping/SKILL.md`** + **`tools/ping/main.py`** — smoke skill.
- **136 tests pass** (ruff + mypy strict clean).

---

## 2. Owner-level impact (UX diff от phase 1)

- **`.env` переехал** из репо в `~/.config/0xone-assistant/.env`;
  `data/assistant.db` → `~/.local/share/0xone-assistant/`. Для обновляющихся
  инсталляций owner запускает `scripts/migrate-phase1-to-phase2.sh` один раз
  перед первым запуском phase 2 (idempotent).
- **Бот отвечает Claude'ом вместо эха.** `"use the ping skill"` → модель
  делает Bash `python tools/ping/main.py` → bot отвечает `pong`/`true`.
- **`parse_mode=None` (plain-text).** Markdown/HTML из phase 1 больше не
  парсится; текущие рендер-атефакты в старых turn'ах — остаются исторически.
- **Startup preflight** вырос до 45s (BW1 hotfix): `claude --print ping`
  в project-dir наблюдался ~13–15s из-за cold MCP init, 15s оказалось
  слишком жёстко.

---

## 3. Spike findings (R1–R13, краткая сводка)

Spike прошёл в 3 волны против реального OAuth CLI, артефакты живут в
`plan/phase2/spikes/` + `spike-findings.md`.

| R | Что выяснили |
|---|---|
| R1 | Multi-turn history — streaming-input `query(prompt=AsyncIterable[dict])` с envelope'ами `{"type":"user","message":{"role":"user","content":...},"session_id":...}`. `resume=session_id` работает, но не используем — наш `ConversationStore` — source of truth. |
| R2 | Thinking — `ClaudeAgentOptions(max_thinking_tokens=N, effort="high")`. `extra_args={"thinking":...}` падает в CLI с "Allowed choices are enabled, adaptive, disabled". В phase 2 OFF по умолчанию (`thinking_budget=0`). |
| R3 | Skills auto-discovery через `setting_sources=["project"]` + `cwd=project_root` работает end-to-end; `SystemMessage(init).data["skills"]` перечисляет подхваченные SKILL.md. |
| R4 | Message stream — блоки приходят уже собранными в `AssistantMessage.content: list[Block]`, не per-token. `include_partial_messages=True` для token-streaming не используем. |
| R5 | Permission layer = `hooks={"PreToolUse":[HookMatcher(...)]}`, **не `can_use_tool`** — последний молча не файрит если `allowed_tools` задан. Shape: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}`. |
| R7 | Automatic prompt caching на уровне CLI (ephemeral_1h tier). `cache_creation ~5700` после первого запроса, `cache_read ~17400` дальше. Zero code change. |
| R8 | Bash allowlist-first + hardened slip-guard — 36/36 bypass vectors денаются (env-dump, base64/hex/openssl, command-chaining, `$()`, octal escape, PATH injection, `python -c` escape). |
| R9 | WebFetch string-only blocklist слепа к DNS rebinding → добавлен второй слой: `socket.getaddrinfo` + `ipaddress` category check. TOCTOU residual → U9 (accepted single-user risk). |
| R10 | Envelope `session_id` SDK **игнорирует** в streaming-input mode — fresh UUID на каждый `query()`. Parallel `query()` не коллизят. Наш `f"chat-{chat_id}"` — cosmetic breadcrumb. |
| R11 | Migration 0002 crash-safe (3 сценария: happy / crash→ROLLBACK→v=1 / re-run на v=2 no-op). Canonical SQL зафиксирован. |
| R12 | NULL `block_type` не должен возникать (миграция ставит DEFAULT 'text' + backfill), но defense-in-depth `btype or "text"` добавлен. |
| R13 | **FIX-PACK:** SDK принимает и модель учитывает envelope `{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":...}]}}` — sentinel-differential ("424242" виден во 2-м turn'е, baseline без envelope — не виден). Отменило synthetic-note shim из v2. |

---

## 4. Owner decisions (Q1–Q13)

| Q | Решение |
|---|---|
| Q1 | `.env` → `~/.config/0xone-assistant/.env` (fallback `./.env`); `data_dir` → `~/.local/share/0xone-assistant/`. |
| Q2 | `mkdir(parents=True, exist_ok=True)` для `data_dir` на старте daemon. |
| Q3 | `claude-agent-sdk>=0.1.59,<0.2` (spike-verified на 0.1.59). |
| Q4 | Permission layer — `hooks={"PreToolUse":[...]}`, не `can_use_tool`. |
| Q5 | 7 explicit `HookMatcher`-ов (Bash + 5 file-tools + WebFetch); regex-collapse отложен до верификации U5. |
| Q6 | `ClaudeBridge.ask` → `AsyncIterator[Block]` + handler получает `emit`-callback. |
| Q7 | `CLAUDE_HISTORY_LIMIT=20` **turn-LIMIT** (не row-LIMIT — исправлено B6). Token-budget deferred to phase 4+. |
| Q8 | `load_recent` пропускает `turns.status != 'complete'`. |
| Q9 | Manifest mtime-cached по `max(skills_dir.mtime, *SKILL.md.mtime)`. |
| Q10 | `tools/ping/main.py` — plain stdlib, без своего `pyproject.toml`. |
| Q11 | `parse_mode=None` (plain text). |
| Q12 | `EchoHandler` удалён полностью — no fallback. |
| Q13 | Migration 0002 (`turns` + `block_type`) shipped в phase 2 — закрывает tech-debt #4 раньше срока. |

---

## 5. Blockers caught & closed

### Wave 2 devil's-advocate fix-pack (B1–B10, S-series, N-series)

| ID | Проблема | Фикс |
|----|----------|------|
| B1 | `_apply_0002` при прямом вызове на v=2 БД стомпал `block_type='tool_use'` 'text'-дефолтом | Early-exit при `PRAGMA user_version >= 2` |
| B2 | `conn.executescript(...)` внутри `BEGIN EXCLUSIVE` коммитит транзакцию implicitly (sqlite3 docs); rollback нечего откатывать | Переход на statement-by-statement `conn.execute(...)` |
| B3 | Phase-1 signal supervision (stop_event + polling_exc + SIGTERM/SIGINT) был потерян в черновике | Восстановлен в `main()` |
| B4 | `IncomingMessage.message_id` + `MessengerAdapter(ABC)` + `Handler(Protocol)` потерялись | Оба сохранены |
| B5 | `ToolResultBlock` изначально классифицировался `role='assistant'` — противоречит Anthropic tools API | `ToolResultBlock → role='user' block_type='tool_result'` |
| B6 | row-LIMIT в `load_recent` режет turn посередине → SDK получает orphan tool_use без tool_result | Переделан на **turn-LIMIT** через CTE |
| B7 | Bash allowlist `cat` проверял только первый позиционный аргумент; `cat README.md .env` проходил | Валидация всех positional args внутри `project_root` |
| B8 | `file_hook` не резолвил относительные пути против `project_root` → `Read("../../etc/passwd")` проскакивал | `Path.resolve()` всегда относительно `project_root` |
| B9 | Пустой path-аргумент для Read/Write/Edit → неявный allow | Explicit deny; optional только для Grep |
| B10 | `socket.getaddrinfo` блокирует event loop до ~5s на timeout | Вынесен в `asyncio.to_thread`; ловим `OSError`/`TimeoutError` |
| S2 | `ResultMessage.model` не существует в SDK 0.1.59 types | `model` захватывается из `AssistantMessage.model` inside the loop |
| S4 | `.claude/skills → ../skills` (relative) ломался при инспекции абсолютными путями | `symlink_to(<project_root>/skills, target_is_directory=True)` — absolute |
| S7 | v2 synthetic-note shim терял multi-turn continuity ассистента | R13-verified verbatim replay user+assistant envelopes |
| S10 | Orphan pending-turn после crash бота оставался pending forever; history его не фильтровала (но и не закрывала) | `cleanup_orphan_pending_turns` на bootstrap — `UPDATE status='interrupted' WHERE status='pending'` |
| S15 | `.claude/settings.json` с `{"hooks":{}}` или `permissions.deny` молча переопределял наши hooks | `assert_no_custom_claude_settings` → `sys.exit(3)` при наличии load-bearing ключей; cosmetic keys проходят с warning |
| N2 | `project_root` мог остаться relative после pydantic validation | Validator резолвит в absolute |
| N6 | `.env.example` + README не упоминали XDG location | Добавлено |

### Wave 3 (post-ship hotfix)

| ID | Проблема | Фикс |
|----|----------|------|
| BW1 | 15s preflight timeout оказался слишком жёстким — `claude --print ping` в project-dir стартует 13–15s (cold MCP init + settings parsing); daemon падал на старте на deploy workstation | `10c641b`: bump 15s → 45s |

---

## 6. Unresolved U-items (residual risk to future phases)

| # | Assumption | Посадочная фаза для верификации / митигации |
|---|------------|----------------------------------------------|
| **U1** | Replay envelope'ов с `tool_use`+`tool_result` blocks (не только text) — R13 покрыл только text-only assistant replay | Phase 3+ (skill-creator/installer поднимет объём tool-вызовов — там и прогонится live probe). Fallback уже заложен: фильтр tool-блоков из history_to_sdk_envelopes. |
| **U2** | SDK отвергает cross-session `ThinkingBlock` replay | Phase 4+ если включим thinking. Сейчас `thinking_budget=0`. Owner-invoked probe на thinking-enabled модели. |
| **U3** | Symlink `.claude/skills → ../skills` подхватывается SDK'ом наравне с real directory (probe писал в real dir, не через symlink) | **Owner live-smoke в phase 2 deploy** — `ls -la .claude/skills` + ping вызов подтвердит. Unit test на manifest builder уже есть. |
| **U5** | `HookMatcher(matcher="Read\|Write\|...")` regex-форма работает (сейчас 7 explicit вместо 2 regex) | Cosmetic collapse. Phase 6 (security hardening) если понадобится. Owner-invoked probe. |
| **U9** | WebFetch SSRF TOCTOU DNS rebinding — between `getaddrinfo` и CLI fetch attacker может rotate RR | **Accepted single-user risk**, в README security-considerations. Phase 6 hardening через OS-level egress ACL (`pf`/`iptables`). |
| **U10** | Assistant envelope shape stability across SDK point-releases | Re-run `plan/phase2/spikes/r13_assistant_envelope_replay.py` на каждом `claude-agent-sdk` upgrade. Marker `requires_claude_cli` на `tests/test_u10_assistant_envelope_shape_live.py`. |

---

## 7. Tests (136 pass)

Регрессионные категории:

- **Bash hook bypass (`test_bash_hook_bypass.py`)** — 36 vectors R8 + 3
  multi-arg cat case (B7).
- **File hook containment (`test_file_hook_relative.py`)** —
  `../../etc/passwd` и absolute `/etc/passwd` → deny; `relative/in_root.py` →
  allow (B8).
- **WebFetch SSRF (`test_webfetch_ssrf.py`)** — 10 direct cases + DNS-mocked
  rebinding (R9 + B10).
- **Migration crash (`test_migration_0002_crash.py`)** — 3 scenarios R11,
  все через `apply_schema(conn)`, не `_apply_0002` напрямую (B1).
- **Migration idempotency (`test_migration_0002_applies_once.py`)** —
  re-run на v=2 с `block_type='tool_use'` не стомпает (B1).
- **History assistant replay (`test_history_assistant_replay.py`)** — R13
  hermetic mapping test.
- **History null block_type (`test_history_null_block_type.py`)** — R12
  defense.
- **Orphan turn cleanup (`test_orphan_turn_cleanup.py`)** — S10.
- **Custom settings block (`test_bootstrap_custom_settings_block.py`)** —
  S15.
- **Turn-limit load_recent (`test_load_recent_respects_limit_and_skips_interrupted.py`)** — B6.
- **Telegram split (`test_telegram_split.py`)** — >4096 splitter.
- **Claude handler (`test_claude_handler.py`)** — block classification,
  try/finally interrupted path.

Manual-verification (`requires_claude_cli` marker, skipped in CI) —
U2/U3/U5/U10 probes owner-invoked на upgrade.

---

## 8. Deploy notes

1. **Owner migration script (`scripts/migrate-phase1-to-phase2.sh`)** —
   идемпотентно переносит `.env` в `~/.config/0xone-assistant/` и
   `data/assistant.db*` (включая `-wal`/`-shm`) в
   `~/.local/share/0xone-assistant/`. Вызывается **один раз** перед первым
   запуском phase 2. На чистой установке просто создаёт директории.
2. **Daemon supervision** — `main()` держит `stop_event` + `polling_exc`;
   SIGTERM/SIGINT → graceful shutdown; detached режим через setsid-wrapper
   (phase-1 pattern сохранён).
3. **Preflight `claude --print ping`** — 45s (не 15s): cold MCP init +
   settings parsing в project-dir может занять 13–15s (наблюдалось на
   deploy workstation, BW1 hotfix).
4. **Orphan cleanup на boot** — `cleanup_orphan_pending_turns` переводит
   `pending → interrupted`, лог `orphan_turns_cleaned=N`. На первом запуске
   `N=0`; если `>0` — предыдущий запуск крэшнулся.
5. **`assert_no_custom_claude_settings`** — fail-fast с `sys.exit(3)` если
   `.claude/settings.json` содержит load-bearing ключи (`hooks`,
   `permissions.deny`). Cosmetic keys — warning + INFO dump redacted.

---

## 9. Open tech-debt (nice-to-haves)

Не блокеры для phase 3; перечислено с рекомендацией фазы:

- **`parse_mode=None` UX для legacy phase-1 turn'ов** — старые HTML-tagged
  rows остаются с `<b>`/`<i>` в plain-text рендере. Phase 3+ если owner
  пожалуется; либо `/start` wipes history. Non-issue для чистой установки.
- **`max_thinking_tokens` формально deprecated в пользу `thinking=...`
  TypedDict**, но TypedDict CLI отклоняет ("enabled/adaptive/disabled"
  only — см. R2). Держим `max_thinking_tokens` пока SDK не догонит.
  Phase 4+ если включим thinking.
- **Manifest cache concurrency** — `_MANIFEST_CACHE` dict без lock.
  Single-event-loop daemon не гоняет `build_manifest` параллельно, но в
  phase 5 (scheduler) — пересмотреть. Вероятный фикс: `asyncio.Lock` вокруг
  rebuild.
- **Synthetic tool-note fallback удалён** (R13). Если U1 для
  `tool_use`+`tool_result` mixed content не пройдёт — нужен revive shim
  только для этой формы envelope (не для text-only). Phase 3+ покроет
  эмпирически.
- **FK `conversations.turn_id REFERENCES turns(turn_id)` не добавлен**
  (recreate-add-FK требует populated `turns` сначала). Phase 4+ второй
  recreate если понадобится реальный FK CASCADE.
- **`session_id` в envelopes cosmetic** (R10) — оставлен как
  human-readable breadcrumb в логах. Если кто-то из будущих разработчиков
  решит положиться на него для continuity — закомментировано в
  `history.py`.

---

## 10. Что phase 3 строит поверх phase 2

Phase 3 (skill-creator/installer) получает в наследство:

- **`ClaudeBridge` API** — `async for block in bridge.ask(...)` + emit
  pattern. Не меняется. Skill-installer вызывается моделью как tool →
  проходит через Bash hook (prefix `python tools/skill-installer/...`
  пойдёт в allowlist).
- **Skills manifest pipeline** — `build_manifest` с mtime-кэшем уже знает
  как инвалидироваться на atomic rename (skill-installer должен писать
  через `tempfile + os.rename()`). `max(skills_dir.mtime, *SKILL.md.mtime)`
  ключ — atomic rename меняет mtime родителя → cache invalidates на
  следующем запросе.
- **Bash hook guard** — skill-installer добавляет новый `tools/<name>/`
  под allowlist? Либо explicit allowlist entry для
  `python tools/skill-installer/main.py`, либо glob-pattern `python
  tools/*/main.py` уже в allowlist (проверить: phase 2 explicit для
  `ping`, не wildcard).
- **XDG filesystem conventions** — `~/.local/share/0xone-assistant/` как
  data root. Skills сами — в `<project_root>/skills/` (под git), tools
  в `<project_root>/tools/`. Skill-installer НЕ пишет в `~/.local/share`
  — только в project tree.
- **Schema 0002 `turns` + `block_type`** — skill-installer-turns
  записываются как обычные turns; никаких схемных миграций 0003 от phase 3
  не требуется если skill-installer не вводит новых persistent entities.
- **`assert_no_custom_claude_settings`** — остаётся как guard; phase 3
  не должен генерить `.claude/settings.json`.

---

## Цитирования

- `plan/phase2/description.md`, `detailed-plan.md`, `implementation.md`
  (v2.1), `spike-findings.md` (R1–R13), `unverified-assumptions.md` (U1–U10).
- Commit `6bd2807` — phase 2 ship; `10c641b` — BW1 preflight hotfix.
- Spike artefacts: `plan/phase2/spikes/r7_prompt_caching.py` ...
  `r13_assistant_envelope_replay.py` + `.json` reports.
