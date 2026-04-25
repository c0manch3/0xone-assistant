# Phase 5 scheduler — runbook

Ops reference for the single-owner Telegram-bot scheduler. Structure
mirrors `plan/phase4/runbook.md`.

Audience: the owner running the bot on their own Mac / VPS, or the
next on-call Claude session that needs to diagnose a missed fire.

---

## 1. Data dir layout

All scheduler artefacts live under `<data_dir>` (default
`~/.local/share/0xone-assistant` on Linux, `~/Library/Application
Support/0xone-assistant` on macOS via XDG lookup in `config.py`).

| Path | Purpose | Mode |
|------|---------|------|
| `assistant.db` | shared sqlite; now holds `schedules` + `triggers` tables in addition to phase-2 `conversations` / `turns` | 0o600 |
| `scheduler-audit.log` | JSONL, one line per `mcp__scheduler__*` tool invocation with truncated `tool_input` | 0o600 |
| `.last_clean_exit` | marker written at `Daemon.stop` top; read at next boot to classify `clean-deploy` vs `suspend-or-crash` | 0o600 (Fix 15) |
| `.daemon.pid` | advisory `fcntl.flock` holder; contains the PID of the running daemon | 0o600 |

No scheduler-specific files live outside `<data_dir>`. `skills/` and
`~/.claude/` are read-only from the scheduler's perspective.

---

## 2. Env var reference (`SCHEDULER_*`)

All scheduler knobs have safe defaults. Override only if a specific
operational constraint demands it. See `.env.example` for the full
commented list.

| Env var | Default | Notes |
|---------|---------|-------|
| `SCHEDULER_ENABLED` | `true` | disable to turn the loop + dispatcher off; `@tool` handlers stay accessible for inspection |
| `SCHEDULER_TICK_INTERVAL_S` | `15` | producer wake cadence; also the floor on observed fire latency |
| `SCHEDULER_TZ_DEFAULT` | `UTC` | fallback when `schedule_add` omits `tz` |
| `SCHEDULER_CATCHUP_WINDOW_S` | `3600` | max age of missed-fire replay on wake; rows older than this are not re-fired |
| `SCHEDULER_DEAD_ATTEMPTS_THRESHOLD` | `5` | `revert_to_pending` count that flips status to `dead` + sends one-shot owner notify |
| `SCHEDULER_SENT_REVERT_TIMEOUT_S` | `360` | **INVARIANT**: MUST be greater than `CLAUDE_TIMEOUT` (default 300). Violation is log-warned at runtime, not fatal — but a value ≤ claude.timeout will prematurely revert in-flight handlers and cause double-fires on the next tick. |
| `SCHEDULER_DISPATCHER_QUEUE_SIZE` | `64` | asyncio queue maxsize between loop and dispatcher |
| `SCHEDULER_MAX_SCHEDULES` | `64` | enabled-schedule cap; guards against a recursion bomb from the model |
| `SCHEDULER_MISSED_NOTIFY_COOLDOWN_S` | `86400` | reserved; recap rate-limit to be wired in phase 5c |
| `SCHEDULER_MIN_RECAP_THRESHOLD` | `2` | catchup-miss count that fires the boot-time recap notification |
| `SCHEDULER_CLEAN_EXIT_WINDOW_S` | `120` | `.last_clean_exit` mtime age that classifies a boot as `clean-deploy` |
| `SCHEDULER_RECLAIM_OLDER_THAN_S` | `30` | `reclaim_pending_not_queued` age threshold; must exceed `tick_interval_s` |

---

## 3. Process manager (Linux / VPS)

### Docker compose (phase 5d, primary)

Production stack lives at `deploy/docker/docker-compose.yml`. Two
services: `0xone-assistant` (the bot daemon, image
`ghcr.io/c0manch3/0xone-assistant:<TAG>`) and `autoheal` (sidecar
that restarts the bot on `unhealthy` status). Install/update/
rollback recipes: `deploy/docker/README.md`. Key scheduler-sensitive
settings:

- `stop_grace_period: 35s` — Docker SIGTERM grace before SIGKILL.
  Matches systemd `TimeoutStopSec=30s` + 5s docker margin so the
  `.last_clean_exit` marker write completes.
