# Vault secret-leak recovery runbook

> Phase 8 (devil w1 H-2). Trigger: a secret was committed to the vault
> repo despite both layers of defence (`.gitignore` and the daemon-side
> `_validate_staged_paths` regex check). Goal: stop the bleed,
> rotate the leaked secret, and restore a clean vault history.

## Defence layers (recap)

1. `<vault>/.gitignore` excludes secret-pattern paths from `git add -A`
   so they never reach the staging area.
2. `VaultSyncSubsystem._validate_staged_paths` runs the
   `secret_denylist_regex` set against staged file names AFTER
   `git add` and BEFORE `git commit`. A match aborts the cycle with
   `result="failed"` + Telegram notify + audit row.
3. `vault-bootstrap.sh` step 5 mirrors the same regex set as a one-time
   pre-push check.

A leak past all three is rare but possible (e.g. an owner manually
running `git add -f secrets/api.env` from inside the VPS, or a
fingerprint-changing rename that beat the regex). This runbook is the
recovery path.

## When this runbook applies

- Owner inspects the GitHub repo at `https://github.com/c0manch3/0xone-vault`
  and sees a file containing real credentials (API key, PEM, .env value).
- `~/.local/share/0xone-assistant/run/vault-sync-audit.jsonl` shows
  recent `result="pushed"` rows whose `commit_sha` corresponds to the
  bad commit.

## Procedure

### 1. Stop the bleed immediately

Disable vault sync on the VPS so no further pushes happen while you
investigate:

```bash
sed -i 's/^VAULT_SYNC_ENABLED=true/VAULT_SYNC_ENABLED=false/' \
    ~/.config/0xone-assistant/.env
docker compose -f /opt/0xone-assistant/deploy/docker/docker-compose.yml restart
```

### 2. Treat the leaked secret as compromised

- Rotate the credential at the source (regenerate the API key, revoke
  the OAuth token, replace the PEM, update the `.env` value, etc.).
- Audit recent activity for any sign the credential was abused. The
  vault repo is private and only the deploy key has write access, so
  the public-leak vector is narrow — but assume worst-case and rotate
  anyway.

### 3. Revoke the deploy key + reissue

The vault deploy key is unique per-VPS-instance (one key, one
repository). Revoke it via
`https://github.com/c0manch3/0xone-vault/settings/keys` and regenerate
on the VPS:

```bash
rm ~/.ssh/vault_deploy ~/.ssh/vault_deploy.pub
sudo -u 0xone /opt/0xone-assistant/deploy/scripts/vault-bootstrap.sh
```

The bootstrap script will re-generate the keypair, prompt you to paste
the new public key into the GitHub Deploy Keys page, and proceed.

### 4. Choose your recovery shape

Two options, depending on how much history you want to keep:

**Option A — full history rewrite (recommended for serious leaks).**
Delete the GitHub repo and recreate it empty:

```bash
gh repo delete c0manch3/0xone-vault --yes
gh repo create c0manch3/0xone-vault --private \
    --description "0xone-assistant vault sync target" \
    --confirm
```

Then on the VPS, remove the bad commit locally and force a clean
push:

```bash
cd ~/.local/share/0xone-assistant/vault
# Find the offending file, delete it from the working tree.
rm path/to/leaked.env
git rm --cached path/to/leaked.env  # if it was once tracked
git add .
git commit -m "vault recovery: post-leak rebuild"
# Re-stage the deploy key on the new repo via the bootstrap script
# (step 3 — paste the public key) and re-run.
```

The vault directory itself preserves the markdown notes; only the
`.git/` history of the leaked path is gone.

**Option B — `git filter-repo` history surgery (advanced).** If you
must keep the full history (rare for a single-user vault), use
`git filter-repo --path <leaked-path> --invert-paths` to scrub the
leaked file from every commit, then `git push --force-with-lease`.
This is risky and not recommended for a single-user setup where
the vault itself is the durable artefact, not the commit log.

### 5. Reactivate vault sync

```bash
sed -i 's/^VAULT_SYNC_ENABLED=false/VAULT_SYNC_ENABLED=true/' \
    ~/.config/0xone-assistant/.env
docker compose -f /opt/0xone-assistant/deploy/docker/docker-compose.yml restart
```

Tail the logs to confirm the first tick fires cleanly:

```bash
docker compose logs --tail 100 0xone-assistant | grep vault_sync
```

### 6. Post-mortem

- Inspect the audit log to identify which `reason` the leak commit
  carried (`scheduled` / `manual`).
- If `manual`: review the conversation that triggered the
  `vault_push_now` invocation; consider tightening the skill's
  trigger phrases or temporarily setting
  `VAULT_SYNC_MANUAL_TOOL_ENABLED=false`.
- Add the leaked path pattern to
  `VaultSyncSettings.secret_denylist_regex` in
  `src/assistant/config.py` if it falls outside the existing regex
  set. Mirror the change in `deploy/scripts/vault-bootstrap.sh`'s
  `DENY_RE` and re-deploy.

## Tip — pre-existing populated vault

The bootstrap script's step 5 catches secret-pattern paths planted in
the vault dir BEFORE the initial push. If a fresh deploy fails the
denylist check at step 5, fix the offending paths in the working tree
and re-run the script (it is idempotent).
