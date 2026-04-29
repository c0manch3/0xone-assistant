# Phase 8 — Wave plan

**Plan version:** detailed-plan.md r2 + implementation.md v2
**Generator:** parallel-split agent, 2026-04-19
**Max concurrent coders per wave (Q locked):** 4 (phase-7 pilot convention; phase-8 uses at most 2)
**Orchestrator model:** `isolation=worktree`, merge = sequential rebase per wave
**Worktree parent dir:** `/tmp/0xone-phase8/` (created before Wave 0; preserved until phase-8 merge complete)
**Total commits:** 8 (C0–C7) with C5 split into C5a + C5b → 9 coder invocations over 7 waves
**Waves:** 4 sequential + 3 parallel; max parallelism = 2 (Wave 1, Wave 2, Wave 6)

**Pre-flight (before Wave 0):**

```bash
mkdir -p /tmp/0xone-phase8
cd /Users/agent2/Documents/0xone-assistant
git tag phase8-pre-start
```

**Per-wave sequence (orchestrator):**

1. Tag `phase8-pre-wave-N` on `main`.
2. For each commit in the wave: `git worktree add <worktree_path> -b <branch>`; spawn coder with manifest prompt referencing `implementation.md v2`.
3. Await all coders; run the wave's per-commit test command in each worktree.
4. Sequential rebase-merge into main (each merge followed by `uv run pytest -q && just lint && uv run mypy src --strict`).
5. `git worktree remove <worktree_path>` per successful merge.
6. If any step red: follow-up coder in same worktree (max 3 retries) → else sequential fallback (§7 parallel-split-agent.md).

---

## Dependency graph (commit level)

Per implementation.md §Commit order summary + §Dependencies lines:

- **C0** → none (pre-flight hotfix, standalone)
- **C1** → C0 (uses post-v0 baseline; phase-7 HEAD stable)
- **C2** → C1 (needs `GitHubSettings` for sub-model import; handlers instantiate directly)
- **C3** → C1, C2 (extends `tools/gh/main.py` from C2; imports `GitHubSettings`)
- **C4** → C1, C2, C3 (extends `tools/gh/main.py` + adds `_lib/{git_ops,lock,vault_git_init}.py`)
- **C5a** → C1 (migrations + seed module + store extension; needs `GitHubSettings` symbol to gate seed)
- **C5b** → C5a (tools/schedule CLI extension; references `seed_tombstones` table created by C5a)
- **C6** → C4, C5a, C5b (Daemon integration needs seed helper + validates gh argv)
- **C7** → C6 (docs + README reference full functionality)

**Splits justification:**

- **C5 → C5a + C5b:** implementation.md §C5 touches TWO disjoint file trees: (a) `src/assistant/state/migrations/*.sql` + `src/assistant/state/db.py` + `src/assistant/scheduler/{store,seed}.py` (migration + store extension + seed helper), and (b) `tools/schedule/main.py` (cmd_rm rewrite + cmd_revive_seed addition). These sets never intersect, so C5b can start as soon as C5a is merged — AND C5b can safely run in parallel with C2 (disjoint from `tools/gh/` scaffold). We schedule C5a in Wave 1 (alongside C1) and C5b in Wave 2 (alongside C2) to maximise early throughput.

**Non-splits:**

- **C4 stays whole:** ~500 LOC source + 400 LOC tests across `tools/gh/main.py` + 3 new `tools/gh/_lib/*.py` modules + 12 new test files. Splitting would cross-pollinate git_ops/lock/vault_git_init imports and defeat worktree isolation. Sequential, alone in its wave.
- **C6 stays whole:** touches `src/assistant/main.py::Daemon.start` + `src/assistant/bridge/hooks.py::_validate_gh_argv`. Single file each but tightly coupled via the seed import + preflight helpers. Alone in its wave.

---

## Wave 0 — Pre-flight hotfix (sequential, 1 coder)

**Depends on:** nothing (phase-7 HEAD).
**Rationale for sequential:** two-line xfail removal + two small edits in one file. Not parallelisable, not worth it.

### C0 — genimage X-1/X-2 hotfix + xpassed reclassification