- `restart: unless-stopped` — daemon-exit recovery + host-reboot
  recovery. Does NOT restart on `unhealthy`; the autoheal sidecar
  watches the `autoheal=true` label for that.
- Healthcheck: pid + `/proc/$pid/exe` readlink (W2-C3) — eliminates
  pid-recycle false positives. `start_period: 60s` covers worst-case
  claude preflight (45s + slack).

Reload / restart recipe:

```bash
cd /opt/0xone-assistant/deploy/docker
docker compose pull        # if TAG changed in .env
docker compose up -d       # idempotent; recreates only on diff
docker compose logs -f 0xone-assistant | jq -R 'fromjson?'
```

### systemd unit (phase 5a fallback, retained)

The unit at `deploy/systemd/0xone-assistant.service` is kept disabled
on the VPS as a documented fallback. Restore via
`systemctl --user enable --now 0xone-assistant.service` after a
`docker compose stop`. Key scheduler-sensitive settings:

- `TimeoutStopSec=30s` — grants `Daemon.stop` a predictable shutdown
  window so the `.last_clean_exit` marker write completes before SIGKILL.
- `Restart=on-failure` + `RestartSec=10s` — rolling backoff for a
  supervisor-exhausted daemon.

Reload / restart recipe (systemd path):

```bash
systemctl --user daemon-reload
systemctl --user restart 0xone-assistant
journalctl --user -u 0xone-assistant -f
```

---

## 4. Backup & disaster recovery

### Daily snapshot (cron-driven)

`assistant.db` now carries owner intent (schedules + triggers), not
just ephemeral chat history. Rebuilding from memory is cheap, but
schedule recovery has no source of truth — back up daily.

```bash
DATA=~/.local/share/0xone-assistant
SNAP=~/Backups/0xone-db
mkdir -p "$SNAP"
sqlite3 "$DATA/assistant.db" ".backup '$SNAP/assistant-$(date +%F).db'"
find "$SNAP" -name 'assistant-*.db' -mtime +30 -delete
```

Wire into a systemd user timer or plain cron. `.backup` is safe vs a
live WAL-mode DB (briefly blocks writer; no torn pages).

### DB integrity check

Docker compose path (phase 5d+ default):

```bash
cd /opt/0xone-assistant/deploy/docker
docker compose stop 0xone-assistant
sqlite3 ~/.local/share/0xone-assistant/assistant.db "PRAGMA integrity_check"
# if "ok" → issue is elsewhere; if not, proceed to restore
cp ~/Backups/0xone-db/assistant-$(date +%F).db ~/.local/share/0xone-assistant/assistant.db
docker compose start 0xone-assistant
```

Systemd fallback path (if the unit was re-enabled):

```bash
systemctl --user stop 0xone-assistant
sqlite3 ~/.local/share/0xone-assistant/assistant.db "PRAGMA integrity_check"
cp ~/Backups/0xone-db/assistant-$(date +%F).db ~/.local/share/0xone-assistant/assistant.db
systemctl --user start 0xone-assistant
```

If no usable snapshot exists:

```bash
sqlite3 assistant.db ".recover" | sqlite3 assistant-recovered.db
# review schedules + triggers; promote if acceptable
```

### Clean-slate restart (nuclear option)

If the state machine is wedged (e.g., every fire dead-letters):

Docker compose path (phase 5d+ default):

1. `cd /opt/0xone-assistant/deploy/docker && docker compose stop 0xone-assistant`
2. Take a fresh snapshot (belt-and-braces).
3. `sqlite3 assistant.db "UPDATE triggers SET status='pending', attempts=0 WHERE status IN ('sent','dead')"`
   — resets orphans without losing schedule config.
4. `docker compose start 0xone-assistant`; tail
   `docker compose logs -f 0xone-assistant | jq -R 'fromjson?'` and
   `scheduler-audit.log` to verify the next tick processes cleanly.

Systemd fallback path:

1. `systemctl --user stop 0xone-assistant`
2. Take a fresh snapshot.
3. Same SQL UPDATE as above.
4. `systemctl --user start 0xone-assistant`; tail journald.

---

