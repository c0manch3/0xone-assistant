# Phase 5b scheduler — DevOps / Ops review

Reviewer: senior DevOps (single-user bot context, VPS deployment).
Scope: ops readiness of the uncommitted phase-5b scheduler on top of a
VPS where phase-5a migration is also uncommitted (commit `2a57b5d` is
the live tip on both Mac and VPS).

---

## Executive summary

The scheduler subsystem is engineered to a noticeably higher ops bar
than typical phase-N code in this project: supervised spawn with rolling
backoff, a tamper-evident audit log at 0o600, explicit boot
classification with a crash-safe marker, and per-trigger dead-letter
plus one-shot owner notify. The patterns (classify boot → clean-slate
`sent` → unlink marker → supervised spawn) are correct and ordered
defensively.

What is missing is not the code — it is the **ops wrapper**. The new
env surface (10 `SCHEDULER_*` knobs) is not in `.env.example`; no
scheduler section exists in any runbook; systemd unit has no
`TimeoutStopSec`, so SIGKILL on stop risks losing the
`.last_clean_exit` marker write and spuriously mis-classifying the
next boot as `suspend-or-crash` (which triggers an unwanted catchup
recap — user-visible failure mode). `assistant.db` is now holding
both turns and schedule state yet has no documented backup path.
These are all small, easily-fixed gaps.

**Ops-readiness verdict: Needs-polish.** Do NOT ship this phase to VPS
without the four runbook/config items listed in §Required pre-deploy.
The code itself is green.

---

## Ops-readiness verdict

| Area | Verdict | Notes |
| --- | --- | --- |
| Startup sequence | Ready | Order and failure modes look correct. |
| Operational surface (files/env) | Needs-polish | `.env.example` + runbook missing. |
| Observability | Ready | Structlog JSON events are well-named and cover the state machine. |
| Deploy process | Needs-polish | `TimeoutStopSec` + marker-write race. |
| Backup | Needs-polish | `assistant.db` now contains schedules; no documented backup. |
| Disaster recovery | Needs-polish | No runbook entry for scheduler failure modes. |
| Monitoring / alerting | Ready | One-shot Telegram notify on dead-letter and respawn exhaustion is appropriate for single-user. |
| Upgrade path | Ready | Migration 0003 is idempotent and behind `user_version`. |
| Secret handling | Ready | No new secrets. Prompt content in DB is a phase-8+ concern. |
| systemd unit | Needs-polish | `TimeoutStopSec` missing; no failure-alert hook. |

**Overall: Needs-polish.** Four of ten categories have discrete,
small gaps. None is a code bug; all are ops artefacts the coder
skipped because the plan did not enumerate them.

---

## Findings by category

### 1. Startup sequence — Ready

Order in `main.py:243-327` is correct:

1. `SchedulerStore` constructed on the shared `assistant.db`.
2. `configure_scheduler` populates MCP `_CTX` before any tool invocation.
3. `classify_boot` reads `.last_clean_exit` mtime (M2.6).
4. `unlink_clean_exit_marker` only AFTER classification (M2.7) — a
   later restart at T+10 min is correctly classed as suspend-or-crash
   instead of clean.
5. `clean_slate_sent` reverts orphan `sent` rows (any `sent` row must
   be orphan since the `.daemon.pid` flock guarantees no concurrent
   daemon — good invariant).
6. `count_catchup_misses` only consulted if boot was NOT clean-deploy.
7. `adapter.start()` before the supervised spawn, so the first trigger
   has a live Telegram client to emit to.

Failure modes worth noting (not bugs — just operational truth):

- **`assistant.db` WAL locked by another process**: the `_acquire_singleton_lock`
  flock at `main.py:48-90` already catches a concurrent 0xone-assistant
  daemon. It does NOT catch another process (e.g. `sqlite3` CLI opened
  by the owner for diagnostics) holding a write lock. The default
  `PRAGMA busy_timeout=5000` in `state/db.py:31` gives us 5s of retry;
  beyond that, `_apply_0003` will raise `sqlite3.OperationalError` and
  crash boot. Owner-facing action: don't hold a writing sqlite session
  during restart. Document in runbook.
