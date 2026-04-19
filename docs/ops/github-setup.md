# GitHub setup — `gh` skill + vault auto-commit (phase 8)

This playbook walks through the one-time setup required for phase-8
GitHub operations: read-only issue/PR/repo access via `gh` and the
daily vault auto-commit that pushes `<data_dir>/vault/` to a separate
GitHub account via an SSH deploy key.

All commands assume macOS / Linux with `zsh` or `bash`. Substitute
your own `ASSISTANT_DATA_DIR` if it differs from the default
`~/.local/share/0xone-assistant`.

## 1. Dedicated `vaultbot-owner` GitHub account

Create a **brand-new GitHub account** (free tier is sufficient) — for
example `vaultbot-owner` — using a throw-away email alias. This
account is **separate** from your main GitHub identity and exists
solely to own the private vault backup repo.

Rationale: blast-radius isolation. If the deploy key on the daemon
host ever leaks, the attacker gains write access to exactly **one**
private repo and no other assets on your main account (no org
memberships, no PATs, no SSH keys for other projects).

## 2. Create private repo `vaultbot-owner/vault-backup`

Log in as `vaultbot-owner` via web UI and create a **private** repo
named `vault-backup` (or whatever you set `GH_VAULT_REMOTE_URL` to).
Leave it empty — the daemon bootstraps the first commit on first run.

Do **not** add collaborators, branch protection, or required status
checks. The deploy key we add in section 3 is the sole writer.

## 3. Generate the SSH deploy key

On the host that runs the daemon, generate a fresh ed25519 keypair
dedicated to vault pushes:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_vault -N "" -C "vault@$(hostname)"
chmod 600 ~/.ssh/id_vault
chmod 644 ~/.ssh/id_vault.pub
```

Copy the **public** key:

```bash
cat ~/.ssh/id_vault.pub
```

On GitHub, go to the `vault-backup` repo → **Settings** → **Deploy
keys** → **Add deploy key**. Paste the public key and **check "Allow
write access"** (unchecked = pushes silently fail with
`remote: Write access not granted`).

Do **not** add this key to your personal SSH agent or to
`~/.ssh/config`. The daemon invokes it explicitly through
`GIT_SSH_COMMAND` with `-o IdentitiesOnly=yes` (invariant I-8.4) so
keeping it out of the default agent/config keeps the key scoped to
vault pushes only.

## 4. First-push TOFU (SF-F1) — pin GitHub's host key

**CRITICAL:** do **not** use `~/.ssh/known_hosts`. The daemon uses an
isolated `UserKnownHostsFile` under
`$ASSISTANT_DATA_DIR/run/gh-vault-known-hosts`, and TOFU state only
"sticks" if you pin the host key into the **same** file the daemon
will consult at 03:00.

```bash
mkdir -p ~/.local/share/0xone-assistant/run
ssh -i ~/.ssh/id_vault \
    -o IdentitiesOnly=yes \
    -o UserKnownHostsFile=$HOME/.local/share/0xone-assistant/run/gh-vault-known-hosts \
    -T git@github.com
```

Run this from a known-safe network. Accept the prompt when `ssh`
displays GitHub's ed25519 fingerprint. A successful auth exits with
`Hi vaultbot-owner/vault-backup! You've successfully authenticated,
but GitHub does not provide shell access.` — that is the expected
completion, not an error.

From then on, the daemon uses
`StrictHostKeyChecking=accept-new` against the same file: new hosts
get pinned once, but subsequent mismatches (key rotation, MITM) are
rejected outright. If you ever see
`REMOTE HOST IDENTIFICATION HAS CHANGED` in the daemon logs, do
**not** auto-accept — investigate whether GitHub rotated its ed25519
key or something on your network path is impersonating it.

## 5. `.env` template (SF-F3)

Add the following block to your `.env` file. **Note the variable
name is `GH_VAULT_SSH_KEY_PATH`, not `GH_VAULT_SSH_KEY`** — the
pydantic field `vault_ssh_key_path` under `env_prefix="GH_"` maps
exactly to `GH_VAULT_SSH_KEY_PATH`. The shorter name is silently
ignored by `extra="ignore"` and leaves the default in place, which
typically points at a file that does not exist on your host.

```env
# --- required ---
GH_VAULT_REMOTE_URL=git@github.com:vaultbot-owner/vault-backup.git
GH_VAULT_SSH_KEY_PATH=~/.ssh/id_vault

# --- allow-list (prevents a typo from pushing to the wrong repo) ---
GH_ALLOWED_REPOS=vaultbot-owner/vault-backup,owner/0xone-assistant

