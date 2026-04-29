# Phase 8 — Vault → GitHub push-only periodic sync

> Spec v0. Owner-fixed scope (no re-litigation): push-only direction,
> dedicated private GH repo, SSH deploy key auth, periodic batch
> trigger. All RQs from the Plan-agent draft are closed below — see
> §1 final paragraph and §3 settings table for the exact values.

## 1. Goal

The bot's long-term memory vault at `<data_dir>/vault/` on the VPS gets
push-only synced to a dedicated private GitHub repo on a periodic batch
schedule. GitHub holds a read-only mirror of the vault contents — the
bot is the sole writer, there is no pull-back path, and out-of-band
edits on the GH side are treated as an error to surface (not
auto-merge).

The single supported flow:

1. Owner chats with the bot → the model invokes the existing phase-4
   `memory_write` MCP tool → markdown files appear under
   `<data_dir>/vault/` (existing phase-4 behaviour, unchanged).
2. On a cron tick (default `0 * * * *` — every hour at :00), the daemon
   runs a vault-sync pass: `git add . && git commit -m "..." &&
   git push origin main` inside `<data_dir>/vault/`.
3. The dedicated GH repo `c0manch3/0xone-vault` accumulates a commit log
   of vault changes, viewable in the browser. No human or other system
   pushes to that repo — the deploy key is the only writer.
4. Model can also trigger an immediate push via the `vault_push_now`
   MCP @tool ("важная заметка, давай сразу засинхрю" → model invokes
   the tool → same git pipeline runs synchronously).

This phase does NOT add `gh` CLI features — pure git-over-SSH only. No
PR creation, no issue read/write, no `gh api` extensions. The sole
GitHub interaction is `git push` over SSH using a dedicated deploy key
unique to the `0xone-vault` repo.

**Closed RQ decisions** (orchestrator-confirmed with owner; folded
into this spec, no longer "open"):

- Repo: `c0manch3/0xone-vault` (dedicated private), SSH URL
  `git@github.com:c0manch3/0xone-vault.git`.
- Cron default: `0 * * * *` (hourly at :00 UTC).
- GH account: `c0manch3` (owner's main account; deploy key scoped to
  the single `0xone-vault` repo so blast radius is bounded).
- Manual trigger: IN scope as `vault_push_now` MCP @tool.
- Notify channel on failure: Telegram, with 24-hour cooldown
  (`notify_cooldown_s=86400`) so a stuck failure mode doesn't spam.
- Scheduler shape: Shape A — new `kind` column added via migration
  `0005_schedules_kind.sql`. The dispatcher branches on the column;
  no sentinel-prompt heuristic.

## 2. Architecture

### 2.1 Trigger mechanism — scheduler-driven cron

The vault-sync subsystem is driven by the existing phase-5b
`SchedulerLoop` already wired into the daemon. We add a single
seeded schedule row at daemon boot:

- `kind = 'system:vault_sync'`
- `cron = settings.vault_sync.cron` (default `"0 * * * *"`)
- `seed_key = "vault_sync"` (idempotent — repeated daemon starts do
  not duplicate the row, see §2.8 migration).

When the cron tick fires, the dispatcher does NOT inject a prompt for
the model. Instead it directly invokes
`VaultSyncSubsystem.run_once(reason="scheduled")`. This avoids paying
a model turn (and the associated Anthropic billing + ~5–15 s wall
latency) for what is purely a mechanical git operation.

### 2.2 Repo layout — `.git` inside the vault dir

The vault directory `<data_dir>/vault/` is itself the working tree of
a dedicated git repo. `.git/` lives directly under
`<data_dir>/vault/.git/`. There is no superproject — the vault is its
own standalone repo.

`.gitignore` (committed at repo init by the bootstrap script in §4)
includes:

```
# anything that shouldn't leave the VPS
*.tmp
*.swp
.DS_Store
*~
# explicitly NO data/media/ — that path is outside the vault dir
# already; this .gitignore is just defence in depth.
```

The vault's git config has `core.autocrlf = false`,
`core.filemode = false`, `user.email` and `user.name` set to a
bot-identity (`0xone-assistant <bot@0xone>`).

