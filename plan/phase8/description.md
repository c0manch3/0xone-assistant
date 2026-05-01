# Phase 8 — Vault → GitHub push-only periodic sync

> Spec v1 — post-devil-w1, simple asyncio loop. Owner-fixed scope (no
> re-litigation): push-only direction, dedicated private GH repo, SSH
> deploy key auth, periodic batch trigger. Scheduler `kind`-column
> integration was considered in v0 and **rejected by the owner** in
> favour of a self-contained `asyncio` loop spawned from
> `Daemon.start()` — see §2.1 and the v0→v1 architecture diff in §7.
> Devil w1 (5 CRITICAL + 6 HIGH + key MEDIUMs) findings folded; line-by-
> line closure table in §7.

## 1. Goal

The bot's long-term memory vault at `<data_dir>/vault/` on the VPS gets
push-only synced to a dedicated private GitHub repo on a periodic batch
schedule. GitHub holds a read-only mirror of the vault contents — the
bot is the sole writer, there is no pull-back path, and out-of-band
edits on the GH side are treated as an error to surface (not
auto-merge).

Trigger architecture: a single `asyncio.create_task(_vault_sync_loop())`
anchored in `Daemon._bg_tasks`, firing every `cron_interval_s` seconds
(default `3600.0` = hourly). **No SchedulerLoop / SchedulerDispatcher
involvement, no `kind` column, no SQL migration in this phase.** The
loop is a leaf component of the daemon, isolated from phase-5b's
prompt-injection schedule path entirely.

The single supported flow:

1. Owner chats with the bot → the model invokes the existing phase-4
   `memory_write` MCP tool → markdown files appear under
   `<data_dir>/vault/` (existing phase-4 behaviour, unchanged).
2. Every `cron_interval_s` seconds (default 3600s), the daemon's
   `_vault_sync_loop` wakes, acquires the vault `fcntl` lock around
   the git pipeline, runs `git status / git add / git commit` (lock
   held), releases the lock, then runs `git push origin main` (no
   working-tree access during push).
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

### 2.1 Trigger mechanism — asyncio.create_task on a dedicated loop

The vault-sync subsystem is a self-contained loop spawned from
`Daemon.start()`. The lifecycle:

```python
# Daemon.start (sketch — implementation in coder phase)
if self._settings.vault_sync.enabled:
    self._cleanup_stale_vault_locks(self._settings.vault_dir)  # see §2.5
    self._vault_sync = VaultSyncSubsystem(
        vault_dir=self._settings.vault_dir,
        index_db_path=self._settings.memory_index_db_path,
        settings=self._settings.vault_sync,
        notifier=self._notifier,
        run_dir=self._settings.run_dir,
    )
    await self._vault_sync.startup_check()  # known_hosts pin + key file
    task = asyncio.create_task(
        self._vault_sync.loop(),
        name="vault_sync_loop",
    )
    self._bg_tasks.add(task)
    task.add_done_callback(self._bg_tasks.discard)
```

The loop body:

```python
async def loop(self) -> None:
    while True:
        try:
            await asyncio.sleep(self._settings.cron_interval_s)
            await self.run_once(reason="scheduled")
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            self._log.exception("vault_sync_loop_error")
            # Continue — never let the loop die.
```

When `enabled=False` (default), no task is spawned, no module state is
constructed, no MCP @tool registers. The daemon is byte-identical to a
phase-7-shipped baseline.

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
bot-identity (`0xone-assistant <bot@0xone.local>`).

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

**H3 / Devil-w1 closure**: `GIT_SSH_COMMAND` is passed via the `env=`
parameter to `asyncio.create_subprocess_exec(...)`. **NEVER set on the
daemon process via `os.environ.update`.** Any unrelated subprocess
spawned by phase-4/5/6 must see the host's default ssh state, not the
vault deploy key path.

This isolates vault git ops from any other ssh-agent or default-key
behaviour on the host. No `gh` CLI dependency — `gh` is not invoked
anywhere in this phase.

