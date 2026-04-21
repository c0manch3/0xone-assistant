# Phase 3 — Detailed Plan

## Вопросы для обсуждения (закрываем до coder-кода)

| # | Вопрос | Recommended | Альтернативы |
|---|---|---|---|
| Q1 | `skill-creator` runtime — stdlib `argparse` или `typer`-проект? | **Одиночный `tools/skill-creator/main.py` на stdlib** (argparse + `pathlib` + re). Phase 2 Q7 уже закрыл принцип: smoke-инструменты — stdlib; `typer` завозить только если команд >5 и нужны cli-UX плюшки. | (a) `uv`-проект с `typer` — красивее, но +зависимость, +`uv sync` на старте проекта. (b) `click` — middle ground. |
| Q2 | `skill-installer` — GitHub через `gh` CLI или `httpx`+raw? | **`httpx` + GitHub REST API (`api.github.com/repos/{owner}/{repo}/contents/{path}`)** + raw.githubusercontent.com для tree-URLов. Anonymous (60 req/hour лимит — хватит одному юзеру). `gh` требует логина и добавляет в allowlist новую бинарь. | (a) `gh api` — проще URL parsing, но нужен `gh auth`. (b) `git clone --depth=1 --filter=tree:0` на всё — оверкилл для скилла в подпапке. |
| Q3 | git clone — `subprocess git` или `GitPython`? | **`subprocess git` через новую запись Bash-allowlist'а** (`git clone --depth=1 <https_url>` с schema-check). `GitPython` — +8 MB deps ради `clone`. | (a) `GitPython` — Pythonic API, но избыточно. (b) `dulwich` — pure-python, но медленнее. |
| Q4 *(pre-wipe historical — CLI framing; dogfood equivalent in BL-1=A + §4/§7)* | Preview UX — текстовый "да/нет" или inline-keyboard? | **Текстовый в phase 3.** Installer печатает preview в stdout → bridge отправляет в Telegram → пользователь отвечает "да" / "yes" / "подтверждаю" **следующим сообщением** → модель в следующем turn'е повторяет `skill-installer install --confirm --url <URL>`. Итоговая схема — **cache-by-URL + re-fetch с SHA-compare** (Вариант C, решено пользователем): cache dir `<data_dir>/run/installer-cache/<sha256(url)[:16]>/`, на `install` installer повторно fetch'ит bundle и сравнивает `sha256_of_tree(bundle)` с закэшированным. Расхождение → `exit 7` "bundle on source changed since preview; re-run `preview`". Inline-keyboard — phase 8. | (a) inline-keyboard — один-тап UX, но +CallbackQueryHandler, +routing, +state per message_id. (b) `--bundle-sha` как аргумент — отклонён: sha видна только в stdout preview, модель легко теряет её между turn'ами. |
| Q5 | Лимиты installer'а | **`MAX_FILES=100`, `MAX_TOTAL_SIZE=10 MB`, `MAX_SINGLE_FILE=2 MB`, `FETCH_TIMEOUT=30s`, `UV_SYNC_TIMEOUT=120s`.** Считаются до распаковки. | Мягче (50 MB) / жёстче (1 MB) — цифры можно крутить потом. |
| Q6 | Sanity-run `tools/<name>/main.py --help` перед copy? | **Нет в phase 3.** Static-validate: frontmatter schema + AST-parse main.py (нет синтакс-ошибок, есть `if __name__ == "__main__"`). Запускать чужой код **до** install'а = дать ему выполниться вне sandbox'а = противоречит principle "preview+confirm". Runtime-валидацию откладываем в phase 8 + sandbox. | (a) Sanity-run — быстрее выявит broken скилы, но **security hole**. |
| Q7 | Где tmpdir для download'а | **`tempfile.mkdtemp(prefix="0xone-install-", dir=<data_dir>/run/tmp/)`** + `finally: shutil.rmtree` (even on success — после copy). Не системный `/tmp` — data_dir гарантирует same FS с `skills/` для atomic rename. Cleanup TTL: при старте бота `Daemon.start()` чистит `<data_dir>/run/tmp/` старше 1 часа. | (a) Системный `/tmp` — cross-FS rename → copy+unlink не atomic. (b) `<project_root>/.installer-cache` — в project_root не хочется мусора. |
| Q8 *(pre-wipe historical — CLI framing; dogfood equivalent in BL-1=A)* | `uv sync` для `tools/<name>/` — sync или async? | **Async via `asyncio.create_subprocess_exec` с таймаутом 120 сек.** CLI-команда возвращает немедленно с `status=pending`; прогресс через `data/run/skills.dirty.sync-<name>.log`. Модель в следующем turn'е может дёрнуть `skill-installer status <name>`. Для первой версии — если скилл без `pyproject.toml` (stdlib-only как ping) — `uv sync` не нужен вовсе. | (a) Sync 30-60 сек — блокирует handler + user видит typing 1 минуту. (b) Background task без статус-API — модель не узнает о завершении. |

### Приложение: дополнительные вопросы (закрыты в интерактивном обсуждении)

| # | Вопрос | Решение |
|---|---|---|
| M1 *(pre-wipe historical — CLI framing; dogfood equivalent in BL-1=A + §4d)* | Откуда брать готовые скилы: отдельный marketplace URL в конфиге, хардкод или несколько? | **Hardcoded константа `MARKETPLACE_URL = "https://github.com/anthropics/skills"`** в `tools/skill-installer/_lib/marketplace.py`. Конфигурабельность откладываем. |
| M2 *(pre-wipe historical — CLI framing; dogfood equivalent in BL-1=A + §4d)* | Marketplace discovery — отдельный скилл или подкоманды существующего installer'а? | **Подкоманды `marketplace list / info / install` в `tools/skill-installer/main.py`** — не отдельный скилл. Меньше surface. |
| M3 *(pre-wipe historical — direct-Python bootstrap in BL-1=A + §6)* | Поведение при отсутствии Anthropic's `skill-creator` локально на старте | **Auto-install молча при первом старте** (`Daemon.start()` → `marketplace install skill-creator --confirm`); fail → `log.warning`, не блокирует. Owner ничего про это в Telegram не видит. |

**ВСЕ Q1-Q8 + M1-M3 ЗАКРЫТЫ пользователем в интерактивном обсуждении.** Отклонения от Recommended: **Q1** — не пишем свой `tools/skill-creator/` вовсе, вместо него ставим Anthropic's skill через marketplace + модель пишет через Write (sandboxed phase 2); **Q2** — выбран `gh` CLI через Bash-allowlist (read-only), не `httpx`.

## Сводка решений

| # | Решение |
|---|---|
| Q1 *(still applicable under BL-1=A)* | Собственный `tools/skill-creator/` CLI **не пишется**. Вместо него — **Anthropic's `skill-creator` skill** из marketplace (`github.com/anthropics/skills/skill-creator`), auto-bootstrap при первом старте `Daemon.start()` (direct Python fetch+Write — no CLI subprocess after BL-1=A). Модель сама пишет `skills/NEW/SKILL.md` + `tools/NEW/main.py` через встроенный Write (phase 2 sandboxed path-guard). |
| Q2 | GitHub fetch — через **`gh` CLI** (read-only) с расширением Bash-allowlist: `gh api <endpoint>` (только GET, endpoint matched на `/repos/.../contents/...` или `/repos/.../tarball/...`), `gh auth status`. Никаких `-X POST/PATCH/DELETE/PUT`. `httpx` оставляем только как fallback для raw.githubusercontent.com-URL'ов, если потребуется. |
| Q3 | `subprocess git clone --depth=1 <https_url> <dest>` через Bash-allowlist с schema-check + path-guard на dest. |
| Q4 *(pre-wipe; dogfood form in BL-1=A)* | Preview UX — **текстовый** "да/нет" в phase 3. Cache-by-URL: dir `<data_dir>/run/installer-cache/<sha256(canonical_url)[:16]>/`. Confirm через `install --confirm --url <URL>` с re-fetch и SHA-compare (TOCTOU); расхождение → `exit 7`. Аргумент `--bundle-sha` убран — sha не приходится проносить между turn'ами модели. Sweeper чистит cache >7 дней. **Dogfood form:** confirm flow через два `@tool` invocations — `skill_preview(url=X)` followed by `skill_install(url=X, confirmed=true)` в последующем turn'е после owner'ского "да"; SHA-compare error → `{"error": ..., "code": 7}` (не exit code). |
| Q5 | Лимиты: `MAX_FILES=100`, `MAX_TOTAL=10 MB`, `MAX_FILE=2 MB`, `FETCH_TIMEOUT=30s`, `UV_SYNC_TIMEOUT=120s`. |
| Q6 | Static validate only (AST-parse main.py + frontmatter schema). Никакого sanity-run чужого кода. |
| Q7 | Tmpdir — `<data_dir>/run/tmp/` + sweeper в `Daemon.start()` для старше 1 часа; same FS с `skills/` для atomic rename. **Sweeper расширен** на `<data_dir>/run/installer-cache/` — TTL 7 дней. |
| Q8 *(pre-wipe; dogfood form in BL-1=A)* | `uv sync` async via `asyncio.create_subprocess_exec` + polling через `skill-installer status NAME`; прогресс-лог `<data_dir>/run/sync/<name>.log` (не внутри `installer-cache/<url_hash>/`, т.к. cache entry удаляется в `finally` после install). **Dogfood form:** полдинг через `@tool("skill_sync_status", ..., {"name": str})`; status file `<data_dir>/run/sync/<name>.status.json`. |
| M1 *(pre-wipe, see BL-1=A for current design)* | Hardcoded `MARKETPLACE_URL = "https://github.com/anthropics/skills"` в `tools/skill-installer/_lib/marketplace.py`. |
| M2 *(pre-wipe, see BL-1=A for current design)* | `marketplace list / info / install` — подкоманды `tools/skill-installer/main.py`, не отдельный скилл. |
| M3 *(pre-wipe, see BL-1=A for current design)* | Auto-install Anthropic's `skill-creator` молча при первом старте `Daemon.start()` после `ensure_skills_symlink`; fail → `log.warning`, старт не блокируется. |
| **M1/M2/M3 (2026-04-17 pre-wipe)** | Questions reframed post-Q-D1=c + BL-1=A (2026-04-21): installer становится `@tool` dogfood, не CLI; bootstrap = direct Python fetch+Write, не `marketplace install` subprocess. M1/M2/M3 historical decisions applicable to CLI flow; dogfood equivalents документированы в current §4/§4d/§6 + Task 1/2. Keep M1/M2/M3 rows for git-history lineage; for current behavior refer to BL-1 row. |
| X1 | PostToolUse hook в `bridge/hooks.py::make_posttool_hooks(data_dir)` для Write + Edit: если `file_path` под `skills/` или `tools/` → `touch <data_dir>/run/skills.dirty`. Полностью заменяет CLI-вызов sentinel. |
| X2 | URL detector — одноразовый regex в `ClaudeHandler._run_turn` до `emit`. Префикс "[system-note: ...]" добавляется только в envelope для SDK; в БД пишется оригинал `user_text`. |
| X3 | SSRF — для `gh api` полагаемся на whitelist endpoints (не пустим модель на произвольный URL); для fallback `httpx` — свой `ipaddress.is_private` check аналогично `bridge/hooks.py::classify_url`. |
| X4 *(pre-wipe; dogfood form in BL-2)* | `gh` отсутствует на хосте (`shutil.which("gh") is None`) → `log.error` в `Daemon.start()` + отключение marketplace-функционала; CLI в целом работает, базовый `install <URL>` через `git clone` остаётся. **Dogfood form:** `_fetch_tool()` helper в `_installer_core.py` returns "gh" | "git" | raises FetchToolMissing; marketplace `@tool`'ы catch и return `{"error": "marketplace requires gh or git", "code": 9}`. |
| Q-D1 | **(c) @tool-decorator pivot** (owner choice 2026-04-21). Phase 3 **не добавляет** enforcement hooks. Adds `mcp_servers={}` slot в ClaudeAgentOptions + researcher spike RQ1 для coexistence verify. Phase 4 memory переезжает на `@tool`; phase 8 gh аналогично. |
| BL-1 | (A) dogfood: skill-installer = @tool functions в src/assistant/tools_sdk/installer.py; skill-creator bootstrap = direct Python fetch+Write. Owner choice post-devil-wave-1 (2026-04-21). Researcher RQ1 spike двух @tool servers. |
| BL-2 | gh/git fallback + `.0xone-installed` marker file gate. Tactical; folded в _installer_core.py helpers. |