### 2.3 Push mechanism — pure git over SSH

The subsystem shells out to the system `git` binary via
`asyncio.subprocess`. Authentication is via an SSH deploy key
generated during the §4 bootstrap, stored at
`/home/0xone/.ssh/0xone_vault_deploy` on the VPS, and registered as
the **only** deploy key with write access on the GH repo.

The subsystem sets `GIT_SSH_COMMAND` for each subprocess invocation:

```
GIT_SSH_COMMAND="ssh -i /home/0xone/.ssh/0xone_vault_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/home/0xone/.ssh/known_hosts_vault"
```

This isolates vault git ops from any other ssh-agent or default-key
behaviour on the host. No `gh` CLI dependency — `gh` is not invoked
anywhere in this phase.

### 2.4 Locking — daemon-scoped `asyncio.Lock`

A single `asyncio.Lock` instance lives on `VaultSyncSubsystem`. Both
the cron-driven dispatcher path AND the model `vault_push_now` @tool
path acquire this lock before running the git pipeline. The two paths
serialise cleanly — if a cron tick fires while a manual push is
in-flight (or vice-versa), the second waits.

The lock is process-local (single daemon, per the CLAUDE.md
non-negotiable "single active daemon at a time across hosts"). No
`fcntl.flock` / on-disk lock is needed because the daemon is the only
writer.

### 2.5 Conflict / divergence handling

The push uses plain `git push origin main` (no `--force`,
no `--force-with-lease`). If the remote has diverged (which should
never happen — only the deploy key writes), git returns non-zero with
`! [rejected] main -> main (non-fast-forward)`.

On divergence the subsystem:

1. Logs a structured-log error with `event=vault_sync_diverged`,
   `local_sha=...`, `remote_sha=...` (fetched on the spot for the
   log).
2. Sends a Telegram notify to the owner: "vault sync failed:
   remote diverged. Manual recovery needed." (subject to the 24-hour
   cooldown per §2.7).
3. Does NOT attempt rebase, merge, or force-push.
4. Returns failure to the caller.

Cron continues to attempt the push every hour; each failure
re-evaluates the cooldown and may or may not notify.

### 2.6 Empty diff — silent no-op

If `git status --porcelain` returns empty after `git add -A`, the
subsystem skips the commit entirely (no empty commits). This is
logged at debug level (`event=vault_sync_no_changes`) and does NOT
notify Telegram. The `vault_push_now` @tool returns
`{"ok": true, "files_changed": 0, "commit_sha": null}` in this case.

### 2.7 Module location — `src/assistant/vault_sync/`

New package `src/assistant/vault_sync/` contains:

- `__init__.py` — exports `VaultSyncSubsystem`, `VaultSyncSettings`.
- `subsystem.py` — `VaultSyncSubsystem` class with `run_once(reason)`
  and the `asyncio.Lock`. Constructor takes a `vault_dir: Path`,
  `settings: VaultSyncSettings`, `notifier: TelegramNotifier`.
- `git_ops.py` — thin async wrappers around `git status`,
  `git add`, `git commit`, `git push` returning typed result
  dataclasses; centralises the `GIT_SSH_COMMAND` env injection.
- `notify.py` — Telegram failure-notify with on-disk cooldown
  state at `<data_dir>/run/vault_sync_last_notify` (epoch seconds).
  Re-notifying respects `settings.vault_sync.notify_cooldown_s`
  (default 86400 = 24 h).

The subsystem is owned by `Daemon` (constructed once at boot, lives
for the daemon lifetime). The dispatcher and the `vault_push_now`
@tool both hold a reference to the same instance.

**Manual @tool path.** A new MCP @tool `vault_push_now` is registered
under a new MCP server group `mcp__vault__` (separate from the
existing `mcp__memory__` and `mcp__scheduler__` groups so its
allow/deny status is independently togglable via skill frontmatter).
The tool body:

1. Acquires `VaultSyncSubsystem._lock` (the same lock cron uses).
2. Calls `VaultSyncSubsystem.run_once(reason="manual")`.
3. Returns `{"ok": True, "files_changed": N, "commit_sha": "<sha>"}`
   on success, `{"ok": False, "reason": "<short error>"}` on
   failure. Failure also drives the same Telegram-notify path with
   the same 24 h cooldown.