### 2.4 Locking — vault_lock (fcntl) AROUND git status/add/commit

A single `asyncio.Lock` in-memory inside `VaultSyncSubsystem` serialises
the cron loop and `vault_push_now` @tool paths against each other. But
that **only** covers daemon-internal concurrency. The vault directory
is also written to by `memory_write` (phase-4) via `atomic_write` which
uses `<vault>/.tmp/.tmp-*.md` + `os.replace`. If a `git add -A` runs
between the `tempfile.NamedTemporaryFile` creation and the `os.replace`
finalise, the half-written `.tmp-*.md` would land in a commit (devil
C-2).

**Closure**: `VaultSyncSubsystem.run_once` acquires
`assistant.tools_sdk._memory_core.vault_lock(...)` BEFORE running any
working-tree-affecting git subprocess. The lock primitive is
fcntl-based on the index-db lock path `<index_db_path>.lock` (NOT
inside the vault dir — same lock primitive that `memory_write`,
`memory_delete`, and `reindex_under_lock` already use):

```python
from assistant.tools_sdk._memory_core import vault_lock

lock_path = self._index_db_path.with_suffix(self._index_db_path.suffix + ".lock")
with vault_lock(lock_path, blocking=True, timeout=30):
    # Working-tree-touching ops only — push runs OUTSIDE the lock.
    await self._git_status()
    await self._git_add()
    await self._git_commit(...)
# Lock released.
await self._git_push()  # push reads .git/objects/, NOT the working tree
```

The lock is held for the entire `status → add → commit` pipeline and
released before `push`. `git push` only reads `.git/objects/` and the
network — it never touches the working tree, so the vault stays
unblocked for `memory_write` during the (potentially slow) network
operation.

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

**Devil H-1 closure.** v0's flat 24h cooldown silenced legitimate signal
on a recovery (a 24h-stale "fail" notify followed by a recovery the
owner never sees). v1 replaces it with an edge-trigger state machine
persisted at `<data_dir>/run/vault_sync_state` (single-line JSON file
with schema `{"last_state": "ok"|"fail", "consecutive_failures": N}`).

Transitions:

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
`"ok"`).

`notify_cooldown_s` from v0 is **dropped**. Replaced by
`notify_milestone_failures: tuple[int, ...]`.

### 2.8 Module location — src/assistant/vault_sync/

New package `src/assistant/vault_sync/` contains:

- `__init__.py` — exports `VaultSyncSubsystem`, `VaultSyncSettings`.
- `subsystem.py` — `VaultSyncSubsystem` class with:
  - `loop()` — the asyncio loop (sleeps, calls `run_once`, never
    dies).
  - `run_once(reason: str) -> RunResult` — the git pipeline,
    holding `vault_lock` around `status/add/commit` and releasing
    before `push`.
  - `startup_check()` — validates `ssh_key_path` exists,
    `ssh_known_hosts_path` exists and contains a recognisable
    GitHub host-key fingerprint (refuses to start vault sync
    otherwise; H4 closure).
  - `_validate_staged_paths()` — re-runs the secret denylist
    against staged paths just before `git commit` (H2 daemon-side
    defence-in-depth).
  - `_lock: asyncio.Lock` — daemon-internal serialisation between
    cron loop and `vault_push_now` @tool.
  - `_last_invocation_at: datetime | None` — for the
    `manual_tool_min_interval_s` rate-limit on `vault_push_now`.
- `git_ops.py` — thin async wrappers around `git status`,
  `git add`, `git commit`, `git push` returning typed result
  dataclasses; centralises the `GIT_SSH_COMMAND` env injection
  (per-subprocess, never daemon-wide).
- `notify.py` — Telegram failure-notify with edge-trigger state
  machine persisted at `<data_dir>/run/vault_sync_state`.