- **Branch:** `phase8-wave-0-commit-c0-genimage-hotfix`
- **Worktree:** `/tmp/0xone-phase8/wt_phase8-wave-0-commit-c0-genimage-hotfix`
- **Files modified:**
  - `tools/genimage/main.py` (two small edits in `_check_and_increment_quota` + `_read_quota_best_effort`)
  - `tests/test_genimage_quota_midnight_rollover.py` (remove two `@pytest.mark.xfail`; rename one test)
- **Test command:** `uv run pytest tests/test_genimage_quota_midnight_rollover.py -q`
- **Merge gate:** 0 failed / 0 xfailed in that file; full `uv run pytest -q` xfail count drops from 5 → 3.

**Wave 0 merge gate:** `uv run pytest -q && just lint && uv run mypy src --strict`.

---

## Wave 1 — Config + migrations/seed (parallel ×2)

**Depends on:** Wave 0 merged.
**Rationale for parallel:** C1 touches ONLY `src/assistant/config.py` + two new test files. C5a touches `src/assistant/state/*`, `src/assistant/scheduler/{store,seed}.py`, and new test files. Disjoint file sets.

### C1 — `GitHubSettings` + URL/tz/path validators

- **Branch:** `phase8-wave-1-commit-c1-github-settings`
- **Worktree:** `/tmp/0xone-phase8/wt_phase8-wave-1-commit-c1-github-settings`
- **Files modified:**
  - `src/assistant/config.py` (append `GitHubSettings(BaseSettings)` + wire into `Settings.github`)
- **Files created:**
  - `tests/test_gh_settings_ssh_url_validation.py`
  - `tests/test_gh_settings_tz_validation.py`
- **Test command:** `uv run pytest tests/test_gh_settings_*.py -x && uv run mypy src/assistant/config.py --strict`
- **Merge gate:** 19 new tests pass (14 original + 5 v2 additions: SF-C1, SF-F3, B-A1) + mypy strict clean + `GitHubSettings()` instantiable without `TELEGRAM_BOT_TOKEN`.

### C5a — Migrations 0005+0006 + seed helper + store extension

- **Branch:** `phase8-wave-1-commit-c5a-migrations-seed`
- **Worktree:** `/tmp/0xone-phase8/wt_phase8-wave-1-commit-c5a-migrations-seed`
- **Files created:**
  - `src/assistant/state/migrations/0005_schedule_seed_key.sql`
  - `src/assistant/state/migrations/0006_seed_tombstones.sql`
  - `src/assistant/scheduler/seed.py`
  - `tests/test_gh_seed_idempotency.py`
  - `tests/test_gh_seed_disabled.py`
  - `tests/test_gh_migration_v5_v6.py`
- **Files modified:**
  - `src/assistant/state/db.py` (bump `SCHEMA_VERSION=6`, add `_apply_v5` + `_apply_v6`)
  - `src/assistant/scheduler/store.py` (extend `insert_schedule(seed_key=...)`; add `find_by_seed_key`, `tombstone_exists`, `insert_tombstone`, `delete_tombstone`, `ensure_seed_row`)
- **Test command:** `uv run pytest tests/test_gh_seed_idempotency.py tests/test_gh_seed_disabled.py tests/test_gh_migration_v5_v6.py -x && uv run mypy src/assistant/scheduler src/assistant/state --strict`
- **Merge gate:** 3 new tests green + `executescript` not used (SF-F2) + migration DDL has `WHERE seed_key IS NOT NULL` (SF-D3 assertion).

**Wave 1 file disjointedness:**

- C1 files: `src/assistant/config.py`, `tests/test_gh_settings_ssh_url_validation.py`, `tests/test_gh_settings_tz_validation.py`
- C5a files: `src/assistant/state/migrations/0005_schedule_seed_key.sql`, `src/assistant/state/migrations/0006_seed_tombstones.sql`, `src/assistant/state/db.py`, `src/assistant/scheduler/seed.py`, `src/assistant/scheduler/store.py`, `tests/test_gh_seed_idempotency.py`, `tests/test_gh_seed_disabled.py`, `tests/test_gh_migration_v5_v6.py`

**Disjoint?** Yes — `config.py` vs `state/*` + `scheduler/*` + distinct test files. No file appears in both sets.