The @tool wiring is gated by `settings.vault_sync.manual_tool_enabled`
— if `False`, the tool is not registered with the SDK MCP server and
the model never sees it in its tool catalogue.

### 2.8 Scheduler integration — new `kind` column (Shape A finalised)

Phase-5b's `schedules` table currently has columns
`(id, chat_id, cron, prompt, seed_key, ...)`. Today the dispatcher
treats every row as "inject `prompt` as an `IncomingMessage(origin=
"scheduler")` for `chat_id` on each cron tick".

Phase-8 adds a new column via migration `0005_schedules_kind.sql`:

```sql
ALTER TABLE schedules ADD COLUMN kind TEXT NOT NULL DEFAULT 'prompt';
CREATE INDEX IF NOT EXISTS idx_schedules_kind ON schedules(kind);
```

The `DEFAULT 'prompt'` clause keeps every pre-existing row (and any
row inserted by phase-5b paths that don't know about the new column)
working unchanged. Phase-8 inserts its seed row with
`kind = 'system:vault_sync'`.

The dispatcher (`scheduler/dispatcher.py`) gains a top-level branch:

```python
if row.kind == 'prompt':
    # existing path — inject IncomingMessage to the model
    ...
elif row.kind == 'system:vault_sync':
    await vault_sync.run_once(reason="scheduled")
else:
    log.warning("unknown_schedule_kind", kind=row.kind, id=row.id)
    # fail closed: do nothing
```

Future system-kinds (`'system:retention_sweep'`, etc.) extend the
branch table without further migrations. The `reason` parameter
("scheduled" | "manual" | "boot") is purely for structured-log
correlation — it doesn't change behaviour.

## 3. Settings spec

New nested settings block `VaultSyncSettings`, mounted on the root
`Settings` as `settings.vault_sync`. Env prefix `VAULT_SYNC_`.

| Field | Type | Default | Purpose |
|---|---|---|---|
| `enabled` | `bool` | `False` | Master switch. Daemon skips all vault-sync wiring (seed insert, @tool registration, dispatcher branch is dormant) when `False`. Defaults to `False` so a fresh checkout doesn't try to push to a non-existent repo. |
| `cron` | `str` | `"0 * * * *"` | Cron expression for the seeded schedule. Hourly at :00 UTC. |
| `repo_url` | `str` | `""` | SSH URL of the dedicated private vault repo. Required if `enabled=True`. Production value: `git@github.com:c0manch3/0xone-vault.git`. |
| `branch` | `str` | `"main"` | Branch to push. |
| `ssh_key_path` | `Path` | `~/.ssh/0xone_vault_deploy` | Deploy key file. Must exist at daemon-start if `enabled=True`. |
| `ssh_known_hosts_path` | `Path` | `~/.ssh/known_hosts_vault` | Dedicated known_hosts file (so vault's SSH host pinning is independent of the user's general SSH state). |
| `commit_author_name` | `str` | `"0xone-assistant"` | `user.name` for vault commits. |
| `commit_author_email` | `str` | `"bot@0xone"` | `user.email` for vault commits. |
| `commit_message_template` | `str` | `"vault sync {timestamp} ({reason})"` | f-string-style template. Available keys: `timestamp` (ISO-8601 UTC), `reason` ("scheduled"/"manual"/"boot"), `files_changed` (int). |
| `notify_cooldown_s` | `int` | `86400` | Min seconds between consecutive Telegram failure notifies. |
| `manual_tool_enabled` | `bool` | `True` | Gates whether `vault_push_now` @tool is wired into the MCP server. Set `False` to disable manual model-driven pushes while keeping the cron path live. |
| `git_timeout_s` | `int` | `60` | Per-subprocess timeout for any single `git` invocation. Push beyond this → log + notify + fail. |

Validation: when `enabled=True`, settings construction asserts
`repo_url` non-empty and starts with `git@github.com:`. The SSH key
existence check is deferred to daemon-start (so `pytest` config
validation doesn't require keys on the dev box).

## 4. VPS bootstrap (one-time, owner does this)

Owner runs these manual steps once on the VPS — the daemon does NOT
self-bootstrap the SSH key or repo (deliberate: cred handling is
owner work, not bot work).

1. **Generate deploy key** on the VPS:

   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/0xone_vault_deploy -N "" \
     -C "0xone-vault deploy key (VPS 193.233.87.118)"
   ```

