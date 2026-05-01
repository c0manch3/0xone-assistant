# Phase 8 — Vault → GitHub push-only periodic sync

> Spec v3.1 — post-4-reviewer-wave fix-pack. Owner-fixed scope (no
> re-litigation): push-only direction, dedicated private GH repo, SSH
> deploy key auth, periodic batch trigger. Scheduler `kind`-column
> integration was considered in v0 and **rejected by the owner** in
> favour of a self-contained `asyncio` loop spawned from
> `Daemon.start()` — see §2.1 and the v0→v1 architecture diff in §7.
> v1 closed devil w1 (5 CRITICAL + 6 HIGH + key MEDIUMs); v2 folded
> devil w2 closures (3 CRITICAL + 4 HIGH + 6 MEDIUM + 3 LOW). v3.1
> folds the convergent fix-pack from the 4-reviewer wave
> (code-reviewer + qa-engineer + devops-expert + devil-w3) — 7
> CRITICAL + 6 HIGH closures listed in §9.
>
> Spec deltas in v3.1 (relative to v3):
>
> - `manual_tool_enabled` default flipped back to `True` (spec §3
>   table) with a smarter validator that distinguishes owner-set vs
>   default and a new computed property `effective_manual_tool_enabled`
>   that gates the @tool registration (F1 + F6).
> - `git_op_timeout_s` default lowered 30→10s; `vault_lock_acquire_timeout_s`
>   bumped to 60s; new validator enforces `vault_lock_acquire_timeout_s
>   >= 4 * git_op_timeout_s` (F4).
> - `commit_message_template` default restored to include `{filenames}`
>   placeholder; em-dash replaced with `--` for legacy log-viewer
>   compat (F5).
> - `secret_denylist_regex` patterns now non-anchored
>   (`(?:^|/)secrets/` etc.) for parity with the recursive
>   `.gitignore` rules (F12).
> - New setting `first_tick_delay_s: float = 60.0` — sleeps BEFORE
>   the first tick to avoid competing with daemon boot pressure (F11);
>   contradicts the previous "fire one immediate tick at startup"
>   contract — owner can override to 0 to restore.
> - Docker compose bind-mounts switched to long-form
>   `type: bind` + `create_host_path: false` so a missing pre-bootstrap
>   key file errors LOUDLY instead of silently auto-creating it as a
>   directory (F2).
> - Cron loop refactored to spawn each tick as a fresh child task
>   that registers in `_vault_sync_pending` and self-removes; the
>   outer loop task NEVER pollutes the pending set so drain budget is
>   no longer always-exhausted (F3).
> - Notify dispatch moved OUTSIDE the `_lock` so a slow Telegram
>   backend cannot stall the cron loop; `notify_*` wrap `send_text` in
>   `asyncio.wait_for(..., timeout=10s)` (F9).
> - `vault_push_now` failed result resets `last_invocation_at` to its
>   prior value so the owner can retry immediately while diagnosing
>   the failure (F10).
> - `GIT_SSH_COMMAND` paths are `shlex.quote`-d (F8).

## 1. Goal

The bot's long-term memory vault at `<data_dir>/vault/` on the VPS gets
push-only synced to a dedicated private GitHub repo on a periodic batch
schedule. GitHub holds a read-only mirror of the vault contents — the
bot is the sole writer, there is no pull-back path, and out-of-band
edits on the GH side are treated as an error to surface (not
auto-merge).

Trigger architecture: a single supervised `_spawn_bg_supervised`
asyncio task anchored in `Daemon._bg_tasks`, firing every
`cron_interval_s` seconds (default `3600.0` = hourly), with the **first
tick fired immediately at startup** so deploys produce visible
push activity within seconds rather than after a full hour.
**No SchedulerLoop / SchedulerDispatcher involvement, no `kind`
column, no SQL migration in this phase.** The loop is a leaf
component of the daemon, isolated from phase-5b's prompt-injection
schedule path entirely.

The single supported flow:

1. Owner chats with the bot → the model invokes the existing phase-4
   `memory_write` MCP tool → markdown files appear under
   `<data_dir>/vault/` (existing phase-4 behaviour, unchanged).
2. At daemon startup, then every `cron_interval_s` seconds, the
   daemon's vault-sync loop wakes, takes the daemon-scoped
   `_lock` (asyncio) for the FULL pipeline, then acquires the
   inner `vault_lock` (fcntl) only around `git status / add /
   commit`, releases the inner fcntl lock before `git push`
   (so concurrent `memory_write` is unblocked during the network
   call), and finally releases the outer asyncio lock once push
   completes.
3. The dedicated GH repo `c0manch3/0xone-vault` accumulates a commit
   log of vault changes, viewable in the browser. No human or other
   system pushes to that repo — the deploy key is the only writer.
4. Model can also trigger an immediate push via the `vault_push_now`
   MCP @tool ("важная заметка, давай сразу засинхрю" → model invokes
   the tool → same git pipeline runs synchronously, gated by a 60s
   per-invocation rate-limit and an audit log).

This phase does NOT add `gh` CLI features — pure git-over-SSH only. No
PR creation, no issue read/write, no `gh api` extensions. The sole
GitHub interaction is `git push` over SSH using a dedicated deploy key
unique to the `0xone-vault` repo.

**Closed RQ decisions** (orchestrator-confirmed with owner; folded
into this spec, no longer "open"):

- Repo: `c0manch3/0xone-vault` (dedicated private), SSH URL
  `git@github.com:c0manch3/0xone-vault.git`.
- Trigger interval: `cron_interval_s = 3600.0` (hourly). Driven by
  `asyncio.sleep` inside the loop, NOT the scheduler subsystem.
- GH account: `c0manch3` (owner's main account; deploy key scoped to
  the single `0xone-vault` repo so blast radius is bounded).
- Manual trigger: IN scope as `vault_push_now` MCP @tool with 60s
  rate-limit + JSONL audit log.
- Notify channel on failure: Telegram, **edge-trigger state machine**
  (ok→fail / fail→ok recovery / milestone N=5/10/24). No flat 24h
  cooldown. See §2.7.
- Subsystem shape: simple asyncio loop owned by `Daemon`. **NO
  scheduler `kind` column. NO migration `0005_schedules_kind.sql`. NO
  dispatcher branch.** This v1 design eliminates the entire phase-5b
  regression surface that v0 introduced.

## 2. Architecture

### 2.1 Trigger mechanism — supervised asyncio loop, immediate first tick

The vault-sync subsystem is a self-contained loop spawned from
`Daemon.start()` via `_spawn_bg_supervised` (NOT raw
`asyncio.create_task` — W2-M1 closure). Supervised spawn matches the
scheduler/dispatcher pattern: an unhandled exception inside the loop
respawns after `backoff_s` seconds, up to `max_respawn_per_hour`
crashes within a rolling hour before the supervisor gives up and
notifies the owner.

**Boot ordering**: insertion point is `Daemon.start()` AFTER subagent
boot (currently the picker spawn at `main.py:570`) and BEFORE the
scheduler boot block (`main.py:572`). Lifecycle:

```python
# Daemon.start (sketch — implementation in coder phase)
# After: self._spawn_bg(self._sub_picker.run())  ~main.py:570
# Before: if sched_enabled: ...                  ~main.py:572
if self._settings.vault_sync.enabled:
    _cleanup_stale_vault_locks(self._settings.vault_dir)  # see §2.5
    self._vault_sync = VaultSyncSubsystem(
        vault_dir=self._settings.vault_dir,
        index_db_lock_path=Path(
            str(self._settings.memory_index_path) + ".lock"
        ),
        settings=self._settings.vault_sync,
        adapter=self._adapter,
        owner_chat_id=self._settings.owner_chat_id,
        run_dir=self._settings.data_dir / "run",
        pending_set=self._vault_sync_pending,  # §2.9
    )
    await self._vault_sync.startup_check()  # known_hosts pin + key file
    self._spawn_bg_supervised(
        self._vault_sync.loop, name="vault_sync_loop"
    )
```

The loop body runs the FIRST tick immediately, then sleeps between
ticks (W2-H1 closure):

```python
async def loop(self) -> None:
    # W2-H1: fire one tick at startup so a deploy produces visible
    # push activity within ~60s instead of waiting cron_interval_s.
    while True:
        try:
            await self.run_once(reason="scheduled")
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            self._log.exception("vault_sync_loop_error")
            # Continue — never let the loop die. Supervisor catches
            # only if the exception escapes; this branch belt-and-
            # braces the inner pipeline.
        await asyncio.sleep(self._settings.cron_interval_s)
```

**Interval-timer semantics, not cron walltime** (W2-H1). Sleep is a
plain `await asyncio.sleep(cron_interval_s)`; clock drift after host
suspend / container freeze / process pause is **accepted**. On wake
exactly one tick fires (no catch-up of missed ticks). This contrasts
with `scheduler/loop.py` which computes next-fire walltime from a cron
expression and is suspend-aware. Vault sync deliberately is NOT —
"sync at least once per hour while the process runs" is the contract,
not "sync at every wall-clock hour boundary".

When `enabled=False` (default), no task is spawned, no module state is
constructed, no MCP @tool registers. The daemon is observably
unchanged from a phase-7-shipped baseline (W2-M6: see AC#5 for the
precise observable form).

**No model turn is paid** for scheduled ticks (the loop calls
`run_once` directly, not via prompt injection). This eliminates devil
w0 §3 scheduler-pre-prompt-injection concerns entirely — a scheduled
tick is a private-API method call, not a model prompt.

### 2.2 Repo layout — .git inside the vault dir

The vault directory `<data_dir>/vault/` is itself the working tree of
a dedicated git repo. `.git/` lives directly under
`<data_dir>/vault/.git/`. There is no superproject — the vault is its
own standalone repo.

`.gitignore` (committed at repo init by the bootstrap script in §4)
includes the secret-leak defence-in-depth list:

```
# anything that shouldn't leave the VPS — defence in depth, also
# enforced by VaultSyncSubsystem._validate_staged_paths (§2.7).
*.env
*.key
*.pem
secrets/
.aws/
.config/0xone-assistant/
# vault-internal — must never be committed
.tmp/
*.lock
memory-index.db
memory-index.db-wal
memory-index.db-shm
# editor / OS clutter
*.swp
.DS_Store
*~
```

The vault's git config has `core.autocrlf = false`,
`core.filemode = false`, `user.email` and `user.name` set to a
bot-identity (`0xone-assistant
<0xone-assistant@users.noreply.github.com>`; W2-L2 — using GH's
`users.noreply` form avoids the rare case where GH email-blocks
private accounts that receive commits with arbitrary domains).

### 2.3 Push mechanism — pure git over SSH, env scoped per-subprocess

The subsystem shells out to the system `git` binary via
`asyncio.create_subprocess_exec` (argv form, never shell). Authentication
is via an SSH deploy key generated during the §4 bootstrap, stored at
`~/.ssh/vault_deploy` on the VPS, and registered as the **only**
deploy key with write access on the GH repo.

Each subprocess invocation builds an explicit `env=` dict copying
`os.environ` and overriding/adding `GIT_SSH_COMMAND`. Exact form:

```python
GIT_SSH_COMMAND = (
    f"ssh -i {settings.ssh_key_path} "
    f"-o IdentitiesOnly=yes "
    f"-o StrictHostKeyChecking=yes "  # H4: not accept-new
    f"-o UserKnownHostsFile={settings.ssh_known_hosts_path}"
)
env = {**os.environ, "GIT_SSH_COMMAND": GIT_SSH_COMMAND}
proc = await asyncio.create_subprocess_exec(
    "git", "push", "origin", settings.branch,
    cwd=str(vault_dir),
    env=env,  # H3: scoped to this subprocess only
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
```

**H3 / Devil-w1 closure (carry-forward)**: `GIT_SSH_COMMAND` is passed
via the `env=` parameter to `asyncio.create_subprocess_exec(...)`.
**NEVER set on the daemon process via `os.environ.update` or any other
mutation of the daemon's process env.** Any unrelated subprocess
spawned by phase-4/5/6 must see the host's default ssh state, not the
vault deploy key path. This is verified by AC#16.

This isolates vault git ops from any other ssh-agent or default-key
behaviour on the host. No `gh` CLI dependency — `gh` is not invoked
anywhere in this phase.

### 2.4 Locking — two-tier: `_lock` (asyncio outer) + `vault_lock` (fcntl inner)

**W2-C2 closure.** v1 used the language "vault_lock around git
status/add/commit" and an `asyncio.Lock` for "daemon-internal
concurrency", but did not crisply spell out which lock covered which
parts of the pipeline. v2 makes the contract explicit.

There are **two** locks in play:

1. **`self._lock: asyncio.Lock`** (daemon-scoped, in-memory). Wraps
   the **entire** `run_once` pipeline including `git push`. Its job
   is to serialise the cron loop and the `vault_push_now` @tool path
   against each other end-to-end so two concurrent `git push origin
   main` invocations cannot race against the same remote ref.
2. **`vault_lock(<index_db>.lock, ...)`** (process-wide, fcntl-based,
   imported from `assistant.tools_sdk._memory_core`). Wraps **only**
   `git status` / `git add` / `git commit`, then is **released
   before** `git push`. Its job is to prevent the half-written
   `<vault>/.tmp/.tmp-XXX.md` files produced by `memory_write`'s
   `tempfile.NamedTemporaryFile` + `os.replace` pattern from landing
   in a commit (devil w1 C-2). Releasing it before push lets
   `memory_write` resume during the (potentially slow) network leg.

The lock primitive is fcntl-based on the index-db lock path
`<index_db_path>.lock` (NOT inside the vault dir — same lock primitive
that `memory_write`, `memory_delete`, and `reindex_under_lock` already
use; `_memory_core.vault_lock` at `_memory_core.py:606`).

`vault_lock` is a SYNCHRONOUS context manager (`@contextmanager` from
`contextlib`). Use plain `with`, NOT `async with`. Run git ops inside
via `asyncio.create_subprocess_exec` so the asyncio loop is not
blocked.

Sequence inside `run_once(reason)`:

```python
async with self._lock:               # OUTER (asyncio)
    # 1. Acquire INNER fcntl lock with separate timeout (W2-C1).
    try:
        with vault_lock(
            lock_path,
            blocking=True,
            timeout=self._settings.vault_lock_acquire_timeout_s,  # 30s
        ):
            # 2. Working-tree-affecting ops only.
            await self._git_status()
            await self._git_add()
            staged = await self._validate_staged_paths()
            if not staged:
                # noop: empty diff → audit row, return.
                ...
                return RunResult(result="noop", ...)
            await self._git_commit(reason=reason, staged=staged)
        # 3. INNER vault_lock RELEASED here. memory_write can now
        #    resume; concurrent vault_sync invocations still block on
        #    the OUTER asyncio _lock above.
    except TimeoutError:
        # W2-C1: vault_lock contention is NOT a push failure.
        return RunResult(result="lock_contention", ...)

    # 4. Push WITHOUT vault_lock — git push only reads .git/objects/
    #    and the network; it never touches the working tree.
    await self._git_push()
# OUTER _lock released here.
```

**W2-C1 closure — `lock_contention` ≠ push failure.** When
`memory_write` holds `vault_lock` longer than
`vault_lock_acquire_timeout_s` (default 30s), `vault_lock` raises
`TimeoutError`. The subsystem **must not** treat this as a push
failure:

- `result="lock_contention"` row written to the audit log (§2.8).
- Structured log line `event=vault_sync_lock_contention`.
- `consecutive_failures` is **not** incremented.
- Edge-trigger state machine (§2.7) does **not** transition
  `ok→fail`.
- No Telegram alert, no milestone notify.

The rationale: the lock-contention condition is a within-bot timing
phenomenon (one process subsystem racing another) and self-corrects
on the next tick. Counting it against the "is GitHub reachable?"
edge-trigger would conflate two unrelated failure modes and burn
milestone notifies on benign slow `memory_write` storms.

Both `vault_push_now` (§2.8) and the cron loop go through the same
`run_once` and inherit both locks. There is no path that pushes
without holding `_lock`.

