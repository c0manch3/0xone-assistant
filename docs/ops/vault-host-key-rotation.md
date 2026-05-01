# Vault host-key rotation runbook

> Phase 8 (W2-H5). Trigger: GitHub rotates its SSH host keys (rare —
> the last rotation was March 2023 after a leaked key). Symptom: every
> vault sync push fails with `Host key verification failed.` and the
> daemon emits `event=vault_sync_host_key_mismatch` once at startup,
> then force-disables vault sync for the process lifetime (AC#26).

## When this runbook applies

- `journalctl -u 0xone-assistant` (or `docker compose logs
  0xone-assistant`) shows
  `event=vault_sync_host_key_mismatch` AT BOOT and the loop is not
  spawned, OR
- A vault sync push attempt fails with stderr containing `Host key
  verification failed` or `REMOTE HOST IDENTIFICATION HAS CHANGED`,
  AND
- GitHub announced (or you found via `gh api meta`) a host-key change
  on its docs / status page.

If the daemon force-disabled itself, all phase 1..6e features still
work (`/ping`, `memory_*`, `schedule_*`, file uploads, photo,
voice/audio, subagent). Only `vault_push_now` and the cron loop are
suppressed.

## Procedure

1. Verify GitHub's current host keys:
   ```bash
   gh api meta | jq -r '.ssh_keys[]' | sed 's/^/github.com /'
   ```
   This should print three lines (ed25519 + ecdsa + rsa).

2. Replace `deploy/known_hosts_vault.pinned` with the fresh output:
   ```bash
   gh api meta | jq -r '.ssh_keys[]' | \
     sed 's/^/github.com /' > deploy/known_hosts_vault.pinned
   git diff deploy/known_hosts_vault.pinned   # eyeball the diff
   ```

3. Commit + push:
   ```bash
   git add deploy/known_hosts_vault.pinned
   git commit -m "phase 8: rotate GitHub pinned host keys"
   git push
   ```

4. Wait for CI to publish a fresh image, then on the VPS:
   ```bash
   sudo -u 0xone /opt/0xone-assistant/deploy/scripts/vault-bootstrap.sh
   ```
   The script's step 2 re-copies the updated pinned file to
   `~/.ssh/known_hosts_vault`.

5. Restart the daemon:
   ```bash
   cd /opt/0xone-assistant/deploy/docker
   docker compose restart
   ```

6. Tail the logs to confirm the loop comes back online:
   ```bash
   docker compose logs --tail 100 0xone-assistant | grep vault_sync
   ```

   Expected lines (in order): `vault_sync_startup_check_ok`,
   `vault_sync_pushed` (or `vault_sync_no_changes`), and the audit row
   in `~/.local/share/0xone-assistant/run/vault-sync-audit.jsonl`.

## Why a rotation is rare

GitHub's host keys are stable for years. The last rotation was after a
leaked private key in March 2023. We pin the keys (W2-H4) to prevent
TOFU-acceptance of an attacker's key in a man-in-the-middle scenario;
the cost is this runbook on the rare occasion GitHub rotates.