- **`<data_dir>` not writable**: caught by the explicit `mkdir(parents=True, exist_ok=True)` at
  the lock acquisition step (line 61). `assistant.db` open also
  creates its parent (`db.py:27`). If the filesystem is read-only we
  will raise `OSError`; daemon crashes with a traceback. Not
  catastrophic — systemd `Restart=on-failure` will loop until the FS
  is fixed. Consider wrapping scheduler `configure_scheduler`
  specifically with a targeted error like `_memory_mod.configure_memory`
  at `main.py:199-211` does (exit code 4 with a hint). Minor polish;
  not blocking.

`_spawn_bg_supervised` policy (5s backoff, 3 crashes/hr → one-shot
Telegram notify + permanent down) is sound. Subtle point: the
supervisor itself is registered as a regular bg task in
`_bg_tasks`, so `stop()` cancels it cleanly. Good.

### 2. Operational surface — Needs-polish

**New files under `<data_dir>`:**

| File | Mode | Rotation | Notes |
| --- | --- | --- | --- |
| `scheduler-audit.log` | 0o600 | None (phase-9 debt) | ~500 B/event; owner-disclosed ~50 events/day ≈ 10 MB/year. Low urgency. |
| `.last_clean_exit` | default (0o644 on tmp+rename) | N/A | Created at stop, unlinked at next boot. |

Two gaps worth calling out:

- **`.last_clean_exit` permissions**: `write_clean_exit_marker`
  (`store.py:502-511`) writes via `tmp.write_text` and `os.replace`.
  Neither sets mode; the default umask applies (0o644 on typical Mac
  and Ubuntu). Audit log and vault are 0o600; why is the boot marker
  world-readable? Low severity — the marker contains only `ts` +
  `pid` (no secrets) — but the directory convention is "everything
  under `<data_dir>` is owner-only." Recommend `os.chmod(marker, 0o600)`
  after the rename.
- **Schema 0003 co-tenancy**: `schedules` and `triggers` live in the
  same `assistant.db` as `conversations` and `turns`. The plan
  justified this (§C, low write rate, fewer files to back up). Ops
  implication: a corrupt `assistant.db` now kills BOTH the chat
  history and the schedules. DR path (§5 below) must reflect this.

**Env vars — CRITICAL runbook gap:**

Ten new `SCHEDULER_*` env vars exist in `config.py:73-103`. ZERO
of them are documented in `.env.example`. An operator (including
the owner) doing `grep SCHEDULER_ .env*` finds nothing. The
runbook at `plan/phase4/runbook.md` §2/§10 documents the memory
surface exhaustively; scheduler needs the same treatment.

Missing entries (all should be commented-out defaults in `.env.example`):

```
# -- Phase 5 scheduler (optional) --
# SCHEDULER_ENABLED=true
# SCHEDULER_TICK_INTERVAL_S=15
# SCHEDULER_TZ_DEFAULT=UTC
# SCHEDULER_CATCHUP_WINDOW_S=3600
# SCHEDULER_DEAD_ATTEMPTS_THRESHOLD=5
# SCHEDULER_SENT_REVERT_TIMEOUT_S=360   # MUST be > CLAUDE_TIMEOUT (300)
# SCHEDULER_DISPATCHER_QUEUE_SIZE=64
# SCHEDULER_MAX_SCHEDULES=64
# SCHEDULER_MISSED_NOTIFY_COOLDOWN_S=86400
# SCHEDULER_MIN_RECAP_THRESHOLD=2
# SCHEDULER_CLEAN_EXIT_WINDOW_S=120
```

The `sent_revert_timeout_s < claude.timeout` warning (`config.py:131-153`)
is great — but it's a runtime log line. Put the invariant next to the
env var in `.env.example` so nobody sets `SCHEDULER_SENT_REVERT_TIMEOUT_S=60`
while forgetting `CLAUDE_TIMEOUT=300`.

### 3. Observability — Ready

Structlog event coverage is solid. I audited every `_log` call site
in `src/assistant/scheduler/`:

- **Tick-level**: `scheduler_loop_tick_error` (exception), `scheduler_cron_parse_error`, `scheduler_tz_unknown`, `scheduler_queue_saturated`.
- **Sweep**: `scheduler_sent_expired_reverted`.
- **Dispatch**: `scheduler_dispatch_dedup`, `scheduler_dispatch_dropped_disabled`, `scheduler_dispatch_error`, `scheduler_adapter_send_failed`, `scheduler_dispatch_acked`, `scheduler_dead_notify_failed`.
- **Boot**: `boot_classified` (`main.py:263-267`), `orphan_sent_reverted`.
- **Marker**: `clean_exit_marker_unlink_failed`.

