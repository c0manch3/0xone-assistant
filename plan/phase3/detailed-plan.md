# Phase 3 — Detailed Plan

## Вопросы для обсуждения (закрываем до coder-кода)

| # | Вопрос | Recommended | Альтернативы |
|---|---|---|---|
| Q1 | `skill-creator` runtime — stdlib `argparse` или `typer`-проект? | **Одиночный `tools/skill-creator/main.py` на stdlib** (argparse + `pathlib` + re). Phase 2 Q7 уже закрыл принцип: smoke-инструменты — stdlib; `typer` завозить только если команд >5 и нужны cli-UX плюшки. | (a) `uv`-проект с `typer` — красивее, но +зависимость, +`uv sync` на старте проекта. (b) `click` — middle ground. |
| Q2 | `skill-installer` — GitHub через `gh` CLI или `httpx`+raw? | **`httpx` + GitHub REST API (`api.github.com/repos/{owner}/{repo}/contents/{path}`)** + raw.githubusercontent.com для tree-URLов. Anonymous (60 req/hour лимит — хватит одному юзеру). `gh` требует логина и добавляет в allowlist новую бинарь. | (a) `gh api` — проще URL parsing, но нужен `gh auth`. (b) `git clone --depth=1 --filter=tree:0` на всё — оверкилл для скилла в подпапке. |
| Q3 | git clone — `subprocess git` или `GitPython`? | **`subprocess git` через новую запись Bash-allowlist'а** (`git clone --depth=1 <https_url>` с schema-check). `GitPython` — +8 MB deps ради `clone`. | (a) `GitPython` — Pythonic API, но избыточно. (b) `dulwich` — pure-python, но медленнее. |
| Q4 | Preview UX — текстовый "да/нет" или inline-keyboard? | **Текстовый в phase 3.** Installer печатает preview в stdout → bridge отправляет в Telegram → пользователь отвечает "да" / "yes" / "подтверждаю" **следующим сообщением** → модель в следующем turn'е повторяет `skill-installer install --confirm --url <URL>`. Итоговая схема — **cache-by-URL + re-fetch с SHA-compare** (Вариант C, решено пользователем): cache dir `<data_dir>/run/installer-cache/<sha256(url)[:16]>/`, на `install` installer повторно fetch'ит bundle и сравнивает `sha256_of_tree(bundle)` с закэшированным. Расхождение → `exit 7` "bundle on source changed since preview; re-run `preview`". Inline-keyboard — phase 8. | (a) inline-keyboard — один-тап UX, но +CallbackQueryHandler, +routing, +state per message_id. (b) `--bundle-sha` как аргумент — отклонён: sha видна только в stdout preview, модель легко теряет её между turn'ами. |
| Q5 | Лимиты installer'а | **`MAX_FILES=100`, `MAX_TOTAL_SIZE=10 MB`, `MAX_SINGLE_FILE=2 MB`, `FETCH_TIMEOUT=30s`, `UV_SYNC_TIMEOUT=120s`.** Считаются до распаковки. | Мягче (50 MB) / жёстче (1 MB) — цифры можно крутить потом. |
| Q6 | Sanity-run `tools/<name>/main.py --help` перед copy? | **Нет в phase 3.** Static-validate: frontmatter schema + AST-parse main.py (нет синтакс-ошибок, есть `if __name__ == "__main__"`). Запускать чужой код **до** install'а = дать ему выполниться вне sandbox'а = противоречит principle "preview+confirm". Runtime-валидацию откладываем в phase 8 + sandbox. | (a) Sanity-run — быстрее выявит broken скилы, но **security hole**. |
| Q7 | Где tmpdir для download'а | **`tempfile.mkdtemp(prefix="0xone-install-", dir=<data_dir>/run/tmp/)`** + `finally: shutil.rmtree` (even on success — после copy). Не системный `/tmp` — data_dir гарантирует same FS с `skills/` для atomic rename. Cleanup TTL: при старте бота `Daemon.start()` чистит `<data_dir>/run/tmp/` старше 1 часа. | (a) Системный `/tmp` — cross-FS rename → copy+unlink не atomic. (b) `<project_root>/.installer-cache` — в project_root не хочется мусора. |
| Q8 | `uv sync` для `tools/<name>/` — sync или async? | **Async via `asyncio.create_subprocess_exec` с таймаутом 120 сек.** CLI-команда возвращает немедленно с `status=pending`; прогресс через `data/run/skills.dirty.sync-<name>.log`. Модель в следующем turn'е может дёрнуть `skill-installer status <name>`. Для первой версии — если скилл без `pyproject.toml` (stdlib-only как ping) — `uv sync` не нужен вовсе. | (a) Sync 30-60 сек — блокирует handler + user видит typing 1 минуту. (b) Background task без статус-API — модель не узнает о завершении. |