# --- optional overrides (defaults shown) ---
GH_VAULT_REMOTE_NAME=vault-backup
GH_VAULT_BRANCH=main
GH_AUTO_COMMIT_ENABLED=true
GH_AUTO_COMMIT_CRON=0 3 * * *
GH_AUTO_COMMIT_TZ=Europe/Moscow
GH_COMMIT_AUTHOR_EMAIL=vaultbot@localhost
```

`GH_VAULT_SSH_KEY_PATH` accepts `~` expansion (SF-F3 validator);
both `~/.ssh/id_vault` and `/Users/you/.ssh/id_vault` are valid.

`GH_ALLOWED_REPOS` is consulted **before** every `gh`/git subprocess
invocation (invariant I-8.5). Any `<owner>/<repo>` outside this list
exits with code 6 `repo_not_allowed` without spawning a child.

If `GH_VAULT_REMOTE_URL` is left empty, `GH_AUTO_COMMIT_ENABLED`
auto-flips to `false` at startup (Q4) and no seed row is created in
the scheduler. This is the intended behaviour for fresh installs
that have not yet configured a remote.

## 6. Main `gh auth login` (separate from vault push)

Read-only `gh issue` / `gh pr` / `gh repo` commands use your **main**
GitHub identity, not `vaultbot-owner`. Log in interactively:

```bash
gh auth login --hostname github.com --git-protocol ssh
```

Pick `Login with a web browser`; scopes `repo` + `read:org` are
sufficient. The OAuth credentials are persisted in
`~/.config/gh/hosts.yml` and read by every `gh` subprocess the
daemon spawns.

You can confirm the session with:

```bash
python tools/gh/main.py auth-status
```

Expected: `rc=0` and JSON `{"ok": true, ...}` (invariant I-8.7 — this
probe intentionally bypasses the flock, the git tree, and vault
settings so it stays safe to call from the preflight).

## 7. Deploy key rotation

Rotate the deploy key periodically (recommended: every 6–12 months,
or immediately after any suspected compromise):

```bash
# 1. Revoke the current key on GitHub:
#    repo → Settings → Deploy keys → delete the old entry.

# 2. Back up the old private key (for rollback within the rotation
#    window) and generate a new one:
mv ~/.ssh/id_vault ~/.ssh/id_vault.bak
ssh-keygen -t ed25519 -f ~/.ssh/id_vault -N "" \
    -C "vault@$(hostname) (rotated $(date +%F))"
chmod 600 ~/.ssh/id_vault
chmod 644 ~/.ssh/id_vault.pub

# 3. Upload the new public key to the repo's Deploy keys,
#    with "Allow write access" checked.

# 4. Re-run the TOFU pin from section 4 if the OpenSSH client
#    installation has changed since the last rotation.

# 5. Restart the daemon so the next push picks up the new key:
#    launchctl kickstart -k gui/$(id -u)/com.agent2.0xone-assistant
#    # or, under systemd:
#    systemctl --user restart 0xone-assistant
```

Once a push under the new key succeeds, delete `~/.ssh/id_vault.bak`.

## 8. Disable auto-commit (Q10 tombstone semantics)

If you want to pause or permanently stop the daily vault backup
without editing `.env`:

```bash
# Find the seed row:
python tools/schedule/main.py ls

