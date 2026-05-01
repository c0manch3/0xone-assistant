"""Phase 8: vault → GitHub push-only periodic sync subsystem.

A self-contained ``asyncio`` loop owned by :class:`assistant.main.Daemon`,
NOT integrated with the phase-5b scheduler dispatcher. The subsystem
takes git status / add / commit while holding the phase-4
``vault_lock`` (fcntl), then releases that lock and runs ``git push``
under an outer ``asyncio.Lock`` so the network leg does not block
``memory_write``.

Public surface:
  - :class:`VaultSyncSubsystem` — the daemon-owned subsystem.
  - :func:`_cleanup_stale_vault_locks` — boot-time hygiene
    (mirrors phase-6a ``_boot_sweep_uploads``).
"""

from __future__ import annotations

from assistant.vault_sync.boot import _cleanup_stale_vault_locks
from assistant.vault_sync.subsystem import VaultSyncSubsystem

__all__ = ["VaultSyncSubsystem", "_cleanup_stale_vault_locks"]