### Приложение: дополнительные вопросы (закрыты в интерактивном обсуждении)

| # | Вопрос | Решение |
|---|---|---|
| M1 | Откуда брать готовые скилы: отдельный marketplace URL в конфиге, хардкод или несколько? | **Hardcoded константа `MARKETPLACE_URL = "https://github.com/anthropics/skills"`** в `tools/skill-installer/_lib/marketplace.py`. Конфигурабельность откладываем. |
| M2 | Marketplace discovery — отдельный скилл или подкоманды существующего installer'а? | **Подкоманды `marketplace list / info / install` в `tools/skill-installer/main.py`** — не отдельный скилл. Меньше surface. |
| M3 | Поведение при отсутствии Anthropic's `skill-creator` локально на старте | **Auto-install молча при первом старте** (`Daemon.start()` → `marketplace install skill-creator --confirm`); fail → `log.warning`, не блокирует. Owner ничего про это в Telegram не видит. |

**ВСЕ Q1-Q8 + M1-M3 ЗАКРЫТЫ пользователем в интерактивном обсуждении.** Отклонения от Recommended: **Q1** — не пишем свой `tools/skill-creator/` вовсе, вместо него ставим Anthropic's skill через marketplace + модель пишет через Write (sandboxed phase 2); **Q2** — выбран `gh` CLI через Bash-allowlist (read-only), не `httpx`.

## Сводка решений