## 5. Diagnostics ("why didn't X fire?")

### Log structured query (Docker-era)

```bash
# Docker compose path (phase 5d+ default).
cd /opt/0xone-assistant/deploy/docker
docker compose logs --since '1h' 0xone-assistant | jq -R 'fromjson?' \
  | jq -c 'select(.event | startswith("scheduler_"))'

# systemd fallback path (if the unit was re-enabled).
journalctl --user -u 0xone-assistant -S '9:00' -U '9:30' --no-pager \
  | jq -c 'select(.event | startswith("scheduler_"))'
```

Key events (from `src/assistant/scheduler/`):

- `scheduler_loop_tick_error` — the outer tick try-except fired;
  likely a corrupt schedules row or a store-level exception.
- `scheduler_cron_parse_error` / `scheduler_tz_unknown` — per-schedule
  config error; the loop skips the row and continues.
- `scheduler_queue_saturated` — dispatcher can't drain fast enough;
  `last_error` on the row is set to `queue saturated...`; next tick's
  reclaim picks it up once the queue has room.
- `scheduler_sent_expired_reverted` — CR2.1 sweep reverted a stale
  `sent` row; a handler hung past `sent_revert_timeout_s`.
- `scheduler_dispatch_dedup` — LRU catch, likely after a `clean_slate`
  replay of an already-delivered fire.
- `scheduler_dispatch_dropped_disabled` — the model disabled the
  schedule between materialise and dispatch.
- `scheduler_dispatch_error` — handler raised (including the new
  Fix 3 `ClaudeBridgeError` re-raise on scheduler-origin turns).
- `scheduler_dispatch_empty_output` — Fix 9: handler returned without
  text; reverted to pending with retry.
- `scheduler_dispatch_acked` — happy path; `out_chars` is the payload
  size sent to the owner.
- `boot_classified` — at startup, one of `first-boot` /
  `clean-deploy` / `suspend-or-crash`.
- `orphan_sent_reverted` — `clean_slate_sent` count from boot.

### SQL quick-look

```bash
DB=~/.local/share/0xone-assistant/assistant.db
# All enabled schedules and last fire time
sqlite3 "$DB" "SELECT id, cron, tz, enabled, last_fire_at FROM schedules ORDER BY id"
# Most recent 20 triggers across the system
sqlite3 "$DB" "SELECT id, schedule_id, status, attempts, scheduled_for, substr(last_error,1,80) AS err FROM triggers ORDER BY id DESC LIMIT 20"
# Anything dead-lettered in the last 24h?
sqlite3 "$DB" "SELECT id, schedule_id, scheduled_for, last_error FROM triggers WHERE status='dead' AND julianday('now')-julianday(scheduled_for) < 1"
```

### Audit log

```bash
tail -f ~/.local/share/0xone-assistant/scheduler-audit.log | jq .
```

Each line records `tool=<name>`, `tool_input` (truncated to 2048 bytes
per field), `content_len`, `is_error`. Useful for reconstructing what
the model asked the scheduler to do and when.

---

## 6. Known operational quirks

- **15-second fire latency floor.** A schedule set to fire at 09:00:00
  may deliver anywhere in `[09:00:00, 09:00:15]` depending on tick
  alignment. This is documented behaviour, not a bug.
- **DST spring-skip minutes fire zero times.** `0 2 * * *` on
  `Europe/Moscow` on the spring-forward day skips — the 02:00
  wall-clock minute does not exist locally. The next fire is the next
  calendar day's 02:00.
- **DST fall-fold minutes fire exactly once** (fold=0). Ambiguous
  minutes during the fall-back hour resolve deterministically to the
  first occurrence.
- **@-aliases + Quartz extensions are rejected.** `@daily`, `@hourly`,
  6/7-field Quartz are all parse errors (code=1). Only 5-field POSIX
  cron with `* , - /` is accepted.
- **Restart during an in-flight fire re-delivers with up to
  `tick_interval_s` latency.** The fire is in `status='sent'` at
  restart → `clean_slate_sent` reverts to `pending` (attempts+1) →
  next tick's materialise path dedups via UNIQUE → reclaim picks it up
  if saturation-noted → dispatcher re-runs. LRU dedup within one
  process catches obvious duplicates, but post-restart LRU is empty.
  At-least-once contract: the owner may see the same fire re-delivered
  10-30 seconds late on restart.
