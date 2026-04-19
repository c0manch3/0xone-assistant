# Phase 8 — GitHub CLI + ежедневный vault auto-commit

## Цель

Подключить GitHub-операции (issues / PR-introspection / `gh api`) и organisational
git-state (commit/push) к боту через тонкий CLI `tools/gh/main.py` плюс
сидируемый по дефолту scheduled-job, который ежедневно коммитит и пушит
содержимое `<data_dir>/vault/` в сконфигурированный owner-репозиторий
(README §Порядок фаз line 44 + §Ключевые архитектурные решения line 13).
Phase 8 расширяет phase-3 read-only `gh api` allowlist на дополнительные
read-only субкоманды (`gh issue`, `gh pr`, `gh repo`) **и** добавляет
узко-сфокусированный write-канал на vault-репозиторий — без расширения
доступа к произвольным remote-репозиториям. Тяжёлые write-операции
(creation PR, force-push, любые `gh api -X POST`) остаются недоступны
модели и переносятся на phase 9 keyboard-confirm flow.

## Вход

- **Phase 7 shipped:** `dispatch_reply` + `_DedupLedger` (артефакты из
  outbox автоматически становятся Telegram-вложениями), Bash hook factory
  с `make_pretool_hooks(project_root, data_dir=None)` (I-7.5),
  `make_subagent_hooks(*, store, adapter, settings, pending_updates,
  dedup_ledger)` (I-7.4), shared `validate_existing_input_path` /
  `validate_future_output_path` в `src/assistant/media/path_guards.py`,
  retention sweeper для `<data_dir>/media/` (НЕ vault).
- **Phase 6 shipped:** SDK-native subagent infrastructure + worker
  AgentDefinition + `tools/task/main.py spawn` для async delegation.
- **Phase 5 shipped:** `SchedulerLoop`, `SchedulerDispatcher`, DB schema v3
  с `schedules` / `triggers` таблицами, scheduler-injected turn с
  `IncomingMessage(origin="scheduler")`, per-chat lock, scheduler
  system-note. Default-seed pattern отложен из phase 5 на phase 8.
- **Phase 4 shipped:** vault на `<data_dir>/vault/`, memory CLI пишет
  markdown-only — binary артефакты в vault не попадают.
- **Phase 3 shipped:** read-only `gh api` allowlist с whitelist endpoints
  `/repos/anthropics/skills/contents/...`; `gh auth status` allow;
  `gh pr create` deny.
- **External:** `gh` CLI установлен на хосте, `gh auth status` успешный;
  SSH-ключ fallback для git push, если `gh auth` не даёт credential helper.

## Выход — пользовательские сценарии (E2E)

1. **"открой issue в репо про баг X"** → модель → `tools/gh/main.py
   issue create --repo <owner>/0xone-assistant --title 'X' --body '<...>'` →
   exit 0, JSON `{"url": "...", "number": 42}`.
2. **"какие открытые issues?"** → `gh issue list --state open --json
   number,title,labels`.
3. **"посмотри PR #15"** → `gh pr view 15 --repo <...> --json
   title,body,state,mergeable`.
4. **Daily vault auto-commit** (scheduler-originated). В 03:00 локального TZ
   scheduler триггерит → handler даёт модели turn с
   `origin="scheduler", text="ежедневный бэкап vault: сделай git add
   data/vault, коммит и git push"` → модель вызывает
   `Bash("python tools/gh/main.py vault-commit-push --message 'vault sync
   2026-04-19'")` → `git -C <project_root> add -- <vault_relpath>` →
   commit → push → exit 0 → модель проактивно отвечает owner'у
   "vault сохранён, N файлов, sha=abc1234". Empty diff → exit 5
   "no_changes" → silent.
5. **Manual "запушь vault сейчас"** → тот же CLI, user-originated turn.
6. **Conflict при push** (race с лаптопом) → CLI exits 7 + JSON
   `{"ok": false, "error": "remote has diverged"}` → fail-fast, owner
   разрешает на лаптопе (авто-rebase не делаем).

## Задачи (ordered)

1. **Phase-7 fix-pack pre-flight (Wave 0).** Закрыть X-1 (isinstance check
   в genimage `_check_and_increment_quota`) + X-2 (UnicodeDecodeError в
   `_read_quota_best_effort`) + идентифицировать xpassed. Один коммит до
   старта основной работы.