| # | Решение |
|---|---|
| Q1 | Собственный `tools/skill-creator/` CLI **не пишется**. Вместо него — **Anthropic's `skill-creator` skill** из marketplace (`github.com/anthropics/skills/skill-creator`), auto-bootstrap при первом старте `Daemon.start()`. Модель сама пишет `skills/NEW/SKILL.md` + `tools/NEW/main.py` через встроенный Write (phase 2 sandboxed path-guard). |
| Q2 | GitHub fetch — через **`gh` CLI** (read-only) с расширением Bash-allowlist: `gh api <endpoint>` (только GET, endpoint matched на `/repos/.../contents/...` или `/repos/.../tarball/...`), `gh auth status`. Никаких `-X POST/PATCH/DELETE/PUT`. `httpx` оставляем только как fallback для raw.githubusercontent.com-URL'ов, если потребуется. |
| Q3 | `subprocess git clone --depth=1 <https_url> <dest>` через Bash-allowlist с schema-check + path-guard на dest. |
| Q4 | Preview UX — **текстовый** "да/нет" в phase 3. Cache-by-URL: dir `<data_dir>/run/installer-cache/<sha256(canonical_url)[:16]>/`. Confirm через `install --confirm --url <URL>` с re-fetch и SHA-compare (TOCTOU); расхождение → `exit 7`. Аргумент `--bundle-sha` убран — sha не приходится проносить между turn'ами модели. Sweeper чистит cache >7 дней. |
| Q5 | Лимиты: `MAX_FILES=100`, `MAX_TOTAL=10 MB`, `MAX_FILE=2 MB`, `FETCH_TIMEOUT=30s`, `UV_SYNC_TIMEOUT=120s`. |
| Q6 | Static validate only (AST-parse main.py + frontmatter schema). Никакого sanity-run чужого кода. |
| Q7 | Tmpdir — `<data_dir>/run/tmp/` + sweeper в `Daemon.start()` для старше 1 часа; same FS с `skills/` для atomic rename. **Sweeper расширен** на `<data_dir>/run/installer-cache/` — TTL 7 дней. |
| Q8 | `uv sync` async via `asyncio.create_subprocess_exec` + polling через `skill-installer status NAME`; прогресс-лог `<data_dir>/run/sync/<name>.log` (не внутри `installer-cache/<url_hash>/`, т.к. cache entry удаляется в `finally` после install). |
| M1 | Hardcoded `MARKETPLACE_URL = "https://github.com/anthropics/skills"` в `tools/skill-installer/_lib/marketplace.py`. |
| M2 | `marketplace list / info / install` — подкоманды `tools/skill-installer/main.py`, не отдельный скилл. |
| M3 | Auto-install Anthropic's `skill-creator` молча при первом старте `Daemon.start()` после `ensure_skills_symlink`; fail → `log.warning`, старт не блокируется. |
| X1 | PostToolUse hook в `bridge/hooks.py::make_posttool_hooks(data_dir)` для Write + Edit: если `file_path` под `skills/` или `tools/` → `touch <data_dir>/run/skills.dirty`. Полностью заменяет CLI-вызов sentinel. |
| X2 | URL detector — одноразовый regex в `ClaudeHandler._run_turn` до `emit`. Префикс "[system-note: ...]" добавляется только в envelope для SDK; в БД пишется оригинал `user_text`. |
| X3 | SSRF — для `gh api` полагаемся на whitelist endpoints (не пустим модель на произвольный URL); для fallback `httpx` — свой `ipaddress.is_private` check аналогично `bridge/hooks.py::classify_url`. |
| X4 | `gh` отсутствует на хосте (`shutil.which("gh") is None`) → `log.error` в `Daemon.start()` + отключение marketplace-функционала; CLI в целом работает, базовый `install <URL>` через `git clone` остаётся. |

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
├── tools/
│   └── skill-installer/
│       ├── main.py                      # NEW — CLI: preview/install/status + marketplace list/info/install
│       ├── pyproject.toml                # NEW — httpx (fallback), pyyaml; gh — системная зависимость
│       └── _lib/
│           ├── __init__.py
│           ├── fetch.py                  # gh api / git clone / (fallback httpx) + ssrf-guard
│           ├── marketplace.py            # NEW — list/info/install через gh api /repos/anthropics/skills/contents/
│           ├── validate.py               # schema + path-traversal + AST-parse
│           ├── preview.py                # render human-readable block
│           └── install.py                # atomic copy + async uv sync
├── skills/
│   ├── skill-installer/
│   │   └── SKILL.md                      # NEW — описание CLI + marketplace flow
│   └── skill-creator/                    # NEW — auto-bootstrapped from Anthropic marketplace (НЕ коммитится).
│       │                                 # Layout (реальный Anthropic bundle, одна директория):
│       ├── SKILL.md                      #   главный manifest (frontmatter БЕЗ allowed-tools → permissive default + warning)
│       ├── scripts/                      #   helper-скрипты из bundle
│       ├── agents/
│       ├── assets/
│       ├── references/
│       └── eval-viewer/                  #   если bundle содержит top-level tools/ — переезжает в <project_root>/tools/skill-creator/
├── src/assistant/
│   ├── bridge/
│   │   ├── claude.py                     # CHANGED — check sentinel в _render_system_prompt; мерж PreToolUse+PostToolUse hooks в _build_options
│   │   ├── hooks.py                      # CHANGED — Bash allowlist (git clone, uv sync, gh api read-only, gh auth status); make_posttool_hooks(data_dir) для Write/Edit→sentinel
│   │   └── skills.py                     # UNCHANGED
│   ├── handlers/
│   │   └── message.py                    # CHANGED — URL detector → одноразовая system-note в envelope
│   └── main.py                           # CHANGED — Daemon.start(): shutil.which("gh"), tmpdir sweeper, _bootstrap_skill_creator() после ensure_skills_symlink
├── plan/phase3/
│   ├── description.md                    # CHANGED
│   └── detailed-plan.md                  # CHANGED (этот файл)
└── tests/
    ├── test_skill_installer_marketplace_list.py      # NEW — mock `gh api /repos/anthropics/skills/contents/skills`
    ├── test_skill_installer_marketplace_install.py   # NEW — mock `gh api` + `git clone` (endpoint включает /skills/ подкаталог)
    ├── test_skill_installer_fetch_mock.py            # NEW
    ├── test_skill_installer_ssrf_deny.py             # NEW
    ├── test_skill_installer_path_escape.py           # NEW
    ├── test_skill_installer_size_limits.py           # NEW
    ├── test_skill_installer_toctou_detection.py      # NEW — preview vX → re-fetch видит vY → exit 7
    ├── test_skill_installer_symlink_rejected.py      # NEW — validator режет любой symlink до copytree
    ├── test_posttool_hook_touches_sentinel.py        # NEW — Write в skills/x/SKILL.md → data/run/skills.dirty
    ├── test_bootstrap_skill_creator.py               # NEW — fire-and-forget: Daemon.start() <500ms при любом исходе bootstrap
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

### 1b. Permissive `allowed-tools` default (phase-2 патч, требуется phase 3)

Anthropic'овый `skill-creator` SKILL.md в frontmatter **не содержит** `allowed-tools`. Phase 2 `_normalize_allowed_tools` сегодня возвращает `[]` при отсутствии поля — это сломает модель ("no tools allowed"). Phase 3 требует **поправить phase-2 код** (в рамках этого плана, без захода в phase 4):