2. **Create the GH repo** as `c0manch3` (on web UI):
   - Name: `0xone-vault`
   - Visibility: Private
   - Initialise with empty README so `main` exists.

3. **Add deploy key with write access** at
   `https://github.com/c0manch3/0xone-vault/settings/keys/new`:
   - Title: "VPS 193.233.87.118 (0xone-assistant daemon)"
   - Key: contents of `~/.ssh/0xone_vault_deploy.pub`
   - Allow write access: **YES**

4. **Pin the host key** to the dedicated known_hosts file:

   ```sh
   ssh-keyscan -t ed25519 github.com > ~/.ssh/known_hosts_vault
   ```

5. **Initialise the vault dir as a git repo** (idempotent — only do
   this if `<data_dir>/vault/.git/` does not exist):

   ```sh
   cd ~/.local/share/0xone-assistant/vault
   git init -b main
   git config user.name  "0xone-assistant"
   git config user.email "bot@0xone"
   git remote add origin git@github.com:c0manch3/0xone-vault.git
   # write the .gitignore from §2.2
   git add .gitignore .
   GIT_SSH_COMMAND="ssh -i ~/.ssh/0xone_vault_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=~/.ssh/known_hosts_vault" \
     git commit -m "initial vault commit"
   GIT_SSH_COMMAND="ssh -i ~/.ssh/0xone_vault_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=~/.ssh/known_hosts_vault" \
     git push -u origin main
   ```

6. **Enable in env file** at
   `~/.config/0xone-assistant/.env`:

   ```
   VAULT_SYNC_ENABLED=true
   VAULT_SYNC_REPO_URL=git@github.com:c0manch3/0xone-vault.git
   ```

7. **Restart the daemon** (`docker compose restart`). On boot:
   - The seed-row insert runs (idempotent on `seed_key="vault_sync"`).
   - `vault_push_now` @tool registers with the SDK MCP server.
   - First cron tick at the next :00 fires
     `VaultSyncSubsystem.run_once(reason="scheduled")`.

If `VAULT_SYNC_ENABLED=false` (default), none of the above wiring
runs and the daemon behaves identically to phase-5d/6/7.

## 5. Acceptance criteria

- **AC#1 — bootstrap dry-run works.** With `enabled=true` and the
  GH repo, deploy key, and vault git repo all set up per §4, owner
  manually invokes `git push` from the vault dir (using
  `GIT_SSH_COMMAND` env from §2.3) and the push succeeds against
  GitHub. (Sanity check before we even start the daemon path.)
- **AC#2 — seed inserts once.** First daemon start with
  `enabled=true` inserts exactly one row in `schedules` with
  `kind='system:vault_sync'`, `cron='0 * * * *'`,
  `seed_key='vault_sync'`. Second daemon start does NOT insert a
  duplicate (idempotency on `seed_key`).
- **AC#3 — scheduled push happy path.** With the daemon up and a
  fresh `memory_write` from a model turn (creating one new
  markdown file under `<data_dir>/vault/`), the next :00 cron tick
  fires the dispatcher, the dispatcher branches on `kind`, the
  vault-sync subsystem acquires the lock, runs the git pipeline,
  and a new commit appears at
  `https://github.com/c0manch3/0xone-vault` containing exactly that
  one file.
- **AC#4 — empty-diff no-op.** A cron tick where vault has zero
  changes since the last push exits silently: no commit, no push,
  no Telegram message, debug log only.
- **AC#5 — divergence fails fast.** If the remote `main` is moved
  out-of-band (e.g. owner force-pushes from another host — never
  expected, but tested), the next cron tick attempts a push, sees
  `non-fast-forward`, logs `event=vault_sync_diverged`, sends one
  Telegram notify (cooldown empty), and does NOT modify the local
  repo state.