**Wave 1 merge gate (after both merged):** `uv run pytest -q && just lint && uv run mypy src --strict`.

---

## Wave 2 — tools/gh scaffolding + schedule CLI extension (parallel ×2)

**Depends on:** Wave 1 merged (C2 needs `GitHubSettings` symbol; C5b needs `seed_tombstones` table from C5a migration).
**Rationale for parallel:** C2 touches ONLY `tools/gh/*` + `skills/gh/SKILL.md`. C5b touches ONLY `tools/schedule/main.py` + one new test file. Disjoint.

### C2 — `tools/gh/` scaffolding + `auth-status` + SKILL.md draft

- **Branch:** `phase8-wave-2-commit-c2-gh-scaffold`
- **Worktree:** `/tmp/0xone-phase8/wt_phase8-wave-2-commit-c2-gh-scaffold`
- **Files created:**
  - `tools/gh/__init__.py`
  - `tools/gh/main.py` (~120 LOC skeleton with argparse subparsers; only `auth-status` has logic)
  - `tools/gh/_lib/__init__.py`
  - `tools/gh/_lib/exit_codes.py`
  - `tools/gh/_lib/gh_ops.py` (skeleton; `build_gh_env` + `gh_auth_status`)
  - `skills/gh/SKILL.md`
  - `tests/test_gh_skill_md_assertion.py`
  - `tests/test_gh_auth_status_probe.py`
- **Test command:** `uv run pytest tests/test_gh_skill_md_assertion.py tests/test_gh_auth_status_probe.py -x && uv run mypy tools/gh --strict`
- **Merge gate:** 3 tests pass (auth-status ok / not-authed / gh-not-on-PATH) + `python tools/gh/main.py auth-status` on dev box → rc=0 + SKILL.md H-13 rule present.

### C5b — `tools/schedule/main.py` `cmd_rm` rewrite + `cmd_revive_seed` + seed_tombstone test

- **Branch:** `phase8-wave-2-commit-c5b-schedule-tombstone`
- **Worktree:** `/tmp/0xone-phase8/wt_phase8-wave-2-commit-c5b-schedule-tombstone`
- **Files modified:**
  - `tools/schedule/main.py` (replace `cmd_rm` body — sync sqlite3 soft-delete + inline tombstone insert; add `cmd_revive_seed`; extend SUBCOMMANDS + subparser table)