All are JSON-structured with stable field names (`trigger_id`,
`schedule_id`, `attempts`, `error`). Together they answer "why didn't
schedule X fire at 9am?":

```bash
journalctl --user -u 0xone-assistant -S '9:00' -U '9:30' --no-pager \
  | jq -c 'select(.event | startswith("scheduler_"))'
```

**Missing happy-path events I'd recommend adding** (phase-6 polish, not
blocking):

- `scheduler_trigger_materialized` (every successful `try_materialize_trigger`):
  currently silent; only failures log. An owner tailing journald during
  smoke sees nothing between 09:00 boot and 09:00:15 firing.
- `scheduler_tick_ok` at DEBUG every N ticks for liveness (rate-limited;
  never spam).

These are quality-of-life; the existing event set is sufficient for
post-hoc diagnosis.

### 4. Deploy process — Needs-polish

Current deploy path (Mac → VPS):

```
# Mac:
git add -A && git commit -m "..." && git push origin main
# VPS (0xone@193.233.87.118):
cd /opt/0xone-assistant && git pull
uv sync
systemctl --user restart 0xone-assistant
```

**Concerns:**

- **No new runtime deps** — good; `uv sync` will be a no-op on lock
  terms (verify `uv.lock` actually stayed identical after the phase-5b
  branch; if the coder ran `uv sync` locally with any transitive
  resolution jitter, `uv.lock` may have drifted).
- **Restart drops in-flight message**: `systemctl restart` sends
  SIGTERM (caught by `asyncio`'s signal handler in `main.py:518-519`,
  which sets `stop_event`). The async `stop()` sequence has no
  TimeoutStopSec cap: if the in-flight Claude turn takes 60s to
  settle, systemd waits the default 90s before SIGKILL. That's
  survivable — the clean-exit marker writes at the top of `stop()`
  (`main.py:465-468`) before cancellation, so even a 90s+ hang, if
  SIGKILL-ed, STILL leaves the marker in place and classifies the
  next boot correctly.
- **BUT** — `write_clean_exit_marker` uses `tmp.write_text` + `os.replace`
  which can itself block if the FS is sluggish. If the owner does
  `systemctl --user stop --now` (forcibly short timeout), the rename
  could be SIGKILL-interrupted between `tmp.write_text` and
  `os.replace`, leaving `.last_clean_exit.tmp` as garbage and NO
  final marker — next boot classifies as `suspend-or-crash` and fires
  a spurious recap. Mitigation: add explicit `TimeoutStopSec=30s` to
  the systemd unit so the owner gets a predictable shutdown window,
  AND have boot-time classification sweep stale `.tmp` siblings.

- **Zero-downtime not a goal** — single-user bot, fine to lose one
  in-flight user message during restart. Noted.

### 5. Backup — Needs-polish

- **Vault** — phase-4 runbook §3 handles this via rsync + git (daily
  commit planned for phase 7). Unchanged.
- **`scheduler-audit.log`** — derived/ephemeral; safe to ignore (but
  explicitly note in phase-7 `.gitignore` for the vault commit; nobody
  wants an operational log committed to a memory vault).
- **`assistant.db`** — this is the new ops liability. Previously
  `assistant.db` held only `conversations` + `turns` (chat history —
  losable). Now it holds `schedules` + `triggers` (owner-authored
  intent — NOT losable without data loss). Corruption recovery has
  no rebuild path; there is no vault-backed source of truth for
  schedules.

Recommended runbook entry:

```bash
# Daily scheduled-state snapshot. sqlite3 .backup is safe vs a live
# WAL-mode DB (writer blocks briefly; no torn pages).
DATA=~/.local/share/0xone-assistant
SNAP=~/Backups/0xone-db
mkdir -p "$SNAP"
sqlite3 "$DATA/assistant.db" ".backup '$SNAP/assistant-$(date +%F).db'"
# Retention: keep 7 daily + 4 weekly.
find "$SNAP" -name 'assistant-*.db' -mtime +30 -delete
```

On VPS, hook this into a systemd user timer (`assistant-db-backup.timer`)
and verify in the runbook with `systemctl --user list-timers`.

