"""Phase 8 §2.7 — Telegram edge-trigger notify wrapper.

The state-machine logic itself (``ok→fail``, ``fail→fail`` silent,
milestones, ``fail→ok`` recovery) lives in
:class:`assistant.vault_sync.subsystem.VaultSyncSubsystem` so the
state file is the single source of truth. This module is a thin
translation layer from "the state machine decided to notify" to "send
the corresponding Russian message via the messenger adapter".

The adapter is the abstract :class:`~assistant.adapters.base.MessengerAdapter`
so tests can swap a fake. ``send_text`` errors are caught + logged but
NEVER re-raised — a Telegram outage must not crash the vault sync
loop (the sync itself is independent of the notify path).

Fix-pack F9 (devops HIGH): every adapter call is wrapped in
``asyncio.wait_for(..., timeout=NOTIFY_TIMEOUT_S)`` so a slow Telegram
backend (HTTPX hangs, network blip) cannot stall the cron loop. The
caller already invokes notify OUTSIDE ``self._lock`` so a timeout
during notify never blocks ``memory_write``; this module's job is to
ensure even the OUTER notify task itself is bounded.
"""

from __future__ import annotations

import asyncio
import contextlib

from assistant.adapters.base import MessengerAdapter
from assistant.logger import get_logger

log = get_logger("vault_sync.notify")

# F9: hard ceiling on a single Telegram send_text call. The adapter
# layer already retries internally; 10s is generous against typical
# httpx p95 (2-3s) and surfaces a real outage promptly.
NOTIFY_TIMEOUT_S: float = 10.0


async def _send_with_timeout(
    adapter: MessengerAdapter,
    chat_id: int,
    text: str,
) -> None:
    """F9: bounded ``send_text`` invocation. Suppresses TimeoutError
    AND any other exception (Telegram failures are best-effort —
    notify path must never crash the cron loop)."""
    with contextlib.suppress(TimeoutError, Exception):
        await asyncio.wait_for(
            adapter.send_text(chat_id, text),
            timeout=NOTIFY_TIMEOUT_S,
        )


async def notify_failure(
    adapter: MessengerAdapter | None,
    owner_chat_id: int,
    error: str,
) -> None:
    """Send the ``vault sync failed: <error>`` Telegram message.

    Called only on the ``ok→fail`` edge transition (NOT on every
    consecutive failure — milestone notifies use
    :func:`notify_milestone` instead).
    """
    if adapter is None:
        log.debug("vault_sync_notify_failure_no_adapter")
        return
    text = f"⚠️ vault sync failed: {error}"
    await _send_with_timeout(adapter, owner_chat_id, text)
    log.info("vault_sync_notify_failure_sent", error=error[:200])


async def notify_milestone(
    adapter: MessengerAdapter | None,
    owner_chat_id: int,
    consecutive_failures: int,
) -> None:
    """Send the ``vault sync still failing — N consecutive failures``
    milestone Telegram message.

    Fires when ``consecutive_failures`` matches one of the configured
    milestones (5/10/24 by default).
    """
    if adapter is None:
        log.debug("vault_sync_notify_milestone_no_adapter")
        return
    text = (
        f"⚠️ vault sync still failing — {consecutive_failures} "
        "consecutive failures"
    )
    await _send_with_timeout(adapter, owner_chat_id, text)
    log.info(
        "vault_sync_notify_milestone_sent",
        consecutive_failures=consecutive_failures,
    )


async def notify_recovery(
    adapter: MessengerAdapter | None,
    owner_chat_id: int,
    prev_failures: int,
) -> None:
    """Send the ``vault sync recovered after N consecutive failures``
    Telegram message.

    Fires only on the ``fail→ok`` edge transition.
    """
    if adapter is None:
        log.debug("vault_sync_notify_recovery_no_adapter")
        return
    text = (
        f"✅ vault sync recovered after {prev_failures} "
        "consecutive failures"
    )
    await _send_with_timeout(adapter, owner_chat_id, text)
    log.info(
        "vault_sync_notify_recovery_sent",
        prev_failures=prev_failures,
    )
