# Phase 8 — Summary

Phase 8 завершена: 13 коммитов (`b174d62..9c2f317`, ~14 k LOC insertions / 49 deletions across 86 файлов) поверх phase-7 HEAD `48d3ccb`. Тесты: **1522 passed, 4 skipped, 3 xfailed, 1 xpassed**. Lint + mypy strict зелёные.

## 1. Shipped scope

**Ось 1 — GitHub CLI wrapper.** `tools/gh/main.py` (~900 LOC stdlib + subprocess) с subcommands `auth-status`, `issue create/list/view`, `pr list/view`, `repo view`, `vault-commit-push`. Модель вызывает через `Bash` hook с argv-whitelist (phase-7 pattern). Allow-list `GitHubSettings.allowed_repos` отклоняет non-whitelisted `owner/repo` ДО spawn'а gh subprocess. Env wipe `GH_TOKEN`/`GITHUB_TOKEN` перед каждым `gh` вызовом.

**Ось 2 — Daily vault auto-commit.** Default-seeded scheduled job в 3:00 Europe/Moscow. Scheduler → handler → модель → `vault-commit-push` → `git -C <vault_dir>` add/commit/push через dedicated SSH deploy key, изолированный через `GIT_SSH_COMMAND` + `IdentitiesOnly=yes` + `UserKnownHostsFile=<data_dir>/run/gh-vault-known-hosts`. Первый push bootstrap'ит empty vault_dir (`git init`, `git checkout -b main`, remote add, initial empty commit).

**Infrastructure changes:**
- `GitHubSettings(BaseSettings, env_prefix="GH_")` inline в `src/assistant/config.py` — 10 полей с pydantic validators.
- Migrations `0005_schedule_seed_key.sql` + `0006_seed_tombstones.sql` (`SCHEMA_VERSION` 4 → 6).
- `src/assistant/scheduler/seed.py` (`_ensure_vault_auto_commit_seed`) + store accessors (`find_by_seed_key`, `tombstone_exists`, `insert_tombstone`, `delete_tombstone`).
- `Daemon.start` preflight probes: `_verify_gh_config_accessible_for_daemon`, `_check_path_not_in_cloud_sync`, `_probe_gh_version`.
- `src/assistant/bridge/hooks.py` — `_validate_gh_argv` + dispatch branch.
- `tools/schedule/main.py` — `cmd_rm` extended (insert tombstone если row имеет seed_key), новый `cmd_revive_seed`.
- `skills/gh/SKILL.md` — H-13 structural, exit-code matrix.
- `docs/ops/github-setup.md` — 12 секций (344 lines).

## 2. Invariants I-8.1 — I-8.9

**I-8.1 Path-pinned `git add`** — vault-commit-push работает ИСКЛЮЧИТЕЛЬНО внутри `<vault_dir>` как `-C` target; `data/media/`, `data/run/`, `data/assistant.db` физически вне vault_dir (Q9 topology). Regression test `tests/test_gh_vault_commit_path_isolation.py` использует real git, не mocks.

**I-8.2 Single-flight flock** — `fcntl.flock(LOCK_EX | LOCK_NB)` на `<data_dir>/run/gh-vault-commit.lock` сразу после argv parse. `BlockingIOError` → exit 9 БЕЗ retry (Q1). Kernel освобождает flock при process termination (verified в `test_gh_flock_released_on_parent_sigkill`). `--dry-run` bypass'ит flock (SF-B3). Реализация: `tools/gh/_lib/lock.py::flock_exclusive_nb`.

**I-8.3 Fail-fast on divergence** — non-fast-forward push → exit 7. NO `git pull`, NO `git rebase`, NO `--force*`. `DIVERGED_RE` pattern matches `non-fast-forward` / `(fetch first)` / `! [rejected]` в stderr (hardened в fix-pack). Реализация: `tools/gh/_lib/git_ops.py::push`.

**I-8.4 SSH key isolation** — deploy key через `GIT_SSH_COMMAND` env в child subprocess: `ssh -F /dev/null -i <key> -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=<data_dir>/run/gh-vault-known-hosts`. НЕ `~/.ssh/config` (fix-pack T1.4), НЕ `ssh-agent`. Commit uses `git -c user.email=X -c user.name=Y` inline. `shlex.quote` на key path (B6). Реализация: `tools/gh/_lib/git_ops.py::_base_env` + `push`.

**I-8.5 Repo allow-list pre-subprocess** — `GitHubSettings.allowed_repos: tuple[str, ...]` parsed from `GH_ALLOWED_REPOS` CSV env. ДО любого `git push` / `gh issue create` / `gh repo view` проверяется target `owner/repo`. Mismatch → exit 6 БЕЗ subprocess spawn. Реализация: `tools/gh/_lib/repo_allowlist.py`.

**I-8.6 Scheduler seed idempotency** — `schedules.seed_key` partial UNIQUE INDEX (`WHERE seed_key IS NOT NULL`, SF-D3). Pre-INSERT guard: `find_by_seed_key("vault_auto_commit")`. Pidfile flock на Daemon гарантирует single seed invocation. Реализация: `src/assistant/scheduler/seed.py::_ensure_vault_auto_commit_seed`.

