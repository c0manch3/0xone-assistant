# Phase 3 — Summary

Документ подводит итоги завершённой фазы 3 проекта `0xone-assistant` (skill-creator через Anthropic marketplace + skill-installer CLI + PostToolUse sentinel + URL detector). Источники: `plan/phase3/{description,detailed-plan,implementation,spike-findings}.md`, исходники `src/assistant/` + `tools/skill-installer/` + `skills/skill-installer/`, 6 коммитов `5e00df2 → 3bf8437` поверх phase-2 HEAD `0e2bdcd`, 44 тестовых файла (282 passed + 1 skipped).

## 1. TL;DR

Внедрён двухпутевой механизм расширения бота: (a) Anthropic's `skill-creator` auto-bootstrap'ится при первом старте Daemon через fire-and-forget таску с 120-с таймаутом; (b) stdlib-only CLI `tools/skill-installer/` ставит скилы из hardcoded-маркета `anthropics/skills` (tarball endpoint — 1 gh-вызов вместо ~84) либо по произвольному URL. Защита: SSRF-redirect через кастомный `urllib` opener, symlink+hardlink reject, TOCTOU detection (cache-by-URL + SHA compare → `exit 7`), argv-allowlist для `gh api` / `git clone` / `uv sync`. PostToolUse hook на Write/Edit под `skills/`|`tools/` touch'ит sentinel → следующий turn видит новый скилл без рестарта. Кодовая база выросла с 1854 до 2594 LOC в `src/` + 1379 LOC в `tools/`; 6 коммитов, 55 файлов, +8406 / −144 строки. 282/282 тестов зелёные (+1 skipped — кросс-FS hardlink-тест на этом хосте), lint+mypy strict чистые. Phase 4 разблокирован.

## 2. Что реализовано

### 2.1 Новый CLI `tools/skill-installer/`