- **Files created:**
  - `tests/test_gh_seed_tombstone.py` (v2 rewrite — subprocess path exercising `rm` → tombstone → seed skip → `revive-seed` → soft-deleted row still there)
  - `tests/test_gh_seed_tombstone_nullable_branch.py` (SF-D5 — `seed_key IS NULL` rows don't create tombstones)
- **Test command:** `uv run pytest tests/test_gh_seed_tombstone.py tests/test_gh_seed_tombstone_nullable_branch.py -x`
- **Merge gate:** 2 tests green + `cmd_rm` still returns rc=0 on existing soft-delete scenarios (no regression of phase-5 G-W2-x schedule rm tests).

**Wave 2 file disjointedness:**

- C2 files: `tools/gh/__init__.py`, `tools/gh/main.py`, `tools/gh/_lib/__init__.py`, `tools/gh/_lib/exit_codes.py`, `tools/gh/_lib/gh_ops.py`, `skills/gh/SKILL.md`, `tests/test_gh_skill_md_assertion.py`, `tests/test_gh_auth_status_probe.py`
- C5b files: `tools/schedule/main.py`, `tests/test_gh_seed_tombstone.py`, `tests/test_gh_seed_tombstone_nullable_branch.py`

**Disjoint?** Yes — `tools/gh/*` vs `tools/schedule/*` + distinct test files.

**Wave 2 merge gate:** `uv run pytest -q && just lint && uv run mypy src --strict`.

---

## Wave 3 — issue/pr/repo read-only (sequential, 1 coder)

**Depends on:** Wave 2 merged (C3 extends `tools/gh/main.py` + `tools/gh/_lib/gh_ops.py` from C2).
**Rationale for sequential:** C3 touches files that C4 will also touch (`tools/gh/main.py`, `tools/gh/_lib/gh_ops.py`). If parallelised with anything in `tools/gh/`, merge conflicts are inevitable. Alone in wave.

### C3 — issue/pr/repo read-only subcommands + allow-list

- **Branch:** `phase8-wave-3-commit-c3-gh-read-ops`
- **Worktree:** `/tmp/0xone-phase8/wt_phase8-wave-3-commit-c3-gh-read-ops`
- **Files modified:**
  - `tools/gh/main.py` (extend with `issue`, `pr`, `repo` subcommands + `_is_unauth_stderr` helper)
  - `tools/gh/_lib/gh_ops.py` (extend with `run_gh_json`)
- **Files created:**
  - `tools/gh/_lib/repo_allowlist.py`
  - `tests/test_gh_issue_create_happy.py`
  - `tests/test_gh_repo_whitelist.py`
  - `tests/test_gh_pr_view_flattens_author.py` (SF-A5)
- **Test command:** `uv run pytest tests/test_gh_issue_create_happy.py tests/test_gh_repo_whitelist.py tests/test_gh_pr_view_flattens_author.py -x && uv run mypy tools/gh --strict`
- **Merge gate:** 3 tests pass + `_is_unauth_stderr` matches `"authenticated"` AND `"not logged into"` (SF-A6) + `run_gh_json` catches `TimeoutExpired` (SF-A7).

**Wave 3 merge gate:** `uv run pytest -q && just lint && uv run mypy src --strict`.

---

## Wave 4 — vault-commit-push (sequential, 1 coder)

**Depends on:** Wave 3 merged (C4 extends `tools/gh/main.py` + imports `repo_allowlist` from C3).
**Rationale for sequential:** largest commit (~500 LOC src + ~400 LOC tests). Creates 3 new `tools/gh/_lib/*.py` modules + 12 new test files. Splitting into "src" and "tests" sub-commits would lose the test-first validation; splitting by test file would produce redundant git_ops fixtures. Alone in wave.

### C4 — `vault-commit-push` + git_ops + flock + vault_git_init

- **Branch:** `phase8-wave-4-commit-c4-vault-commit-push`
- **Worktree:** `/tmp/0xone-phase8/wt_phase8-wave-4-commit-c4-vault-commit-push`
- **Files modified:**
  - `tools/gh/main.py` (extend with `_cmd_vault_commit_push` + `_render_message` + `_ssh_key_readable` + `_do_push_cycle` + `vault-commit-push` subparser)
- **Files created:**
  - `tools/gh/_lib/git_ops.py` (~160 LOC — `porcelain_status`, `stage_all`, `commit`, `push`, `unpushed_commit_count`, `reset_soft_head_one`, `DIVERGED_RE`, `_base_env`)
  - `tools/gh/_lib/lock.py` (~50 LOC — `flock_exclusive_nb`)
  - `tools/gh/_lib/vault_git_init.py` (~80 LOC — `bootstrap` with `.gitignore`)
  - `tests/test_gh_vault_commit_push_happy.py`
  - `tests/test_gh_vault_commit_push_no_changes.py`
  - `tests/test_gh_vault_commit_push_diverged.py`
  - `tests/test_gh_vault_commit_path_isolation.py` (CRITICAL — I-8.1)
  - `tests/test_gh_flock_concurrency.py`
  - `tests/test_gh_ssh_key_missing.py`
  - `tests/test_gh_vault_git_bootstrap.py`
  - `tests/test_gh_dispatch_reply_no_artefact_match.py` (Q11 regression corpus — imports from `assistant.media.artefacts`)
  - `tests/test_gh_vault_commit_push_mkdir_fresh.py` (B-A3)
  - `tests/test_gh_vault_commit_push_unpushed_retry.py` (B-B2)
  - `tests/test_gh_vault_commit_push_diverged_resets.py` (B-B2)
  - `tests/test_gh_vault_commit_push_dry_run_no_flock.py` (SF-B3)
  - `tests/test_gh_flock_released_on_parent_sigkill.py` (S1)
  - `tests/fixtures/gh_responses.txt` (copy from `spikes/phase8/spike_artefact_re_corpus.txt`)
- **Test command:** `uv run pytest tests/test_gh_vault_commit_*.py tests/test_gh_flock_*.py tests/test_gh_ssh_key_missing.py tests/test_gh_vault_git_bootstrap.py tests/test_gh_dispatch_reply_no_artefact_match.py -x && uv run mypy tools/gh --strict`
- **Merge gate:** 12 new tests green + path-isolation test passes using REAL git (no mocks for the critical invariant) + `GIT_SSH_COMMAND` contains `IdentitiesOnly=yes` in happy path + `--dry-run` bypasses flock + mypy clean.

**Wave 4 merge gate:** `uv run pytest -q && just lint && uv run mypy src --strict`.

---

## Wave 5 — Daemon integration + `_validate_gh_argv` (sequential, 1 coder)

**Depends on:** Wave 4 merged (needs `tools.gh._lib.gh_ops.build_gh_env` import) AND Wave 1/2 merged (needs `ensure_vault_auto_commit_seed` from C5a).
**Rationale for sequential:** touches `src/assistant/main.py::Daemon.start` AND `src/assistant/bridge/hooks.py`. Both are single files but tightly coupled via the preflight helpers + seed import. Alone in wave.

### C6 — Daemon integration + `_validate_gh_argv` + preflight helpers

- **Branch:** `phase8-wave-5-commit-c6-daemon-integration`
- **Worktree:** `/tmp/0xone-phase8/wt_phase8-wave-5-commit-c6-daemon-integration`
- **Files modified:**
  - `src/assistant/main.py` (extend `Daemon.start` with preflight + ssh-key-parent cloud-sync warn + allowed_repos warn + gh preflight probe + seed call; add top-level `_verify_gh_config_accessible_for_daemon` async helper + `_check_path_not_in_cloud_sync` helper)
  - `src/assistant/bridge/hooks.py` (add `_validate_gh_argv` + dispatch in `_validate_python_invocation`)
- **Files created:**
  - `tests/test_gh_validate_argv.py` (12 allow + 14 deny cases including SF-C6 additions)
  - `tests/test_gh_bash_hook_integration.py`
- **Test command:** `uv run pytest tests/test_gh_validate_argv.py tests/test_gh_bash_hook_integration.py tests/test_daemon_*.py -x && uv run mypy src/assistant/main.py src/assistant/bridge/hooks.py --strict`
- **Merge gate:** 2 new tests green + all existing Daemon/hook tests still green (no regression) + `_verify_gh_config_accessible_for_daemon` is async (SF-E1) + forbidden `--body-file`, `pin`, `unpin`, `develop`, `status`, `repo clone`, `repo create` all denied (SF-C6).

**Wave 5 merge gate:** `uv run pytest -q && just lint && uv run mypy src --strict`.

---

## Wave 6 — Docs + README polish (single coder)

**Depends on:** Wave 5 merged (full functionality assembled; docs reference real behaviour).
**Rationale (single track):** phase-8 v2 (B-D6) removed the E2E test commit. C7 is docs-only. A second parallel track for "E2E tests" no longer exists in v2. The doc edit is one coder invocation.

### C7 — docs + README/CLAUDE.md mention

- **Branch:** `phase8-wave-6-commit-c7-docs`
- **Worktree:** `/tmp/0xone-phase8/wt_phase8-wave-6-commit-c7-docs`
- **Files created:**
  - `docs/ops/github-setup.md` (~180 LOC; 12-section playbook — dedicated account + repo + deploy key + .env template with `GH_VAULT_SSH_KEY_PATH` + `gh auth login` + key rotation + TOFU with isolated `UserKnownHostsFile` + override/disable cron + encryption warning + HOME caveat + future manual smoke note)
- **Files modified:**
  - `README.md` (add "Phase 8 — GitHub CLI wrapper + daily vault auto-commit" under "Phases shipped")
  - `CLAUDE.md` (same line + `gh` skill listed)
- **Test command:** `just lint && uv run pytest -q`
- **Merge gate:** lint green + all 12 doc sections present + `.env` template uses `GH_VAULT_SSH_KEY_PATH` (SF-F3) + TOFU section uses isolated `UserKnownHostsFile` (SF-F1) + README/CLAUDE mention shipped + phase-8 acceptance-summary items (§implementation.md) all satisfiable.

**Wave 6 merge gate:** `uv run pytest -q && just lint && uv run mypy src --strict`.

---

## Execution protocol per wave

1. Each coder spawned in `isolation: "worktree"` (git worktree).
2. Explicit commit in own worktree; NO push (orchestrator handles main-branch merges).
3. Between waves: orchestrator merges worktrees sequentially to main (`git rebase origin/main` in worktree, then `git merge --ff-only <branch>` on main, then `git push origin HEAD:main`).
4. Tests run full suite after merging the whole wave (`uv run pytest -q && just lint && uv run mypy src --strict`).
5. Rebase conflicts → block wave; orchestrator spawns fix-coder for the specific worktree with diff + conflict output. Max 3 retries. If still red: sequential fallback per §7 parallel-split-agent.md (drop parallelism for that wave).
6. `git worktree remove <path>` only after successful merge.
7. Tag next wave pre-start: `git tag phase8-pre-wave-<N+1>`.

## Estimated coder invocations

| Wave | Coders | Commits |
|---|---|---|
| Wave 0 | 1 | C0 |
| Wave 1 | 2 (parallel) | C1 + C5a |
| Wave 2 | 2 (parallel) | C2 + C5b |
| Wave 3 | 1 | C3 |
| Wave 4 | 1 | C4 |
| Wave 5 | 1 | C6 |
| Wave 6 | 1 | C7 |

**Total coder invocations:** 9.
**Maximum concurrency:** 2 per wave (Waves 1 and 2).
**Total elapsed merge cycles:** 7.

---

## Wave boundary rules

- **C5 split justified:** C5a (SQL migrations + state/db.py + scheduler/store.py + scheduler/seed.py + their tests) is a data-layer change. C5b (tools/schedule/main.py extension + CLI tests) is a CLI change. Files are disjoint: the data-layer does NOT touch `tools/schedule/main.py`, and the CLI does NOT touch migrations or store. C5b references `seed_tombstones` table defined by 0006 migration from C5a — hence C5a must merge before C5b starts (enforced by placing them in consecutive waves 1 → 2).

- **C4 is sequential (too big for splitting):** 3 new `tools/gh/_lib/*.py` modules all imported by the single `_cmd_vault_commit_push` handler. Splitting into "git_ops module commit" + "lock+vault_git_init+main.py commit" would leave the handler half-wired between worktrees and make test-first infeasible. Alone in Wave 4.

- **C6 is sequential:** touches `src/assistant/main.py` AND `src/assistant/bridge/hooks.py`. Both single-file edits but coupled: the Daemon wires `ensure_vault_auto_commit_seed` (from C5a) AND the hook validates argv that calls the CLI (from C4). Split wouldn't help; both depend on same upstream merges. Alone in Wave 5.

- **Wave 6 single-track:** v2 B-D6 removed the E2E test commit, so what was originally a "C7a docs + C7b E2E tests" split collapses to a single docs commit. Only one coder.

---

## Verify file disjointedness per wave

### Wave 0
- C0 only. Trivially disjoint (single coder).

### Wave 1
- C1: `src/assistant/config.py`, `tests/test_gh_settings_ssh_url_validation.py`, `tests/test_gh_settings_tz_validation.py`
- C5a: `src/assistant/state/migrations/0005_schedule_seed_key.sql`, `src/assistant/state/migrations/0006_seed_tombstones.sql`, `src/assistant/state/db.py`, `src/assistant/scheduler/seed.py`, `src/assistant/scheduler/store.py`, `tests/test_gh_seed_idempotency.py`, `tests/test_gh_seed_disabled.py`, `tests/test_gh_migration_v5_v6.py`

**Disjoint?** Yes — `config.py` ≠ `state/*` ≠ `scheduler/*`; no test filename overlaps.

### Wave 2
- C2: `tools/gh/__init__.py`, `tools/gh/main.py`, `tools/gh/_lib/__init__.py`, `tools/gh/_lib/exit_codes.py`, `tools/gh/_lib/gh_ops.py`, `skills/gh/SKILL.md`, `tests/test_gh_skill_md_assertion.py`, `tests/test_gh_auth_status_probe.py`
- C5b: `tools/schedule/main.py`, `tests/test_gh_seed_tombstone.py`, `tests/test_gh_seed_tombstone_nullable_branch.py`

**Disjoint?** Yes — `tools/gh/*` ≠ `tools/schedule/*`; distinct test files.

### Wave 3
- C3 only. `tools/gh/main.py` + `tools/gh/_lib/gh_ops.py` (both touched) + new `tools/gh/_lib/repo_allowlist.py` + 3 new test files. Trivially disjoint.

### Wave 4
- C4 only. `tools/gh/main.py` + 3 new `_lib` modules + 12 new test files + 1 new fixture. Trivially disjoint.

### Wave 5
- C6 only. `src/assistant/main.py` + `src/assistant/bridge/hooks.py` + 2 new test files. Trivially disjoint.

### Wave 6
- C7 only. `docs/ops/github-setup.md` (new) + `README.md` + `CLAUDE.md` (modified). Trivially disjoint.

---

## Merge-conflict risk per parallel wave

- **Wave 1 (C1 ∥ C5a):** LOW. `config.py` has no cross-import with `scheduler/store.py` until C5a runs. Risk only if C5a's `store.py` adds an import cycle via `assistant.config.GitHubSettings` — which it DOES (seed helper imports it). Mitigation: C5a's import is from the already-shipped `GitHubSettings` symbol if C1 merges first; otherwise C5a has a stub. Orchestrator MUST merge C1 before C5a in Wave 1 to avoid this. **Merge order in Wave 1: C1 first, then C5a.**

- **Wave 2 (C2 ∥ C5b):** MINIMAL. `tools/gh/*` vs `tools/schedule/*` are entirely separate trees. Only shared touchpoint is hypothetical `pyproject.toml` if either coder adds a script entry — FORBID that (tools invoked by path, not script).

---

## Notes for orchestrator

- **Wave 0** starts immediately (depends on nothing).
- **Wave 1** starts after Wave 0 merge. **Merge order in wave:** C1 first → C5a second (C5a's seed module imports `GitHubSettings`).
- **Wave 2** starts after Wave 1 merge. **Merge order in wave:** C2 first → C5b second (no true ordering requirement, but keep `tools/gh/*` shaped before any downstream coder consumes; either order works).
- **Wave 3** sequential; starts after Wave 2 merge.
- **Wave 4** sequential; starts after Wave 3 merge.
- **Wave 5** sequential; starts after Wave 4 merge (needs full `tools/gh/` + seed + migrations all in main).
- **Wave 6** sequential; starts after Wave 5 merge.
- **Rollback:** if a wave fails — before retrying the coder, orchestrator runs `git worktree remove <path>` + creates a new worktree from latest main. Preserves the `phase8-pre-wave-N` tag as rollback anchor.
- **Fallback:** if >50% of parallel waves (Waves 1 + 2) require >1 follow-up, set `PHASE8_PARALLEL_DISABLED=1` and re-run all remaining parallel waves sequentially (single coder per commit).

---

## Per-coder manifest-prompt skeleton (template for orchestrator)

> You are a Wave-N coder for phase 8 (commit CX). Read `/Users/agent2/Documents/0xone-assistant/plan/phase8/implementation.md` §<relevant section>. Also read `/Users/agent2/Documents/0xone-assistant/plan/phase8/wave-plan.md` (this document) Wave N. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase8/detailed-plan.md` r2 for cross-reference. Produce EXACTLY the files listed under your commit in this wave-plan; do NOT edit files outside the listed set. Commit message format: `phase 8: <commit short description>` (single commit, in your worktree, NO push). Run the per-commit test command listed under your commit; must be green before reporting done. Pitfalls to respect: implementation.md §0 items 1–21 (all spike-verified). OAuth via `claude` CLI; NO `ANTHROPIC_API_KEY`.

(Orchestrator substitutes N, CX, and §section per row; uses absolute paths.)

---

This wave-plan is derived from implementation.md v2 (2646 lines) + detailed-plan.md r2 (609 lines) + phase-7 wave-plan.md format template. Ready for multi-wave coder execution.