- `audit.py` — JSONL audit log writer at
  `<data_dir>/run/vault-sync-audit.jsonl`. Append-only, fields
  `{"ts": iso, "reason": "scheduled|manual", "result":
  "pushed|noop|rate_limited|failed", "files_changed": N,
  "commit_sha": "..."}`.

The subsystem is owned by `Daemon` (constructed once at boot, lives
for the daemon lifetime). The `vault_push_now` @tool body holds a
reference to the same instance via the @tool's closure (set up during
MCP server registration).

**Manual @tool path.** A new MCP @tool `vault_push_now` is registered
under a new MCP server group `mcp__vault__` (separate from the
existing `mcp__memory__` and `mcp__scheduler__` groups so its
allow/deny status is independently togglable via skill frontmatter).
The tool body:

1. Checks rate-limit: if `now - _last_invocation_at <
   manual_tool_min_interval_s` (default 60s), return
   `{"ok": false, "reason": "rate_limit", "next_eligible_in_s": N}`
   WITHOUT running git ops. Audit log records `result="rate_limited"`.
2. Acquires `VaultSyncSubsystem._lock` (the same asyncio lock the
   cron loop uses).
3. Creates an asyncio task for `run_once(reason="manual")` and
   registers it in `Daemon._vault_sync_pending` (set, mirror of
   `_audio_persist_pending`). The task self-removes via
   `add_done_callback`.
4. Awaits the task.
5. Returns `{"ok": True, "files_changed": N, "commit_sha": "<sha>"}`
   on success, `{"ok": False, "reason": "<short error>"}` on
   failure. Failure also drives the §2.7 edge-trigger notify path.
6. Updates `_last_invocation_at = now` AT INVOCATION TIME, not
   completion time, so the rate-limit covers the operation duration.

The @tool wiring is gated by `settings.vault_sync.manual_tool_enabled`
— if `False`, the tool is not registered with the SDK MCP server and
the model never sees it in its tool catalogue.

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
# In Daemon.stop, BEFORE _bg_tasks cancel:
if self._vault_sync_pending:
    pending = list(self._vault_sync_pending)
    deadline = self._settings.vault_sync.drain_timeout_s  # 45s
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=deadline,
        )
    except asyncio.TimeoutError:
        log.warning(
            "vault_sync_drain_timeout",
            pending=len(pending),
            deadline_s=deadline,
        )
        # Fall through: _bg_tasks cancel below will tear down hard.
self._vault_sync_pending.clear()
# ... existing _bg_tasks drain follows ...
```

Budget: `drain_timeout_s = 45.0` covers p99 SSH push latency on a slow
VPS link (typical observed: 1-5s; pathological large-vault first push:
~30s). On budget exhaustion, the `_bg_tasks` cancel path tears down
forcibly; the next boot's `_cleanup_stale_vault_locks` reaps any
`.git/index.lock` artefact.

### 2.10 NO schema migration

**Devil C-1 closure.** v0's `0005_schedules_kind.sql` migration is
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
    git_user_email: str = "bot@0xone.local"
    git_op_timeout_s: int = 30
    push_timeout_s: int = 60
    drain_timeout_s: float = 45.0
    manual_tool_enabled: bool = True
    manual_tool_min_interval_s: float = 60.0
    notify_milestone_failures: tuple[int, ...] = (5, 10, 24)
    secret_denylist_globs: tuple[str, ...] = (
        "*.env", "*.key", "*.pem", "secrets/*",
        ".aws/*", ".config/0xone-assistant/*",
    )
    commit_message_template: str = (
        "vault sync {timestamp} ({reason}) — {files_changed} files: {filenames}"
    )
```

