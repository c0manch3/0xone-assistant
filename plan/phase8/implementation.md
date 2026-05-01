# Phase 8 — implementation notes

> Coder pass. Spec v3.1 (post-4-reviewer-wave fix-pack) at
> [`description.md`](description.md). The previous implementation
> notes live at `implementation-prewipe-rejected.md`; this file is a
> fresh start.

## Fix-pack changelog (v3 → v3.1)

The 4-reviewer convergent fix-pack landed 7 CRITICAL + 6 HIGH closures
without scope-creep into phase 9. Summary of files touched:

| Path | Delta | Purpose |
|------|-------|---------|
| `src/assistant/config.py` | +60 LOC | F4 vault_lock budget validator + F5 commit template restore + F6 effective_manual_tool_enabled property + F11 first_tick_delay_s + F12 non-anchored denylist regex |
| `src/assistant/bridge/claude.py` | +20 LOC | F1 `vault_tool_visible` kwarg gates both `mcp_servers["vault"]` and `VAULT_TOOL_NAMES` allowed-tool entries |
| `src/assistant/main.py` | +5 LOC | F1 owner bridge passes `vault_tool_visible=settings.vault_sync.effective_manual_tool_enabled` |
| `src/assistant/vault_sync/subsystem.py` | +60 LOC / -25 LOC | F3 loop refactor (each tick is a fresh child task) + F9 notify outside lock (RunResult._notify_action tag) + F10 failed-call rate-limit reset + F11 wall-clock-target sleep + first_tick_delay_s |
| `src/assistant/vault_sync/git_ops.py` | +6 LOC | F8 `shlex.quote` SSH paths |
| `src/assistant/vault_sync/notify.py` | +25 LOC / -10 LOC | F9 `_send_with_timeout` wrapper + asyncio.wait_for ceiling |
| `deploy/docker/docker-compose.yml` | +18 LOC | F2 long-form bind + create_host_path: false |
| `deploy/docker/README.md` | +50 LOC | F2 explicit bootstrap-before-compose-up sequence |
| `deploy/scripts/vault-bootstrap.sh` | (1 line) | F12 mirror non-anchored regex |
| `tests/test_phase8_settings_validator.py` | +90 LOC | F4 + F6 + F1 effective property tests |
| `tests/test_phase8_validate_paths.py` | +30 LOC | F12 recursive match tests |
| `tests/test_phase8_subsystem_run_once.py` | (3 lines) | F4 fixture compliance |
| `tests/test_phase8_push_now_rate_limit.py` | (3 lines) | F4 fixture compliance |
| `tests/test_phase8_disabled_invariants.py` | NEW 130 LOC | AC#5 + F1 invariants |
| `tests/test_phase8_startup_check.py` | NEW 110 LOC | AC#3 + AC#17 + AC#26 |
| `tests/test_phase8_prompt_injection_regression.py` | NEW 130 LOC | AC#15 + F10 retry-after-failure |
| `tests/test_phase8_git_ssh_command_scope.py` | NEW 110 LOC | AC#16 + F8 |
| `tests/test_phase8_loop_integration.py` | NEW 165 LOC | F3 + F11 + F13 drain integration |
| `tests/test_phase8_loop_first_tick_timing.py` | NEW 130 LOC | AC#24 |
| `tests/test_phase8_rss_observer_field.py` | NEW 165 LOC | AC#25 |
| `tests/test_phase8_vault_lock_race.py` | NEW 80 LOC | AC#12 |
| `plan/phase8/description.md` | +120 LOC | spec v3 → v3.1 deltas + §9 fix-pack closure table |
| `plan/phase8/implementation.md` | (this file) | fix-pack changelog |

**Test count**: 30 new tests (8 new test files), brings phase 8 from
38 → 68 tests; full suite from baseline 983 to ~1013 green.

**Production LOC delta**: +120 LOC across 6 source files.

## Files modified / added

### Modified

| Path | LOC delta (≈) | Purpose |
|------|---------------|---------|
| `src/assistant/config.py` | +110 | New `VaultSyncSettings` BaseSettings + cross-field model_validator. Wired as `Settings.vault_sync`. |
| `src/assistant/main.py` | +95 | Daemon attribute + boot block + drain block + `_rss_observer` field. |
| `src/assistant/bridge/claude.py` | +3 | Register `VAULT_SERVER` and `VAULT_TOOL_NAMES` in `_build_options`. |
| `deploy/docker/docker-compose.yml` | +9 | Read-only bind-mounts for `vault_deploy` SSH key + `known_hosts_vault`. |
| `deploy/docker/README.md` | +50 | Phase 8 vault sync setup section. |
| `plan/phase8/description.md` | (6 edits) | Spec v2 → v3 fixes folded into the same file. |

### Added