- `src/assistant/bridge/skills.py::_normalize_allowed_tools` — при отсутствующем / `None` поле возвращать sentinel (самый простой вариант — сам `None`, отличая от пустого списка `[]` = "автор явно запретил все тулы").
- `src/assistant/bridge/skills.py::parse_skill` — пропускает sentinel как есть в возвращаемый dict под ключом `allowed_tools`.
- `src/assistant/bridge/claude.py::_build_options` — сейчас собирает `allowed_tools=["Bash", …]` статически. Когда собирается объединённый набор per-skill (phase-3 / phase-4), при встрече sentinel: передавать полный baseline `["Bash","Read","Write","Edit","Glob","Grep","WebFetch"]` **и** писать `log.warning("skill_permissive_default", skill_name=<name>, reason="allowed-tools missing in SKILL.md")`.
- Пустой список `allowed_tools: []` в frontmatter — продолжает означать "нет тулов" (explicit lockdown).

**Важная ремарка.** Permissive default для per-skill frontmatter **не снимает** phase-2 защит: PreToolUse hooks (path-guard на Write/Edit/Read/Glob/Grep, Bash-allowlist, WebFetch SSRF) срабатывают **независимо от manifest'а** и остаются универсальным defense-in-depth. Anthropic `skill-creator` фактически получает полный набор tools — это принятый baseline, задокументирован в "Риски".

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

### 4. `tools/skill-installer/main.py` + `_lib/`

Flow: **cache-by-URL + re-fetch с SHA-compare** (Вариант C, Q4).

- `preview <URL>`:
  1. Canonicalize URL (strip trailing slash, normalize scheme/host casing, keep query params как есть).
  2. `url_hash = sha256(canonical_url).hexdigest()[:16]`.
  3. Cache dir `<data_dir>/run/installer-cache/<url_hash>/`; внутри — `bundle/` + `manifest.json`.
  4. Fetch → `bundle/`. Validate → raises on fatal. `bundle_sha = sha256_of_tree(bundle)` (канонический порядок: `sorted(rglob)` с NFC-normalized relative paths, hash'им `rel_path + NUL + content` для каждого файла, финальный hexdigest'ом собираем дерево).
  5. Записать `manifest.json = {"url": canonical_url, "bundle_sha": bundle_sha, "fetched_at": iso8601, "file_count": N, "total_size": B}`.
  6. stdout: preview report + `To install run: skill-installer install --confirm --url <URL>`.
- `install --confirm --url <URL>`:
  1. Тот же canonicalize → `url_hash`.
  2. Загрузить `manifest.json` из cache; если нет — `exit 2` "run `preview <URL>` first".
  3. Re-fetch в `<data_dir>/run/installer-cache/<url_hash>/verify/`.
  4. `new_sha = sha256_of_tree(verify)`.
  5. Если `new_sha != manifest["bundle_sha"]` → удалить весь cache entry → `exit 7` с stderr "bundle on source changed since preview; re-run `preview <URL>` to see new content" (exit code 7 зарезервирован под TOCTOU).
  6. Иначе: re-validate (`validate_bundle(verify)`) + `atomic_install(verify, report, project_root)` + `SENTINEL.touch()`.
  7. В `finally` — `shutil.rmtree(cache_entry, ignore_errors=True)` (оба `bundle/` и `verify/` уходят).

```python
# main.py
import argparse, hashlib, json, shutil, sys, tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from _lib.fetch import fetch_bundle, URLError
from _lib.validate import validate_bundle, ValidationError, sha256_of_tree
from _lib.preview import render_preview
from _lib.install import atomic_install

DATA_DIR = ...
CACHE = DATA_DIR / "run" / "installer-cache"
SENTINEL = DATA_DIR / "run" / "skills.dirty"

EXIT_TOCTOU = 7

def _canonicalize_url(url: str) -> str:
    s = urlsplit(url.strip())
    scheme = s.scheme.lower()
    netloc = s.netloc.lower()
    path = s.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, s.query, ""))  # drop fragment

def _cache_dir(url: str) -> Path:
    h = hashlib.sha256(_canonicalize_url(url).encode()).hexdigest()[:16]
    return CACHE / h

def cmd_preview(args):
    cdir = _cache_dir(args.url); cdir.mkdir(parents=True, exist_ok=True)
    bundle_dir = cdir / "bundle"
    if bundle_dir.exists(): shutil.rmtree(bundle_dir)
    fetch_bundle(args.url, bundle_dir)
    report = validate_bundle(bundle_dir)  # also rejects symlinks, path-traversal
    bsha = sha256_of_tree(bundle_dir)
    (cdir / "manifest.json").write_text(json.dumps({
        "url": _canonicalize_url(args.url),
        "bundle_sha": bsha,
        "fetched_at": _utcnow_iso(),
        "file_count": report["file_count"],
        "total_size": report["total_size"],
    }))
    print(render_preview(args.url, bundle_dir, bsha, report))
    print(f"\nTo install run: skill-installer install --confirm --url {args.url}")

def cmd_install(args):
    if not args.confirm: sys.exit("install requires --confirm flag")
    cdir = _cache_dir(args.url)
    mpath = cdir / "manifest.json"
    if not mpath.is_file(): sys.exit(2 if False else "no cached preview; run `preview <URL>` first")
    manifest = json.loads(mpath.read_text())
    verify = cdir / "verify"
    if verify.exists(): shutil.rmtree(verify)
    try:
        fetch_bundle(args.url, verify)
        new_sha = sha256_of_tree(verify)
        if new_sha != manifest["bundle_sha"]:
            shutil.rmtree(cdir, ignore_errors=True)
            sys.stderr.write(
                "bundle on source changed since preview; "
                "re-run `preview <URL>` to see new content\n"
            )
            sys.exit(EXIT_TOCTOU)
        report = validate_bundle(verify)
        atomic_install(verify, report, project_root=...)
        SENTINEL.touch()
        print(json.dumps({"status": "ok", "name": report["name"]}))
    finally:
        shutil.rmtree(cdir, ignore_errors=True)
```