| Field | Default | Purpose |
|---|---|---|
| `enabled` | `False` | Master switch. Daemon skips all vault-sync wiring (loop spawn, @tool registration, startup_check, lock cleanup) when `False`. Defaults `False` so a fresh checkout does not try to push to a non-existent repo. |
| `repo_url` | `None` | SSH URL of the dedicated private vault repo. Required if `enabled=True`. Production value: `git@github.com:c0manch3/0xone-vault.git`. Validator regex: `^git@[a-z0-9.-]+:[\w.-]+/[\w.-]+\.git$` (L-2 — allows self-hosted forge in future). |
| `ssh_key_path` | `~/.ssh/vault_deploy` | Deploy key file. Existence checked at `startup_check`. |
| `ssh_known_hosts_path` | `~/.ssh/known_hosts_vault` | Static-pinned known_hosts file. Must exist AND contain a GitHub-recognisable host-key fingerprint at `startup_check` (H4). |
| `branch` | `"main"` | Branch to push. |
| `cron_interval_s` | `3600.0` | Sleep duration of the asyncio loop. Replaces v0's `cron: str = "0 * * * *"`. Float so tests can set sub-second values. |
| `git_user_name` | `"0xone-assistant"` | `user.name` for vault commits. |
| `git_user_email` | `"bot@0xone.local"` | `user.email` for vault commits. |
| `git_op_timeout_s` | `30` | Per-subprocess timeout for non-push git ops (`status`, `add`, `commit`). |
| `push_timeout_s` | `60` | Per-subprocess timeout for `git push`. |
| `drain_timeout_s` | `45.0` | `Daemon.stop` budget for in-flight push drain (§2.9). |
| `manual_tool_enabled` | `True` | Gates `vault_push_now` @tool registration. |
| `manual_tool_min_interval_s` | `60.0` | Min seconds between consecutive `vault_push_now` invocations (C-4). 2nd call within 60s returns `rate_limited` with audit log row. |
| `notify_milestone_failures` | `(5, 10, 24)` | Edge-trigger milestone notify thresholds (H-1). Replaces v0's `notify_cooldown_s`. |
| `secret_denylist_globs` | see code | Glob patterns rejected by `_validate_staged_paths` defence-in-depth before commit (H-2). |
| `commit_message_template` | see code | f-string-style template, M-2. Available keys: `timestamp` (ISO-8601 UTC), `reason` ("scheduled"/"manual"/"boot"), `files_changed` (int), `filenames` (first 3 staged paths comma-joined, truncated). |

Validation: when `enabled=True`, settings construction asserts
`repo_url` non-empty and matches the regex. SSH key and known_hosts
existence checks are deferred to `VaultSyncSubsystem.startup_check`
so `pytest` config validation does not require keys on the dev box.

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
  GitHub's current ed25519 + rsa host keys. Verify against
  `https://api.github.com/meta` before each release that touches this
  file. `vault-bootstrap.sh` copies this file into
  `~/.ssh/known_hosts_vault` rather than running `ssh-keyscan` (H-4
  closure: no TOFU).