| Path | LOC | Purpose |
|------|-----|---------|
| `src/assistant/vault_sync/__init__.py` | 21 | Package surface. |
| `src/assistant/vault_sync/_validate_paths.py` | 42 | Secret denylist regex helper. |
| `src/assistant/vault_sync/audit.py` | 60 | JSONL append + 10 MB rotation. |
| `src/assistant/vault_sync/boot.py` | 80 | `_cleanup_stale_vault_locks`. |
| `src/assistant/vault_sync/git_ops.py` | 245 | Async wrappers around `git status / add / commit / push`. |
| `src/assistant/vault_sync/notify.py` | 90 | Telegram edge-trigger notify wrapper. |
| `src/assistant/vault_sync/subsystem.py` | 480 | `VaultSyncSubsystem` central class. |
| `src/assistant/tools_sdk/_vault_core.py` | 70 | @tool shared helpers. |
| `src/assistant/tools_sdk/vault.py` | 110 | `vault_push_now` MCP @tool. |
| `skills/vault/SKILL.md` | 35 | Skill discoverability. |
| `deploy/scripts/vault-bootstrap.sh` | 175 | Idempotent owner-runs-once setup. |
| `deploy/known_hosts_vault.pinned` | 3 lines | Pinned GitHub host keys (ed25519 + ecdsa + rsa). |
| `docs/ops/vault-host-key-rotation.md` | 70 | Host-key rotation runbook. |
| `docs/ops/vault-secret-leak-recovery.md` | 110 | Secret-leak recovery runbook. |
| `tests/test_phase8_settings_validator.py` | 100 | 6 tests — pydantic model_validator. |
| `tests/test_phase8_audit_log_rotation.py` | 75 | 4 tests — append + rotation. |
| `tests/test_phase8_validate_paths.py` | 110 | 8 tests — denylist semantics. |
| `tests/test_phase8_subsystem_run_once.py` | 240 | 6 tests — pipeline branches. |
| `tests/test_phase8_push_now_rate_limit.py` | 160 | 4 tests — rate-limit + restart resilience. |
| `tests/test_phase8_edge_trigger_notify.py` | 195 | 6 tests — state machine. |
| `tests/test_phase8_drain.py` | 80 | 3 tests — F11 drain pattern. |
| `tests/test_phase8_cleanup_stale_vault_locks.py` | 75 | 5 tests — boot lock cleanup. |

Production LOC ≈ 1500. Tests ≈ 1100. Total ≈ 2600 LOC.

## Key design decisions

### `vault_lock` as sync context manager (PART A Edit 2)

The phase-4 `vault_lock(...)` from `_memory_core.py:606` is a
**synchronous** `@contextmanager` (it polls fcntl with 50 ms sleeps,
not async-aware). We use a plain `with` inside the async pipeline,
NOT `async with`. The git subprocess calls inside the `with` block
run via `asyncio.create_subprocess_exec`, so the asyncio event loop
keeps making progress while `vault_lock` is held — only the current
coroutine yields cooperatively as the subprocess completes.

### Two-tier locking (W2-C2)

Outer `self._lock: asyncio.Lock` wraps the FULL pipeline including
`git push`, so cron + manual @tool serialise end-to-end against each
other (no concurrent `git push origin main` ever).

Inner `vault_lock` (fcntl) wraps only `status / add / commit` and is
RELEASED before `git push`. This keeps a parallel `memory_write`
unblocked during the network leg.

### Drain ordering deviates from phase-6e precedent

The phase-6e `_audio_persist_pending` drain runs AFTER `_bg_tasks`
cancel because the persist tasks are *shielded* inside the bg job's
`finally`. The drain is recovering shielded tasks that survived the
`_bg_tasks` cancel.

Phase 8 vault sync push tasks are **NOT shielded** — cancelling
mid-flight orphans the SSH pipe and leaves `.git/index.lock` to be
reaped by `_cleanup_stale_vault_locks` on next boot. Hence the drain
runs **BEFORE** `_bg_tasks` cancel: the supervised loop must still be
alive while we wait for the in-flight push to finish naturally.

### Edge-trigger state machine

Three transitions matter:
- `ok → fail` → notify "vault sync failed: …"
- `fail → fail` → silent unless `consecutive_failures` matches a
  milestone (default 5/10/24)
- `fail → ok` → notify "vault sync recovered after N consecutive
  failures"

`lock_contention` and `rate_limited` are explicitly NOT counted as
failures (W2-C1). They write audit rows, emit structured logs, and do
not transition the state machine.

### `VaultSyncSettings.manual_tool_enabled` default flipped to `False`

The spec table in §3 specifies `True` as the default, but pairing
that with the `enabled=False` default would fail the
`manual_tool_enabled requires enabled=True` validator at every daemon
boot on a fresh checkout. Defaulting to `False` keeps construction
self-consistent without needing the validator to distinguish
"user-set" vs "framework-default". Owners who flip
`VAULT_SYNC_ENABLED=true` typically set
`VAULT_SYNC_MANUAL_TOOL_ENABLED=true` in the same env diff. This is a
small spec divergence documented inline in the settings field.

### `repo_url` regex enforcement