`_lib/fetch.py`:

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

### 4b. `_lib/validate.py` — проверки

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

### 4c. `_lib/install.py` — single-dir copy + optional tools-split

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

### 4a. `tools/skill-installer/_lib/marketplace.py`

Обёртка над Anthropic's public skills репо через `gh` CLI (phase 2 Bash-allowlist → §1a). Никакого `httpx` для marketplace — полагаемся на SSRF-guard Bash-hook'а.

**Важно (результат эмпирической проверки репозитория).** Anthropic'ы скилы лежат **не** в корне репо, а в подкаталоге `skills/`:
- API-path: `/repos/anthropics/skills/contents/skills` — это список скилов (entries с `type=="dir"`).
- API-path на отдельный скилл: `/repos/anthropics/skills/contents/skills/<name>/SKILL.md`.
- Web/tree URL: `https://github.com/anthropics/skills/tree/main/skills/<name>/`.

```python
# Псевдокод:
MARKETPLACE_URL       = "https://github.com/anthropics/skills"  # hardcoded
MARKETPLACE_REPO      = "anthropics/skills"                     # для gh api
MARKETPLACE_BASE_PATH = "skills"                                # подкаталог внутри репо
MARKETPLACE_REF       = "main"
GH_TIMEOUT = 30

def _gh_bin(): return shutil.which("gh") or raise MarketplaceError(
    "gh CLI not found; marketplace disabled. Install https://cli.github.com/"
)

def _gh_api(endpoint: str) -> Any:
    # subprocess.run([gh, "api", endpoint], capture_output=True, timeout=GH_TIMEOUT)
    # rc != 0 → MarketplaceError(stderr); parse JSON stdout
    ...

def list_skills() -> list[dict]:
    entries = _gh_api(f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}")
    return [
        {"name": e["name"], "path": e["path"]}
        for e in entries
        if e["type"] == "dir" and not e["name"].startswith(".")
    ]

def fetch_skill_md(name: str) -> str:
    payload = _gh_api(
        f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}/{name}/SKILL.md"
    )
    assert payload.get("encoding") == "base64"
    return base64.b64decode(payload["content"]).decode("utf-8")

def install_from_marketplace(name: str) -> str:
    """Return the tree-URL that the main installer pipeline will preview/fetch.

    URL shape: {MARKETPLACE_URL}/tree/{MARKETPLACE_REF}/{MARKETPLACE_BASE_PATH}/{name}
    e.g. https://github.com/anthropics/skills/tree/main/skills/skill-creator
    """
    return f"{MARKETPLACE_URL}/tree/{MARKETPLACE_REF}/{MARKETPLACE_BASE_PATH}/{name}"
```

`_lib/fetch.py::GITHUB_TREE_RE` соответственно должен матчить path с `skills/<name>` (а не просто `<name>`). Текущий regex `^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)$` уже обобщённый — парсит `owner=anthropics, repo=skills, ref=main, path=skills/skill-creator` — и работает без изменений; внутри `_github_tree_download` просто пробрасываем этот `path` в `/repos/{owner}/{repo}/contents/{path}?ref={ref}`.

Fallback: если marketplace index нужен offline или `gh` отказал — `git ls-tree --name-only origin/main -- skills/` после shallow clone. В phase 3 не реализуем, задокументировано как future.

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

Сам bootstrap:

```python
async def _bootstrap_skill_creator_bg(self) -> None:
    """Fire-and-forget. Does not block Daemon.start()."""
    if (self._settings.project_root / "skills" / "skill-creator").exists():
        return
    if not shutil.which("gh"):
        log.warning("skill_creator_bootstrap_skipped_no_gh")
        return
    log.info("skill_creator_bootstrap_starting")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self._settings.project_root / "tools" / "skill-installer" / "main.py"),
            "marketplace", "install", "skill-creator", "--confirm",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "ASSISTANT_BOOTSTRAP": "1"},
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=120)
        if rc != 0:
            stderr = (await proc.stderr.read()).decode(errors="replace")[:500]
            log.warning("skill_creator_bootstrap_failed", rc=rc, stderr=stderr)
        else:
            log.info("skill_creator_bootstrap_ok")
    except asyncio.TimeoutError:
        log.warning("skill_creator_bootstrap_timeout")
    except Exception as e:
        log.warning("skill_creator_bootstrap_exception", error=str(e))
```

**Не блокирует старт** — таймаут 120с **внутри таски**, не в главном flow. `Daemon.start()` завершается за <2с независимо от доступности GitHub (см. "Критерии готовности"). Owner в Telegram про процедуру не видит. Manifest cache видит `skill-creator` на следующем query (PostToolUse hook touch'ит sentinel при Write в `skills/skill-creator/SKILL.md` внутри install'а — но при success-пути `atomic_install` сам делает `SENTINEL.touch()`).

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

### 7. `skills/skill-installer/SKILL.md`

```markdown
---
name: skill-installer
description: "Install new skills from a URL or from the Anthropic marketplace. Invoke when the user asks to add/install a skill, shares a git/github URL pointing at a SKILL.md, or asks what skills are available in the marketplace. Run via Bash: `python tools/skill-installer/main.py <cmd>`."
allowed-tools: [Bash]
---

# skill-installer

Use to preview and install skills from URLs or from the Anthropic marketplace.
Creating a brand-new skill from scratch is the job of Anthropic's `skill-creator`
skill (installed automatically at first boot) — invoke that one instead when
the user wants to design a fresh skill.

## Commands

Ad-hoc URL install:
- `python tools/skill-installer/main.py preview <URL>` — fetch bundle, run static
  validation, print human-readable preview + bundle SHA. Cache by URL.
- `python tools/skill-installer/main.py install --confirm --url <URL>` —
  re-fetch bundle, compare SHA against cached preview; on mismatch exit 7
  ("bundle on source changed since preview"). On match, copy files and
  (optionally) kick off async `uv sync`.
- `python tools/skill-installer/main.py status <NAME>` — check async `uv sync` progress.

Marketplace (Anthropic's public skills repo, hardcoded):
- `marketplace list` — JSON list of available skills (reads `/repos/anthropics/skills/contents/skills`).
- `marketplace info <NAME>` — print SKILL.md preview for one skill.
- `marketplace install <NAME>` — convenience shortcut: internally constructs the
  tree URL `https://github.com/anthropics/skills/tree/main/skills/<NAME>/` and runs
  preview → waits for user's "да" → install.

Preview+confirm is mandatory. Never call `install` without a prior `preview`
of the same URL — the cache entry is the safety gate. If the preview is stale
(cache expired after 7 days) run `preview <URL>` again.
```

### 8. URL detector в `handlers/message.py`

```python
import re
_URL_RE = re.compile(r"https?://\S+|git@[^\s:]+:\S+", re.IGNORECASE)

async def _run_turn(self, msg, emit):
    enriched_text = msg.text
    urls = _URL_RE.findall(msg.text)
    if urls:
        hint = (f"\n\n[system-note: user message contains URL(s): {urls[:3]}. "
                "If the user appears to want a skill installed, call "
                "`skill-installer preview <url>` first; otherwise reply as usual.]")
        enriched_text = msg.text + hint
    # ... existing logic using enriched_text for bridge call and DB write