# Delete it. This also inserts a "tombstone" entry (I-8.9) so that
# the next Daemon.start does NOT re-seed a fresh row — the absence
# of the schedule is owner-intentful, not an accident:
python tools/schedule/main.py rm <id>
```

To re-enable later:

```bash
python tools/schedule/main.py revive-seed vault_auto_commit
# Then restart the daemon; on startup the seed helper sees the
# tombstone has been cleared and recreates the row with the
# defaults from GitHubSettings (cron "0 3 * * *", Europe/Moscow).
launchctl kickstart -k gui/$(id -u)/com.agent2.0xone-assistant
```

To override the schedule without disabling it (e.g. run at 04:15
instead of 03:00): edit `GH_AUTO_COMMIT_CRON` in `.env` and restart
the daemon. The seed helper is idempotent — it updates the existing
row's cron/tz if they diverge from settings (partial UNIQUE INDEX on
`seed_key`, invariant I-8.6).

## 9. Encryption warning (Q12)

**Vault markdown is pushed to GitHub in plaintext.** Phase 8 does
not encrypt repository contents. The protection stack is:

1. **Private repo ACL** — only the `vaultbot-owner` account has
   read access.
2. **Dedicated GitHub account** — compromise of the deploy key does
   not cascade into your main identity (no 2FA reset surface, no
   org memberships, no other repos).
3. **Write-only deploy key scoped to one repo** — the key cannot
   read any other repo, cannot clone private forks, cannot open
   issues/PRs.

This is adequate for a single-user bot's threat model (vault
contents are personal notes, not PII/PHI/secrets). If your vault
ever needs to hold regulated data, consider layering `git-crypt` or
`age` encryption — tentatively planned for phase 10, **out of scope
for phase 8**.

Do **not** put API tokens, passwords, or seed phrases into the
vault. Use a password manager for those.

## 10. `vault_dir` topology (Q9)

`<data_dir>/vault/` is a **standalone git repo**, independent from
the main `0xone-assistant` project tree. It is bootstrapped by
`_cmd_vault_commit_push` on the first run (`git init` + remote add
+ initial commit) — invariant I-8.8 guarantees the handler never
touches `settings.project_root`.

Consequences:

- Staging is path-pinned: `git -C <vault_dir> add -A` (I-8.1)
  never captures files outside the vault. Audit with
  `git -C <vault_dir> log -p` if in doubt.
- You can replicate the vault on another machine by cloning
  directly:

  ```bash
  git clone git@github.com:vaultbot-owner/vault-backup.git \
      /Users/you/vault
  ```

  The working tree there is independent — sync is manual via
  `git pull` / `git push`. The daemon does **not** pull remote
  changes; on divergence it fails fast (exit 7, invariant I-8.3)
  and leaves the local tree for the owner to reconcile.

## 11. HOME caveat (B7) — systemd / launchd contexts

The `gh auth` session lives in `~/.config/gh/hosts.yml`. `gh`
resolves `~` via the `HOME` env var. If the daemon runs under a
different user than the one that performed `gh auth login`, or
under systemd with a stripped environment, `HOME` may point at
`/root` or `/var/empty` and `gh` will report "not logged in"
(phase-8 preflight → `gh_config_not_accessible` warning, exit 4
on the `auth-status` probe).

Fixes:

- **Preferred:** run the daemon under the same user that owns the
  shell session where `gh auth login` was performed. For
  `launchd`, this is the default — `launchctl bootstrap
  gui/$(id -u) ...` picks up the caller's `HOME`. See
  `docs/ops/launchd.plist.example`.
- **systemd (user unit):** place the unit under
  `~/.config/systemd/user/0xone-assistant.service` and enable via
  `systemctl --user enable --now 0xone-assistant`. User-mode
  systemd inherits `HOME` from the invoking user.
- **systemd (system unit, last resort):** add
  `Environment=HOME=/Users/owner` (macOS) or
  `Environment=HOME=/home/owner` (Linux) to the `[Service]`
  section. Less preferred because `gh` config updates from an
  interactive shell may not be re-read until the daemon is
  restarted.

To confirm the session is visible to the daemon process, tail
`stderr.log` around startup and look for the `gh_auth_ok` preflight
line, or shell into the same user and run
`HOME=$HOME python tools/gh/main.py auth-status`.

## 12. Manual smoke procedure (optional, v2 B-D6)

Phase-8 acceptance covers the functional path via component tests;
the end-to-end scheduler → handler → CLI → remote chain is not
exercised by a `pytest` fixture (B-D6 downgrade — see
`plan/phase8/implementation.md` §C7). If you want a hand-verified
smoke before trusting the nightly cron, run:

1. Write a marker file into the vault:

   ```bash
   echo "smoke $(date -u +%FT%TZ)" \
       > ~/.local/share/0xone-assistant/vault/smoke-$(date +%s).md
   ```

2. Invoke the CLI directly (the same binary the 03:00 cron calls):

   ```bash
   python tools/gh/main.py vault-commit-push --message "smoke test"
   ```

   Expected: `rc=0` and JSON payload

   ```json
   {"ok": true, "commit_sha": "<40-char sha>", "pushed": true, ...}
   ```

3. Verify the commit landed on GitHub via the web UI (`vaultbot-owner/vault-backup`
   → Commits) or from the command line:

   ```bash
   git ls-remote git@github.com:vaultbot-owner/vault-backup.git \
       refs/heads/main
   ```

4. Optional: shorten the cron to every minute temporarily
   (`GH_AUTO_COMMIT_CRON="* * * * *"` in `.env`, restart daemon),
   edit a vault file, wait 90 s, inspect `git log --oneline` on
   the remote. **Revert** the cron afterwards:
   `python tools/schedule/main.py rm <id>` followed by
   `python tools/schedule/main.py revive-seed vault_auto_commit`
   and daemon restart.

If step 2 exits with a non-zero code, check the mapping in
`skills/gh/SKILL.md` (`auth-not-logged`, `repo_not_allowed`,
`vault_ssh_key_missing`, `diverged`, `push_failed` etc.) — each
exit code is actionable and points at the specific config bit to
inspect.