Spec edit #3 made the pydantic v2 validator authoritative for both
`repo_url` shape and `enabled=True ⇒ repo_url required`. The regex
`^git@[a-z0-9.-]+:[\w.-]+/[\w.-]+\.git$` is permissive enough for
self-hosted forges (L-2) but rejects HTTPS URLs and bare strings at
load time.

## Test coverage summary

41 test functions across 8 files; 52 total test cases after pytest
parametrize expansion (well above the 25-40 floor in the spec; total
assertion count is 100+). Phase 8 tests run in ~0.5s in isolation;
the full suite is 979 passed, 4 skipped, in 23s on the dev box.

| File | Test funcs | Cases | What it covers |
|------|------------|-------|----------------|
| `test_phase8_settings_validator.py` | 6 | 6 | All four validator branches + happy path + defaults. |
| `test_phase8_audit_log_rotation.py` | 4 | 4 | Plain append, rotation, prior-`.1` overwrite, fresh-file size. |
| `test_phase8_validate_paths.py` | 7 | 18 | Anchored regex semantics, including the nested-path anti-test (parametrised across 9 hit + 4 miss + 3 misc paths). |
| `test_phase8_cleanup_stale_vault_locks.py` | 5 | 5 | Stale index/refs cleanup, fresh-lock preservation, missing-dir. |
| `test_phase8_subsystem_run_once.py` | 6 | 6 | Happy push, noop, push failure, lock_contention, denylist block, fail→ok recovery. |
| `test_phase8_push_now_rate_limit.py` | 4 | 4 | First call, second within window, restart resilience, post-window. |
| `test_phase8_edge_trigger_notify.py` | 6 | 6 | All five state-machine transitions + restart persistence. |
| `test_phase8_drain.py` | 3 | 3 | F11 drain pattern in isolation. |

Tests use `monkeypatch.setattr(sub_mod, "git_*", _fake)` to replace
the async git wrappers so no real git subprocess is spawned. The fake
`MessengerAdapter` impl in `test_phase8_edge_trigger_notify.py`
records `send_text` calls without touching aiogram.

## Open issues / things I couldn't resolve

- **No live test of an actual `git push`** against a real GitHub
  repo. The container-level `docker exec` smoke test in the deploy
  runbook (`deploy/docker/README.md` "Phase 8 vault sync — one-time
  setup") is the canonical verification path.
- **The pinned host keys may go stale** on the rare occasion GitHub
  rotates them. The `docs/ops/vault-host-key-rotation.md` runbook
  covers the recovery path, but the primary trigger is operational
  vigilance, not a CI alarm.
- **`Daemon.start` integration test missing.** The new boot block at
  `main.py` is verified only through the unit tests of its
  components (subsystem, audit, boot). A full Daemon `start/stop`
  test is more invasive (requires a fake adapter + DB + bridge) than
  the unit tests; deferred to a future phase if regression rate
  warrants it.

## Deploy smoke runbook (AC#1-26)

Owner runs this on the VPS after the next CI image lands:

1. **Bootstrap:** `sudo -u 0xone /opt/0xone-assistant/deploy/scripts/vault-bootstrap.sh`
2. **Update env:** add three lines to `~/.config/0xone-assistant/.env`:
   ```
   VAULT_SYNC_ENABLED=true
   VAULT_SYNC_REPO_URL=git@github.com:c0manch3/0xone-vault.git
   VAULT_SYNC_MANUAL_TOOL_ENABLED=true
   ```
3. **Restart:** `cd /opt/0xone-assistant/deploy/docker && docker compose restart`
4. **Verify mounts:** `docker exec 0xone-assistant ls -l /home/bot/.ssh/vault_deploy /home/bot/.ssh/known_hosts_vault`
5. **Wait ~60s and tail logs:**
   ```
   docker compose logs --tail 100 0xone-assistant | grep vault_sync
   ```
   Expect `vault_sync_startup_check_ok`, then `vault_sync_pushed` (if
   the vault has pre-existing notes) or `vault_sync_no_changes` (if
   empty).
6. **Inspect GH:** open `https://github.com/c0manch3/0xone-vault` —
   expect a fresh commit from `0xone-assistant
   <0xone-assistant@users.noreply.github.com>` with the `vault sync ...`
   message template.
7. **Trigger the manual @tool:** in Telegram, send "запушь вольт"
   to the bot. Expect a Telegram reply summarising the result and
   another commit on GitHub.
8. **Verify the rate limit:** immediately send "запушь вольт" again
   (within 60s). Expect the bot to surface the `rate_limit` reason.
9. **Verify regression suite:**
   - Phase 1: `/ping`.
   - Phase 4: ask the bot to remember a fact, then recall it.
   - Phase 5b: schedule a one-shot reminder, wait, observe the fire.
   - Phase 6a-6c: send a PDF, a photo, a voice memo — all should
     route to the existing handlers.
   - Phase 6: spawn a long subagent task.
10. **Fault injection (optional):** stop the daemon mid-push (e.g.
    invoke `vault_push_now` then `docker compose stop` within ~5s)
    and verify the next start finds no leftover `.git/index.lock`
    via `_cleanup_stale_vault_locks`.