```

Важно: **оригинал** `msg.text` пишется в БД как user-message (иначе в истории будет мусор), а `enriched_text` идёт только в SDK call. Либо — пишется enriched, и мы не теряем след почему модель среагировала. Recommended — записывать оригинал, модели передавать enriched как ephemeral system-note внутри envelope content.

### 9. Тесты

Удалённые (собственного skill-creator CLI больше нет):
- ~~`test_skill_creator_scaffold.py`~~, ~~`test_skill_creator_validate.py`~~, ~~`test_skill_creator_remove.py`~~ — удалены.

Новые:
- `test_skill_installer_marketplace_list.py` — mock subprocess `gh api /repos/anthropics/skills/contents/skills` → проверяем правильный endpoint path (c `skills/` подкаталогом), парсинг, фильтр dotfiles, JSON-output CLI.
- `test_skill_installer_marketplace_install.py` — mock `gh api` (SKILL.md fetch, endpoint `/repos/anthropics/skills/contents/skills/skill-creator/SKILL.md`) + `git clone` → preview → install через `--url` (без `--bundle-sha`) → файлы на месте в `skills/skill-creator/`.
- `test_skill_installer_toctou_detection.py` — mock fetch: первый вызов возвращает bundle_v1, второй (на re-fetch при `install`) — bundle_v2 (один байт изменён). Ожидаем `exit code 7`, stderr содержит "bundle on source changed since preview", cache entry удалён.
- `test_skill_installer_symlink_rejected.py` — создать bundle-каталог с `scripts/evil -> /etc/passwd` (absolute) и отдельно с `scripts/loop -> ./SKILL.md` (relative внутрь). Оба случая → `ValidationError` **до** `atomic_install`; в `skills/` ничего не создаётся.
- `test_posttool_hook_touches_sentinel.py` — вызов hook'а с `file_path="skills/test/SKILL.md"` → `data/run/skills.dirty` существует; `file_path="foo.py"` (вне skills/tools) → sentinel не создаётся.
- `test_bootstrap_skill_creator.py` — mock `asyncio.create_subprocess_exec`; **проверяем**: `Daemon.start()` возвращает управление за <500мс при любом поведении mock'а (sleep, exception, rc!=0); при отсутствии `skills/skill-creator/` subprocess вызван с аргументами `marketplace install skill-creator --confirm`; при наличии — no-op; `shutil.which("gh") is None` → skip + log.warning.
- `test_sweep_run_dirs.py` — создать fake entries с разными mtime: `tmp/old` (2h) / `tmp/new` (10 min) / `installer-cache/stale` (8 days) / `installer-cache/fresh` (1 day). После `_sweep_run_dirs()` → `old` и `stale` удалены, `new` и `fresh` остались.
- `test_bash_allowlist_gh_cli.py` — **расширенная matrix**:
  - **Allow (5):** `gh api /repos/anthropics/skills/contents/skills`, `gh api /repos/anthropics/skills/contents/skills/skill-creator/SKILL.md`, `gh api "/repos/x/y/contents/skills?ref=main"`, `gh api /repos/x/y/tarball/main`, `gh auth status`.
  - **Deny (13+):** `gh api /graphql`, `gh api /user`, `gh api -X POST /repos/x/y/issues`, `gh api --method PATCH /repos/x/y`, `gh api -F title=X /repos/x/y/issues`, `gh api -f title=X /repos/x/y/issues`, `gh api --input foo.json /repos/x/y`, `gh pr create`, `gh issue create`, `gh repo create`, `gh workflow run`, `gh secret set FOO`, `gh config set editor vim`, `gh gist create`, `gh release create`, `gh auth login`, `gh auth logout`.

Сохраняются:
- `test_skill_installer_fetch_mock.py`, `test_skill_installer_ssrf_deny.py`, `test_skill_installer_path_escape.py`, `test_skill_installer_size_limits.py`.
- `test_url_detector.py` — `https://`, `git@github.com:`, embedded URLs, multiple URLs.
- `test_skills_sentinel_hot_reload.py` — touch sentinel → `_render_system_prompt` вызывает invalidate_cache + unlink.
- `test_bash_allowlist_git_clone.py` — `git clone --depth=1 https://github.com/x/y /tmp/x` → deny (path escape); `git clone --depth=1 https://github.com/x/y skills/y` → allow.
- Обновить/добавить `test_skill_permissive_default.py` — SKILL.md без поля `allowed-tools` → `parse_skill` возвращает sentinel (None); `_build_options` подставляет полный baseline + `log.warning skill_permissive_default`. Пустой список `[]` → передаётся как `[]` (lockdown), без warning.

## Критерии готовности