- **AC#6 — Telegram cooldown.** While the divergence persists, the
  next cron tick within 24 h of the previous notify still attempts
  the push (logs the failure) but does NOT send a second Telegram
  message. After 24 h, the next failed tick does notify again.
- **AC#7 — `enabled=false` is fully dormant.** With
  `VAULT_SYNC_ENABLED=false`: no schedule row gets inserted, no
  @tool registers, the dispatcher's `system:vault_sync` branch is
  unreachable from any cron row, and zero Telegram notifies fire.
  Daemon behaviour matches phase-7-shipped baseline byte-for-byte.
- **AC#8 — credential isolation.** `git ps -ocomm,etime,args` (or
  equivalent `/proc` inspection) on the VPS during a vault-sync push
  shows `GIT_SSH_COMMAND` env containing the dedicated deploy key
  path. The host's `~/.ssh/id_*` default keys are NOT present in
  the env. (Verified once via owner shell session, not in CI.)
- **AC#9 — no `gh` CLI dependency.** `grep -R "\bgh \b" src/assistant/vault_sync/`
  returns zero matches. The whole subsystem talks to GitHub via
  pure git over SSH.
- **AC#10 — phase-7 invariants preserved.** All phase-1 through
  phase-7 acceptance criteria still pass on the VPS smoke test:
  ping, memory write/search/list/get/delete/seed, skill-installer,
  scheduler add/list/remove/run, file/photo/voice/URL ingestion.
  Vault-sync phase introduces no regression in the existing paths.
- **AC#11 — manual @tool path.** With `enabled=True` and
  `manual_tool_enabled=True`, the model has access to the
  `vault_push_now` MCP @tool. Owner says "запушь вольт" → model
  invokes the tool → tool acquires `VaultSyncSubsystem._lock` →
  runs the same git pipeline as cron → returns
  `{"ok": true, "files_changed": N, "commit_sha": "<sha>"}` to the
  model, which surfaces a confirmation to owner. Two concurrent
  invocations (cron tick firing at the same instant the model
  invokes the tool) serialise on the asyncio.Lock without
  double-push or interleaved git commands — verified via a focused
  pytest with `asyncio.gather` on two `run_once` calls and a
  subprocess-mock that asserts call ordering.

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
3. **Per-file commit messages.** All changes since the last push
   collapse into one commit with a templated message. Per-note
   commit attribution would require model cooperation on every
   `memory_write`; not worth the complexity.
4. **Encrypted vault (git-crypt / age).** Vault content is plain
   markdown on a private repo. Encryption is a separate axis of
   protection that can be layered on later without changing the
   sync pipeline.
5. **Multiple vault remotes.** The `repo_url` field is a single
   string. Mirror to a second remote (e.g. self-hosted Gitea) is a
   future phase if ever needed.
6. **Auto-rebase on divergence.** Fail-fast is the policy.
   Auto-rebase risks silent history rewrites. Owner manually
   resolves on the rare occasion this happens.
7. **Conflict UI in Telegram.** When divergence happens, the
   Telegram notify is plaintext ("vault sync failed: remote
   diverged. Manual recovery needed."). No inline-keyboard
   "force push?" buttons. Manual SSH session is the recovery path.
8. **Per-skill `allowed-tools` enforcement on `vault_push_now`.**
   Phase 8 ships the @tool unconditionally available to the
   model when `manual_tool_enabled=True`. A later phase that
   introduces per-skill MCP tool gating can scope this tool to
   specific skills; not now.

---

> **Phase-7 integration note (vault vs media).** Phase 6a–6c put
> inbox/outbox media under `<data_dir>/media/` with retention sweep.
> Vault is the separate hierarchy `<data_dir>/vault/`. Phase 8's
> `git add` runs from inside `<data_dir>/vault/` — the working tree
> is the vault dir itself, so `data/media/`, `data/run/`, and the
> SQLite DB are physically outside the working tree and cannot be
> staged. There is no path-isolation test needed (the geometry is
> the test).