**Push concurrency invariant (AC#6).** While `_lock` is held by one
caller (cron tick or manual @tool), the other caller blocks on
`_lock.acquire()` and waits its turn — there is never a moment when
two `git push origin main` subprocesses are both running.

The `.gitignore` listing of `.tmp/` and `*.lock` is defence-in-depth:
even if a write somehow lands during a brief lock-release window, those
artefacts are excluded from `git add -A`.

### 2.5 Stale .git/index.lock cleanup at boot

Devil C-5: a SIGKILL'd daemon mid-`git commit` leaves
`<vault>/.git/index.lock` on disk; the next vault sync cycle hangs
indefinitely on `fatal: Unable to create '.git/index.lock': File
exists`. We mirror the `_boot_sweep_uploads` pattern for these locks.

`Daemon.start()` runs `_cleanup_stale_vault_locks(vault_dir)` BEFORE
spawning the loop. The function:

1. If `vault_dir/.git/index.lock` exists AND its `mtime` is older
   than `60s`, `unlink(missing_ok=True)` it. Log
   `event=vault_sync_stale_index_lock_cleared`.
2. Walk `vault_dir/.git/refs/**/*.lock` (e.g. `refs/heads/main.lock`)
   and remove any older than `60s` with the same predicate.
3. Errors during removal are logged + swallowed — boot must succeed
   even if a permissions oddity prevents cleanup; the next git op
   will surface the real error.

The 60s threshold is generous — a healthy `git commit` releases the
index lock in milliseconds, so any lock present at boot is by
definition stale (the previous daemon is gone).

### 2.6 Conflict / divergence handling

The push uses plain `git push origin main` (no `--force`,
no `--force-with-lease`). If the remote has diverged (which should
never happen — only the deploy key writes), git returns non-zero with
`! [rejected] main -> main (non-fast-forward)`.

On divergence the subsystem:

1. Logs a structured-log error with `event=vault_sync_diverged`,
   `local_sha=...`, `remote_sha=...` (fetched on the spot for the
   log).
2. Drives the §2.7 edge-trigger notify state machine (state ok→fail
   surfaces a Telegram message; subsequent fail→fail are silent).
3. Does NOT attempt rebase, merge, or force-push.
4. Returns failure to the caller.

### 2.7 Notify — edge-trigger state machine

**Devil w1 H-1 closure.** v0's flat 24h cooldown silenced legitimate
signal on a recovery (a 24h-stale "fail" notify followed by a recovery
the owner never sees). v1+v2 replaces it with an edge-trigger state
machine persisted at `<data_dir>/run/vault_sync_state` (single-line
JSON file with schema `{"last_state": "ok"|"fail",
"consecutive_failures": N, "last_invocation_at": "<iso8601>" | null}`;
the `last_invocation_at` field is added in v2 — W2-M2).

Transitions (driven only by `result` ∈ {"pushed", "noop", "failed"};
`result="lock_contention"` and `result="rate_limited"` do **NOT**
trigger a transition — see W2-C1):

- **ok → fail**: send notify immediately ("vault sync failed: …").
  Bump `consecutive_failures` to `1`.
- **fail → fail**: silent (no notify). Bump `consecutive_failures`.
  If new value is in `notify_milestone_failures`
  (default `(5, 10, 24)`), send a milestone notify ("vault sync still
  failing — N consecutive failures").
- **fail → ok** (recovery): send notify ("vault sync recovered after
  N consecutive failures"). Reset `consecutive_failures` to `0`,
  set `last_state` to `"ok"`.
- **ok → ok**: silent (the happy path).

State file is written atomically (tmp + rename); a corrupted state
file is recoverable by deleting it (next cycle treats `last_state` as
`"ok"`, `consecutive_failures` as `0`, and `last_invocation_at` as
`null`).

`notify_cooldown_s` from v0 is **dropped**. Replaced by
`notify_milestone_failures: tuple[int, ...]`.

**W2-M2 closure — rate-limit persistence across restart.** v1 stored
`_last_invocation_at` only in-memory; a daemon restart 5 seconds after
a `vault_push_now` invocation would reset the rate-limit window and
permit an immediate second push. v2 persists `last_invocation_at`
inside the same JSON state file alongside `last_state` and
`consecutive_failures`. On startup, the subsystem loads both and
honours the rate-limit across the restart boundary.

### 2.8 Module location — src/assistant/vault_sync/

New package `src/assistant/vault_sync/` contains:

- `__init__.py` — exports `VaultSyncSubsystem`, `VaultSyncSettings`.
- `subsystem.py` — `VaultSyncSubsystem` class with:
  - `loop()` — the asyncio loop (immediate-first-tick semantics —
    §2.1 — with belt-and-braces internal exception handling so the
    supervisor's respawn budget is reserved for actually-fatal
    issues).
  - `run_once(reason: str) -> RunResult` — the git pipeline,
    holding `_lock` (asyncio outer) for the whole pipeline and
    `vault_lock` (fcntl inner) only around `status/add/commit`.
    `RunResult.result` ∈ {"pushed", "noop", "rate_limited",
    "lock_contention", "failed"}.
  - `startup_check()` — validates `ssh_key_path` exists,
    `ssh_known_hosts_path` exists and contains a recognisable
    GitHub host-key fingerprint (refuses to start vault sync
    otherwise; H4 closure + W2-H5 host-key-mismatch handling).
  - `_validate_staged_paths()` — re-runs the secret denylist
    against staged paths just before `git commit` (H2 daemon-side
    defence-in-depth). Uses `re.search` with the regex set
    documented in §3 — see W2-H4.
  - `_lock: asyncio.Lock` — daemon-internal serialisation between
    cron loop and `vault_push_now` @tool (§2.4 OUTER lock).
  - `_state: VaultSyncState` — loaded from
    `<data_dir>/run/vault_sync_state` at boot, persists
    `last_state`, `consecutive_failures`, `last_invocation_at`.
- `git_ops.py` — thin async wrappers around `git status`,
  `git add`, `git commit`, `git push` returning typed result
  dataclasses; centralises the `GIT_SSH_COMMAND` env injection
  (per-subprocess, never daemon-wide).
- `notify.py` — Telegram failure-notify with edge-trigger state
  machine persisted at `<data_dir>/run/vault_sync_state`.
- `audit.py` — JSONL audit log writer at
  `<data_dir>/run/vault-sync-audit.jsonl`. Append-only, fields
  `{"ts": iso, "reason": "scheduled|manual|boot", "result":
  "pushed|noop|rate_limited|lock_contention|failed",
  "files_changed": N, "commit_sha": "..."}`. Rotation policy
  (W2-H2): before each append, the writer stats the file; if size
  exceeds `audit_log_max_size_mb` MB (default 10), it `os.rename`s
  the file to `<path>.1` (overwriting any prior `.1` — single-step
  rotation, no chain), then opens a fresh file at the original
  path. Rotation is atomic (single rename); no log lines from the
  pre-rotation file are lost. Rotation is NOT a full logrotate —
  it is intentionally minimal to avoid a logrotate dependency.

The subsystem is owned by `Daemon` (constructed once at boot, lives
for the daemon lifetime). The `vault_push_now` @tool body holds a
reference to the same instance via the @tool's closure (set up during
MCP server registration).

**Manual @tool path.** A new MCP @tool `vault_push_now` is registered
under a new MCP server group `mcp__vault__` (separate from the
existing `mcp__memory__` and `mcp__scheduler__` groups so its
allow/deny status is independently togglable via skill frontmatter).
The tool body:

1. Checks rate-limit: if `now - _state.last_invocation_at <
   manual_tool_min_interval_s` (default 60s), return
   `{"ok": false, "reason": "rate_limit", "next_eligible_in_s": N}`
   WITHOUT running git ops. Audit log records `result="rate_limited"`.
2. Acquires `_lock` (the same asyncio lock the cron loop uses; held
   for the entire pipeline including push — see §2.4).
3. Creates an asyncio task for `run_once(reason="manual")` and
   registers it in `Daemon._vault_sync_pending` (set, mirror of
   `_audio_persist_pending`). The task self-removes via
   `add_done_callback`.
4. Awaits the task.
5. Returns `{"ok": True, "files_changed": N, "commit_sha": "<sha>"}`
   on success, `{"ok": False, "reason": "<short error>"}` on
   failure. Failure also drives the §2.7 edge-trigger notify path.
6. Updates `_state.last_invocation_at = now` AT INVOCATION TIME, not
   completion time, so the rate-limit covers the operation duration.
   The state file is rewritten atomically on this update.

The @tool wiring is gated by `settings.vault_sync.manual_tool_enabled`
— if `False`, the tool is not registered with the SDK MCP server and
the model never sees it in its tool catalogue.

**W2-C3 closure — settings validator.** `manual_tool_enabled=True`
combined with `enabled=False` is a logically inconsistent
configuration: the @tool would be registered, the model would see it,
but the subsystem itself would not be constructed. v1 had no
validator; v2 adds an explicit `__post_init__` validator on
`VaultSyncSettings` that raises `ValueError("manual_tool_enabled
requires enabled=True")` at settings-load time. The daemon refuses to
start; the failure is logged with an actionable hint. See AC#22.

### 2.9 Daemon.stop drain for in-flight push (H-6)

`Daemon.__init__` adds:

```python
self._vault_sync_pending: set[asyncio.Task[Any]] = set()
```

(next to the existing `_audio_persist_pending` set). Each
`vault_push_now` invocation, and any in-flight scheduled-cycle push
that is past the `_lock.acquire()`, registers its task in this set
and self-removes via `add_done_callback`.

`Daemon.stop` drains `_vault_sync_pending` BEFORE cancelling
`_bg_tasks` (which would otherwise tear down the loop mid-push):

```python
# In Daemon.stop, BEFORE _bg_tasks cancel.
# F11 pattern (matches phase-6e _audio_persist_pending drain at
# main.py:919-934): use ``asyncio.wait`` so the timeout snapshot
# observes the real outstanding set rather than the post-cancel
# empty list ``asyncio.wait_for(asyncio.gather(...))`` would
# produce. Drain order deviates from the phase-6e precedent: vault
# sync subprocess push tasks aren't shielded inside ``finally`` like
# audio persist tasks were, so cancelling mid-flight orphans the
# SSH pipe and leaves ``.git/index.lock``. Hence the wait-then-cancel
# sequence here.
if self._vault_sync_pending:
    pending = list(self._vault_sync_pending)
    log.info("daemon_draining_vault_sync", count=len(pending))
    try:
        done, not_done = await asyncio.wait(
            pending,
            timeout=self._settings.vault_sync.drain_timeout_s,
            return_when=asyncio.ALL_COMPLETED,
        )
        if not_done:
            log.warning(
                "daemon_vault_sync_drain_timeout",
                outstanding=[t.get_name() for t in not_done],
            )
            for t in not_done:
                t.cancel()
            await asyncio.gather(
                *not_done, return_exceptions=True
            )
    except Exception as exc:
        log.warning(
            "daemon_vault_sync_drain_error", error=repr(exc)
        )
self._vault_sync_pending.clear()
# ... existing _bg_tasks drain follows ...
```

**W2-M3 closure — `drain_timeout_s >= push_timeout_s` invariant.** v1
defaulted `drain_timeout_s=45.0` while `push_timeout_s=60`; a slow but
otherwise healthy push could have its drain budget exhausted while the
push subprocess was still legitimately running, leading to a
forced-tear-down + retry next boot for an op that would have completed
in 50s. v2 raises the default `drain_timeout_s = 60.0` to match
`push_timeout_s` exactly, and a settings validator enforces
`drain_timeout_s >= push_timeout_s` (raises `ValueError` on violation
at load time). The invariant is documented inline so future tuning
respects the dependency.

On budget exhaustion, the `_bg_tasks` cancel path tears down forcibly;
the next boot's `_cleanup_stale_vault_locks` reaps any
`.git/index.lock` artefact.

**W2-M4 closure — RSS observer hook.** Phase-6e's `_rss_observer` at
`main.py:625-684` already correlates RSS with `bg_tasks`,
`audio_persist_pending`, and `sub_pending` set sizes. v2 extends the
`daemon_rss` log line with one additional field
`vault_sync_pending=len(self._vault_sync_pending)` so a stuck push
becomes visible in the RSS sample stream. This is a coder-deliverable
change to `main.py:_rss_observer`, listed here so the reviewer
catches its absence as a regression. See AC#25.

### 2.10 NO schema migration

**Devil w1 C-1 closure.** v0's `0005_schedules_kind.sql` migration is
**dropped**. `state/db.py` `SCHEMA_VERSION` stays at `4` (its current
value). No ALTER TABLE, no dispatcher branch, no `kind` column,
nothing.

This eliminates:

- Devil C-1 (SCHEMA_VERSION bump risk to existing rows).
- Devil C-3 (dispatcher branch on unknown `kind` value).
- The entire phase-5b regression surface (scheduler dispatch path
  is untouched).

If a future phase needs scheduled-tick semantics for non-prompt
operations, that phase can introduce the `kind` column with its own
migration; it will not be inheriting unrelated phase-8 risk.

## 3. Settings spec

New nested settings block `VaultSyncSettings`, mounted on the root
`Settings` as `settings.vault_sync`. Env prefix `VAULT_SYNC_`.

```python
class VaultSyncSettings(BaseSettings):
    enabled: bool = False
    repo_url: str | None = None
    ssh_key_path: Path | None = None  # default ~/.ssh/vault_deploy
    ssh_known_hosts_path: Path | None = None  # default ~/.ssh/known_hosts_vault
    branch: str = "main"
    cron_interval_s: float = 3600.0
    git_user_name: str = "0xone-assistant"
    git_user_email: str = "0xone-assistant@users.noreply.github.com"
    git_op_timeout_s: int = 30
    push_timeout_s: int = 60
    drain_timeout_s: float = 60.0  # W2-M3 — must be >= push_timeout_s
    vault_lock_acquire_timeout_s: float = 30.0  # W2-C1 (separate from git_op_timeout_s)
    audit_log_max_size_mb: int = 10  # W2-H2
    manual_tool_enabled: bool = True
    manual_tool_min_interval_s: float = 60.0
    notify_milestone_failures: tuple[int, ...] = (5, 10, 24)
    secret_denylist_regex: tuple[str, ...] = (
        r"^secrets/",
        r"^\.aws/",
        r"^\.config/0xone-assistant/",
        r"\.env$",
        r"\.key$",
        r"\.pem$",
    )
    commit_message_template: str = (
        "vault sync {timestamp} ({reason}) — {files_changed} files: {filenames}"
    )

    @model_validator(mode="after")
    def _validate_vault_sync_consistency(
        self,
    ) -> "VaultSyncSettings":
        """Pydantic v2 cross-field validator. Mirrors the
        ``_validate_whisper_pair`` precedent in ``config.py:293-310``.
        """
        # W2-C3: manual_tool requires the subsystem itself enabled.
        if self.manual_tool_enabled and not self.enabled:
            raise ValueError(
                "manual_tool_enabled requires enabled=True; "
                "set VAULT_SYNC_MANUAL_TOOL_ENABLED=false or "
                "VAULT_SYNC_ENABLED=true"
            )
        # W2-M3: short drain = guaranteed push cancellation.
        if self.drain_timeout_s < self.push_timeout_s:
            raise ValueError(
                "drain_timeout_s must be >= push_timeout_s "
                f"(got {self.drain_timeout_s} < {self.push_timeout_s})"
            )
        # repo_url required + regex check when enabled=True.
        if self.enabled and self.repo_url is None:
            raise ValueError(
                "repo_url required when enabled=True; set "
                "VAULT_SYNC_REPO_URL=git@github.com:<owner>/<repo>.git"
            )
        if self.repo_url is not None:
            import re

            if not re.match(
                r"^git@[a-z0-9.-]+:[\w.-]+/[\w.-]+\.git$",
                self.repo_url,
            ):
                raise ValueError(
                    f"repo_url must match SSH form "
                    f"git@host:owner/repo.git (got {self.repo_url!r})"
                )
        return self
```

| Field | Default | Purpose |
|---|---|---|
| `enabled` | `False` | Master switch. Daemon skips all vault-sync wiring (loop spawn, @tool registration, startup_check, lock cleanup) when `False`. Defaults `False` so a fresh checkout does not try to push to a non-existent repo. |
| `repo_url` | `None` | SSH URL of the dedicated private vault repo. Required if `enabled=True`. Production value: `git@github.com:c0manch3/0xone-vault.git`. Validator regex: `^git@[a-z0-9.-]+:[\w.-]+/[\w.-]+\.git$` (L-2 — allows self-hosted forge in future). |
| `ssh_key_path` | `~/.ssh/vault_deploy` | Deploy key file. Existence checked at `startup_check`. |
| `ssh_known_hosts_path` | `~/.ssh/known_hosts_vault` | Static-pinned known_hosts file. Must exist AND contain a GitHub-recognisable host-key fingerprint at `startup_check` (H4 + W2-H5). |
| `branch` | `"main"` | Branch to push. |
| `cron_interval_s` | `3600.0` | Sleep duration between ticks of the asyncio loop. Replaces v0's `cron: str = "0 * * * *"`. Float so tests can set sub-second values. Interval-timer (NOT cron-walltime) — see §2.1. |
| `git_user_name` | `"0xone-assistant"` | `user.name` for vault commits. |
| `git_user_email` | `"0xone-assistant@users.noreply.github.com"` | `user.email` for vault commits. W2-L2: GH `users.noreply` form avoids the rare "private email blocked" failure mode where GH rejects commits from synthetic local domains for accounts with strict privacy settings. |
| `git_op_timeout_s` | `30` | Per-subprocess timeout for non-push git ops (`status`, `add`, `commit`). Distinct from `vault_lock_acquire_timeout_s` (W2-C1). |
| `push_timeout_s` | `60` | Per-subprocess timeout for `git push`. |
| `drain_timeout_s` | `60.0` | `Daemon.stop` budget for in-flight push drain (§2.9). W2-M3: validator enforces `>= push_timeout_s`. |
| `vault_lock_acquire_timeout_s` | `30.0` | W2-C1 — timeout for acquiring the inner fcntl `vault_lock`. On `TimeoutError` the cycle returns `result="lock_contention"` (audit log only; does NOT increment `consecutive_failures`, does NOT drive edge-trigger notify). Distinct from `git_op_timeout_s` because lock contention is an *internal* timing phenomenon, not a remote failure. |
| `audit_log_max_size_mb` | `10` | W2-H2 — when the audit log exceeds this size, the next append rotates the file to `<path>.1` (single-step `os.rename`, no chain). Keeps the JSONL log bounded over a multi-year deploy without depending on `logrotate`. |
| `manual_tool_enabled` | `True` | Gates `vault_push_now` @tool registration. W2-C3: validator rejects `True` when `enabled=False`. |
| `manual_tool_min_interval_s` | `60.0` | Min seconds between consecutive `vault_push_now` invocations (C-4). 2nd call within 60s returns `rate_limited` with audit log row. Rate-limit window survives daemon restart (W2-M2 — `_state.last_invocation_at` persisted in `<data_dir>/run/vault_sync_state`). |
| `notify_milestone_failures` | `(5, 10, 24)` | Edge-trigger milestone notify thresholds (H-1). Replaces v0's `notify_cooldown_s`. |
| `secret_denylist_regex` | see code | W2-H4 — explicitly anchored regex patterns matched via `re.search` against vault-relative paths (output of `git diff --cached --name-only`). Replaces v1's `secret_denylist_globs` (which had ambiguous anchoring against `fnmatch`). The bootstrap script `vault-bootstrap.sh` MUST use the SAME regex set — single source of truth lives in this settings field; the script duplicates the regex with a comment "MUST stay in sync with VaultSyncSettings.secret_denylist_regex". `_validate_staged_paths` and the bootstrap pre-push check share the patterns verbatim. AC#19 verifies parity. |
| `commit_message_template` | see code | f-string-style template, M-2. Available keys: `timestamp` (ISO-8601 UTC), `reason` (`"scheduled"`/`"manual"`/`"boot"`), `files_changed` (int), `filenames` (first 3 staged paths comma-joined, truncated). W2-L3 — filenames passed into the template MUST be sanitised: newlines and ASCII control chars (`\x00-\x1f`, `\x7f`) are stripped before substitution so a hostile filename cannot break the commit-message format or smuggle a forged trailer. |

Validation: when `enabled=True`, settings construction asserts
`repo_url` non-empty and matches the regex. SSH key and known_hosts
existence checks are deferred to `VaultSyncSubsystem.startup_check`
so `pytest` config validation does not require keys on the dev box.
The `__post_init__` validators above ALWAYS run (whether or not
`enabled=True`) because their checks are about config self-
consistency, not host filesystem state.

## 4. VPS bootstrap (one-time, owner does this)

Owner runs the bootstrap script `deploy/scripts/vault-bootstrap.sh`
once on the VPS — the daemon does NOT self-bootstrap the SSH key or
repo (deliberate: cred handling is owner work, not bot work). The
script is **idempotent** — re-running it skips already-completed
steps.

**New deliverables in the repo:**

- `deploy/scripts/vault-bootstrap.sh` — idempotent shell script
  running the steps below.
- `deploy/known_hosts_vault.pinned` — checked-in file containing
  GitHub's current ed25519 + ecdsa + rsa host keys (all three
  returned by `gh api meta | jq -r '.ssh_keys[]'`). Verify against
  `https://api.github.com/meta` before each release that touches this
  file. `vault-bootstrap.sh` copies this file into
  `~/.ssh/known_hosts_vault` rather than running `ssh-keyscan` (H-4
  closure: no TOFU).
- `docs/ops/vault-secret-leak-recovery.md` — boilerplate runbook for
  the case where a secret gets committed despite both layers of
  defence (force-push to overwrite history, rotate the leaked
  credential, revoke deploy key + reissue, audit the audit log).
- `docs/ops/vault-host-key-rotation.md` — W2-H5. Boilerplate runbook
  for the rare event where GitHub rotates SSH host keys. Outlines
  the procedure: owner runs `gh api meta | jq -r '.ssh_keys[]' >
  deploy/known_hosts_vault.pinned`, commits, redeploys. While the
  pinned file is stale, `startup_check` fails fast, the daemon logs
  `vault_sync_host_key_mismatch`, force-disables vault sync for the
  process lifetime, and continues serving phase-1..6e traffic.
  AC#26.
- `deploy/docker/docker-compose.yml` patch — W2-M5. The bot
  container needs to read `~/.ssh/vault_deploy` and
  `~/.ssh/known_hosts_vault` from the host. The compose file gains
  two read-only bind-mounts:
  ```yaml
  volumes:
    # ...existing mounts...
    - ${HOME}/.ssh/vault_deploy:/home/bot/.ssh/vault_deploy:ro
    - ${HOME}/.ssh/known_hosts_vault:/home/bot/.ssh/known_hosts_vault:ro
  ```
  (Container path is illustrative; coder picks the exact in-container
  location and updates `VAULT_SYNC_SSH_KEY_PATH` /
  `VAULT_SYNC_SSH_KNOWN_HOSTS_PATH` env vars accordingly.) Without
  the bind-mount, the container has no path to authenticate with
  GitHub. Restart procedure (step 9 below) explicitly verifies the
  mount via `docker exec 0xone-assistant ls -l /home/bot/.ssh/`.

**Steps run by `vault-bootstrap.sh`:**

1. Generate deploy key (idempotent — skip if file exists):
   ```sh
   if [[ ! -f ~/.ssh/vault_deploy ]]; then
     ssh-keygen -t ed25519 -f ~/.ssh/vault_deploy -N "" \
       -C "0xone-vault deploy key (VPS 193.233.87.118)"
   fi
   ```

2. Copy pinned known_hosts:
   ```sh
   cp /opt/0xone-assistant/deploy/known_hosts_vault.pinned \
      ~/.ssh/known_hosts_vault
   chmod 600 ~/.ssh/known_hosts_vault
   ```
   (NOT `ssh-keyscan` — accept-new TOFU is the H-4 vulnerability v0
   shipped.)

3. Print deploy key fingerprint and pause for owner to:
   - Create the GH repo `c0manch3/0xone-vault` (Private,
     initialise with empty README so `main` exists).
   - Add `~/.ssh/vault_deploy.pub` as a deploy key with **write**
     access at `https://github.com/c0manch3/0xone-vault/settings/keys/new`.
   Owner presses Enter to continue.

4. Initialise the vault dir as a git repo (idempotent — only if
   `<data_dir>/vault/.git/` does not exist):
   ```sh
   if [[ ! -d ~/.local/share/0xone-assistant/vault/.git ]]; then
     cd ~/.local/share/0xone-assistant/vault
     git init -b main
     git config user.name  "0xone-assistant"
     git config user.email "0xone-assistant@users.noreply.github.com"
     git remote add origin git@github.com:c0manch3/0xone-vault.git
     # Write the §2.2 .gitignore from a heredoc.
     cat > .gitignore <<'EOF'
     ... (see §2.2)
     EOF
     git add .gitignore
   fi
   ```

5. Pre-push secret-leak validation (refuses to commit/push if any
   denylist match). Used both as a one-off check during bootstrap
   AND mirrored in daemon-side `_validate_staged_paths` (defence in
   depth). W2-H4: the regex set MUST stay in sync with
   `VaultSyncSettings.secret_denylist_regex`; the script preserves a
   verbatim copy with an inline comment marking the dependency.
   ```sh
   # MUST match VaultSyncSettings.secret_denylist_regex (§3).
   DENY_RE='^secrets/|^\.aws/|^\.config/0xone-assistant/|\.env$|\.key$|\.pem$'
   STAGED=$(git diff --cached --name-only)
   if echo "$STAGED" | grep -E "$DENY_RE" > /dev/null; then
     echo "ERROR: staged files match secret denylist:"
     echo "$STAGED" | grep -E "$DENY_RE"
     exit 1
   fi
   ```

6. Initial commit + push (with `GIT_SSH_COMMAND` scoped to the new
   key + pinned known_hosts):
   ```sh
   GIT_SSH_COMMAND="ssh -i ~/.ssh/vault_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=~/.ssh/known_hosts_vault" \
     git commit -m "initial vault commit" && \
     git push -u origin main
   ```
   **W2-L1 — pre-existing populated vault.** If the vault dir
   already contains markdown notes from earlier phases (typical: any
   deploy that has been running phase-4 memory tools), the
   bootstrap commit at this step will include ONLY `.gitignore`
   (the existing notes are not staged because `git add` only ran on
   `.gitignore` in step 4). The previously-existing notes will be
   committed by the FIRST cron tick of `_vault_sync_loop` after the
   daemon restart in step 9 (covered by AC#1). If the owner wants
   the existing notes pushed immediately rather than waiting for the
   first tick, they can manually invoke `vault_push_now` after the
   restart — this is documented in
   `docs/ops/vault-secret-leak-recovery.md` as a side note.

7. Configure `safe.directory` for the VPS host user (Docker UID
   mismatch — L-4 closure):
   ```sh
   git config --global --add safe.directory \
     ~/.local/share/0xone-assistant/vault
   ```

8. Print env-file additions for owner to apply manually to
   `~/.config/0xone-assistant/.env`:
   ```
   VAULT_SYNC_ENABLED=true
   VAULT_SYNC_REPO_URL=git@github.com:c0manch3/0xone-vault.git
   ```

9. Restart the daemon. Procedure:
   - Verify the docker-compose.yml has the W2-M5 SSH bind-mounts:
     `grep -A 2 'vault_deploy' deploy/docker/docker-compose.yml`.
   - `docker compose restart`.
   - Verify the keys reach inside the container:
     `docker exec 0xone-assistant ls -l /home/bot/.ssh/vault_deploy
     /home/bot/.ssh/known_hosts_vault`. Both files must exist with
     `-r--------` permissions.
   - On boot the daemon then runs:
     - `_cleanup_stale_vault_locks` first.
     - `VaultSyncSubsystem.startup_check` validates the pinned
       known_hosts + key file. Refuses to start the loop on any
       missing/malformed input — daemon logs error, force-disables
       `enabled` for the rest of the process lifetime, and continues
       with all other phases functional (AC#3, AC#17, AC#26).
     - `_spawn_bg_supervised(self._vault_sync.loop, ...)` (W2-M1).
     - First tick fires immediately (W2-H1): owner sees a
       "scheduled" audit row and (if there are notes to commit)
       a fresh GitHub commit within ~60s of the restart, not 1h.
     - `vault_push_now` @tool registers with the SDK MCP server.

If `VAULT_SYNC_ENABLED=false` (default), none of the above wiring
runs and the daemon behaves identically to phase-5d/6/7.

## 5. Acceptance criteria

**AC#1 — happy-path push (cron tick).** With `enabled=True`, deploy
key + pinned known_hosts + initialised vault repo all in place, a
fresh `memory_write` from a model turn produces a new markdown file
under `<data_dir>/vault/`. Within `cron_interval_s` seconds the loop
fires `run_once(reason="scheduled")`; a new commit appears at
`https://github.com/c0manch3/0xone-vault` containing exactly that file.
Audit log row appended.

**AC#2 — empty diff no-op.** A loop tick where vault has zero
changes since the last push exits silently: `git status --porcelain`
empty → no `git add`, no commit, no push. Debug log only
(`event=vault_sync_no_changes`). No Telegram message. Audit log row
with `result="noop"`.

**AC#3 — SSH key missing → daemon logs + force-disable + continues.**
With `enabled=True` but `~/.ssh/vault_deploy` absent,
`startup_check` logs an error, sets the in-process flag to skip the
loop, and the daemon continues serving phase-1..6e traffic
unaffected. No loop task spawned. No exception escapes to crash the
daemon.

**AC#4 — push failure → edge-trigger notify.** When the remote is
unreachable (DNS/network) or rejects the push (divergence), the §2.7
state machine sends ONE Telegram notify on the ok→fail edge. The next
two consecutive failures are silent. At consecutive-failure count = 5
(first milestone in `notify_milestone_failures`), a milestone notify
fires regardless of edge-trigger pattern. **NOT** a flat 24h cooldown.

**AC#5 — `enabled=False` default → no observable change vs phase 6e.**
With `VAULT_SYNC_ENABLED=false` (W2-M6 reword): no loop task spawned,
no `vault_push_now` MCP @tool registered with the SDK, no
`_cleanup_stale_vault_locks` invocation, no `vault_sync_pending` set
written, no `<data_dir>/run/vault-sync-audit.jsonl` created, zero
Telegram notifies. The phase-6e RSS observer's `daemon_rss` log line
does NOT include the `vault_sync_pending` field because the subsystem
attribute is `None`. The `vault_sync` Python module may be imported
(it is part of the package), but no objects from it are constructed
and no asyncio task spawned. The contract is "no externally-
observable behavioural change" — not a literal byte-for-byte process
diff.

**AC#6 — concurrency / two-tier locking serialises cron + manual end-
to-end.** Two concurrent invocations (cron tick firing at the same
instant the model invokes `vault_push_now`) serialise on `_lock`
(asyncio outer) such that exactly one `git push origin main`
subprocess runs at a time. Inside each pipeline, `vault_lock` (fcntl
inner) wraps only `status/add/commit` and is RELEASED before push so
a parallel `memory_write` task that produces a `.tmp/.tmp-XXX.md`
file can resume during the network leg. Test verifies via
`asyncio.gather` on two `run_once` calls + a subprocess-mock that
asserts at most one `git push` is in-flight at any time + a parallel
`memory_write` task whose `os.replace` finalisation is allowed during
the push window of the OTHER pipeline.

**AC#7 — manual `vault_push_now` MCP @tool fires within 60s
rate-limit.** With `manual_tool_enabled=True`, owner says "запушь
вольт" → model invokes the tool → tool acquires `_lock` → registers
task in `_vault_sync_pending` → runs the same git pipeline as cron →
returns `{"ok": true, "files_changed": N, "commit_sha": "<sha>"}` to
the model.

**AC#8 — phase 1..6e regression-free with enabled=False AND with
enabled=True.** Concrete enumeration:
- `/ping` — phase 2 echo skill.
- `memory_write`, `memory_search`, `memory_list`, `memory_get`,
  `memory_delete`, `memory_seed` — phase 4 MCP @tools.
- `marketplace_install`, `skill_activate`, `skill_uninstall` —
  phase 3 skill-installer @tools.
- `schedule_add`, `schedule_list`, `schedule_remove`, `schedule_run` —
  phase 5b scheduler @tools.
- File ingestion: PDF, DOCX, TXT, MD, XLSX (phase 6a).
- Photo / multimodal vision (phase 6b).
- Voice / audio / URL transcription (phase 6c).
- Subagent spawn via Task tool (phase 6).
- Audio bg dispatch parallel concurrency (phase 6e).
ALL must pass on the VPS smoke test with both `enabled=False`
(baseline) and `enabled=True` (vault sync running).

**AC#9 — Daemon.stop drain for in-flight push within 60s budget;
hard-kill leaves clean state for next boot.** Manual `vault_push_now`
invoked at T-5s (slow push in progress). `Daemon.stop` called at T+0.
Push completes within the 60s budget AND the daemon shuts down
cleanly OR the budget exhausts and `_bg_tasks` cancel forces shutdown.
On next boot, `_cleanup_stale_vault_locks` reaps any
`.git/index.lock` artefact and the next loop tick succeeds. Verified
in container with `docker compose stop` mid-push. Budget default
chosen so `drain_timeout_s == push_timeout_s` (W2-M3 invariant) — a
push that hits its own timeout must always also hit the drain
timeout, never the other way round.

**AC#10 — `.gitignore` enforcement + secret denylist
defence-in-depth.** A test that places `secrets/api.env` in the
vault dir and runs `run_once`:
- `.gitignore` excludes `secrets/` from `git add -A` → the file is
  never staged.
- (Defence in depth) Even if forced staged via `git add -f
  secrets/api.env`, `_validate_staged_paths` rejects the commit with
  `event=vault_sync_denylist_block` + Telegram notify, sets
  `consecutive_failures += 1`, and audit log records
  `result="failed"`.
- The matching uses `re.search` against the regex set in
  `secret_denylist_regex` (W2-H4). Both daemon and bootstrap reject
  the same set of paths.
- Boot-time supervised loop spawn uses `_spawn_bg_supervised` so a
  pathological `_validate_staged_paths` exception (e.g. malformed
  regex) crashes the loop, the supervisor respawns up to 3x/h, then
  notifies the owner — the daemon as a whole keeps running (W2-M1).

**AC#11 — stale `.git/index.lock` cleanup at boot.** Plant
`<vault>/.git/index.lock` with mtime 5 minutes old before daemon
start. `_cleanup_stale_vault_locks` removes it during start. Vault
sync proceeds without manual intervention. Verified via filesystem
assertion + log line `event=vault_sync_stale_index_lock_cleared`.

**AC#12 — vault_lock × memory_write atomic-rename serialisation.**
Two concurrent tasks: `memory_write` (creating
`<vault>/.tmp/.tmp-XXX.md`) and `vault_push_now` (running `git add
-A`). They serialise via `vault_lock`. Any commit produced contains
ONLY the renamed-final markdown, NEVER the `.tmp-XXX.md` artefact.
Verified via pytest with deterministic timing (release one
`vault_lock` while the other is mid-`atomic_write`).

**AC#13 — `vault_push_now` rate-limit, restart-resilient.** First
call at T+0 returns `{"ok": true, ...}`. Second call at T+30s returns
`{"ok": false, "reason": "rate_limit", "next_eligible_in_s": 30}`
WITHOUT running git ops. Third call at T+61s succeeds. **W2-M2
extension**: a daemon restart at T+10s does NOT reset the rate-limit
window — the next call at T+30s post-restart still returns
`rate_limit`. The persisted `last_invocation_at` field in the state
file at `<data_dir>/run/vault_sync_state` is what enforces this.

**AC#14 — `vault_push_now` audit log writes JSONL row per
invocation, with rotation.** `<data_dir>/run/vault-sync-audit.jsonl`
contains one line per invocation (success, no-op, rate-limited,
failed, lock_contention) with the documented field set. Test parses
the JSONL and asserts row counts + field shape. **W2-H2 extension**:
when the file size exceeds `audit_log_max_size_mb` (default 10) MB,
the next append rotates the file via `os.rename` to `<path>.1` (any
prior `.1` is overwritten — single-step rotation, no chain). No log
lines from the pre-rotation file are lost; the rename is atomic
relative to the new-file open. Test plants a 10.5 MB file, asserts a
single rename, and that the new file starts at 0 bytes.

**AC#15 — prompt-injection regression: synthetic adversarial
transcript.** A test injects (via SDK mock) a transcript that prompts
the model to "call vault_push_now in a loop". The rate-limit
prevents `N>1` invocations of the underlying git pipeline within the
60s window — the audit log contains exactly one
`result="pushed"` row and zero or more `result="rate_limited"` rows
in any sequence the model attempts.

**AC#16 — `GIT_SSH_COMMAND` env scope.** During an in-flight vault
sync push, an unrelated subprocess (e.g. an `env`-printing helper
spawned via `asyncio.create_subprocess_exec` from a memory tool path
or test fixture) does NOT see `GIT_SSH_COMMAND` in its env. Verified
via `test_git_ssh_command_scope.py`. The daemon's process-wide
`os.environ` is also inspected and asserted to NOT contain
`GIT_SSH_COMMAND` at any moment during or after the push.

**AC#17 — host-key pin: with `known_hosts_vault.pinned` removed,
daemon refuses to start vault_sync.** Bootstrap with the pinned file
absent → `startup_check` logs error + force-disables vault sync;
daemon continues with all other phases functional; no loop task
spawned. Owner sees the error in `journalctl -u 0xone-assistant` (or
docker logs).

**AC#18 — skill markdown discoverability: `skills/vault/SKILL.md`
loads + appears in skills catalogue + Cyrillic trigger phrase test.**
Model (in dry-run) sees the skill catalogue listing `vault` with
trigger phrases including "запушь вольт", "сделай бэкап заметок",
"синхронизируй vault", "push vault now". Skill frontmatter declares
`allowed-tools: ["mcp__vault__vault_push_now"]` (documentation; per-
skill enforcement is phase 4 carry-forward — `bridge/skills.py:64`
warns `skill_lockdown_not_enforced` because the global baseline is
the only gate today). The body includes explicit anti-pattern
guidance: "do NOT call `vault_push_now` as side-effect of
`memory_write` or other tool chains; this tool is for explicit owner
request only".

**AC#19 — bootstrap pre-push secret-leak check uses identical regex
semantics as daemon-side.** Run `vault-bootstrap.sh` with
`secrets/dummy.env` planted in the vault dir → script's pre-push
validation rejects the commit with explicit error message and
non-zero exit. **W2-H4 extension**: the test parametrises across the
full set of denylist-matching paths (`secrets/foo`, `.aws/creds`,
`.config/0xone-assistant/x`, `bar.env`, `bar.key`, `bar.pem`) and
asserts that the daemon-side `_validate_staged_paths` rejects EXACTLY
the same set (no path rejected by one and accepted by the other).

**AC#20 — fail → ok recovery notify.** After 3 consecutive failed
pushes (network outage), one successful push → owner gets a single
"vault sync recovered after 3 consecutive failures" Telegram message.
State file resets to `{"last_state": "ok",
"consecutive_failures": 0, "last_invocation_at": ...}`.

**AC#21 — milestone notify at N=5.** Sustained outage → at the 5th
consecutive failure, owner gets a milestone Telegram notify ("vault
sync still failing — 5 consecutive failures") regardless of the
silent `fail→fail` rule. The next milestone fires at 10, then 24.

**AC#22 — settings validator: `manual_tool_enabled=True` +
`enabled=False` rejected at load.** Constructing
`VaultSyncSettings(enabled=False, manual_tool_enabled=True)` raises
`ValueError("manual_tool_enabled requires enabled=True")` (or the
pydantic validation equivalent). The daemon refuses to start with
this config; the error is logged with an actionable hint pointing
to the two env vars. Mirror test for the `drain_timeout_s <
push_timeout_s` validator (W2-M3): construction with
`drain_timeout_s=30, push_timeout_s=60` raises `ValueError`.

**AC#23 — `lock_contention` does NOT trip the edge-trigger state
machine.** Test scenario: a long-running `memory_write` holds
`vault_lock` for 35s (longer than `vault_lock_acquire_timeout_s=30`).
A scheduled cron tick during that window sees its `vault_lock`
acquisition raise `TimeoutError`. The cycle:
- writes ONE audit row with `result="lock_contention"`.
- emits structured log `event=vault_sync_lock_contention`.
- does NOT increment `consecutive_failures`.
- does NOT cause `last_state` to flip `ok → fail`.
- does NOT send any Telegram message.
The next tick (after `memory_write` releases the lock) succeeds
normally with `result="pushed"` and the state stays `ok`.

**AC#24 — first tick fires within ~60s of daemon start.** Deploy
with `enabled=True` at T+0. The first scheduled `run_once` invocation
completes its audit-log write by T+60s (covers daemon startup
overhead, supervised-task spawn, lock cleanup, `startup_check`, and
the actual git pipeline on a small vault). The behaviour where the
loop sleeps `cron_interval_s` BEFORE the first tick (v1's pattern)
is explicitly rejected — verified by inspecting the audit log
timestamp of the first scheduled row vs the daemon-start log line.

**AC#25 — RSS observer logs `vault_sync_pending`.** With
`enabled=True` and at least one `vault_push_now` task in flight, a
`daemon_rss` log line in the next 60s window contains the
`vault_sync_pending=N` field where N is the current size of
`Daemon._vault_sync_pending`. With `enabled=False`, the field is
absent (or `0`, depending on coder's choice — spec accepts either,
provided `enabled=False` parity is preserved per AC#5).

**AC#26 — host-key mismatch: graceful degradation.** With the
pinned known_hosts file present but synthesised with a wrong
fingerprint (e.g. corrupted byte, simulating a host-key rotation
that owner has not picked up yet), `startup_check` fails fast,
emits structured log `event=vault_sync_host_key_mismatch`,
force-disables vault_sync for the process lifetime, and the daemon
continues serving phase-1..6e traffic. Owner is expected to follow
`docs/ops/vault-host-key-rotation.md` to refresh the pinned file
and redeploy. No loop task spawned, no `vault_push_now` registered,
no Telegram crash notify (the daemon as a whole did not crash).

## 6. Carry-forwards — explicitly OUT of scope

The following are deferred to later phases and MUST NOT be
implemented in phase 8. Each was considered and explicitly cut to
keep phase 8 bounded:

1. **Pull-back / two-way sync.** GitHub is a read-only mirror. A
   future phase can add a sync-down mechanism if owner ever wants
   to edit vault on GitHub web UI; not now.
2. **`gh` CLI integration.** No `gh issue`, `gh pr`, `gh repo`,
   `gh api` — none of it. Phase 8 is pure git-over-SSH. A separate
   later phase can add a `gh`-based subsystem for issue / PR
   workflows; that phase will live entirely outside
   `src/assistant/vault_sync/`.
3. **GitHub App OAuth.** Deploy key only. A future phase may
   replace the deploy key with a GitHub App token if scoped tokens
   are required.
4. **Per-file commit messages.** All changes since the last push
   collapse into one commit with the §3 templated message
   (timestamp + reason + filename head). Per-note commit attribution
   would require model cooperation on every `memory_write`; not worth
   the complexity.
5. **Encrypted vault (git-crypt / age).** Vault content is plain
   markdown on a private repo. Encryption is a separate axis of
   protection that can be layered on later without changing the
   sync pipeline.
6. **Multiple vault remotes.** The `repo_url` field is a single
   string. Mirror to a second remote (e.g. self-hosted Gitea) is a
   future phase if ever needed.
7. **Auto-rebase on divergence.** Fail-fast is the policy.
   Auto-rebase risks silent history rewrites. Owner manually
   resolves on the rare occasion this happens.
8. **Conflict UI in Telegram.** When divergence happens, the
   Telegram notify is plaintext ("vault sync failed: remote
   diverged. Manual recovery needed."). No inline-keyboard
   "force push?" buttons. Manual SSH session is the recovery path.
9. **Per-skill `allowed-tools` enforcement on `vault_push_now`.**
   Phase 8 ships the @tool unconditionally available to the
   model when `manual_tool_enabled=True`. The `skills/vault/SKILL.md`
   shipped here uses the per-skill allowed-tools mechanism for
   discoverability, but the @tool is not gated by skill activation
   — any active skill can call it (subject to model judgement and
   the rate-limit). A later phase that introduces strict per-skill
   MCP tool gating can scope this tool to specific skills; not now.
10. **Squash-history every N days.** Long-term commit log grows
    unbounded. A future maintenance phase may run periodic
    `git filter-repo` or `git rebase --root --squash` operations.
    Out of scope here.
11. **Webhook receiver.** No webhook listener for GitHub events
    (push notifications, issue comments). Phase 8 is one-way
    push-only.
12. **Bidirectional sync.** Same as #1 above; called out
    separately because a "minimal" two-way sync (cron-poll
    `git fetch + merge` from main) is sometimes mis-classified as
    "still push-only". It is not. Out of scope.
13. **Suspend-aware tick catch-up.** §2.1 explicitly chose interval-
    timer semantics over cron-walltime semantics. After a long
    container freeze / VM suspend, exactly one tick fires on wake
    rather than catching up missed wall-clock hours. A future phase
    could add walltime semantics if the missed-hour count ever
    becomes operationally interesting; for the "at least once per
    hour while running" contract it is not.

## 7. Devil w1 closure table

Citations reference the v0 spec line numbers in the previous file
revision. v1 fixes are §-anchored to this document.

### CRITICAL

| Devil ID | v0 risk | v0 line | v1 fix |
|---|---|---|---|
| C-1 | SCHEMA_VERSION bump (v0 implied 3→4 via 0005 migration; reality: already 4) | v0 §2.8 L201–204 | §2.10 — NO migration. `SCHEMA_VERSION` stays `4`. Risk void by simple-loop architecture. |
| C-2 | `vault_lock` × atomic-rename race (`memory_write` `.tmp/.tmp-*.md` could land in commit) | v0 §2.4 L113–124 | §2.4 — `VaultSyncSubsystem.run_once` acquires `_memory_core.vault_lock(<index_db_path>.lock, blocking=True, timeout=30)` AROUND `git status/add/commit`; releases before push. `.gitignore` lists `.tmp/`, `*.lock`, `memory-index.db*`. AC#12. |
| C-3 | Unknown `kind` dispatch (dispatcher silently no-ops on unknown values) | v0 §2.8 L211–222 | §2.10 — no `kind` column, no dispatcher branch. Risk void by simple-loop architecture. |
| C-4 | `vault_push_now` prompt-injection amplifier (model spam-loops the tool) | v0 §2.7 L181–190 | §2.8 — `manual_tool_min_interval_s=60.0` rate-limit + JSONL audit log at `<data_dir>/run/vault-sync-audit.jsonl`. `skills/vault/SKILL.md` includes anti-pattern directive. AC#13, AC#14, AC#15. |
| C-5 | Stale `.git/index.lock` at boot (hard-killed daemon → next sync hangs) | not in v0 | §2.5 — `_cleanup_stale_vault_locks` runs in `Daemon.start()` BEFORE loop spawn; mirrors `_boot_sweep_uploads`. AC#11. |

### HIGH

| Devil ID | v0 risk | v0 line | v1 fix |
|---|---|---|---|
| H-1 | Flat 24h cooldown silences signal | v0 §2.7 L167–169, §3 L245 | §2.7 — edge-trigger state machine (ok→fail / fail→ok / milestone N=5/10/24). `notify_cooldown_s` dropped; replaced by `notify_milestone_failures`. AC#4, AC#20, AC#21. |
| H-2 | Bootstrap secret-leak (no validation of staged files) | v0 §4 L254–299 | §4 — `vault-bootstrap.sh` includes pre-push denylist check. Daemon-side `VaultSyncSubsystem._validate_staged_paths()` runs the same check before `git commit`. `secret_denylist_globs` setting documents the denylist. `docs/ops/vault-secret-leak-recovery.md` placeholder. AC#10, AC#19. |
| H-3 | `GIT_SSH_COMMAND` env scope (potential leak via `os.environ.update`) | v0 §2.3 L103–107 | §2.3 — `env=` parameter on `asyncio.create_subprocess_exec`, NEVER `os.environ.update`. AC#16. |
| H-4 | `accept-new` host-key TOFU on first push | v0 §2.3 L106, §4 L280–282 | §4 — static-pinned `deploy/known_hosts_vault.pinned` checked into repo (verifiable via `https://api.github.com/meta`). Bootstrap copies, never `ssh-keyscan`. `StrictHostKeyChecking=yes`. `startup_check` validates pinned file exists + contains GitHub fingerprint. AC#17. |
| H-5 | Missing `skills/vault/SKILL.md` (model has no discoverability hook) | not in v0 | §2.8 + §6 — `skills/vault/SKILL.md` mandatory deliverable with Cyrillic trigger phrases, `allowed-tools` frontmatter, anti-pattern directive in body. AC#18. |
| H-6 | `Daemon.stop` drain for in-flight push (hard-kill mid-push leaves dirty state) | not in v0 | §2.9 — `_vault_sync_pending: set[asyncio.Task]` mirrors `_audio_persist_pending`. `Daemon.stop` drains BEFORE `_bg_tasks` cancel with `drain_timeout_s` budget. AC#9. |

### MEDIUM (key items)

| Devil ID | v0 risk | v0 line | v1 fix |
|---|---|---|---|
| M-2 | Generic commit message lacks forensic value | v0 §3 L244 | §3 `commit_message_template` defaults to `"vault sync {timestamp} ({reason}) — {files_changed} files: {filenames}"` with first 3 staged paths comma-joined and truncated. |
| M-3 | Bootstrap is 7 manual steps (footgun-prone) | v0 §4 L254–315 | §4 — `deploy/scripts/vault-bootstrap.sh` script wraps the steps idempotently. Owner still runs once. |
| M-4 | Coarse "all phase ACs pass" regression list | v0 AC#10 L364–368 | §5 AC#8 — concrete enumeration of phase 1..6e ACs. |

### LOW (key items)

| Devil ID | v0 risk | v0 line | v1 fix |
|---|---|---|---|
| L-2 | `repo_url` validator hardcoded to `git@github.com:` | v0 §3 L249–251 | §3 validator regex `^git@[a-z0-9.-]+:[\w.-]+/[\w.-]+\.git$` — allows self-hosted forge. |
| L-4 | `git safe.directory` not configured for VPS host UID | not in v0 | §4 step 7 — `git config --global --add safe.directory <vault_dir>`. |

### Architecture diff (v0 → v1)

| Aspect | v0 | v1 |
|---|---|---|
| Trigger | scheduler `kind=system:vault_sync` | `asyncio.create_task(_vault_sync_loop)` |
| Migration | `0005_schedules_kind.sql` | NONE |
| Lock primitive | `asyncio.Lock` only | `vault_lock` (fcntl) AROUND git ops + `_lock` (asyncio) for daemon-scoped serialisation + `_vault_sync_pending` task set |
| Notify | flat 24h cooldown | edge-trigger state machine |
| Host key | `accept-new` (TOFU) | static-pinned `known_hosts_vault.pinned` |
| Bootstrap | 7 manual steps | `vault-bootstrap.sh` script (still owner-runs-once) |
| Skill markdown | not mentioned | `skills/vault/SKILL.md` mandatory |
| Drain | not in `Daemon.stop` | `_vault_sync_pending` set drained BEFORE `_bg_tasks` |
| Rate limit | none | 60s min between `vault_push_now` invocations + audit log |
| Pre-commit secret deny-list | none | `_validate_staged_paths` daemon defence + bootstrap pre-push check |

## 8. Devil w2 closure table

v1→v2 fixes. Citations reference the v1 spec line numbers in commit
`361349d`.

### CRITICAL (3)

| Devil ID | Description | v1 line | v2 fix |
|---|---|---|---|
| W2-C1 | `vault_lock` `TimeoutError` was implicitly classified as a push failure, which would burn edge-trigger ok→fail and milestone notifies on benign slow-`memory_write` storms. | v1 §2.4 L216 (`timeout=30` literal) + v1 §2.7 L276–294 (state-machine driven by `result` set that did not include `lock_contention`). | §2.4 + §2.7 + §3 — new setting `vault_lock_acquire_timeout_s: float = 30.0` (separate from `git_op_timeout_s`). `TimeoutError` from `vault_lock` becomes `result="lock_contention"`: audit log row written, structured log emitted, but `consecutive_failures` NOT incremented and edge-trigger state machine NOT transitioned. AC#23. |
| W2-C2 | Two-tier locking spec was inconsistent — v1 said "vault_lock around status/add/commit" and "asyncio.Lock for daemon-internal concurrency" without spelling out which lock covered push, leaving open a window for two concurrent `git push origin main` subprocesses. | v1 §2.4 L194–229 (mixed prose) | §2.4 — explicit two-tier contract: outer `_lock` (asyncio) wraps the FULL pipeline including push, so cron + manual serialise end-to-end and there is never concurrent `git push`. Inner `vault_lock` (fcntl) wraps only `status/add/commit` and is RELEASED before push so concurrent `memory_write` is unblocked during the network leg. Sequence diagram added. AC#6 reworded to verify "exactly one `git push` in flight at a time". |
| W2-C3 | `manual_tool_enabled=True` combined with `enabled=False` was undefined behaviour — the @tool would register but the subsystem itself would not be constructed. | v1 §3 L437 + L448 (no validator) | §2.8 + §3 — `VaultSyncSettings.__post_init__` (or pydantic field validator) raises `ValueError("manual_tool_enabled requires enabled=True")` at config load. Daemon refuses to start; error logged with actionable hint. AC#22. |

### HIGH (4)

| Devil ID | Description | v1 line | v2 fix |
|---|---|---|---|
| W2-H1 | Two issues bundled: (a) v1 used `asyncio.sleep(3600)` semantics without explicitly documenting interval-timer (vs cron-walltime) trade-off; (b) loop slept BEFORE the first tick, so a deploy with `enabled=True` saw no commit until 1h after restart. | v1 §2.1 L99–110 (sleep before run_once) | §2.1 — explicit interval-timer documentation + loop body restructured so `run_once` runs FIRST, then `asyncio.sleep`. Owner sees first commit within ~60s of restart. Carry-forward §6 #13 documents that suspend-aware catch-up is OUT of scope. AC#24. |
| W2-H2 | Audit log file at `<data_dir>/run/vault-sync-audit.jsonl` had no rotation policy; over a multi-year deploy it would grow unbounded. | v1 §2.8 L331–335 (no rotation mention) | §2.8 + §3 — new setting `audit_log_max_size_mb: int = 10`. Writer stats the file before each append; if size exceeds the limit, `os.rename` to `<path>.1` (overwriting any prior `.1` — single-step rotation, no chain). Atomic via rename; no log lines lost. AC#14. |
| W2-H4 | `secret_denylist_globs` had ambiguous anchoring (different `fnmatch` impls treat `secrets/*` as "exactly `secrets/<one-component>`" or "`secrets/` followed by anything"). Bootstrap script and daemon could disagree on matches. | v1 §3 L451–454 + §4 L555–561 (script regex uses different anchoring than glob) | §3 — replaced with `secret_denylist_regex: tuple[str, ...]` with explicitly anchored patterns. Daemon `_validate_staged_paths` uses `re.search` against vault-relative paths. Bootstrap script duplicates the same regex set verbatim with an inline "MUST stay in sync" comment. AC#19 verifies parity across the full denylist set. |
| W2-H5 | No procedure documented for the rare event of GitHub host-key rotation; with a stale pinned file the daemon would fail-fast on every boot. | not in v1 | §4 — new deliverable `docs/ops/vault-host-key-rotation.md`. `startup_check` distinguishes "file missing" (AC#17) from "fingerprint mismatch" (AC#26 — emits `event=vault_sync_host_key_mismatch`, force-disables, daemon continues running). |

### MEDIUM (6)

| Devil ID | Description | v1 line | v2 fix |
|---|---|---|---|
| W2-M1 | Boot-ordering coupling was vague — v1 cited "`Daemon.start()`" without a line anchor, and used `asyncio.create_task` directly rather than the supervised wrapper used by scheduler. | v1 §2.1 L77–95 | §2.1 — explicit insertion point: AFTER subagent boot (~`main.py:570`), BEFORE scheduler boot (~`main.py:572`). Spawn via `_spawn_bg_supervised(self._vault_sync.loop, name="vault_sync_loop")` for parity with scheduler — supervised respawn on unhandled exception, with the same `max_respawn_per_hour=3` budget. AC#10 references supervised spawn. |
| W2-M2 | Rate-limit `_last_invocation_at` was in-memory only; a daemon restart 5s after a `vault_push_now` reset the window and permitted an immediate second push. | v1 §2.8 L324 (`_last_invocation_at: datetime | None` not persisted) | §2.7 + §2.8 — `last_invocation_at` field added to the persisted state JSON at `<data_dir>/run/vault_sync_state` alongside `last_state` and `consecutive_failures`. Loaded at boot. AC#13 extended to cover restart resilience. |
| W2-M3 | `drain_timeout_s=45.0` was less than `push_timeout_s=60`, so a slow-but-healthy push could be torn down mid-flight by `Daemon.stop`. | v1 §3 L447 + §2.9 L389 | §3 — default `drain_timeout_s = 60.0` (matches `push_timeout_s`). `__post_init__` validator enforces `drain_timeout_s >= push_timeout_s`. §2.9 documents the invariant inline. AC#9 + AC#22 cover the scenarios. |
| W2-M4 | The phase-6e RSS observer correlates RSS with `bg_tasks`, `audio_persist_pending`, and `sub_pending` set sizes, but did not include `_vault_sync_pending`. A stuck push would not show up in RSS samples. | v1 §2.9 L370–404 (no observer hook mention) + `main.py:625-684` | §2.9 — explicit instruction that `_rss_observer` adds `vault_sync_pending=len(self._vault_sync_pending)` to the `daemon_rss` log line. This is part of v2 deliverables (a coder-touched line in `main.py`). AC#25. |
| W2-M5 | The Docker compose file did not bind-mount `~/.ssh/vault_deploy` or `~/.ssh/known_hosts_vault` into the container, so a daemon-side `git push` would have no path to authenticate. | v1 §4 (no compose patch mentioned) + `deploy/docker/docker-compose.yml:62-67` | §4 deliverable — compose file gains two read-only bind-mounts for the SSH key and pinned known_hosts. Step 9 of the bootstrap procedure explicitly verifies the mount via `docker exec ls -l`. |
| W2-M6 | "byte-identical to phase 6e" claim in AC#5 was over-strong — module imports do happen, even if no objects are constructed. | v1 AC#5 L629–635 | §5 AC#5 reworded to "no externally-observable behavioural change": no loop task, no MCP @tool registered, no audit log file, RSS observer omits `vault_sync_pending`, etc. Module imports are explicitly allowed. |

### LOW (3)

| Devil ID | Description | v1 line | v2 fix |
|---|---|---|---|
| W2-L1 | Spec did not address pre-existing populated vault dir — typical deploy already has phase-4 markdown notes. Bootstrap commit pattern was unclear about whether they would land in the initial commit or the first cron tick. | v1 §4 step 4 L538–548 | §4 step 6 — explicit note: "if the vault dir already contains markdown notes, bootstrap commits ONLY `.gitignore`. Existing notes will be staged and committed by the FIRST cron tick after restart (covered by AC#1). Owner can manually invoke `vault_push_now` for immediate first commit if desired." |
| W2-L2 | `git_user_email = "bot@0xone.local"` could be email-blocked on accounts with strict GH privacy settings. | v1 §3 L444 + §2.2 L154 | §3 + §2.2 — default changed to `"0xone-assistant@users.noreply.github.com"`. Comment explains the rationale. |
| W2-L3 | `commit_message_template` substitutes filenames straight into the message; a hostile filename containing newlines or control chars could break the commit-message format or smuggle a forged trailer. | v1 §3 L455–457 | §3 commit_message_template note — daemon strips newlines and ASCII control chars (`\x00-\x1f`, `\x7f`) from filenames before substitution; the constraint is documented inline. |

### Architecture diff (v1 → v2)

| Aspect | v1 | v2 |
|---|---|---|
| Locking spec | "vault_lock around git ops" + "asyncio.Lock for daemon-internal" (mixed prose) | Explicit two-tier: outer `_lock` (asyncio) wraps full pipeline incl. push; inner `vault_lock` (fcntl) wraps only status/add/commit and releases before push |
| Lock-contention semantics | implicit (would have counted as failure) | explicit `result="lock_contention"`; audit log only; does NOT trip edge-trigger |
| First-tick timing | sleep `cron_interval_s` then run | run immediately, then sleep (deploy → first commit within ~60s) |
| Audit log rotation | none (unbounded growth) | 10 MB single-step `os.rename` to `.1` |
| Denylist anchoring | `secret_denylist_globs` (ambiguous fnmatch) | `secret_denylist_regex` (explicit anchors); same regex shared by daemon + bootstrap |
| Settings validators | only `repo_url` regex | + `manual_tool_enabled requires enabled=True`, + `drain_timeout_s >= push_timeout_s` |
| Rate-limit persistence | in-memory only | persisted in `<data_dir>/run/vault_sync_state` (`last_invocation_at`) |
| Boot ordering anchor | "`Daemon.start()`" (vague) | after `main.py:570` (subagent), before `main.py:572` (scheduler) |
| Loop spawn primitive | `asyncio.create_task` (orphan-prone on unhandled exc) | `_spawn_bg_supervised` (parity with scheduler) |
| RSS observer hook | not mentioned | `vault_sync_pending` added to `daemon_rss` line |
| Docker bind-mounts | not addressed | compose patch for `~/.ssh/vault_deploy` + `~/.ssh/known_hosts_vault` |
| Host-key rotation procedure | not addressed | `docs/ops/vault-host-key-rotation.md` deliverable + AC#26 |
| `enabled=False` parity claim | "byte-identical" | "no externally-observable behavioural change" |
| `git_user_email` default | `bot@0xone.local` | `0xone-assistant@users.noreply.github.com` |
| Filename safety in commit msg | not documented | newline + control-char strip before template substitution |
| AC count | 21 | 26 |

---

> **Phase-7 integration note (vault vs media).** Phase 6a–6c put
> inbox/outbox media under `<data_dir>/media/` with retention sweep.
> Vault is the separate hierarchy `<data_dir>/vault/`. Phase 8's
> `git add` runs from inside `<data_dir>/vault/` — the working tree
> is the vault dir itself, so `data/media/`, `data/run/`, and the
> SQLite DB are physically outside the working tree and cannot be
> staged. There is no path-isolation test needed (the geometry is
> the test).

## 9. Fix-pack closure table (4-reviewer wave: code-review + qa + devops + devil-w3)

The 4-reviewer wave (`code-reviewer` + `qa-engineer` + `devops-expert`
+ `devil-w3`) ran post-coder; multiple reviewers converged on these
blockers, raising confidence they were real (not noise). Closures
applied in v3.1:

### CRITICAL (7)

| ID | Convergent across | v3 risk | v3.1 fix |
|---|---|---|---|
| F1 | code-review HIGH-1, qa CRIT-1 | `mcp__vault__vault_push_now` unconditionally added to `allowed_tools` and `vault` to `mcp_servers` — model saw the @tool with `enabled=False`. AC#5 violation. | New `vault_tool_visible: bool = False` kwarg on `ClaudeBridge.__init__`. Daemon owner-bridge passes `vault_sync.effective_manual_tool_enabled`; picker/audio bridges default to False. |
| F2 | devops CRIT-1, qa CRIT-2 | Compose short-form bind on `~/.ssh/vault_deploy` auto-creates the host path as a DIRECTORY when bootstrap hasn't run; container starts but vault sync silently fails. | Long-form `type: bind` + `bind: { create_host_path: false }` so Docker errors LOUDLY. README updated with explicit ordering: bootstrap.sh BEFORE compose up. |
| F3 | code-review CRIT-1, qa CRIT-3 | `_run_once_tracked` called `asyncio.current_task()` from inside `loop()`, capturing the OUTER infinite loop task and never removing it from `_vault_sync_pending`. Drain ALWAYS exhausted budget + cancelled the loop. | Refactored: each tick runs as a fresh `asyncio.create_task(_run_once(...), name="vault_sync_tick")` child task that registers + self-removes via `add_done_callback`. `_run_once_tracked` removed; manual `push_now` follows the same fresh-child pattern. |
| F4 | devil w3 vault_lock hold | `git_op_timeout_s=30` × 4 sequential ops = 120s worst case while `vault_lock_acquire_timeout_s=30s` → memory_write times out. | `git_op_timeout_s` default 30 → 10s. `vault_lock_acquire_timeout_s` default 30 → 60s. Validator enforces `vault_lock_acquire_timeout_s >= 4 * git_op_timeout_s`. |
| F5 | qa HIGH-4 W2-M2 regression | Coder default reverted to v0 generic without `{filenames}`, dropping forensic value. | Default restored: `"vault sync {timestamp} ({reason}) -- {files_changed} files: {filenames}"`. Em-dash replaced with `--` for legacy viewer compat (devops LOW-4). |
| F6 | devil W3-CRIT-4, qa MED-1 | Coder flipped `manual_tool_enabled` default to False because validator hard-rejected `True+enabled=False` foot-gun. Spec said True. | Default restored to True. New computed property `effective_manual_tool_enabled = enabled AND manual_tool_enabled` is the actual @tool gate. Validator now uses `model_fields_set` to distinguish owner-explicit vs framework-default and only RAISES on owner-explicit `manual_tool_enabled=True + enabled=False`. |
| F11 | devops CRIT-2 cron drift | Sleep-after-work accumulates drift; immediate-first-tick competes with boot pressure. | Sleep uses wall-clock target (`loop.time() + cron_interval_s`); new `first_tick_delay_s: float = 60.0` setting sleeps BEFORE first tick by default. Owner can override to 0 to restore immediate-first-tick. **Spec contradiction with W2-H1 acknowledged**: boot-pressure protection wins; the "first commit within ~60s" goal becomes "first commit within ~120s" by default. |

### HIGH (6)

| ID | Reviewer | v3 risk | v3.1 fix |
|---|---|---|---|
| F8 | devops + qa | `GIT_SSH_COMMAND` path values not quoted; a path with spaces would break tokenisation. | Both paths `shlex.quote`-d in `build_ssh_command`. |
| F9 | devops HIGH | `notify_*` awaited inside `async with self._lock`; slow Telegram = stalled cron + blocked `memory_write`. | Notify dispatch moved OUTSIDE `_lock`. New `RunResult._notify_action` field tells the outer code which edge to drive. `notify.py` wraps every `send_text` in `asyncio.wait_for(..., timeout=10s)` with `contextlib.suppress`. |
| F10 | qa UX | `last_invocation_at` set on INVOCATION; failed manual call burns the rate-limit window — owner can't retry for 60s. | `push_now` snapshots the prior value; on `result == "failed"` restores it so successive calls sail through. Successful + noop + lock_contention paths keep the timer. |
| F12 | devops + 4-reviewer convergent | Daemon regex `^secrets/` only matches at root; `.gitignore secrets/` matches recursively. Force-staged `notes/secrets/api.env` would bypass daemon while gitignore rejects. | Regex set rewritten with `(?:^|/)` prefix for the dir patterns; bootstrap script's `grep -E` mirrored verbatim. |
| F13 | qa | Drain test used a synthetic helper instead of driving the real `loop()` task. | New integration test `test_phase8_loop_integration.py` exercises the real loop + drain end-to-end. |
| F7 | qa AC coverage | AC#3 / AC#12 / AC#15 / AC#16 / AC#17 / AC#24 / AC#25 / AC#26 had no tests. | Added: `test_phase8_startup_check.py`, `test_phase8_prompt_injection_regression.py`, `test_phase8_git_ssh_command_scope.py`, `test_phase8_loop_first_tick_timing.py`, `test_phase8_rss_observer_field.py`, `test_phase8_vault_lock_race.py`, `test_phase8_disabled_invariants.py`, `test_phase8_loop_integration.py`. |

### Carry-forward to phase 9

The following items from the 4-reviewer wave are deferred:

- DevOps HIGH-3: audit log retention scheme (date-stamped rotation
  beyond the single-step `.1`).
- DevOps MED-2: CI host-key drift check (weekly cron job in GH Actions).
- DevOps HIGH-7: bootstrap script `git reset` between re-runs.
- Devil MED-5: periodic `startup_check` re-run for runtime host-key
  rotation detection.
- QA security: property-based denylist tests (Hypothesis).
- Devil HIGH-3: real-subprocess `git_ops.py` tests (requires git
  binary in CI image — currently mocked).
- W3-CRIT-3: commit message template injection — daemon-side sanitisation
  is in place (`_sanitize_filename`); deferred test to phase 9 limitations.

### Architecture diff (v3 → v3.1)

| Aspect | v3 | v3.1 |
|---|---|---|
| `vault_push_now` MCP @tool gate | unconditional registration in bridge | conditional on `vault_tool_visible=True` kwarg; daemon passes `effective_manual_tool_enabled` |
| `manual_tool_enabled` default | False (coder divergence from spec) | True (per spec) + soft validator that distinguishes owner-explicit vs default |
| Cron loop tick task | outer loop task captured by `asyncio.current_task()` | each tick is a fresh `asyncio.create_task` child, self-registers + self-removes |
| First tick timing | immediate (W2-H1) | `first_tick_delay_s` default 60s (boot-pressure protection); owner can revert to 0 |
| Sleep semantics | `await asyncio.sleep(cron_interval_s)` after each tick (drift accumulates) | wall-clock target via `loop.time() + cron_interval_s` (drift-free) |
| Notify dispatch | inside `async with self._lock` | OUTSIDE `_lock`; bounded by `asyncio.wait_for(..., timeout=10s)` per call |
| Failed `vault_push_now` rate-limit | timer set on invocation, NOT reset on failure | prior value restored on `result == "failed"` so owner can retry immediately |
| `git_op_timeout_s` default | 30s | 10s |
| `vault_lock_acquire_timeout_s` default | 30s | 60s; validator enforces `>= 4 * git_op_timeout_s` |
| `secret_denylist_regex` anchoring | `^secrets/`, `^\.aws/`, `^\.config/0xone-assistant/` | `(?:^|/)secrets/`, `(?:^|/)\.aws/`, `(?:^|/)\.config/0xone-assistant/` (recursive parity with .gitignore) |
| Commit message template | `vault sync {timestamp} ({reason}) — {files_changed} files` | `vault sync {timestamp} ({reason}) -- {files_changed} files: {filenames}` |
| Docker compose SSH bind | short-form `:ro` (auto-creates dir if missing) | long-form `type: bind` + `create_host_path: false` (errors loudly if missing) |
| `GIT_SSH_COMMAND` paths | f-string interpolation | `shlex.quote(str(path))` |
| AC test coverage | AC#1, AC#2, AC#10, AC#13, AC#14, AC#23 | + AC#3, AC#5, AC#12, AC#15, AC#16, AC#17, AC#19, AC#22, AC#24, AC#25, AC#26 |
