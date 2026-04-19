# Phase 8 — Detailed plan

Based on description.md @ 48d3ccb. Revision r2 — devil wave 1 fixes + Q9-Q12 decisions applied.

## §1 Цель и scope

Phase 8 расширяет бота по двум ортогональным осям. **Ось 1 (GitHub operations)** — тонкая CLI-обёртка `tools/gh/main.py`, которую модель вызывает через `Bash` для read-only issue/PR/repo introspection плюс write-операции типа `issue create`. Существующий phase-3 `_validate_gh_invocation` (read-only `gh api` + `gh auth status`) остаётся нетронутым; новый канал проходит через `_validate_python_invocation → _validate_gh_argv` (phase-7 pattern). **Ось 2 (daily vault auto-commit)** — default-seeded scheduled job, материализуемый `SchedulerLoop` в 03:00 локального TZ, делегируемый handler'у, модель вызывает `tools/gh/main.py vault-commit-push`, CLI выполняет узкий path-pinned commit+push в dedicated GitHub-аккаунт по SSH deploy key.

**Чего не делаем (out of scope, ссылка на §"Явно НЕ в phase 8"):** PR creation через `gh pr create`, issue close/comment/edit, `gh api -X POST/PUT/PATCH/DELETE`, `git push --force*`, auto-rebase при divergence, encrypted vault, webhook receive, GitHub Actions, multi-vault, GitHub-App OAuth. Inline-keyboard approval workflow явно переносится на phase 9. `HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap остаётся phase-4 carryover.

Phase 8 СОХРАНЯЕТ все phase-7 инварианты: `make_subagent_hooks(*, store, adapter, settings, pending_updates, dedup_ledger)` signature (I-7.4), `_DedupLedger` TTL=300/cap=256 (I-7.1), media retention sweeper трогает ТОЛЬКО `<data_dir>/media/` (I-7.2 — `<data_dir>/vault/` не попадает в sweep), `dispatch_reply` + `_ARTEFACT_RE` v3 не меняются (gh CLI output не содержит outbox-paths, значит regex false-positive невозможен).

## §2 Wave 0 pre-flight — genimage X-1/X-2 hotfix + xpassed investigation

**Single commit** до старта phase-8 основных работ. Closes технический долг из phase-7 summary §4.

### X-1: `_check_and_increment_quota` не проверяет shape распарсённого JSON

**Файл:** `tools/genimage/main.py`
**Функция:** `_check_and_increment_quota` (line ~295)
**Текущий баг:** после `state = json.loads(raw.decode("utf-8")) if raw else {}` (line ~335) код предполагает, что `state` это `dict` — вызовы `state.get("date")` и `state.get("count", 0)` крашатся с `AttributeError`, если quota-файл распарсился в `list` / scalar (e.g. при disk-fill mid-write или ручном hand-edit).
**Фикс:** после `json.loads(...)` добавить `if not isinstance(state, dict): state = {}`. Симметричный с `_read_quota_best_effort` (line ~376) где guard уже присутствует.
**Тест:** снять `@pytest.mark.xfail(strict=True)` с `tests/test_genimage_quota_midnight_rollover.py::test_wrong_shape_list_payload_recovers`.

### X-2: `_read_quota_best_effort` пропускает `UnicodeDecodeError`

**Файл:** тот же
**Функция:** `_read_quota_best_effort` (line ~363)
**Текущий баг:** `path.read_text(encoding="utf-8")` падает с `UnicodeDecodeError` если quota-файл содержит binary bytes (partial fsync after crash); exception протекает к caller'у вместо graceful empty-dict.
**Фикс:** `path.read_bytes()` + `raw.decode("utf-8", errors="replace")` ДО `json.loads`; либо `except (OSError, UnicodeDecodeError): return {}`. Предпочтительно bytes-path с errors=replace — консистентно с locked write-path.
**Тест:** снять xfail с `tests/test_genimage_quota_midnight_rollover.py::test_best_effort_reader_binary_input_xfail`.

### Xpassed investigation: `test_dedup_ttl_real_clock`

**Файл:** `tests/test_dispatch_reply_dedup_ledger.py` (line ~157)
**Статус:** `xfail(strict=False, reason="real clock dependent")`. Timing-variant; в CI на loaded runner flakes.
**Решение:** НЕ снимать xfail, оставить как smoke-detector. Альтернатива с `pytest-rerunfailures` — отклонена (не расширяем зависимости).

**Ожидаемый xfail count после Wave 0:** 5 → 3 (X-1, X-2 closed; остаются 3 S-2 regex adjacency residuals).

## §3 Новые файлы / модифицированные

| Path | New/Modified | Purpose |
|------|-------------|---------|
| `tools/gh/__init__.py` | **New** | Empty package marker |
| `tools/gh/main.py` | **New** (~350 LOC) | CLI entrypoint: `auth-status`, `issue create/list/view`, `pr list/view`, `repo view`, `vault-commit-push` |
| `tools/gh/pyproject.toml` | **New** | `uv` project with only stdlib deps (subprocess-only) |
| `tools/gh/_lib/__init__.py` | **New** | Package marker |
| `tools/gh/_lib/git_ops.py` | **New** (~120 LOC) | `git -C` wrappers: `is_inside_work_tree`, `diff_quiet`, `add_path_pinned`, `commit_only_path`, `push_with_ssh_key`, `rev_parse_head` |
| `tools/gh/_lib/gh_ops.py` | **New** (~100 LOC) | `gh auth status`, `gh issue/pr/repo` subprocess wrappers returning JSON |
| `tools/gh/_lib/lock.py` | **New** (~40 LOC) | `fcntl.flock`-based context manager on `<data_dir>/run/gh-vault-commit.lock` |
| `tools/gh/_lib/vault_git_init.py` | **New** (~60 LOC) | Bootstrap: `git init` + `remote add vault-backup` + initial branch setup внутри `<data_dir>/vault/` на первом vault-commit-push |
| `tools/schedule/main.py` | **Modified** | Расширение `rm` на insert tombstone если row имеет seed_key; новый subcommand `revive-seed <seed_key>` удаляет tombstone |
| `tools/gh/_lib/repo_allowlist.py` | **New** (~30 LOC) | Parse `GH_ALLOWED_REPOS` + extract `owner/repo` from `git@github.com:owner/repo.git` ssh url |
| `tools/gh/_lib/exit_codes.py` | **New** | Module constants: `OK=0, ARGV=2, VALIDATION=3, GH_NOT_AUTHED=4, NO_CHANGES=5, REPO_NOT_ALLOWED=6, DIVERGED=7, PUSH_FAILED=8, LOCK_BUSY=9, SSH_KEY_ERROR=10` |
| `skills/gh/SKILL.md` | **New** (~120 LOC) | Frontmatter + dialog examples + exit-code matrix (H-13 pattern) |
| `src/assistant/config.py` | **Modified** | Add `GitHubSettings(env_prefix="GH_")` class + `Settings.github` field (inline, NOT separate settings/ package) |
| `src/assistant/scheduler/seed.py` | **New** (~80 LOC) | `_ensure_vault_auto_commit_seed(store, gh_settings)` — idempotent seed by `seed_key` |
| `src/assistant/state/migrations/0005_schedule_seed_key.sql` | **New** | `ALTER TABLE schedules ADD COLUMN seed_key TEXT` + partial UNIQUE INDEX |
| `src/assistant/state/migrations/0006_seed_tombstones.sql` | **New** | `CREATE TABLE seed_tombstones(seed_key TEXT PRIMARY KEY, deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)` |
| `src/assistant/state/db.py` | **Modified** | Bump `SCHEMA_VERSION = 4 → 5` + add `_apply_v5()` + dispatch branch |
| `src/assistant/scheduler/store.py` | **Modified** | Extend `insert_schedule` with optional `seed_key` + `find_by_seed_key` accessor |
| `src/assistant/main.py` | **Modified** | `Daemon.start`: call `_ensure_vault_auto_commit_seed` after `apply_schema` + `clean_slate_sent` |
| `src/assistant/bridge/hooks.py` | **Modified** | Add `_validate_gh_argv` + dispatch branch в `_validate_python_invocation` для `tools/gh/main.py` |
| `tools/genimage/main.py` | **Modified (Wave 0)** | X-1 shape guard + X-2 UnicodeDecodeError fallback |
| `docs/ops/github-setup.md` | **New** (~150 LOC) | Deploy-key creation walkthrough, env-var template, rotation procedure |
| `tests/test_gh_validate_argv.py` | **New** | Argv allow/deny matrix для `_validate_gh_argv` |
| `tests/test_gh_vault_commit_push_happy.py` | **New** | Mock subprocess happy-path |
| `tests/test_gh_vault_commit_push_no_changes.py` | **New** | Empty diff → exit 5 |
| `tests/test_gh_vault_commit_push_diverged.py` | **New** | Non-ff push → exit 7 |
| `tests/test_gh_vault_commit_path_isolation.py` | **New** | CRITICAL: `data/media/`, `data/run/`, `data/assistant.db` не попадают в commit |
| `tests/test_gh_vault_git_bootstrap.py` | **New** | Первый push на empty vault_dir → `git init` + `remote add` + `git add` + push (local bare repo test infra) |
| `tests/test_gh_dispatch_reply_no_artefact_match.py` | **New** | Corpus 30+ synthetic gh CLI model responses → assert `_ARTEFACT_RE.search(text) is None` (B5/Q11) |
| `tests/test_gh_issue_create_happy.py` | **New** | Mock `gh issue create` |
| `tests/test_gh_repo_whitelist.py` | **New** | Non-allowed `--repo` → exit 6 |
| `tests/test_gh_seed_idempotency.py` | **New** | Double `Daemon.start` не дублирует row |
| `tests/test_gh_seed_tombstone.py` | **New** | `rm` seeded row → tombstone создан → next `Daemon.start` **не** re-seed'ит; `revive-seed` → tombstone удалён → next start re-creates |
| `tests/test_gh_seed_disabled.py` | **New** | `auto_commit_enabled=False` → no seed |
| `tests/test_gh_flock_concurrency.py` | **New** | 2 parallel `vault-commit-push` → second exits 9 |
| `tests/test_gh_bash_hook_integration.py` | **New** | `_validate_python_invocation` routes to `_validate_gh_argv` |
| `tests/test_gh_skill_md_assertion.py` | **New** | H-13 SKILL.md structural assertion |
| `tests/test_gh_ssh_key_missing.py` | **New** | Missing key file → exit 10 with actionable message |
| `tests/test_gh_auth_status_probe.py` | **New** | Unauthed `gh` → exit 4 (I-8.7 guard) |
| `tests/test_gh_settings_ssh_url_validation.py` | **New** | HTTPS URL rejected at pydantic layer |

**Settings placement.** Проект использует single-module `src/assistant/config.py` (не `settings/` package). `GitHubSettings` inline в `config.py` — консистентно с `ClaudeSettings`, `MemorySettings`, `SchedulerSettings`, `SubagentSettings`, `MediaSettings`. Description drift: упоминает "`settings/github.py`" — нужно скорректировать.

**Migration numbering drift:** description упоминает `0004_schedule_seed_key.sql`, но `0004_subagent.sql` уже существует. Правильный номер — `0005_schedule_seed_key.sql`.

**Deployment topology (Q9 decision).** `vault_dir` — **standalone git repository**, отдельный от main project's `0xone-assistant` git checkout. Default XDG setup (`<data_dir> = ~/.local/share/0xone-assistant/`, `<vault_dir> = <data_dir>/vault/`) — ortogonal к `project_root`. `vault-commit-push` вообще не обращается к `project_root`; все git commands проходят через `git -C <vault_dir>` или `cwd=<vault_dir>`. Первый push вызывает bootstrap: `git init`, `git remote add vault-backup <vault_remote_url>`, `git checkout -b main`, затем стандартный flow. Helper `tools/gh/_lib/vault_git_init.py`.

## §4 Commit-by-commit breakdown

### C0: Wave 0 pre-flight (genimage X-1/X-2)

**Files:**
- `tools/genimage/main.py` (edit `_check_and_increment_quota` + `_read_quota_best_effort`)
- `tests/test_genimage_quota_midnight_rollover.py` (remove `xfail(strict=True)` на 2 tests)

**Tests:** `test_wrong_shape_list_payload_recovers` passes strictly; `test_best_effort_reader_binary_input_xfail` passes strictly (переименовать в `test_best_effort_reader_binary_input_recovers`).
**Dependencies:** none (phase-7 HEAD).

### C1: `GitHubSettings` + URL validation

**Files:**
- `src/assistant/config.py` (add `GitHubSettings` + wire в `Settings.github`)
- `tests/test_gh_settings_ssh_url_validation.py`

**Поля `GitHubSettings(BaseSettings)` (env_prefix `GH_`):**
- `vault_remote_url: str` (required; pydantic validator — must match `r"^git@github\.com:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.git$"` (SF2: углерод из `\w` в ASCII-only); дополнительно reject если `..` в owner или repo segment; reject `https://`)
- `vault_ssh_key_path: Path = Path.home() / ".ssh" / "id_vault"` (pydantic validator — B6/SF: reject paths с whitespace, shell metacharacters `$;&|<>'"\\` и substring `" -o "`; также `_verify_key_permissions` на startup: `stat.st_mode & 0o077 == 0`, иначе warning)
- `vault_remote_name: str = "vault-backup"` (Q5 — избегаем конфликта с existing `origin` = main project repo)
- `vault_branch: str = "main"`
- `auto_commit_enabled: bool = True`
- `auto_commit_cron: str = "0 3 * * *"` (validated via `assistant.scheduler.cron.parse_cron`)
- `auto_commit_tz: str = "Europe/Moscow"` (Q6 — основной TZ owner'а; IANA zone через `zoneinfo`)
- `commit_message_template: str = "vault sync {date}"` (fixed-template, ровно `{date}` placeholder)
- `commit_author_email: str = "vaultbot@localhost"`
- `allowed_repos: tuple[str, ...] = ()` (tuple of `owner/repo` slugs; parsed from comma-separated env)

**Deferred pattern:** если `vault_remote_url` пуст — `auto_commit_enabled` auto-disable с warning в Daemon.start.

**Tests:**
- ssh URL format accepted
- https URL rejected (`ValidationError`)
- `allowed_repos` parsed from `GH_ALLOWED_REPOS="foo/bar,baz/qux"` env
- cron validation integrates с `parse_cron`
- empty `vault_remote_url` with `auto_commit_enabled=True` → auto-disable path
- `vault_remote_url` с unicode characters (кириллица) → rejected (SF2)
- `vault_remote_url` с `..` в segments → rejected (SF2)
- `vault_ssh_key_path` с пробелом (`/tmp/id vault`) → rejected (B6)
- `vault_ssh_key_path` с подстрокой ` -o ` → rejected (B6)
- `vault_ssh_key_path` с mode 0o644 → startup warning logged (B6)

**Dependencies:** C0.

### C2: `tools/gh/` scaffolding + `auth-status` subcommand + SKILL.md draft

**Files:**
- `tools/gh/__init__.py`, `tools/gh/main.py` (skeleton), `tools/gh/pyproject.toml`, `tools/gh/_lib/{__init__,gh_ops,exit_codes}.py`
- `skills/gh/SKILL.md`
- `tests/test_gh_skill_md_assertion.py`
- `tests/test_gh_auth_status_probe.py`

**`main.py` skeleton** — argparse subparsers: `auth-status`, `issue`, `pr`, `repo`, `vault-commit-push`. Exit codes из `exit_codes.py`.

**`auth-status`:** pure read-only `gh auth status --hostname github.com` → rc=0 → exit OK + JSON `{"ok": true}`; rc≠0 → exit GH_NOT_AUTHED=4 + JSON `{"ok": false, "error": "not_authenticated"}`.

**SKILL.md** — frontmatter `name: gh`, `description: GitHub issues/PRs/repos + daily vault backup commits`, `allowed-tools: [Bash]`. Exit code matrix table. 5 dialog examples (mirror description.md §Выход).

**Tests:**
- SKILL.md H-13 structural assertion
- `auth-status` on unauthed mocked gh → exit 4
- `auth-status` on authed → exit 0

**Dependencies:** C1.

### C3: `tools/gh/` — issue/pr/repo read-only operations

**Files:**
- `tools/gh/main.py` (extend with `issue create/list/view`, `pr list/view`, `repo view`)
- `tools/gh/_lib/repo_allowlist.py`
- `tools/gh/_lib/gh_ops.py` (`run_gh_json(args) -> dict`)
- `tests/test_gh_issue_create_happy.py`
- `tests/test_gh_repo_whitelist.py`

**Allow-list enforcement (I-8.5):** EVERY subcommand с `--repo OWNER/REPO` проверяет against `GitHubSettings.allowed_repos` ДО вызова `gh`. Mismatch → exit REPO_NOT_ALLOWED=6.

**Env isolation (Q2 decision):** Каждый subprocess вызов `gh` в `gh_ops.py` делает `env = os.environ.copy(); env.pop("GH_TOKEN", None); env.pop("GITHUB_TOKEN", None)` и передаёт `env=env` в `subprocess.run`. Это форсирует `gh` CLI использовать OAuth session из `~/.config/gh/` вместо env-токена, если owner выставил `GITHUB_TOKEN` для другого инструмента. R-3 закрыт.

**`issue create`:** argv `--repo X --title Y --body Z [--label L]*`. Вызывает `gh issue create --repo X --title Y --body Z --json url,number [--label L]*`.

**`issue list/view`, `pr list/view`, `repo view`:** чистые read wrappers. Все команды add `--json` field selectors.

**Tests:**
- Mock `gh issue create` returns JSON; CLI exit 0 с pass-through JSON
- `--repo otherowner/private` не в allowed → exit 6, БЕЗ вызова gh
- `pr view 15 --repo allowed/repo` → gh CLI invoked с `--json title,body,state,mergeable`

**Dependencies:** C2.

### C4: `tools/gh/` — `vault-commit-push` subcommand

**Files:**
- `tools/gh/main.py` (extend)
- `tools/gh/_lib/git_ops.py` (git wrappers)
- `tools/gh/_lib/lock.py` (flock helper)
- `tests/test_gh_vault_commit_push_happy.py`
- `tests/test_gh_vault_commit_push_no_changes.py`
- `tests/test_gh_vault_commit_push_diverged.py`
- `tests/test_gh_vault_commit_path_isolation.py`
- `tests/test_gh_flock_concurrency.py`
- `tests/test_gh_ssh_key_missing.py`

**Subcommand argv:** `vault-commit-push [--message MSG] [--dry-run]`. If `--message` omitted → render `commit_message_template` с `{date}=datetime.now(UTC).strftime("%Y-%m-%d")`.

**Execution flow (Q9 decision: vault_dir-as-own-repo):**

1. **Pre-flight auth probe:** `gh auth status` — best-effort warning (gh auth здесь не блокер, push идёт через SSH). **R-3 closed:** env wipe (Q2) в gh_ops.py.
2. **Validate vault_dir exists** (`<settings.vault_dir>.is_dir()`). Если нет — exit 3 (`vault_not_configured`). vault_dir является standalone git repo, independent от project_root. **project_root вообще не участвует в этом flow.**
3. **Bootstrap если нужно (Q9).** `git -C <vault_dir> rev-parse --is-inside-work-tree` → если fail (vault_dir не git repo) → вызвать `vault_git_init.bootstrap(vault_dir, settings)`: `git init`, `git checkout -b {vault_branch}`, `git remote add {vault_remote_name} {vault_remote_url}`, `git -c user.email={commit_author_email} commit --allow-empty -m "bootstrap"`. Только через subprocess с env-wipe.
4. **Repo allow-list (I-8.5):** extract `owner/repo` из `vault_remote_url` через `repo_allowlist.extract_owner_repo()`. Assert в `allowed_repos`. Иначе exit 6.
5. **SSH key check (B6):** `vault_ssh_key_path.is_file() and os.access(..., os.R_OK)` и `stat.st_mode & 0o077 == 0`. False → exit 10. Permissions warning → log но не block.
6. **Acquire flock (I-8.2):** `fcntl.flock(LOCK_EX | LOCK_NB)` на `<data_dir>/run/gh-vault-commit.lock`. `BlockingIOError` → exit 9 (**Q1**: fail-fast, R-5 closed).
7. **Change-detection (B2, SF):** `git -C <vault_dir> status --porcelain` — parse output; если ПУСТО (нет modified/untracked/deleted/renamed) → exit 5 `no_changes`. **КРИТИЧНО:** `git diff --quiet` НЕ используем (упускает untracked) — **B2 fix**.
8. If `--dry-run` → print planned args + porcelain output → exit 0.
9. **Path-pinned stage (I-8.1):** `git -C <vault_dir> add -A`. **OK здесь `-A`** — мы уже в vault_dir как cwd, `-A` затрагивает только выше vault_dir. **НЕ `git add -A` из project_root** — это принципиально другое поведение.
10. **Commit с inline config overrides (I-8.4):** `git -C <vault_dir> -c user.email={commit_author_email} -c user.name="vaultbot" commit -m <msg>`. `-c` overrides `git config` для этого вызова только.
11. **Render commit message (B4 fix, Q6 TZ):** если `--message` omitted → `datetime.now(ZoneInfo(auto_commit_tz)).strftime("%Y-%m-%d")` для `{date}` placeholder. **НЕ UTC** — матч календарной дате owner'а. **R-10 фиксится здесь.**
12. **Re-check after add (SF, race-window):** `git -C <vault_dir> diff --cached --quiet` — если exit 0 (nothing staged после add, race с external owner rollback) → exit 5 `no_changes` в лог.
13. **Env setup (I-8.4):** `env = os.environ.copy(); env.pop("GH_TOKEN", None); env.pop("GITHUB_TOKEN", None); env["GIT_SSH_COMMAND"] = "ssh -i " + shlex.quote(str(vault_ssh_key_path)) + " -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=" + shlex.quote(str(data_dir / "run" / "gh-vault-known-hosts"))`. **shlex.quote на paths (B6)** выключает shell-split injection.
14. **Push (fail-fast I-8.3):** `git -C <vault_dir> push {vault_remote_name} {vault_branch}` с env + no `--force*`:
    - rc=0 → exit 0 + JSON `{"ok": true, "commit_sha": "...", "files_changed": N}`
    - stderr matches `non-fast-forward` / `(fetch first)` / `rejected` → exit 7
    - other non-zero → exit 8

**Tests:**
- Happy: `git diff --quiet` returns 1 + `git add/commit/push` returns 0 → exit 0
- No changes: diff returns 0 → exit 5
- Diverged: push с `stderr=b"! [rejected] main -> main (non-fast-forward)"` → exit 7
- Path isolation (CRITICAL, I-8.1): seed `<data_dir>/media/outbox/leak.png` + `<data_dir>/assistant.db` + vault file; run CLI; `git show --stat HEAD` содержит ТОЛЬКО `data/vault/*`
- Flock: 2 subprocess через `multiprocessing`; второй exits 9
- SSH key missing: `GH_VAULT_SSH_KEY=/nonexistent` → exit 10
- **Untracked file detection (B2):** seed новый markdown в vault_dir (без `git add`) → run CLI → porcelain detects → exit 0, commit включает новый file
- **Bootstrap (Q9):** empty vault_dir (no .git) → CLI вызывает vault_git_init.bootstrap → init + remote + empty commit, затем user's файл коммитится
- **TZ rendering (B4/R-10):** freeze `datetime.now(ZoneInfo('Europe/Moscow'))` на 01:00 MSK = 22:00 UTC предыдущего дня → commit message должен рендериться в today-MSK (не yesterday-UTC)
- **Push verification via local bare repo (B9/R-14):** `git init --bare /tmp/vault.git` + `vault_remote_url=file:///tmp/vault.git` (test-only, bypass ssh); верифицировать через `git -C /tmp/vault.git log --oneline`
- **GIT_SSH_COMMAND shell-quote (B6):** `vault_ssh_key_path = "/tmp/key with space"` → pydantic validator reject с `ValidationError`; если байпас validator в test (прямой monkeypatch) → env string должен содержать single-quoted path
- **Argv symlink path-isolation (B9):** seed symlink `<vault_dir>/evil → /etc/passwd` → CLI `git add` следует symlink? Это Git default behavior (коммитит symlink metadata, не content) — ассерт что content не exfiltrated
- **.gitattributes LFS (SF3):** seed `.gitattributes` в vault root с `* filter=lfs` → ассерт что CLI не падает; если git-lfs не установлен, push fails чётко с exit 8

**Dependencies:** C1, C2, C3.

### C5: Migration `0005_schedule_seed_key.sql` + seed helper

**Files:**
- `src/assistant/state/migrations/0005_schedule_seed_key.sql`
- `src/assistant/state/db.py` (bump `SCHEMA_VERSION=5` + `_apply_v5`)
- `src/assistant/scheduler/seed.py`
- `src/assistant/scheduler/store.py` (extend `insert_schedule` + `find_by_seed_key`)
- `tests/test_gh_seed_idempotency.py`
- `tests/test_gh_seed_disabled.py`

**Migrations (Q10 tombstone):**

`0005_schedule_seed_key.sql`:
```sql
ALTER TABLE schedules ADD COLUMN seed_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_seed_key
    ON schedules(seed_key) WHERE seed_key IS NOT NULL;
PRAGMA user_version = 5;
```

`0006_seed_tombstones.sql` (Q10 decision):
```sql
CREATE TABLE IF NOT EXISTS seed_tombstones (
    seed_key TEXT PRIMARY KEY,
    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
PRAGMA user_version = 6;
```

**Semantics:** tombstone =означает "owner явно удалил этот seed, не пересоздавай autonomously". Возрождение только через явный `python tools/schedule/main.py revive-seed <key>` → `DELETE FROM seed_tombstones WHERE seed_key=?` → next `Daemon.start` re-seed'ит.

**`tools/schedule/main.py rm` extension (Q10):** В `rm <id>` перед DELETE: прочитать row, если `seed_key IS NOT NULL` → `INSERT OR REPLACE INTO seed_tombstones (seed_key) VALUES (?)`. Затем DELETE. Single transaction.

**`_ensure_vault_auto_commit_seed` pseudocode:**

```
if await store.tombstone_exists("vault_auto_commit"):
    log.info("vault_auto_commit_seed_tombstoned_skip"); return
if not gh_settings.auto_commit_enabled:
    log.info("vault_auto_commit_seed_skipped_disabled"); return
if not gh_settings.vault_remote_url:
    log.warning("vault_remote_not_configured"); return
if not gh_settings.vault_ssh_key_path.is_file():
    log.warning("vault_ssh_key_missing", path=...); return
existing = await store.find_by_seed_key("vault_auto_commit")
if existing:
    log.info("vault_auto_commit_seed_present", schedule_id=...); return
sid = await store.insert_schedule(
    cron=gh_settings.auto_commit_cron,
    prompt="ежедневный бэкап vault: сделай git add data/vault, коммит и git push",
    tz=gh_settings.auto_commit_tz,
    seed_key="vault_auto_commit",
)
log.info("vault_auto_commit_seed_created", schedule_id=sid, cron=...)
```

**Idempotency (I-8.6):** UNIQUE INDEX + pre-INSERT check. Race-safe: pidfile flock на Daemon гарантирует single seed invocation.

**Tests:**
- Double-call seed helper в same test → ровно 1 row
- `auto_commit_enabled=False` → 0 rows
- `vault_remote_url=""` → warning, 0 rows
- Migration v4→v5 → `seed_key` column + index `idx_schedules_seed_key`
- `rm` seeded row → tombstone inserted → next seed call no-op (Q10)
- `revive-seed vault_auto_commit` → tombstone deleted → next seed call re-creates row
- v4 → v5 → v6 sequential migration: ассерт обе схемы applied полностью
- v3 → v6 (skip): ассерт migration chain применяется sequentially (no skip)
- pre-existing schedules rows get `seed_key = NULL` (partial index excludes)

**Dependencies:** C1.

### C6: Daemon integration + scheduler-injected turn handling

**Files:**
- `src/assistant/main.py` (`Daemon.start` — добавить seed call после `clean_slate_sent`)
- `src/assistant/bridge/hooks.py` (add `_validate_gh_argv` + dispatch)
- `tests/test_gh_bash_hook_integration.py`
- `tests/test_gh_validate_argv.py`

**`Daemon.start` diff:** после `reverted = await sched_store.clean_slate_sent()` добавить:

**Migration ordering (SF4):** `apply_schema` (v4→v6) выполняется ПОСЛЕ pidfile flock аквизиции, чтобы OLD daemon's WAL connection не блокировал ALTER TABLE. Pidfile уже механизмом single-daemon защищает.

```python
# B7: gh CLI config accessibility probe (analogous D3 guard)
gh_preflight = _verify_gh_config_accessible()  # tools/gh/_lib/gh_ops.py
if not gh_preflight.ok:
    log.warning("gh_config_not_accessible", home=gh_preflight.home, reason=gh_preflight.reason)
    # non-fatal; seed still runs, но model calls to gh issue/pr будут падать exit 4

# B8: SSH key cloud-sync guard (extend D3)
if settings.github.auto_commit_enabled:
    _check_path_not_in_cloud_sync(settings.github.vault_ssh_key_path.parent, "vault_ssh_key_path")

# B10: allowed_repos preflight sanity
if (settings.github.auto_commit_enabled 
    and settings.github.vault_remote_url
    and not settings.github.allowed_repos):
    log.warning("vault_auto_commit_allowed_repos_empty_will_reject")

# gh version preflight (analogous claude --version from phase 7 fix-pack)
_gh_version = _probe_gh_version()  # best-effort, warning if missing
if _gh_version is None:
    log.warning("gh_cli_not_found_issue_pr_disabled")

# Q10: seed (respects tombstone check internally)
from assistant.scheduler.seed import _ensure_vault_auto_commit_seed
await _ensure_vault_auto_commit_seed(sched_store, self._settings.github)
```

**`_validate_gh_argv`:**

```python
_GH_CLI_SUBCMDS = frozenset({
    "auth-status", "issue", "pr", "repo", "vault-commit-push"
})
_GH_FORBIDDEN_SUBSUBS = frozenset({
    "close", "comment", "edit", "delete", "merge", "create-pr",
})

def _validate_gh_argv(args: list[str]) -> str | None:
    if not args:
        return "gh CLI requires a subcommand"
    sub = args[0]
    if sub not in _GH_CLI_SUBCMDS:
        return f"gh CLI subcommand '{sub}' not allowed"
    seen_flags: set[str] = set()
    for arg in args[1:]:
        if arg.startswith("--"):
            key = arg.split("=", 1)[0]
            if key in seen_flags:
                return f"gh CLI duplicate flag {key}"
            seen_flags.add(key)
    for bad in ("--force", "--force-with-lease", "--no-verify", "--amend"):
        if bad in args:
            return f"gh CLI flag {bad} not allowed"
    # SF5: rate-limit cap on --limit value to prevent pagination burst
    for i, arg in enumerate(args):
        if arg == "--limit" and i + 1 < len(args):
            try:
                if int(args[i + 1]) > 100:
                    return "gh CLI --limit max 100"
            except ValueError:
                return "gh CLI --limit requires integer"
        elif arg.startswith("--limit="):
            try:
                if int(arg.split("=", 1)[1]) > 100:
                    return "gh CLI --limit max 100"
            except ValueError:
                return "gh CLI --limit requires integer"
    if sub == "issue" and len(args) >= 2 and args[1] in _GH_FORBIDDEN_SUBSUBS:
        return f"gh issue subsub '{args[1]}' not allowed (phase 9 keyboard-confirm)"
    if sub == "pr" and len(args) >= 2 and args[1] in ("create", "merge", "close"):
        return f"gh pr subsub '{args[1]}' not allowed (phase 9)"
    return None
```

**Dispatch в `_validate_python_invocation`:**

```python
if script == "tools/gh/main.py":
    return _validate_gh_argv(argv[2:])
```

**Tests:**
- `test_gh_validate_argv`: allow matrix (`auth-status`, `issue list`, `pr view 15 --repo X/Y`, `vault-commit-push --message 'x'`) + deny matrix (`issue create --force`, `pr merge`, `issue close 1`, unknown sub, dup flags)
- `test_gh_bash_hook_integration`: `_validate_bash_argv(["python", "tools/gh/main.py", "auth-status"], project_root)` → None

**Dependencies:** C4, C5.

### C7: E2E tests + docs

**Files:**
- `docs/ops/github-setup.md`
- `tests/test_gh_e2e_scheduler_to_commit.py` (integration, may `skip_unless SDK_INT=1`)
- `README.md` / `CLAUDE.md` — mention phase-8 shipped

**docs/ops/github-setup.md sections:**
1. Create dedicated GitHub account `vaultbot-owner`
2. Create private repo `vaultbot-owner/vault-backup`
3. Generate deploy key: `ssh-keygen -t ed25519 -f ~/.ssh/id_vault -C "vault@host"` + add pub key в repo settings (write access)
4. `.env` template: `GH_VAULT_REMOTE_URL=git@github.com:vaultbot-owner/vault-backup.git`, `GH_ALLOWED_REPOS=vaultbot-owner/vault-backup,owner/0xone-assistant`, `GH_AUTO_COMMIT_TZ=Europe/Moscow`
5. Main `gh auth login` на отдельном host account (для issues/PRs)
6. Rotate procedure: revoke old key в GitHub settings → replace `~/.ssh/id_vault*` → restart daemon
7. Override cron via `tools/schedule/main.py rm <seed_id> && tools/schedule/main.py add "0 4 * * *" "..."`
8. **Disable auto-commit (Q10 decision):** `python tools/schedule/main.py rm <id>` — создаст tombstone, daemon больше не re-seed'ит. Для re-enable: `python tools/schedule/main.py revive-seed vault_auto_commit` + restart daemon.
9. **Encryption warning (Q12):** Vault markdown пушится в GitHub в plaintext. Private repo + separate vaultbot account + write-only deploy key — достаточная защита для стандартного threat model. Для более высокого sensitivity — рассмотреть git-crypt в phase 10+ (out-of-scope для phase 8).
10. **vault_dir as own repo (Q9):** Default XDG путь `<data_dir>/vault/` будет standalone git repo. Bot bootstrap'ит его на первом vault-commit-push. Owner может `git clone git@github.com:vaultbot-owner/vault-backup.git <path>` на другом laptop — independent working tree.

**E2E test:** spawn daemon с mocked Telegram + mocked gh CLI; advance clock past cron; assert commit+push invoked с correct env.

**Dependencies:** C6.

## §5 Invariants (I-8.x)

**I-8.1 — Path-pinned `git add`.** `vault-commit-push` NEVER runs `git add -A`, `git add .`, `git add data/`. Only `git add -- <vault_relpath>`. Commit uses `git commit --only -- <vault_relpath>` — double-guard. Regression test: `test_gh_vault_commit_path_isolation.py`.

**I-8.2 — Single-flight flock.** `fcntl.flock(LOCK_EX | LOCK_NB)` на `<data_dir>/run/gh-vault-commit.lock` FIRST после argv parse. Lock held через push completion. `BlockingIOError` → exit 9 БЕЗ retry. Kernel освобождает flock при die или close(fd). OOM-killed процесс → lock auto-released (R-4 spike).

**I-8.3 — Fail-fast on divergence.** Non-fast-forward push → exit 7. NO `git pull`, `git fetch + merge`, `git rebase`, `--force`, `--force-with-lease`. Owner resolves manually.

**I-8.4 — SSH key isolation.** Deploy key через `GIT_SSH_COMMAND` env в child. НЕ `~/.ssh/config`, НЕ `ssh-agent`, НЕ global git config. `ssh -i {key} -o IdentitiesOnly=yes` гарантирует ТОЛЬКО указанный key. `UserKnownHostsFile={data_dir}/run/gh-vault-known-hosts` isolates TOFU. Commit использует `git -c user.email=X -c user.name=Y` — НЕ `git config`.

**I-8.5 — Repo allow-list pre-subprocess.** `GitHubSettings.allowed_repos` tuple. ДО любого `git push` / `gh issue create` / `gh repo view` проверяется target repo. Mismatch → exit 6 БЕЗ subprocess spawn.

**I-8.6 — Scheduler seed idempotency.** `schedules.seed_key` UNIQUE INDEX (partial). `find_by_seed_key("vault_auto_commit")` pre-INSERT guard. Daemon restart = no-op. Unique index как last-barrier race guard.

**I-8.7 — `auth-status` subcommand isolation.** НЕ touches vault, НЕ acquires flock, НЕ resolves `vault_remote_url`, НЕ spawns `git`. Чистый `gh auth status --hostname github.com`.

**I-8.8 — vault_dir independence.** `vault-commit-push` работает исключительно с `<settings.vault_dir>` как cwd/`-C` target. **project_root в этом flow не участвует.** vault_dir является standalone git repo, bootstrap'уется на first push. Это zero-overlap guarantee: main `0xone-assistant` repo git status не затрагивается vault push'ами. Q9 decision.

**I-8.9 — Seed tombstone respects owner intent.** `tools/schedule/main.py rm` на seeded row создаёт tombstone в `seed_tombstones`. `_ensure_vault_auto_commit_seed` check'ает tombstone перед INSERT. Daemon не re-seed'ит autonomously. Revive только через явный `revive-seed <key>` CLI. Q10 decision.

## §6 Security surface

**Deploy key compromise.** Attacker получает `~/.ssh/id_vault`. Blast radius:
- Write access ТОЛЬКО к `vaultbot-owner/vault-backup` (deploy key scoped)
- НЕТ доступа к другим repo того account
- НЕТ доступа к main `0xone-assistant` repo (отдельный `gh auth login`)
- Mitigation: rotate через GitHub repo settings → regenerate key → replace file → restart

**DOCTYPE/XXE injection.** Not applicable — CLI не парсит XML. JSON output от `gh` validated `json.loads`.

**TOFU (Trust On First Use).** `StrictHostKeyChecking=accept-new` accept's GitHub host key на first push. MITM ДО first push = persistent compromise. Mitigation: перед первым scheduler run owner вручную делает `ssh -T git@github.com` с известно безопасного окружения. Documented в `docs/ops/github-setup.md`.

**Allow-list as defence-in-depth.** `vault_remote_url` config typo (e.g. `git@github.com:vaultbot-owner/wrong-repo.git`) → `_validate_repo_in_allowlist` rejects before push.

**Model-induced argv injection.** Модель может попытаться `--repo malicious/exfil` в `issue create`. Две линии защиты:
1. `_validate_gh_argv` в Bash hook отрицает неизвестные subcommands и force flags
2. `repo_allowlist.py` в CLI отрицает non-allowlisted repos

**Commit-message injection.** `--message` идёт через `git commit -m` argv (subprocess без shell=True). Template fixed, `{date}` placeholder rendered `strftime` output (ASCII-safe).

**Lock file DoS.** Malicious/crashed процесс держит flock → все scheduler runs exit 9. Mitigation: pidfile flock на Daemon гарантирует single-daemon; race возможна только при manual trigger + scheduled run — exit 9 корректен.

## §7 Open questions для researcher spike

**R-1 — `gh auth status` exit code shape.** Реальное поведение `gh auth status --hostname github.com` когда не залогинен (и когда logged-in-to-other-host). Нужен spike с 3 сценариями: fresh install, logged-in main but not hostname, expired token.

**R-2 — `git -C <path>` vs `subprocess(cwd=path)`.** Поведение при symlinks (Yandex Disk scenario). macOS `realpath` может normalize differently. Рекомендую `cwd=<project_root>` + always-absolute paths. Spike нужен.

**R-3 — `gh` CLI и `GH_TOKEN`/`GITHUB_TOKEN` env leak.** [CLOSED Q2] Owner решил: wipe `GH_TOKEN` + `GITHUB_TOKEN` из env перед каждым `subprocess.run(["gh", ...])`. Реализация в `gh_ops.py` helper. Никакого GH_CONFIG_DIR overload. Spike не нужен для решения, но researcher может empirically подтвердить что `gh auth status` работает после env-wipe (использует OAuth из `~/.config/gh/`).

**R-4 — `fcntl.flock` при OOM kill.** Linux/macOS kernel закрывает fd при termination → lock released. Spike: `kill -9` subprocess holding flock.

**R-5 — flock wait semantics.** [CLOSED Q1] Owner решил: fail-fast `LOCK_NB`. Description.md §8 "30s wait" override'ится. Scheduler thread не блокируется; manual×scheduled overlap → exit 9 `lock_busy` немедленно. Test `test_gh_flock_concurrency.py` проверяет non-blocking behavior.

**R-6 — Migration 0005 ADD COLUMN.** `ALTER TABLE ADD COLUMN seed_key TEXT NULL` без FK → простой ALTER безопасен. Spike не нужен, но comment в SQL.

**R-7 — `git commit --only -- <path>` semantics.** Commits ONLY specified path, игнорируя other staged changes — critical для I-8.1. Edge case: staged changes в других частях working tree — остаются staged после commit (не consumed). Spike: подтверждение.

---

**После devil wave 1 добавлены:**

**R-8 — Deployment topology verification.** [CLOSED Q9] vault_dir = own git repo. Спайк research не нужен, но researcher должен empirically verify `git -C <vault_dir>` flow: init, remote add, initial push. `vault_git_init.py` bootstrap behaviour.

**R-9 — Untracked file detection via `git status --porcelain`.** [CLOSED B2] Plan switched to porcelain. Researcher spike: test shape `--porcelain=v1` vs `v2`, confirm untracked files appear with `?? ` prefix. Edge cases: renamed, deleted, submodules.

**R-10 — `{date}` commit-message TZ rendering.** [CLOSED B4] Rendered в `auto_commit_tz`, не UTC. Researcher подтвердить `datetime.now(ZoneInfo(tz))` works on macOS Darwin 24 (zoneinfo requires Python 3.9+ and tzdata package).

**R-11 — Tombstone semantics.** [CLOSED Q10] New migration 0006, `seed_tombstones` table. Researcher: verify `tombstone_exists` query perf (single row lookup), test reverse flow (tombstone + revive).

**R-12 — `GIT_SSH_COMMAND` shell-quote sanity.** [CLOSED B6] `shlex.quote` на key path + known_hosts path. Researcher: test paths с single-quote inside (`don't`), double-quote, backslash, UTF-8. Git `GIT_SSH_COMMAND` parsing documented in git source (`run_command.c`). **Empirical probe required.**

**R-13 — `dispatch_reply` + ARTEFACT_RE v3 corpus.** [CLOSED Q11] Assemble synthetic corpus: commit_sha strings, `git show --stat` output lines (`data/vault/note.md | 3 +++`), JSON `{"ok": true, "commit_sha": "abc"}`, error strings. Assert `_ARTEFACT_RE.search` returns None for all. Corpus lives in test fixture `tests/fixtures/gh_responses.txt`.

**R-14 — Push verification via local bare repo.** [CLOSED B9] `git init --bare /tmp/vault.git` + `vault_remote_url=file:///tmp/vault.git` в test-only env. Bypass ssh with `GIT_SSH_COMMAND` unset для local file urls. Researcher: verify `git push file://...` works, no ssh path taken.

**R-15 — `gh auth status` из daemon process (B7).** [OPEN] Verify HOME discovery: systemd user service, launchd user agent, sudo context. `gh` reads `~/.config/gh/hosts.yml`, HOME must match owner's interactive shell. Spike: run `gh auth status --hostname github.com` из subprocess с `env={"HOME": "/tmp/other"}` → exit code shape. `_verify_gh_config_accessible` helper spec.

**R-16 — Migration chain race with OLD daemon.** [OPEN] Scenario: two daemon processes, OLD holds WAL connection, NEW applies `_apply_v5` (ALTER TABLE). SQLite behaviour: EXCLUSIVE lock timeout (5s busy_timeout default). OLD daemon crashes → NEW continues. Empirical test: spawn OLD daemon, spawn NEW daemon, NEW acquires pidfile → OLD fails pidfile lock → exit. This should prevent migration race entirely. Verify pidfile acquisition ordering.

## §8 Risk / tradeoff table

| Severity | Risk | Mitigation |
|----------|------|------------|
| High | Deploy key leak → vault exfil + tamper | Write-only scope; separate GitHub account; rotation procedure documented; filesystem perms 0o600 check at startup |
| High | `vault_remote_url` typo pushes в foreign repo | `allowed_repos` tuple enforced pre-subprocess; exit 6 |
| Med | Daemon crash MID-push (commit applied, no push) | Flock released on crash; next run включает prior commit (git-idempotent) |
| Med | GitHub rate-limit на `gh issue list/pr view` | Phase-8 out-of-scope (single-user, low volume); phase-9 throttle |
| Med | SSH host key rotation (GitHub changes ed25519) | Isolated `known_hosts`; rotation requires manual re-accept-new |
| Med | Race vs owner laptop git push → diverged | Fail-fast exit 7; 03:00 cron minimizes race; manual resolution |
| Med | `gh auth status` expires silently | CLI probe pre-flight exit 4; scheduler-turn delivers error owner |
| Low | Force push via model | Bash hook denies `--force*`, `--no-verify`, `--amend` |
| Low | Vault grows >100MB daily | Git markdown diffs efficiently; `<data_dir>/media/` excluded (I-8.1) |
| Low | Scheduler double-seed on restart | UNIQUE INDEX; idempotent helper |
| Low | `gh` CLI отсутствует на host | `Daemon.start` warning; vault push не требует gh |
| Low | Commit-template `{date}` drift | Template fixed в settings |
| Med | Daemon runs as different user (systemd) → `gh auth` HOME mismatch → exit 4 on issue/pr | B7: `_verify_gh_config_accessible` preflight + docs `github-setup.md` warning |
| Med | SSH key located under iCloud/Dropbox (`.icloud` placeholder) → ssh fails | B8: D3 guard extended to check `vault_ssh_key_path.parent` at startup |
| Low | `allowed_repos=()` пуст, auto_commit_enabled=true → all pushes exit 6 | B10: Daemon.start warning + docs instructs `GH_ALLOWED_REPOS` setting |

## §9 Критерии готовности

**Functional acceptance:**
- [ ] `python tools/gh/main.py vault-commit-push --message "test"` с dirty vault → exit 0, JSON `{"ok": true, "commit_sha": "...", "files_changed": N}`, remote имеет новый commit
- [ ] Same с clean vault → exit 5, silent в scheduler-path
- [ ] Non-ff divergence → exit 7 `diverged`, НЕТ force push, НЕТ rebase
- [ ] Missing `GH_VAULT_SSH_KEY` → exit 10 `ssh_key_error`
- [ ] `auth-status` с unauthed gh → exit 4 `gh_not_authed`
- [ ] `issue create --repo evil/repo` (not в allowed) → exit 6, БЕЗ subprocess
- [ ] `issue list --repo allowed/repo --state open` → exit 0, JSON array

**Scheduler integration:**
- [ ] `Daemon.start()` с valid config → 1 row в `schedules` с `seed_key='vault_auto_commit'`, cron `0 3 * * *`
- [ ] Second start → та же 1 row (idempotent)
- [ ] `GH_AUTO_COMMIT_ENABLED=false` → 0 rows seeded
- [ ] Manually trigger scheduled row → handler → model invokes `vault-commit-push` → commit pushed

**Bash hook guardrails:**
- [ ] `_validate_python_invocation(["python", "tools/gh/main.py", "vault-commit-push"], ...)` → None
- [ ] Same с `"pr", "create"` → error
- [ ] Same с `--force` → error
- [ ] Phase-3 `_validate_gh_invocation(["gh", "pr", "create"])` → error (not regressed)

**Path isolation (critical):**
- [ ] Seed `<data_dir>/media/outbox/fake.png` + `<data_dir>/assistant.db` + `<data_dir>/vault/note.md`; run CLI; `git show --stat HEAD` содержит ТОЛЬКО `data/vault/note.md`
- [ ] Commit message matches `vault sync YYYY-MM-DD`
- [ ] Commit author = `commit_author_email` config value

**Phase-7 invariants preserved:**
- [ ] `make_subagent_hooks` signature unchanged
- [ ] `_DedupLedger` TTL=300, cap=256
- [ ] `dispatch_reply` ВЫЗЫВАЕТСЯ на scheduler-turn output (phase-7 path). Критично: `_ARTEFACT_RE v3` false-positive rate = 0 на corpus из 30+ model responses о gh CLI (commit_sha, `git show --stat`, JSON passthrough, error messages). Q11 fix + regression test `test_gh_dispatch_reply_no_artefact_match.py`.
- [ ] `make_pretool_hooks(project_root, data_dir=None)` backward-compat

**Wave 0 pre-flight:**
- [ ] `test_wrong_shape_list_payload_recovers` passes strictly (xfail removed)
- [ ] `test_best_effort_reader_binary_input_recovers` passes strictly
- [ ] xfail count 5 → 3

**CI gates:**
- [ ] `uv sync` green
- [ ] `just lint` green (ruff + format + mypy strict)
- [ ] `uv run pytest -q` — 1200+ passed, 3 xfailed, 0 failed
- [ ] `tools/gh/` mypy clean

**Documentation:**
- [ ] `docs/ops/github-setup.md` covers account/key/env/rotation/cron
- [ ] `README.md` / `CLAUDE.md` mention phase-8 shipped + skill listed

## §10 Q&A decisions log (before devil wave 1)

Зафиксированы 8 ответов owner'а на развилки, не покрытые description.md:

| # | Question | Decision | Impact on plan |
|---|----------|----------|----------------|
| Q1 | Flock behavior при concurrent vault-commit-push | Fail-fast `LOCK_NB` exit 9 | §4 C4 step 6; R-5 closed; test `test_gh_flock_concurrency.py` |
| Q2 | `GH_TOKEN`/`GITHUB_TOKEN` env isolation | Wipe env перед subprocess | §4 C3 (new paragraph); R-3 closed; impl в `gh_ops.py` |
| Q3 | `GitHubSettings` placement | Inline в `config.py` | §3 file table (confirmed); не создаём `settings/` package |
| Q4 | Empty `vault_remote_url` с `auto_commit_enabled=True` | Auto-disable + warning log | §4 C1 pydantic `model_validator` |
| Q5 | `vault_remote_name` default | `vault-backup` | §4 C1 field default; избегаем конфликта с existing `origin` |
| Q6 | `auto_commit_tz` default | `Europe/Moscow` | §4 C1 field default; match owner TZ |
| Q7 | `commit_author_email` default | `vaultbot@localhost` | §4 C1 field default (confirmed); synthetic email, no GitHub account match |
| Q8 | Cron update behavior на env drift | Seed once, ignore after | §4 C5 `_ensure_vault_auto_commit_seed` (confirmed); `find_by_seed_key` skip без UPDATE; docs §C7 mention `rm+rerun` procedure |
| Q9 | Deployment topology | vault_dir = own git repo | §3 (new row `vault_git_init.py`); §4 C4 flow rewritten; new I-8.8 |
| Q10 | Tombstone для seed rm | New `seed_tombstones` table | §3 (new migration 0006); §4 C5 `tombstone_exists` check; new I-8.9 |
| Q11 | dispatch_reply invariant | Fix wording + ARTEFACT_RE regression test | §9 invariant bullet; new test file `test_gh_dispatch_reply_no_artefact_match.py`; R-13 |
| Q12 | Vault encryption | Keep out-of-scope | §4 C7 docs warning #9; Risk table unchanged |

Все Recommended приняты (Q5-Q8 — после дополнительных пояснений). Два open question'а (R-3, R-5) закрыты без spike'а. R-1/R-2/R-4/R-6/R-7 остаются на researcher spike.

### Devil wave 1 tech fixes (auto-applied без owner Q&A):

- **B2** untracked file detection: `git diff --quiet` → `git status --porcelain` (§4 C4 step 7)
- **B4** commit message `{date}` TZ: UTC → `auto_commit_tz` (§4 C4 step 11)
- **B6** `vault_ssh_key_path` validator + `shlex.quote` в `GIT_SSH_COMMAND` (§4 C1 + C4 step 13)
- **B7** `_verify_gh_config_accessible` preflight в Daemon.start (§4 C6)
- **B8** D3 cloud-sync guard extended на `vault_ssh_key_path.parent` (§4 C6)
- **B9** Test strategy: local bare repo (`file:///tmp/vault.git`) для push verification (§4 C4 tests, R-14)
- **B10** `allowed_repos=()` preflight warning в Daemon.start (§4 C6)

- **SF2** `vault_remote_url` regex `[\w.-]+` → `[A-Za-z0-9._-]+` + reject `..` (§4 C1)
- **SF4** migration ordering после pidfile flock (§4 C6)
- **SF5** `_validate_gh_argv` cap `--limit 100` (§4 C6)