2. **`GitHubSettings(BaseSettings)` с `env_prefix="GH_"`**: `vault_remote_url`,
   `vault_remote_name` (default `"origin"`), `vault_branch` (default `"main"`),
   `auto_commit_enabled` (bool, default `True`), `auto_commit_cron` (default
   `"0 3 * * *"`), `auto_commit_tz` (default `"UTC"`),
   `commit_message_template` (default `"vault sync {date}"`),
   `commit_author_email`, `allowed_repos` (tuple of `owner/repo` slugs).
   Nested под `Settings.github`.
3. **`tools/gh/main.py`** (~250 LOC stdlib). Subcommands:
   - `auth-status`, `issue create/list/view`, `pr list/view`, `repo view`.
   - `vault-commit-push --message MSG [--dry-run]`:
     a. `git -C <project_root> rev-parse --is-inside-work-tree`.
     b. `git -C <...> diff --quiet -- <vault_relpath>` → exit 0 = nop → exit 5.
     c. `git -C <...> add -- <vault_relpath>` (path-pinned, НЕ `-A`).
     d. `git -C <...> commit -m <msg> --only -- <vault_relpath>` с
        GIT_AUTHOR из config.
     e. `git -C <...> push <remote> <branch>` без `--force`. На non-ff → exit 7.
     f. JSON `{"ok": true, "commit_sha": "...", "files_changed": N}`.
   - **Out of scope:** PR creation, `gh api -X POST/...`, force-push, push
     на не-vault remote.
4. **`skills/gh/SKILL.md`** с `allowed-tools: [Bash]`, примеры диалогов,
   exit-code matrix (0 ok, 2 argv, 3 validation, 4 gh not authed, 5
   no_changes, 6 repo_not_allowed, 7 diverged, 8 push_failed, 9 lock_busy).
5. **Bash allowlist extension в `bridge/hooks.py`.** Добавить
   `_validate_gh_argv` для `tools/gh/main.py` подкоманд (phase-7 pattern).
   Существующий `_validate_gh_invocation` для прямого `gh api` НЕ
   расширяется.
6. **Default-seed scheduled job** в `Daemon.start()`. После
   `ensure_media_dirs` → `_ensure_vault_auto_commit_seed(store, settings.github)`:
   check existing schedule with seed_key=`"vault_auto_commit"` (migration
   `0004_schedule_seed_key.sql`); отсутствует → INSERT с cron из config.
   Idempotent (unique constraint). `auto_commit_enabled=False` → skip.
7. **Vault-remote bootstrap helper.** Если `<project_root>/.git` отсутствует
   или нет remote `<vault_remote_name>` → `Daemon.start` логгирует
   `vault_remote_not_configured`; owner получает one-time system-note при
   первом gh-запросе. Auto-commit seed пропускается до настройки.
8. **Concurrency / locking.** `vault-commit-push` берёт `fcntl.flock` на
   `<data_dir>/run/gh-vault-commit.lock`; параллельный запуск ждёт 30 s
   → exit 9 "lock_busy".
9. **Тесты** (~600 LOC, 12 файлов):
   - `test_gh_validate_argv.py` (argv allow/deny matrix)
   - `test_gh_vault_commit_push_happy.py` (mock subprocess)
   - `test_gh_vault_commit_push_no_changes.py` (exit 5)
   - `test_gh_vault_commit_push_diverged.py` (exit 7)
   - `test_gh_vault_commit_path_isolation.py` (phase-7 critical: data/media
     НЕ попадает в commit)
   - `test_gh_issue_create_happy.py` (mock subprocess + whitelist)
   - `test_gh_repo_whitelist.py` (non-allowed repo → exit 6)
   - `test_gh_seed_idempotency.py` (double-start не дублирует row)
   - `test_gh_seed_disabled.py` (auto_commit_enabled=False → skip)
   - `test_gh_flock_concurrency.py` (2 parallel → second waits or exit 9)
   - `test_gh_bash_hook_integration.py` (`_validate_python_invocation`
     dispatcher routing)
   - `test_gh_skill_md_assertion.py` (H-13 паттерн: SKILL.md valid)
10. **Документация `docs/ops/github-setup.md`.** `gh auth login` setup,
    private vault repo creation, env vars, cron override через
    `tools/schedule/main.py`, SSH key fallback.

## Критерии готовности

- Bot может создавать / читать issues и читать PR через `tools/gh/main.py`.
- Default seed после `Daemon.start()` виден через `tools/schedule/main.py list`.
- В 03:00 (или через ручной trigger) scheduler → handler → модель →
  `vault-commit-push` → коммит на remote с vault-файлами only.
- `git log` на remote показывает коммиты БЕЗ `data/media/`, `data/assistant.db`,
  `data/run/`.
