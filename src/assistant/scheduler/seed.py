"""Phase 8 default-seed for the ``vault_auto_commit`` schedule.

Idempotent on every Daemon boot: the critical section
(tombstone-check + find-by-seed-key + INSERT) is delegated to
``SchedulerStore.ensure_seed_row``, which wraps all three SQL ops in a
single ``BEGIN IMMEDIATE`` transaction (v2 SF-B1).

GitHubSettings-level gating (disabled / empty URL / missing ssh key)
happens **before** the transaction so we never hold a write lock while
deciding we should do nothing at all (v2 B-B1).

The partial UNIQUE INDEX ``idx_schedules_seed_key`` (migration 0005) is
the last-barrier race guard — even if two Daemons somehow bypassed both
the pidfile flock and the ``BEGIN IMMEDIATE`` transaction, the unique
index would reject the second INSERT.
"""

from __future__ import annotations

from assistant.config import GitHubSettings
from assistant.logger import get_logger
from assistant.scheduler.store import SchedulerStore

log = get_logger("scheduler.seed")

SEED_KEY_VAULT_AUTO_COMMIT = "vault_auto_commit"
SEED_PROMPT = (
    "ежедневный бэкап vault: сделай git add data/vault, коммит и git push"
)


async def ensure_vault_auto_commit_seed(
    store: SchedulerStore,
    gh: GitHubSettings,
) -> int | None:
    """Ensure a ``vault_auto_commit`` schedule exists; return its id.

    Return value:
        * ``None`` — skipped (disabled, un-configured, key missing, or
          tombstoned by the owner).
        * ``int`` — the schedule row id, whether it was just inserted or
          already present (including a soft-deleted row where
          ``enabled=0``; the caller does not resurrect it).

    Call-site contract (``Daemon.start``):
        * Must be invoked AFTER ``apply_schema`` so migrations 0005+0006
          have created the ``seed_key`` column and ``seed_tombstones``
          table.
        * Must be invoked while the pidfile flock is held so no peer
          Daemon instance can race on the seed insert.
    """
    if not gh.auto_commit_enabled:
        log.info("vault_auto_commit_seed_skipped_disabled")
        return None
    if not gh.vault_remote_url:
        log.warning("vault_remote_not_configured")
        return None
    if not gh.vault_ssh_key_path.is_file():
        log.warning(
            "vault_ssh_key_missing",
            path=str(gh.vault_ssh_key_path),
        )
        return None

    schedule_id, action = await store.ensure_seed_row(
        seed_key=SEED_KEY_VAULT_AUTO_COMMIT,
        cron=gh.auto_commit_cron,
        prompt=SEED_PROMPT,
        tz=gh.auto_commit_tz,
    )
    if action == "tombstoned":
        log.info("vault_auto_commit_seed_tombstoned_skip")
        return None
    if action == "exists":
        log.info("vault_auto_commit_seed_present", schedule_id=schedule_id)
        return schedule_id
    # action == "inserted"
    log.info(
        "vault_auto_commit_seed_created",
        schedule_id=schedule_id,
        cron=gh.auto_commit_cron,
        tz=gh.auto_commit_tz,
    )
    return schedule_id
