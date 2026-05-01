"""Phase 8 ``vault_push_now`` @tool shared helpers.

TRUSTED in-process glue — NOT ``@tool``-decorated. The single ``@tool``
handler in :mod:`assistant.tools_sdk.vault` reads the configured
:class:`~assistant.vault_sync.subsystem.VaultSyncSubsystem` reference
through this module's ``_CTX`` dict. Mirrors the
``_subagent_core`` / ``_scheduler_core`` / ``_memory_core`` pattern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from assistant.vault_sync.subsystem import VaultSyncSubsystem

# ---------------------------------------------------------------------------
# Module context (populated by configure_vault)
# ---------------------------------------------------------------------------
_CTX: dict[str, object] = {}
_CONFIGURED: bool = False


def configure_vault(*, subsystem: VaultSyncSubsystem) -> None:
    """Idempotent one-shot configuration.

    Re-calling with a different subsystem instance raises
    :class:`RuntimeError` — ``_CTX`` would otherwise fall out of sync
    with the daemon's actual state. Tests must call
    :func:`reset_vault_for_tests` between configs.
    """
    global _CONFIGURED
    if _CONFIGURED:
        cur = _CTX.get("subsystem")
        if cur is not subsystem:
            raise RuntimeError(
                "configure_vault re-called with different subsystem"
            )
        return
    _CTX["subsystem"] = subsystem
    _CONFIGURED = True


def reset_vault_for_tests() -> None:
    """Test-only: drop module state so successive tests can reconfigure."""
    global _CONFIGURED
    _CTX.clear()
    _CONFIGURED = False


def get_configured_subsystem() -> VaultSyncSubsystem | None:
    """Return the configured subsystem or ``None`` if
    :func:`configure_vault` has not been called yet (i.e. the daemon
    booted with vault sync disabled)."""
    if not _CONFIGURED:
        return None
    sub = _CTX.get("subsystem")
    if sub is None:
        return None
    # Late-bound import to avoid a circular at module load.
    from assistant.vault_sync.subsystem import VaultSyncSubsystem

    if isinstance(sub, VaultSyncSubsystem):
        return sub
    return None