### 6. Disaster recovery — Needs-polish

- **Accidental `systemctl --user stop` mid-fire** — trigger in `sent`
  state. Next boot `clean_slate_sent` reverts (`main.py:268-270`;
  `store.py:359-373`). The catchup sweep then decides whether to
  re-fire based on `scheduled_for` age and `catchup_window_s=3600s`.
  Worst case: an owner who runs `systemctl stop` at 09:00:07 and
  `start` at 09:10:00 will see the 09:00 trigger re-fire at 09:10:15
  (tick interval 15s). That's intentional at-least-once; the
  dispatcher's LRU dedup (256 slots) catches accidental double-fire
  within the same process lifetime only — NOT across restart. Owner
  UX: "reminder arrived 10 minutes late." Acceptable; document.
- **DB corruption** — `memory-index.db` has a rebuild path. `assistant.db`
  does not. Recovery:
    1. Stop daemon (`systemctl --user stop 0xone-assistant`).
    2. `sqlite3 assistant.db "PRAGMA integrity_check"` → if OK the
       issue is elsewhere.
    3. Restore from the `~/Backups/0xone-db` snapshot: `cp ...latest... assistant.db`.
    4. If no backup exists, `sqlite3 .recover` into a fresh file:
       `sqlite3 assistant.db ".recover" | sqlite3 assistant-recovered.db`.
    5. Accept the data-loss window between last snapshot and crash.
- **Timezone misconfig** — if the owner adds a schedule with
  `tz=Europe/Moscow` but the VPS has `timedatectl` set to
  `Etc/UTC` (typical for fresh Ubuntu), the scheduler is STILL
  correct: `SchedulerLoop._tick_once` at `loop.py:136` constructs a
  `ZoneInfo(sch["tz"])` and `is_due` uses THAT — not the OS clock.
  The only OS-clock dependency is `datetime.now(UTC)` which works
  on any machine (UTC is always available). Verify the VPS has
  `zoneinfo` tzdata installed (`python -c 'from zoneinfo import ZoneInfo; print(ZoneInfo("Europe/Moscow"))'`).
  On Ubuntu 24.04 this should be pre-installed via the `tzdata`
  apt package, but the runbook should say "verify."

### 7. Monitoring / alerting — Ready

One-shot Telegram notifications fire on two ops events:

- **Dead-letter** after `dead_attempts_threshold=5` consecutive
  failures (`dispatcher.py:163-180`).
- **Supervised-task respawn exhaustion** after 3 crashes in one hour
  (`main.py:379-388`).

Both use best-effort `adapter.send_text` wrapped in
`contextlib.suppress(Exception)`/try-except — Telegram being down
doesn't crash the daemon. Good.

No Prometheus, no external monitoring. Appropriate for single-user.

One gap: **systemd unit failure** itself (daemon crashed, supervisor
gave up, service stays down). Owner won't be notified unless they
manually `systemctl --user status`. Phase-9 polish: add an
`OnFailure=` unit that posts to Telegram, or use `healthchecks.io`
via a periodic cron — either is a 30-line addition.

### 8. Upgrade path — Ready

Migration 0003 is idempotent and version-gated. Future phase-6
(e.g. `schedule_metadata` for description text) would be `_apply_0004`
with the same pattern. No concerns.

### 9. Secret handling — Ready

No new secrets in phase 5b. `.env` is unchanged. Scheduled-prompt
content stored in `schedules.prompt` is unencrypted at rest — a
privileged reader of the VPS disk sees it. This is consistent with
vault contents and `conversations.content_json`. Phase-8+ concern
(disk encryption or app-level encryption if threat model changes).

### 10. systemd unit review — Needs-polish

Current unit from `reference_vps_deployment.md`:

```ini
[Unit]
Description=0xone-assistant Telegram bot daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/0xone-assistant
ExecStart=/home/0xone/.local/bin/uv run python -m assistant
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal
Environment=PATH=/home/0xone/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=default.target
```

**Recommended additions** (phase-5b-critical):

```ini
# Guarantee the daemon's graceful-stop sequence completes. Without
# this, systemd's default 90s SIGKILL may pre-empt .last_clean_exit
# marker write and cause a spurious suspend-or-crash classification
# on the next boot (which emits an unwanted catchup recap).
TimeoutStopSec=30s

# Belt-and-braces hardening (cheap and orthogonal):
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/home/0xone/.local/share/0xone-assistant
  /home/0xone/.config/0xone-assistant
  /home/0xone/.claude
  /opt/0xone-assistant
ProtectHome=false   # we deliberately need ~/.local and ~/.config
```