1. `just lint` + `just test` зелёные (+15 новых тестов).
2. Диалог "сделай скилл `echo` с описанием X" → модель через Anthropic's `skill-creator` (установленный при бутстрапе) пишет `skills/echo/SKILL.md` + `tools/echo/main.py` через Write → PostToolUse hook touch'ит `data/run/skills.dirty` → следующий turn видит `echo` в `{skills_manifest}` → "используй echo" → tool_use invocation → `{"ok": true, "tool": "echo"}`.
3. Диалог "какие есть скилы в marketplace" → `marketplace list` → JSON со списком → пользователь выбирает → `marketplace install NAME` → preview → "да" → установлено → виден в manifest на следующем turn'е.
4. **Auto-bootstrap fire-and-forget:** `Daemon.start()` завершается за **<2 сек** даже при отсутствии сети / зависшем GitHub. `skills/skill-creator/` появляется асинхронно — `log.info skill_creator_bootstrap_ok` при успехе, `log.warning skill_creator_bootstrap_{failed,timeout,exception}` при неудаче. Если `gh` не установлен — `log.warning skill_creator_bootstrap_skipped_no_gh`, marketplace disabled в целом.
5. PostToolUse hook: Write в `skills/test/SKILL.md` → `data/run/skills.dirty` существует сразу после возврата. Write в `foo.py` (вне skills/tools) → sentinel не создаётся.
6. **TOCTOU detection:** сценарий `preview <URL>` → подменить bundle на источнике → `install --confirm --url <URL>` → exit code 7, stderr "bundle on source changed since preview; re-run `preview`", cache entry очищен, `skills/<name>/` не создан.
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
| `gh` CLI не установлен на хосте | Средняя | `Daemon.start()` делает `shutil.which("gh")`; если нет → `log.error` с понятным сообщением + отключение marketplace-функционала (`marketplace list/info/install` возвращают stderr "gh required"); CLI в целом запускается, `install <URL>` через git clone работает. Задокументировано в README. |
| Anthropic marketplace URL/structure меняется | Средняя | Researcher делает 10-мин spike до coder'а: подтвердить что `github.com/anthropics/skills/contents/<skill>/SKILL.md` существует и отдаёт base64-encoded frontmatter. Hardcode `MARKETPLACE_URL` на известный формат; при 404 — `log.warning`. |
| Sentinel race — Write завершился, но PostToolUse hook не успел | Низкая | Fallback: `_manifest_mtime` сам поднимется после atomic rename (phase 2 тест `test_skills_manifest_cache`); sentinel — оптимизация. |
| `git clone` hang на GitHub outage | Низкая | TIMEOUT=30s + `subprocess.TimeoutExpired` → cleanup tmpdir + URLError. |
| Path-traversal в bundle (symlinks внутри tarball указывают `../../`) | Средняя | `_lib/validate.py` делает `Path(f).resolve()` + `.is_relative_to(bundle_root)`; **любой symlink** (не только наружу) → reject через `is_symlink()` ещё до `copytree`. `copytree(…, symlinks=True)` как defense-in-depth, если validator что-то пропустил. |
| Installer оставляет мусор в `<data_dir>/run/tmp/` и `installer-cache/` на crash | Низкая | `Daemon.start()` sweeps: `tmp/` >1ч и `installer-cache/` >7д; installer сам cleans on exception через finally. |
| **Anthropic's `skill-creator` получает полный набор tools** (в его SKILL.md нет `allowed-tools`) | **Средняя (acceptable baseline)** | Permissive default документирован + `log.warning skill_permissive_default` на каждый parse. Defense-in-depth — **phase 2 universal PreToolUse hooks** (Write/Read path-guard на `project_root`, Bash argv-allowlist, WebFetch SSRF) — срабатывают независимо от per-skill manifest'а. Это baseline риск для всех Anthropic-скилов, не именно для `skill-creator`. |
| TOCTOU: bundle подменён между `preview` и `install` | **Средняя → Низкая после митигации** | cache-by-URL + re-fetch + `sha256_of_tree` compare; расхождение → `exit 7`, cache entry удаляется, пользователь должен явно запустить `preview` заново. Покрыто тестом `test_skill_installer_toctou_detection.py`. |
| `Daemon.start()` заблокирован медленным GitHub при bootstrap'е | Низкая | Fire-and-forget `asyncio.create_task` — главный flow не ждёт. Тест `test_bootstrap_skill_creator.py` проверяет, что `start()` возвращается за <500мс даже при mock'е bootstrap'а, который sleep'ит 60 секунд. |
| `uv sync` 2 минуты блокирует следующее сообщение | Средняя | Async launch + status-polling CLI; typing-indicator + сообщение "устанавливаю зависимости, подожди N сек". |
| URL detector false-positive (юзер прислал картинку с URL не для install) | Средняя | System-note сформулирован как "возможно хочет — проверь; иначе игнорируй" — модель сама решает. |
| Bash allowlist v2 ломает phase 2 тесты | Низкая | Новые allowlist-тесты дописывать; существующие `test_bash_allowlist_security.py` не должны regress. |
| TOCTOU между preview и install (bundle заменён локально на диске между командами) | Низкая | Cache-by-URL: обе команды используют один и тот же `<url_hash>` dir; install делает полный re-fetch в `verify/` + SHA-compare, а не доверяет содержимому `bundle/` из preview'а. `validate_bundle` прогоняется повторно на `verify/`. |
| `gh api` GET к неожиданному endpoint (через сконструированный путь от модели) | Низкая | `_GH_API_SAFE_ENDPOINT_RE` whitelist жёстко ограничивает `/repos/.../contents/...` и `/repos/.../tarball/...`; endpoint `/user`, `/search`, etc. — deny. |