- Empty diff → exit 5, silent (no Telegram message).
- Conflict → exit 7 → понятный owner message, auto-rebase НЕ делается.
- Bash hook rejects:
  - `--force`, `--no-verify` → deny.
  - `--repo otheruser/private` (не в allowed_repos) → CLI exit 6.
  - `gh pr create` через прямой gh → phase-3 hook deny.
- `auto_commit_enabled=False` → seed не создаётся.
- `gh auth status` non-zero → vault-commit-push exit 4.
- **Phase-7 invariants preserved:** `make_subagent_hooks` signature (I-7.4),
  `_DedupLedger` TTL=300/cap=256 (I-7.1), `MediaSettings` retention (I-7.2),
  `dispatch_reply` unchanged, `_ARTEFACT_RE` v3 unchanged (gh output не
  содержит outbox paths).
- X-1/X-2 закрыты в Wave 0 (xfail count 5 → 3).

## Зависимости

- **Phase 7 (КРИТИЧНО, инварианты):** hook factory signature, dedup ledger
  lifecycle, media/vault separation. Tests `test_gh_vault_commit_path_isolation`
  явно гарантирует invariant сохранения.
- **Phase 6:** worker AgentDefinition — optional для очень больших vault
  commits (десятки MB).
- **Phase 5:** scheduler + DB schema v3 + scheduler-injected turn + UDS IPC —
  основа для default-seed. Migration `0004_schedule_seed_key.sql` (optional
  idempotency column).
- **Phase 4:** vault на `<data_dir>/vault/` — git add pinned.
- **Phase 3:** existing `_validate_gh_invocation` (read-only `gh api`)
  остаётся; новый `_validate_gh_argv` для `tools/gh/main.py` — phase-7 pattern.
- **External:** `gh auth login` выполнен owner'ом; `<project_root>` — git repo
  с configured remote.

## Явно НЕ в phase 8

- PR creation через `gh pr create` (phase 9 keyboard-confirm).
- Issue close / comment / edit (phase 9).
- `gh api -X POST/PUT/PATCH/DELETE`.
- `git push --force`, `--force-with-lease`, `--no-verify`.
- Auto-rebase / auto-merge при conflict.
- Multiple vault remotes / multi-vault.
- Encrypted vault (git-crypt).
- Inline-keyboard для approve PR (phase 9, требует callback_query handler).
- Webhook receiving (GitHub → bot).
- GitHub Actions integration.
- Per-skill `allowed-tools` enforcement (phase 9).
- `HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap (phase 9).

## Риск

**Средний.**

| Severity | Risk | Mitigation |
|---|---|---|
| 🟡 | Случайный push с secret'ами | `git add` pinned на `<vault_dir>/` (markdown only через memory CLI); bootstrap добавляет `.gitignore` в vault (`*.env`, `*.key`, `secrets/`). |
| 🟡 | gh credentials истекают → silent fail | CLI exit 4; scheduler-turn доставляет ошибку owner'у; health-check (phase 9 optional). |
| 🟡 | Race с лаптопом owner'а | CLI exit 7 fail-fast; owner разрешает `git pull --rebase` на лаптопе; default cron 03:00 минимизирует race. |
| 🟡 | Issue create в неправильный репо (typo) | `GitHubSettings.allowed_repos` whitelist; exit 6 if not listed. |
| 🟢 | Force push через model | CLI argv валидация отклоняет `--force*`; `_validate_python_invocation` argv-allowlist (phase-7 pattern). |
| 🟢 | Vault grow 100 MB+ ежедневно | git хорошо diff'ит markdown; `data/media/` явно ИСКЛЮЧЕН. |
| 🟢 | Scheduler spam при cron misconfig | Single seed с unique key — дубликаты не создаются. |
| 🟢 | gh CLI отсутствует | `Daemon.start` warning + отключает seed. |

> **Phase-7 integration note (vault vs media).** Phase 7 кладёт inbox/outbox
> в `<data_dir>/media/` с retention sweep (14d/7d/2GB LRU). Vault —
> отдельная иерархия `<data_dir>/vault/`, sweeper не трогает (I-7.2).
> Phase-8 vault auto-commit `git add`'ит **только** `<vault_dir>` путь;
> `data/media/` и `data/run/` явно НЕ попадают — критично для приватности
> (фото owner'а остаются локальными) и размера repo. Photo-attachments не
> попадают в vault, если модель не сохранила их через memory skill явно.
> Тест `test_gh_vault_commit_path_isolation.py` гарантирует что outbox-файл
> не попал в diff vault-коммита.