| Файл | LOC | Роль | Ключевые решения |
|---|---|---|---|
| `/Users/agent2/Documents/0xone-assistant/tools/skill-installer/main.py` | 370 | Argparse-роутер subcommand'ов `preview/install/status/marketplace {list,info,install}` | Stdlib-only (B-4) — installer не импортирует из `src/assistant/` и сам правит `sys.path` для `_lib/`. Per-cache-entry `fcntl.LOCK_EX` (S-2) сериализует параллельный `preview` + `install` одного URL. Exit codes семантичны: `0/2/3/4/5/7/8/9` (`EXIT_TOCTOU=7` — перезагружается в wire-контракте со скилом). |
| `/Users/agent2/Documents/0xone-assistant/tools/skill-installer/_lib/fetch.py` | 310 | Fetch по URL-shape (git clone / github tree / raw SKILL.md / marketplace tarball) | `_SafeRedirectHandler` — кастомный `urllib.request` opener, re-classify каждого 3xx-redirect через `classify_url_sync` (блокирует `302 Location: http://169.254.169.254/...` metadata exfil). Default-branch-only для tree без `gh`; с `gh` работает любой ref через tarball endpoint. `_reject_dotdot_segments` отдельным слоем поверх regex (C-9). |
| `/Users/agent2/Documents/0xone-assistant/tools/skill-installer/_lib/marketplace.py` | 130 | Обёртка `gh api` для `anthropics/skills` | Hardcoded `MARKETPLACE_URL`/`MARKETPLACE_REPO`/`MARKETPLACE_BASE_PATH="skills"`. `_gh_api` парсит `rc=0 + body {"status":"404"}` (spike S2.d). `install_tree_url` делегирует в `fetch.py` через tarball endpoint. `_parse_gh_json` skip'ает banner-строки старых версий gh (H-4). |
| `/Users/agent2/Documents/0xone-assistant/tools/skill-installer/_lib/validate.py` | 268 | Bundle-validate + stable tree hash | `sha256_of_tree` исключает `.git/`, `__pycache__/`, `.DS_Store`, `*.pyc` (B-3 — иначе digest флапает между re-clone). Каждый record length-prefixed (`len(rel)` + NUL + `len(data)`) — unambiguous framing (H-3). `_reject_unsafe_paths` отклоняет **и** symlinks (`is_symlink`), **и** hardlinks (`st_nlink > 1` — review must-fix #4, `resolve()` не ловит hardlink). AST-parse каждого `.py`. |
| `/Users/agent2/Documents/0xone-assistant/tools/skill-installer/_lib/install.py` | 110 | Atomic copy + optional `tools/` split | `shutil.copytree(symlinks=True)` defence-in-depth даже после validator'а. Два tmp-стейджа (`skills/` + опционально `tools/`); каждый `rename()` — POSIX-atomic на одном FS. Rollback: если второй rename упал, первый откатывается `rmtree`. `diff_trees` рендерит `ADDED/REMOVED/CHANGED` для TOCTOU stderr. |
| `/Users/agent2/Documents/0xone-assistant/tools/skill-installer/_lib/preview.py` | 62 | Human-readable превью | Печатает имя, описание, file_count, total_size, first-16 SHA. |
| `/Users/agent2/Documents/0xone-assistant/tools/skill-installer/_lib/_net_mirror.py` | 104 | **Зеркало** SSRF-helpers из `src/assistant/bridge/net.py` | Между sentinel'ами `SSRF_MIRROR_START` / `SSRF_MIRROR_END` — byte-equal копия. Тест `tests/test_ssrf_mirror_in_sync.py` падает при drift. Mirror нужен т.к. installer не импортирует из main пакета (B-4). |

### 2.2 Новый модуль `src/assistant/bridge/net.py`

Выделен из `bridge/hooks.py` в отдельный модуль для зеркалирования. 102 LOC, три публичные функции:
- `is_private_address(addr)` — `private|loopback|link_local|reserved|multicast|unspecified`.
- `resolve_hostname(host, deadline_s)` — `loop.getaddrinfo` под `asyncio.timeout`.
- `classify_url(url, dns_timeout=3.0)` — scheme→host→IP-literal→DNS→классификация; IDN не нормализуется (phase 4 открытый вопрос).

`src/assistant/bridge/hooks.py` re-exports их как `_is_private_address` / `classify_url` для обратной совместимости с phase-2 тестами.

### 2.3 Расширенный `src/assistant/bridge/hooks.py`

Был 544 LOC → стал **765 LOC** (+221). Основные добавления:
- `_validate_gh_invocation` — только `gh api <read-only-endpoint>` + `gh auth status`. Forbidden-flags расширены `{-H, --header, --hostname, --cache, -p, --preview}` (review #2) сверх `{-X, --method, -F, --field, -f, --raw-field, --input}`. Endpoint regex — `^/repos/.+/(contents|tarball).*` (query-string разрешена).
- `_validate_git_clone` ветка внутри `_validate_git_invocation` — `--depth=1 <https-or-ssh-url> <dest-inside-project-root>` + `is_private_address` reject IP-literal хостов.
- `_validate_uv_sync` — только `uv sync --directory=tools/<name>` (Bash не видит `cd`).
- **H-1 prefix расширен**: `_PYTHON_ALLOWED_PREFIXES = ("tools/", "skills/")` — Anthropic'ов `skill-creator` bundle кладёт helper-скрипты в `skills/skill-creator/scripts/*.py`, без этого bootstrap ломался с "python script must live under tools/".
- `make_posttool_hooks(project_root, data_dir)` — возвращает `[HookMatcher(matcher="Write"), HookMatcher(matcher="Edit")]`, оба с общим `make_posttool_sentinel_hook`. Callback touch'ит `<data_dir>/run/skills.dirty` iff `tool_input.file_path` после `Path.resolve()` — under `<project_root>/skills/` или `<project_root>/tools/`. Substring-check `"/skills/" in path` отклонён как неверный (H-1.2).

### 2.4 Изменённые модули `src/assistant/bridge/`

| Файл | Что изменилось |
|---|---|
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/skills.py` (110 → 152 LOC) | `_normalize_allowed_tools` стал **3-way**: `None` (missing/malformed) → permissive sentinel; `[]` → honest lockdown (warned, не enforced в phase 3); list → как есть. `build_manifest` логгирует `skill_permissive_default` (для missing) и `skill_lockdown_not_enforced` (для empty). Публичный API `invalidate_manifest_cache()` + `touch_skills_dir()` не изменился. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/claude.py` (195 → 250 LOC) | `_build_options` теперь мержит `PreToolUse + PostToolUse` hooks в одном `hooks` dict (spike S3.a подтвердил coexistence). `_render_system_prompt` дёргает новый `_check_skills_sentinel()` перед `build_manifest` — iff `<data_dir>/run/skills.dirty` есть, invalidate cache + touch skills_dir + unlink (с `FileNotFoundError` swallow для кросс-чат гонки). `ask(..., system_notes: list[str] \| None = None)` — новый параметр для URL-detector'а; notes идут как extra `{type: text}`-блоки в current user envelope's content, **не** в ConversationStore. |

### 2.5 Изменённый `src/assistant/handlers/message.py` (190 → 236 LOC)

URL detector добавлен: `_URL_RE = r"https?://[^\s<>\[\]()]+|git@[^\s:]+:\S+"` + `_URL_TRAILING_STRIP = ".,;:!?)"` + `_URL_DETECT_MAX = 3`. Найденные URL попадают в `system_notes` с RU-инструкцией "если пользователь хочет поставить скилл — запусти `preview <URL>` сначала". ConversationStore пишет **оригинал** `msg.text` (не enriched envelope) — history остаётся честной.

### 2.6 Изменённый `src/assistant/main.py` (128 → 402 LOC)

Четыре новых блока, каждый — review-fix высокого приоритета:

1. **Fire-and-forget housekeeping.** `_bg_tasks: set[asyncio.Task[None]]` + `_spawn_bg(coro, *, name)` с `task.add_done_callback(self._bg_tasks.discard)` — `_bg_tasks` держит strong ref (CPython иначе GC'нет floating task, Python ≥3.12 кидает `RuntimeWarning`). Два таска: `_sweep_run_dirs` (tmp >1ч, installer-cache >7д) и `_bootstrap_skill_creator_bg`.
2. **`_bootstrap_skill_creator_bg`.** Skip если skill уже есть, skip если `shutil.which("gh") is None`, иначе subprocess `python tools/skill-installer/main.py marketplace install skill-creator --confirm` под `asyncio.wait_for(..., timeout=120s)`. Любой нехороший исход → `log.warning` + `_bootstrap_notify_failure`.
3. **Marker rotation (`_bootstrap_notify_failure`).** `<data_dir>/run/.bootstrap_notified` — JSON `{rc, reason, ts_epoch, ts}`. `O_CREAT|O_EXCL|O_WRONLY` close'ит race parallel-стартов (обе ноды не спамят). Re-notify только при (a) marker absent, (b) `ts` >7д, (c) `rc` изменился (regression → новое условие). Успех авто-стирает marker (`_bootstrap_clear_notify_marker`).
4. **`Daemon.stop` drain.** `asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5s)` — hung bg-task не блокирует shutdown навечно; на timeout cancel + второй gather (ограничен cancel-response time).

### 2.7 Новые артефакты

- `/Users/agent2/Documents/0xone-assistant/skills/skill-installer/SKILL.md` — документация для модели (`preview` → confirm → `install --confirm --url`).
- `/Users/agent2/Documents/0xone-assistant/skills/skill-creator/` — auto-bootstrap'ится при первом старте из marketplace; в репо **не коммитится** (в `.gitignore`).
- `/Users/agent2/Documents/0xone-assistant/spikes/marketplace_probe.py` + `.json` report — empirical marketplace layout (17 скилов под `skills/`, none of them declares `allowed-tools`).
- `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe_posthook.py` + `.json` report — PostToolUse hook API + `HookEvent` literal set.

## 3. Ключевые архитектурные решения (15 пунктов)

1. **Anthropic's `skill-creator` вместо собственного CLI (Q1 override).** Phase 3 не пишет свой `tools/skill-creator/` — ставит Anthropic'овый из marketplace, а модель сама через Write tool (phase-2 sandboxed path-guard) создаёт `skills/<name>/SKILL.md` + `tools/<name>/main.py`. Это сэкономило ~300 LOC CLI и свело job-to-be-done к "preview+confirm для внешнего кода".
2. **`gh` CLI через subprocess (Q2 override).** Отклонили `httpx`+raw REST: `gh api` уже авторизован, tarball endpoint делает **1 запрос** вместо 84+ contents-walk (rate-limit relief от 60 req/h). Deny-heavy allowlist: только `api` read-only + `auth status`; 12 forbidden-flags.
3. **Marketplace tarball endpoint (review #3).** Раньше marketplace-install делал gh api contents + recursive walk по директориям — 60+ запросов на bundle = лимит exhausted. Tarball: `gh api /repos/anthropics/skills/tarball/main` → `tarfile.extractall(filter="data")` (Py 3.12+) → keep only `skills/<name>/` subtree. Единственный fallback на contents-walk — non-default branch через `_fetch_github_tree_fallback` (default-branch-only без `gh`).
4. **Stdlib-only `tools/skill-installer/` (B-4).** Нет chicken-and-egg с venv: installer может работать даже если `uv sync` проекта ещё не прошёл. Нет `pyproject.toml`, нет внешних deps. `sys.path` подставляется внутри `main.py`.
5. **SSRF helper mirror через `SSRF_MIRROR_START/END` sentinels.** Installer физически не может импортить `src/assistant/bridge/net.py` (B-4). Дублируем ~60 LOC между двух файлов + `tests/test_ssrf_mirror_in_sync.py` fail'ит при drift. Дешевле чем installable shared sub-package.
6. **PostToolUse sentinel вместо explicit CLI-вызова.** Model не нужно помнить про `invalidate_manifest_cache()`; любой Write/Edit под `skills/`|`tools/` авто-touch'ит `<data_dir>/run/skills.dirty`. Spike S3.a подтвердил — Pre+Post coexist в одном `hooks` dict.
7. **Sentinel-based hot reload.** `ClaudeBridge._render_system_prompt` чекает sentinel при **каждом** turn'е (до `build_manifest`). Race 2 chat-ов на unlink → swallow `FileNotFoundError` (idempotent).
8. **3-way `allowed-tools` semantics (B-1/B-6).** `None` (missing) → permissive + warn `skill_permissive_default`; `[]` → honest lockdown, но **в phase 3 не enforced** (warn `skill_lockdown_not_enforced`). Phase 4 будет переключать baseline на per-skill gating.
9. **H-1 `_PYTHON_ALLOWED_PREFIXES=("tools/", "skills/")`.** Anthropic'овый `skill-creator` кидает helper scripts в `skills/skill-creator/scripts/*.py`. Без skills-prefix — bootstrap fail "python script must live under tools/".
10. **Cache-by-URL + re-fetch + SHA compare (Q4).** Preview пишет `<data_dir>/run/installer-cache/<sha256(canonical_url)[:16]>/{bundle,manifest.json}`. Install re-fetch'ит в `verify/`, считает `sha256_of_tree`, сверяет. Mismatch → `rmtree(cache) + exit 7` + `diff_trees` stderr. Аргумент `--bundle-sha` удалён: модель теряла hash между turn'ами.
11. **Fire-and-forget bootstrap с drain.** `_bg_tasks: set` + `add_done_callback(discard)` держит ref (иначе GC). `Daemon.stop` drain'ит `asyncio.wait_for(gather(..., return_exceptions=True), 5s)` + cancel-escape.
12. **O_EXCL + JSON-marker с rotation.** `<data_dir>/run/.bootstrap_notified` — защита от двух источников спама: (a) parallel daemons; (b) persistent fail на каждом рестарте. Re-notify iff rc изменился ИЛИ >7д. Успешный bootstrap clear'ит marker.
13. **`urllib.request` + `_SafeRedirectHandler`.** SSRF-classify каждого redirect target. Без этого — URL на public CDN мог 302-ить на `http://169.254.169.254/latest/meta-data` и installer бы аккуратно скачал cloud-credentials.
14. **Hardlink reject (`st_nlink > 1`).** `Path.is_symlink()` / `resolve()` **не** ловят hardlink на `/etc/passwd` — validator был дыряв. Review must-fix #4 добавил явный `st_nlink > 1` reject (тест skip'ается на хостах где tmp на другом FS и hardlink невозможен).
15. **Port canonicalisation `:80`/`:443` strip.** `_canonicalize_url` фолдит `github.com:443/x` в `github.com/x` — иначе две cache-entry на ту же URL + двойной TOCTOU check.

## 4. Процесс

**Участники (15+ агент-вызовов):**

1. Plan v1 (planner) — `description.md` + `detailed-plan.md`.
2. Interactive Q&A Q1–Q8 + M1–M3 — все Recommended вопросы закрыты пользователем, Q1 и Q2 override (свой CLI → Anthropic's skill-creator; `httpx` → `gh` CLI).
3. Devil's-advocate wave 1 на план — закрыт 8 items (B1/B2/B3/G3/G5/G6/G7/G9). B-1: реальный marketplace layout под `skills/` (не корень). B-2: permissive-default для missing `allowed-tools`. G-5: fire-and-forget вместо `await`.
4. Researcher SDK+marketplace spike — `spikes/marketplace_probe.py` + `spikes/sdk_probe_posthook.py` + `spike-findings.md` (S1–S5 empirically подтверждены).
5. Implementation.md v1 (researcher, 846 LOC).
6. Devil's-advocate wave 2 на implementation — 6 blockers + 8 strategic + 4 security concerns.
7. Researcher apply fixes → implementation.md v2 (1651 LOC, +805).
8. Coder wave 1 упал на misinterpretation memory rule — coder прочитал старую memory про `.claude/skills` и пошёл по ней вместо текущей. Memory уточнена (generic learning), coder возобновлён.
9. Coder → 4 коммита (`5e00df2` planning docs → `03c2dba` B-1/B-6/H-1/gh-git-uv allowlist → `888b376` skill-installer CLI → `dd3a25a` PostToolUse+URL+Daemon bootstrap/sweeper).
10. **Параллельно** code-reviewer + devils-advocate на commit `dd3a25a` — непересекающиеся issues. Reviewer: "fix-then-ship" + style/test-coverage nits. Devils: "reconsider aspects" — SSRF redirect, tarball endpoint, hardlink, `gh -H`/`--hostname`, port canon.
11. Coder fix-pack (5 🔴 + 7 🟡 + 5 🟢 = 17 items) → 2 финальных коммита (`1f298b4` hardening; `3bf8437` test coverage + marker rotation semantics + nice-to-haves).
12. Researcher summary (этот документ) + Plan phase 4 v1.

**Что сработало:**

- **Спайк до кодинга.** `spike-findings.md` зафиксировал: 17 скилов под `skills/` (не корень), zero symlinks, все bundles ≤5.5 MB, ни один не объявляет `allowed-tools`. Без спайка coder бы угадывал structure.
- **Tarball discovery в reverse-PR.** Review wave 2 девилс-адвокат предложил tarball endpoint (rate-limit relief 60→1). Это обратимый design change — applied в fix-pack без роллбэка.
- **Parallel reviewers после coder.** Code-review ловил code quality; devils security/architecture. Непересекающиеся findings → 17 items не дублировались.
- **Memory rule gotcha.** Coder wave 1 прочитал устаревшую pre-phase-2 memory про `.claude/skills` и пошёл по ней. Добавили generic-уровня learning "check current state before trusting memory" — не project-specific, переиспользуемо.

**Размер diff'ов:**

| Коммит | Файлов | +/− | Суть |
|---|---|---|---|
| `5e00df2` | 8 | +3290 / −33 | Planning docs + spike artifacts |
| `03c2dba` | 8 | +801 / −97 | B-1/B-6 sentinel + H-1 skills/ prefix + gh/git/uv allowlist |
| `888b376` | 24 | +2089 / −38 | skill-installer CLI (stdlib-only) + 10 test файлов |
| `dd3a25a` | 13 | +1132 / −7 | PostToolUse + URL detector + Daemon bootstrap/sweeper |
| `1f298b4` | 6 | +368 / −157 | Fix-pack A: SSRF redirect + tarball + hardlink + gh flags + port canon |
| `3bf8437` | 14 | +967 / −53 | Fix-pack B: test coverage + marker rotation + nice-to-haves |
| **Итого** | **55** | **+8406 / −144** | |

## 5. Security-hardening breakdown

Фикс-пак review A + B закрыл 17 items. Каждый **🔴** — реальный bypass в предыдущем коде `dd3a25a` или раньше.

| # | Pri | Issue | Bypass | Фикс | Тест |
|---|---|---|---|---|---|
| 1 | 🔴 | SSRF redirect bypass — легитимный URL 302-ит на IMDS | `urllib.request` follow 10 redirects без re-validate | `_SafeRedirectHandler.redirect_request` → `classify_url_sync` на каждый target | `tests/test_fetch_redirect_to_imds_blocked.py` (4 cases) |
| 2 | 🔴 | `gh api -H "X-HTTP-Method-Override: DELETE"` writes через header | Forbidden-flags не включал `-H`/`--header` | `_GH_FORBIDDEN_FLAGS` += `{-H, --header, --hostname, --cache, -p, --preview}` | `tests/test_bash_allowlist_gh_cli.py` (5 allow + 19+ deny) |
| 3 | 🔴 | Rate-limit: marketplace install делал 84+ `gh api` на bundle | Recursive contents-walk | Tarball endpoint (1 call) + stdlib `tarfile` safe extract (`filter="data"`) | `tests/test_marketplace_install_via_tarball.py` (6 cases) |
| 4 | 🔴 | Hardlink в bundle → `resolve()` не видит, copytree копирует содержимое | `is_symlink()` false для hardlink | `st_nlink > 1` reject в `_reject_unsafe_paths` | `tests/test_skill_installer_hardlink_rejected.py` (skipped на cross-FS) |
| 5 | 🔴 | Port canon: `github.com:443/x` vs `github.com/x` → 2 cache entries | `urlsplit` не нормализует default port | `_canonicalize_url` strip `:80`/`:443` | `tests/test_canonicalize_url_port_normalization.py` (4 cases) |
| 6 | 🟡 | Non-default branch без `gh` падает silent | contents-walk не fallback'ится | `_fetch_github_tree_fallback` warns + работает только для `main/master`; non-default → explicit FetchError | `tests/test_fetch_non_default_ref_requires_gh.py` (3 cases) |
| 7 | 🟡 | Bootstrap fail silent при persistent error | `log.warning` only, owner не узнаёт | `_bootstrap_notify_failure` Telegram alert + marker rotation | `tests/test_bootstrap_marker_rotation.py` (12 cases) |
| 8 | 🟡 | `..` segments в GitHub URL прoходят regex `[A-Za-z0-9_.-]+` | Regex не различает `.` и `..` | `_reject_dotdot_segments(urlparse.path)` на каждом entry path | `tests/test_skill_installer_fetch_mock.py::test_dotdot_rejected` |
| 9 | 🟡 | Symlink в bundle смузит `copytree(symlinks=False)` (default follows) | Validator мог забыть | `copytree(symlinks=True)` defence-in-depth даже после validator-reject | `tests/test_skill_installer_symlink_rejected.py` (3 cases) |
| 10 | 🟡 | `Daemon.stop` hang при deadlock'нутом bg-task | `gather` без timeout | `asyncio.wait_for(gather, 5s)` + cancel-escape | `tests/test_daemon_stop_drains_bg_tasks.py` (2 cases) |
| 11 | 🟡 | Marker race: parallel daemons оба notify | `exists()` check + write не атомарно | `O_CREAT\|O_EXCL\|O_WRONLY\|O_TRUNC` at open-syscall | `tests/test_bootstrap_marker_rotation.py::test_o_excl_race` |
| 12 | 🟡 | `ts_epoch` not present → на каждом старте re-notify | Corrupt/incomplete marker cold-start | Corrupt → unlink + True; rc-change unlocks re-notify | covered above |
| 13 | 🟢 | `sha256_of_tree` digest flapping — `.git/` внутри cloned bundle | Не skip'ал мета-директории | `_HASH_SKIP_PART_NAMES = {.git, __pycache__, .ruff_cache, .mypy_cache, .pytest_cache}` | `tests/test_sha256_of_tree.py` (6 cases) |
| 14 | 🟢 | `gh` banner "update available" ломал JSON parse | `json.loads(stdout)` падает | `_parse_gh_json` skip'ает banner-строки до первой `{`/`[` | `tests/test_skill_installer_marketplace_list.py` |
| 15 | 🟢 | Empty marker test после O_EXCL refactor стал false-positive | `touch()` пустой → corrupt → re-notify infinitely | Test seed'ит valid JSON marker | `tests/test_bootstrap_skill_creator.py` |
| 16 | 🟢 | `diff_trees` memory-bound при >400 MB bundle | Worst-case `read_bytes` × 2 | Validator cap 100×2MB + docstring note | `install.py::diff_trees` docstring |
| 17 | 🟢 | `_bg_tasks` GC floating task на Py3.12+ `RuntimeWarning` | `create_task` без ref | `set` + `add_done_callback(discard)` pattern | `tests/test_daemon_stop_drains_bg_tasks.py` |

## 6. Отложенный технический долг (для phase 4+)

| # | Pri | Замечание | Файл:строка | Фаза закрытия |
|---|---|---|---|---|
| 1 | 🔴 | History-replay drop'ает tool_use/tool_result — multi-turn memory теряет контекст (inherited из phase 2) | `src/assistant/bridge/history.py:14-28` | **Phase 4**: U1 verify live / `resume=session_id` / расширить synthetic note кратким summary tool_result'а |
| 2 | 🟡 | `cmd_status` — stub "unknown"; не проверяет runtime здоровья скилла | `tools/skill-installer/main.py` + `skills/skill-installer/SKILL.md:28-29` | **Phase 4+**: polling runtime-health через `uv sync --dry-run` или первый ping |
| 3 | 🟡 | Async `uv sync` status-polling CLI — заглушка; ни один Anthropic скилл не имеет pyproject | `tools/skill-installer/main.py` (нет cmd_status, нет background task) | **Phase 5+**: если появятся скилы с deps |
| 4 | 🟡 | Per-skill enforcement `allowed_tools` — phase 3 использует global baseline | `src/assistant/bridge/skills.py:109-124` (warnings only); `src/assistant/bridge/claude.py:93-97` (статический allowed_tools) | **Phase 4**: merge per-skill sets в `_build_options`; requires SDK per-skill hook partition |
| 5 | 🟡 | `_fetch_github_tree_fallback` без `gh` работает только для default branch | `tools/skill-installer/_lib/fetch.py:55` (`_DEFAULT_REFS = {"main", "master"}`) | **Phase 4**: либо require `gh` явно, либо raw.githubusercontent.com per-file walk с auth header |
| 6 | 🟡 | IDN / punycode hostnames в SSRF classifier | `src/assistant/bridge/net.py:67-99` (`classify_url` не нормализует IDN) | **Phase 5+**: `idna.encode(hostname)` до resolve |
| 7 | 🟡 | Manifest cache 1-с FS granularity окно (inherited из phase 2) | `src/assistant/bridge/skills.py:66-75` | **Phase 4**: mtime+size tuple в cache key |
| 8 | 🟢 | Marker TTL rotation разумный, но metrics/alerting вручную | `src/assistant/main.py:220-298` | **Phase 5+ ops**: Prometheus counter на re-notify? |
| 9 | 🟢 | `anthropics/skills` marketplace single-source; конфигурабельность отложена | `tools/skill-installer/_lib/marketplace.py:16-18` (hardcoded) | **Phase 6+**: если появится 3rd-party marketplace |
| 10 | 🟢 | URL detector `_URL_DETECT_MAX=3` — если юзер прислал 10 URL, последние 7 silent | `src/assistant/handlers/message.py:34` | **Phase 6+ UX**: либо warn "много URL", либо debounce |
| 11 | 🟢 | Sandboxing runtime'а установленных tools не реализован (документировано в description.md:65) | — | **Phase 7+**: subprocess isolation / nsjail |

## 7. Метрики

**LOC исходников (без тестов):**
- `src/assistant/` — **2594** LOC в 20 `.py` (+740 vs phase 2 end):
  - `bridge/hooks.py`: 544 → **765** (+221 — gh/git/uv validators + PostToolUse).
  - `bridge/claude.py`: 195 → **250** (+55 — sentinel check + system_notes + PostToolUse wiring).
  - `bridge/skills.py`: 110 → **152** (+42 — 3-way allowed-tools semantics + telemetry).
  - `bridge/net.py`: NEW **102** (extracted from hooks).
  - `handlers/message.py`: 190 → **236** (+46 — URL detector).
  - `main.py`: 128 → **402** (+274 — bg-tasks + bootstrap + marker rotation + drain).
- `tools/skill-installer/` — NEW **1379** LOC в 9 `.py`:
  - `main.py`: 370. `_lib/fetch.py`: 310. `_lib/validate.py`: 268. `_lib/marketplace.py`: 130. `_lib/install.py`: 110. `_lib/_net_mirror.py`: 104. `_lib/preview.py`: 62. `_lib/__init__.py`: 7.

**LOC тестов:** **4270** строк в **44** файлах (было 1434 LOC / 16 файлов; +2836 LOC / +28 файлов). 282 passed + 1 skipped (hardlink test — cross-FS unsupported на tmp этого хоста).

**Коммиты phase 3:** 6 (`5e00df2`, `03c2dba`, `888b376`, `dd3a25a`, `1f298b4`, `3bf8437`). Total diff: 55 файлов, +8406 / −144.

**Критический bootstrap timing:** `tests/test_bootstrap_skill_creator.py::test_daemon_start_under_500ms_regardless_of_bootstrap_outcome` — `Daemon.start()` завершается **<500ms** при любом исходе bootstrap (gh missing, timeout, rc!=0, skill-present). Fire-and-forget гарантия документирована инвариантом теста.

**Marketplace efficiency:** tarball endpoint делает **1 gh-call** на bundle vs **84+** при старом contents-walk (`skills/canvas-design` — worst case, spike S1.c). Anonymous rate-limit 60 req/h → 60 install'ов в час вместо 0.7.

**CI-gates:** `uv sync` OK, `just lint` зелёный (ruff check + format-check + mypy strict), `just test` — **282 passed + 1 skipped** в ~7s.

## 8. Готовность к phase 4

**Готово без новых архитектурных решений:**

- Sentinel hot-reload (`ClaudeBridge._check_skills_sentinel` + PostToolUse) — memory-скилл сможет писать `skills/memory/SKILL.md` и мгновенно регистрироваться.
- Path-guard (`_is_inside_skills_or_tools`, `check_file_path`) + Bash allowlist (`gh/git/uv`) — memory-скилл сможет писать vault-файлы если их путь попадает под allowed prefix.
- 3-way allowed-tools semantics (`None` / `[]` / `list`) — per-skill enforcement можно включать точечно, без breaking changes.
- `IncomingMessage.origin = "scheduler"` (phase 2) — phase 5 scheduler может инжектить memory-recall turn'ы без telegram envelope.
- Stdlib-only installer pattern + `_net_mirror` — phase 4 может добавить similar stdlib-only helper (FTS5 indexer CLI?) без coupling'а.

**Требует решения в phase 4 planning:**

1. **History replay strategy (inherited блокер).** Текущий `history_to_user_envelopes` drop'ает tool_use/tool_result → memory-recall блоки теряются в multi-turn. Три пути: (a) U1 verify live — SDK может принять replay tool блоков → enable; (b) `resume=session_id` — bypass history вообще; (c) synthetic summary с выжимкой tool_result (короткая, но информативная).
2. **Per-skill enforcement `allowed_tools`.** Phase 3 отложил. Phase 4 должна научить `_build_options` merge'ить per-skill sets. Вопрос: как SDK hooks'ам сказать "этот hook активен только когда active skill == memory"?
3. **FTS5 index + vault path.** Где хранить? `<data_dir>/vault/memory.db`? `<project_root>/data/vault/`? Кросс-FS rename при atomic update?
4. **Concurrent write lock на vault.** Phase 2 имеет per-chat `asyncio.Lock`, но memory — другой layer (могут writer'ить из разных chat'ов одновременно). SQLite WAL + PRAGMA busy_timeout vs отдельный `aiosqlite.Connection` + lock?
5. **Sandbox runtime для чужого кода.** Не решение phase 4, но фаза может вскрыть нужду: memory-скилл от Anthropic может захотеть `Bash` (например `sqlite3`-интеграции). Нужно ли expand allowlist, или memory-скилл должен быть Python-only через `Read/Write`?

---

Phase 3 закрыт. Security-surface расширен до skill-install flow (SSRF-redirect, hardlink, TOCTOU, gh header/hostname flags — все закрыты). Auto-bootstrap `skill-creator` работает при первом старте (<500ms fire-and-forget). Marketplace-install оптимизирован до 1 gh-call через tarball endpoint. 282 теста фиксируют инварианты (+175 vs phase 2). Phase 4 (memory skill) разблокирован на уровне sentinel/hot-reload/path-guard API; открытые вопросы — в плоскости enforcement (per-skill allowed_tools) и history-replay (inherited из phase 2).
