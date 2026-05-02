"""Phase 9 ``render_doc`` @tool shared helpers.

TRUSTED in-process glue — NOT ``@tool``-decorated. The single ``@tool``
handler in :mod:`assistant.tools_sdk.render_doc` reads the configured
:class:`~assistant.render_doc.subsystem.RenderDocSubsystem` reference
through this module's ``_CTX`` dict. Mirrors the
``_vault_core`` / ``_subagent_core`` / ``_scheduler_core`` /
``_memory_core`` pattern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from assistant.render_doc.subsystem import RenderDocSubsystem

_CTX: dict[str, object] = {}
_CONFIGURED: bool = False


def configure_render_doc(*, subsystem: RenderDocSubsystem) -> None:
    """Idempotent one-shot configuration.

    Re-calling with a different subsystem instance raises
    :class:`RuntimeError` — ``_CTX`` would otherwise fall out of sync
    with the daemon's actual state. Tests must call
    :func:`reset_render_doc_for_tests` between configs.
    """
    global _CONFIGURED
    if _CONFIGURED:
        cur = _CTX.get("subsystem")
        if cur is not subsystem:
            raise RuntimeError(
                "configure_render_doc re-called with different subsystem"
            )
        return
    _CTX["subsystem"] = subsystem
    _CONFIGURED = True


def reset_render_doc_for_tests() -> None:
    """Test-only: drop module state so successive tests can reconfigure."""
    global _CONFIGURED
    _CTX.clear()
    _CONFIGURED = False


def get_configured_subsystem() -> RenderDocSubsystem | None:
    """Return the configured subsystem or ``None`` if
    :func:`configure_render_doc` has not been called yet."""
    if not _CONFIGURED:
        return None
    sub = _CTX.get("subsystem")
    if sub is None:
        return None
    from assistant.render_doc.subsystem import (
        RenderDocSubsystem,
    )

    if isinstance(sub, RenderDocSubsystem):
        return sub
    return None