### Закрытые блокеры из devil's advocate review (phase 3 plan)

| # | Был | Итоговое решение |
|---|---|---|
| B1 | `/<name>/` в API path (скилы якобы в корне репо) | Реальный layout: `/repos/anthropics/skills/contents/skills/<name>/`. `MARKETPLACE_BASE_PATH = "skills"`; tree-URL — `…/tree/main/skills/<name>/`. См. §4a. |
| B2 | `allowed-tools` отсутствует в SKILL.md (Anthropic `skill-creator`) → непонятно что делать | **Permissive default + `log.warning skill_permissive_default`**. Phase 2 PreToolUse hooks (path-guard, Bash allowlist, WebFetch SSRF) — defense-in-depth. Требуется phase-2-патч: `_normalize_allowed_tools` возвращает sentinel при missing, `ClaudeBridge._build_options` мапит sentinel → полный набор `["Bash","Read","Write","Edit","Glob","Grep","WebFetch"]`. См. §1b. |
| B3 | Планируемый split bundle → `bundle/skill` + `bundle/tool` | Anthropic'овый бандл — **одна директория** со SKILL.md, scripts/, agents/, assets/, references/. `atomic_install` копирует всё в `skills/<name>/`, опциональный split: если внутри bundle есть `tools/` — содержимое переезжает в `<project_root>/tools/<name>/`. См. §4. |
| G3 | Bundle SHA теряется между preview и install (модель не проносит hash в историю turn'ов) | Cache-by-URL + re-fetch с SHA-compare. Preview фиксирует `bundle_sha` в manifest.json; install повторно фетчит и сравнивает → `exit 7` при расхождении. Аргумент `--bundle-sha` удалён, вместо него `--url`. См. §4 и §9. |
| G5 | `await proc.wait(timeout=60)` в bootstrap'е блокирует `Daemon.start()` на минуту при медленной сети | Fire-and-forget: `asyncio.create_task(self._bootstrap_skill_creator_bg())`. Таймаут внутри таски — 120 сек. `Daemon.start()` завершается за <2с независимо от GitHub. См. §6. |
| G6 | `shutil.copytree(..., follow_symlinks=True by default)` → symlink внутри bundle может указывать наружу | `copytree(…, symlinks=True)` + **validator отклоняет любой symlink** (`is_symlink()` / `lstat`) ещё до copy. Anthropic скилы symlinks не используют — exceptions нет. См. §4 и §4b. |
| G7 | `installer-cache/` растёт неограниченно | Sweeper расширен: `tmp/` >1ч и `installer-cache/` >7д. См. §6 (`_sweep_run_dirs`). |
| G9 | Тест `test_bash_allowlist_gh_cli.py` слишком тонкий — не ловит edge-cases вроде `--field`, `--input`, `gh gist create`, `gh workflow run`. | Расширен до 20+ кейсов (8 allow + 12+ deny). Whitelist subcommands — `{"api","auth"}`; `auth` только `status`; `api` — regex endpoint + deny flags `{"-X","--method","-F","--field","-f","--raw-field","--input","--method-override"}`. См. §1a и §9. |

## Дерево файлов (добавляется / меняется)

```
0xone-assistant/
├── src/assistant/tools_sdk/
│   ├── __init__.py                       # NEW — tools_sdk package root
│   ├── installer.py                      # NEW — 7 @tool functions (skill_preview, skill_install, skill_uninstall, marketplace_list, marketplace_info, marketplace_install, skill_sync_status)
│   └── _installer_core.py                # NEW — shared helpers: canonicalize_url, fetch_bundle_async, validate_bundle, sha256_of_tree, atomic_install, marketplace_list_entries, marketplace_tree_url, _fetch_tool, spawn_uv_sync_bg
├── skills/
│   ├── skill-installer/
│   │   └── SKILL.md                      # NEW — discoverability aid (trigger phrases + examples); NO body-instruction
│   └── skill-creator/                    # NEW — auto-bootstrapped via direct Python fetch+Write (НЕ коммитится).
│       │                                 # Layout (реальный Anthropic bundle, одна директория):
│       ├── SKILL.md                      #   главный manifest (frontmatter БЕЗ allowed-tools → permissive default + warning)
│       ├── scripts/                      #   helper-скрипты из bundle
│       ├── agents/
│       ├── assets/
│       ├── references/
│       └── eval-viewer/                  #   если bundle содержит top-level tools/ — переезжает в <project_root>/tools/skill-creator/
├── src/assistant/
│   ├── bridge/
│   │   ├── claude.py                     # CHANGED — check sentinel в _render_system_prompt; мерж PreToolUse+PostToolUse hooks + mcp_servers={"installer": ...} в _build_options
│   │   ├── hooks.py                      # CHANGED — Bash allowlist (git clone, uv sync, gh api read-only, gh auth status); make_posttool_hooks(data_dir) для Write/Edit→sentinel
│   │   └── skills.py                     # UNCHANGED
│   ├── handlers/
│   │   └── message.py                    # CHANGED — URL detector → одноразовая system-note в envelope
│   └── main.py                           # CHANGED — Daemon.start(): shutil.which("gh"/"git"), tmpdir sweeper, _bootstrap_skill_creator_bg() (direct Python, fire-and-forget) после ensure_skills_symlink
├── plan/phase3/
│   ├── description.md                    # CHANGED
│   └── detailed-plan.md                  # CHANGED (этот файл)
└── tests/
    ├── test_installer_tool_marketplace_list.py       # NEW — invoke `mcp__installer__marketplace_list`; mock `gh api /repos/anthropics/skills/contents/skills`
    ├── test_installer_tool_marketplace_install.py    # NEW — marketplace_install + skill_install(confirmed=true); mock `gh api` + `git clone`
    ├── test_installer_tool_fetch_mock.py             # NEW — skill_preview с mock fetch_bundle_async
    ├── test_installer_tool_ssrf_deny.py              # NEW — skill_preview с SSRF-URL → error code 4
    ├── test_installer_tool_path_escape.py            # NEW — bundle с `../` paths → ValidationError → error code 5
    ├── test_installer_tool_size_limits.py            # NEW — bundle exceeding MAX_TOTAL → ValidationError
    ├── test_installer_tool_toctou_detection.py       # NEW — skill_preview vX → mutate source → skill_install → error code 7
    ├── test_installer_tool_symlink_rejected.py       # NEW — validator режет любой symlink до atomic_install
    ├── test_installer_tool_unconfirmed_install.py    # NEW — skill_install(confirmed=false) → error code 3
    ├── test_installer_tool_missing_fetch_tool.py     # NEW — no gh AND no git → error code 9
    ├── test_posttool_hook_touches_sentinel.py        # NEW — Write в skills/x/SKILL.md → data/run/skills.dirty
    ├── test_bootstrap_skill_creator.py               # NEW — fire-and-forget: Daemon.start() <500ms; direct Python path uses _installer_core; marker gate idempotent
    ├── test_sweep_run_dirs.py                        # NEW — tmp >1h + installer-cache >7d удалены, свежее — нет
    ├── test_skill_permissive_default.py              # NEW — allowed-tools отсутствует → permissive + warning; [] → lockdown
    ├── test_bash_allowlist_gh_cli.py                 # NEW — 5 allow + 13+ deny (см. §9)
    ├── test_bash_allowlist_git_clone.py              # NEW
    ├── test_url_detector.py                          # NEW
    └── test_skills_sentinel_hot_reload.py            # NEW
```

## Пошаговая реализация

### 1. Bash allowlist расширение (`bridge/hooks.py`)

Phase 2 уже имеет `_BASH_PROGRAMS` dict. Добавляем `git clone`, `uv sync --directory ...`, `gh api` (read-only), `gh auth status`. `rm`/`mv`/`cp` НЕ добавляются — installer работает через Python `shutil`.

Расширить `_validate_git_invocation`:

```python
_GIT_ALLOWED_SUBCMDS_EXTENDED = frozenset({"status", "log", "diff", "clone"})

def _validate_git_clone(argv: list[str], project_root: Path) -> str | None:
    # git clone --depth=1 <url> <dest>
    if len(argv) < 4: return "git clone requires --depth=1 URL DEST"
    if argv[2] != "--depth=1": return "only --depth=1 is allowed"
    url = argv[3]
    if not (url.startswith("https://") or url.startswith("git@github.com:")):
        return "only https:// or git@github.com: URLs are allowed"
    # SSRF-guard на IP-literals и private hosts — параметрически
    # (parse URL → hostname → ipaddress check)
    dest = Path(argv[4]) if len(argv) > 4 else None
    if dest and not _path_safely_inside(project_root / dest, project_root):
        return "clone dest must be inside project_root"
    return None
```

Расширить `_validate_uv_invocation`:

```python
# Allow: uv run tools/<x>  (already)
# Add:   uv sync --directory tools/<name>
def _validate_uv_invocation(argv, project_root):
    if len(argv) < 2: return "uv requires subcommand"
    if argv[1] == "sync":
        # require --directory=<path-under-tools>
        dir_arg = next((a for a in argv[2:] if a.startswith("--directory")), None)
        if not dir_arg: return "uv sync requires --directory=tools/<name>"
        path = dir_arg.split("=", 1)[1] if "=" in dir_arg else argv[argv.index(dir_arg)+1]
        if not _path_safely_inside(project_root / path, project_root / "tools"):
            return "uv sync --directory must be under tools/"
        return None
    if argv[1] == "run":
        ...  # existing logic
    return f"uv subcommand '{argv[1]}' not allowed"
```

**Важно:** Bash-hook не знает текущий `cwd` — `cd tools/<name> && uv sync` НЕ пройдёт (phase 2 запрещает `;`/`&&` metachars). Используем CLI-флаг `--directory=tools/<name>` с path-guard.

### 1a. `gh` CLI allowlist

Добавляется новая запись `_BASH_PROGRAMS["gh"] = _validate_gh_invocation`. Разрешены **только два subcommand'а**: `api` и `auth status`. Всё остальное (`pr`, `issue`, `repo`, `workflow`, `secret`, `config`, `gist`, `release`, `auth login/logout`, …) → deny.

```python
_GH_ALLOWED_SUBCMDS = frozenset({"api", "auth"})
_GH_AUTH_ALLOWED_SUBSUB = frozenset({"status"})

# Эндпоинт должен быть read-only: /repos/<owner>/<repo>/contents[/<path>][?ref=…]
# или /repos/<owner>/<repo>/tarball[/<ref>]. Query string — только '?key=value'
# без whitespace. Других слов не допускаем.
_GH_API_SAFE_ENDPOINT_RE = re.compile(
    r"^/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(contents(/[^?\s]*)?|tarball(/[^?\s]*)?)"
    r"(\?[^\s]*)?$"
)

_GH_FORBIDDEN_FLAGS = frozenset({
    "-X", "--method", "--method-override",   # deny any explicit method override
    "-F", "--field", "-f", "--raw-field",    # POST-body flags
    "--input",
})

def _validate_gh_invocation(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 2: return "gh requires subcommand"
    sub = argv[1]
    if sub not in _GH_ALLOWED_SUBCMDS:
        return f"gh subcommand '{sub}' not allowed"
    if sub == "auth":
        if len(argv) == 3 and argv[2] in _GH_AUTH_ALLOWED_SUBSUB: return None
        return "only `gh auth status` is allowed"
    # sub == "api"
    for flag in argv[2:]:
        # exact match or prefix match (covers '--field=foo=bar', '-Fname=…')
        if flag in _GH_FORBIDDEN_FLAGS or any(
            flag == f or flag.startswith(f + "=") for f in _GH_FORBIDDEN_FLAGS
        ):
            return f"gh api: flag {flag} not allowed (read-only)"
    endpoint = next((a for a in argv[2:] if a.startswith("/")), None)
    if not endpoint: return "gh api requires endpoint path"
    if not _GH_API_SAFE_ENDPOINT_RE.match(endpoint):
        return f"gh api: endpoint {endpoint} not in read-only whitelist"
    return None
```

Правила (matrix ниже покрыта тестами в §9):

Allow:
- `gh api /repos/anthropics/skills/contents/skills`
- `gh api /repos/anthropics/skills/contents/skills/skill-creator/SKILL.md`
- `gh api "/repos/x/y/contents/skills?ref=main"` (query string)
- `gh api /repos/x/y/tarball/main`
- `gh auth status`

Deny:
- `gh api /graphql` / `gh api /user` / `gh api /search/...`
- `gh api -X POST /repos/x/y/issues` / `--method PATCH /repos/x/y`
- `gh api -F title=X /repos/x/y/issues` / `-f title=X …` / `--field ...` / `--raw-field ...`
- `gh api --input foo.json /repos/x/y`
- `gh pr create` / `gh issue create` / `gh repo create`
- `gh workflow run` / `gh secret set FOO` / `gh config set editor vim`
- `gh gist create` / `gh release create`
- `gh auth login` / `gh auth logout`

### 1b. `_normalize_allowed_tools` helper + documentation-only permissive default

Anthropic'овый `skill-creator` SKILL.md в frontmatter **не содержит** `allowed-tools`. Phase 2 `parse_skill` сегодня (`src/assistant/bridge/skills.py:31`) делает inline `meta.get("allowed-tools", [])` — пустой список при отсутствующем поле. В phase-2 это mostly okay потому что `ClaudeAgentOptions.allowed_tools` задаётся hardcoded списком `["Bash","Read","Write","Edit","Glob","Grep","WebFetch","Skill"]` на уровне `_build_options` (см. `bridge/claude.py:137`) и **per-skill `allowed-tools` из frontmatter SDK'ом игнорируется** (см. `memory/reference_claude_agent_sdk_gotchas.md` — "SKILL.md `allowed-tools:` frontmatter is a NO-OP in SDK"). Phase 3 всё равно хочет:
(1) **extract** helper `_normalize_allowed_tools(raw: Any) -> list[str] | None` — возвращает `None` при missing field, `[]` при explicit lockdown.
(2) передавать это поле в manifest для **документационных целей** (системный промпт покажет owner'у какие тулы скилл заявляет использовать).
(3) **не** менять `ClaudeAgentOptions.allowed_tools` — он остаётся hardcoded baseline'ом.
Важная коррекция: permissive-default warning полезен **только** как owner-facing diagnostic, а не как security gate — реальная защита по-прежнему lives в PreToolUse hooks.

**Важная ремарка.** Permissive default для per-skill frontmatter **не снимает** phase-2 защит: PreToolUse hooks (path-guard на Write/Edit/Read/Glob/Grep, Bash-allowlist, WebFetch SSRF) срабатывают **независимо от manifest'а** и остаются универсальным defense-in-depth. Anthropic `skill-creator` фактически получает полный набор tools — это принятый baseline, задокументирован в "Риски".

### 1c. @tool-decorator pivot groundwork (Q-D1=c)

Closes phase-2 known-debt D1 via architectural pivot: skills cease to be Bash-macros; instead memory/gh/media (phase 4+) становятся first-class SDK custom tools. Phase 3 реализует только **groundwork** — wiring `mcp_servers={}` в `ClaudeAgentOptions` + researcher spike.

**Phase 3 code changes:**
- `src/assistant/bridge/claude.py::_build_options` — add `mcp_servers: dict[str, ...] = {}` parameter passed to `ClaudeAgentOptions(...)`. Empty в phase 3, тип hint'ом подписан как `dict[str, Any]` пока SDK 0.1.63 types.py не имеет publicly-exported `McpServerConfig` TypedDict; если есть — use it.
- `src/assistant/bridge/mcp.py` (NEW empty module) — placeholder для phase 4+ `@tool` definitions. Documented как "phase-3 groundwork, populate в phase 4 with memory_search, memory_write; phase 8 with gh_list_issues, gh_get_file, etc."
- NO new hooks, NO sentinel state dir, NO skill-post-hook.

**Researcher spike RQ1 (step 4 requirement):**

Hermetic live probe `plan/phase3/spikes/rq1_tool_decorator_coexist.py` tests **два** `@tool` servers одновременно (dogfood pivot + future memory placeholder):

```python
from claude_agent_sdk import tool, create_sdk_mcp_server, query, ClaudeAgentOptions

# Server 1: installer (dogfood) — dummy skill_preview
@tool("skill_preview", "Test installer tool", {"url": str})
async def skill_preview(args):
    return {"content": [{"type": "text", "text": f"PREVIEW-OK: {args['url']}"}]}

installer_server = create_sdk_mcp_server(name="installer", version="0.1.0", tools=[skill_preview])

# Server 2: memory placeholder (future phase 4)
@tool("memory_search", "Placeholder memory tool", {"query": str})
async def memory_search(args):
    return {"content": [{"type": "text", "text": f"MEMORY-OK: {args['query']}"}]}

memory_server = create_sdk_mcp_server(name="memory", version="0.1.0", tools=[memory_search])

opts = ClaudeAgentOptions(
    cwd=str(project_root),
    setting_sources=["project"],
    allowed_tools=["Bash", "Read", "Skill", "mcp__installer__skill_preview", "mcp__memory__memory_search"],
    mcp_servers={"installer": installer_server, "memory": memory_server},
)
```

**RQ1 acceptance criteria (extended):**
1. `SystemMessage(init).data["tools"]` содержит оба `"mcp__installer__skill_preview"` И `"mcp__memory__memory_search"`.
2. Model "use skill_preview tool with url=https://example/x" → ToolUseBlock(name="mcp__installer__skill_preview") → marker "PREVIEW-OK: https://example/x".
3. Model "search memory for foo" → ToolUseBlock(name="mcp__memory__memory_search") → marker "MEMORY-OK: foo".
4. Default PreToolUse hooks (Bash/file/WebFetch) НЕ триггерят на mcp__ tool invocations — они фильтруются по tool_name в `_classify_block`. Подтверждение что hooks scoped narrow.
5. Explicit `HookMatcher(matcher="mcp__installer__.*", hooks=[test_audit_hook])` **срабатывает** на installer tool calls — confirms future phase 4+ ability to audit/rate-limit custom tools.
6. `setting_sources=["project"]` + `mcp_servers={installer, memory}` coexist without conflict. If `.claude/settings.json` в project root declares `mcpServers`, SDK merges OR rejects — spike documents actual behavior (test with synthetic `.claude/settings.local.json` containing empty `mcpServers: {}` to measure).

**Fallback decision tree if RQ1 fails:**
- (a) Tool registration fails → check SDK version, try 0.1.64+ upgrade; if still fails, escalate to owner — phase 3 blocked.
- (b) Matcher regex unsupported → fallback to explicit list `HookMatcher(matcher="mcp__installer__skill_preview", ...)` × N tools; more verbose but workable.
- (c) `setting_sources` + `mcp_servers` conflict → pivot to **remove** `setting_sources=["project"]` (which kills CLI's skill auto-discovery); model-side skill discovery via system_prompt manifest only (already how we do it). Loss: `.claude/skills/` native CLI integration. Acceptable trade-off for single-user bot.
- Document outcome в новой секции `## RQ1 (<date>) — @tool coexistence verification` в `spike-findings.md`. Do NOT modify frozen S1-S5 findings. Mark explicitly что S3 PostToolUse findings остаются valid under dogfood pivot (sentinel hook unchanged).

**Phase 4 memory plan preview (phase 4 документирует details):**
- `src/assistant/tools_sdk/memory.py` — modules с `@tool("memory_search", ...)`, `@tool("memory_write", ...)` etc (следует dogfood pattern phase 3 installer).
- `Daemon.start()` создаёт `memory_server = create_sdk_mcp_server(tools=[...])` и merges в `ClaudeBridge._build_options.mcp_servers`.
- Manifest в system_prompt references tools by name `mcp__memory__memory_search`; model invokes them first-class same as installer tools phase 3.
- **Phase 8 gh tool** analogously — `src/assistant/tools_sdk/gh.py` with read-only `@tool("gh_list_repos", ...)`, `@tool("gh_get_file", ...)`. Defers PostToolUse audit hook design to phase 8 plan (если owner pожелать rate limiting / daily quota tracking).

### Task BL-2: `gh`/`git` presence + partial-install recovery

Part of `src/assistant/tools_sdk/_installer_core.py` helpers:
- `_ensure_fetch_tool_available()` — returns "gh" | "git" | None. Caching first-success.
- `atomic_install(tmp_dir, dest_skill_dir, dest_tool_dir)` — tmp+rename pattern + `(dest_skill_dir / ".0xone-installed").touch()` ONLY after both renames succeed. Partial failure → `tmp` cleaned by `_sweep_run_dirs`, marker не touched → bootstrap re-attempts.
- `Daemon.start()` — early call `_sweep_run_dirs(data_dir)` sweeps `<data_dir>/run/tmp/*` older than 1 hour.

### 2. Sentinel-based hot reload

В `src/assistant/bridge/claude.py::_render_system_prompt`:

```python
def _check_skills_sentinel(self) -> None:
    sentinel = self._settings.data_dir / "run" / "skills.dirty"
    if sentinel.exists():
        from assistant.bridge.skills import invalidate_manifest_cache, touch_skills_dir
        invalidate_manifest_cache()
        touch_skills_dir(self._settings.project_root / "skills")
        sentinel.unlink(missing_ok=True)
        log.info("skills_cache_invalidated_via_sentinel")
```

Вызывается в начале `_render_system_prompt()` до `build_manifest(...)`. Гарантирует: на следующий query после install — новый скилл виден.

### 3. (удалено) собственный `tools/skill-creator/main.py`

Phase 3 **не создаёт** собственный skill-creator CLI. Вместо этого Anthropic's `skill-creator` skill ставится из marketplace при первом старте (см. §6 «Auto-bootstrap»), а модель через встроенный Write tool (sandboxed phase 2 path-guard) пишет `skills/<name>/SKILL.md` + `tools/<name>/main.py` напрямую. Автоматическая инвалидация кэша — через PostToolUse hook (§5), не через CLI-вызов sentinel.

### 4. skill-installer `@tool` functions (`src/assistant/tools_sdk/installer.py`)

Dogfood pivot per BL-1=A (2026-04-21): installer flow = model invoking first-class SDK `@tool` functions, не CLI subprocess. Legacy `tools/skill-installer/main.py` **не создаётся**. Tool logic lives in `src/assistant/tools_sdk/installer.py`; shared helpers (fetch, validate, atomic_install, canonicalize_url, sha256_of_tree) — в `src/assistant/tools_sdk/_installer_core.py`. Registration: `create_sdk_mcp_server(name="installer", version="0.1.0", tools=[skill_preview, skill_install, skill_uninstall, marketplace_list, marketplace_info, marketplace_install, skill_sync_status])` и далее в `ClaudeAgentOptions.mcp_servers={"installer": ...}`.

Flow: **cache-by-URL + re-fetch с SHA-compare** (Вариант C, Q4). Same invariants as pre-pivot design, just reframed как tool-internal logic:

- `@tool("skill_preview", ..., {"url": str})`:
  1. Canonicalize URL (strip trailing slash, normalize scheme/host casing, keep query params как есть).
  2. `url_hash = sha256(canonical_url).hexdigest()[:16]`.
  3. Cache dir `<data_dir>/run/installer-cache/<url_hash>/`; внутри — `bundle/` + `manifest.json`.
  4. Fetch → `bundle/` via `_installer_core.fetch_bundle`. Validate → raises `ValidationError` on fatal. `bundle_sha = sha256_of_tree(bundle)` (канонический порядок: `sorted(rglob)` с NFC-normalized relative paths, hash'им `rel_path + NUL + content` для каждого файла, финальный hexdigest'ом собираем дерево).
  5. Записать `manifest.json = {"url": canonical_url, "bundle_sha": bundle_sha, "fetched_at": iso8601, "file_count": N, "total_size": B}`.
  6. Return JSON: `{"preview": {...SKILL.md summary + files list + tool_count + source_sha...}, "cache_key": url_hash, "confirm_hint": "call skill_install(url=<URL>, confirmed=true) after user confirms"}`.
- `@tool("skill_install", ..., {"url": str, "confirmed": bool})`:
  1. Require `confirmed=True` — иначе return `{"error": "install requires confirmed=true; call skill_preview first and ask user to confirm", "code": 3}`.
  2. Тот же canonicalize → `url_hash` → lookup cache.
  3. Загрузить `manifest.json` из cache; если нет — return `{"error": "no cached preview; call skill_preview first", "code": 2}`.
  4. Re-fetch в `<data_dir>/run/installer-cache/<url_hash>/verify/`.
  5. `new_sha = sha256_of_tree(verify)`.
  6. Если `new_sha != manifest["bundle_sha"]` → удалить весь cache entry → return `{"error": "bundle on source changed since preview; call skill_preview again", "code": 7}` (code 7 зарезервирован под TOCTOU).
  7. Иначе: re-validate (`validate_bundle(verify)`) + `atomic_install(verify, report, project_root)` + `SENTINEL.touch()`.
  8. Launch async `uv sync --directory tools/<name>` via `asyncio.create_task` (background, writes to `<data_dir>/run/sync/<name>.status.json`); marker `.0xone-installed` touched внутри `atomic_install` ONLY after both renames succeed.
  9. В `finally` — `shutil.rmtree(cache_entry, ignore_errors=True)` (оба `bundle/` и `verify/` уходят).
  10. Return `{"installed": true, "name": report["name"], "sync_pending": has_tools_dir}`.

```python
# src/assistant/tools_sdk/installer.py
from claude_agent_sdk import tool
from assistant.tools_sdk import _installer_core as core

CACHE = core.DATA_DIR / "run" / "installer-cache"
SENTINEL = core.DATA_DIR / "run" / "skills.dirty"

CODE_NOT_PREVIEWED = 2
CODE_NOT_CONFIRMED = 3
CODE_TOCTOU = 7
CODE_NO_FETCH_TOOL = 9


@tool("skill_preview", "Fetch + validate skill bundle from URL, return preview JSON", {"url": str})
async def skill_preview(args):
    url = args["url"]
    canonical = core.canonicalize_url(url)
    cdir = core.cache_dir_for(canonical)
    cdir.mkdir(parents=True, exist_ok=True)
    bundle_dir = cdir / "bundle"
    if bundle_dir.exists():
        core.rmtree(bundle_dir)
    try:
        await core.fetch_bundle_async(url, bundle_dir)
    except core.FetchToolMissing:
        return core.tool_error("marketplace requires gh or git", CODE_NO_FETCH_TOOL)
    except core.URLError as e:
        return core.tool_error(str(e), code=4)
    try:
        report = core.validate_bundle(bundle_dir)   # rejects symlinks, path-traversal, size
    except core.ValidationError as e:
        core.rmtree(cdir)
        return core.tool_error(f"validation failed: {e}", code=5)
    bsha = core.sha256_of_tree(bundle_dir)
    core.write_manifest(cdir, canonical, bsha, report)
    return {
        "content": [{"type": "text", "text": core.render_preview_text(url, report, bsha)}],
        "structured": {
            "preview": {
                "name": report["name"],
                "description": report["description"],
                "file_count": report["file_count"],
                "total_size": report["total_size"],
                "has_tools_dir": report["has_tools_dir"],
                "source_sha": bsha,
            },
            "cache_key": cdir.name,
            "confirm_hint": f"call skill_install(url={url!r}, confirmed=true) after user says yes",
        },
    }


@tool("skill_install", "Install previewed skill after user confirmation", {"url": str, "confirmed": bool})
async def skill_install(args):
    if not args.get("confirmed"):
        return core.tool_error(
            "install requires confirmed=true; call skill_preview first and ask user to confirm",
            CODE_NOT_CONFIRMED,
        )
    url = args["url"]
    cdir = core.cache_dir_for(core.canonicalize_url(url))
    mpath = cdir / "manifest.json"
    if not mpath.is_file():
        return core.tool_error("no cached preview; call skill_preview first", CODE_NOT_PREVIEWED)
    manifest = core.read_manifest(cdir)
    verify = cdir / "verify"
    if verify.exists():
        core.rmtree(verify)
    try:
        try:
            await core.fetch_bundle_async(url, verify)
        except core.FetchToolMissing:
            return core.tool_error("marketplace requires gh or git", CODE_NO_FETCH_TOOL)
        new_sha = core.sha256_of_tree(verify)
        if new_sha != manifest["bundle_sha"]:
            core.rmtree(cdir)
            return core.tool_error(
                "bundle on source changed since preview; call skill_preview again",
                CODE_TOCTOU,
            )
        report = core.validate_bundle(verify)
        core.atomic_install(verify, report, project_root=core.PROJECT_ROOT)
        SENTINEL.touch()
        sync_pending = False
        if report["has_tools_dir"]:
            core.spawn_uv_sync_bg(report["name"])    # asyncio.create_task — fire-and-forget
            sync_pending = True
        return {
            "content": [{"type": "text", "text": f"installed {report['name']}"}],
            "structured": {"installed": True, "name": report["name"], "sync_pending": sync_pending},
        }
    finally:
        core.rmtree(cdir)
```

Analogous `@tool` bodies для `skill_uninstall(name, confirmed)`, `marketplace_list()`, `marketplace_info(name)`, `marketplace_install(name)` (последний = convenience shortcut: построить tree-URL `https://github.com/anthropics/skills/tree/main/skills/<name>/` + дважды вызвать internal preview→install-после-confirm pipeline), `skill_sync_status(name)` (читает `<data_dir>/run/sync/<name>.status.json`).

**Важно:** `@tool` function body = trusted Python. Model cannot bypass installer security через hostile arguments — validation/size/AST/path-traversal checks embedded прямо в `_installer_core.py`, не в Bash body-instruction. See §4a для обоснования.

`fetch_bundle_async()` helper в `_installer_core.py` (excerpt):

```python
import ipaddress, re, shutil, subprocess, socket
from pathlib import Path
from urllib.parse import urlparse
import httpx

GITHUB_TREE_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)$")
GIT_REPO_RE = re.compile(r"^(https://github\.com/[^/]+/[^/]+(?:\.git)?|git@github\.com:[^/]+/[^/]+\.git)$")
RAW_RE = re.compile(r"^https://raw\.githubusercontent\.com/.+/SKILL\.md$")
GIST_RE = re.compile(r"^https://gist\.github\.com/[^/]+/[0-9a-f]+$")

MAX_TOTAL = 10 * 1024 * 1024
MAX_FILES = 100
MAX_FILE = 2 * 1024 * 1024
TIMEOUT = 30.0

class URLError(Exception): ...

def _check_host_safety(hostname: str) -> None:
    """SSRF guard — аналог bridge/hooks.py::classify_url"""
    try: ip = ipaddress.ip_address(hostname)
    except ValueError: ip = None
    addrs = [ip] if ip else [ipaddress.ip_address(info[4][0])
                              for info in socket.getaddrinfo(hostname, None)]
    for a in addrs:
        if a.is_private or a.is_loopback or a.is_link_local or a.is_reserved:
            raise URLError(f"SSRF: {hostname} resolves to {a}")

def fetch_bundle(url: str, dest: Path) -> None:
    p = urlparse(url) if url.startswith("http") else None
    if p: _check_host_safety(p.hostname or "")

    if GIT_REPO_RE.match(url):
        _git_clone(url, dest)
    elif m := GITHUB_TREE_RE.match(url):
        _github_tree_download(m, dest)
    elif GIST_RE.match(url):
        _gist_download(url, dest)
    elif RAW_RE.match(url):
        _raw_skill_md(url, dest)
    else:
        raise URLError(f"unsupported URL format: {url}")

    _enforce_limits(dest)

def _git_clone(url, dest):
    # invokes `git clone --depth=1 <url> <dest>` via subprocess
    # respected by bridge/hooks.py Bash allowlist when called by the model
    subprocess.run(["git", "clone", "--depth=1", url, str(dest)],
                   check=True, timeout=TIMEOUT)

def _github_tree_download(m, dest):
    owner, repo, ref, path = m.group(1), m.group(2), m.group(3), m.group(4)
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    with httpx.Client(timeout=TIMEOUT) as c:
        # recursive walk via API; each file → raw download with size check
        ...

def _enforce_limits(root: Path):
    total = 0; count = 0
    for f in root.rglob("*"):
        if f.is_file():
            count += 1
            size = f.stat().st_size
            if size > MAX_FILE: raise URLError(f"file too large: {f}")
            total += size
    if count > MAX_FILES: raise URLError("too many files")
    if total > MAX_TOTAL: raise URLError("bundle too large")
```

### 4a. Bash prefilter для installer internals

`@tool` functions (`skill_preview`, `skill_install`, etc.) из `tools_sdk/installer.py` выполняют shell calls через `asyncio.create_subprocess_exec`:
- `gh api <endpoint>` — read-only gated regex (см. §1a Bash allowlist).
- `git clone --depth=1 <url> <dest>` — dest внутри `<data_dir>/run/tmp/`, validated.
- `uv sync --directory tools/<name>` — background task, output → `<data_dir>/run/sync/<name>.status.json`.
- `httpx.get(raw_url)` — Python in-process (не subprocess); SSRF guard via WebFetch hook applies only если model invokes WebFetch tool directly — но installer internals не routing через SDK tools.

Важно: `@tool` internals spawning subprocess НЕ интерцептируются PreToolUse hooks (hooks apply to model's Bash tool calls, не к Python-originated subprocess). Значит validator в `_installer_core.py` MUST duplicate enforcement: URL scheme check, host allowlist (github.com, gist.github.com, raw.githubusercontent.com), path-traversal scan, AST parse, size limits — все внутри `@tool` function body before any subprocess spawn. **Это critical: installer = our trusted code, model не может bypass installer security через creative arguments.**

Per-layer defense summary:
- Layer 1 (Bash allowlist, §1a): blocks model's direct `Bash(command="gh api /user")` calls. Applies when model invokes the SDK's built-in Bash tool.
- Layer 2 (`_installer_core` validators): blocks hostile args passed to `skill_preview(url=...)` — URL regex, SSRF host check, scheme whitelist. Applies to subprocess spawned from inside `@tool` body.
- Layer 3 (static bundle validate): post-fetch, pre-install — AST parse, symlink reject, path-traversal reject, size/count limits.
- Layer 4 (`atomic_install` marker gate): `.0xone-installed` touched only after successful rename; bootstrap checks marker (not dir existence) to detect partial installs.

### 4b. `validate_bundle()` helper в `_installer_core.py` — проверки

Реальная структура Anthropic-скиллов — **одна директория**: SKILL.md в корне + опциональные `scripts/`, `agents/`, `assets/`, `references/`, `eval-viewer/`, иногда `tools/` (которое мы переносим отдельно). Никакого split'а `skill/` vs `tool/` нет.

Проверки:
- SKILL.md в корне bundle'а обязателен.
- **Symlinks отклоняются безусловно.** До любого `copytree`:

  ```python
  for p in bundle.rglob("*"):
      # lstat — НЕ follows link; is_symlink() на самом Path делает lstat.
      if p.is_symlink():
          raise ValidationError(
              f"symlink not allowed: {p.relative_to(bundle)} -> {os.readlink(p)}"
          )
  ```

  Anthropic скилы symlinks не используют; даже symlink, указывающий на файл **внутри** bundle — reject (ради простоты; никаких исключений не делаем).
- Frontmatter schema: `name` (matches `_NAME_RE`), `description` (len>0). **`allowed-tools` opt-in**: отсутствие/`None` → permissive-sentinel (см. §1b); список — каждый элемент должен быть в `ALLOWED_TOOLS`; пустой список `[]` — lockdown (явно валидно).
- **Path-traversal:** для каждого файла `p.resolve().is_relative_to(bundle.resolve())`; любые `..` в SKILL.md ссылках на вложенные файлы — reject.
- Entry point: если есть `scripts/*.py` или `tools/<name>/main.py` — AST-parse (нет синтакс-ошибок); отсутствие — OK (pure-doc skill).
- Возвращает dict `{name, description, allowed_tools, files: [...], file_count, total_size, has_tools_dir: bool, warnings: [...]}`.

Также экспортируется `sha256_of_tree(bundle: Path) -> str` — детерминированный хеш всего дерева:

```python
def sha256_of_tree(root: Path) -> str:
    h = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    for p in files:
        rel = p.relative_to(root).as_posix().encode("utf-8")
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        h.update(b"\x00")
        data = p.read_bytes()
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()
```

### 4c. `atomic_install()` helper в `_installer_core.py` — single-dir copy + optional tools-split + marker gate

```python
import shutil, uuid
from pathlib import Path

def atomic_install(bundle: Path, report: dict, project_root: Path) -> None:
    """bundle — whole Anthropic-style skill directory (contains SKILL.md at root).

    Copies the whole bundle into <project_root>/skills/<name>/.
    If bundle contains a top-level `tools/` subdir, moves its content to
    <project_root>/tools/<name>/ (convention: Anthropic's skills that ship an
    executable helper put it under tools/; we surface it at the project level so
    the Bash allowlist (phase 2) can reach it via `python tools/<name>/main.py`).
    """
    name = report["name"]
    skills_dst = project_root / "skills" / name
    tools_dst = project_root / "tools" / name
    if skills_dst.exists(): raise InstallError(f"skill {name} already installed")
    if tools_dst.exists(): raise InstallError(f"tools/{name} already exists")

    # Stage adjacent to target — same FS → atomic rename.
    stage = project_root / "skills" / f".tmp-{name}-{uuid.uuid4().hex}"
    # symlinks=True preserves symlinks as symlinks (not follows) — combined with
    # validator that rejected them already, any symlink here is a programmer error.
    shutil.copytree(bundle, stage, symlinks=True)

    inner_tools = stage / "tools"
    tools_stage = None
    if inner_tools.is_dir():
        tools_stage = project_root / "tools" / f".tmp-{name}-{uuid.uuid4().hex}"
        shutil.move(str(inner_tools), str(tools_stage))

    try:
        stage.rename(skills_dst)
        if tools_stage is not None:
            tools_stage.rename(tools_dst)
    except OSError:
        # best-effort rollback if first rename succeeded and second failed
        if skills_dst.exists() and tools_stage is not None and not tools_dst.exists():
            shutil.rmtree(skills_dst, ignore_errors=True)
        raise
```

Итог — в `<project_root>/skills/<name>/` лежит **весь** bundle (SKILL.md, scripts/, assets/, agents/, references/, eval-viewer/ — что было), а `tools/<name>/` создаётся **только** если внутри bundle'а был `tools/`. Для Anthropic'ового `skill-creator` это означает `skills/skill-creator/` со всеми вспомогательными файлами; `tools/skill-creator/` НЕ создаётся, потому что у него нет своего CLI-обёрточного `tools/` subdir'а.

`pyproject.toml` в корне bundle (если есть) уезжает в `skills/<name>/pyproject.toml` как часть общего copy — `uv sync --directory=skills/<name>` для него делать не нужно (skills/ — не исполняемый код; реальный `uv sync` нужен только когда переехал `tools/<name>/pyproject.toml`, т. е. когда был `bundle/tools/`).

### 4d. Marketplace helpers в `_installer_core.py`

`marketplace_list`, `marketplace_info`, `marketplace_install` `@tool` functions делегируют common logic приватным helpers в `src/assistant/tools_sdk/_installer_core.py`. Никакого отдельного `_lib/marketplace.py` — всё в одном shared module (import-able from any `@tool`).

**Важно (результат эмпирической проверки репозитория).** Anthropic'ы скилы лежат **не** в корне репо, а в подкаталоге `skills/`:
- API-path: `/repos/anthropics/skills/contents/skills` — это список скилов (entries с `type=="dir"`).
- API-path на отдельный скилл: `/repos/anthropics/skills/contents/skills/<name>/SKILL.md`.
- Web/tree URL: `https://github.com/anthropics/skills/tree/main/skills/<name>/`.

```python
# src/assistant/tools_sdk/_installer_core.py (excerpt)
MARKETPLACE_URL       = "https://github.com/anthropics/skills"  # hardcoded
MARKETPLACE_REPO      = "anthropics/skills"                     # for gh api
MARKETPLACE_BASE_PATH = "skills"                                # subdir inside repo
MARKETPLACE_REF       = "main"
GH_TIMEOUT = 30


class FetchToolMissing(RuntimeError):
    """Neither gh nor git available on PATH."""


def _fetch_tool() -> str:
    """Return 'gh' | 'git'. Raises FetchToolMissing if neither is on PATH."""
    if shutil.which("gh"):
        return "gh"
    if shutil.which("git"):
        return "git"
    raise FetchToolMissing("marketplace requires gh or git")


async def _gh_api_async(endpoint: str) -> Any:
    """asyncio.create_subprocess_exec(['gh', 'api', endpoint]) -> parsed JSON."""
    # rc != 0 -> MarketplaceError(stderr); parse JSON stdout via json.loads
    ...


async def marketplace_list_entries() -> list[dict]:
    entries = await _gh_api_async(
        f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}"
    )
    return [
        {"name": e["name"], "path": e["path"]}
        for e in entries
        if e["type"] == "dir" and not e["name"].startswith(".")
    ]


async def marketplace_fetch_skill_md(name: str) -> str:
    payload = await _gh_api_async(
        f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}/{name}/SKILL.md"
    )
    assert payload.get("encoding") == "base64"
    return base64.b64decode(payload["content"]).decode("utf-8")


def marketplace_tree_url(name: str) -> str:
    """Return the tree-URL that skill_install -> _installer_core.fetch_bundle consumes.

    URL shape: {MARKETPLACE_URL}/tree/{MARKETPLACE_REF}/{MARKETPLACE_BASE_PATH}/{name}
    e.g. https://github.com/anthropics/skills/tree/main/skills/skill-creator
    """
    return f"{MARKETPLACE_URL}/tree/{MARKETPLACE_REF}/{MARKETPLACE_BASE_PATH}/{name}"
```

`@tool("marketplace_list", ...)` -> await `marketplace_list_entries()` -> wrap in ToolResult JSON.
`@tool("marketplace_info", ..., {"name": str})` -> await `marketplace_fetch_skill_md(name)` -> return SKILL.md body text.
`@tool("marketplace_install", ..., {"name": str})` -> internally calls `skill_preview(url=marketplace_tree_url(name))` pipeline, then returns preview — owner must still explicitly confirm via separate turn invoking `skill_install(url=..., confirmed=true)`. No single-call shortcut past confirmation — preserves preview+confirm gate.

`_installer_core.GITHUB_TREE_RE` (internal canonical URL matcher) matches path с `skills/<name>` (а не просто `<name>`). Regex `^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)$` — generic; парсит `owner=anthropics, repo=skills, ref=main, path=skills/skill-creator` — и работает без изменений; внутри `_github_tree_download` просто пробрасываем этот `path` в `/repos/{owner}/{repo}/contents/{path}?ref={ref}`.

Fallback: если `gh` недоступен, но `git` есть — `marketplace_list_entries` делает `git clone --depth=1 --sparse` на `<data_dir>/run/tmp/anthropics-skills/` и iter'ирует `skills/` directory listing. Если ни `gh`, ни `git` — tool returns `{"error": "marketplace requires gh or git", "code": 9}`.

### 5. PostToolUse hook (`bridge/hooks.py`)

Полностью заменяет CLI-вызов sentinel: любой Write/Edit модели внутри `skills/` или `tools/` авто-инвалидирует manifest cache.

```
# Псевдокод:
def make_posttool_hooks(data_dir: Path) -> list[HookMatcher]:
    async def write_touched_skills(input_data, tool_use_id, context):
        path = input_data.get("file_path") or ""
        if "/skills/" in path or "/tools/" in path:
            sentinel = data_dir / "run" / "skills.dirty"
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
        return _allow()  # same helper as PreToolUse

    return [
        HookMatcher(matcher="Write", hooks=[write_touched_skills]),
        HookMatcher(matcher="Edit", hooks=[write_touched_skills]),
    ]
```

`ClaudeBridge._build_options` мержит PreToolUse + PostToolUse hooks:

```
hooks = {
    "PreToolUse":  make_pretool_hooks(self._settings),
    "PostToolUse": make_posttool_hooks(self._settings.data_dir),
}
```

### 6. Auto-bootstrap Anthropic's `skill-creator` (`main.py` `Daemon.start()`)

**Fire-and-forget.** `Daemon.start()` создаёт фоновую задачу и немедленно возвращает управление:

```python
# Daemon.start():
asyncio.create_task(self._bootstrap_skill_creator_bg())   # NO await
asyncio.create_task(self._sweep_run_dirs())               # NO await
# ... далее старт Telegram-адаптера ...
```

Сам bootstrap — **direct Python fetch+validate+atomic_install** внутри daemon process (no subprocess, no model). This bypasses body-compliance issue (#39851) entirely: no model invocation, no SKILL.md body-following, только `_installer_core` helpers called прямо.

```python
async def _bootstrap_skill_creator_bg(self) -> None:
    """Fire-and-forget direct Python install. Does not block Daemon.start()."""
    from assistant.tools_sdk import _installer_core as core

    marker = self._settings.project_root / "skills" / "skill-creator" / ".0xone-installed"
    if marker.is_file():
        return   # idempotent — previous boot already installed
    try:
        tool = core._fetch_tool()  # 'gh' | 'git' — raises FetchToolMissing
    except core.FetchToolMissing:
        log.warning("skill_creator_bootstrap_skipped_no_gh_nor_git")
        return
    log.info("skill_creator_bootstrap_starting", via=tool)
    tmp = self._settings.data_dir / "run" / "tmp" / "skill-creator-boot"
    if tmp.exists():
        core.rmtree(tmp)
    try:
        async def _do_bootstrap():
            url = core.marketplace_tree_url("skill-creator")
            await core.fetch_bundle_async(url, tmp)
            report = core.validate_bundle(tmp)
            core.atomic_install(tmp, report, project_root=self._settings.project_root)
            (self._settings.data_dir / "run" / "skills.dirty").touch()
        await asyncio.wait_for(_do_bootstrap(), timeout=120)
        log.info("skill_creator_bootstrap_ok")
    except asyncio.TimeoutError:
        log.warning("skill_creator_bootstrap_timeout")
    except Exception as e:
        log.warning("skill_creator_bootstrap_failed", error=str(e))
    finally:
        if tmp.exists():
            core.rmtree(tmp)
```

**Не блокирует старт** — таймаут 120с **внутри таски**, не в главном flow. `Daemon.start()` завершается за <2с независимо от доступности GitHub (см. "Критерии готовности"). Owner в Telegram про процедуру не видит. Manifest cache видит `skill-creator` на следующем query — bootstrap сам touch'ит `skills.dirty` sentinel после успешного `atomic_install`. Idempotency guard — marker file `skills/skill-creator/.0xone-installed` (not directory existence) — ловит случай partial install, где dir создан но rename второй half не прошёл.

### 6a. Sweeper для `<data_dir>/run/`

Тоже fire-and-forget-таска в `Daemon.start()`. Чистит **два** поддерева с разными TTL:

```python
async def _sweep_run_dirs(self) -> None:
    now = time.time()
    bases = [
        (self._settings.data_dir / "run" / "tmp", 3600),             # 1 hour
        (self._settings.data_dir / "run" / "installer-cache", 7 * 86400),  # 7 days
    ]
    for base, ttl in bases:
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            try:
                age = now - entry.stat().st_mtime
                if age > ttl:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
            except OSError:
                pass  # best-effort, never fail startup
```

`installer-cache/` TTL 7 дней — чтобы `preview <URL>` → пользователь вернулся через пару дней → `install --confirm --url <URL>` ещё работает (cache жив). `tmp/` TTL 1 час — остатки оборванных fetch'ей.

### 7. Preview → confirm → install flow (dogfood @tool)

Owner types в Telegram: "поставь скилл https://github.com/foo/weather"

1. URL detector в `handlers/message.py` (§8) emit system-note "user provided URL X".
2. Model invokes `mcp__installer__skill_preview(url="https://github.com/foo/weather")`.
3. `skill_preview` tool:
   a. URL validation (scheme, host allowlist, path structure).
   b. Fetch bundle (gh api | git clone | httpx).
   c. Static validate (AST, size, paths, SKILL.md frontmatter).
   d. Cache bundle в `<data_dir>/installer-cache/<sha256(url)[:16]>/` + compute `sha256_of_tree(bundle)`.
   e. Return `{"preview": {...SKILL.md summary + files list + tool_count + source_sha...}, "cache_key": sha256_of_url}`.
4. Model transmits preview content + "Установить? (да/нет)" к owner.
5. Owner types "да".
6. Model invokes `mcp__installer__skill_install(url="...", confirmed=true)`.
7. `skill_install` tool:
   a. Lookup cache by URL sha256.
   b. Re-fetch source → compute `sha256_of_tree(fresh)`.
   c. Compare с cached `sha256_of_tree(bundle)` — mismatch → return `{"error": "source changed since preview", "code": 7}` (TOCTOU guard).
   d. Atomic install via `atomic_install()` helper: tmp+rename skills/<name>/ + tools/<name>/; touch `.0xone-installed` marker only after both renames succeed.
   e. Launch async `uv sync --directory tools/<name>` (background task writing to `<data_dir>/run/sync/<name>.status.json`).
   f. Touch `<data_dir>/run/skills.dirty` sentinel → PostToolUse hook / next-turn `_render_system_prompt` invalidates manifest.
   g. Return `{"installed": true, "name": "weather", "sync_pending": true}`.
8. Model responds к owner: "Установлено, deps устанавливаются в фоне. Проверь `skill_sync_status` через 30с если захочешь".
9. Owner: "готово?" → model `mcp__installer__skill_sync_status(name="weather")` → reads status file → "ok" или "pending (15s elapsed)".

**Key dogfood property:** all flow steps = model calling `@tool` functions. No "run via Bash body-instruction". Per Q-D1=c + BL-1=A: no compliance risk — custom tools are first-class SDK tool invocations, model reliably selects them based on discoverability metadata (description + examples в skill-installer SKILL.md).

### 7a. `skills/skill-installer/SKILL.md` — discoverability aid (not body-instruction)

Frontmatter + body purely **descriptive**: tells the model когда использовать `skill_preview` / `skill_install` / `marketplace_*` / `skill_sync_status` / `skill_uninstall`. Body НЕ содержит "run via Bash" — tools first-class through MCP server. SKILL.md is a discoverability aid: model видит tool names в `SystemMessage(init).data["tools"]`, а SKILL.md даёт trigger phrases + example dialogues.

```markdown
---
name: skill-installer
description: "Manage skill installation from GitHub/marketplace. Use when the user wants to preview, install, uninstall, or browse skills."
---

# skill-installer

Installer exposes **seven** SDK custom tools (no CLI). Invoke them directly
as `mcp__installer__<name>`:

- `skill_preview(url)` — fetch + validate a skill bundle, return a preview.
  Trigger phrases: "поставь скилл <url>", "глянь что за скилл", "preview
  this skill".
- `skill_install(url, confirmed)` — install after user explicitly confirms.
  NEVER invoke with `confirmed=true` without the user saying "да" / "yes"
  in the previous turn.
- `skill_uninstall(name, confirmed)` — remove an installed skill.
- `marketplace_list()` — list Anthropic's official skills.
  Trigger phrases: "какие скилы есть", "что в marketplace".
- `marketplace_info(name)` — show SKILL.md for one marketplace skill.
- `marketplace_install(name)` — shortcut: build the tree-URL for the
  marketplace skill and run the preview pipeline; still requires explicit
  `skill_install(..., confirmed=true)` in a follow-up turn.
- `skill_sync_status(name)` — check background `uv sync` progress after
  an install that had `sync_pending: true`.

## Preview-confirm invariant

Never call `skill_install(url=X, confirmed=true)` in the same turn as the
initial `skill_preview(url=X)`. The owner must see the preview text and
respond affirmatively in a subsequent message. If the cache entry has
expired (7 days) between preview and install, re-run `skill_preview`.

## Brand-new skills

Creating a skill from scratch is a different job — Anthropic's own
`skill-creator` skill (auto-installed at first daemon boot) handles that.
When the user says "сделай скилл для X" invoke `skill-creator` guidance
instead, which writes files via the built-in `Write` tool.
```

### 8. URL detector в `handlers/message.py`

`src/assistant/handlers/message.py::_detect_urls(text)` regex `https?://|git@[^:]+:`. При match → append to user_text before `bridge.ask(...)`:

```python
import re
_URL_RE = re.compile(r"https?://\S+|git@[^\s:]+:\S+", re.IGNORECASE)

async def _run_turn(self, msg, emit):
    enriched_text = msg.text
    urls = _URL_RE.findall(msg.text)
    if urls:
        hint = (
            f"\n\n[system-note: the user's message contains URL(s): {urls[:3]}. "
            "If they appear to be pointing at a GitHub repository or gist that "
            "looks like a skill bundle, you can use the `skill_preview` tool to "
            "fetch and show them a preview before asking to install. Otherwise, "
            "treat the URL as regular reference content and respond normally.]"
        )
        enriched_text = msg.text + hint
    # ... existing logic using enriched_text for bridge call and DB write
```

Note не обязывает модель installing; просто reminds о существовании installer tools. Hermetic test asserts detector fires, но НЕ asserts model auto-installs.

Важно: **оригинал** `msg.text` пишется в БД как user-message (иначе в истории будет мусор), а `enriched_text` идёт только в SDK call. Recommended — записывать оригинал, модели передавать enriched как ephemeral system-note внутри envelope content.

### 9. Тесты

Удалённые (собственного skill-creator CLI больше нет; dogfood pivot — legacy `tools/skill-installer/main.py` CLI не создаётся):
- ~~`test_skill_creator_scaffold.py`~~, ~~`test_skill_creator_validate.py`~~, ~~`test_skill_creator_remove.py`~~ — удалены.
- Any pre-pivot `test_skill_installer_*` tests that shelled out to `python tools/skill-installer/main.py` — replaced by `test_installer_tool_*` equivalents that invoke `@tool` functions directly.

Новые:
- `test_installer_tool_marketplace_list.py` — invoke `skill_list_entries` via `await marketplace_list.execute({})`; mock `_gh_api_async` to return synthetic `/repos/anthropics/skills/contents/skills` payload; assert endpoint path includes `/skills/` subdir, dotfile entries filtered, ToolResult is JSON list.
- `test_installer_tool_marketplace_install.py` — mock `_gh_api_async` (SKILL.md fetch) + `_git_clone_async`; invoke `marketplace_install(name="skill-creator")` → get preview ToolResult → invoke `skill_install(url=..., confirmed=true)` → files appear в `skills/skill-creator/`, marker `.0xone-installed` touched.
- `test_installer_tool_toctou_detection.py` — mock `fetch_bundle_async`: first call returns bundle_v1, second (re-fetch during `skill_install`) — bundle_v2 (one byte changed). Expect ToolResult `{"error": ..., "code": 7}`, cache entry removed, `skills/<name>/` не создан.
- `test_installer_tool_symlink_rejected.py` — build bundle directory с `scripts/evil -> /etc/passwd` (absolute) и отдельно с `scripts/loop -> ./SKILL.md` (relative). Both cases → `validate_bundle` raises `ValidationError` **before** `atomic_install`; в `skills/` ничего не создаётся; tool returns `{"error": ..., "code": 5}`.
- `test_installer_tool_unconfirmed_install.py` — `skill_install(url=X, confirmed=false)` → `{"error": ..., "code": 3}`, nothing fetched/installed.
- `test_installer_tool_missing_fetch_tool.py` — `shutil.which("gh")` None AND `shutil.which("git")` None → `skill_preview` returns `{"error": ..., "code": 9}`.
- `test_installer_tool_fetch_mock.py`, `test_installer_tool_ssrf_deny.py`, `test_installer_tool_path_escape.py`, `test_installer_tool_size_limits.py` — unit tests for `_installer_core` helpers (fetch URL regex, SSRF host check, path traversal detector, size enforcement).
- `test_posttool_hook_touches_sentinel.py` — вызов hook'а с `file_path="skills/test/SKILL.md"` → `data/run/skills.dirty` существует; `file_path="foo.py"` (вне skills/tools) → sentinel не создаётся.
- `test_bootstrap_skill_creator.py` — mock `_installer_core.fetch_bundle_async` и `_installer_core.atomic_install`; **проверяем**: `Daemon.start()` возвращает управление за <500мс при любом поведении mock'а (sleep, exception); при отсутствии marker `.0xone-installed` — bootstrap вызывает direct Python pipeline `fetch_bundle_async → validate_bundle → atomic_install → touch sentinel`; при наличии marker — no-op (idempotent); `_fetch_tool()` raises `FetchToolMissing` → skip + `log.warning skill_creator_bootstrap_skipped_no_gh_nor_git`.
- `test_sweep_run_dirs.py` — создать fake entries с разными mtime: `tmp/old` (2h) / `tmp/new` (10 min) / `installer-cache/stale` (8 days) / `installer-cache/fresh` (1 day). После `_sweep_run_dirs()` → `old` и `stale` удалены, `new` и `fresh` остались.
- `test_bash_allowlist_gh_cli.py` — **расширенная matrix** (phase 2 Bash hook protects model's direct Bash tool calls, orthogonal to `@tool` internals):
  - **Allow (5):** `gh api /repos/anthropics/skills/contents/skills`, `gh api /repos/anthropics/skills/contents/skills/skill-creator/SKILL.md`, `gh api "/repos/x/y/contents/skills?ref=main"`, `gh api /repos/x/y/tarball/main`, `gh auth status`.
  - **Deny (13+):** `gh api /graphql`, `gh api /user`, `gh api -X POST /repos/x/y/issues`, `gh api --method PATCH /repos/x/y`, `gh api -F title=X /repos/x/y/issues`, `gh api -f title=X /repos/x/y/issues`, `gh api --input foo.json /repos/x/y`, `gh pr create`, `gh issue create`, `gh repo create`, `gh workflow run`, `gh secret set FOO`, `gh config set editor vim`, `gh gist create`, `gh release create`, `gh auth login`, `gh auth logout`.

Сохраняются:
- `test_url_detector.py` — `https://`, `git@github.com:`, embedded URLs, multiple URLs.
- `test_skills_sentinel_hot_reload.py` — touch sentinel → `_render_system_prompt` вызывает invalidate_cache + unlink.
- `test_bash_allowlist_git_clone.py` — `git clone --depth=1 https://github.com/x/y /tmp/x` → deny (path escape); `git clone --depth=1 https://github.com/x/y skills/y` → allow.
- Обновить/добавить `test_skill_permissive_default.py` — SKILL.md без поля `allowed-tools` → `parse_skill` возвращает sentinel (None); `_build_options` подставляет полный baseline + `log.warning skill_permissive_default`. Пустой список `[]` → передаётся как `[]` (lockdown), без warning.
- **NEW:** `test_installer_mcp_registration.py` — `create_sdk_mcp_server(name="installer", tools=[...7...])` → `ClaudeAgentOptions.mcp_servers={"installer": ...}` → assert `SystemMessage(init).data["tools"]` contains all 7 `mcp__installer__*` names.

## Критерии готовности

1. `just lint` + `just test` зелёные (+15 новых тестов).
2. Диалог "сделай скилл `echo` с описанием X" → модель через Anthropic's `skill-creator` (установленный при бутстрапе direct-Python path) пишет `skills/echo/SKILL.md` + `tools/echo/main.py` через Write → PostToolUse hook touch'ит `data/run/skills.dirty` → следующий turn видит `echo` в `{skills_manifest}` → "используй echo" → tool_use invocation → `{"ok": true, "tool": "echo"}`.
3. Диалог "какие есть скилы в marketplace" → model invokes `mcp__installer__marketplace_list` → JSON со списком → user выбирает "поставь weather" → model `mcp__installer__marketplace_install(name="weather")` → preview ToolResult → owner "да" → model `mcp__installer__skill_install(url=..., confirmed=true)` → установлено → виден в manifest на следующем turn'е.
4. **Auto-bootstrap fire-and-forget:** `Daemon.start()` завершается за **<2 сек** даже при отсутствии сети / зависшем GitHub. `skills/skill-creator/` появляется асинхронно через direct Python pipeline (не subprocess) — `log.info skill_creator_bootstrap_ok` при успехе, `log.warning skill_creator_bootstrap_{failed,timeout}` при неудаче. Если ни `gh`, ни `git` не установлены — `log.warning skill_creator_bootstrap_skipped_no_gh_nor_git`, marketplace-зависимые `@tool`'ы возвращают `{"error": ..., "code": 9}` в runtime.
5. PostToolUse hook: Write в `skills/test/SKILL.md` → `data/run/skills.dirty` существует сразу после возврата. Write в `foo.py` (вне skills/tools) → sentinel не создаётся.
6. **TOCTOU detection:** сценарий `skill_preview(url=X)` → подменить bundle на источнике → `skill_install(url=X, confirmed=true)` → ToolResult `{"error": "bundle on source changed since preview; call skill_preview again", "code": 7}`, cache entry очищен, `skills/<name>/` не создан.
7. **Symlink rejection:** bundle с `scripts/evil -> /etc/passwd` ИЛИ `scripts/loop -> ./SKILL.md` → `ValidationError` до `atomic_install`; `skills/` не затронут.
8. **Sweeper:** при старте `Daemon.start()` → entries в `tmp/` старше 1 часа удалены; entries в `installer-cache/` старше 7 дней удалены; свежее — сохранено.
9. Ручной smoke install: дать модели URL публичного тестового репо со SKILL.md → preview → confirm → установка → использование.
10. Ручной smoke security:
    - URL `http://169.254.169.254/latest/` → fetch errors out до clone.
    - URL с `file://` — rejected.
    - Репо с 200 MB LFS → rejected size-check после clone (`--depth=1` без LFS).
11. Manifest cache — после Write/Edit в `skills/<name>/` следующий query видит скилл без рестарта бота (через PostToolUse sentinel).
12. **Permissive allowed-tools:** SKILL.md без `allowed-tools` → `_build_options` подставляет полный baseline + `log.warning skill_permissive_default skill_name=skill-creator reason="allowed-tools missing in SKILL.md"`; phase 2 PreToolUse hooks всё ещё активны (проверяется существующими phase-2 тестами).
13. Bash allowlist — расширенная matrix:
    - Allow: `gh api /repos/anthropics/skills/contents/skills`, `.../contents/skills/skill-creator/SKILL.md`, `gh api "/repos/x/y/contents/skills?ref=main"`, `gh api /repos/x/y/tarball/main`, `gh auth status`, `git clone --depth=1 https://github.com/x/y skills/y`, `uv sync --directory tools/foo`.
    - Deny: `gh api /graphql`, `gh api /user`, `gh api -X POST /repos/x/y/issues`, `gh api --method PATCH …`, `gh api -F/-f/--field/--raw-field/--input …`, `gh pr create`, `gh issue create`, `gh repo create`, `gh workflow run`, `gh secret set`, `gh config set`, `gh gist create`, `gh release create`, `gh auth login`, `gh auth logout`, `git clone --depth=1 … /tmp/x`, `git push`.

## Зависимости follow-on фаз

- **Phase 4 memory precondition (D1 closeout).** Phase 3 owner selected (c) pivot → memory skill в phase 4 реализуется как `@tool`-decorator custom SDK tool в `src/assistant/tools_sdk/memory.py`, зарегистрированный через `create_sdk_mcp_server`. SKILL.md-based memory tool отменён. Phase 4 description.md обновлён этой precondition (см. ниже).
- **Phase 8 gh tool** унаследует allowlist pattern из §1a (read-only `gh api`).
- **Bootstrap idempotency.** `_bootstrap_skill_creator_bg` — fire-and-forget, running на каждом start. Последующие фазы (scheduler demon в phase 5) не должны блокировать startup ожиданием bootstrap — same pattern.

## Явные не-цели

- **Собственный `tools/skill-creator/` CLI** — заменён Anthropic's skill из marketplace + Write.
- **Sandbox для runtime выполнения установленных tools** — не в phase 3; tools/<new>/ запускается с теми же правами что и built-in. Митигация — preview+confirm UX + static validate.
- **Inline-keyboard callback_query для preview confirm** — откладываем в phase 8 (ops/admin panel).
- **Оффлайн-кэш marketplace index** — не phase 3; каждый `marketplace list` живьём дёргает `gh api`.
- **Множественные marketplaces** — hardcoded один (`anthropics/skills`). Конфигурабельность — не phase 3.
- **`gh auth login` / authenticated GitHub requests** — только read-only unauthenticated `gh api` (60 req/hour). Скорее всего хватит; если нет — phase 8.
- **Auto-update скилов** (`skill-installer update NAME`) — phase 3 только install + remove. Update = remove + install.
- **Sanity-run tools/<name>/main.py --help** — запрещено (см. Q6).
- **Recursive skill dependencies** (скилл, зависящий от другого скилла) — нет.
- **uv sync** — best-effort, status через polling; синхронного ожидания в handler'е нет.

## Риски

| Риск | Вероятность | Митигация |
|---|---|---|
| Установка вредоносного скилла — полный Bash access. Marketplace (`anthropics/skills`) = trust Anthropic (acceptable); произвольные user-URL'ы = требуют preview+confirm. | **Высокая** | preview+confirm обязателен для user-URL; static validate; denylist built-in names; SSRF guards (Bash-hook + CLI `ipaddress` check); size/count/timeout limits; schemes whitelist (`https://`, `git@github.com:`) |
| `gh` CLI не установлен на хосте | Средняя | `Daemon.start()` делает `shutil.which("gh")` и `shutil.which("git")`; если ни одного — `log.warning skill_creator_bootstrap_skipped_no_gh_nor_git` + marketplace-`@tool`'ы в runtime возвращают `{"error": ..., "code": 9}`. При наличии хотя бы `git` — ad-hoc `skill_preview(url=<git-repo>)` работает через `git clone --depth=1`. Задокументировано в README. |
| Anthropic marketplace URL/structure меняется | Средняя | Researcher делает 10-мин spike до coder'а: подтвердить что `github.com/anthropics/skills/contents/<skill>/SKILL.md` существует и отдаёт base64-encoded frontmatter. Hardcode `MARKETPLACE_URL` на известный формат; при 404 — `log.warning`. |
| Sentinel race — Write завершился, но PostToolUse hook не успел | Низкая | Fallback: `_manifest_mtime` сам поднимется после atomic rename (phase 2 тест `test_skills_manifest_cache`); sentinel — оптимизация. |
| `git clone` hang на GitHub outage | Низкая | TIMEOUT=30s + `subprocess.TimeoutExpired` → cleanup tmpdir + URLError. |
| Path-traversal в bundle (symlinks внутри tarball указывают `../../`) | Средняя | `_installer_core.validate_bundle` делает `Path(f).resolve()` + `.is_relative_to(bundle_root)`; **любой symlink** (не только наружу) → reject через `is_symlink()` ещё до `copytree`. `copytree(…, symlinks=True)` как defense-in-depth, если validator что-то пропустил. |
| Installer оставляет мусор в `<data_dir>/run/tmp/` и `installer-cache/` на crash | Низкая | `Daemon.start()` sweeps: `tmp/` >1ч и `installer-cache/` >7д; installer сам cleans on exception через finally. |
| **Anthropic's `skill-creator` получает полный набор tools** (в его SKILL.md нет `allowed-tools`) | **Средняя (acceptable baseline)** | Permissive default документирован + `log.warning skill_permissive_default` на каждый parse. Defense-in-depth — **phase 2 universal PreToolUse hooks** (Write/Read path-guard на `project_root`, Bash argv-allowlist, WebFetch SSRF) — срабатывают независимо от per-skill manifest'а. Это baseline риск для всех Anthropic-скилов, не именно для `skill-creator`. |
| TOCTOU: bundle подменён между `skill_preview` и `skill_install` | **Средняя → Низкая после митигации** | cache-by-URL + re-fetch + `sha256_of_tree` compare; расхождение → ToolResult `{"error": ..., "code": 7}`, cache entry удаляется, model должен явно снова вызвать `skill_preview`. Покрыто тестом `test_installer_tool_toctou_detection.py`. |
| `Daemon.start()` заблокирован медленным GitHub при bootstrap'е | Низкая | Fire-and-forget `asyncio.create_task` — главный flow не ждёт. Тест `test_bootstrap_skill_creator.py` проверяет, что `start()` возвращается за <500мс даже при mock'е bootstrap'а, который sleep'ит 60 секунд. |
| `uv sync` 2 минуты блокирует следующее сообщение | Средняя | Async launch + status-polling CLI; typing-indicator + сообщение "устанавливаю зависимости, подожди N сек". |
| URL detector false-positive (юзер прислал картинку с URL не для install) | Средняя | System-note сформулирован как "возможно хочет — проверь; иначе игнорируй" — модель сама решает. |
| Bash allowlist v2 ломает phase 2 тесты | Низкая | Новые allowlist-тесты дописывать; существующие `test_bash_allowlist_security.py` не должны regress. |
| TOCTOU между preview и install (bundle заменён локально на диске между командами) | Низкая | Cache-by-URL: обе команды используют один и тот же `<url_hash>` dir; install делает полный re-fetch в `verify/` + SHA-compare, а не доверяет содержимому `bundle/` из preview'а. `validate_bundle` прогоняется повторно на `verify/`. |
| `gh api` GET к неожиданному endpoint (через сконструированный путь от модели) | Низкая | `_GH_API_SAFE_ENDPOINT_RE` whitelist жёстко ограничивает `/repos/.../contents/...` и `/repos/.../tarball/...`; endpoint `/user`, `/search`, etc. — deny. |