- `docs/ops/vault-secret-leak-recovery.md` — boilerplate runbook for
  the case where a secret gets committed despite both layers of
  defence (force-push to overwrite history, rotate the leaked
  credential, revoke deploy key + reissue, audit the audit log).

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
     git config user.email "bot@0xone.local"
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
   depth):
   ```sh
   STAGED=$(git diff --cached --name-only)
   if echo "$STAGED" | grep -E '\.(env|key|pem)$|^secrets/|^\.aws/|^\.config/0xone-assistant/' > /dev/null; then
     echo "ERROR: staged files match secret denylist:"
     echo "$STAGED" | grep -E '...'
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

9. Restart the daemon (`docker compose restart`). On boot:
   - `_cleanup_stale_vault_locks` runs first.
   - `VaultSyncSubsystem.startup_check` validates the pinned
     known_hosts + key file. Refuses to start the loop on any
     missing/malformed input — daemon logs error, force-disables
     `enabled` for the rest of the process lifetime, and continues
     with all other phases functional.
   - `_vault_sync_loop` spawns; first tick fires after
     `cron_interval_s` seconds.
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

**AC#5 — `enabled=False` default → no task spawned, byte-identical to
phase 6e.** With `VAULT_SYNC_ENABLED=false`: no loop task, no @tool
registration, no `_cleanup_stale_vault_locks`, no module state, no
audit log file created, zero Telegram notifies. Daemon process
matches phase-7-shipped baseline byte-for-byte (verified by
inspecting `_bg_tasks` count + MCP tool catalogue + filesystem under
`<data_dir>/run/`).

**AC#6 — concurrency / vault_lock + asyncio task list serialise cron
+ manual.** Two concurrent invocations (cron tick firing at the same
instant the model invokes `vault_push_now`) serialise on
`_lock` (asyncio) AND on `vault_lock` (fcntl) such that no
interleaved git commands occur and no `.tmp/.tmp-*.md` artefact reaches
a commit. Verified via focused pytest with `asyncio.gather` on two
`run_once` calls + a subprocess-mock that asserts call ordering, plus
a parallel `memory_write` task that produces a tmp file mid-cycle.

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

**AC#9 — Daemon.stop drain for in-flight push within 45s budget;
hard-kill leaves clean state for next boot.** Manual `vault_push_now`
invoked at T-5s (slow push in progress). `Daemon.stop` called at T+0.
Push completes within the 45s budget AND the daemon shuts down
cleanly OR the budget exhausts and `_bg_tasks` cancel forces shutdown.
On next boot, `_cleanup_stale_vault_locks` reaps any
`.git/index.lock` artefact and the next loop tick succeeds. Verified
in container with `docker compose stop` mid-push.

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

**AC#13 — `vault_push_now` rate-limit.** First call at T+0 returns
`{"ok": true, ...}`. Second call at T+30s returns
`{"ok": false, "reason": "rate_limit", "next_eligible_in_s": 30}`
WITHOUT running git ops. Third call at T+61s succeeds.

**AC#14 — `vault_push_now` audit log writes JSONL row per
invocation.** `<data_dir>/run/vault-sync-audit.jsonl` contains one
line per invocation (success, no-op, rate-limited, failed) with the
documented field set. Test parses the JSONL and asserts row counts
+ field shape.

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
via `test_git_ssh_command_scope.py`.

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
"синхронизируй vault", "push vault now". Skill frontmatter restricts
`allowed-tools: ["mcp__vault__vault_push_now"]`. The body includes
explicit anti-pattern guidance: "do NOT call `vault_push_now` as
side-effect of `memory_write` or other tool chains; this tool is
for explicit owner request only".

**AC#19 — bootstrap pre-push secret-leak check.** Run
`vault-bootstrap.sh` with `secrets/dummy.env` planted in the vault
dir → script's pre-push validation rejects the commit with explicit
error message and non-zero exit.

**AC#20 — fail → ok recovery notify.** After 3 consecutive failed
pushes (network outage), one successful push → owner gets a single
"vault sync recovered after 3 consecutive failures" Telegram message.
State file resets to `{"last_state": "ok",
"consecutive_failures": 0}`.

**AC#21 — milestone notify at N=5.** Sustained outage → at the 5th
consecutive failure, owner gets a milestone Telegram notify ("vault
sync still failing — 5 consecutive failures") regardless of the
silent `fail→fail` rule. The next milestone fires at 10, then 24.

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
| H-6 | `Daemon.stop` drain for in-flight push (hard-kill mid-push leaves dirty state) | not in v0 | §2.9 — `_vault_sync_pending: set[asyncio.Task]` mirrors `_audio_persist_pending`. `Daemon.stop` drains BEFORE `_bg_tasks` cancel with `drain_timeout_s=45.0` budget. AC#9. |

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

---

> **Phase-7 integration note (vault vs media).** Phase 6a–6c put
> inbox/outbox media under `<data_dir>/media/` with retention sweep.
> Vault is the separate hierarchy `<data_dir>/vault/`. Phase 8's
> `git add` runs from inside `<data_dir>/vault/` — the working tree
> is the vault dir itself, so `data/media/`, `data/run/`, and the
> SQLite DB are physically outside the working tree and cannot be
> staged. There is no path-isolation test needed (the geometry is
> the test).