**I-8.7 `auth-status` subcommand isolation** — НЕ touches vault, НЕ acquires flock, НЕ resolves `vault_remote_url`, НЕ spawns `git`. Чистый `gh auth status --hostname github.com` с env-wiped subprocess. Unauthed → exit 4. Реализация: `tools/gh/main.py::_cmd_auth_status`.

**I-8.8 vault_dir independence** — `vault-commit-push` работает исключительно с `<settings.vault_dir>` как cwd/`-C` target. **project_root в этом flow не участвует.** vault_dir — standalone git repo (Q9), bootstrap'уется на first push. Zero-overlap guarantee. Реализация: `tools/gh/_lib/vault_git_init.py`.

**I-8.9 Seed tombstone respects owner intent** — `tools/schedule/main.py rm` на seeded row (`seed_key IS NOT NULL`) в одной транзакции: `INSERT OR REPLACE INTO seed_tombstones` + DELETE schedules row. `_ensure_vault_auto_commit_seed` check'ает tombstone ПЕРЕД INSERT. Revive только через явный `python tools/schedule/main.py revive-seed vault_auto_commit`. Q10 decision.

## 3. Q&A decisions log

| # | Question | Decision |
|---|----------|----------|
| Q1 | Flock behavior при concurrent push | Fail-fast `LOCK_NB` exit 9 |
| Q2 | `GH_TOKEN`/`GITHUB_TOKEN` env isolation | Wipe env перед subprocess |
| Q3 | `GitHubSettings` placement | Inline в `config.py` |
| Q4 | Empty `vault_remote_url` + `auto_commit_enabled=True` | Auto-disable + warning |
| Q5 | `vault_remote_name` default | `vault-backup` |
| Q6 | `auto_commit_tz` default | `Europe/Moscow` |
| Q7 | `commit_author_email` default | `vaultbot@localhost` |
| Q8 | Cron update behavior на env drift | Seed once, ignore after |
| Q9 | Deployment topology | vault_dir = standalone git repo |
| Q10 | Tombstone для seed rm | New `seed_tombstones` table |
| Q11 | `_ARTEFACT_RE` v3 regression for gh output | Regression corpus 83 lines |
| Q12 | Vault encryption | Keep out-of-scope |

Open spikes R-3 и R-5 закрыты без probe'а на основании Q2/Q1.

## 4. Blockers closed

**Devil wave 1 (10 blockers B1-B10):** B1 deployment topology (Q9) → standalone vault_dir + bootstrap helper; B2 `git diff --quiet` upuskает untracked → switched to `git status --porcelain`; B3 tombstone semantics (Q10) → migration 0006; B4 `{date}` TZ drift → rendered в `auto_commit_tz`; B5 dispatch_reply не acknowledged (Q11) → regression corpus test; B6 SSH path shell-injection → pydantic validator + `shlex.quote`; B7 `gh` HOME mismatch → `_verify_gh_config_accessible_for_daemon` preflight; B8 SSH key под iCloud → D3 cloud-sync guard extended; B9 push verify flaky → `file:///tmp/vault.git` local bare repo (R-14); B10 `allowed_repos=()` empty → preflight warning.

**Devil wave 2 (6 blockers + 20 should-fix):** B-A1 tuple NoDecode для CSV env → `Annotated[tuple[...], NoDecode]`; B-A2 `get_settings()` TG dependency → CLI standalone pattern (direct `GitHubSettings()`); B-A3 vault_dir mkdir missing → `parents=True, exist_ok=True`; B-B2 local commit lingers after divergence → `reset --soft HEAD~1` on divergence + unpushed detection; B-D1 sync schedule contradiction → preserved phase-5 soft-delete pattern; B-D6 E2E flaky → downgraded на manual smoke.

**Final devil (parallel reviewers):** T6.1 blocker — `unpushed_commit_count` использовал `@{u}..HEAD` но `push()` никогда не passes `-u` → branch unreachable в production. Fixed в fix-pack `9c2f317` — switched to `<remote>/<branch>..HEAD` refspec (auto-populated by git push).

**Fix-pack 10 should-fix:** T1.4 `-F /dev/null` в ssh_cmd; T2.4 validators для vault_remote_name + vault_branch; T4.1 `core.hooksPath /dev/null` в bootstrap; T4.3 extended `.gitignore` template + merge semantics; T5.1 pin HOME в build_gh_env; S-1 SKILL.md repo view `--repo` flag; S-2 docs §8 rewrite cron update procedure; S-3 TimeoutExpired в `_run_git`; S-6 validators для commit_message_template + commit_author_email; S-9 env-scrub consistency test.

## 5. Test coverage

Старт (phase-7 HEAD): 1240 tests. Финал (phase-8 HEAD `9c2f317`): **1522 passed, 4 skipped, 3 xfailed, 1 xpassed**. Delta: **+282 tests**.

