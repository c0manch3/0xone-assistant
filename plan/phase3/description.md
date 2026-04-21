# Phase 3 — Skill-creator (marketplace) & skill-installer

**Цель:** бот умеет нативно расширять себя двумя путями: (1) модель сама пишет новый скилл через встроенный Write tool, опираясь на Anthropic's `skill-creator` skill (берётся из публичного marketplace); (2) пользователь/модель ставит готовый скилл по URL или из marketplace.

**Вход:** phase 2 shipped (commit `fbd9c18`): `ClaudeBridge` + 7 PreToolUse hooks + `_MANIFEST_CACHE` mtime-max dict (no public invalidation API yet) + sandboxed Write/Edit path-guard + `SystemPromptPreset` preset-append. Phase 3 **introduces** public helpers `invalidate_manifest_cache()` + `touch_skills_dir()` in `bridge/skills.py` and extracts `_normalize_allowed_tools` as a named helper.

**Выход:** два рабочих сценария через бот:
1. "сделай скилл для погоды" → модель подтягивает `skills/skill-creator/SKILL.md` (установленный при auto-bootstrap из Anthropic marketplace) → через Write пишет `skills/weather/SKILL.md` + `tools/weather/main.py` (Write sandboxed phase 2) → PostToolUse hook touch'ит `data/run/skills.dirty` → следующий же запрос видит новый скилл в manifest.
2. "какие есть готовые скилы?" → модель вызывает `mcp__installer__marketplace_list` → JSON-список → пользователь выбирает → `mcp__installer__marketplace_install(name="NAME")` → preview → "да" → `mcp__installer__skill_install(url=..., confirmed=true)` → atomic copy → готово. Либо тот же flow с произвольным URL: `mcp__installer__skill_preview(url=<URL>)` → confirm → `skill_install`.

**Источник marketplace:** Anthropic's skills лежат в подкаталоге `skills/` своего репо. API-путь — `/repos/anthropics/skills/contents/skills` (не корень), а tree-URL-шаблон — `https://github.com/anthropics/skills/tree/main/skills/<name>/`. Hardcoded constants (`MARKETPLACE_URL`, `MARKETPLACE_BASE_PATH="skills"`) живут в `src/assistant/tools_sdk/_installer_core.py` (imported shared helpers module for all installer `@tool` functions). Fetch chooses первое из available: `gh api`, `git clone --depth=1`, `httpx` (raw.githubusercontent.com fallback для single-file fetches).

## Задачи