- **`missed_notify_cooldown_s` is reserved.** Currently not consulted
  by any code path; recap notify fires on every
  non-`clean-deploy` boot where `catchup_missed >=
  min_recap_threshold`. Phase 5c will wire it.

---

## 7. Troubleshooting playbooks

### "Scheduled fire didn't arrive"

1. Confirm the schedule is enabled: `sqlite3 assistant.db "SELECT enabled FROM schedules WHERE id=?"`.
2. Check `last_fire_at` vs expectation — is it being materialised at all?
3. Tail audit log during the expected window; look for
   `mcp__scheduler__*` entries and any `scheduler_dispatch_error`.
4. Check `triggers` for the row: `SELECT * FROM triggers WHERE
   schedule_id=? AND scheduled_for LIKE '2026-04-21T09:%';`.
5. If status='dead' or attempts>=5, inspect `last_error`. Dead-letter
   + owner Telegram notify should already have fired; verify adapter
   isn't down. Docker: `docker compose ps` (Up + healthy expected).
   Systemd fallback: `systemctl --user status 0xone-assistant`.

### "Scheduler completely silent after restart"

Docker compose path (phase 5d+ default):

1. `cd /opt/0xone-assistant/deploy/docker && docker compose ps` — Up?
   Healthy? Restarting in a hot loop?
2. `docker compose logs --since 10m 0xone-assistant | jq -R 'fromjson?' | grep bg_task_giving_up`
   — supervisor exhausted? Expect a Telegram notify; absent = adapter
   also dead.
3. `sqlite3 assistant.db "PRAGMA integrity_check"` — DB corrupt?
4. Restore latest snapshot (see §4) if integrity fails.

Systemd fallback path:

1. `systemctl --user status 0xone-assistant` — is it running?
2. `journalctl --user -u 0xone-assistant | grep bg_task_giving_up` —
   supervisor exhausted?
3. Same DB integrity check + restore steps as above.

### "Boot emitted a spurious catchup recap"

1. Check marker classification:
   - Docker: `docker compose logs --since '1 min ago' 0xone-assistant
     | jq -R 'fromjson?' | grep boot_classified`.
   - Systemd: `journalctl --user -u 0xone-assistant --since '1 min ago'
     | grep boot_classified`.
   If `cls=suspend-or-crash` but you restarted cleanly, the marker
   write was pre-empted.
2. Verify shutdown grace is active:
   - Docker: `grep stop_grace_period
     /opt/0xone-assistant/deploy/docker/docker-compose.yml` (must show
     `35s`); also `cat /etc/docker/daemon.json` (host-reboot path —
     `shutdown-timeout` >= 40, see Docker README).
   - Systemd: `systemctl --user show 0xone-assistant | grep
     TimeoutStopUSec` (must show `30s`).
3. Check `.last_clean_exit` mtime vs wall clock on the PRIOR stop —
   was shutdown graceful?

### "All schedules unexpectedly disabled"

1. `grep mcp__scheduler__schedule_disable scheduler-audit.log | tail`.
2. Compare against the memory's trace of what the model intended.
3. Re-enable by calling `schedule_enable` via the tool surface, or
   directly: `sqlite3 assistant.db "UPDATE schedules SET enabled=1
   WHERE id=?"`.

---

## 8. Deferred ops items (phase 5c / later)

- Log rotation for `scheduler-audit.log` (currently unbounded; ~10 MB/yr
  on owner-disclosed volume).
- Crash-notify hook for daemon-level failures where the supervisor
  gave up. Docker (default): no equivalent today — owner must manually
  `docker compose ps`. Systemd fallback: `OnFailure=` hook unwired.
- `missed_notify_cooldown_s` wiring (crash-loop recap spam guard).
- Container hardening (phase 9): `read_only: true`, `cap_drop: [ALL]`,
  `no-new-privileges: true`, tmpfs for `/tmp`. Systemd unit equivalents
  (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`) live in the
  fallback unit at `deploy/systemd/`.
