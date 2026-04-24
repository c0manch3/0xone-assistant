---
phase: 5a
title: VPS migration — deploy on 193.233.87.118 with systemd user unit
date: 2026-04-23
status: shipped (VPS daemon live + owner smoke AC#1+AC#3 verified on VPS)
---

# Phase 5a — VPS Migration Summary

Phase 5 was split 2026-04-23 into two ships:
- **5a (this)** — infrastructure move from Mac workstation → VPS 193.233.87.118.
- **5b** — scheduler @tool MCP server (shipped together under the same branch but separate commit).

## What shipped

- **Claude OAuth session transfer** — macOS stores OAuth tokens in Keychain (`Claude Code-credentials`), Linux stores them in `~/.claude/.credentials.json`. Session JSON shape is identical across platforms. Transfer recipe:
  ```
  security find-generic-password -s 'Claude Code-credentials' -w \
    | ssh -i ~/.ssh/bot 0xone@193.233.87.118 \
          "cat > ~/.claude/.credentials.json && chmod 600 ~/.claude/.credentials.json"
  ```
  Version-match discipline: claude CLI must be the same minor version across Mac + VPS. Upgrading 2.1.81 → 2.1.116 on VPS invalidated its pre-existing session (401); fresh transfer fixed it.

- **VPS environment** — Ubuntu 24.04, user `0xone` with passwordless sudo. Python 3.12.3 pre-installed. Added `uv` (`~/.local/bin/uv`), `claude` CLI upgraded to 2.1.116 to match Mac dev.

- **Deployment layout:**
  - Code: `/opt/0xone-assistant/` (root-owned parent, `chown 0xone:0xone` on the app dir).
  - Data: `~/.local/share/0xone-assistant/` (vault, memory-index.db, assistant.db, audit logs).
  - Env: `~/.config/0xone-assistant/.env` (TELEGRAM_BOT_TOKEN, OWNER_CHAT_ID, LOG_LEVEL).
  - Seed vault rsynced from Mac: 12 notes, 8 indexable.

- **systemd user unit** `deploy/systemd/0xone-assistant.service` committed to repo:
  ```
  [Service]
  Type=simple
  WorkingDirectory=/opt/0xone-assistant
  ExecStart=/home/0xone/.local/bin/uv run python -m assistant
  Restart=on-failure
  RestartSec=10s
  TimeoutStopSec=30s       # phase 5b devops gate — ensures .last_clean_exit marker write
  ```
  Install recipe in `deploy/systemd/README.md`.

- **Long polling** — bot does NOT need external IP (aiogram outbound `getUpdates`). VPS 193.233.87.118 only needs outbound 443. No webhook, no Caddy proxy.

## Timeline

- **2026-04-23 10:14** — first VPS daemon boot; auto-reindex seed vault (8 notes); bot polling `@zeroXone_bot`.
- **2026-04-23 10:52** — owner smoke: `запомни, что у жены день рождения 3 апреля` → `inbox/wife-birthday.md` written.
- **2026-04-23 10:53** — daemon restart; AC#3 verified: `когда у жены день рождения?` → `memory_search` hit on wife-birthday.md → "3 апреля".
- **2026-04-23 14:10** — provider maintenance briefly downed VPS network; systemd auto-restarted at 15:50:43 UTC when connectivity returned.
- **2026-04-23 15:56** — bot resumed handling user traffic (`turn_complete cost=$0.21 duration=52s`).
- **2026-04-23 22:xx** — phase 5b code + fix-pack completed locally; committed together with 5a infrastructure.

## What changed in CLAUDE.md

- "No cloud deployment" → **deploys to VPS 193.233.87.118**.
- "Phases shipped: None" → reflects phases 1-4 + 5a/5b actually shipped.
- Added VPS section with path to `reference_vps_deployment.md` memory + `deploy/systemd/` pattern.

## Known ops quirks

- **Mac is dev-only** — transcription + image-gen services will stay local (owner decision). Bot daemon lives on VPS.
- **OAuth divergence risk** — if Mac `claude` CLI refreshes session (via refresh_token), VPS file becomes stale. If VPS refreshes, Mac Keychain stays stale. Single-tool deployment (only one active daemon) avoids conflict; singleton lock (`.daemon.pid`) enforces this within-host; owner discipline enforces across-host.
- **`_fs_type_check` false warning** — VPS ext4 filesystem returns `stat -f -c '%T'` = `ext2/ext3` (combined alias). Memory tool logs `memory_vault_unrecognized_fs` but writes proceed. Cosmetic only.

## References

- `reference/vps_deployment.md` memory — credentials, recipes, version discipline.
- `project/phase5a_inflight.md` memory — mid-flight state during provider outage (now superseded; this summary is the closing record).
- `deploy/systemd/README.md` — install recipe for fresh hosts.
- Phase 5b runbook (`plan/phase5/runbook.md`) — operational diagnostics that also apply to phases 1-4 on VPS.