- **@tool-decorator pivot (D1 closeout, owner-decided Q-D1=c, 2026-04-21)** — Opus 4.7 системно игнорирует imperative "run X via Bash" в skill body (GH #39851/#41510); owner выбрал architectural pivot вместо PostToolUse enforcement. Phase 3 **не добавляет** D1 hooks или cross-turn state tracking. Вместо этого:
  - Skills остаются prompt-expansions only (phase-2 ping pattern, midomis-style): SKILL.md body описывает поведение как text-gen / reference / workflow guidance, НЕ как Bash-macro.
  - Phase 3 вводит **groundwork** для `@tool`-decorator custom SDK tools: в `bridge/claude.py` добавляется `mcp_servers={}` slot в `ClaudeAgentOptions` (пустой в phase 3, заполнится в phase 4+ memory).
  - Researcher spike RQ1 в step 4 MUST verify: (a) `claude_agent_sdk.tool` decorator + `create_sdk_mcp_server` live-работают, (b) custom tools видны model'ю через `SystemMessage(init).data["tools"]`, (c) существующие PreToolUse hooks intercept custom-tool calls (Bash/Read/Write still gated), (d) `setting_sources=["project"]` + `mcp_servers=` coexist без конфликта.
  - Phase 4 memory precondition: memory_search/memory_write будут `@tool` functions в `src/assistant/tools_sdk/memory.py`, зарегистрированные через `create_sdk_mcp_server`. SKILL.md-based memory tool **отменён**.
  - Phase 8 gh tool аналогично.
  
  Acceptance criterion: researcher spike RQ1 PASSES (hermetic live test с 1 dummy `@tool`) + phase-4 plan description.md обновлён что memory = `@tool`.
  
  См. `plan/phase2/known-debt.md#D1` + `memory/reference_claude_agent_sdk_gotchas.md`.

1. **`src/assistant/tools_sdk/installer.py`** — `@tool`-decorator functions replacing legacy CLI approach. Dogfood pivot per BL-1 (2026-04-21): model invokes installer как first-class SDK tools, не через SKILL.md+Bash body-instruction pattern. Functions:
   - `@tool("skill_preview", "Fetch + validate skill from URL, return preview JSON", {"url": str})` — базовый flow: fetch (gh api | git clone | httpx для raw) → static validate (SKILL.md frontmatter, Python AST, no path traversal, size limits) → preview JSON с `{name, description, files, sha}`. Cache в `<data_dir>/installer-cache/<sha256_of_url[:16]>/`, TTL 7 days.
   - `@tool("skill_install", "Install previewed skill after user confirmation", {"url": str, "confirmed": bool})` — requires prior `skill_preview` in same session; re-fetches, SHA-compares с cached bundle → расхождение = `{"error": "source changed since preview", "code": 7}` (model surfaces to user). Atomic copy skills/<name>/ + tools/<name>/ via tmp+rename. Returns `{"installed": true, "name": name, "sync_pending": true}`.
   - `@tool("skill_uninstall", "Remove installed skill", {"name": str, "confirmed": bool})` — requires `confirmed=true`. rmtree skills/<name> + tools/<name>, sentinel touch. Idempotent on missing → `{"removed": false, "reason": "not installed"}`.
   - `@tool("marketplace_list", "List available skills from Anthropic marketplace", {})` — hardcoded `gh api /repos/anthropics/skills/contents/skills` → JSON `[{name, description}]`. Gated behind `shutil.which("gh")`; если нет — fallback `git clone --depth=1 --sparse` на `<data_dir>/run/tmp/anthropics-skills/` (if `git` available). Если ни `gh`, ни `git` — returns `{"error": "marketplace requires gh or git", "code": 9}`.
   - `@tool("marketplace_info", "Show SKILL.md for marketplace skill", {"name": str})` — fetch конкретный SKILL.md через `gh api contents/skills/<NAME>/SKILL.md` (или git sparse).
   - `@tool("marketplace_install", "Shortcut to install from marketplace by name", {"name": str})` — строит tree-URL `https://github.com/anthropics/skills/tree/main/skills/<NAME>/` и делегирует `skill_install` flow (preview inclusive).
   - `@tool("skill_sync_status", "Check async uv sync status for installed skill", {"name": str})` — читает `<data_dir>/run/sync/<name>.status.json` → `{"status": "pending|ok|failed", "elapsed_sec": N, "error": ...}`.

   All `@tool`s register через `create_sdk_mcp_server(name="installer", version="0.1.0", tools=[...])` и передаются в `ClaudeAgentOptions.mcp_servers={"installer": ...}`. Internal helpers live в `src/assistant/tools_sdk/_installer_core.py` (fetch, validate, atomic_install, etc.) — importable units, не `@tool`.

   Legacy `tools/skill-installer/main.py` CLI **не создаётся**. Если owner захочет manual invocation outside of chat — future phase (или direct Python module invocation). Phase 3 single entry point = model через `@tool`.

   **BL-2 closeout:** installer detects missing `gh` AND missing `git` в `_gh_wrapper`/`_git_wrapper` helpers → returns `{"error": "marketplace requires gh or git", "code": 9}` в tool response. Partial-install detection: `atomic_install` touch'ит marker `.0xone-installed` AFTER atomic rename; bootstrap checks marker (not directory existence) to detect partial installs. `<data_dir>/run/tmp/` auto-cleaned on `Daemon.start()` via existing `_sweep_run_dirs`.
2. **Auto-bootstrap Anthropic's `skill-creator` via direct Python fetch+Write (fire-and-forget).** В `Daemon.start()` после `ensure_skills_symlink` запускаем `asyncio.create_task(self._bootstrap_skill_creator_bg())`. Task:
   - Check marker `skills/skill-creator/.0xone-installed` → if exists, return (idempotent).
   - Attempt fetch bundle (gh api OR git clone fallback) in `<data_dir>/run/tmp/skill-creator-boot/`.
   - Validate bundle (same static checks as `_installer_core`): SKILL.md frontmatter valid, files within size limits, Python AST OK, no path traversal.
   - Atomic copy (tmp+rename) → `skills/skill-creator/`. Touch `skills/skill-creator/.0xone-installed` marker.
   - Touch sentinel `<data_dir>/run/skills.dirty`.
   - Log `skill_creator_bootstrap_ok` or `_failed` / `_timeout` (120s task-internal timeout) / `_exception`.

   **Key change vs pre-wipe plan:** bootstrap is direct Python fetch+Write inside `_bootstrap_skill_creator_bg`, NOT `marketplace install skill-creator` subprocess (which would rely on skill-installer being already available — chicken-and-egg). Body-compliance issue (#39851) bypassed entirely: no model, no body-following, just Python I/O.

   `Daemon.start()` never awaits this task. `shutil.which("gh")` None AND `shutil.which("git")` None → bootstrap skipped immediately, log.warning surfaces in daemon log (owner может проверить через `tail /tmp/0xone-phase2-daemon.log`). Acceptance criterion (§"Критерии"): bootstrap exit состояние `ok|failed|timeout|skipped_no_gh_nor_git` всегда logged.
3. **`skills/skill-installer/SKILL.md`** — thin description (не body-instruction). Frontmatter: `name: skill-installer`, `description: Manage skill installation from GitHub/marketplace. Use when user wants to preview, install, or uninstall a skill.`. Body покрывает **когда** использовать `skill_preview/skill_install/marketplace_list/etc.` tools (с trigger phrases "поставь скилл", "какие скилы есть", "удали скилл X"), с examples диалогов. Но body НЕ говорит "run via Bash" — tools first-class через MCP. Model увидит tool names в `SystemMessage(init).data["tools"]`, description/triggers в skill body = discoverability aid.
4. **URL detector в `handlers/message.py`** — регексп на http(s)/git@; если URL найден — одноразовая system-note ("пользователь прислал URL X; если похоже на skill bundle, используй `skill_preview` tool, иначе игнорируй").
5. **Расширение Bash-allowlist в `bridge/hooks.py`** (phase 2 `_BASH_PROGRAMS`):
   - `git clone --depth=1 <https-url> <dest>` — schema-check + path-guard на dest.
   - `uv sync --directory tools/<name>` — path-guard.
   - `gh api <endpoint>` — **read-only**: разрешён только GET (дефолт), endpoint должен matched'иться на `/repos/.../contents/...` или `/repos/.../tarball/...`; любые `-X POST/PATCH/DELETE/PUT` / другие endpoints → deny.
   - `gh auth status` — read-only, allow.
   - Остальные `gh` и `git` подкоманды остаются закрытыми.
   - Требуется `gh` на хосте — задокументировано в README; при отсутствии `shutil.which("gh")` в `Daemon.start()` логируем `log.error` и отключаем marketplace-функционал, CLI в целом остаётся живым.
6. **PostToolUse hook для auto-sentinel.** В `bridge/hooks.py::make_posttool_hooks(data_dir)` возвращаем `[HookMatcher(matcher="Write"), HookMatcher(matcher="Edit")]`: если `file_path` лежит внутри `skills/` или `tools/` — `touch data/run/skills.dirty`. Это полностью заменяет необходимость CLI `skill-creator` самому дёргать sentinel: любой Write/Edit модели в skills/tools авто-инвалидирует кэш. `ClaudeBridge._build_options` мержит PreToolUse + PostToolUse hook'и.
7. **Hot-reload скилов через sentinel-файл.** Phase 3 добавляет в `bridge/skills.py` публичные `invalidate_manifest_cache()` (сбрасывает `_MANIFEST_CACHE` dict) и `touch_skills_dir(skills_dir)` (делает `os.utime(skills_dir)` — bumps mtime так что phase-2 cache-key fires). `ClaudeBridge._render_system_prompt` в начале каждого query проверяет `data/run/skills.dirty` → вызывает оба helper'а + `unlink(sentinel)`.
8. **Тесты:** marketplace list/install happy-path (mock `gh api` subprocess), installer git-mock, installer ssrf-deny, path-traversal, size limits, URL detector, PostToolUse hook → sentinel, auto-bootstrap (mock subprocess → skill-creator появляется), bash allowlist для `gh api` и `git clone`.

## Критерии готовности

- Диалог "сделай скилл echo" → skill-creator installed через auto-bootstrap (direct Python — no model body-following) → model invoked `mcp__installer__skill_preview(url=<tree-URL>)` или напрямую использует skill-creator guidance → пишет `skills/echo/SKILL.md` + `tools/echo/main.py` через `Write` tool (phase-2 path-guard) → PostToolUse hook touch'ит sentinel → следующий turn видит `echo` в manifest.
- Диалог "какие есть скилы в marketplace" → model invokes `mcp__installer__marketplace_list` → Telegram sees `[{name, description}, ...]` → owner "поставь weather" → model `mcp__installer__marketplace_install(name="weather")` → preview text inlined в response → owner "да" → model `mcp__installer__skill_install(url=<cached-preview-url>, confirmed=true)` → atomic copy → async `uv sync` starts → `{"installed": true, "sync_pending": true}` → manifest обновлён на след. turn.
- Auto-bootstrap **не блокирует `Daemon.start()`** — старт завершается за <2 сек независимо от доступности GitHub; `skills/skill-creator/` появляется асинхронно. Fail → `log.warning skill_creator_bootstrap_failed` (или `_timeout` / `_exception`), бот продолжает работать.
- PostToolUse hook: Write в `skills/test/SKILL.md` → `data/run/skills.dirty` существует сразу после возврата Write'а.
- Bash allowlist:
  - `gh api /repos/anthropics/skills/contents/skills` → allow.
  - `gh api /repos/anthropics/skills/contents/skills/skill-creator/SKILL.md` → allow.
  - `gh auth status` → allow.
  - `gh api -X DELETE /repos/x/y` → deny.
  - `gh pr create ...` → deny.
  - `git clone --depth=1 https://github.com/x/y skills/y` → allow; `git clone --depth=1 https://... /tmp/x` → deny (path escape).
- Вредоносный SKILL.md с `../../../etc/passwd` — отклонён validator'ом.
- Bundle содержит symlink (любой, даже указывающий внутрь bundle'а) — отклонён validator'ом до `copytree`.
- TOCTOU detection: `preview <URL>` → bundle v1 → изменить источник → `install --confirm --url <URL>` → re-fetch видит bundle v2 → SHA-compare fail → `exit 7` с сообщением "bundle on source changed since preview".
- URL на `http://169.254.169.254/latest/meta-data` — SSRF guard режет fetch up-front.
- `<data_dir>/run/` sweeper при старте: `tmp/` >1ч и `installer-cache/` >7д удаляются.
- **@tool groundwork + dogfood installer (Q-D1=c + BL-1=A closeout):** `ClaudeAgentOptions.mcp_servers={"installer": <create_sdk_mcp_server>}` wired в `_build_options`. `src/assistant/tools_sdk/installer.py` defines `@tool("skill_preview"|"skill_install"|"skill_uninstall"|"marketplace_list"|"marketplace_info"|"marketplace_install"|"skill_sync_status")` — 7 tools total. Researcher spike RQ1 pass hermetically: (a) registered tools visible в SystemMessage(init).data["tools"] as `mcp__installer__*`, (b) model invokes any installer tool + receives ToolResultBlock, (c) PreToolUse hooks на Bash/Read/Write still fire when installer internals call Bash (e.g. `gh api` subprocess via `asyncio.create_subprocess_exec`) — hook intercepts Bash invocation regardless of parent `@tool` context.

## Closed architectural decisions (Q&A 2026-04-21)

| Q | Решение | Impact |
|---|---|---|
| Q-D1 | (c) @tool-decorator pivot | Phase 3 scope **уменьшается**: no D1 enforcement hooks. Groundwork `mcp_servers={}` in ClaudeAgentOptions. Phase 4 memory = @tool. Phase 8 gh = @tool. Researcher spike RQ1 в step 4 verifies `@tool`+`setting_sources` coexistence. |
| Q2 | All together | Marketplace + installer + @tool groundwork shipped in single phase-3 pass. |
| Q3 | Hardcoded marketplace | `MARKETPLACE_URL = "https://github.com/anthropics/skills"` — no env-var configuration. |
| Q4 | GitHub + gist + raw | `gh api` + `git clone --depth=1` + `httpx` for raw. SSRF guard bounded. |
| Q5 | Plain-text confirm | `install --confirm` через "да"/"нет" в Telegram. Inline keyboards — phase 8. |
| Q6 | URL detector all URLs | Regexp emit system-note per any URL. Hermetic test asserts note fires, model decides install. |
| Q7 | Fire-and-forget bootstrap | Anthropic's skill-creator auto-installed через background task 120s timeout. `Daemon.start()` не ждёт. |
| Q8 | gh required + git fallback | `shutil.which("gh")` None → log.error + disable marketplace subcommands; ad-hoc URL install через git clone. |
| Q9 *(pre-wipe; dogfood form in BL-1=A)* | Async uv sync | Per-tool `uv sync --directory` в background; `skill-installer status NAME` polls. **Dogfood form:** polling через `@tool("skill_sync_status", {"name": str})`. |
| Q10 *(pre-wipe; dogfood form in BL-1=A)* | Uninstall subcommand | `skill-installer uninstall NAME` = rmtree skills+tools + sentinel touch. ~30 LOC. **Dogfood form:** `@tool("skill_uninstall", {"name": str, "confirmed": bool})`. |
| Q11 | Sentinel: skills/+tools/ | PostToolUse(Write|Edit) triggers on both dirs. |
| Q12 | Cache TTL 7 days | `installer-cache/` old entries pruned на install + boot. |

## Зависимости

Phase 2 shipped (commit `fbd9c18`): `ClaudeBridge` streaming-input, manifest mtime-max cache, Bash/file/WebFetch PreToolUse hooks, sandboxed Write path-guard. Phase 3 **вводит** публичные `invalidate_manifest_cache` / `touch_skills_dir` helpers поверх phase-2 internal cache dict.

## Явно НЕ в phase 3

- Собственный `tools/skill-creator/` CLI — вместо него Anthropic's skill + Write.
- Inline-keyboard callback_query для preview confirm — phase 8 (ops polish).
- Оффлайн-кэш marketplace index.
- Множественные marketplaces — hardcoded один (`anthropics/skills`).
- `gh auth login` / auth flows — используем только read-only unauthenticated `gh api` (60 req/hour лимит).
- Sandbox для runtime выполнения установленных tools — future work.

## Риск

**Высокий (security).** Установка чужого кода = исполнение в Bash под полными правами модели. Phase 2 уже задизайнил strict argv-allowlist; сам акт создания файла (Write через Anthropic's skill-creator) sandboxed в `project_root`. Marketplace (`anthropics/skills`) — trust Anthropic (acceptable baseline); произвольные user-URL'ы — требуют preview+confirm. Митигации: preview+confirm обязателен; повторный fetch + SHA-compare при `install` (TOCTOU); static validate (AST + frontmatter schema); **symlinks в bundle отклоняются безусловно** до `copytree`; SSRF guard; size/count/timeout limits; schemes whitelist; `gh` read-only; `git clone --depth=1` без LFS; `gh` не установлен → marketplace-функционал отключается, бот живой.

**Известный baseline:** у Anthropic'ового `skill-creator` SKILL.md в frontmatter **нет** `allowed-tools`. Наш loader трактует отсутствие поля как "permissive" (передаёт полный набор `["Bash","Read","Write","Edit","Glob","Grep","WebFetch"]` в SDK) + `log.warning skill_permissive_default`. Per-skill allowed-tools НЕ единственная защита: phase 2 PreToolUse hooks (path-guard, Bash allowlist, WebFetch SSRF) срабатывают независимо от manifest'а и остаются универсальным defense-in-depth.
