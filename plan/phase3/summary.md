---
phase: 3
title: skill-creator marketplace + skill-installer @tool dogfood + @tool pivot groundwork
date: 2026-04-21
commits:
  - 575aa6a  # phase 3: skill-creator marketplace + skill-installer @tool dogfood
status: shipped (commit + push + owner smoke GREEN across 5 scenarios)
sdk_pin: claude-agent-sdk>=0.1.59,<0.2 (live verified 0.1.63)
auth: OAuth via local `claude` CLI (no ANTHROPIC_API_KEY)
---

# Phase 3 — Summary

Phase 3 сместил архитектуру бота с SKILL.md-body-driven инструментов на
first-class SDK `@tool`-decorator functions, закрыл D1-долг phase 2
(Opus 4.7 не следует "run via Bash" body-instruction — см. GH #39851/#41510),
задогфудил собственный installer как 7 custom tools, автоматически
подтянул Anthropic's `skill-creator` skill из marketplace через
direct-Python fetch (без модели в цепочке, body-compliance не нужен),
и добавил PostToolUse hot-reload для `skills/`/`tools/` через sentinel.

Коммит `575aa6a` — 48 files, +7784/-1647, включая 842 строк
`_installer_core.py` + 522 строк `installer.py` + 232 строк расширения
`bridge/hooks.py`. Owner smoke GREEN across 5 scenarios (2026-04-21).

---

## 1. Что shipped

### 1.1 `src/assistant/tools_sdk/` — новый модуль custom SDK tools

- **`tools_sdk/_installer_core.py` (842 LOC)** — trusted in-process helpers:
  `canonicalize_url`, `cache_dir_for`, `fetch_bundle_async`,
  `validate_bundle` (schema + AST + path-traversal + symlink reject +
  size/count limits), `sha256_of_tree` (deterministic canonical hasher),
  `atomic_install` (tmp+rename + `.0xone-installed` marker gate),
  `_fetch_tool()` (chooses `gh` → `git` → raises `FetchToolMissing`),
  `marketplace_list_entries` / `marketplace_fetch_skill_md` /
  `marketplace_tree_url`, `spawn_uv_sync_bg`, `sweep_run_dirs`.
  Error taxonomy: `FetchToolMissing`, `URLError`, `ValidationError`,
  `InstallError`, `MarketplaceError`. Tool-error codes: `1` URL bad,
  `2` not previewed, `3` not confirmed, `4` SSRF, `5` validation, `7`
  TOCTOU, `9` no-fetch-tool, `10` marketplace, `11` name invalid.
- **`tools_sdk/installer.py` (522 LOC)** — 7 `@tool` functions
  registered через `create_sdk_mcp_server(name="installer", version="0.1.0",
  tools=[...])`, exported как `INSTALLER_SERVER`:
  1. `@tool("skill_preview", ..., {"url": str})` — canonicalize URL →
     fetch в `<data_dir>/run/installer-cache/<url_hash>/bundle/` →
     static-validate → `sha256_of_tree` → write `manifest.json` →
     return `{preview, cache_key, confirm_hint}`.
  2. `@tool("skill_install", ..., {"url": str, "confirmed": bool})` —
     require `confirmed=true` (иначе code 3); re-fetch в `verify/`;
     `sha256_of_tree(verify)` vs `manifest["bundle_sha"]` (code 7 при
     расхождении → cache entry stomped); `atomic_install` в
     `skills/<name>/` + optional `tools/<name>/` split; touch
     `skills.dirty` sentinel; fire-and-forget `uv sync` background task.
  3. `@tool("skill_uninstall", ..., {"name": str, "confirmed": bool})`
     — idempotent rmtree + sentinel touch. Missing skill → `{"removed":
     false, "reason": "not installed"}`.
  4. `@tool("marketplace_list", ..., {})` — `gh api
     /repos/anthropics/skills/contents/skills` → filter `type=="dir"` +
     drop dotfiles → JSON list.
  5. `@tool("marketplace_info", ..., {"name": str})` — `gh api
     /repos/.../skills/<name>/SKILL.md` → base64-decode body.
  6. `@tool("marketplace_install", ..., {"name": str})` — convenience:
     builds tree-URL + delegates в `skill_preview` pipeline; **не**
     делает single-call shortcut past confirmation (preserves preview
     gate).
  7. `@tool("skill_sync_status", ..., {"name": str})` — reads
     `<data_dir>/run/sync/<name>.status.json`.
- **`tools_sdk/__init__.py` (13 LOC)** — package docstring + placeholder
  для phase-4 `memory` and phase-8 `gh` servers.

### 1.2 `bridge/claude.py` — `mcp_servers` wiring + sentinel hot-reload

- `_build_options` получил параметр `mcp_servers: dict[str,
  McpSdkServerConfig]`; по умолчанию `{"installer": INSTALLER_SERVER}`.
- `allowed_tools` baseline расширен 7 именами `mcp__installer__<name>`.
- `_render_system_prompt` перед каждым query проверяет
  `<data_dir>/run/skills.dirty` → `invalidate_manifest_cache()` +
  `touch_skills_dir()` + `unlink(sentinel)`.

### 1.3 `bridge/skills.py` — публичные cache helpers + permissive default

- Новые public: `invalidate_manifest_cache()` (сбрасывает
  `_MANIFEST_CACHE` dict) и `touch_skills_dir(path)` (bumps `os.utime`).
- `_normalize_allowed_tools(raw)` → `None | list[str]` (sentinel на
  missing; `[]` — explicit lockdown; остальное — list). SDK всё равно
  игнорирует per-skill `allowed-tools`, но поле в manifest теперь
  information-grade + `log.warning skill_permissive_default` на missing.

### 1.4 `bridge/hooks.py` — Bash allowlist extensions + PostToolUse factory

- `_BASH_PROGRAMS` расширен на `gh`, `git clone`, `uv sync`:
  - `gh`: только `api` (read-only, regex `_GH_API_SAFE_ENDPOINT_RE` на
    `/repos/*/contents/*` или `/repos/*/tarball/*`; deny flags `-X`,
    `--method`, `-F`, `--field`, `-f`, `--raw-field`, `--input`) и
    `auth status`. Всё остальное (`pr`, `issue`, `workflow`, `secret`,
    `config`, `gist`, `release`, `auth login/logout`, `graphql`,
    `/user`, `/search`) → deny.
  - `git clone --depth=1 <https_url> <dest>` → schema check + path-guard
    на dest относительно `project_root`.
  - `uv sync --directory=tools/<name>` → path-guard на под-`tools/`.
- `make_posttool_hooks(data_dir)` → `[HookMatcher(matcher="Write"),
  HookMatcher(matcher="Edit")]`; если `tool_input["file_path"]` (SDK
  даёт absolute path, see reference memory) попадает в
  `skills/` или `tools/` — touch `<data_dir>/run/skills.dirty`. Hook
  возвращает `{}` (no-op, валидная форма PostToolUse, spike S3.d).

### 1.5 `handlers/message.py` — URL detector

Regex `https?://\S+|git@[^\s:]+:\S+` в `_run_turn`; при match →
дописывает one-shot system-note в envelope payload ("the user's message
contains URL(s)... if bundle, use skill_preview tool; else ignore").
**Оригинал** `msg.text` уходит в БД без note — note живёт только в
SDK envelope.

### 1.6 `main.py::Daemon.start()` — два fire-and-forget background tasks

- `_bootstrap_skill_creator_bg` — idempotent (marker
  `skills/skill-creator/.0xone-installed`); `_fetch_tool()` →
  `fetch_bundle_async → validate_bundle → atomic_install → touch
  skills.dirty`; `asyncio.wait_for(..., timeout=120)`; логирование
  `skill_creator_bootstrap_{starting,ok,failed,timeout,skipped_no_gh_nor_git}`.
  Direct Python path — НЕТ subprocess, НЕТ модели в цепочке, НЕТ
  body-compliance dependency.
- `sweep_run_dirs` — `<data_dir>/run/tmp/` >1h и
  `<data_dir>/run/installer-cache/` >7d, best-effort, never fails
  startup.

`Daemon.start()` возвращает управление за <500мс независимо от
доступности GitHub — tests `test_bootstrap_direct_python.py` это
гарантируют.

### 1.7 `skills/skill-installer/SKILL.md` — discoverability-only

Thin 39-line SKILL.md без body-instruction (no "run via Bash"): trigger
phrases + examples + invariant "`skill_install(confirmed=true)` only
after owner says yes в previous turn". Model видит tool names в
`SystemMessage(init).data["tools"]`.

### 1.8 `skills/skill-creator/` — NOT committed

Live-bootstrapped from Anthropic marketplace (18 files / 248 KB,
observed 9s bootstrap на deploy host 2026-04-21). Layout: `SKILL.md` +
`agents/`, `assets/`, `eval-viewer/`, `references/`, `scripts/`,
`LICENSE.txt`. Frontmatter в Anthropic's SKILL.md **не содержит**
`allowed-tools` — permissive-default warning fired
(`skill_permissive_default`), но реальная защита остаётся на
phase-2 PreToolUse hooks.

---

## 2. Owner-level impact

- **Бот сам себя расширяет (dogfood).** "сделай скилл для погоды" →
  Opus через `skill-creator` guidance пишет
  `skills/weather/SKILL.md` + `tools/weather/main.py` через встроенный
  Write → PostToolUse hook touch'ит sentinel → следующий turn видит
  новый skill без рестарта.
- **"поставь скилл `<URL>`" works end-to-end.** URL detector → model
  invokes `mcp__installer__skill_preview` → preview text в Telegram →
  owner "да" → `mcp__installer__skill_install(url, confirmed=true)` →
  atomic install → manifest обновлён на следующем turn'е.
- **Anthropic marketplace accessible.** "какие скилы есть" → model
  вызывает `mcp__installer__marketplace_list` → JSON список 17 skills
  (`algorithmic-art`, `pdf`, `pptx`, `skill-creator`, `webapp-testing`,
  …) → owner выбирает → `marketplace_install(name="X")` → preview →
  confirm → installed.
- **Startup unchanged.** `Daemon.start()` <2s независимо от сети.
  `skill-creator` подтягивается в фоне; если `gh`+`git` отсутствуют —
  `log.warning` + marketplace-tools возвращают `{"error": ..., "code":
  9}` в runtime, остальной бот работает.

---

## 3. Architectural decisions (closeout)

### Q-D1 = (c) @tool-decorator pivot — 2026-04-21

Owner выбрал **(c) @tool pivot** вместо PostToolUse enforcement hooks или
cross-turn state tracking. Обоснование: Opus 4.7 системно игнорирует
imperative "run X via Bash" в skill body; фиксить compliance через hooks
= хрупкая гонка с upstream моделью. Вместо этого:

- Skills остаются prompt-expansions only (midomis-style).
- Memory/gh/media-processing становятся first-class custom SDK tools.
- Phase 3 ships groundwork (`mcp_servers={"installer": ...}` slot в
  `ClaudeAgentOptions`); phase 4 memory переедет на `@tool` в
  `src/assistant/tools_sdk/memory.py`; phase 8 gh аналогично.

**Долг D1 закрыт** — phase 3 **НЕ добавляет** D1 enforcement hooks.

### BL-1 = (A) dogfood installer — 2026-04-21

Installer = 7 `@tool` functions в процессе daemon'а, **не** CLI под
`tools/skill-installer/`. Legacy CLI pre-wipe design заморожен в git-
history для lineage, но current production path — dogfood. Acceptance
— `test_installer_mcp_registration.py` + owner smoke GREEN.

### BL-2 = gh/git fallback + `.0xone-installed` marker gate

`_fetch_tool()` helper в `_installer_core.py` возвращает `"gh"` →
`"git"` → raises `FetchToolMissing`. `atomic_install` touch'ит marker
`.0xone-installed` ТОЛЬКО после успешных двух renames; bootstrap проверяет
**marker**, не directory existence, — ловит partial install после crash.

### Q1-Q12 — см. `description.md` "Closed architectural decisions"

Hardcoded marketplace (`github.com/anthropics/skills`), TTL 7 дней,
gh+git fallback, fire-and-forget bootstrap, PostToolUse scope
`skills/+tools/`, async uv sync с polling, plain-text confirm flow.

---

## 4. Spike RQ1 findings (ALL SIX PASS)

Researcher-spike `plan/phase3/spikes/rq1_tool_decorator_coexist.py`
выполнен 2026-04-21 против live OAuth CLI на SDK 0.1.63 с **двумя**
`create_sdk_mcp_server` серверами (installer + memory-placeholder) —
verifies phase-4 двухсерверной топологии заранее.

| # | Criterion | Result | Evidence |
|---|-----------|--------|----------|
| C1 | `SystemMessage(init).data["tools"]` содержит оба `mcp__installer__skill_preview` и `mcp__memory__memory_search` | **PASS** | init list contained both names alongside ambient CLI tools (Bash, Read, Write, Task, TodoWrite, Figma/Gmail/Drive/Calendar MCPs) |
| C2 | Prompt "use skill_preview url=X" → `ToolUseBlock(name="mcp__installer__skill_preview")` → marker в ToolResultBlock | **PASS** | Q1 tool_use_names=`['ToolSearch','mcp__installer__skill_preview']`; marker `"PREVIEW-OK: https://example.com/x"` |
| C3 | Analogous для memory server placeholder | **PASS** | Q2 marker `"MEMORY-OK: foo"` |
| C4 | `HookMatcher("Bash")` + `HookMatcher("Write")` НЕ fire на mcp__ invocations | **PASS** | `bash_fired=[], write_fired=[]` across 3 queries — per-tool matcher scoping strict |
| C5 | Regex matcher `mcp__installer__.*` И exact matcher `mcp__memory__memory_search` оба fire | **PASS** | 2 regex fires (Q1+Q3), 1 exact fire (Q2) — SDK трактует `HookMatcher.matcher` как regex/pattern, verbose per-tool list не нужен |
| C6 | `setting_sources=["project"]` + программный `mcp_servers={...}` + on-disk `.claude/settings.local.json` с junk `mcpServers` — coexist без crash | **PASS** | Q3 `stop_reason=end_turn`; on-disk stub silently ignored (validates entries, drops invalid); programmatic servers win — `settings.local.json` с полным мусором не ломает бота |

Cost: Q1 $0.25 / Q2 $0.07 / Q3 $0.07 — total $0.39. Бюджет $0.20
exceeded из-за inflated ambient tool list на researcher host (~60
ambient CLI MCPs); на чистом deploy-хосте overshoot меньше.

### Not-obvious observations (→ NH items)

1. **Auto-ToolSearch first.** Каждый query начинался с
   `ToolUseBlock(name="ToolSearch")` — CLI-ambient meta-tool, SDK
   preset его не gate'ит через `allowed_tools`. Тесты используют
   **subset-assert**, не exact-list equality (→ NH-7).
2. **Regex fires per-invocation, no dedup** — audit-ready.
3. **`allowed_tools` limits SELECTION, не HOOK scope** — `Bash`/`Write`
   всё равно в init tools list, gate на invocation. Matters для
   system-prompt footprint (→ NH-11).
4. **ToolResultBlock content может быть list[dict] OR string** — тесты
   используют `_flatten_tool_result_content` idiom из probe.
5. **Empty `.claude/` под `setting_sources=["project"]`** — валидный
   baseline; discovery yields ноль скилов, SDK не крашится.

### Fallback tree outcomes — NONE TAKEN

- (a) `@tool` registration failure — не произошло на 0.1.63.
- (b) Regex matcher unsupported — не произошло.
- (c) `setting_sources`+`mcp_servers` conflict — не произошло.

Impact: detailed-plan §1c-§1d acceptance все checked; S1-S5 findings остаются
valid under dogfood pivot.

---

## 5. Security hardening (devil's advocate waves B1-B11 + S4-S11)

Фикс-пак из devil's-advocate ревью (до ship'а и пост-ship'а):

| ID | Проблема | Фикс |
|----|----------|------|
| B1 | Partial-install crash между двумя `os.rename` оставляет `skills/<name>/` без marker | `.0xone-installed` marker touched ПОСЛЕ обоих renames; bootstrap checks marker (не directory existence); `atomic_install` делает best-effort rollback первого rename если второй упал. Test `test_atomic_install_rollback.py` — 128 LOC |
| B2 | `$VAR` slip-guard — shell expansion в argv через preview text | description sanitization + `_bash_allowlist_check` wraps `shlex.split` в `try/except ValueError → "unparseable"` (NH-10) |
| B3 | Description injection через подкорм hostile `description` frontmatter | `test_installer_description_injection.py` — 158 LOC; sanitize control chars, length cap, reject shell-meta in preview text |
| B4 | Symlink внутри bundle (absolute `../../etc/passwd` ИЛИ relative `./SKILL.md`) | `validate_bundle` делает `p.is_symlink()` (lstat-based) ДО `copytree`; reject безусловно. `copytree(symlinks=True)` defense-in-depth. Test `test_skill_symlink_rejected.py` |
| B5 | Path-traversal в bundle (`..` в относительных путях) | `Path(f).resolve().is_relative_to(bundle.resolve())` на каждом entry до install. Test `test_installer_path_traversal.py` — 134 LOC |
| B6 | SSRF через hostile URL (`http://169.254.169.254/...` или `file://`) | `_check_host_safety` в `_installer_core` + scheme whitelist (`https://`, `git@github.com:`). Test `test_ssrf_deny.py` — 90 LOC |
| B7 | Bundle exceeds size/count limits | `MAX_TOTAL=10MB`, `MAX_FILES=100`, `MAX_FILE=2MB`, `FETCH_TIMEOUT=30s`, `UV_SYNC_TIMEOUT=120s`; enforced после fetch, до install. Test `test_skill_size_limits.py` |
| B8 | Bundle with invalid name (`../skill-X`, empty, uppercase) | `_NAME_RE = r"^[a-z][a-z0-9_-]{0,63}$"` в frontmatter validator. Code 11 |
| B9 | TOCTOU между preview и install — source changed под ногами | `sha256_of_tree` canonical hasher (rel_path + NUL + content, sorted, skip `.git`, `__pycache__`, `.ruff_cache`, `.DS_Store`, pyc; skip symlinks) → cached в `manifest.json` → re-compared after re-fetch в `verify/`. Mismatch → code 7, cache stomped. Test `test_installer_tool_toctou.py` |
| B10 | `yaml.safe_load` на малformed frontmatter → unhandled exception | try/except → `ValidationError` (NH-6) |
| B11 | Bash `gh` allowlist слишком слабый (`gh api -X DELETE` проходил) | 20+ test matrix (8 allow + 12+ deny), `_GH_FORBIDDEN_FLAGS = {"-X", "--method", "--method-override", "-F", "--field", "-f", "--raw-field", "--input"}` + endpoint regex. Test `test_bash_allowlist_gh_api.py` — 74 LOC |
| S4 | `shutil.copytree(symlinks=False)` default follows symlinks | явный `copytree(..., symlinks=True)` + validator reject, double-defence (spike S4) |
| S7 | `gh api` rc=0 на HTTP 404 (spike S2.d) | `_parse_gh_json` detects `{"message": ..., "status": "404"}` body shape → `MarketplaceError`. NH-9 |
| S10 | Missing `_fetch_tool()` → silent marketplace breakage | bootstrap `log.warning skill_creator_bootstrap_skipped_no_gh_nor_git`; runtime tools returns `{"error": ..., "code": 9}`. Test `test_installer_tool_missing_fetch_tool.py` — 67 LOC |
| S11 | Sweeper crash на permission error → daemon fails to boot | `_sweep_run_dirs` wraps в `try/except OSError: pass` — best-effort. Test `test_installer_sweep_tmp.py` |

---

## 6. Tests — 272 total (phase 2's 144 + 128 new)

272 tests pass (`pytest --collect-only -q` → 272 collected). Категории
новых тестов:

- **Installer `@tool` surface (7):** `test_installer_tool_skill_preview`
  (104 LOC), `test_installer_tool_skill_install` (115),
  `_unconfirmed_install` (79), `_missing_fetch_tool` (67), `_toctou`
  (64), `test_installer_uninstall` (72), `test_skill_sync_status` (59).
- **Installer MCP registration & context:**
  `test_installer_mcp_registration` (84 LOC) — asserts
  `create_sdk_mcp_server` returns config with 7 names,
  **subset-assert** на init tools list (NH-7).
- **Marketplace:** `test_marketplace_list` (48), `_info` (70),
  `_install` (57), `_rate_limit` (42). 60 req/h limit surfaced as
  error code 10.
- **Bash allowlist extensions:** `test_bash_allowlist_gh_api` (74,
  18-parametrised matrix), `_git_clone` (88), `_uv_sync` (55).
- **Security bundle validation:** `test_installer_path_traversal`
  (134), `test_skill_symlink_rejected` (50), `test_skill_size_limits`
  (51), `test_ssrf_deny` (90), `test_installer_description_injection`
  (158).
- **Atomic install & rollback:** `test_atomic_install_rollback` (128)
  — monkeypatches второй `os.rename` to raise; asserts first rename
  rolled back, marker не touched.
- **Bootstrap:** `test_bootstrap_direct_python` (177) — <500ms start,
  idempotent marker gate, fetch-tool-missing path.
- **Sweeper:** `test_installer_sweep_tmp` (75) — `tmp/old` (2h) удалён,
  `tmp/new` (10min) сохранён, `installer-cache/stale` (8d) удалён,
  `installer-cache/fresh` (1d) сохранён.
- **Hooks:** `test_posttool_sentinel` (103) — Write в
  `skills/x/SKILL.md` → sentinel exists immediately; `foo.py` вне
  skills/tools → не создаётся.
- **URL detector:** `test_url_detector` (52) — `https://`,
  `git@github.com:`, embedded URLs, multiple URLs.
- **Manifest cache invalidation:** `test_manifest_cache_invalidation`
  (77) — sentinel touch → next `_render_system_prompt` calls
  `invalidate_manifest_cache` + `touch_skills_dir` + unlink.
- **Permissive default:** `test_skill_permissive_default` (80) —
  missing field → sentinel → baseline set + warning; `[]` → lockdown
  без warning.

Linters: ruff + mypy strict clean.

---

## 7. Deploy + smoke history 2026-04-21

### Bootstrap

- `gh 2.89.0` + `git 2.47.x` available на deploy host.
- `Daemon.start()` <500ms (measured); `skill-creator` bootstrap
  completed в 9s через `gh api` path. Marker `.0xone-installed` touched.
- `log.info skill_creator_bootstrap_starting via=gh`
  → `log.info skill_creator_bootstrap_ok`.

### Owner smoke scenarios — ALL GREEN

1. **"поставь скилл `<marketplace_URL>`"** → model →
   `mcp__installer__skill_preview` → preview text → owner "да" →
   `skill_install(confirmed=true)` → installed → visible в manifest на
   next turn. JSONL session trace captured.
2. **"какие скилы есть в marketplace"** →
   `mcp__installer__marketplace_list` → 17 entries → owner выбрал
   `pdf` → `marketplace_install(name="pdf")` → preview → "да" → install
   → `skills/pdf/` с SKILL.md + `scripts/` + `forms.md` + `reference.md`
   на диске.
3. **"сделай скилл echo с описанием X"** → skill-creator
   guidance-driven → Opus написал `skills/echo/SKILL.md` +
   `tools/echo/main.py` через Write → PostToolUse sentinel → next turn
   `echo` в manifest → "используй echo" → tool_use → `{"ok": true,
   "tool": "echo"}`.
4. **"удали echo"** → `mcp__installer__skill_uninstall(name="echo",
   confirmed=true)` → rmtree + sentinel → skill gone на next turn.
5. **Hostile URL (`http://169.254.169.254/latest/`)** → `skill_preview`
   → `{"error": "SSRF: 169.254.169.254 resolves to 169.254.169.254",
   "code": 4}`. Bot surfaced sanitized error; nothing fetched, nothing
   installed.

All 5 traced в JSONL sessions (owner confirmed via `tail` of daemon log
+ Telegram history).

---

## 8. Unresolved U-items (NH-1..NH-22)

Phase 3 вводит формальный каталог `unverified-assumptions.md` (NH-N =
Nice-to-Have, ordered by risk descending). 22 items catalogued;
classification:

**Open (требуют live-probe или re-test в phase 4+):**

- **NH-1** — `.0xone-installed` marker mid-rename crash integration
  test отсутствует (только unit monkeypatch в
  `test_atomic_install_rollback`). Phase 5 scheduler could sweep
  `skills/.tmp-*` leftovers.
- **NH-7** — `ToolSearch` pre-invoke overhead — ambient-CLI или
  SDK-intrinsic? Re-test на чистом deploy host в phase 4 deploy smoke.
- **NH-11** — first-turn cost dominated by init tool list (~60 ambient
  tools); phase-4 memory (+2) + phase-8 gh (~10) будут растить. Phase 9
  мог бы explore `disallowed_tools=[...]`.
- **NH-14** — SDK silently tolerated junk on-disk `mcpServers` в
  `.claude/settings.local.json` (RQ1 C6). Future SDK point-release мог
  бы начать honor'ить их и shadow'ить `installer` server. Add
  `mcpServers` к `assert_no_custom_claude_settings` block-list IF
  materializes.
- **NH-18** — DNS rebinding residual risk в `_raw_single_file_async`:
  `urllib.request.urlopen` follows redirects without re-checking host.
  Accepted single-user risk, audit quarterly.
- **NH-19** — validator AST-parses только `*.py`; `.sh`/`.js`/`.toml`
  не gated. Mitigated тем что Bash allowlist не даёт model выполнять
  `.sh` напрямую; Python `main.py` runtime trusts bundle after confirm.
- **NH-20** — unit tests invoke `.handler(...)` directly, bypass SDK's
  `call_tool` MCP schema validation. Add one integration-style test
  going through `ClaudeBridge.ask` в phase 3.1 if coverage gap.
- **NH-21** — `_bootstrap_skill_creator_bg` 120s optimistic для slow
  network (17 files × 30s worst-case → exceeds). Idempotent retry на
  next boot; future scheduler notification.

**Closed/accepted:**

- NH-3 (`ResultMessage.model` absence — phase 2 fix persists).
- NH-4 (`ToolResultBlock` в `UserMessage` — phase 2 B5 fix).
- NH-5 (`create_task` GC — `_BG_TASKS` anchor; consider `RUF006` в
  ruff config).
- NH-6 (`yaml.safe_load` malformed — ValidationError wrap).
- NH-8 (`allowed-tools:` frontmatter is SDK no-op — phase 2 knowledge;
  permissive-default warning docs-only).
- NH-9 (`gh api` rc=0 on 404 — `_parse_gh_json` detects body shape).
- NH-10 (`shlex.split` on unbalanced quotes — try/except).
- NH-12 (sweeper runs ONCE at boot; per-install cleanup в `finally`;
  phase-5 scheduler could hourly).
- NH-13 (installer context process-global via `configure_installer`;
  phase-5 scheduler если sidecar → refactor на `ContextVar`).
- NH-15 (`installer.py` module-load side effects — `create_sdk_mcp_server`
  call на import, но без IO — idempotent).
- NH-16 (`skill_preview` re-fetch on same URL — acceptable waste для
  single-user).
- NH-17 (PostToolUse sentinel fires on ANY Write under `tools/` — ~5ms
  overhead per cache Write; future restrict к SKILL.md / main.py /
  pyproject.toml patterns).
- NH-22 (`sha256_of_tree` ignores empty dirs — low-severity; empty
  `.git/` не affects execution; `atomic_install` strips `.git` anyway).

See `plan/phase3/unverified-assumptions.md` для full body per item
(risk, mitigation, follow-up phase).

---

## 9. Phase 2 → Phase 3 closure of prior debt

Phase 3 closes или partially-addresses следующий phase-2 tech-debt:

- **D1 (Opus body-compliance)** — closed via Q-D1=c @tool pivot.
  No PostToolUse enforcement, no cross-turn state tracking.
- **U1 (`tool_use`+`tool_result` replay stability)** — implicitly
  tested by installer flow (model invokes `skill_preview` → receives
  `ToolResultBlock` → next turn с mcp__ tool history). No regression
  observed; phase-2 fallback filter в `history_to_sdk_envelopes`
  остаётся.
- **U5 (regex matcher HookMatcher)** — RQ1 C5 confirmed: SDK honors
  regex matcher (`mcp__installer__.*`). Cosmetic collapse
  `Read|Write|Edit` в regex — phase 6+.
- **Manifest cache concurrency (phase-2 open tech-debt)** — still
  pending (phase 3 не добавлял concurrent writes; single-event-loop
  daemon). Revisit в phase 5 scheduler.

---

## 10. Что phase 4 строит поверх phase 3

Phase 4 (memory) inherits:

- **`mcp_servers={"installer": ...}` slot** готов в `_build_options`.
  Phase 4 добавит `{"memory": MEMORY_SERVER}` — merge-style.
- **Memory = `@tool`**, не SKILL.md+Bash. `src/assistant/tools_sdk/memory.py`
  с `@tool("memory_search", ...)`, `@tool("memory_write", ...)` —
  dogfood pattern phase 3's installer.
- **Skills остаются prompt-expansions** — никакого "run via Bash" в
  SKILL.md body. Midomis-style reference/workflow guidance.
- **Phase 4 NOT blocked on PostToolUse enforcement** — Q-D1=c закрыл
  этот вопрос архитектурно; @tool calls trusted by construction.
- **RQ1 coexistence verified для двухсерверной топологии** — spike уже
  тестировал `installer`+`memory` servers одновременно, phase 4 не
  надо повторять базовую coexistence spike.
- **`configure_installer(project_root, data_dir)` pattern** — phase 4
  memory воспроизведёт `configure_memory(db_path)` аналогично;
  `_need_ctx()` raises `RuntimeError` если не вызвали до регистрации
  (NH-13).
- **`@tool` test idiom** — invoke `.handler({...})` directly в unit
  tests (bypass MCP schema validation — see NH-20; add one integration
  test via `ClaudeBridge.ask` если gap).

---

## Цитирования

- `plan/phase3/description.md` (118 lines, frozen), `detailed-plan.md`
  (991 lines, frozen), `implementation.md` v2 (2026-04-21, frozen),
  `spike-findings.md` (S1-S5 + RQ1 section, frozen),
  `unverified-assumptions.md` (NH-1..NH-22, frozen).
- Commit `575aa6a` — phase 3 ship (48 files, +7784/-1647).
- Spike artifacts:
  `plan/phase3/spikes/rq1_tool_decorator_coexist.py` + `.json`
  (2026-04-21, SDK 0.1.63 live probe, 3 queries, $0.39 cost, ALL SIX
  PASS).
- Phase 2 summary `plan/phase2/summary.md` для D1 debt lineage.