(The `Protect*` hardenings are optional polish — phase-9 territory —
but `TimeoutStopSec` is phase-5b-essential because it couples directly
to the `.last_clean_exit` marker contract the scheduler relies on.)

---

## Recommended runbook items (create `plan/phase5/runbook.md`)

Structure mirroring phase-4 runbook:

1. **Data dir layout** — list new artifacts (`scheduler-audit.log`,
   `.last_clean_exit`, new tables in `assistant.db`).
2. **Env var reference** — the 10 `SCHEDULER_*` knobs with defaults,
   units, and the `sent_revert_timeout_s > claude.timeout` invariant.
3. **systemd unit additions** — document `TimeoutStopSec=30s` rationale.
4. **Backup / DR** — daily `sqlite3 .backup` for `assistant.db`;
   recovery recipe; what "scheduler lost intent" looks like.
5. **Diagnostics**:
     - `journalctl --user -u 0xone-assistant -S <when> | jq 'select(.event|startswith("scheduler_"))'` — the canonical "why didn't X fire?" query.
     - `sqlite3 assistant.db "SELECT id, cron, enabled, last_fire_at FROM schedules"`
     - `sqlite3 assistant.db "SELECT id, status, attempts, scheduled_for, last_error FROM triggers ORDER BY id DESC LIMIT 20"`
     - `tail -f scheduler-audit.log | jq .`
6. **Known operational quirks**:
     - Restart during a fire may re-deliver with up to `tick_interval_s`
       (15s default) of latency.
     - `@daily` / `@weekly` cron aliases are rejected (see SKILL.md).
     - DST spring-skip minutes fire zero times; DST fall-fold minutes
       fire once at fold=0.
7. **Recovery playbook**:
     - Scheduler completely silent → check `systemctl --user status`
       for `bg_task_giving_up` events.
     - All schedules paused unexpectedly → check `audit.log` for
       unauthorised `schedule_disable` calls.
     - Boot emits spurious recap → verify `.last_clean_exit` marker
       was written during last stop (mtime check); TimeoutStopSec
       too low is the likely culprit.

---

## Phase-6+ prerequisites

Before the next phase ships, the following ops artefacts must exist
to prevent silent drift:

1. **`plan/phase5/runbook.md`** covering the items above.
2. **`.env.example`** updated with the 10 `SCHEDULER_*` entries.
3. **systemd unit** updated on VPS with `TimeoutStopSec=30s` AND the
   same unit file committed somewhere reproducible (e.g. `deploy/systemd/0xone-assistant.service`).
   Right now the unit exists only on VPS disk; a VPS rebuild needs
   owner to remember every line.
4. **Backup timer** — either documented manually OR wired as a
   `.timer` unit under `~/.config/systemd/user/`.
5. **`.last_clean_exit` chmod** — change `write_clean_exit_marker` to
   `0o600` to match the rest of `<data_dir>`.

None is blocking the code-level review of phase 5b itself; all are
blocking a safe VPS deploy of the scheduler.

---

## Positive observations

- Boot classification with marker `mtime` (M2.6) and unlink-after-classify
  (M2.7) is genuinely thoughtful — an easy class of bugs (ancient
  leftover markers masquerading as clean exits) is pre-empted.
- Supervised spawn with one-shot Telegram notify on exhaustion is
  the right ops UX for a single-user bot.
- Scheduler audit log uses the exact same pattern as memory audit
  (0o600, JSONL, `_truncate_strings`, `content_len` meta) — operator
  muscle memory transfers directly.
- Prompt snapshot into `triggers.prompt` (CR2.2) means mid-flight
  `UPDATE schedules SET prompt=...` cannot corrupt an in-flight
  delivery. This is a correctness property with ops implications:
  the owner can safely edit a schedule while a trigger is mid-
  dispatch, which matters for a tool the owner will hand-edit.
- Singleton flock protects `assistant.db` across BOTH daemon
  restart races AND "oops I forgot a `pkill -f` on the old host"
  dual-daemon scenarios — important during VPS↔Mac cutover drills.