Breakdown: C0 (2 xfail→passed, count 5→3); C1 (~20 tests ssh/tz/CSV/path); C5a (~15 seed_idempotency/disabled/migration_v5_v6); C2 (~10 SKILL/auth-status); C5b (~10 tombstone/revive); C3 (~6 issue_create/whitelist/pr_view); **C4 (~107, largest — 12 new test files)**; C6 (~55 validate_argv 26 cases + bash_hook + daemon probes); fix-pack (+7 shared env/DIVERGED_RE/gh version/known_hosts/bootstrap + remote/branch validators + timeout + hooks_path_neutral).

3 xfailed preserved: S-2 regex adjacency residuals (phase-7 inherited). 1 xpassed pre-existing: `test_dedup_ttl_real_clock` (timing-variant, kept as smoke-detector).

## 6. Known limitations / deferred

- **E2E test downgraded (B-D6)** — manual smoke через `docs/ops/github-setup.md` §12. Unit tests coverage каждое звено отдельно.
- **Phase-8 should-fix не закрыты** (phase 9+): S-4 `.gitignore` already covered; S-5 seed helper TOCTOU — defer; S-7 overblock docstring; S-8 public `rev_parse_head` helper; S-10 `_flatten_gh_json` extension policy; S-11 Protocol для `_do_push_cycle`; S-12 docs split.
- **Encryption out-of-scope (Q12)** — git-crypt в phase 10+.
- **PR creation, issue close/comment/edit** → phase 9 keyboard-confirm.
- **Auto-rebase при conflict** принципиально не делаем.

## 7. Phase 7 integration preserved

- **I-7.4** `make_subagent_hooks(*, store, adapter, settings, pending_updates, dedup_ledger)` signature unchanged.
- **I-7.5** `make_pretool_hooks(project_root, data_dir=None)` backward-compat; `_validate_gh_argv` dispatch внутри `_validate_python_invocation` без изменения factory signature.
- **I-7.1** `_DedupLedger` TTL=300/cap=256 unchanged.
- **I-7.6** `_ARTEFACT_RE` v3 unchanged; Q11 corpus 83 lines, 0 false-positives в `test_gh_dispatch_reply_no_artefact_match.py`.
- **dispatch_reply** scheduler-turn output из vault-commit-push идёт через тот же dispatch_reply path; JSON `{"ok": true, "commit_sha": "..."}` не матчит artefact regex.

Phase-7 wave-12 fix-pack полностью preserved. Retention sweeper трогает только `<data_dir>/media/`, vault_dir остаётся в зоне phase-8.

## 8. Deployment notes

- **First-time operator** читает `docs/ops/github-setup.md` (344 lines, 12 sections): dedicated `vaultbot-owner` GitHub account → private repo → deploy key generation → `.env` template → main `gh auth login` на отдельном host-аккаунте → TOFU через isolated `UserKnownHostsFile`.
- **Default XDG data layout** (`<data_dir>/vault/`) работает out-of-the-box — vault_dir standalone git repo (Q9), bootstrap'ится при первом `vault-commit-push`.
- **Migration v4→v5→v6** на `Daemon.start` idempotent, applied после pidfile flock (SF4). Pre-existing schedules rows получают `seed_key = NULL` (partial UNIQUE INDEX их excludes).
- **Disable auto-commit**: `python tools/schedule/main.py rm <id>` → tombstone → daemon не re-seed'ит. Re-enable: `revive-seed vault_auto_commit` + restart.
- **Key rotation**: revoke old key в GitHub → `ssh-keygen` new → replace `~/.ssh/id_vault*` → restart daemon.
- **SSH host-key pinning**: `StrictHostKeyChecking=accept-new` + isolated known_hosts. Manual `ssh -T git@github.com` из known-safe окружения перед first scheduler run рекомендуется (docs §4).

## 9. Pipeline discipline notes (methodology feedback)

- **1 coder per commit в parallel waves worked well** — Wave 1 (C1 ∥ C5a) и Wave 2 (C2 ∥ C5b) без merge conflicts. File disjointness verified в wave-plan.md заранее.
- **2 fix-ups — expected cascade** — `417f2f4` (phase-4 migration test assertion cascade от SCHEMA_VERSION 4→6) и `2cd73d5` (mock C6 preflights cascade от Daemon.start). Orchestrator должен закладывать buffer на cascade.
- **Parallel reviewers gave overlapping findings — good signal.** code-reviewer + devil-final: 1 blocker + 17 should-fix. 11/18 items batched в один fix-pack commit (`9c2f317`) — single bisectable point.
- **Pytest buffer-hang (6+ occurrences).** Persistent session issue — subprocess hang при `uv run pytest ... | tail -N` и в post-teardown. Workaround: `> /tmp/log 2>&1` + `kill` stuck processes вручную. Phase 9 backlog: `pytest-timeout` dep.
- **Devil wave ordering critical** — devil wave 1 ДО researcher spike (auto-fixes) → saved 10 Q&A rounds; devil wave 2 ПОСЛЕ researcher → explicit blocker list уменьшился в 2 раза; final devil (parallel с code-reviewer) ПОСЛЕ code done → 1 blocker dead-code T6.1 caught.
- **Q&A via AskUserQuestion (Q1-Q12).** 12 development forks закрыты в один session. Workflow «researcher predicts fork + devil underscores + owner picks via AskUserQuestion» efficient на phase 8.
